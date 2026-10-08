# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""LaTeX 证据门控（Evidence Gate）。

论文正文里**不允许出现手写的数值**。所有结果数字必须以宏
``\\result{<claim_id>}`` 的形式引用台账，编译前由 ``resolve`` 从台账回填真实数值。

这样做的原因很直接：靠 prompt 要求模型"别编数字"是不可验证的约束；而让数字
根本没有手写入口，是可验证的。模型能做的只剩两件事——引用一个已登记且校验
通过的 claim，或者把数字写死在正文里——后者会被 ``gate`` 的二次扫描抓出来。

流水线位置
----------
1. ``analyst`` 产出 JSON 指标产物 → ``evidence register`` 登记 artifact；
2. ``evidence claim`` 把每个要写进论文的数字登记成 claim；
3. ``writer`` 写正文时只写 ``\\result{c-xxxx}``，不写数字；
4. ``resolve`` 生成可编译的 ``.tex``（数字由台账注入）；
5. ``gate`` 在 PreToolUse 钩子里拦截不合规的写入，在 ``typesetter`` 前做终检。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from paperswarm.evidence import Ledger, LedgerError

RESULT_CALL_RE = re.compile(r"\\result\*?\{([^{}]*)\}")
"""匹配 ``\\result{c-abc}`` / ``\\result*{c-abc}``。"""

RESULT_DEF_RE = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand|def)\s*\{?\\result\b",
    re.MULTILINE,
)
"""匹配 ``\\result`` 宏的自定义 —— 正文里出现即视为绕过门控。"""

SECTION_RE = re.compile(r"\\section\*?\{[^{}]*\}[ \t]*\n?")
"""匹配顶层 ``\\section{}`` / ``\\section*{}``。

注意 ``\\subsection`` 不含子串 ``\\section``（``subsection`` 里没有 ``section``
这个词），所以本正则不会误伤子标题。

章节标题由组装器统一接管：写手只产出正文，组装器负责插入唯一的一级标题。
这条分工曾经只写在注释里而没有被实现，导致编译出的 PDF **一个章节标题都没有** ——
模型写 ``\\subsection{}`` 时因为缺少父级 ``\\section``，还被 hyperref 提升成了
书签的一级条目。
"""

HEADER = (
    "% 本文件由 paperswarm.tex_gate 从台账自动生成，请勿手工编辑。\n"
    "% 每个数值均由证据台账注入：claim_id -> (artifact SHA-256, JSON Pointer)。\n"
)


@dataclass
class GateIssue:
    """一条门控问题。"""

    line: int
    claim_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "claim_id": self.claim_id, "reason": self.reason}


@dataclass
class GateReport:
    """门控报告。"""

    tex_path: str
    total_refs: int = 0
    resolved: int = 0
    issues: list[GateIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    macro_redefined: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues and not self.macro_redefined

    def to_dict(self) -> dict[str, Any]:
        return {
            "tex_path": self.tex_path,
            "total_refs": self.total_refs,
            "resolved": self.resolved,
            "ok": self.ok,
            "macro_redefined": self.macro_redefined,
            "issues": [item.to_dict() for item in self.issues],
            "warnings": list(self.warnings),
        }


def iter_result_refs(text: str) -> Iterator[tuple[int, str]]:
    """逐行产出 ``(行号, claim_id)``；行号从 1 开始，便于定位。"""
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in RESULT_CALL_RE.finditer(line):
            yield lineno, match.group(1).strip()


def _line_of_offset(text: str, offset: int) -> int:
    """把字符偏移换算成 1 起的行号。"""
    return text.count("\n", 0, offset) + 1


def gate(
    tex_path: Path | str,
    ledger: Ledger,
    *,
    skip_artifact_check: bool = False,
) -> GateReport:
    """校验一份 ``.tex``：所有 ``\\result{}`` 必须能解析到已校验通过的 claim。

    Args:
        tex_path: 待检正文。
        ledger: 证据台账。
        skip_artifact_check: 跳过产物摘要重算（仅用于快速自检）。

    Returns:
        :class:`GateReport`；``ok`` 为真才允许继续编译。
    """
    path = Path(tex_path)
    if not path.is_file():
        raise LedgerError(f"正文文件不存在: {path}")
    text = path.read_text(encoding="utf-8")
    return gate_text(text, ledger, tex_path=str(path), skip_artifact_check=skip_artifact_check)


def gate_text(
    text: str,
    ledger: Ledger,
    *,
    tex_path: str = "<memory>",
    skip_artifact_check: bool = False,
    require_refs: bool = False,
    strict_literals: bool = False,
) -> GateReport:
    """校验正文文本（不落盘版本，便于单测与钩子内联调用）。

    Args:
        text: 正文文本。
        ledger: 证据台账。
        tex_path: 报告里回显的路径，便于定位。
        skip_artifact_check: 跳过产物摘要重算。
        require_refs: 要求正文至少有一个 ``\\result{}`` 引用；否则记为问题。
            写入路径必须开启——否则"一个引用都不写、数字全靠手打"就成了绕过门控的通道。
        strict_literals: 把结果语境附近的手写数字从告警升级为拦截。

    Returns:
        :class:`GateReport`。
    """
    report = GateReport(tex_path=tex_path)

    if RESULT_DEF_RE.search(text):
        report.macro_redefined = True
        report.issues.append(
            GateIssue(
                line=_line_of_offset(text, RESULT_DEF_RE.search(text).start()),  # type: ignore[union-attr]
                claim_id="",
                reason="正文重新定义了 \\result 宏，这会让证据门控失效；宏定义必须放在模板中",
            )
        )

    verify_report = ledger.verify(check_artifacts=not skip_artifact_check)
    failed = {
        item.claim_id: item.reason for item in verify_report.results if not item.ok
    }

    for lineno, claim_id in iter_result_refs(text):
        report.total_refs += 1
        if not claim_id:
            report.issues.append(
                GateIssue(lineno, claim_id, "\\result{} 为空，必须给出 claim_id")
            )
            continue
        try:
            ledger.claim(claim_id)
        except LedgerError as exc:
            report.issues.append(GateIssue(lineno, claim_id, str(exc)))
            continue
        reason = failed.get(claim_id)
        if reason is not None:
            report.issues.append(
                GateIssue(lineno, claim_id, f"claim 未通过校验：{reason}")
            )
            continue
        report.resolved += 1

    if report.total_refs == 0:
        message = (
            "正文没有任何 \\result{} 引用：要么这篇论文不含数值结论，"
            "要么数值被手写进了正文（请人工复核）"
        )
        if require_refs:
            report.issues.append(GateIssue(0, "", message))
        else:
            report.warnings.append(message)

    literal_hits = _check_literal_numbers(text)
    if literal_hits:
        if strict_literals:
            for hit in literal_hits:
                # 行号已在 _check_literal_numbers 里格式化进文本，这里统一记为第 0 行，
                # 因为该检查是正则启发式，行号已在 reason 里给出更精确的定位。
                report.issues.append(GateIssue(0, "", hit))
        else:
            report.warnings.extend(literal_hits)
    return report


def gate_strict(text: str, ledger: Ledger, *, tex_path: str = "<memory>") -> GateReport:
    """写入路径专用门控：要求至少有引用，且阻断结果语境下的手写数字。

    与 :func:`gate_text` 的区别是"告警"与"拦截"的边界不同：

    - ``require_refs=True`` 堵住"一个 ``\\result`` 都不写"的绕过通道；
    - ``strict_literals=True`` 把结果语境附近的手写数字升级为拦截。

    手写数字检查是正则启发式的，可能对"2026 年"这类非结果数字误判，因此
    正则要求数字附近出现 achiev/improve/accuracy/F1/score 等结果语境词才命中。
    宁可让模型重写一次，也不放行一个无法追溯到产物的数字。
    """
    return gate_text(
        text,
        ledger,
        tex_path=tex_path,
        require_refs=True,
        strict_literals=True,
    )


def _check_literal_numbers(text: str) -> list[str]:
    """启发式扫描：结果语境附近的手写数字。

    这是**告警级**检查，不是拦截级：自然语言里难免出现年份、章节号等数字，
    正则无法可靠区分。它的作用是给审阅者提示，真正的硬约束由 ``\\result{}``
    的强制引用机制承担。因此本函数返回的条目一律进 ``warnings``。
    """
    pattern = re.compile(
        r"(?:achiev\w*|improv\w*|outperform\w*|reduc\w*|increas\w*|accuracy|"
        r"F1|BLEU|perplexity|PPL|score|coefficient|p\s*[<=])"
        r"[^.\n]{0,60}?(?<![\w{])(\d+(?:\.\d+)?\s*\\?%?)",
        re.IGNORECASE,
    )
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = RESULT_CALL_RE.sub("", line)
        for match in pattern.finditer(stripped):
            hits.append(f"第 {lineno} 行疑似手写数值 {match.group(1)!r}：{line.strip()[:80]}")
    return hits


def resolve(
    tex_path: Path | str,
    out_path: Path | str,
    ledger: Ledger,
    *,
    skip_artifact_check: bool = False,
    literal_warnings: bool = True,
    heading: str | None = None,
) -> GateReport:
    """把 ``\\result{claim_id}`` 回填成台账中的真实数值，写出可编译的 ``.tex``。

    门控不通过时**不写文件**，避免产出带有占位符的中间件被误编译。

    Args:
        tex_path: 源正文。
        out_path: 输出正文路径。
        ledger: 证据台账。
        skip_artifact_check: 跳过产物摘要重算。
        literal_warnings: 是否附带手写数字的启发式告警。
        heading: 若给出，则在正文最前面写入唯一的 ``\\section{<heading>}``，
            并先剥掉正文自带的全部顶层 ``\\section{}``。章节标题因此由组装器
            独占，不依赖模型是否听话。

    Returns:
        :class:`GateReport`（含 warnings）。

    Raises:
        LedgerError: 门控未通过，或源文件不存在。
    """
    src = Path(tex_path)
    if not src.is_file():
        raise LedgerError(f"正文文件不存在: {src}")
    text = src.read_text(encoding="utf-8")
    report = gate_text(
        text,
        ledger,
        tex_path=str(src),
        skip_artifact_check=skip_artifact_check,
    )
    if literal_warnings:
        report.warnings.extend(_check_literal_numbers(text))
    if not report.ok:
        return report

    def _substitute(match: re.Match[str]) -> str:
        claim = ledger.claim(match.group(1).strip())
        return claim.display

    rendered = RESULT_CALL_RE.sub(_substitute, text)
    if heading is not None:
        stripped = SECTION_RE.findall(rendered)
        if stripped:
            report.warnings.append(
                f"正文自带了 {len(stripped)} 个顶层 \\section，已剥离；"
                "标题统一由组装器写入"
            )
            rendered = SECTION_RE.sub("", rendered)
        rendered = f"\\section{{{heading}}}\n\n{rendered.lstrip()}"

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER + rendered, encoding="utf-8")
    return report


def _cmd_check(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    report = gate(args.tex, ledger, skip_artifact_check=args.skip_artifact_check)
    if args.literal_warnings:
        report.warnings.extend(
            _check_literal_numbers(Path(args.tex).read_text(encoding="utf-8"))
        )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    for warning in report.warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
    if not report.ok:
        for issue in report.issues:
            print(f"[FAIL] 第 {issue.line} 行 \\result{{{issue.claim_id}}}: {issue.reason}", file=sys.stderr)
        return 1
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    ledger = Ledger(args.root, args.artifacts_root)
    report = resolve(
        args.tex,
        args.out,
        ledger,
        skip_artifact_check=args.skip_artifact_check,
        literal_warnings=args.literal_warnings,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    for warning in report.warnings:
        print(f"[WARN] {warning}", file=sys.stderr)
    if not report.ok:
        for issue in report.issues:
            print(f"[FAIL] 第 {issue.line} 行: {issue.reason}", file=sys.stderr)
        return 1
    print(f"已写出 {args.out}（{report.resolved} 个数值由台账注入）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="tex_gate",
        description="LaTeX 证据门控：校验 / 回填 \\result{claim_id} 引用。",
    )
    parser.add_argument("--root", required=True, help="台账根目录")
    parser.add_argument("--artifacts-root", default=None, help="artifact 路径基准目录")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("check", "只校验，不产出文件（PreToolUse 钩子用这个）"),
        ("resolve", "校验通过后回填数值并写出可编译的 .tex"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("tex", help="待处理的 .tex 路径")
        if name == "resolve":
            p.add_argument("--out", required=True, help="输出 .tex 路径")
        p.add_argument(
            "--skip-artifact-check",
            action="store_true",
            help="跳过产物摘要重算",
        )
        p.add_argument(
            "--literal-warnings",
            action="store_true",
            help="附带手写数字的启发式告警（仅提示，不拦截）",
        )
        p.set_defaults(func=_cmd_resolve if name == "resolve" else _cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；默认取 ``sys.argv[1:]``。

    Returns:
        进程退出码：0 门控通过，1 门控失败或输入非法。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LedgerError as exc:
        print(f"门控错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
