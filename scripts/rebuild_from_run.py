#!/usr/bin/env python
# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""用已有 run 的草稿与台账**重新组装并编译**，不调用模型。

用途
----
排版的迭代（章节标题、模板、字体）不该每次都重新付费调用模型。草稿
（``paper/drafts/*.tex``，含 ``\\result{}`` 宏）与台账（``ledger/``）都是自足的
中间产物，只要证据门控仍能通过，就能随时重新组装出一份可编译的正文。

这同时是一条**回归通道**：修复组装器后，可以直接拿历史 run 的真实草稿复跑，
证明修复在真实链路上生效，而不是只在单测的合成样本上生效。

用法::

    python scripts/rebuild_from_run.py --run runs/paper-20260929-161913
    python scripts/rebuild_from_run.py --run runs/paper-20260929-161913 \\
        --out runs/paper-20260929-161913/paper_rebuilt
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from full_paper import BIB_ENTRIES, PAPER_TITLE, SECTION_SPECS  # noqa: E402
from paperswarm.assemble import AssemblyError, assemble_paper  # noqa: E402
from paperswarm.evidence import Ledger, LedgerError  # noqa: E402

SECTIONS = tuple((stem, title) for stem, title, *_ in SECTION_SPECS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从已有 run 的草稿+台账重新组装并编译 PDF（不调用模型）。"
    )
    parser.add_argument("--run", required=True, help="run 目录，如 runs/paper-20260929-161913")
    parser.add_argument("--drafts", default="paper/drafts", help="草稿子目录（相对 run）")
    parser.add_argument("--out", default=None, help="输出工作目录（默认 <run>/paper）")
    parser.add_argument("--template-dir", default=str(ROOT / "templates" / "iclr2026"))
    parser.add_argument("--tectonic", default="E:/tools/tectonic/tectonic.exe")
    parser.add_argument("--bundle", default="E:/tools/tectonic/bundle/texlive2024-0312.ttb")
    parser.add_argument("--cache-dir", default="E:/tools/tectonic/cache")
    args = parser.parse_args(argv)

    run_dir = Path(args.run).resolve()
    if not run_dir.is_dir():
        print(f"[FATAL] run 目录不存在: {run_dir}", file=sys.stderr)
        return 1

    drafts_dir = run_dir / args.drafts
    ledger_dir = run_dir / "ledger"
    if not drafts_dir.is_dir():
        print(f"[FATAL] 草稿目录不存在: {drafts_dir}", file=sys.stderr)
        return 1
    if not ledger_dir.is_dir():
        print(f"[FATAL] 台账目录不存在: {ledger_dir}", file=sys.stderr)
        return 1

    # 必须绝对路径：tectonic 的 --outdir 按相对路径解析，且不在本进程的 cwd 下，
    # 传相对路径会得到 "output directory ... does not exist"。
    work_dir = (Path(args.out) if args.out else run_dir / "paper").resolve()
    print(f"[rebuild] run      = {run_dir}")
    print(f"[rebuild] drafts   = {drafts_dir}")
    print(f"[rebuild] ledger   = {ledger_dir}")
    print(f"[rebuild] work_dir = {work_dir}")

    ledger = Ledger(ledger_dir)
    verify = ledger.verify()
    print(f"[rebuild] 台账校验 passed={verify.passed} failed={verify.failed} ok={verify.ok}")
    if not verify.ok:
        print("[FATAL] 台账自校验未通过，拒绝用不干净的证据组装", file=sys.stderr)
        return 1

    try:
        report = assemble_paper(
            template_dir=args.template_dir,
            sections_dir=drafts_dir,
            work_dir=work_dir,
            ledger=ledger,
            title=PAPER_TITLE,
            tectonic=args.tectonic,
            bundle=args.bundle,
            cache_dir=args.cache_dir,
            bib_entries=BIB_ENTRIES,
            sections=SECTIONS,
        )
    except (AssemblyError, LedgerError) as exc:
        print(f"[FATAL] 组装失败: {exc}", file=sys.stderr)
        return 1

    summary = report.to_dict()
    (work_dir / "assembly_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print()
    print("=" * 66)
    if report.compile:
        print(f"编译: ok={report.compile.ok} attempts={report.compile.attempts}")
        if not report.compile.ok:
            print(report.compile.stderr[-3000:])
    if report.pdf:
        info = report.pdf
        print(
            f"PDF : {info.path.name}  pages={info.pages}(<=9:{info.pages_ok}) "
            f"size={info.size_bytes / 1024 / 1024:.3f}MB(<=10:{info.size_ok})"
        )
    for item in report.sections:
        flag = "  " if not item.warnings else "!!"
        print(f"  {flag} {item.name:14s} refs={item.refs} resolved={item.resolved}")
    print("=" * 66)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
