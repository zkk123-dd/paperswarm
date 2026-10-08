# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""证据台账（Claim–Evidence Ledger）。

本模块是 PaperSwarm 的反伪造内核，**不依赖任何 Agent 框架**，因此可以脱离
JiuwenSwarm 单独运行与测试。

设计要点
--------
论文里每一个数值型结论都必须先登记成一条 claim，并且该 claim 必须指向一个
**真实存在、内容未被篡改**的 artifact 路径 + locator。校验时会重新计算
artifact 的 SHA-256 并与台账记录比对，再从 artifact 中按 locator 读出数值，
确认与 claim 声明的一致。

这带来一条可验证的硬约束：**数值不能凭空出现在论文里**。它不是靠 prompt 劝
模型"不要编造"，而是靠产物比对拦截——prompt 约束是概率性的，产物校验是确定性的。

数据文件（均为 JSON Lines，追加写入）
-------------------------------------
- ``artifacts.jsonl``：每个实验产物一行，含 path / sha256 / bytes / producer。
- ``claims.jsonl``：每条 claim 一行，含 claim_id / value / artifact_id / locator。

两者都在台账根目录下，台账根目录建议放在团队共享工作区内，随实验结果一起
落盘，从而与轨迹（``team_observability`` 的 file exporter）互相印证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ARTIFACTS_FILE = "artifacts.jsonl"
CLAIMS_FILE = "claims.jsonl"

_CLAIM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CHUNK_BYTES = 1 << 20


class LedgerError(RuntimeError):
    """台账操作失败（输入非法、文件损坏、记录缺失等）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    """返回 ISO 8601 UTC 时间戳（秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256，避免把大文件读进内存。

    Args:
        path: 待计算的文件路径。

    Returns:
        小写十六进制摘要。

    Raises:
        LedgerError: 路径不存在或不是普通文件。
    """
    if not path.is_file():
        raise LedgerError(f"artifact 不是普通文件: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _new_id(prefix: str) -> str:
    """生成形如 ``a-3f9c1d2e`` 的短标识。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL；文件不存在时返回空列表，坏行直接报错而不是静默跳过。"""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path.name} 第 {lineno} 行不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LedgerError(f"{path.name} 第 {lineno} 行不是 JSON 对象")
        records.append(parsed)
    return records


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    """追加一行 JSON 并立即落盘（追加模式，不重写历史）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()


def _json_pointer(root: Any, pointer: str) -> Any:
    """按 RFC 6901 解析 JSON Pointer，例如 ``/results/seed42/val_ppl``。

    Args:
        root: 已解析的 JSON 对象。
        pointer: 以 ``/`` 开头的 JSON Pointer；空串表示整个文档。

    Returns:
        指针指向的值。

    Raises:
        LedgerError: 指针路径不存在。
    """
    if pointer in ("", "/"):
        return root
    if not pointer.startswith("/"):
        raise LedgerError(f"locator 必须是以 / 开头的 JSON Pointer，收到: {pointer!r}")
    node = root
    for raw_token in pointer.lstrip("/").split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                raise LedgerError(f"locator {pointer!r} 在字段 {token!r} 处不存在")
            node = node[token]
        elif isinstance(node, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise LedgerError(
                    f"locator {pointer!r} 期望数组下标，收到 {token!r}"
                ) from exc
            if not 0 <= index < len(node):
                raise LedgerError(f"locator {pointer!r} 下标越界: {index}")
            node = node[index]
        else:
            raise LedgerError(f"locator {pointer!r} 无法继续下钻（遇到标量）")
    return node


def _values_match(expected: Any, actual: Any, rel_tol: float, abs_tol: float) -> bool:
    """比较 claim 声明值与 artifact 实际值。

    数值按相对/绝对容差比较（浮点在不同精度下不宜用等号），其余类型直接相等。
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if math.isnan(float(expected)) or math.isnan(float(actual)):
            return False
        return math.isclose(
            float(expected), float(actual), rel_tol=rel_tol, abs_tol=abs_tol
        )
    return expected == actual


# ---------------------------------------------------------------------------
# 记录模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRecord:
    """一个实验产物的登记记录。"""

    artifact_id: str
    path: str
    sha256: str
    size_bytes: int
    producer: str
    created_at: str
    entry: str = "artifact"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaimRecord:
    """一条数值型 claim 的登记记录。"""

    claim_id: str
    text: str
    value: Any
    display: str
    artifact_id: str
    locator: str
    created_at: str
    run_id: str = ""
    entry: str = "claim"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    """单条 claim 的校验结果。"""

    claim_id: str
    ok: bool
    reason: str = ""
    value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    """整份台账的校验报告。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.total > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "ok": self.ok,
            "results": [item.to_dict() for item in self.results],
        }


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


class Ledger:
    """证据台账：登记实验产物与数值 claim，并提供可复核的校验。

    Args:
        root: 台账根目录（建议放团队共享工作区）。
        artifacts_root: 解析 artifact 相对路径的基准目录；默认与 ``root`` 相同。
    """

    def __init__(self, root: Path | str, artifacts_root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.artifacts_root = (
            Path(artifacts_root).expanduser().resolve()
            if artifacts_root is not None
            else self.root
        )
        self._artifacts: dict[str, ArtifactRecord] | None = None
        self._claims: dict[str, ClaimRecord] | None = None

    # -- 路径 ---------------------------------------------------------------

    @property
    def artifacts_path(self) -> Path:
        return self.root / ARTIFACTS_FILE

    @property
    def claims_path(self) -> Path:
        return self.root / CLAIMS_FILE

    def _load(self) -> None:
        if self._artifacts is not None and self._claims is not None:
            return
        artifacts: dict[str, ArtifactRecord] = {}
        for record in _read_jsonl(self.artifacts_path):
            artifact_id = str(record.get("artifact_id") or "")
            if not artifact_id:
                raise LedgerError(f"{ARTIFACTS_FILE} 存在缺少 artifact_id 的记录")
            artifacts[artifact_id] = ArtifactRecord(
                artifact_id=artifact_id,
                path=str(record.get("path") or ""),
                sha256=str(record.get("sha256") or ""),
                size_bytes=int(record.get("size_bytes") or 0),
                producer=str(record.get("producer") or ""),
                created_at=str(record.get("created_at") or ""),
            )
        claims: dict[str, ClaimRecord] = {}
        for record in _read_jsonl(self.claims_path):
            claim_id = str(record.get("claim_id") or "")
            if not claim_id:
                raise LedgerError(f"{CLAIMS_FILE} 存在缺少 claim_id 的记录")
            claims[claim_id] = ClaimRecord(
                claim_id=claim_id,
                text=str(record.get("text") or ""),
                value=record.get("value"),
                display=str(record.get("display") or ""),
                artifact_id=str(record.get("artifact_id") or ""),
                locator=str(record.get("locator") or ""),
                created_at=str(record.get("created_at") or ""),
                run_id=str(record.get("run_id") or ""),
            )
        self._artifacts = artifacts
        self._claims = claims

    def reload(self) -> None:
        """丢弃缓存并重新读取台账文件。"""
        self._artifacts = None
        self._claims = None
        self._load()

    # -- 登记 ---------------------------------------------------------------

    def register_artifact(
        self,
        path: Path | str,
        *,
        producer: str,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        """登记一个实验产物并固化其 SHA-256。

        Args:
            path: 产物路径；相对路径按 ``artifacts_root`` 解析。
            producer: 产出者标识（角色名 / 命令 / 脚本路径），用于溯源。
            artifact_id: 指定标识；默认自动生成。

        Returns:
            落盘后的 :class:`ArtifactRecord`。未显式指定 ``artifact_id`` 时，
            若同一路径、同一摘要已登记过，直接返回该记录（幂等）。

        Raises:
            LedgerError: 文件不存在。同一 artifact_id 已登记且摘要不同时报错，
                避免静默覆盖（产物被改写必须显式重新登记）。
        """
        self._load()
        target = Path(path)
        if not target.is_absolute():
            target = self.artifacts_root / target
        target = target.resolve()
        digest = sha256_file(target)
        size = target.stat().st_size

        resolved_id = artifact_id or _new_id("a")
        assert self._artifacts is not None

        # 幂等：同一个文件、同一份内容被重复登记时，返回已有记录而不是新建一条。
        # 重复登记在实践中很常见（脚本登记一次、Agent 又登记一次），若每次都生成新
        # artifact_id，台账里会出现指向同一文件的多条记录，claim 的来源归属会变模糊。
        if artifact_id is None:
            for existing in self._artifacts.values():
                if existing.sha256 == digest and Path(existing.path) == target:
                    return existing

        existing = self._artifacts.get(resolved_id)
        if existing is not None:
            if existing.sha256 != digest:
                raise LedgerError(
                    f"artifact {resolved_id} 已登记且摘要不同"
                    f"（已存 {existing.sha256[:12]}…，当前 {digest[:12]}…）；"
                    "产物被改写时请使用新的 artifact_id 重新登记"
                )
            return existing

        record = ArtifactRecord(
            artifact_id=resolved_id,
            path=str(target),
            sha256=digest,
            size_bytes=size,
            producer=producer,
            created_at=_utc_now(),
        )
        _append_jsonl(self.artifacts_path, record.to_dict())
        self._artifacts[resolved_id] = record
        return record

    def add_claim(
        self,
        *,
        text: str,
        artifact_id: str,
        locator: str,
        value: Any = None,
        display: str | None = None,
        claim_id: str | None = None,
        run_id: str = "",
    ) -> ClaimRecord:
        """登记一条数值 claim。

        Args:
            text: 该数值在论文中要支持的论断（自然语言，供审阅者对照）。
            artifact_id: 数值来源产物的标识。
            locator: 产物内的 JSON Pointer，例如 ``/results/seed42/val_ppl``。
            value: 声明值；为 ``None`` 时从产物中读取实际值并写入。
            display: LaTeX 中显示的文本；默认用 ``value`` 的字符串形式。
            claim_id: 指定标识；默认自动生成。
            run_id: 关联的 SwarmFlow run id，便于与轨迹台账对齐。

        Returns:
            落盘后的 :class:`ClaimRecord`。

        Raises:
            LedgerError: 论断为空、artifact 未登记、locator 无效，或声明的
                ``value`` 与产物中的实际值不一致。
        """
        self._load()
        assert self._artifacts is not None
        assert self._claims is not None

        if not text.strip():
            raise LedgerError("claim 的 text 不能为空：它必须说明这个数字支持什么论断")
        if not artifact_id.strip():
            raise LedgerError("claim 必须绑定 artifact_id（禁止无来源数字）")
        if artifact_id not in self._artifacts:
            raise LedgerError(
                f"claim 指向未登记的 artifact {artifact_id!r}；请先 register_artifact"
            )

        target = Path(self._artifacts[artifact_id].path)
        actual = _json_pointer(_load_json_document(target), locator)

        if value is not None and not _values_match(value, actual, 1e-9, 1e-12):
            raise LedgerError(
                f"claim 声明值 {value!r} 与产物实际值 {actual!r} 不一致"
                f"（artifact={artifact_id} locator={locator}）"
            )
        stored_value = actual if value is None else value

        resolved_id = claim_id or _new_id("c")
        if not _CLAIM_ID_RE.match(resolved_id):
            raise LedgerError(
                f"非法 claim_id {resolved_id!r}：只允许字母数字与 _ . : -，"
                "且必须以字母数字开头（该标识会写进 LaTeX）"
            )
        if resolved_id in self._claims:
            raise LedgerError(f"claim_id {resolved_id!r} 已存在，标识必须唯一")

        display_text = display if display is not None else _default_display(stored_value)

        # 幂等：同一个产物、同一个定位、同一段论断被重复登记时复用已有记录。
        # Agent 在弱模型上很容易把同一条 claim 登记两遍；若每次都生成新标识，
        # 台账会膨胀、上下文被无谓占用，而且论文里会出现多个指向同一数值的宏，
        # 让"这个数字支持哪条论断"变得难以核对。
        if claim_id is None:
            for existing in self._claims.values():
                if (
                    existing.artifact_id == artifact_id
                    and existing.locator == locator
                    and existing.text == text.strip()
                    and existing.display == display_text
                ):
                    return existing

        record = ClaimRecord(
            claim_id=resolved_id,
            text=text.strip(),
            value=stored_value,
            display=display_text,
            artifact_id=artifact_id,
            locator=locator,
            created_at=_utc_now(),
            run_id=run_id,
        )
        _append_jsonl(self.claims_path, record.to_dict())
        self._claims[resolved_id] = record
        return record

    # -- 读取 ---------------------------------------------------------------

    def claim(self, claim_id: str) -> ClaimRecord:
        """按标识取一条 claim，不存在时报错。"""
        self._load()
        assert self._claims is not None
        try:
            return self._claims[claim_id]
        except KeyError as exc:
            raise LedgerError(f"台账中不存在 claim {claim_id!r}") from exc

    def claims(self) -> list[ClaimRecord]:
        """按登记顺序返回全部 claim。"""
        self._load()
        assert self._claims is not None
        return list(self._claims.values())

    def artifacts(self) -> list[ArtifactRecord]:
        """按登记顺序返回全部 artifact。"""
        self._load()
        assert self._artifacts is not None
        return list(self._artifacts.values())

    # -- 校验 ---------------------------------------------------------------

    def verify(
        self,
        *,
        check_artifacts: bool = True,
        rel_tol: float = 1e-9,
        abs_tol: float = 1e-12,
    ) -> VerifyReport:
        """复核整份台账。

        校验四件事：

        1. claim 绑定的 artifact 在台账中存在；
        2. artifact 文件此刻仍然存在；
        3. artifact 的 SHA-256 与登记时一致（**这是防伪造的核心**：实验结果被
           事后改写会被这里抓住）；
        4. 按 locator 从 artifact 读出的实际值，与 claim 声明值一致。

        Args:
            check_artifacts: 是否重新计算文件摘要。关闭后只比对台账内部一致性，
                适合在产物尚未回传的场景下做快速自检。
            rel_tol: 数值相对容差。
            abs_tol: 数值绝对容差。

        Returns:
            :class:`VerifyReport`。
        """
        self._load()
        assert self._artifacts is not None
        assert self._claims is not None

        report = VerifyReport()
        for claim in self._claims.values():
            report.total += 1
            result = self._verify_one(
                claim,
                self._artifacts.get(claim.artifact_id),
                check_artifacts=check_artifacts,
                rel_tol=rel_tol,
                abs_tol=abs_tol,
            )
            report.results.append(result)
            if result.ok:
                report.passed += 1
            else:
                report.failed += 1
        return report

    def _verify_one(
        self,
        claim: ClaimRecord,
        artifact: ArtifactRecord | None,
        *,
        check_artifacts: bool,
        rel_tol: float,
        abs_tol: float,
    ) -> CheckResult:
        if artifact is None:
            return CheckResult(
                claim.claim_id, False, f"artifact {claim.artifact_id!r} 未登记"
            )

        target = Path(artifact.path)
        if check_artifacts:
            if not target.is_file():
                return CheckResult(
                    claim.claim_id, False, f"artifact 文件已不存在: {artifact.path}"
                )
            try:
                digest = sha256_file(target)
            except LedgerError as exc:
                return CheckResult(claim.claim_id, False, str(exc))
            if digest != artifact.sha256:
                return CheckResult(
                    claim.claim_id,
                    False,
                    "artifact 内容已被修改（摘要不匹配，疑似篡改实验结果）",
                )

        try:
            actual = _json_pointer(_load_json_document(target), claim.locator)
        except LedgerError as exc:
            return CheckResult(claim.claim_id, False, str(exc))

        if not _values_match(claim.value, actual, rel_tol, abs_tol):
            return CheckResult(
                claim.claim_id,
                False,
                f"值与产物不一致：声明 {claim.value!r}，实际 {actual!r}",
            )
        return CheckResult(claim.claim_id, True, "", actual)


def _load_json_document(path: Path) -> Any:
    """读取 artifact 并解析为 JSON。

    目前只支持 JSON 产物：只有可结构化读取的产物才能被机器校验。
    实验脚本必须以 JSON 落盘指标，这也顺带统一了实验产物格式。
    """
    if not path.is_file():
        raise LedgerError(f"artifact 文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"artifact 不是合法 JSON，无法机器校验: {path}（{exc}）"
        ) from exc


def _default_display(value: Any) -> str:
    """生成 LaTeX 默认显示文本。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_register(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    record = ledger.register_artifact(
        args.path, producer=args.producer, artifact_id=args.artifact_id
    )
    print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_claim(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    value: Any = None
    if args.value is not None:
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
    record = ledger.add_claim(
        text=args.text,
        artifact_id=args.artifact_id,
        locator=args.locator,
        value=value,
        display=args.display,
        claim_id=args.claim_id,
        run_id=args.run_id,
    )
    print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    report = ledger.verify(check_artifacts=not args.skip_artifact_check)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.total == 0:
        print("台账为空：没有任何 claim 需要校验。", file=sys.stderr)
        return 1
    if not report.ok:
        for item in report.results:
            if not item.ok:
                print(f"  [FAIL] {item.claim_id}: {item.reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    payload = {
        "root": str(ledger.root),
        "artifacts": [item.to_dict() for item in ledger.artifacts()],
        "claims": [item.to_dict() for item in ledger.claims()],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="evidence",
        description="PaperSwarm 证据台账：登记实验产物与数值 claim，并复核其未被篡改。",
    )
    parser.add_argument("--root", required=True, help="台账根目录")
    parser.add_argument(
        "--artifacts-root",
        default=None,
        help="解析 artifact 相对路径的基准目录（默认与 --root 相同）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_register = sub.add_parser("register", help="登记一个实验产物并固化 SHA-256")
    p_register.add_argument("path", help="产物路径")
    p_register.add_argument("--producer", required=True, help="产出者标识（角色/命令）")
    p_register.add_argument("--artifact-id", default=None, help="指定标识")
    p_register.set_defaults(func=_cmd_register)

    p_claim = sub.add_parser("claim", help="登记一条数值 claim")
    p_claim.add_argument("--text", required=True, help="该数字支持的论断")
    p_claim.add_argument("--artifact-id", required=True, help="数值来源产物标识")
    p_claim.add_argument("--locator", required=True, help="产物内的 JSON Pointer")
    p_claim.add_argument(
        "--value",
        default=None,
        help="声明值；省略则从产物读取。传 JSON 字面量或裸字符串",
    )
    p_claim.add_argument("--display", default=None, help="LaTeX 显示文本")
    p_claim.add_argument("--claim-id", default=None, help="指定标识")
    p_claim.add_argument("--run-id", default="", help="关联的 SwarmFlow run id")
    p_claim.set_defaults(func=_cmd_claim)

    p_verify = sub.add_parser("verify", help="复核台账（摘要 + locator 双重校验）")
    p_verify.add_argument(
        "--skip-artifact-check",
        action="store_true",
        help="跳过文件摘要重算，只做台账内部一致性检查",
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_show = sub.add_parser("show", help="打印台账全量内容")
    p_show.set_defaults(func=_cmd_show)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；默认取 ``sys.argv[1:]``。

    Returns:
        进程退出码：0 成功，1 校验失败或输入非法。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LedgerError as exc:
        print(f"台账错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
