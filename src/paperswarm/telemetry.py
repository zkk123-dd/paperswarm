# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""资源遥测台账：把每一次 LLM 调用与流水线事件落成可追溯的 JSONL。

为什么单独成层
--------------
本项目有两份交付物依赖同一批数据：

1. 大赛要求的「资源报告：Token 消耗、运行时长等统计数据/日志（可追溯）」；
2. 论文本身的实验部分（成本 / 吞吐 / 成功率）。

因此遥测不是"顺带打的日志"，而是**一级产物**：每条记录都带 ``run_id`` 与
``span``，可以按角色、按阶段、按模型任意切片，且与证据台账的 ``artifact``
通过 run_id 互相印证。

成本口径（重要）
----------------
本模块**不内置任何模型价格表**。原因很直接：价格是随时会变的外部事实，
把它硬编进代码等于给自己埋一个会静默失真的数据源。成本只有在调用方显式
提供单价（通过 ``LLMConfig.price_per_1k_*``）时才会计算，否则 ``cost_usd``
为 ``None`` 并在汇总里标注 ``cost_basis = "unpriced"``。
本机 Ollama 的后端单价为 0，属于显式已知，会写成 ``0.0`` 而不是 ``None``。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

LLM_CALLS_FILE = "llm_calls.jsonl"
EVENTS_FILE = "events.jsonl"
RUN_SUMMARY_FILE = "run_summary.json"


def _utc_now() -> str:
    """ISO 8601 UTC 时间戳（毫秒级）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class LLMCallRecord:
    """一次 LLM 调用的资源记录。"""

    ts: str
    run_id: str
    span: str
    role: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    attempts: int
    retries: int
    finish_reason: str
    tool_call_count: int
    ok: bool
    error: str = ""
    cost_usd: float | None = None
    attempt_log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EventRecord:
    """一条流水线事件（阶段进出、门控结果、决策点）。"""

    ts: str
    run_id: str
    span: str
    kind: str
    ok: bool
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "entry": "event"}


class Telemetry:
    """运行期遥测：追加写 JSONL，并提供汇总。

    Args:
        root: 落盘目录，通常为 ``runs/<run_id>/``。
        run_id: 本次运行的标识，写进每一条记录。
    """

    def __init__(self, root: Path | str, run_id: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._calls: list[LLMCallRecord] = []
        self._events: list[EventRecord] = []

    # -- 路径 ---------------------------------------------------------------

    @property
    def calls_path(self) -> Path:
        return self.root / LLM_CALLS_FILE

    @property
    def events_path(self) -> Path:
        return self.root / EVENTS_FILE

    @property
    def summary_path(self) -> Path:
        return self.root / RUN_SUMMARY_FILE

    # -- 写入 ---------------------------------------------------------------

    def _append(self, path: Path, record: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()

    def record_call(
        self,
        *,
        span: str,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        attempts: int,
        finish_reason: str = "",
        tool_call_count: int = 0,
        ok: bool = True,
        error: str = "",
        price_per_1k_prompt: float | None = None,
        price_per_1k_completion: float | None = None,
        attempt_log: Sequence[Mapping[str, Any]] | None = None,
    ) -> LLMCallRecord:
        """记录一次 LLM 调用。

        Args:
            span: 流水线阶段标识，如 ``writer.draft``。
            role: 发起调用的角色名，如 ``writer``。
            model: 实际响应中报告的模型名（以后端回报为准，不用配置值顶替）。
            prompt_tokens / completion_tokens: 后端回报的用量。
            latency_ms: 端到端耗时（含全部重试等待）。
            attempts: 总尝试次数。
            finish_reason: 后端给出的结束原因。
            tool_call_count: 本次返回的工具调用数量。
            ok: 调用是否成功。
            error: 失败原因（成功时为空）。
            price_per_1k_prompt / price_per_1k_completion: 单价；两者都给出才计算成本。
            attempt_log: 重试明细。

        Returns:
            落盘后的记录。
        """
        cost: float | None = None
        if price_per_1k_prompt is not None and price_per_1k_completion is not None:
            cost = (
                prompt_tokens / 1000.0 * price_per_1k_prompt
                + completion_tokens / 1000.0 * price_per_1k_completion
            )
        record = LLMCallRecord(
            ts=_utc_now(),
            run_id=self.run_id,
            span=span,
            role=role,
            model=model,
            prompt_tokens=int(prompt_tokens),
            completion_tokens=int(completion_tokens),
            total_tokens=int(prompt_tokens) + int(completion_tokens),
            latency_ms=int(latency_ms),
            attempts=int(attempts),
            retries=max(0, int(attempts) - 1),
            finish_reason=finish_reason,
            tool_call_count=int(tool_call_count),
            ok=bool(ok),
            error=error,
            cost_usd=cost,
            attempt_log=[dict(item) for item in (attempt_log or [])],
        )
        self._append(self.calls_path, record.to_dict())
        self._calls.append(record)
        return record

    def record_event(
        self,
        *,
        span: str,
        kind: str,
        ok: bool = True,
        detail: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """记录一条流水线事件。"""
        record = EventRecord(
            ts=_utc_now(),
            run_id=self.run_id,
            span=span,
            kind=kind,
            ok=bool(ok),
            detail=detail,
            payload=dict(payload or {}),
        )
        self._append(self.events_path, record.to_dict())
        self._events.append(record)
        return record

    # -- 汇总 ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """汇总当前运行期的资源消耗。

        Returns:
            含总量、分位延迟、分角色/分阶段切片与成本基准标注的字典。
        """
        calls = self._calls
        latencies = sorted(item.latency_ms for item in calls)
        failed = [item for item in calls if not item.ok]
        priced = [item for item in calls if item.cost_usd is not None]

        def _slice(key: str) -> dict[str, Any]:
            buckets: dict[str, dict[str, Any]] = {}
            for item in calls:
                bucket = buckets.setdefault(
                    str(getattr(item, key)),
                    {"calls": 0, "tokens": 0, "latency_ms": 0},
                )
                bucket["calls"] += 1
                bucket["tokens"] += item.total_tokens
                bucket["latency_ms"] += item.latency_ms
            return buckets

        summary: dict[str, Any] = {
            "run_id": self.run_id,
            "root": str(self.root),
            "calls": len(calls),
            "calls_ok": len(calls) - len(failed),
            "calls_failed": len(failed),
            "success_rate": (len(calls) - len(failed)) / len(calls) if calls else 0.0,
            "prompt_tokens": sum(item.prompt_tokens for item in calls),
            "completion_tokens": sum(item.completion_tokens for item in calls),
            "total_tokens": sum(item.total_tokens for item in calls),
            "latency_ms_total": sum(item.latency_ms for item in calls),
            "latency_ms_p50": statistics.median(latencies) if latencies else 0,
            "latency_ms_p95": (
                latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]
                if latencies
                else 0
            ),
            "retries_total": sum(item.retries for item in calls),
            "tool_calls_total": sum(item.tool_call_count for item in calls),
            "events": len(self._events),
            "by_role": _slice("role"),
            "by_span": _slice("span"),
            "by_model": _slice("model"),
            "cost_basis": "priced" if priced and len(priced) == len(calls) else "unpriced",
        }
        if priced:
            summary["cost_usd_total"] = sum(
                item.cost_usd for item in priced if item.cost_usd is not None
            )
        else:
            summary["cost_usd_total"] = None
        return summary

    def write_summary(self, extra: Mapping[str, Any] | None = None) -> Path:
        """把汇总写进 ``run_summary.json`` 并返回路径。"""
        payload = self.summary()
        if extra:
            payload = {**payload, **dict(extra)}
        self.summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return self.summary_path
