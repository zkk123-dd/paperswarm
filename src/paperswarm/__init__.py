# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""PaperSwarm 内核：证据台账 + LaTeX 门控 + 有界 Agent 循环 + 资源遥测。

本包刻意不依赖任何 Agent 框架（openjiuwen / JiuwenSwarm），原因有三条：

1. 证据校验的正确性不该被框架版本演进绑架，它是可以直接单测的纯逻辑；
2. 框架侧只负责"在合适的时机调用它"（工具、PreToolUse 钩子），
   调用契约比实现逻辑更容易随上游变化；
3. 竖切片可以在没有框架、没有 key 的情况下先跑通并被验证。

对外入口：
- :mod:`paperswarm.evidence` — 实验产物与数值 claim 的登记与复核；
- :mod:`paperswarm.tex_gate` — ``\\result{claim_id}`` 的校验与回填；
- :mod:`paperswarm.llm` — OpenAI 兼容客户端（显式超时 + 有界重试）；
- :mod:`paperswarm.telemetry` — Token / 时延 / 成本的追加式台账；
- :mod:`paperswarm.agentloop` — 带四重上界的工具调用循环；
- :mod:`paperswarm.tools` — 把证据台账包装成可被模型调用的工具。
"""

from __future__ import annotations

from paperswarm import agentloop, evidence, llm, telemetry, tex_gate, tools

__all__ = [
    "agentloop",
    "evidence",
    "llm",
    "telemetry",
    "tex_gate",
    "tools",
    "__version__",
]

__version__ = "0.2.0"
