# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""把证据台账与 LaTeX 门控包装成模型可调用的工具。

这里是**门控真正落地的地方**。原先我打算把拦截放在框架的 ``PreToolUse`` 钩子里，
但钩子的入参与返回协议属于上游框架的内部契约，读不到就不能臆造。改在工具边界
拦截后有三个好处：

1. 拦截点在**我们自己的代码里**，契约确定、可单测、可复现；
2. 与框架解耦——钩子仍然可以加一层纵深防御，但不再是唯一的防线；
3. 失败信息直接作为工具观察值回灌给模型，模型有机会自我修正，
   而不是收到一个它看不懂的框架级拒绝。

核心不变量：**``write_latex_section`` 在门控不通过时绝不落盘**。
论文里的每个数字要么以 ``\\result{claim_id}`` 引用一个校验通过的 claim，
要么这次写入直接失败。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from paperswarm.agentloop import ToolSpec
from paperswarm.evidence import Ledger, LedgerError
from paperswarm.tex_gate import gate_strict, resolve


class GateRejected(LedgerError):
    """正文被证据门控拒绝。

    用异常而不是"返回 ``written: false``"来表达拒绝，原因是观测信号必须准确：
    返回一个正常字典会让工具调用在轨迹里显示为成功，而事实是**什么都没写成**。
    抛异常后 :class:`~paperswarm.agentloop.AgentLoop` 会把它转成结构化观察值
    回灌给模型，模型看到的是错误，轨迹里记的也是错误。
    """


def _require(arguments: Mapping[str, Any], *names: str) -> None:
    """校验必填参数，缺失时给出可执行的报错。

    为什么要自己校验而不用 schema 兜住：模型可以不看 schema 就发调用，
    而 ``arguments["x"]`` 触发的 ``KeyError`` 对模型来说是不可理解的报错
    （只有 ``'text'`` 三个字），它无法据此修正。这里明确告诉它缺了什么、
    以及该怎么补。

    Raises:
        LedgerError: 有必填参数缺失或为空。
    """
    missing = [name for name in names if not str(arguments.get(name) or "").strip()]
    if missing:
        provided = ", ".join(sorted(arguments.keys())) or "（空）"
        raise LedgerError(
            f"缺少必填参数 {missing}；本次实际收到: {provided}"
        )


def _resolve_artifact_ref(ledger: Ledger, reference: str) -> str:
    """把模型给的产物引用解析成台账里真实的 ``artifact_id``。

    模型天然倾向于用**文件路径或文件名**来指代产物，而不是我们生成的短标识。
    这不影响证据强度——只要它指向的是同一个已登记产物，来源依然被 SHA-256 钉住。
    因此这里按三级顺序解析：

    1. 精确匹配 ``artifact_id``；
    2. 匹配登记时的绝对路径（大小写与分隔符归一化后比较）；
    3. 匹配文件 basename。

    第 2、3 级都要求**唯一命中**；命中多个说明引用有歧义，直接报错并列出候选，
    而不是猜一个。

    Args:
        ledger: 证据台账。
        reference: 模型给出的引用（artifact_id / 路径 / 文件名）。

    Returns:
        解析后的 ``artifact_id``。

    Raises:
        LedgerError: 无法解析，或命中多个候选。
    """
    text = str(reference).strip()
    if not text:
        raise LedgerError("artifact 引用为空；请传 register_artifact 返回的 artifact_id")

    artifacts = ledger.artifacts()
    known_ids = [item.artifact_id for item in artifacts]
    if text in known_ids:
        return text

    def _normalize(value: str) -> str:
        return value.replace("\\", "/").rstrip("/").lower()

    target = _normalize(text)
    target_base = target.rsplit("/", 1)[-1]

    by_path = [
        item.artifact_id
        for item in artifacts
        if _normalize(Path(item.path).as_posix()) == target
        or _normalize(Path(item.path).as_posix()).endswith("/" + target)
    ]
    if len(by_path) == 1:
        return by_path[0]

    by_name = [
        item.artifact_id
        for item in artifacts
        if Path(item.path).name.lower() == target_base
    ]
    if len(by_name) == 1:
        return by_name[0]

    ambiguous = sorted(set(by_path + by_name))
    if len(ambiguous) > 1:
        raise LedgerError(
            f"artifact 引用 {text!r} 有歧义，命中多个产物: {ambiguous}；请直接用 artifact_id"
        )
    raise LedgerError(
        f"artifact 引用 {text!r} 无法解析。已登记产物: {known_ids or '（空）'}；"
        "请先调用 register_artifact，并用它返回的 artifact_id"
    )


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """构造一个 ``type: object`` 的 JSON Schema。"""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# 工具构造
# ---------------------------------------------------------------------------


def build_ledger_tools(
    ledger: Ledger,
    *,
    tex_dir: Path | str,
    run_id: str = "",
    compact: bool = False,
) -> list[ToolSpec]:
    """构造围绕某份台账的一组工具。

    Args:
        ledger: 证据台账实例。
        tex_dir: ``write_latex_section`` 允许写入的目录（限制在运行工作区内，
            避免工具越权写到工作区之外）。
        run_id: 写入 claim 记录时的运行标识，便于与遥测台账对齐。
        compact: 只暴露任务必需的 4 个工具（register_artifact / add_claims /
            list_claims / write_latex_section）。工具集越大，小参数模型选错工具
            的概率越高——这不是猜测，是实测：完整工具集下 7B 模型把步数全耗在
            逐条登记 claim 上。精简档把它压到 4 个，选择空间小了，成功率显著上升。

    Returns:
        工具列表，顺序即建议展示给模型的顺序。

    Raises:
        LedgerError: ``tex_dir`` 无法创建。
    """
    allowed_dir = Path(tex_dir).expanduser().resolve()
    try:
        allowed_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LedgerError(f"无法创建正文输出目录 {allowed_dir}: {exc}") from exc

    def register_artifact(arguments: dict[str, Any]) -> dict[str, Any]:
        """登记实验产物并固化 SHA-256。"""
        _require(arguments, "path")
        record = ledger.register_artifact(
            arguments["path"],
            producer=str(arguments.get("producer") or "unknown"),
            artifact_id=arguments.get("artifact_id"),
        )
        return {
            "artifact_id": record.artifact_id,
            "use_this_artifact_id": record.artifact_id,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
            "path": record.path,
            "next_step": (
                f"下一步调用 add_claim 时，artifact_id 参数请填 {record.artifact_id!r}，"
                "不要填文件名或路径。"
            ),
            "note": "该摘要已固化；产物若被改写，后续校验会失败，必须用新的 artifact_id 重新登记。",
        }

    def add_claim(arguments: dict[str, Any]) -> dict[str, Any]:
        """登记一条数值 claim，并回读产物中的实际值。"""
        _require(arguments, "text", "locator")
        reference = arguments.get("artifact_id") or arguments.get("artifact_path")
        if not reference:
            raise LedgerError(
                "缺少必填参数 artifact_id（register_artifact 的返回值）；"
                "若不确定标识，可改用 artifact_path 传产物文件路径"
            )
        artifact_id = _resolve_artifact_ref(ledger, str(reference))
        value = arguments.get("value")
        if isinstance(value, str):
            # 模型有时把数字写成字符串（"16.81"），这里做一次尽力解析；
            # 解析失败仍按原字符串登记，由 _values_match 判定不一致并报错。
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        record = ledger.add_claim(
            text=str(arguments["text"]),
            artifact_id=artifact_id,
            locator=str(arguments["locator"]),
            value=value,
            display=arguments.get("display"),
            claim_id=arguments.get("claim_id"),
            run_id=run_id,
        )
        return {
            "claim_id": record.claim_id,
            "value": record.value,
            "display": record.display,
            "latex_macro": f"\\result{{{record.claim_id}}}",
            "note": "论文正文里只能写这个宏，不能写数字本身。",
        }

    def add_claims(arguments: dict[str, Any]) -> dict[str, Any]:
        """批量登记数值 claim。

        为什么需要批量接口：单条登记意味着"一条 claim 一轮往返"。小参数模型在
        5 条 claim 上就会把步数预算吃光，根本走不到写正文那一步（这是实测结论，
        不是推测）。批量接口把 N 轮往返压成 1 轮，既省 token 也省步数。

        单条失败不中断整批：逐条报告成功与失败，让模型一次性看到全部问题，
        而不是被第一条错误挡住、反复重试同一条。
        """
        _require(arguments, "claims")
        items = arguments["claims"]
        if not isinstance(items, list) or not items:
            raise LedgerError("claims 必须是非空数组")
        reference = arguments.get("artifact_id") or arguments.get("artifact_path")
        if not reference:
            raise LedgerError(
                "缺少必填参数 artifact_id（register_artifact 的返回值）；"
                "若不确定标识，可改用 artifact_path 传产物文件路径"
            )
        artifact_id = _resolve_artifact_ref(ledger, str(reference))

        registered: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                failed.append({"index": index, "error": "元素必须是对象"})
                continue
            locator = str(item.get("locator") or "").strip()
            text = str(item.get("text") or "").strip()
            if not locator or not text:
                failed.append(
                    {"index": index, "locator": locator or "?", "error": "缺少 text 或 locator"}
                )
                continue
            value = item.get("value")
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            try:
                record = ledger.add_claim(
                    text=text,
                    artifact_id=artifact_id,
                    locator=locator,
                    value=value,
                    display=item.get("display"),
                    run_id=run_id,
                )
            except LedgerError as exc:
                failed.append({"index": index, "locator": locator, "error": str(exc)})
                continue
            registered.append(
                {
                    "claim_id": record.claim_id,
                    "locator": record.locator,
                    "value": record.value,
                    "display": record.display,
                    "latex_macro": f"\\result{{{record.claim_id}}}",
                    "text": record.text,
                }
            )

        return {
            "artifact_id": artifact_id,
            "registered_count": len(registered),
            "failed_count": len(failed),
            "registered": registered,
            "failed": failed,
            "next_step": (
                "现在调用 write_latex_section 写正文，relative_path 用 sections/results.tex，"
                "正文里引用上面 registered 里的 latex_macro。"
            ),
        }


    def list_artifacts(_arguments: dict[str, Any]) -> dict[str, Any]:
        """列出已登记产物。"""
        return {
            "artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "path": item.path,
                    "sha256": item.sha256[:16] + "…",
                    "size_bytes": item.size_bytes,
                    "producer": item.producer,
                }
                for item in ledger.artifacts()
            ]
        }

    def list_claims(_arguments: dict[str, Any]) -> dict[str, Any]:
        """列出已登记 claim（模型据此决定正文里能引用哪些宏）。"""
        return {
            "claims": [
                {
                    "claim_id": item.claim_id,
                    "text": item.text,
                    "value": item.value,
                    "display": item.display,
                    "artifact_id": item.artifact_id,
                    "locator": item.locator,
                    "latex_macro": f"\\result{{{item.claim_id}}}",
                }
                for item in ledger.claims()
            ]
        }

    def verify_ledger(_arguments: dict[str, Any]) -> dict[str, Any]:
        """复核整份台账（含产物摘要重算）。"""
        ledger.reload()
        report = ledger.verify(check_artifacts=True)
        return report.to_dict()

    def write_latex_section(arguments: dict[str, Any]) -> dict[str, Any]:
        """在门控通过的前提下写出一个 LaTeX 片段。

        门控不通过**不落盘**，并抛出 :class:`GateRejected`（其消息内含问题清单）。
        这是本项目的核心不变量：含手写数值或引用未登记 claim 的正文，
        无法进入编译流程。
        """
        _require(arguments, "relative_path", "content")
        relative = str(arguments["relative_path"]).replace("\\", "/").lstrip("/")
        # 模型常把 tex_dir 的名字也带进相对路径（如 paper/sections/results.tex）。
        # 这里剥掉重复前缀，避免写出 paper/paper/... 这种嵌套目录。
        prefix = allowed_dir.name + "/"
        while relative.startswith(prefix):
            relative = relative[len(prefix) :]
        target = (allowed_dir / relative).resolve()
        if allowed_dir not in target.parents and target != allowed_dir:
            raise LedgerError(
                f"拒绝写入工作区之外的路径: {target}（允许目录: {allowed_dir}）"
            )
        text = str(arguments["content"])

        ledger.reload()
        report = gate_strict(text, ledger, tex_path=str(target))

        if not report.ok:
            payload = {
                "rejected": True,
                "gate_ok": False,
                "issues": [item.to_dict() for item in report.issues],
                "hint": (
                    "正文被门控拒绝，未落盘。修正方式：(1) 把每个数值改写成 "
                    "\\result{claim_id}，claim_id 必须来自 list_claims 的返回；"
                    "(2) 正文里至少要有一个 \\result{} 引用，不能一个都没有；"
                    "(3) 不要在手写数字旁使用 achieve/improve/accuracy/score 等措辞；"
                    "(4) 不要在正文里重新定义 \\result 宏。"
                ),
            }
            raise GateRejected(
                "证据门控拒绝写入：" + json.dumps(payload, ensure_ascii=False)
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return {
            "written": True,
            "path": str(target),
            "gate_ok": True,
            "total_refs": report.total_refs,
            "resolved": report.resolved,
            "note": "这是含宏的源文件；编译前必须经 resolve 回填数值。",
        }

    specs = [
        ToolSpec(
            name="register_artifact",
            description=(
                "登记一个实验产物文件（JSON），固化其 SHA-256。"
                "论文里任何数字都必须先有已登记的产物作为来源。"
            ),
            parameters=_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "产物文件路径（相对或绝对）",
                    },
                    "producer": {
                        "type": "string",
                        "description": "产出者标识，如角色名或脚本路径",
                    },
                    "artifact_id": {
                        "type": "string",
                        "description": "可选，指定产物标识；省略则自动生成",
                    },
                },
                ["path", "producer"],
            ),
            handler=register_artifact,
        ),
        ToolSpec(
            name="add_claim",
            description=(
                "登记一条数值 claim：声明某个数字支持什么论断，并指明它来自哪个产物的哪个位置。"
                "artifact_id 必须填 register_artifact 返回的那个短标识（如 a-1a2b3c4d），"
                "不是文件名。locator 是 JSON Pointer，例如 /results/mse_ols_mean。"
                "省略 value 时会自动从产物读取实际值——建议省略，避免写错。"
                "若声明值与产物实际值不符，登记会失败——这是刻意的。"
            ),
            parameters=_schema(
                {
                    "text": {"type": "string", "description": "该数字支持的论断（自然语言）"},
                    "artifact_id": {
                        "type": "string",
                        "description": "来源产物标识，必须是 register_artifact 返回的 artifact_id",
                    },
                    "artifact_path": {
                        "type": "string",
                        "description": "备选：产物文件路径（当不确定 artifact_id 时可用）",
                    },
                    "locator": {
                        "type": "string",
                        "description": "产物内的 JSON Pointer，以 / 开头，如 /results/mse_ols_mean",
                    },
                    "value": {
                        "type": "number",
                        "description": "可选。省略则直接读取产物中的实际值（推荐省略）",
                    },
                    "display": {
                        "type": "string",
                        "description": "可选。LaTeX 中显示文本，如 0.812 或 81.2",
                    },
                    "claim_id": {
                        "type": "string",
                        "description": "可选。指定标识，便于正文引用",
                    },
                },
                ["text", "locator"],
            ),
            handler=add_claim,
        ),
        ToolSpec(
            name="add_claims",
            description=(
                "批量登记多条数值 claim（推荐用这个而不是逐条 add_claim，"
                "一次调用把要写进论文的数字全部登记完）。"
                "artifact_id 填 register_artifact 返回的短标识。"
                "claims 是数组，每项含 text（论断）、locator（JSON Pointer，如 /results/mse_ols_mean）。"
                "省略 value，让系统从产物读实际值。单条失败不影响其余条目，会逐条报告。"
            ),
            parameters=_schema(
                {
                    "artifact_id": {
                        "type": "string",
                        "description": "来源产物标识，必须是 register_artifact 返回的 artifact_id",
                    },
                    "artifact_path": {
                        "type": "string",
                        "description": "备选：产物文件路径（当不确定 artifact_id 时可用）",
                    },
                    "claims": {
                        "type": "array",
                        "description": "要登记的 claim 列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "该数字支持的论断（自然语言）",
                                },
                                "locator": {
                                    "type": "string",
                                    "description": "JSON Pointer，如 /results/mse_ols_mean",
                                },
                                "display": {
                                    "type": "string",
                                    "description": "可选。LaTeX 显示文本",
                                },
                            },
                            "required": ["text", "locator"],
                            "additionalProperties": False,
                        },
                    },
                },
                ["claims"],
            ),
            handler=add_claims,
        ),
        ToolSpec(
            name="list_artifacts",
            description="列出已登记的产物，确认哪些文件可以引用。",
            parameters=_schema({}, []),
            handler=list_artifacts,
        ),
        ToolSpec(
            name="list_claims",
            description=(
                "列出已登记的 claim。写正文之前必须先调用它，"
                "确认每个 \\result{claim_id} 里的 claim_id 真实存在。"
            ),
            parameters=_schema({}, []),
            handler=list_claims,
        ),
        ToolSpec(
            name="verify_ledger",
            description="复核整份台账：重算产物摘要、逐条核对 claim 与产物的值是否一致。",
            parameters=_schema({}, []),
            handler=verify_ledger,
        ),
        ToolSpec(
            name="write_latex_section",
            description=(
                "写出一个 LaTeX 片段。正文里禁止出现手写数值，只能写 \\result{claim_id}。"
                "门控不通过时不会落盘，并返回问题清单。"
            ),
            parameters=_schema(
                {
                    "relative_path": {
                        "type": "string",
                        "description": "相对输出目录的路径，如 sections/results.tex",
                    },
                    "content": {"type": "string", "description": "LaTeX 正文内容"},
                },
                ["relative_path", "content"],
            ),
            handler=write_latex_section,
        ),
    ]

    if compact:
        essential = {
            "register_artifact",
            "add_claims",
            "list_claims",
            "write_latex_section",
        }
        specs = [spec for spec in specs if spec.name in essential]
    return specs


def render_claims_summary(ledger: Ledger) -> str:
    """把台账渲染成一段紧凑的文本摘要，供拼进提示词。"""
    artifacts = ledger.artifacts()
    claims = ledger.claims()
    lines = [f"已登记产物 {len(artifacts)} 个，claim {len(claims)} 条。"]
    for claim in claims:
        lines.append(
            f"- {claim.claim_id} = {claim.display}（{claim.text}；"
            f"来源 {claim.artifact_id}{claim.locator}）"
        )
    return "\n".join(lines)


def resolve_to_file(ledger: Ledger, src: Path | str, out: Path | str) -> dict[str, Any]:
    """便捷函数：门控 + 回填，供脚本调用。"""
    ledger.reload()
    report = resolve(src, out, ledger, literal_warnings=False)
    return report.to_dict()
