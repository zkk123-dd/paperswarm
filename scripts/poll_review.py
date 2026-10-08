#!/usr/bin/env python
"""轮询 Stanford Agentic Reviewer 的评测状态，结果就绪后落盘。

用法::

    python scripts/poll_review.py --token <TOKEN> --wait-seconds 1800

退出码：0 = 已拿到评测；3 = 仍在处理中（超时退出，不代表失败）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://paperreview.ai"
TIMEOUT = 60
READY_STATES = {"completed", "complete", "done", "finished", "ready", "reviewed", "success"}
FAILED_STATES = {"failed", "error", "rejected"}


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def fetch(token: str, path: str) -> object:
    try:
        r = requests.get(f"{BASE}{path}", timeout=TIMEOUT)
        try:
            return r.json()
        except Exception:
            return {"http": r.status_code, "text": r.text[:1500]}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _collect_numbers(obj, out, prefix=""):
    """从任意嵌套结构里收集 (key, 数值) 对，便于找出评分字段。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_numbers(v, out, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _collect_numbers(v, out, f"{prefix}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append((prefix, obj))


def summarize(review: dict) -> dict:
    nums: list[tuple[str, float]] = []
    _collect_numbers(review, nums)
    interesting = [
        (k, v) for k, v in nums
        if any(t in k.lower() for t in ("score", "rating", "overall", "total", "decision", "confidence", "soundness", "contribution", "presentation"))
    ]
    return {
        "top_level_keys": sorted(review.keys()) if isinstance(review, dict) else None,
        "score_like_fields": interesting,
        "all_numeric_fields": nums[:80],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--wait-seconds", type=int, default=1800)
    ap.add_argument("--interval", type=int, default=25)
    ap.add_argument("--out", default="reports/review_result.json")
    ap.add_argument("--state-file", default="reports/review_status_latest.json")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    state_file = Path(args.state_file)
    deadline = time.time() + args.wait_seconds

    last_status = None
    while True:
        status = fetch(args.token, f"/api/status/{args.token}")
        state_file.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
        cur = str(status.get("status", "")).lower() if isinstance(status, dict) else "?"
        if cur != last_status:
            log(f"status={cur}  title={status.get('title','')!r}  venue={status.get('venue','')!r}")
            last_status = cur

        if cur in FAILED_STATES:
            log(f"服务端报告失败状态: {json.dumps(status)[:400]}")
            return 1

        if cur in READY_STATES:
            review = fetch(args.token, f"/api/review/{args.token}")
            result = {
                "fetched_at": ts(),
                "token": args.token,
                "status": status,
                "review": review,
                "summary": summarize(review) if isinstance(review, dict) else None,
            }
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"评测已就绪，写入 {out}")
            log(f"评分相关字段: {json.dumps(result['summary']['score_like_fields'], ensure_ascii=False)}")
            return 0

        if time.time() >= deadline:
            log(f"等待 {args.wait_seconds}s 后仍在 {cur}，退出（非失败）。token={args.token}")
            return 3

        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
