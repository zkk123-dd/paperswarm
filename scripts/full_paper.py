#!/usr/bin/env python
# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""端到端论文生成：实验 → 证据台账 → LLM 写各章节 → 组装 → 编译 PDF。

流水线
------
1. **实验**：:mod:`paperswarm.lab` 跑真实对照实验，写出 ``metrics.json``；
2. **证据**：把产物登记为 artifact（固化 SHA-256），把每个要写进论文的数值
   登记为 claim（声明值必须与产物实际值一致，否则登记失败）；
3. **写作**：逐章节调用 LLM，每章输出都过证据门控 —— 正文里出现手写数字
   或引用不存在的 claim，直接拒绝并让模型重写；
4. **组装**：套 ICLR 2026 官方模板，用台账回填全部 ``\\result{}``；
5. **编译**：tectonic（本地 bundle）产出 PDF，校验页数与体积。

硬约束（违反即中止，不产出 PDF）
--------------------------------
- 任何一个章节/摘要的门控不通过 → 不组装；
- 任何一个 ``\\result{}`` 解析不到 claim → 不组装；
- 编译失败 → 明确报错，不做"先生成一个再说"的降级。

用法::

    python scripts/full_paper.py
    python scripts/full_paper.py --max-attempts 4 --skip-llm-check
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from paperswarm.assemble import (  # noqa: E402
    AssemblyError,
    assemble_paper,
    check_section_prose,
)
from paperswarm.envfile import load_env_file  # noqa: E402
from paperswarm.evidence import Ledger, LedgerError  # noqa: E402
from paperswarm.lab import run_experiment  # noqa: E402
from paperswarm.llm import ChatClient, LLMConfig  # noqa: E402
from paperswarm.telemetry import Telemetry  # noqa: E402
from paperswarm.tex_gate import GateReport, gate_text  # noqa: E402

PAPER_TITLE = (
    "PaperSwarm: An Evidence-Gated Multi-Agent System for "
    "Reproducible Paper Generation"
)

#: ``(claim_id, 该数值支撑的论断, 产物内 JSON Pointer)``
CLAIM_SPECS: tuple[tuple[str, str, str], ...] = (
    (
        "cl-ols-mean",
        "Mean test MSE of the minimum-norm OLS solution, averaged over all seeds.",
        "/results/ols_mse_mean",
    ),
    (
        "cl-ols-std",
        "Standard deviation of the OLS test MSE across seeds (shows high variance).",
        "/results/ols_mse_std",
    ),
    (
        "cl-ridge-mean",
        "Mean test MSE of ridge regression at the selected penalty.",
        "/results/ridge_mse_mean",
    ),
    (
        "cl-ridge-std",
        "Standard deviation of the ridge test MSE across seeds (shows low variance).",
        "/results/ridge_mse_std",
    ),
    (
        "cl-improve",
        "Relative reduction in mean test MSE achieved by ridge over OLS, in percent.",
        "/results/improvement_pct",
    ),
    (
        "cl-alpha",
        "Selected ridge penalty (alpha) attaining the lowest mean test MSE.",
        "/results/best_alpha",
    ),
    (
        "cl-noise",
        "Irreducible noise floor (additive noise variance) used as an MSE reference.",
        "/results/noise_floor_mse",
    ),
    (
        "cl-seeds",
        "Number of random seeds each configuration was averaged over.",
        "/results/n_seeds",
    ),
    (
        "cl-features",
        "Number of features in the design matrix.",
        "/results/n_features",
    ),
    (
        "cl-train",
        "Number of training samples per split.",
        "/results/n_train",
    ),
    (
        "cl-test",
        "Number of test samples per split.",
        "/results/n_test",
    ),
)

#: ``(章节 stem, 标题, 是否必须引用数值, 角色名, 目标词数)``
SECTION_SPECS: tuple[tuple[str, str, bool, str, int], ...] = (
    ("introduction", "Introduction", False, "writer.intro", 320),
    ("related_work", "Related Work", False, "writer.related", 240),
    ("method", "Method", False, "writer.method", 380),
    ("experiments", "Experiments", True, "writer.exp", 400),
    ("discussion", "Discussion", False, "writer.discuss", 280),
    ("conclusion", "Conclusion", False, "writer.conclusion", 160),
)

BIB_ENTRIES: tuple[str, ...] = (
    # --- 直接相关的先前工作：证据门控 / 溯源 / 证据图自主研究系统 ---
    # 每一条都按 arXiv 编号核对过标题与作者（见 reports/bib-verification.md）。
    """@misc{xia2026researchloop,
  title        = {{ResearchLoop}: An Evidence-Gated Control Plane for {AI}-Assisted
                  Research},
  author       = {Xia, Yihan and Wang, Taotao},
  year         = {2026},
  eprint       = {2605.28282},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  note         = {Introduces claim ledgers, evidence contracts and a gate-predicate
                  specification as durable project state}
}""",
    """@misc{ren2026evigraph,
  title        = {{EviGraph}: Evidence-Guided Autonomous Research Agents},
  author       = {Ren, Zhenjiang and Li, Ruiji and Zhang, Xujing and Pang, Ziliang
                  and Ren, Shuo and Zhang, Jiajun},
  year         = {2026},
  eprint       = {2608.04738},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  note         = {Uses a typed evidence graph as the operational state and reports
                  a 40.19\\% Claim Support Rate gain over the strongest baseline}
}""",
    """@misc{jha2026paperpilot,
  title        = {{Paper Pilot}: A Human-in-the-Loop Expert System for
                  Evidence-Traceable Scientific Manuscript Generation in Applied
                  Sciences},
  author       = {Jha, Nidhi and Chaudhary, Siddharth and Kulkarni, Ajinkya},
  year         = {2026},
  eprint       = {2608.28596},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  note         = {Eight human approval gates; reports zero fabricated citations
                  under evidence-locked rules vs. up to 25\\% when ungated}
}""",
    """@misc{luo2026xscientist,
  title        = {{XScientist}: A Git-Like Research Protocol for Long-Running
                  Autonomous Scientific Discovery},
  author       = {Luo, Jixiang},
  year         = {2026},
  eprint       = {2607.12301},
  archivePrefix = {arXiv},
  primaryClass = {cs.SE},
  note         = {Claim-to-evidence anchors; explicitly warns that a passing
                  integrity check is not scientific truth}
}""",
    """@misc{nam2026medsci,
  title        = {Deterministic Integrity Gates for {LLM}-Assisted Clinical
                  Manuscript Preparation: An Auditable Biomedical Informatics
                  Architecture},
  author       = {Nam, Yoojin and Jeong, Jinhoon and Kim, Namkug},
  year         = {2026},
  eprint       = {2606.09500},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  note         = {Seeded-defect ablation: deterministic gates detect 27/27 injected
                  defects where a single-prompt LLM reviewer detects 11}
}""",
    """@misc{gaddipati2026mlreplicate,
  title        = {{MLReplicate}: Benchmarking Autonomous Research Systems for
                  Machine Learning Reproducibility},
  author       = {Gaddipati, Sasi Kiran and Muhammed, Diyana and Keya, Farhana
                  and Rabby, Gollam and Auer, S{\\"o}ren},
  year         = {2026},
  eprint       = {2605.16616},
  archivePrefix = {arXiv},
  primaryClass = {cs.LG},
  note         = {45 generated manuscripts; 59\\% of accepted automated reviews
                  contained fabricated or unsupported claims}
}""",
    # --- 本文实验用到的经典统计方法 ---
    """@article{hoerl1970ridge,
  title   = {Ridge Regression: Biased Estimation for Nonorthogonal Problems},
  author  = {Hoerl, Arthur E. and Kennard, Robert W.},
  journal = {Technometrics},
  volume  = {12},
  number  = {1},
  pages   = {55--67},
  year    = {1970}
}""",
)

#: 可引用的文献键（与 :data:`BIB_ENTRIES` 一一对应）。写作时只把这些交给模型，
#: 模型不得引入表外的 key —— 这是"不编造引用"的第一道约束。
CITATION_KEYS: tuple[str, ...] = (
    "xia2026researchloop",
    "ren2026evigraph",
    "jha2026paperpilot",
    "luo2026xscientist",
    "nam2026medsci",
    "gaddipati2026mlreplicate",
    "hoerl1970ridge",
)

SYSTEM_PROMPT = """You are one agent in PaperSwarm, a multi-agent system that writes ICLR-style
research papers. You are writing one section of a submission.

THE SINGLE HARD RULE OF THIS SYSTEM:
Every numeric result in the body text MUST be written as the LaTeX macro
\\result{<claim_id>}, where <claim_id> is one of the ids given to you.
You must NEVER write a digit that reports an experimental result.
The system replaces each \\result{...} with the real value at build time, and it
REJECTS any section that contains a hand-written result number. A section that
reports a number without a claim id is worthless to us, even if the number is correct.

SCOPE DISCIPLINE (this is graded as harshly as the numeric rule):
You are given the claims THIS section is allowed to report. Report only those.
Do NOT restate a result that another section owns just to fill space. In a previous
build every section repeated the same eleven numbers and a reviewer wrote that
"Sections 2-5 restate the same numbers with minimal new insight". Each section must
carry its own weight:
- Introduction: motivation and the single headline number. No setup, no baselines.
- Related Work: what other systems do and how this one differs. NO numbers at all.
- Method: the system's design, state representation and gate predicates. NO numbers.
- Experiments: the setup and the comparison. This is where the numbers live.
- Discussion: what the result means and where the evidence stops. Few numbers.
- Conclusion: one paragraph, headline number only, no new claims.

Other rules:
- English, academic style, third person, no marketing language.
- Output ONLY the body of the section. Do NOT output \\documentclass, \\usepackage,
  \\begin{document}, \\end{document}, or the \\section{...} heading itself.
- Do NOT use \\subsection. Write 3-5 continuous paragraphs of plain prose.
- Do NOT redefine \\result, and do not use \\newcommand on it.
- Do not invent citations. Only use the citation keys you are given, and cite the
  system you are describing, not a call for papers or a blog post.
- No markdown code fences, no commentary before or after the LaTeX."""


#: 每章的写作契约：允许报告的 claim 子集 + 推荐引用的文献 + 内容边界。
#:
#: 为什么把 claim 按章切成子集：v1 里 7B 模型把同一批结果写进了所有章节，
#: 评审据此判 Writing_Clarity = -1。"重复"的物理来源就是 claim 清单对每一章
#: 都完全一样 —— 与其在 prompt 里求它别重复，不如直接把可用的数字按章限定。
SECTION_BRIEFS: dict[str, dict[str, Any]] = {
    "introduction": {
        "claims": ("cl-improve", "cl-ridge-mean", "cl-ols-mean"),
        "cites": ("xia2026researchloop", "ren2026evigraph", "luo2026xscientist"),
        "guidance": (
            "Motivate the problem: autonomous paper generators can produce a fluent "
            "manuscript whose claims are easier to state than to audit. Say what "
            "PaperSwarm adds (binding every reported number to a re-verifiable artifact "
            "and refusing to assemble a section that violates that binding) and quote "
            "the single headline number. Do NOT describe the dataset, the baselines, or "
            "the noise floor; do NOT write a concluding paragraph."
        ),
    },
    "related_work": {
        "claims": (),
        "cites": (
            "xia2026researchloop",
            "ren2026evigraph",
            "jha2026paperpilot",
            "luo2026xscientist",
            "nam2026medsci",
            "gaddipati2026mlreplicate",
        ),
        "guidance": (
            "Cite at least five of the available keys. For EACH key you cite, state in "
            "one sentence what that system does, then state what PaperSwarm does "
            "differently. Do not claim PaperSwarm is the first to gate on evidence -- "
            "several of these systems do exactly that. Position PaperSwarm honestly as "
            "a smaller, standalone, within-manuscript gate whose contribution is showing "
            "where such gates stop working. This section reports NO numbers; never use "
            "\\result{} here."
        ),
    },
    "method": {
        "claims": (),
        "cites": ("hoerl1970ridge",),
        "guidance": (
            "Describe the SYSTEM. Report no experimental numbers; never use \\result{}. "
            "Ground every statement in exactly these facts, and invent nothing beyond "
            "them:\n"
            "(a) A run writes two append-only JSONL ledgers. artifacts.jsonl records each "
            "experiment artifact as (artifact_id, path, sha256, bytes, producer). "
            "claims.jsonl records each numeric claim as (claim_id, artifact_id, a JSON "
            "Pointer into that artifact, value, display string).\n"
            "(b) A claim may only be registered if the stated value equals the value the "
            "artifact actually holds at that JSON Pointer; any mismatch aborts the run "
            "before writing.\n"
            "(c) Section writers never emit digits. They emit \\result{claim_id}; the "
            "assembler substitutes the verified value at build time.\n"
            "(d) The gate is a predicate over the draft text. It rejects a section when "
            "the text references an unknown claim_id, when a referenced claim fails "
            "re-verification against its artifact, when the text redefines the \\result "
            "macro, or when a literal number appears in a result context.\n"
            "(e) A rejected draft is returned to the writer with the list of violations "
            "and rewritten from scratch, up to a fixed attempt budget; if the budget is "
            "exhausted the run terminates without producing a PDF rather than shipping a "
            "weaker paper.\n"
            "(f) A structural check rejects a section that writes another section's "
            "material. The experiment itself is a controlled synthetic regression study; "
            "the statistical method for the baseline is due to \\citet{hoerl1970ridge}.\n"
            "IMPORTANT: never emit the literal macro text anywhere in this section -- "
            "not even inside \\texttt{}. The build-time checker treats any occurrence of "
            "that literal in the compiled PDF as an unresolved placeholder. Refer to it "
            "in words instead, e.g. 'a result macro that takes a claim identifier'."
        ),
    },
    "experiments": {
        "claims": (
            "cl-ols-mean",
            "cl-ols-std",
            "cl-ridge-mean",
            "cl-ridge-std",
            "cl-improve",
            "cl-alpha",
            "cl-noise",
            "cl-seeds",
            "cl-features",
            "cl-train",
            "cl-test",
        ),
        "cites": ("hoerl1970ridge",),
        "guidance": (
            "This is the only section that reports numbers, so it must use at least "
            "five \\result{} references. State the full data-generating process so a "
            "reader could reproduce it: a design matrix with 120 features generated from "
            "a low-rank latent factor model, 90 training rows and 200 test rows per "
            "split, additive Gaussian noise on the target with standard deviation 0.35 "
            "whose square is the irreducible noise floor, averaged over 8 random seeds. "
            "State the protocol explicitly: the ridge penalty is chosen by 5-fold "
            "cross-validation inside the training split, and the test split is evaluated "
            "exactly once. Be explicit that an earlier version selected the penalty on "
            "the test set and that this was a methodological error now corrected. "
            "Report the OLS and ridge means with standard deviations, the relative "
            "improvement, the selected penalty and the noise floor, then comment on how "
            "far each method remains above the floor."
        ),
    },
    "discussion": {
        "claims": ("cl-improve", "cl-ridge-mean", "cl-ols-mean", "cl-noise"),
        "cites": ("luo2026xscientist",),
        "guidance": (
            "Interpret the result and then state the boundary of the evidence. Use the "
            "two means, the improvement and the noise floor; add no other numbers. "
            "Two points must be made explicit. First, why regularisation helps here: the "
            "design matrix is ill-conditioned (120 features against 90 training rows), "
            "so the minimum-norm solution has high variance. Second, and more important, "
            "state the limitation of the contribution: the gate verifies that a reported "
            "value matches its artifact, which is a statement about provenance, not about "
            "validity. In this very study the pre-registered penalty was selected on the "
            "test set and the gate passed the resulting number without objection, because "
            "the number was faithfully recorded. Provenance is not validity, and a gate "
            "that only binds numbers to artifacts cannot detect a flaw in the design that "
            "produced the artifact. \\citet{luo2026xscientist} makes the adjacent point "
            "that a passing integrity check is not scientific truth."
        ),
    },
    "conclusion": {
        "claims": ("cl-improve",),
        "cites": (),
        "guidance": (
            "One paragraph. Restate the contribution, the headline number and the main "
            "limitation (provenance is not validity). Introduce no new numbers and no "
            "new citations."
        ),
    },
}


# 注意：越界检测的实现放在内核 paperswarm.assemble 里（check_section_prose），
# 而不是留在这个脚本里 —— 离线测试只加载 src/，放在这里就没法被回归测试覆盖。


def strip_fences(text: str) -> str:
    """剥离 Markdown 代码围栏（模型常自作主张包一层）。"""
    stripped = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", stripped, re.S)
    if fenced:
        return fenced.group(1).strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    return stripped.strip()


def strip_section_heading(text: str) -> str:
    """去掉模型可能自己加的 ``\\section{...}``（标题由组装器统一加）。"""
    return re.sub(r"\\section\*?\{[^}]*\}\s*", "", text, count=1).strip()


def build_claim_table(claims: Sequence[tuple[str, str, str]]) -> str:
    """把 claim 清单渲染成给模型看的表格文本；空清单返回空串。"""
    if not claims:
        return ""
    lines = ["| claim_id | what the number means |", "|---|---|"]
    for claim_id, meaning, _pointer in claims:
        lines.append(f"| `{claim_id}` | {meaning} |")
    return "\n".join(lines)


def claims_for_section(stem: str) -> tuple[tuple[str, str, str], ...]:
    """按 :data:`SECTION_BRIEFS` 把全局 claim 清单切出本章可用的子集。"""
    allowed = set(SECTION_BRIEFS[stem]["claims"])
    return tuple(spec for spec in CLAIM_SPECS if spec[0] in allowed)


def build_section_prompt(
    *,
    section_title: str,
    target_words: int,
    must_cite: bool,
    claim_table: str,
    citation_keys: Sequence[str],
    guidance: str,
) -> str:
    """构造某一章节的用户侧提示。"""
    parts = [
        f"Paper title: {PAPER_TITLE}",
        f"Section to write: {section_title}",
        "",
        "How this section must be written:",
        guidance,
        "",
        "Evidence claims this section is allowed to report (these are the ONLY numbers "
        "you may report; a claim that is not in this list belongs to another section "
        "and must not appear here):",
        claim_table if claim_table else "(none -- this section reports no numbers)",
        "",
        f"Target length: about {target_words} words.",
    ]
    if must_cite:
        parts += [
            "",
            "This section MUST report experimental results, so it MUST contain at "
            "least one \\result{claim_id} reference. Aim for 5-8 of them.",
        ]
    if citation_keys:
        parts += [
            "",
            "Available citation keys (use \\citep{key} or \\citet{key}; do not use "
            "any key outside this list): "
            + ", ".join(f"`{key}`" for key in citation_keys),
        ]
    else:
        parts += [
            "",
            "You have no citation keys for this section: do not cite anything here.",
        ]
    parts += [
        "",
        "Reminder: any experimental number written as literal digits will cause the "
        "section to be rejected. Use \\result{claim_id} instead.",
    ]
    return "\n".join(parts)


def generate_section(
    client: ChatClient,
    telemetry: Telemetry,
    *,
    ledger: Ledger,
    section_title: str,
    stem: str,
    must_cite: bool,
    role: str,
    target_words: int,
    max_attempts: int,
) -> tuple[str, int]:
    """生成一个章节，并通过证据门控；不通过就带着问题让模型重写。

    Returns:
        ``(正文文本, 实际尝试次数)``。

    Raises:
        RuntimeError: 用尽尝试次数仍未通过门控。
    """
    brief = SECTION_BRIEFS[stem]
    claim_table = build_claim_table(claims_for_section(stem))
    citation_keys = tuple(str(key) for key in brief["cites"])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_section_prompt(
                section_title=section_title,
                target_words=target_words,
                must_cite=must_cite,
                claim_table=claim_table,
                citation_keys=citation_keys,
                guidance=str(brief["guidance"]),
            ),
        },
    ]

    last_issues: list[str] = []
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        response = client.chat(messages, temperature=0.4, max_tokens=6000)
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry.record_call(
            span=f"writer.{stem}",
            role=role,
            model=response.model or "unknown",
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=latency_ms,
            attempts=response.attempts,
            finish_reason=response.finish_reason,
            ok=True,
            # 单价从 profile 显式透传。遥测层不内置价格表：本机 Ollama 的 0 元是
            # 配置里写明的已知事实，而不是"未定价"。
            price_per_1k_prompt=client.config.price_per_1k_prompt,
            price_per_1k_completion=client.config.price_per_1k_completion,
        )
        text = strip_section_heading(strip_fences(response.content))

        report: GateReport = gate_text(
            text,
            ledger,
            tex_path=f"<{stem}>",
            require_refs=must_cite,
            strict_literals=True,
        )
        # 结构问题与证据问题走同一个重写循环：两者都意味着"这一章不能用"。
        structural = check_section_prose(text, section_title)
        if report.ok and not structural:
            telemetry.record_event(
                span=f"writer.{stem}",
                kind="gate_pass",
                detail=f"attempt={attempt} refs={report.total_refs}",
                payload={"warnings": report.warnings},
            )
            return text, attempt

        last_issues = [
            f"line {item.line} ({item.claim_id or '-'}): {item.reason}"
            for item in report.issues
        ] + structural
        telemetry.record_event(
            span=f"writer.{stem}",
            kind="gate_reject",
            ok=False,
            detail=f"attempt={attempt}",
            payload={"issues": last_issues},
        )
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your section was REJECTED by the evidence gate. Problems:\n"
                    + "\n".join(f"- {item}" for item in last_issues)
                    + "\n\nRewrite the ENTIRE section from scratch, fixing every "
                    "problem. Every experimental number must be \\result{claim_id} "
                    "with an id from the table. Do not write literal result digits."
                ),
            }
        )

    raise RuntimeError(
        f"章节 {stem} 在 {max_attempts} 次尝试后仍未通过证据门控：{last_issues}"
    )


def generate_abstract(
    client: ChatClient,
    telemetry: Telemetry,
    *,
    ledger: Ledger,
    section_texts: dict[str, str],
    max_attempts: int,
) -> str:
    """基于已完成章节生成摘要。"""
    claim_table = build_claim_table(CLAIM_SPECS)
    digest = "\n\n".join(
        f"### {title}\n{text[:1200]}" for title, text in section_texts.items()
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Paper title: {PAPER_TITLE}\n\n"
                "Write the ABSTRACT of this paper (single paragraph, about 160 words).\n\n"
                "Here are the sections already written:\n" + digest + "\n\n"
                "Available evidence claims:\n" + claim_table + "\n\n"
                "The abstract SHOULD quote the headline result: use "
                "\\result{cl-improve} for the relative improvement and "
                "\\result{cl-ridge-mean} / \\result{cl-ols-mean} for the two means. "
                "Do not write those digits yourself."
            ),
        },
    ]

    last_issues: list[str] = []
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        response = client.chat(messages, temperature=0.4, max_tokens=3000)
        latency_ms = int((time.perf_counter() - started) * 1000)
        telemetry.record_call(
            span="writer.abstract",
            role="writer.abstract",
            model=response.model or "unknown",
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=latency_ms,
            attempts=response.attempts,
            finish_reason=response.finish_reason,
            ok=True,
            price_per_1k_prompt=client.config.price_per_1k_prompt,
            price_per_1k_completion=client.config.price_per_1k_completion,
        )
        text = strip_fences(response.content)
        report = gate_text(text, ledger, tex_path="<abstract>", strict_literals=True)
        if report.ok:
            return text
        last_issues = [
            f"line {item.line}: {item.reason}" for item in report.issues
        ]
        messages.append({"role": "assistant", "content": response.content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "The abstract was rejected:\n"
                    + "\n".join(f"- {item}" for item in last_issues)
                    + "\n\nRewrite the abstract, using \\result{claim_id} for every "
                    "experimental number."
                ),
            }
        )
    raise RuntimeError(f"摘要未通过门控：{last_issues}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="端到端生成一篇 ICLR 论文 PDF。")
    parser.add_argument("--config", default=str(ROOT / "configs" / "model.yaml"))
    parser.add_argument(
        "--profile",
        default="local-ollama",
        help="configs/model.yaml 里的 profile 名（默认本机 Ollama，零边际成本）",
    )
    parser.add_argument("--template-dir", default=str(ROOT / "templates" / "iclr2026"))
    parser.add_argument("--tectonic", default="E:/tools/tectonic/tectonic.exe")
    parser.add_argument(
        "--bundle", default="E:/tools/tectonic/bundle/texlive2024-0312.ttb"
    )
    parser.add_argument("--cache-dir", default="E:/tools/tectonic/cache")
    parser.add_argument("--runs-root", default=str(ROOT / "runs"))
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--skip-llm-check",
        action="store_true",
        help="不调用模型，验证实验/台账/组装/编译链路",
    )
    args = parser.parse_args(argv)

    # 入口加载 .env.local：库层只认 os.environ，"文件 -> 环境变量"这一步只在入口做。
    # 已存在的环境变量优先，所以命令行 export 仍可临时覆盖。
    applied = load_env_file(base_dir=ROOT)
    if applied:
        print(f"[env] 载入 {ROOT / '.env.local'}：{', '.join(applied)}")

    run_id = "paper-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run] {run_id} -> {run_dir}")

    telemetry = Telemetry(run_dir, run_id)
    ledger = Ledger(run_dir / "ledger")

    # --- 1. 实验 ---------------------------------------------------------
    print("[1/5] 跑对照实验 ...")
    started = time.perf_counter()
    experiment = run_experiment()
    metrics_path = experiment.write_json(run_dir / "exp" / "metrics.json")
    experiment_seconds = time.perf_counter() - started
    print(
        f"      OLS={experiment.results['ols_mse_mean']} "
        f"Ridge={experiment.results['ridge_mse_mean']} "
        f"improve={experiment.results['improvement_pct']}% "
        f"({experiment_seconds:.2f}s)"
    )
    telemetry.record_event(
        span="experiment",
        kind="experiment_done",
        detail=f"{experiment_seconds:.2f}s",
        payload={"metrics": experiment.results},
    )

    # --- 2. 证据 ---------------------------------------------------------
    print("[2/5] 登记证据台账 ...")
    artifact = ledger.register_artifact(
        metrics_path, producer="analyst", artifact_id="artifact.metrics"
    )
    print(f"      artifact {artifact.artifact_id} sha256={artifact.sha256[:12]}…")
    for claim_id, meaning, pointer in CLAIM_SPECS:
        record = ledger.add_claim(
            text=meaning,
            artifact_id=artifact.artifact_id,
            locator=pointer,
            claim_id=claim_id,
            run_id=run_id,
        )
        print(f"      claim {record.claim_id:14s} = {record.display}")
    verify = ledger.verify()
    print(f"      台账校验: passed={verify.passed} failed={verify.failed} ok={verify.ok}")
    if not verify.ok:
        print("[FATAL] 台账自校验未通过，中止", file=sys.stderr)
        return 1

    # --- 3. 写作 ---------------------------------------------------------
    # 关键：草稿目录与回填输出目录必须不同。
    # 曾经两者都是 paper/sections/，导致 resolve 覆盖掉含 \result{} 的原始稿件 ——
    # 原始稿是审计证据（它证明"模型没写数字"），不能被中间产物覆盖掉。
    sections_dir = run_dir / "paper" / "drafts"
    sections_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_llm_check:
        print("[3/5] 跳过模型写作（--skip-llm-check），写入最小占位正文 ...")
        placeholder = (
            "This build was produced with --skip-llm-check. "
            "The pipeline below (ledger, gate, assembly, compilation) is "
            "exercised, but no model-generated prose is included."
        )
        (sections_dir / "abstract.tex").write_text(
            placeholder + " Ridge improves over OLS by \\result{cl-improve} percent.\n",
            encoding="utf-8",
        )
        for stem, title, _must, _role, _words in SECTION_SPECS:
            body = f"\\subsection*{{Placeholder}}\n{placeholder}\n"
            if stem == "experiments":
                body += (
                    "OLS mean test MSE is \\result{cl-ols-mean} "
                    "(standard deviation \\result{cl-ols-std}); ridge attains "
                    "\\result{cl-ridge-mean} with standard deviation "
                    "\\result{cl-ridge-std} at penalty \\result{cl-alpha}. "
                    "The relative improvement is \\result{cl-improve} percent, "
                    "against an irreducible noise floor of \\result{cl-noise}.\n"
                )
            (sections_dir / f"{stem}.tex").write_text(body, encoding="utf-8")
    else:
        print("[3/5] 调用模型逐章写作（每章过证据门控）...")
        config = LLMConfig.from_yaml(args.config, profile=args.profile)
        print(f"      model={config.model} endpoint={config.endpoint}")
        client = ChatClient(config)

        section_texts: dict[str, str] = {}
        for stem, title, must_cite, role, words in SECTION_SPECS:
            text, attempts = generate_section(
                client,
                telemetry,
                ledger=ledger,
                section_title=title,
                stem=stem,
                must_cite=must_cite,
                role=role,
                target_words=words,
                max_attempts=args.max_attempts,
            )
            (sections_dir / f"{stem}.tex").write_text(text + "\n", encoding="utf-8")
            section_texts[title] = text
            print(f"      {stem:14s} ok (attempts={attempts}, {len(text)} chars)")

        abstract = generate_abstract(
            client,
            telemetry,
            ledger=ledger,
            section_texts=section_texts,
            max_attempts=args.max_attempts,
        )
        (sections_dir / "abstract.tex").write_text(abstract + "\n", encoding="utf-8")
        print(f"      abstract       ok ({len(abstract)} chars)")

    # --- 4/5. 组装 + 编译 -------------------------------------------------
    print("[4/5] 组装论文（套 ICLR 2026 模板 + 台账回填）...")
    print("[5/5] 编译 PDF ...")
    try:
        report = assemble_paper(
            template_dir=args.template_dir,
            sections_dir=sections_dir,
            work_dir=run_dir / "paper",
            ledger=ledger,
            title=PAPER_TITLE,
            tectonic=args.tectonic,
            bundle=args.bundle,
            cache_dir=args.cache_dir,
            bib_entries=BIB_ENTRIES,
            sections=tuple((stem, title) for stem, title, *_ in SECTION_SPECS),
        )
    except (AssemblyError, LedgerError) as exc:
        print(f"[FATAL] 组装失败: {exc}", file=sys.stderr)
        telemetry.record_event(span="assemble", kind="failed", ok=False, detail=str(exc))
        telemetry.write_summary({"run_id": run_id, "ok": False})
        return 1

    summary = report.to_dict()
    (run_dir / "assembly_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    telemetry.record_event(
        span="assemble",
        kind="assembly_done",
        ok=report.ok,
        payload={"pdf": summary.get("pdf")},
    )
    telemetry.write_summary(
        {
            "run_id": run_id,
            "ok": report.ok,
            "experiment_seconds": round(experiment_seconds, 3),
            "pdf": summary.get("pdf"),
        }
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
    print(f"产出: {report.work_dir}")
    print("=" * 66)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
