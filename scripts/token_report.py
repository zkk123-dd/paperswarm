#!/usr/bin/env python
# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""把 ``runs/*/`` 的遥测台账聚合成一份 tokens / 时延 / 成本报告。

为什么单独做一个脚本
--------------------
遥测层（``paperswarm.telemetry``）只负责**逐次调用**落盘（``llm_calls.jsonl``）
与**单次运行**汇总（``run_summary.json``）。跨运行的口径聚合一旦写死进遥测层，
口径变更就得改核心代码。放在这里则是一条纯读取的旁路：**不改动任何产物**，
只是把已有台账重新切片。

硬约束
------
- **只读**：不写入 ``runs/`` 下任何文件。
- **不内置价格表**：单价是外部事实，会变。脚本只用台账里已经记下的
  ``cost_usd``；缺失即记 ``unpriced``，绝不用"估计价"填补。
- **小数字不四舍五入成假精确**：tokens 是整数，时延保留毫秒整数。

用法::

    python scripts/token_report.py
    python scripts/token_report.py --runs-root runs --out reports/token_report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]

#: 运行目录下按此优先级挑选"每次 LLM 调用"的明细台账。
CALL_LEDGER_NAMES = ("llm_calls.jsonl",)


#: 容器目录：其子目录才是真实运行，本层自身不计入统计。
CONTAINER_DIR_NAMES = ("_history",)


def _iter_run_dirs(runs_root: Path) -> Iterable[Path]:
    """产出真实运行目录。

    ``_history/`` 是归档容器，它自己不是一次运行——把它当成一个 run 会得出
    "tokens=0" 的假条目并把 runs 计数加一。这里展开一层，只收它的子目录。
    """
    if not runs_root.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(runs_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name in CONTAINER_DIR_NAMES:
            found.extend(
                sorted((p for p in entry.iterdir() if p.is_dir()), key=lambda p: p.name)
            )
            continue
        found.append(entry)
    return found


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """逐行读 JSONL；坏行跳过并计数，不让一条坏记录毁掉整份报告。"""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _load_summary(run_dir: Path) -> dict[str, Any] | None:
    summary_path = run_dir / "run_summary.json"
    if not summary_path.is_file():
        return None
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _load_calls(run_dir: Path) -> list[dict[str, Any]]:
    for name in CALL_LEDGER_NAMES:
        records = _read_jsonl(run_dir / name)
        if records:
            return records
    return []


def _percentile(values: Sequence[int], fraction: float) -> int | None:
    """最近秩百分位。样本 < 2 时返回 None——单个样本算不出分布。"""
    if len(values) < 2:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return int(ordered[index])


def _summarize_calls(calls: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从逐次调用台账里聚合 tokens / 时延 / 成本 / 重试。"""
    if not calls:
        return {
            "calls": 0,
            "ok": 0,
            "failed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "latency_ms_total": 0,
            "latency_ms_p50": None,
            "latency_ms_p95": None,
            "retries": 0,
            "cost_basis": "unpriced",
            "cost_usd": None,
        }

    prompt_tokens = sum(int(c.get("prompt_tokens") or 0) for c in calls)
    completion_tokens = sum(int(c.get("completion_tokens") or 0) for c in calls)
    total_tokens = sum(
        int(c.get("total_tokens") or 0) for c in calls
    ) or (prompt_tokens + completion_tokens)
    latencies = [int(c.get("latency_ms") or 0) for c in calls]
    retries = sum(int(c.get("retries") or 0) for c in calls)

    priced = [c.get("cost_usd") for c in calls]
    known = [float(value) for value in priced if value is not None]
    if not known:
        cost_basis, cost_usd = "unpriced", None
    elif len(known) == len(priced):
        cost_basis, cost_usd = "priced", sum(known)
    else:
        # 混合：部分调用有价，部分没有。上报已计价部分，并显式标注口径不完整。
        cost_basis, cost_usd = "partially_priced", sum(known)

    return {
        "calls": len(calls),
        "ok": sum(1 for c in calls if c.get("ok")),
        "failed": sum(1 for c in calls if not c.get("ok")),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "latency_ms_total": sum(latencies),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "retries": retries,
        "cost_basis": cost_basis,
        "cost_usd": cost_usd,
    }


def _group_by(calls: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        label = str(call.get(key) or "unknown")
        buckets.setdefault(label, []).append(call)
    return {label: _summarize_calls(items) for label, items in sorted(buckets.items())}


def build_report(runs_root: Path) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    totals = {
        "runs": 0,
        "runs_with_llm": 0,
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "latency_ms_total": 0,
        "retries": 0,
        "cost_usd": 0.0,
        "unpriced_calls": 0,
    }

    for run_dir in _iter_run_dirs(runs_root):
        calls = _load_calls(run_dir)
        summary = _load_summary(run_dir) or {}
        call_stats = _summarize_calls(calls)

        pdf = summary.get("pdf") or {}
        entry = {
            "run_id": summary.get("run_id") or run_dir.name,
            "dir": str(run_dir),
            "kind": run_dir.name.split("-")[0],
            "ok": summary.get("ok"),
            "stop_reason": summary.get("stop_reason"),
            "agent_steps": summary.get("agent_steps"),
            "tool_calls_total": summary.get("tool_calls_total"),
            "experiment_seconds": summary.get("experiment_seconds")
            or summary.get("experiment_wall_seconds"),
            "claims_total": summary.get("claims_total"),
            "claims_passed": summary.get("claims_passed"),
            "gate_ok": summary.get("gate_ok"),
            "pdf": {
                "pages": pdf.get("pages"),
                "size_bytes": pdf.get("size_bytes"),
                "pages_ok": pdf.get("pages_ok"),
                "size_ok": pdf.get("size_ok"),
            }
            if pdf
            else None,
            "llm": call_stats,
            "by_role": _group_by(calls, "role"),
            "by_span": _group_by(calls, "span"),
            "by_model": _group_by(calls, "model"),
        }
        runs.append(entry)

        totals["runs"] += 1
        if call_stats["calls"]:
            totals["runs_with_llm"] += 1
            totals["calls"] += call_stats["calls"]
            totals["prompt_tokens"] += call_stats["prompt_tokens"]
            totals["completion_tokens"] += call_stats["completion_tokens"]
            totals["total_tokens"] += call_stats["total_tokens"]
            totals["latency_ms_total"] += call_stats["latency_ms_total"]
            totals["retries"] += call_stats["retries"]
            if call_stats["cost_usd"] is not None:
                totals["cost_usd"] += call_stats["cost_usd"]
            if call_stats["cost_basis"] != "priced":
                totals["unpriced_calls"] += call_stats["calls"]

    totals["cost_basis"] = "priced" if totals["unpriced_calls"] == 0 else "partially_priced"
    if totals["unpriced_calls"]:
        totals["cost_usd"] = round(totals["cost_usd"], 6)

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runs_root": str(runs_root),
        "note": (
            "本报告只做跨运行聚合，不内置任何模型价格表。cost_basis=priced 表示"
            "每次调用都有显式单价（本机 Ollama 单价为已知的 0），unpriced 表示"
            "台账里没有价格，脚本不做估算。"
        ),
        "totals": totals,
        "runs": runs,
    }


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "# PaperSwarm Token / 时延 / 成本报告（自动生成）",
        "",
        f"> 生成时间：{report['generated_at']}　扫描目录：`{report['runs_root']}`",
        "> " + report["note"],
        "",
        "## 汇总",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 运行目录数 | {totals['runs']}（其中含 LLM 调用：{totals['runs_with_llm']}） |",
        f"| LLM 调用次数 | {totals['calls']} |",
        f"| 输入 tokens | {totals['prompt_tokens']:,} |",
        f"| 输出 tokens | {totals['completion_tokens']:,} |",
        f"| **总 tokens** | **{totals['total_tokens']:,}** |",
        f"| LLM 累计时延 | {totals['latency_ms_total'] / 1000:.1f} s |",
        f"| 重试次数 | {totals['retries']} |",
        f"| 成本口径 | `{totals['cost_basis']}` |",
        f"| 成本（USD） | {totals['cost_usd']} |",
        "",
        "## 逐运行明细",
        "",
        "| run_id | 类型 | ok | steps | 工具 | tokens | P50/P95 (ms) | PDF | 成本 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in report["runs"]:
        llm = run["llm"]
        pdf = run["pdf"] or {}
        p50 = llm["latency_ms_p50"] if llm["latency_ms_p50"] is not None else "-"
        p95 = llm["latency_ms_p95"] if llm["latency_ms_p95"] is not None else "-"
        pdf_cell = (
            f"{pdf.get('pages')}p / {pdf.get('size_bytes', 0) / 1024:.0f}KB"
            if pdf.get("pages")
            else "-"
        )
        cost = llm["cost_usd"]
        cost_cell = f"{cost}" if cost is not None else f"({llm['cost_basis']})"
        lines.append(
            f"| `{run['run_id']}` | {run['kind']} | {run['ok']} | "
            f"{run['agent_steps'] if run['agent_steps'] is not None else '-'} | "
            f"{run['tool_calls_total'] if run['tool_calls_total'] is not None else '-'} | "
            f"{llm['total_tokens']:,} | {p50}/{p95} | {pdf_cell} | {cost_cell} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="聚合 runs/ 下的 tokens/时延/成本台账。")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--out", default=str(ROOT / "reports" / "token_report.json"))
    parser.add_argument("--markdown", default=str(ROOT / "reports" / "token_report.md"))
    args = parser.parse_args(argv)

    report = build_report(Path(args.runs_root))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path = Path(args.markdown)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    totals = report["totals"]
    print(
        f"runs={totals['runs']} calls={totals['calls']} "
        f"tokens={totals['total_tokens']} cost_basis={totals['cost_basis']} "
        f"cost_usd={totals['cost_usd']}"
    )
    print(f"report -> {out_path}")
    print(f"report -> {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
