# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""把"写作重复"这条主观批评变成可测量的数。

外部评审对论文的批评里有一条是主观的："Sections 2-5 restate the same
ridge-vs-OLS numbers with minimal new insight"。主观批评没法当验收标准用，
所以这里把它拆成三个可观测量：

1. **跨章节 n-gram 重叠**：任取两章，4-gram 集合的包含率
   ``|A ∩ B| / min(|A|, |B|)``。包含率而不是 Jaccard，是因为我们要问的是
   "短的那一章是不是基本被长的那一章盖住了"，Jaccard 会被长度差稀释。
2. **同一 claim 跨章节重复引用次数**：``\\result{cl-x}`` 在各章出现的次数。
   同一个数字在 6 个章节各讲一遍，就是评审说的"restate"。
3. **章节词汇多样性**：type-token ratio，用来发现通篇同一套句式的章节。

用法::

    <python> scripts/repetition_audit.py runs/paper-*/paper/drafts
    <python> scripts/repetition_audit.py <dir> --json reports/repetition_audit.json

退出码：0 = 全部在阈值内；1 = 有章节超阈值（可当门控用）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

#: 4-gram 包含率上限。超过就认为这两章在互相复述。
MAX_PAIRWISE_CONTAINMENT = 0.25

#: 单个 claim 允许出现的章节数上限（含自身）。超过就是"同一个数字讲太多遍"。
MAX_CLAIM_SECTION_SPREAD = 3

#: 章节词汇多样性下限（type-token ratio，按词形）。低于它就说明句式高度重复。
MIN_TYPE_TOKEN_RATIO = 0.45

#: 比较重叠时忽略的英文功能词 —— 它们必然到处出现，留着只会稀释信号。
STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have in is it its of on or that the
    their there these this to was were which with we our us they them can be
    not but also more than such may show shows using used use into over
    """.split()
)

TEX_COMMAND_RE = re.compile(r"\\[a-zA-Z@]+\*?")
RESULT_MACRO_RE = re.compile(r"\\result\{([^}]*)\}")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']*")


@dataclass
class SectionText:
    """一章的纯文本与统计量。"""

    name: str
    path: Path
    words: list[str] = field(default_factory=list)
    result_macros: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.words)

    @property
    def type_token_ratio(self) -> float:
        if not self.words:
            return 0.0
        return len(set(self.words)) / len(self.words)

    def shingles(self, n: int) -> set[tuple[str, ...]]:
        return {tuple(self.words[i : i + n]) for i in range(len(self.words) - n + 1)}


def strip_latex(text: str) -> tuple[list[str], list[str]]:
    """把 LaTeX 源码剥成词序列，同时把 ``\\result{...}`` 的 claim id 单独收集起来。

    ``\\result{cl-ols-mean}`` 这类宏在正文里会被替换成真实数字，所以它们既不是
    普通词、也不是标点 —— 单独收集，才能统计"同一个数字被讲了几遍"。
    """
    macro_ids = RESULT_MACRO_RE.findall(text)
    stripped = RESULT_MACRO_RE.sub(" ", text)
    stripped = stripped.replace("%", " ")
    stripped = TEX_COMMAND_RE.sub(" ", stripped)
    stripped = re.sub(r"[{}$&#_^~\\]", " ", stripped)
    words = [
        w.lower()
        for w in WORD_RE.findall(stripped)
        if w.lower() not in STOPWORDS and len(w) > 1
    ]
    return words, macro_ids


def load_sections(directory: Path) -> list[SectionText]:
    """读取目录下所有 ``.tex``，按文件名排序（跳过组装产物与模板）。"""
    skip_prefixes = ("_",)
    sections: list[SectionText] = []
    for path in sorted(directory.glob("*.tex")):
        if path.name.startswith(skip_prefixes):
            continue
        words, macros = strip_latex(path.read_text(encoding="utf-8"))
        if not words and not macros:
            continue
        sections.append(
            SectionText(name=path.stem, path=path, words=words, result_macros=macros)
        )
    return sections


def audit(sections: list[SectionText], ngram: int = 4) -> dict:
    """跑三项检查，返回结构化结果。"""
    pairwise: list[dict] = []
    for left, right in combinations(sections, 2):
        a, b = left.shingles(ngram), right.shingles(ngram)
        if not a or not b:
            continue
        intersection = len(a & b)
        containment = intersection / min(len(a), len(b))
        pairwise.append(
            {
                "left": left.name,
                "right": right.name,
                "shared_ngrams": intersection,
                "containment": round(containment, 4),
                "left_ngrams": len(a),
                "right_ngrams": len(b),
                "verdict": "RESTATES" if containment > MAX_PAIRWISE_CONTAINMENT else "ok",
            }
        )
    pairwise.sort(key=lambda row: -row["containment"])

    macro_sections: dict[str, list[str]] = {}
    macro_counts: dict[str, int] = {}
    for section in sections:
        for macro in section.result_macros:
            macro_counts[macro] = macro_counts.get(macro, 0) + 1
            macro_sections.setdefault(macro, [])
            if section.name not in macro_sections[macro]:
                macro_sections[macro].append(section.name)

    macro_rows = [
        {
            "claim_id": macro,
            "occurrences": macro_counts[macro],
            "sections": macro_sections[macro],
            "section_spread": len(macro_sections[macro]),
            "verdict": "OVER-REPEATED"
            if len(macro_sections[macro]) > MAX_CLAIM_SECTION_SPREAD
            else "ok",
        }
        for macro in sorted(macro_counts, key=lambda m: (-macro_counts[m], m))
    ]

    trace_macros = [m for m in macro_counts if m.startswith("tr-")]
    reportable_macros = [m for m in macro_counts if not m.startswith("tr-") and m != "bib"]

    failures: list[str] = []
    for row in pairwise:
        if row["verdict"] != "ok":
            failures.append(
                f"章节复述：{row['left']} vs {row['right']} "
                f"4-gram 包含率 {row['containment']:.2%} > {MAX_PAIRWISE_CONTAINMENT:.0%}"
            )
    for row in macro_rows:
        if row["verdict"] != "ok":
            failures.append(
                f"数值重复：{row['claim_id']} 在 {row['section_spread']} 个章节出现 "
                f"(>{MAX_CLAIM_SECTION_SPREAD})：{', '.join(row['sections'])}"
            )
    for section in sections:
        if section.word_count >= 80 and section.type_token_ratio < MIN_TYPE_TOKEN_RATIO:
            failures.append(
                f"词汇贫乏：{section.name} type-token ratio "
                f"{section.type_token_ratio:.2f} < {MIN_TYPE_TOKEN_RATIO}"
            )
    if not reportable_macros:
        failures.append(
            "正文一个 \\result{} 引用都没有 —— 数值不是来自台账（这是硬失败，不是风格问题）"
        )

    return {
        "directory": str(sections[0].path.parent) if sections else "",
        "ngram": ngram,
        "thresholds": {
            "max_pairwise_containment": MAX_PAIRWISE_CONTAINMENT,
            "max_claim_section_spread": MAX_CLAIM_SECTION_SPREAD,
            "min_type_token_ratio": MIN_TYPE_TOKEN_RATIO,
        },
        "sections": [
            {
                "name": s.name,
                "word_count": s.word_count,
                "type_token_ratio": round(s.type_token_ratio, 4),
                "result_macro_occurrences": len(s.result_macros),
                "distinct_result_macros": len(set(s.result_macros)),
            }
            for s in sections
        ],
        "pairwise": pairwise,
        "claim_macros": macro_rows,
        "totals": {
            "sections": len(sections),
            "result_macro_occurrences": sum(len(s.result_macros) for s in sections),
            "distinct_result_macros": len(macro_counts),
            "trace_macros": len(trace_macros),
            "reportable_macros": len(reportable_macros),
            "repeated_claim_occurrences": sum(
                row["occurrences"] - 1 for row in macro_rows if row["occurrences"] > 1
            ),
        },
        "failures": failures,
        "passed": not failures,
    }


def render(result: dict, top_pairs: int = 12) -> str:
    """把结果渲染成人可读的文本报告。"""
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append(f"写作重复度审计：{result['directory']}")
    lines.append("=" * 78)

    lines.append("")
    lines.append("章节概览")
    lines.append(
        f"  {'section':<18s} {'words':>7s} {'TTR':>7s} {'result{}':>9s} {'distinct':>9s}"
    )
    for row in result["sections"]:
        flag = ""
        if row["word_count"] >= 80 and row["type_token_ratio"] < result["thresholds"][
            "min_type_token_ratio"
        ]:
            flag = "  <- 词汇贫乏"
        lines.append(
            f"  {row['name']:<18s} {row['word_count']:>7d} "
            f"{row['type_token_ratio']:>7.3f} {row['result_macro_occurrences']:>9d} "
            f"{row['distinct_result_macros']:>9d}{flag}"
        )

    lines.append("")
    lines.append("跨章节 4-gram 包含率 TOP")
    lines.append(f"  {'left':<16s} {'right':<16s} {'containment':>12s} {'shared':>8s}")
    for row in result["pairwise"][:top_pairs]:
        flag = "  <- RESTATES" if row["verdict"] != "ok" else ""
        lines.append(
            f"  {row['left']:<16s} {row['right']:<16s} "
            f"{row['containment']:>11.2%} {row['shared_ngrams']:>8d}{flag}"
        )

    lines.append("")
    lines.append("同一 claim 被讲了几遍")
    if not result["claim_macros"]:
        lines.append("  （正文没有任何 \\result{} 引用）")
    for row in result["claim_macros"]:
        flag = "  <- OVER-REPEATED" if row["verdict"] != "ok" else ""
        lines.append(
            f"  {row['claim_id']:<16s} x{row['occurrences']:<3d} "
            f"sections={row['section_spread']} "
            f"[{', '.join(row['sections'])}]{flag}"
        )

    totals = result["totals"]
    lines.append("")
    lines.append("汇总")
    lines.append(f"  章节数                       = {totals['sections']}")
    lines.append(f"  \\result{{}} 出现总次数         = {totals['result_macro_occurrences']}")
    lines.append(f"  不同 claim 数                = {totals['distinct_result_macros']}")
    lines.append(f"  实质 claim 数（非 tr- 追踪）  = {totals['reportable_macros']}")
    lines.append(f"  重复引用的多余次数            = {totals['repeated_claim_occurrences']}")

    lines.append("")
    if result["passed"]:
        lines.append("结论：PASS（未触及任何阈值）")
    else:
        lines.append(f"结论：FAIL（{len(result['failures'])} 条）")
        for item in result["failures"]:
            lines.append(f"  - {item}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="论文写作重复度审计")
    parser.add_argument("directory", type=Path, help="含 .tex 章节的目录")
    parser.add_argument("--json", type=Path, default=None, help="把结果另存为 JSON")
    parser.add_argument("--ngram", type=int, default=4, help="n-gram 阶数，默认 4")
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"[error] 不是目录：{args.directory}", file=sys.stderr)
        return 2

    sections = load_sections(args.directory)
    if not sections:
        print(f"[error] 目录里没有可读的 .tex：{args.directory}", file=sys.stderr)
        return 2

    result = audit(sections, ngram=args.ngram)
    print(render(result))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[write] {args.json}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
