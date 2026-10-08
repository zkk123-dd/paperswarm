#!/usr/bin/env python
# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""提交前自检：在**没有**任何外部凭证的情况下，把能本地验的都验掉。

为什么需要这个脚本
------------------
外部评测（Stanford Agentic Reviewer）的硬约束里有两条是**纯本地可判定的**：

* PDF ≤ 10 MB；
* 只分析前 15 页 —— 所以正文必须在 15 页内把贡献说清。

再加上几条本地可判定的质量门槛：论文必须是英文、必须是双盲可提交的形态、
正文里不能残留未回填的 ``\\result{...}`` 宏。这些检查**不需要网络、不需要 token**，
所以没有理由等到提交那一天才发现不合格。

脚本只做**判定**，不做网络调用、不做提交。**它不会替你提交任何东西。**

用法::

    python scripts/review_readiness.py                 # 自动挑 runs/ 下最新的 main.pdf
    python scripts/review_readiness.py --pdf path.pdf  # 指定文件
    python scripts/review_readiness.py --json out.json # 额外落一份机器可读报告

退出码 0 表示全部通过，1 表示有 FAIL。WARN 不影响退出码（但会打印）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]

#: 已核实的官方硬约束（来源：paperreview.ai 首页表单）。
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_ANALYZED_PAGES = 15

#: 双盲提交的形态标记。ICLR 模板默认输出 "Anonymous authors" / "Paper under double-blind review"。
ANONYMITY_MARKERS = ("anonymous", "double-blind", "under review")

#: 未回填的宏。出现即说明组装链断了。
UNRESOLVED_MACRO = re.compile(r"\\result\s*\{")

#: 纯数字 token（ICLR 模板的行号槽会产出大量 "000".."053"），做语言判定时要排除。
_PURE_NUMBER = re.compile(r"^\d+$")


def find_latest_pdf(runs_root: Path) -> Path | None:
    """按修改时间挑最新的 ``main.pdf``。"""
    candidates = [p for p in runs_root.glob("*/paper/main.pdf") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _english_ratio(text: str) -> float:
    """返回 ASCII 字母在"非纯数字 token"里的占比。

    只用字母占比判语言，不用停用词表——停用词表会引入一个需要维护的外部数据源，
    而这里要判的只是"这段文字是不是英文"，粗判足够，且不引入维护负担。
    """
    tokens = [t for t in text.split() if not _PURE_NUMBER.match(t)]
    if not tokens:
        return 0.0
    letters = sum(1 for ch in "".join(tokens) if ch.isascii() and ch.isalpha())
    total = sum(1 for ch in "".join(tokens) if not ch.isspace())
    return letters / total if total else 0.0


def inspect_pdf(pdf_path: Path) -> dict[str, Any]:
    """读 PDF 并产出逐项检查结果。任何读取失败都变成 FAIL 项，而不是抛异常。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: Any) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    add("pdf_exists", "PASS" if pdf_path.is_file() else "FAIL", str(pdf_path))
    if not pdf_path.is_file():
        return {"checks": checks, "facts": {}}

    size_bytes = pdf_path.stat().st_size
    add(
        "size_within_10mb",
        "PASS" if size_bytes <= MAX_PDF_BYTES else "FAIL",
        {
            "bytes": size_bytes,
            "mib": round(size_bytes / 1024 / 1024, 4),
            "limit_bytes": MAX_PDF_BYTES,
        },
    )

    try:
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover - 依赖缺失时降级
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            add(
                "pdf_readable",
                "FAIL",
                "需要 pymupdf（pip install pymupdf）才能校验页数与正文",
            )
            return {"checks": checks, "facts": {"size_bytes": size_bytes}}

    try:
        document = pymupdf.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - 校验器要报告任何读取失败
        add("pdf_readable", "FAIL", f"{type(exc).__name__}: {exc}")
        return {"checks": checks, "facts": {"size_bytes": size_bytes}}

    try:
        pages = document.page_count
        page_texts = [document.load_page(i).get_text() for i in range(pages)]
    finally:
        document.close()

    add("pdf_readable", "PASS", {"pages": pages})
    add(
        "analyzed_pages_within_15",
        "PASS" if pages <= MAX_ANALYZED_PAGES else "FAIL",
        {"pages": pages, "limit": MAX_ANALYZED_PAGES},
    )

    # 前 15 页决定分数，所以语言与形态也只看这 15 页。
    analyzed = "\n".join(page_texts[:MAX_ANALYZED_PAGES])
    first_page = page_texts[0] if page_texts else ""

    ratio = _english_ratio(analyzed)
    add(
        "english_language",
        "PASS" if ratio >= 0.60 else "FAIL",
        {"ascii_letter_ratio": round(ratio, 3), "threshold": 0.60},
    )

    lowered = analyzed.lower()
    matched = [marker for marker in ANONYMITY_MARKERS if marker in lowered]
    add(
        "anonymized_for_double_blind",
        "PASS" if matched else "WARN",
        {"markers_found": matched, "expected_any_of": list(ANONYMITY_MARKERS)},
    )

    unresolved = UNRESOLVED_MACRO.findall(analyzed)
    add(
        "no_unresolved_result_macro",
        "PASS" if not unresolved else "FAIL",
        {"occurrences": len(unresolved)},
    )

    # 首页必须自带贡献说明：只分析前 15 页的服务，首页信息密度直接决定第一印象。
    head_tokens = [t for t in first_page.split() if not _PURE_NUMBER.match(t)]
    add(
        "first_page_has_content",
        "PASS" if len(head_tokens) >= 80 else "WARN",
        {"first_page_tokens": len(head_tokens), "threshold": 80},
    )

    facts = {
        "size_bytes": size_bytes,
        "pages": pages,
        "ascii_letter_ratio": round(ratio, 3),
        "anonymity_markers": matched,
        "unresolved_macros": len(unresolved),
        "first_page_tokens": len(head_tokens),
        "title_line": (head_tokens[:14] if head_tokens else []),
    }
    return {"checks": checks, "facts": facts}


def render_markdown(pdf_path: Path, report: dict[str, Any]) -> str:
    lines = [
        "# 外部评测提交前自检（本地，无网络）",
        "",
        f"> 文件：`{pdf_path}`",
        f"> 时间：{report['generated_at']}",
        "> 本检查不联网、不提交，仅判定官方硬约束与本地可判定的质量门槛。",
        "",
        "| 检查 | 状态 | 明细 |",
        "|---|---|---|",
    ]
    for item in report["checks"]:
        detail = item["detail"]
        if isinstance(detail, dict):
            detail = ", ".join(f"{k}={v}" for k, v in detail.items())
        lines.append(f"| `{item['check']}` | **{item['status']}** | {detail} |")
    lines += [
        "",
        f"**汇总**：PASS {report['counts']['PASS']} / "
        f"WARN {report['counts']['WARN']} / FAIL {report['counts']['FAIL']}",
        "",
    ]
    facts = report.get("facts") or {}
    if facts.get("title_line"):
        lines.append("首页题名（前 14 个 token，仅供人工核对）：")
        lines.append("")
        lines.append("> " + " ".join(str(t) for t in facts["title_line"]))
        lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="外部评测提交前自检（离线，不提交任何内容）。"
    )
    parser.add_argument("--pdf", default=None, help="待检 PDF；缺省取 runs/ 下最新的 main.pdf")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--json", default=str(ROOT / "reports" / "review_readiness.json"))
    parser.add_argument(
        "--markdown", default=str(ROOT / "reports" / "review_readiness.md")
    )
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf) if args.pdf else find_latest_pdf(Path(args.runs_root))
    if pdf_path is None:
        print("[FAIL] 在 runs/ 下找不到任何 main.pdf；先跑 scripts/full_paper.py", file=sys.stderr)
        return 1

    result = inspect_pdf(pdf_path)
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for item in result["checks"]:
        counts[item["status"]] += 1

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "pdf": str(pdf_path),
        "constraints": {
            "max_pdf_bytes": MAX_PDF_BYTES,
            "max_analyzed_pages": MAX_ANALYZED_PAGES,
        },
        "counts": counts,
        "checks": result["checks"],
        "facts": result["facts"],
    }

    json_path = Path(args.json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path = Path(args.markdown)
    md_path.write_text(render_markdown(pdf_path, report), encoding="utf-8")

    print(f"pdf: {pdf_path}")
    for item in result["checks"]:
        print(f"  [{item['status']}] {item['check']}")
    print(
        f"PASS {counts['PASS']} / WARN {counts['WARN']} / FAIL {counts['FAIL']}"
    )
    print(f"report -> {json_path}")
    print(f"report -> {md_path}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
