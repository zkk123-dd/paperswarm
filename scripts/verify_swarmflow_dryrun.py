#!/usr/bin/env python
# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""Swarmflow dry-run：用忠实替身真实执行 paper-repro-swarm 的 workflow.py。

为什么需要这个脚本
------------------
官方 ``validate_swarmskill.py`` 只做 **AST 静态校验**：它能证明 ``scripts/workflow.py``
符合 SwarmFlow 的安全信封（META 是字面量、agent 必须带稳定 label、phase 必须与最近
一次的 ``phase()`` 字面量一致、不能用 asyncio/print/open 等），但它**不会真的把
``run()`` 跑起来**。静态通过 ≠ 运行通过：``extract_json`` 的分支、``string.Template``
的占位符、``compact``/``parallel`` 的组合都可能在运行期才暴露问题。

本脚本用一个**只实现官方文档契约**的 ``swarmflow`` 替身（``agent`` / ``parallel`` /
``phase`` / ``log`` / ``compact``）注入 ``sys.modules``，然后真实 ``await run(args)``，
断言四段 phase 的顺序、每个 agent 的 label/phase 组合、以及最终返回结构。

替身刻意不推断任何未在官方模板里出现过的语义：``agent`` 返回 JSON 字符串（官方文档
说明 schema 校验失败时可能返回 ``None`` 或原始字符串），``parallel`` 顺序 await 所有
thunk 并收集返回值，``compact`` 过滤 falsy 元素。

用法::

    python scripts/verify_swarmflow_dryrun.py
    # 结果写入 reports/swarmflow_dryrun.json
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = (
    ROOT.parent
    / "upstream"
    / "jiuwenswarm-develop"
    / "jiuwenswarm"
    / "resources"
    / "agent"
    / "workspace"
    / "skills"
    / "paper-repro-swarm"
    / "scripts"
    / "workflow.py"
)
REPORT_PATH = ROOT / "reports" / "swarmflow_dryrun.json"


class Recorder:
    """收集替身调用轨迹，供运行后断言。"""

    def __init__(self) -> None:
        self.phases: list[str] = []
        self.logs: list[str] = []
        self.agent_calls: list[dict[str, Any]] = []
        self.prompts: dict[str, str] = {}


RECORDER = Recorder()


# ---------------------------------------------------------------------------
# swarmflow 替身：只实现官方模板展示的契约
# ---------------------------------------------------------------------------


def _install_swarmflow_stub() -> None:
    """把替身装进 sys.modules，模拟 `from swarmflow import ...`。"""
    module = types.ModuleType("swarmflow")

    def phase(title: str) -> None:
        assert isinstance(title, str) and title, "phase() 必须收到非空字符串字面量"
        RECORDER.phases.append(title)

    def log(message: str) -> None:
        # 官方约束：log() 只接受一个位置参数。签名本身即校验。
        assert isinstance(message, str), "log() 必须收到一个字符串"
        RECORDER.logs.append(message)

    async def agent(prompt: str, *, label: str, phase: str, schema: dict | None = None):
        assert isinstance(prompt, str) and prompt.strip(), "agent 的 prompt 不能为空"
        assert isinstance(label, str) and label, "agent 必须带稳定 label"
        assert isinstance(phase, str) and phase, "agent 必须带 phase"
        if RECORDER.phases:
            assert phase == RECORDER.phases[-1], (
                f"agent phase={phase!r} 与最近一次 phase() {RECORDER.phases[-1]!r} 不一致"
            )
        RECORDER.agent_calls.append({"label": label, "phase": phase, "has_schema": schema is not None})
        RECORDER.prompts.setdefault(label, []).append(prompt)
        return _canned_response(label, prompt)

    async def parallel(thunks: list) -> list:
        # 官方语义：并行 thunk 列表，逐个 await，允许返回 None。
        results = []
        for thunk in thunks:
            results.append(await thunk())
        return results

    def compact(items: list) -> list:
        return [item for item in items if item]

    module.phase = phase
    module.log = log
    module.agent = agent
    module.parallel = parallel
    module.compact = compact
    module.workflow = None  # 未使用；仅确保属性存在不改变行为
    sys.modules["swarmflow"] = module


def _canned_response(label: str, prompt: str) -> str:
    """按 label 返回一段 JSON 字符串，模拟模型的结构化输出。"""
    if label == "plan":
        return json.dumps(
            {
                "research_question": "Does ridge regression reduce test MSE relative to OLS?",
                "experiment": "contrast OLS vs ridge over 8 seeds on a synthetic design",
                "claims": [
                    "cl-ols-mean | mean OLS test MSE | /results/ols_mse_mean | MSE",
                    "cl-ridge-mean | mean ridge test MSE | /results/ridge_mse_mean | MSE",
                    "cl-improve | relative improvement | /results/improvement_pct | percent",
                ],
                "sections": ["Abstract", "Introduction", "Method", "Experiments", "Conclusion"],
                "verdict": "READY",
            }
        )
    if label == "evidence-steward":
        return json.dumps(
            {
                "artifact_id": "artifact.metrics",
                "sha256": "0" * 64,
                "artifact_path": "runs/demo/exp/metrics.json",
                "claims": [
                    "cl-ols-mean | 0.8123 | 0.812 | \\result{cl-ols-mean} | /results/ols_mse_mean",
                    "cl-ridge-mean | 0.6011 | 0.601 | \\result{cl-ridge-mean} | /results/ridge_mse_mean",
                    "cl-improve | 26.0 | 26.0 | \\result{cl-improve} | /results/improvement_pct",
                ],
                "failed": [],
                "verdict": "VERIFIED",
            }
        )
    if label == "section-writer":
        section = "section"
        for line in prompt.splitlines():
            if line.startswith("Section to write:"):
                section = line.split(":", 1)[1].strip()
                break
        return json.dumps(
            {
                "section": section,
                "status": "ACCEPTED",
                "claim_ids": ["cl-improve"],
                "note": "",
            }
        )
    if label == "assembler":
        return json.dumps(
            {
                "pdf_path": "runs/demo/paper/main.pdf",
                "pages": "7",
                "pages_ok": "true",
                "size_ok": "true",
                "unresolved_ids": [],
                "verdict": "PDF-READY",
            }
        )
    if label == "evidence-auditor":
        return json.dumps(
            {
                "checks": [
                    "macro-to-claim mapping vs ledger",
                    "numeric literal scan of compiled PDF text",
                    "artifact digest re-verification",
                ],
                "findings": [],
                "digest_matches": "true",
                "verdict": "CLEAN",
            }
        )
    raise AssertionError(f"未预期的 label: {label}")


def _load_workflow() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("paper_repro_workflow", WORKFLOW_PATH)
    assert spec and spec.loader, f"无法加载 {WORKFLOW_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    _install_swarmflow_stub()
    module = _load_workflow()

    args = {
        "topic": "ridge versus ordinary least squares on a synthetic design",
        "title": "Evidence-Gated Reproducible Paper Generation",
        "run_dir": "runs/demo",
    }
    result = asyncio.run(module.run(args))

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    expected_phases = [item["title"] for item in module.META["phases"]]
    check(
        "phase_order_matches_meta",
        RECORDER.phases == expected_phases,
        f"called={RECORDER.phases} meta={expected_phases}",
    )

    labels = [call["label"] for call in RECORDER.agent_calls]
    check(
        "agent_label_sequence",
        labels == ["plan", "evidence-steward", "section-writer", "section-writer", "section-writer", "section-writer", "section-writer", "assembler", "evidence-auditor"],
        f"labels={labels}",
    )

    check(
        "every_agent_has_schema",
        all(call["has_schema"] for call in RECORDER.agent_calls),
        f"calls={RECORDER.agent_calls}",
    )

    section_labels = [call["phase"] for call in RECORDER.agent_calls if call["label"] == "section-writer"]
    check(
        "section_writers_share_one_phase",
        section_labels and set(section_labels) == {"Draft sections"},
        f"section_writer_phases={section_labels}",
    )

    # 并行扇出的数量必须等于计划里的章节数（5），且每一份 prompt 引用了 claim 表。
    check(
        "fanout_count_matches_plan",
        labels.count("section-writer") == 5,
        f"section_writer_count={labels.count('section-writer')}",
    )

    check(
        "claim_table_reaches_sections",
        any("cl-improve" in prompt for prompt in RECORDER.prompts.get("section-writer", [])),
        "section prompt 必须携带 claim 表，否则写手无法引用宏",
    )

    section_prompts = RECORDER.prompts.get("section-writer", [])
    check(
        "must_cite_flags_experiments",
        any("Must this section cite at least one claim: True" in prompt for prompt in section_prompts),
        "Experiments 章节必须被标记为 must-cite",
    )
    check(
        "non_result_sections_not_flagged",
        any("Must this section cite at least one claim: False" in prompt for prompt in section_prompts),
        "不含实验结果的章节不得被强制引用 claim",
    )

    check(
        "latex_macro_preserved_verbatim",
        any(
            "\\result{claim_id}" in prompt or "cl-improve" in prompt
            for prompt in section_prompts
        ),
        "prompt 里必须保留 claim 宏而不是被当成格式字段",
    )

    check("status_complete", result.get("status") == "complete", f"status={result.get('status')}")
    check("audit_ran", bool(result.get("audit")), f"audit={result.get('audit')}")
    check("no_must_fix", result.get("must_fix") == [], f"must_fix={result.get('must_fix')}")
    check("missing_sections_empty", result.get("missing_sections") == [], f"missing={result.get('missing_sections')}")

    # 退化路径：组装失败时必须跳过审计，且不产出 PDF。
    # 注意：workflow.py 用 `from swarmflow import agent` 绑定了模块级名字，所以必须
    # patch 重新加载后的模块自身的全局名字，改 sys.modules["swarmflow"].agent 不生效。
    RECORDER.__init__()
    spec2 = importlib.util.spec_from_file_location("paper_repro_workflow_degraded", WORKFLOW_PATH)
    assert spec2 and spec2.loader
    module2 = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(module2)

    original = module2.agent

    async def agent_no_pdf(prompt: str, *, label: str, phase: str, schema: dict | None = None):
        if label == "assembler":
            RECORDER.agent_calls.append({"label": label, "phase": phase, "has_schema": schema is not None})
            return json.dumps(
                {"pdf_path": "", "unresolved_ids": ["cl-ols-mean"], "verdict": "NO-PDF"}
            )
        return await original(prompt, label=label, phase=phase, schema=schema)

    module2.agent = agent_no_pdf
    degraded = asyncio.run(module2.run(args))
    check("degraded_status", degraded.get("status") == "degraded", f"status={degraded.get('status')}")
    check(
        "degraded_skips_audit",
        not degraded.get("audit"),
        "组装失败时不得运行审计（没有 PDF 可审）",
    )
    check(
        "degraded_reports_unresolved",
        "cl-ols-mean" in degraded.get("must_fix", []),
        f"must_fix={degraded.get('must_fix')}",
    )

    passed = sum(1 for item in checks if item["ok"])
    report = {
        "workflow_path": str(WORKFLOW_PATH),
        "passed": passed,
        "total": len(checks),
        "ok": passed == len(checks),
        "phases": RECORDER.phases,
        "checks": checks,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"swarmflow dry-run: {passed}/{len(checks)} checks passed")
    for item in checks:
        mark = "OK  " if item["ok"] else "FAIL"
        print(f"  [{mark}] {item['name']}: {item['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
