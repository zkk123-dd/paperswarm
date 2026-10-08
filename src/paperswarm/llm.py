# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""LLM 客户端层：OpenAI 兼容协议 + 显式超时 + 有界重试 + 可插拔后端。

为什么自己写而不直接用某个 SDK
------------------------------
1. 本项目要接的后端至少有三类：本机 Ollama、华为云 MaaS、任意 OpenAI 兼容云服务。
   它们都提供 ``/v1/chat/completions``，直接说协议比绑死某家 SDK 更稳。
2. JiuwenSwarm 侧另有自己的 model 配置；本模块只负责**竖切片与离线评测**，
   保持零框架依赖，从而能被单测直接驱动。
3. 重试语义必须自己掌握：哪些状态码可重试、退避与抖动参数、超时数值，
   都要在代码里显式可查（硬约束 C4）。

安全约定
--------
- ``api_key`` 只从环境变量或配置文件读取，**永不写进日志**；
  :meth:`LLMConfig.redacted` 是唯一用于展示的形态。
- 每次请求都有连接超时与读取超时；无超时的外部调用在本项目里视为缺陷。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import requests
import yaml

# 可重试的 HTTP 状态：限流、冲突、超时与 5xx。4xx 中其余状态属于请求本身有问题，
# 重试只会浪费配额，因此直接失败。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

ENV_BASE_URL = "PAPER_LLM_BASE_URL"
ENV_API_KEY = "PAPER_LLM_API_KEY"
ENV_MODEL = "PAPER_LLM_MODEL"


class LLMError(RuntimeError):
    """LLM 调用失败的基类。"""


class LLMConfigError(LLMError):
    """配置缺失或非法（例如没有配置 base_url）。"""


class LLMTransientError(LLMError):
    """可重试错误耗尽重试次数后抛出。"""


class LLMPermanentError(LLMError):
    """不可重试错误（4xx 参数错误、响应结构非法等）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """一个 OpenAI 兼容后端的连接配置。

    Args:
        base_url: 形如 ``http://127.0.0.1:11434/v1`` 的根地址（含 ``/v1``）。
        model: 模型名，必须与后端实际可用名称一致。
        api_key: 密钥。本机 Ollama 不校验，可用占位符。
        connect_timeout: TCP 连接超时（秒）。
        read_timeout: 读取响应超时（秒）。长文本生成需要放宽，但不能不设。
        max_retries: 额外重试次数（总尝试次数 = max_retries + 1）。
        backoff_base: 指数退避基数（秒）。
        backoff_cap: 单次退避上限（秒）。
        temperature: 默认采样温度。
        max_tokens: 默认最大生成 token 数。
    """

    base_url: str
    model: str
    api_key: str = "not-needed"
    connect_timeout: float = 10.0
    read_timeout: float = 180.0
    max_retries: int = 3
    backoff_base: float = 1.5
    backoff_cap: float = 20.0
    temperature: float = 0.2
    max_tokens: int = 2048
    price_per_1k_prompt: float | None = None
    price_per_1k_completion: float | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise LLMConfigError("base_url 不能为空")
        if not self.model:
            raise LLMConfigError("model 不能为空")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise LLMConfigError("超时必须是正数；无超时的外部调用在本项目中不允许")
        if self.max_retries < 0:
            raise LLMConfigError("max_retries 不能为负")

    @property
    def endpoint(self) -> str:
        """拼接 chat completions 端点。"""
        return self.base_url.rstrip("/") + "/chat/completions"

    def redacted(self) -> dict[str, Any]:
        """用于日志/报告的脱敏形态：密钥只留前 4 位。"""
        shown = "***" if not self.api_key else f"{self.api_key[:4]}***"
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": shown,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_env(cls, *, model: str | None = None, **overrides: Any) -> "LLMConfig":
        """从环境变量构造配置。

        读取 ``PAPER_LLM_BASE_URL`` / ``PAPER_LLM_API_KEY`` / ``PAPER_LLM_MODEL``，
        显式传入的 ``model`` 与 ``overrides`` 优先级更高。
        """
        base_url = os.environ.get(ENV_BASE_URL, "")
        if not base_url:
            raise LLMConfigError(
                f"环境变量 {ENV_BASE_URL} 未设置；"
                "本机 Ollama 可设为 http://127.0.0.1:11434/v1"
            )
        resolved_model = model or os.environ.get(ENV_MODEL, "")
        if not resolved_model:
            raise LLMConfigError(
                f"模型名缺失：请设置 {ENV_MODEL} 或显式传入 model 参数"
            )
        params: dict[str, Any] = {
            "base_url": base_url,
            "model": resolved_model,
            "api_key": os.environ.get(ENV_API_KEY) or "not-needed",
        }
        params.update(overrides)
        return cls(**params)

    @classmethod
    def from_yaml(cls, path: Path | str, profile: str = "default") -> "LLMConfig":
        """从 YAML 配置文件的某个 profile 构造配置。

        文件形如::

            default:
              base_url: http://127.0.0.1:11434/v1
              model: qwen2.5:7b-instruct-q4_K_M
              api_key_env: PAPER_LLM_API_KEY
        """
        config_path = Path(path)
        if not config_path.is_file():
            raise LLMConfigError(f"配置文件不存在: {config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping):
            raise LLMConfigError(f"{config_path} 顶层必须是映射（profile 名 -> 配置）")
        section = raw.get(profile)
        if section is None:
            raise LLMConfigError(
                f"配置文件里没有 profile {profile!r}；现有 profile: {sorted(raw.keys())}"
            )
        if not isinstance(section, Mapping):
            raise LLMConfigError(f"profile {profile!r} 必须是映射")
        params = dict(section)
        # 密钥通过 *_env 间接引用，避免把明文密钥写进版本库。
        key_env = params.pop("api_key_env", None)
        if key_env:
            params["api_key"] = os.environ.get(str(key_env)) or "not-needed"
        return cls(**params)


# ---------------------------------------------------------------------------
# 消息与响应
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """模型发起的一次工具调用。

    Attributes:
        call_id: 后端返回的调用标识，回填 ``tool`` 消息时必须原样带回。
        name: 工具名。
        arguments_raw: 模型给出的原始 JSON 字符串。
        arguments: 解析后的参数字典；解析失败时为空字典。
        parse_error: 参数不是合法 JSON 时的错误说明（由调用方决定如何处置）。
    """

    call_id: str
    name: str
    arguments_raw: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str | None = None


@dataclass
class ChatResponse:
    """一次 ``chat`` 调用的完整结果，含用量与耗时，便于台账落盘。"""

    content: str
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    attempts: int
    finish_reason: str
    model: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _parse_tool_calls(raw_calls: Sequence[Mapping[str, Any]] | None) -> list[ToolCall]:
    """把后端返回的 tool_calls 数组转成 :class:`ToolCall` 列表。"""
    parsed: list[ToolCall] = []
    for index, item in enumerate(raw_calls or []):
        function = item.get("function") or {}
        name = str(function.get("name") or "")
        arguments_raw = function.get("arguments")
        if not isinstance(arguments_raw, str):
            arguments_raw = json.dumps(arguments_raw or {}, ensure_ascii=False)
        call = ToolCall(
            call_id=str(item.get("id") or f"call_{index}"),
            name=name,
            arguments_raw=arguments_raw,
        )
        try:
            loaded = json.loads(arguments_raw) if arguments_raw.strip() else {}
        except json.JSONDecodeError as exc:
            call.parse_error = f"工具参数不是合法 JSON: {exc}"
        else:
            if isinstance(loaded, Mapping):
                call.arguments = dict(loaded)
            else:
                call.parse_error = f"工具参数必须是 JSON 对象，收到 {type(loaded).__name__}"
        parsed.append(call)
    return parsed


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class ChatClient:
    """OpenAI 兼容的对话客户端。

    Args:
        config: 连接配置。
        session: 可注入的 ``requests.Session``（便于测试替换）。
    """

    def __init__(self, config: LLMConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    # -- 内部 ---------------------------------------------------------------

    def _sleep_before_retry(self, attempt: int, reason: str) -> float:
        """计算并执行退避等待，返回实际等待秒数。

        指数退避 + 全抖动（full jitter）：等待时间在 ``[0, cap]`` 内均匀取值，
        避免多 worker 同时重试造成同步冲击。
        """
        ceiling = min(self.config.backoff_cap, self.config.backoff_base * (2 ** attempt))
        delay = random.uniform(0.0, ceiling)
        _ = reason  # 等待原因由调用方记入遥测，这里不重复使用
        time.sleep(delay)
        return delay

    # -- 公开 API -----------------------------------------------------------

    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | Mapping[str, Any] | None = "auto",
        temperature: float | None = None,
        max_tokens: int | None = None,
        retry_log: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """发起一次对话补全，带显式超时与有界重试。

        Args:
            messages: OpenAI 格式的消息列表。
            tools: 工具 JSON Schema 列表；``None`` 表示不启用工具。
            tool_choice: 工具选择策略，默认 ``auto``。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认最大生成 token 数。
            retry_log: 若传入，每次重试都会追加一条记录（供遥测落盘）。

        Returns:
            :class:`ChatResponse`。

        Raises:
            LLMTransientError: 可重试错误在重试耗尽后仍未成功。
            LLMPermanentError: 不可重试错误或响应结构非法。
        """
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": (
                self.config.temperature if temperature is None else float(temperature)
            ),
            "max_tokens": self.config.max_tokens if max_tokens is None else int(max_tokens),
        }
        if tools:
            payload["tools"] = list(tools)
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
        }
        timeout = (self.config.connect_timeout, self.config.read_timeout)

        last_error: str = ""
        attempts = 0
        for attempt in range(self.config.max_retries + 1):
            attempts = attempt + 1
            started = time.perf_counter()
            try:
                response = self.session.post(
                    self.config.endpoint,
                    headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=timeout,
                )
            except requests.Timeout as exc:
                last_error = f"请求超时（connect={self.config.connect_timeout}s, read={self.config.read_timeout}s）"
                if retry_log is not None:
                    retry_log.append(
                        {"attempt": attempts, "kind": "timeout", "detail": str(exc)}
                    )
                if attempt < self.config.max_retries:
                    self._sleep_before_retry(attempt, "timeout")
                    continue
                raise LLMTransientError(last_error) from exc
            except requests.RequestException as exc:
                last_error = f"网络错误: {exc}"
                if retry_log is not None:
                    retry_log.append(
                        {"attempt": attempts, "kind": "network", "detail": str(exc)}
                    )
                if attempt < self.config.max_retries:
                    self._sleep_before_retry(attempt, "network")
                    continue
                raise LLMTransientError(last_error) from exc

            latency_ms = int((time.perf_counter() - started) * 1000)

            if response.status_code in RETRYABLE_STATUS:
                body = response.text[:300]
                last_error = f"HTTP {response.status_code}: {body}"
                if retry_log is not None:
                    retry_log.append(
                        {"attempt": attempts, "kind": "http", "status": response.status_code}
                    )
                if attempt < self.config.max_retries:
                    self._sleep_before_retry(attempt, f"http {response.status_code}")
                    continue
                raise LLMTransientError(
                    f"可重试错误在 {attempts} 次尝试后仍未成功；最后错误：{last_error}"
                )

            if response.status_code >= 400:
                raise LLMPermanentError(
                    f"HTTP {response.status_code}（不可重试）: {response.text[:500]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise LLMPermanentError(
                    f"响应不是合法 JSON: {response.text[:300]}"
                ) from exc
            if not isinstance(data, Mapping) or not data.get("choices"):
                raise LLMPermanentError(f"响应缺少 choices 字段: {str(data)[:300]}")

            choice = data["choices"][0]
            message = choice.get("message") or {}
            usage = data.get("usage") or {}
            return ChatResponse(
                content=str(message.get("content") or ""),
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                latency_ms=latency_ms,
                attempts=attempts,
                finish_reason=str(choice.get("finish_reason") or ""),
                model=str(data.get("model") or self.config.model),
            )

        # 正常情况下不会走到这里：循环内的每个分支都会 return 或 raise。
        raise LLMTransientError(f"重试循环异常退出，最后错误：{last_error}")

    def probe(self) -> dict[str, Any]:
        """探测后端可用性：一次极小的补全，返回用量与耗时。

        用于环境自检（Phase 2 的 V1 验收）。失败时抛 :class:`LLMError` 子类，
        不做静默降级。
        """
        response = self.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=8,
            temperature=0.0,
        )
        return {
            "model": response.model,
            "content": response.content.strip(),
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "latency_ms": response.latency_ms,
            "attempts": response.attempts,
        }
