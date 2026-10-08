# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""内核离线测试：重试语义、循环上界、门控拦截、产物引用解析。

为什么这些必须有离线测试
------------------------
1. **重试与上界是概率性行为的边界**，只在"出错时"才触发。靠真实调用去撞这些分支
   既不可复现，也覆盖不全。用假 client 可以把每个分支钉死。
2. **门控的拒绝路径必须被证明会拒绝**。一个从不拒绝的门控和一个没有门控等价，
   而"没拒绝"这件事在正常运行时看不出来。
3. 这些测试不依赖网络、不依赖模型、不依赖 Linux，因此可以在任何机器上作为
   回归基线（Phase 5 评测集的第一层）。

运行::

    python tests/test_kernel_offline.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paperswarm.agentloop import AgentLoop, MAX_STEPS_HARD_CAP, ToolSpec  # noqa: E402
from paperswarm.assemble import (  # noqa: E402
    AssemblyError,
    assert_single_section,
    check_section_prose,
    resolve_sections,
)
from paperswarm.evidence import Ledger, LedgerError  # noqa: E402
from paperswarm.llm import (  # noqa: E402
    ChatClient,
    ChatResponse,
    LLMConfig,
    LLMConfigError,
    LLMPermanentError,
    LLMTransientError,
    ToolCall,
)
from paperswarm.telemetry import Telemetry  # noqa: E402
from paperswarm.tex_gate import SECTION_RE  # noqa: E402
from paperswarm.tools import GateRejected, build_ledger_tools  # noqa: E402

CWD = Path(tempfile.mkdtemp(prefix="paperswarm-tests-"))

_total = 0
_passed = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    global _total, _passed
    _total += 1
    if condition:
        _passed += 1
        print(f"PASS  {name}")
    else:
        _failures.append(name)
        print(f"FAIL  {name}  {detail}")


def expect_raises(name: str, exc_type: type[BaseException], fn, *args, **kwargs) -> BaseException | None:
    """断言某个调用抛出指定异常，返回异常实例以便进一步检查。"""
    try:
        fn(*args, **kwargs)
    except exc_type as exc:  # noqa: BLE001
        check(name, True)
        return exc
    except BaseException as exc:  # noqa: BLE001
        check(name, False, f"期望 {exc_type.__name__}，实到 {type(exc).__name__}: {exc}")
        return None
    check(name, False, f"期望 {exc_type.__name__}，但没有抛出异常")
    return None


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class FakeResponse:
    """模拟 requests 的响应对象。"""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


def _ok_payload(content: str = "hello") -> dict[str, Any]:
    return {
        "model": "fake-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class ScriptedSession:
    """按脚本依次返回响应的假 session。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    def post(self, url: str, headers=None, data=None, timeout=None, **kwargs):  # noqa: ANN001
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _fast_config(**overrides: Any) -> LLMConfig:
    """退避压到 1ms，避免测试等待。"""
    params: dict[str, Any] = {
        "base_url": "http://fake.invalid/v1",
        "model": "fake-model",
        "read_timeout": 5.0,
        "max_retries": 3,
        "backoff_base": 0.001,
        "backoff_cap": 0.001,
    }
    params.update(overrides)
    return LLMConfig(**params)


import requests  # noqa: E402  — 放在假件之后导入，避免与类型注解顺序混淆

# ---------------------------------------------------------------------------
# A. 配置校验
# ---------------------------------------------------------------------------


def test_config() -> None:
    expect_raises("config_rejects_empty_base_url", LLMConfigError, LLMConfig, base_url="", model="m")
    expect_raises(
        "config_rejects_nonpositive_timeout",
        LLMConfigError,
        LLMConfig,
        base_url="http://x/v1",
        model="m",
        read_timeout=0.0,
    )
    expect_raises(
        "config_rejects_negative_retries",
        LLMConfigError,
        LLMConfig,
        base_url="http://x/v1",
        model="m",
        max_retries=-1,
    )
    cfg = _fast_config(api_key="sk-supersecret-abcdef")
    check("config_redacts_api_key", "supersecret" not in json.dumps(cfg.redacted()))
    check("config_endpoint_joined", cfg.endpoint == "http://fake.invalid/v1/chat/completions")


# ---------------------------------------------------------------------------
# B. 重试语义
# ---------------------------------------------------------------------------


def test_retry_semantics() -> None:
    session = ScriptedSession(
        [
            FakeResponse(503, text="busy"),
            FakeResponse(429, text="rate limited"),
            FakeResponse(200, _ok_payload("recovered")),
        ]
    )
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    response = client.chat([{"role": "user", "content": "hi"}])
    check("retry_recovers_on_transient", response.content == "recovered", response.content)
    check("retry_attempts_counted", response.attempts == 3, str(response.attempts))
    check("retry_post_called_three_times", session.calls == 3, str(session.calls))

    session = ScriptedSession([FakeResponse(400, text="bad request")])
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    expect_raises(
        "retry_permanent_error_not_retried",
        LLMPermanentError,
        client.chat,
        [{"role": "user", "content": "hi"}],
    )
    check("retry_permanent_called_once", session.calls == 1, str(session.calls))

    session = ScriptedSession(
        [FakeResponse(500, text="boom"), FakeResponse(500, text="boom"), FakeResponse(500, text="boom"), FakeResponse(500, text="boom")]
    )
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    expect_raises(
        "retry_exhausted_raises_transient",
        LLMTransientError,
        client.chat,
        [{"role": "user", "content": "hi"}],
    )
    check("retry_exhausted_used_all_attempts", session.calls == 4, str(session.calls))

    session = ScriptedSession(
        [
            requests.Timeout("connect timeout"),
            requests.Timeout("read timeout"),
            FakeResponse(200, _ok_payload("after timeout")),
        ]
    )
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    response = client.chat([{"role": "user", "content": "hi"}])
    check("retry_timeout_then_success", response.content == "after timeout", response.content)

    session = ScriptedSession([FakeResponse(200, payload={"choices": []})])
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    expect_raises(
        "retry_malformed_response_permanent",
        LLMPermanentError,
        client.chat,
        [{"role": "user", "content": "hi"}],
    )

    # 工具参数不是合法 JSON 时不能崩，要标记出来交给循环处置
    bad_args = {
        "model": "fake-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{not json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    session = ScriptedSession([FakeResponse(200, bad_args)])
    client = ChatClient(_fast_config(), session=session)  # type: ignore[arg-type]
    response = client.chat([{"role": "user", "content": "hi"}])
    check("tool_arg_parse_error_is_flagged", response.tool_calls[0].parse_error is not None)
    check("tool_arg_parse_error_keeps_raw", response.tool_calls[0].arguments_raw == "{not json")


# ---------------------------------------------------------------------------
# C. 遥测
# ---------------------------------------------------------------------------


def test_telemetry() -> None:
    telemetry = Telemetry(CWD / "telemetry", "run-t")
    telemetry.record_call(
        span="writer",
        role="writer",
        model="m1",
        prompt_tokens=1000,
        completion_tokens=500,
        latency_ms=200,
        attempts=1,
        price_per_1k_prompt=0.001,
        price_per_1k_completion=0.002,
    )
    telemetry.record_call(
        span="writer",
        role="analyst",
        model="m2",
        prompt_tokens=2000,
        completion_tokens=1000,
        latency_ms=400,
        attempts=2,
        ok=False,
        error="boom",
    )
    summary = telemetry.summary()
    check("telemetry_call_count", summary["calls"] == 2, str(summary["calls"]))
    check("telemetry_total_tokens", summary["total_tokens"] == 4500, str(summary["total_tokens"]))
    check("telemetry_success_rate", abs(summary["success_rate"] - 0.5) < 1e-9, str(summary["success_rate"]))
    check("telemetry_cost_computed", abs(summary["cost_usd_total"] - 0.002) < 1e-12, str(summary["cost_usd_total"]))
    check("telemetry_cost_basis_mixed", summary["cost_basis"] == "unpriced", summary["cost_basis"])
    check("telemetry_retries_total", summary["retries_total"] == 1, str(summary["retries_total"]))
    check("telemetry_by_role_split", set(summary["by_role"]) == {"writer", "analyst"}, str(list(summary["by_role"])))
    check("telemetry_jsonl_written", telemetry.calls_path.is_file() and len(telemetry.calls_path.read_text(encoding="utf-8").strip().splitlines()) == 2)
    check("telemetry_summary_written", telemetry.write_summary().is_file())


# ---------------------------------------------------------------------------
# D. 循环上界
# ---------------------------------------------------------------------------


class FakeClient:
    """按脚本返回响应的假 LLM 客户端。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.config = _fast_config()
        self.seen_tool_names: list[str] = []

    def chat(self, messages, **kwargs) -> ChatResponse:  # noqa: ANN001, ANN003
        schemas = kwargs.get("tools") or []
        self.seen_tool_names = [item["function"]["name"] for item in schemas]
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _resp(content: str = "", tool_calls: list[ToolCall] | None = None, tokens: tuple[int, int] = (10, 5)) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        latency_ms=1,
        attempts=1,
        finish_reason="tool_calls" if tool_calls else "stop",
        model="fake-model",
    )


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name=name,
        arguments_raw=json.dumps(args, ensure_ascii=False),
        arguments=args,
    )


def _echo_tool() -> ToolSpec:
    return ToolSpec(
        name="echo",
        description="回显参数",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=lambda args: {"echo": args.get("x", "")},
    )


def test_loop_bounds() -> None:
    # 正常收敛
    client = FakeClient([_resp(tool_calls=[_call("echo", {"x": "1"})]), _resp(content="done")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop1", "r1"),
        run_id="r1",
        max_steps=5,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_final_answer", result.stop_reason == "final_answer" and result.ok, result.stop_reason)
    check("loop_steps_counted", result.steps == 2, str(result.steps))

    # max_steps 用尽
    client = FakeClient([_resp(tool_calls=[_call(f"echo", {"x": str(i)}, call_id=f"c{i}")]) for i in range(4)])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop2", "r2"),
        run_id="r2",
        max_steps=2,
        repeat_limit=99,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_max_steps_bound", result.stop_reason == "max_steps", result.stop_reason)
    check("loop_max_steps_count", result.steps == 2, str(result.steps))

    # 死循环检测
    same = _call("echo", {"x": "same"})
    client = FakeClient([_resp(tool_calls=[_call("echo", {"x": "same"}, call_id=f"c{i}")]) for i in range(5)])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop3", "r3"),
        run_id="r3",
        max_steps=10,
        repeat_limit=3,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_detected", result.stop_reason == "loop_detected", result.stop_reason)
    check("loop_detected_at_third_repeat", result.steps == 3, str(result.steps))
    del same

    # 墙钟上限
    client = FakeClient([_resp(tool_calls=[_call("echo", {"x": "1"})]) for _ in range(3)])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop4", "r4"),
        run_id="r4",
        max_steps=5,
        wall_clock_seconds=1e-9,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_wall_clock_bound", result.stop_reason == "wall_clock", result.stop_reason)

    # token 预算
    client = FakeClient(
        [_resp(tool_calls=[_call("echo", {"x": str(i)}, call_id=f"c{i}")], tokens=(10, 5)) for i in range(4)]
    )
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop5", "r5"),
        run_id="r5",
        max_steps=5,
        token_budget=15,
        repeat_limit=99,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_token_budget_bound", result.stop_reason == "token_budget", result.stop_reason)
    check("loop_token_budget_stops_at_two", result.steps == 2, str(result.steps))

    # LLM 错误不崩，落成 stop_reason
    client = FakeClient([LLMTransientError("net down")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop6", "r6"),
        run_id="r6",
        max_steps=3,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_llm_error_handled", result.stop_reason == "llm_error", result.stop_reason)
    check("loop_llm_error_not_ok", not result.ok)

    # max_steps 越界拒绝
    expect_raises(
        "loop_rejects_unbounded_max_steps",
        ValueError,
        AgentLoop,
        client=FakeClient([]),  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop7", "r7"),
        run_id="r7",
        max_steps=MAX_STEPS_HARD_CAP + 1,
    )
    expect_raises(
        "loop_rejects_zero_max_steps",
        ValueError,
        AgentLoop,
        client=FakeClient([]),  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop8", "r8"),
        run_id="r8",
        max_steps=0,
    )
    expect_raises(
        "loop_rejects_repeat_limit_one",
        ValueError,
        AgentLoop,
        client=FakeClient([]),  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop9", "r9"),
        run_id="r9",
        repeat_limit=1,
    )


def test_loop_tool_dispatch() -> None:
    # 危险工具在缺少确认回调时必须被拒绝（fail-closed）
    dangerous = ToolSpec(
        name="rm_all",
        description="危险操作",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args: {"done": True},
        dangerous=True,
    )
    client = FakeClient([_resp(tool_calls=[_call("rm_all", {})]), _resp(content="done")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[dangerous],
        telemetry=Telemetry(CWD / "loop10", "r10"),
        run_id="r10",
        max_steps=4,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_dangerous_fail_closed", result.tool_trace[0].ok is False)
    check("loop_dangerous_not_confirmed", result.tool_trace[0].confirmed is False)
    check("loop_dangerous_mentions_rejection", "未获人工确认" in result.tool_trace[0].error)

    # 提供确认回调且返回 True 时执行
    client = FakeClient([_resp(tool_calls=[_call("rm_all", {})]), _resp(content="done")])
    approvals: list[str] = []
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[dangerous],
        telemetry=Telemetry(CWD / "loop11", "r11"),
        run_id="r11",
        max_steps=4,
        confirm=lambda name, args: (approvals.append(name), True)[1],
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_dangerous_confirmed_executes", result.tool_trace[0].ok is True)
    check("loop_dangerous_confirm_called", approvals == ["rm_all"], str(approvals))

    # 工具抛异常 → 结构化观察值回灌，循环继续
    def boom(_args: dict[str, Any]) -> Any:
        raise RuntimeError("工具内部炸了")

    failing = ToolSpec(
        name="boom",
        description="会失败的工具",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=boom,
    )
    client = FakeClient([_resp(tool_calls=[_call("boom", {})]), _resp(content="recovered")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[failing],
        telemetry=Telemetry(CWD / "loop12", "r12"),
        run_id="r12",
        max_steps=4,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_tool_exception_captured", result.tool_trace[0].ok is False)
    check("loop_tool_exception_type_in_error", "工具内部炸了" in result.tool_trace[0].error)
    check("loop_survives_tool_exception", result.stop_reason == "final_answer", result.stop_reason)
    check(
        "loop_tool_error_fed_back",
        any(
            message.get("role") == "tool" and "工具内部炸了" in str(message.get("content"))
            for message in result.transcript
        ),
    )

    # 未注册工具
    client = FakeClient([_resp(tool_calls=[_call("nonexistent", {})]), _resp(content="done")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop13", "r13"),
        run_id="r13",
        max_steps=4,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_unknown_tool_rejected", result.tool_trace[0].ok is False)
    check("loop_unknown_tool_lists_available", "echo" in result.tool_trace[0].error)

    # 参数不是合法 JSON
    bad = ToolCall(call_id="c1", name="echo", arguments_raw="{oops", arguments={}, parse_error="不是合法 JSON")
    client = FakeClient([_resp(tool_calls=[bad]), _resp(content="done")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[_echo_tool()],
        telemetry=Telemetry(CWD / "loop14", "r14"),
        run_id="r14",
        max_steps=4,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    check("loop_bad_args_flagged", result.tool_trace[0].ok is False)
    check("loop_bad_args_no_duplicate_execute", result.tool_trace[0].elapsed_ms == 0)

    # 结果截断
    long_tool = ToolSpec(
        name="long",
        description="返回超长结果",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda args: {"blob": "x" * 20000},
    )
    client = FakeClient([_resp(tool_calls=[_call("long", {})]), _resp(content="done")])
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]
        tools=[long_tool],
        telemetry=Telemetry(CWD / "loop15", "r15"),
        run_id="r15",
        max_steps=4,
        max_tool_result_chars=500,
    )
    result = loop.run(task="t", system_prompt="s", span="sp", role="writer")
    fed = [m for m in result.transcript if m.get("role") == "tool"][0]["content"]
    check("loop_long_result_truncated", len(fed) < 900 and "截断" in fed, str(len(fed)))


# ---------------------------------------------------------------------------
# E. 工具与门控
# ---------------------------------------------------------------------------


def _make_ledger() -> tuple[Ledger, Path]:
    root = CWD / f"ledger-{len(list(CWD.glob('ledger-*')))}"
    exp = root / "exp"
    exp.mkdir(parents=True, exist_ok=True)
    metrics = {
        "results": {
            "mse_ols_mean": 0.5865,
            "mse_ridge_mean": 0.1502,
            "relative_improvement_pct": 74.39,
        }
    }
    (exp / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return Ledger(root / "ledger"), root


def test_tools_and_gate() -> None:
    ledger, root = _make_ledger()
    tools = {spec.name: spec for spec in build_ledger_tools(ledger, tex_dir=root / "paper", run_id="r")}

    registered = tools["register_artifact"].handler(
        {"path": str(root / "exp" / "metrics.json"), "producer": "test"}
    )
    artifact_id = registered["artifact_id"]
    check("tool_register_returns_id", bool(artifact_id))
    check("tool_register_hints_next_step", registered["use_this_artifact_id"] == artifact_id)

    # 用文件名而非 artifact_id —— 应当被解析到同一产物
    claim = tools["add_claim"].handler(
        {
            "text": "岭回归的平均 MSE 低于 OLS",
            "artifact_id": "metrics.json",
            "locator": "/results/mse_ridge_mean",
        }
    )
    check("tool_claim_via_filename_resolves", claim["value"] == 0.1502, str(claim["value"]))
    check("tool_claim_returns_macro", claim["latex_macro"].startswith("\\result{"), claim["latex_macro"])
    claim_id = claim["claim_id"]

    # 声明值与产物不符必须失败
    expect_raises(
        "tool_claim_value_mismatch_rejected",
        LedgerError,
        tools["add_claim"].handler,
        {
            "text": "伪造一个漂亮结果",
            "artifact_id": artifact_id,
            "locator": "/results/mse_ridge_mean",
            "value": 0.0001,
        },
    )

    # locator 指向不存在的字段
    expect_raises(
        "tool_claim_bad_locator_rejected",
        LedgerError,
        tools["add_claim"].handler,
        {"text": "t", "artifact_id": artifact_id, "locator": "/results/nope"},
    )

    # 无法解析的产物引用
    err = expect_raises(
        "tool_claim_unknown_artifact_rejected",
        LedgerError,
        tools["add_claim"].handler,
        {"text": "t", "artifact_id": "totally-unknown", "locator": "/results/mse_ols_mean"},
    )
    if err is not None:
        check("tool_claim_unknown_lists_known_ids", artifact_id in str(err), str(err))

    # 批量登记：一次调用登记多条，并逐条报告失败
    batch = tools["add_claims"].handler(
        {
            "artifact_id": artifact_id,
            "claims": [
                {"text": "OLS 平均 MSE", "locator": "/results/mse_ols_mean"},
                {"text": "岭回归相对改进", "locator": "/results/relative_improvement_pct"},
                {"text": "不存在的字段", "locator": "/results/nope"},
            ],
        }
    )
    check("batch_registers_good_items", batch["registered_count"] == 2, str(batch["registered_count"]))
    check("batch_reports_bad_items", batch["failed_count"] == 1, str(batch["failed_count"]))
    check("batch_returns_macros", all("latex_macro" in item for item in batch["registered"]))

    # 幂等：同一 artifact + locator + text 重复登记返回同一条，不新增
    again = tools["add_claims"].handler(
        {
            "artifact_id": artifact_id,
            "claims": [{"text": "OLS 平均 MSE", "locator": "/results/mse_ols_mean"}],
        }
    )
    first_id = [item for item in batch["registered"] if item["locator"] == "/results/mse_ols_mean"][0][
        "claim_id"
    ]
    check("batch_idempotent_reuses_claim", again["registered"][0]["claim_id"] == first_id, str(again))

    # 缺少必填参数时给出可执行的报错，而不是 KeyError
    err = expect_raises(
        "batch_requires_claims",
        LedgerError,
        tools["add_claims"].handler,
        {"artifact_id": artifact_id},
    )
    if err is not None:
        check("batch_missing_param_named", "claims" in str(err), str(err))

    check("tool_count_full_profile", len(tools) == 7, str(sorted(tools)))
    compact_tools = build_ledger_tools(ledger, tex_dir=root / "paper-compact", run_id="r", compact=True)
    check(
        "tool_compact_profile_is_four",
        sorted(spec.name for spec in compact_tools)
        == ["add_claims", "list_claims", "register_artifact", "write_latex_section"],
        str(sorted(spec.name for spec in compact_tools)),
    )

    # 正文没有任何 \result 引用 → 拒绝
    expect_raises(
        "gate_rejects_zero_refs",
        GateRejected,
        tools["write_latex_section"].handler,
        {"relative_path": "sections/results.tex", "content": "We present a study.\\n"},
    )

    # 手写数字（结果语境）→ 拒绝
    expect_raises(
        "gate_rejects_hardcoded_number",
        GateRejected,
        tools["write_latex_section"].handler,
        {
            "relative_path": "sections/results.tex",
            "content": "Our method achieves 0.1502 MSE, improving over the baseline.\\n",
        },
    )

    # 引用不存在的 claim → 拒绝
    expect_raises(
        "gate_rejects_unknown_claim",
        GateRejected,
        tools["write_latex_section"].handler,
        {"relative_path": "sections/results.tex", "content": f"Result is \\result{{c-notexist}}.\\n"},
    )

    # 正文重新定义宏 → 拒绝
    expect_raises(
        "gate_rejects_macro_redefinition",
        GateRejected,
        tools["write_latex_section"].handler,
        {
            "relative_path": "sections/results.tex",
            "content": f"\\newcommand{{\\result}}[1]{{0.99}}\nValue \\result{{{claim_id}}}.\\n",
        },
    )

    # 越权路径 → 拒绝
    expect_raises(
        "tool_rejects_path_escape",
        LedgerError,
        tools["write_latex_section"].handler,
        {"relative_path": "../../escape.tex", "content": f"\\result{{{claim_id}}}\\n"},
    )

    # 合规正文 → 写入成功
    written = tools["write_latex_section"].handler(
        {
            "relative_path": "sections/results.tex",
            "content": (
                "Ridge regression reduces the mean squared error to "
                f"\\result{{{claim_id}}}, confirming the regularization gain.\\n"
            ),
        }
    )
    check("gate_accepts_valid_ref", written["written"] is True, str(written))
    check("gate_reports_resolved_count", written["resolved"] == 1, str(written["resolved"]))
    target = Path(written["path"])
    check("gate_wrote_file", target.is_file() and target.name == "results.tex")
    check(
        "gate_write_path_not_doubled",
        "paper/sections/results.tex" in target.as_posix(),
        target.as_posix(),
    )

    # 产物被篡改后，台账校验必须失败
    (root / "exp" / "metrics.json").write_text(
        json.dumps({"results": {"mse_ols_mean": 0.5865, "mse_ridge_mean": 0.0001, "relative_improvement_pct": 74.39}}),
        encoding="utf-8",
    )
    ledger.reload()
    report = ledger.verify(check_artifacts=True)
    check("tampered_artifact_detected", report.failed >= 1, str(report.to_dict()))

    # 篡改后再次写正文也必须被拒
    expect_raises(
        "gate_rejects_after_tamper",
        GateRejected,
        tools["write_latex_section"].handler,
        {"relative_path": "sections/results_2.tex", "content": f"Result \\result{{{claim_id}}}.\\n"},
    )


def test_section_heading_structure() -> None:
    """一级标题必须由组装器独占写入，且每章恰好一个。

    回归用例，来自一次真实产出：写手被要求在正文里**不要写** ``\\section``，
    组装器被假定会统一补标题（连 docstring 都这么写）。但 ``resolve_sections``
    拿到 ``(stem, title)`` 后把 ``title`` 丢掉了 —— 两边都没写标题。结果是编译
    出的 PDF 六个章节只有两个带标题，而且都是 ``\\subsection`` 级：``\\subsection``
    在缺少父级 ``\\section`` 时会被 hyperref 提升为书签一级条目，只看书签很难发现。

    同一个 run 里 ``introduction.tex`` 还把其它章节的内容搬了进来（子标题叫
    Motivation and Background / Problem Formulation / Experimental Setup /
    Baseline and Noise Floor / Conclusion），所以越界检测一并钉在这里。
    """
    ledger, root = _make_ledger()
    artifact = ledger.register_artifact(root / "exp" / "metrics.json", producer="test")
    claim = ledger.add_claim(
        text="Relative improvement of ridge over OLS.",
        artifact_id=artifact.artifact_id,
        locator="/results/relative_improvement_pct",
        claim_id="c-imp",
        run_id="r",
    )

    src = root / "drafts"
    src.mkdir(parents=True, exist_ok=True)
    # 复刻真实故障样本：自带 \subsection（还撞了别章名字），没有 \section。
    (src / "introduction.tex").write_text(
        "\\subsection{Experimental Setup}\n"
        f"We observe a relative improvement to \\result{{c-imp}} percent.\n",
        encoding="utf-8",
    )

    resolved = resolve_sections(
        src, root / "paper" / "sections", ledger, sections=(("introduction", "Introduction"),)
    )
    out = Path(resolved[0].dst).read_text(encoding="utf-8")

    check("heading_injected", "\\section{Introduction}" in out, out[:200])
    check("heading_count_is_one", len(SECTION_RE.findall(out)) == 1, out[:200])
    check("heading_is_not_subsection", "\\subsection{Introduction}" not in out, out[:200])
    check("number_still_resolved", claim.display in out, out[:200])
    check(
        "heading_precedes_body",
        out.index("\\section{Introduction}")
        < out.index("\\subsection{Experimental Setup}"),
    )

    # 模型自作主张写了 \section：必须被剥离，而不是叠加成两个标题。
    (src / "method.tex").write_text(
        "\\section{Method}\n\nBody text with no numbers at all.\n", encoding="utf-8"
    )
    resolved2 = resolve_sections(
        src, root / "paper" / "sections", ledger, sections=(("method", "Method"),)
    )
    out2 = Path(resolved2[0].dst).read_text(encoding="utf-8")
    check("model_section_stripped", len(SECTION_RE.findall(out2)) == 1, out2[:200])
    check(
        "strip_recorded_as_warning",
        any("剥离" in item for item in resolved2[0].warnings),
        str(resolved2[0].warnings),
    )

    # 结构断言本身必须会拒绝 —— 一个从不拒绝的断言和没有断言等价。
    bad = root / "paper" / "sections" / "bad.tex"
    bad.write_text("% 只有注释\nBody.\n", encoding="utf-8")
    expect_raises(
        "assert_rejects_missing_heading", AssemblyError, assert_single_section, bad, "Bad"
    )
    bad.write_text("\\section{Wrong}\nBody.\n", encoding="utf-8")
    expect_raises(
        "assert_rejects_wrong_title", AssemblyError, assert_single_section, bad, "Bad"
    )
    bad.write_text("\\section{Bad}\n\\section{Again}\nBody.\n", encoding="utf-8")
    expect_raises(
        "assert_rejects_double_heading", AssemblyError, assert_single_section, bad, "Bad"
    )
    bad.write_text("\\section{Bad}\nBody.\n", encoding="utf-8")
    check(
        "assert_accepts_correct",
        isinstance(assert_single_section(bad, "Bad"), type(None)),
    )

    # 写手越界检测
    check(
        "prose_flags_foreign_subsection",
        len(check_section_prose("\\subsection{Conclusion}\nSome text.", "Introduction")) == 1,
        str(check_section_prose("\\subsection{Conclusion}\nSome text.", "Introduction")),
    )
    check(
        "prose_flags_own_section_header",
        bool(check_section_prose("\\section{Introduction}\nText here.", "Introduction")),
    )
    check(
        "prose_flags_empty_body",
        bool(check_section_prose("   \n", "Introduction")),
    )
    check(
        "prose_accepts_clean_text",
        check_section_prose("Two paragraphs of plain prose.", "Introduction") == [],
    )
    check(
        "prose_accepts_harmless_subsection",
        check_section_prose("\\subsection{Design Goals}\nProse.", "Method") == [],
    )


def main() -> int:
    """跑完全部离线测试。"""
    print(f"工作目录: {CWD}\n")
    test_config()
    test_retry_semantics()
    test_telemetry()
    test_loop_bounds()
    test_loop_tool_dispatch()
    test_tools_and_gate()
    test_section_heading_structure()
    print(f"\n{_passed}/{_total} passed")
    if _failures:
        print("失败用例: " + ", ".join(_failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
