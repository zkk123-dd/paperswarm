# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""有界 Agent 循环。

本模块实现硬约束 C5：**任何自主循环都必须有界**。上界有五个，且全部显式可查：

================= ==========================================================
上界               触发后的行为
================= ==========================================================
max_steps          步数用尽即停，``stop_reason = "max_steps"``
token 预算         累计 token 超预算即停，``stop_reason = "token_budget"``
墙钟上限           超过 deadline 即停，``stop_reason = "wall_clock"``
循环检测           连续 N 次完全相同的工具调用即停，``stop_reason = "loop_detected"``
必需工具未完成     提醒若干次仍未完成即停，``stop_reason = "required_tools_missing"``
================= ==========================================================

设计取舍
--------
- **不提供无限循环入口**：构造函数强制 ``max_steps >= 1`` 且上限 64，防止调用方
  传入 ``10**9`` 把有界变成无界。
- **危险工具必须人工确认**：``ToolSpec.dangerous=True`` 的工具，其执行前置一个
  ``confirm`` 回调；回调缺失即视为拒绝（fail-closed，而不是 fail-open）。
- **工具异常不静默**：异常被捕获后作为 ``tool`` 消息回灌给模型，并计入
  ``tool_trace``；模型若无法自愈，循环最终仍会被某个上界终止。
- **工具结果截断**：超长结果按 ``max_tool_result_chars`` 截断并显式标注，
  避免单次工具输出吃光上下文。
- **``required_tools`` 是完成条件，不是提示词**：小参数模型很容易"少做一步就宣布
  做完"。靠 prompt 里写"你必须调用 X"是不可验证的约束；把"必需工具是否成功执行过"
  做成循环的完成判据，才是可验证的。提醒次数同样有上限——无限提醒等于无限循环。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from paperswarm.llm import ChatClient, ChatResponse, LLMError
from paperswarm.telemetry import Telemetry

MAX_STEPS_HARD_CAP = 64
"""max_steps 的硬上限。超过即拒绝构造，避免"有界"被参数消解。"""


class ToolError(RuntimeError):
    """工具执行失败（参数非法、前置条件不满足等）。"""


class HumanRejected(RuntimeError):
    """危险操作被人工拒绝。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个可被模型调用的工具。

    Attributes:
        name: 工具名，必须与 JSON Schema 中的 ``function.name`` 一致。
        description: 给模型看的用途说明。写清"什么时候用"比写清"它做什么"更重要。
        parameters: JSON Schema（``type: object`` 层级）。
        handler: 执行体，接收解析后的参数字典，返回可 JSON 序列化的结果。
        dangerous: 是否属于需要人工确认的操作（删除、对外发送、改生产数据等）。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    dangerous: bool = False

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 tools 数组元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCallTrace:
    """一次工具调用的执行轨迹（用于审计与回放）。"""

    step: int
    name: str
    arguments: dict[str, Any]
    ok: bool
    result_preview: str
    error: str = ""
    elapsed_ms: int = 0
    confirmed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "name": self.name,
            "arguments": self.arguments,
            "ok": self.ok,
            "result_preview": self.result_preview,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
            "confirmed": self.confirmed,
        }


@dataclass
class LoopResult:
    """一次有界循环的完整结果。"""

    final_text: str
    stop_reason: str
    steps: int
    prompt_tokens: int
    completion_tokens: int
    tool_trace: list[ToolCallTrace] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def ok(self) -> bool:
        """是否正常收敛（模型主动给出最终答复）。"""
        return self.stop_reason == "final_answer"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_reason": self.stop_reason,
            "ok": self.ok,
            "steps": self.steps,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": [item.to_dict() for item in self.tool_trace],
        }


class AgentLoop:
    """有界工具调用循环。

    Args:
        client: LLM 客户端。
        tools: 可用工具集。
        telemetry: 遥测台账。
        run_id: 运行标识。
        max_steps: 最大步数（1..64）。
        token_budget: 累计 token 预算；超限即停。
        wall_clock_seconds: 墙钟上限（秒）。
        repeat_limit: 连续出现多少次完全相同的工具调用即判定为死循环。
        max_tool_result_chars: 单条工具结果写回上下文的最大字符数。
        required_tools: 本次任务必须**成功执行过至少一次**的工具名。
            模型在未满足该条件时试图收尾，会被提醒一次并继续循环；
            提醒次数用尽仍未完成，则以 ``required_tools_missing`` 结束，
            绝不把"少做一步"伪装成成功。
        max_completion_nudges: 最多提醒多少次（防止提醒本身变成无限循环）。
        confirm: 危险操作确认回调；签名 ``(tool_name, arguments) -> bool``。
            未提供时，任何 ``dangerous=True`` 的工具都会被拒绝（fail-closed）。
    """

    def __init__(
        self,
        *,
        client: ChatClient,
        tools: Sequence[ToolSpec],
        telemetry: Telemetry,
        run_id: str,
        max_steps: int = 10,
        token_budget: int = 200_000,
        wall_clock_seconds: float = 900.0,
        repeat_limit: int = 3,
        max_tool_result_chars: int = 4000,
        required_tools: Sequence[str] = (),
        max_completion_nudges: int = 2,
        confirm: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        if not 1 <= max_steps <= MAX_STEPS_HARD_CAP:
            raise ValueError(
                f"max_steps 必须在 1..{MAX_STEPS_HARD_CAP} 之间（有界性是硬约束），收到 {max_steps}"
            )
        if token_budget <= 0:
            raise ValueError("token_budget 必须为正数")
        if wall_clock_seconds <= 0:
            raise ValueError("wall_clock_seconds 必须为正数")
        if repeat_limit < 2:
            raise ValueError("repeat_limit 至少为 2，否则无法区分死循环与正常重试")
        if max_completion_nudges < 0:
            raise ValueError("max_completion_nudges 不能为负")

        self.client = client
        self.tools = {spec.name: spec for spec in tools}
        if len(self.tools) != len(tools):
            raise ValueError("工具名重复：registry 中每个 name 必须唯一")
        unknown_required = [name for name in required_tools if name not in self.tools]
        if unknown_required:
            raise ValueError(
                f"required_tools 含未注册的工具 {unknown_required}；"
                f"可用工具: {sorted(self.tools)}"
            )
        self.required_tools = tuple(dict.fromkeys(required_tools))
        self.max_completion_nudges = max_completion_nudges
        self.telemetry = telemetry
        self.run_id = run_id
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.wall_clock_seconds = wall_clock_seconds
        self.repeat_limit = repeat_limit
        self.max_tool_result_chars = max_tool_result_chars
        self.confirm = confirm

    # -- 工具执行 -----------------------------------------------------------

    def _dispatch(self, name: str, arguments: dict[str, Any], step: int) -> tuple[str, ToolCallTrace]:
        """执行一个工具调用，返回 ``(回灌内容, 轨迹)``。

        任何异常都在这里被转成结构化错误回灌给模型，而不是让整个 run 崩掉——
        模型有机会修正参数后重试；若它修不好，上界会兜住。
        """
        started = time.perf_counter()
        spec = self.tools.get(name)
        if spec is None:
            available = ", ".join(sorted(self.tools)) or "(空)"
            trace = ToolCallTrace(
                step=step,
                name=name,
                arguments=arguments,
                ok=False,
                result_preview="",
                error=f"未注册的工具 {name!r}；可用工具: {available}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return json.dumps({"error": trace.error}, ensure_ascii=False), trace

        confirmed: bool | None = None
        if spec.dangerous:
            approved = bool(self.confirm and self.confirm(name, arguments))
            confirmed = approved
            if not approved:
                trace = ToolCallTrace(
                    step=step,
                    name=name,
                    arguments=arguments,
                    ok=False,
                    result_preview="",
                    error=f"危险操作 {name!r} 未获人工确认，已拒绝执行",
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    confirmed=False,
                )
                return json.dumps({"error": trace.error}, ensure_ascii=False), trace

        try:
            result = spec.handler(arguments)
        except Exception as exc:  # noqa: BLE001 — 工具异常必须转成可回灌的观察值
            detail = f"{type(exc).__name__}: {exc}"
            trace = ToolCallTrace(
                step=step,
                name=name,
                arguments=arguments,
                ok=False,
                result_preview="",
                error=detail,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                confirmed=confirmed,
            )
            return json.dumps({"error": detail}, ensure_ascii=False), trace

        try:
            serialized = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = json.dumps({"result": str(result)}, ensure_ascii=False)
        truncated = len(serialized) > self.max_tool_result_chars
        if truncated:
            serialized = (
                serialized[: self.max_tool_result_chars]
                + f"\n…[结果已截断，原始长度 {len(serialized)} 字符]"
            )
        trace = ToolCallTrace(
            step=step,
            name=name,
            arguments=arguments,
            ok=True,
            result_preview=serialized[:300],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            confirmed=confirmed,
        )
        return serialized, trace

    # -- 主循环 -------------------------------------------------------------

    def run(
        self,
        *,
        task: str,
        system_prompt: str,
        span: str,
        role: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LoopResult:
        """执行一次有界循环，直到模型给出最终答复或触达某个上界。

        Args:
            task: 交给模型的用户侧指令。
            system_prompt: 系统提示词（角色定义与行为约束）。
            span: 遥测阶段标识。
            role: 角色名。
            max_tokens: 覆盖单次生成上限。
            temperature: 覆盖采样温度。

        Returns:
            :class:`LoopResult`。
        """
        deadline = time.monotonic() + self.wall_clock_seconds
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        schemas = [spec.to_openai_schema() for spec in self.tools.values()]
        trace: list[ToolCallTrace] = []
        succeeded: set[str] = set()
        prompt_tokens = 0
        completion_tokens = 0
        last_signature: tuple[str, str] | None = None
        repeat_count = 0
        nudges = 0
        stop_reason = "max_steps"
        final_text = ""

        started_wall = time.monotonic()
        for step in range(1, self.max_steps + 1):
            if time.monotonic() > deadline:
                stop_reason = "wall_clock"
                self.telemetry.record_event(
                    span=span,
                    kind="loop_bound_hit",
                    ok=False,
                    detail=f"墙钟上限 {self.wall_clock_seconds}s 用尽于第 {step} 步",
                )
                break
            if prompt_tokens + completion_tokens >= self.token_budget:
                stop_reason = "token_budget"
                self.telemetry.record_event(
                    span=span,
                    kind="loop_bound_hit",
                    ok=False,
                    detail=(
                        f"token 预算 {self.token_budget} 用尽"
                        f"（已用 {prompt_tokens + completion_tokens}）"
                    ),
                )
                break

            retry_log: list[dict[str, Any]] = []
            call_started = time.perf_counter()
            try:
                response = self.client.chat(
                    messages,
                    tools=schemas or None,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    retry_log=retry_log,
                )
            except LLMError as exc:
                elapsed_ms = int((time.perf_counter() - call_started) * 1000)
                self.telemetry.record_call(
                    span=span,
                    role=role,
                    model=self.client.config.model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    latency_ms=elapsed_ms,
                    attempts=1 + len(retry_log),
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    price_per_1k_prompt=self.client.config.price_per_1k_prompt,
                    price_per_1k_completion=self.client.config.price_per_1k_completion,
                    attempt_log=retry_log,
                )
                stop_reason = "llm_error"
                final_text = f"[LLM 调用失败] {type(exc).__name__}: {exc}"
                break

            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            self.telemetry.record_call(
                span=span,
                role=role,
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                latency_ms=response.latency_ms,
                attempts=response.attempts,
                finish_reason=response.finish_reason,
                tool_call_count=len(response.tool_calls),
                ok=True,
                price_per_1k_prompt=self.client.config.price_per_1k_prompt,
                price_per_1k_completion=self.client.config.price_per_1k_completion,
                attempt_log=retry_log,
            )

            if not response.tool_calls:
                missing = [name for name in self.required_tools if name not in succeeded]
                if missing and nudges < self.max_completion_nudges:
                    nudges += 1
                    messages.append({"role": "assistant", "content": response.content})
                    reminder = (
                        "你还没有成功调用以下必需工具："
                        + "、".join(missing)
                        + "。任务尚未完成，现在请调用它们。"
                        "如果之前调用失败过，请先读错误信息、修正参数再重试，不要跳过。"
                    )
                    messages.append({"role": "user", "content": reminder})
                    # 新消息改变了上下文，之前观察到的重复签名不再代表死循环
                    last_signature = None
                    repeat_count = 0
                    self.telemetry.record_event(
                        span=span,
                        kind="completion_blocked",
                        ok=False,
                        detail=f"第 {step} 步模型试图收尾，但必需工具未完成: {missing}",
                        payload={"nudge": nudges, "missing": missing},
                    )
                    continue
                if missing:
                    stop_reason = "required_tools_missing"
                    final_text = response.content
                    messages.append({"role": "assistant", "content": response.content})
                    self.telemetry.record_event(
                        span=span,
                        kind="loop_bound_hit",
                        ok=False,
                        detail=(
                            f"提醒 {nudges} 次后仍缺少必需工具调用: {missing}；"
                            "本次运行判定为未完成，而不是成功"
                        ),
                        payload={"missing": missing},
                    )
                    break
                final_text = response.content
                stop_reason = "final_answer"
                messages.append({"role": "assistant", "content": response.content})
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments_raw,
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )

            for call in response.tool_calls:
                signature = (call.name, call.arguments_raw)
                if signature == last_signature:
                    repeat_count += 1
                else:
                    repeat_count = 1
                    last_signature = signature

                if call.parse_error:
                    observation = json.dumps(
                        {"error": call.parse_error, "received": call.arguments_raw[:300]},
                        ensure_ascii=False,
                    )
                    trace.append(
                        ToolCallTrace(
                            step=step,
                            name=call.name,
                            arguments={},
                            ok=False,
                            result_preview="",
                            error=call.parse_error,
                        )
                    )
                else:
                    observation, item = self._dispatch(call.name, call.arguments, step)
                    trace.append(item)
                    if item.ok:
                        succeeded.add(call.name)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": observation,
                    }
                )

            if repeat_count >= self.repeat_limit:
                stop_reason = "loop_detected"
                self.telemetry.record_event(
                    span=span,
                    kind="loop_bound_hit",
                    ok=False,
                    detail=(
                        f"连续 {repeat_count} 次完全相同的工具调用 "
                        f"{last_signature[0] if last_signature else '?'}，判定为死循环"
                    ),
                )
                break

        elapsed = int((time.monotonic() - started_wall) * 1000)
        self.telemetry.record_event(
            span=span,
            kind="agent_loop_end",
            ok=stop_reason == "final_answer",
            detail=f"stop_reason={stop_reason}",
            payload={
                "steps": step,
                "elapsed_ms": elapsed,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tool_calls": len(trace),
            },
        )
        return LoopResult(
            final_text=final_text,
            stop_reason=stop_reason,
            steps=step,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_trace=trace,
            transcript=messages,
        )
