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
    """@misc{fars2026,
  title        = {{FARS}: A Fully Automated Research System},
  author       = {{Analemma}},
  year         = {2026},
  howpublished = {\\url{https://analemma.ai/blog/introducing-fars/}},
  note         = {Accessed 2026-09-29}
}""",
    """@misc{iclr2026cfp,
  title        = {International Conference on Learning Representations 2026:
                  Call for Papers and Author Guidelines},
  author       = {{ICLR} {2026} Organizing Committee},
  year         = {2026},
  howpublished = {\\url{https://iclr.cc/}},
  note         = {Submission format: 9 pages of main text; anonymous review}
}""",
    """@article{hoerl1970ridge,
  title   = {Ridge Regression: Biased Estimation for Nonorthogonal Problems},
  author  = {Hoerl, Arthur E. and Kennard, Robert W.},
  journal = {Technometrics},
  volume  = {12},
  number  = {1},
  pages   = {55--67},
  year    = {1970},
  note    = {BibTeX entry transcribed by the authors; verify against the
             publisher record before camera-ready}
}""",
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

Other rules:
- English, academic style, third person, no marketing language.
- Output ONLY the body of the section. Do NOT output \\documentclass, \\usepackage,
  \\begin{document}, \\end{document}, or the \\section{...} heading itself.
- Do NOT use \\subsection either. Write 3-5 continuous paragraphs of plain prose
  with no sub-headings at all.
- Write ONLY about the section you were asked to write. Never put another
  section's material inside it: the Introduction must NOT contain an
  experimental-setup breakdown, a baseline discussion, or a "Conclusion"
  paragraph -- those belong to their own sections.
- Do NOT redefine \\result, and do not use \\newcommand on it.
- Do not invent citations. Only use the citation keys you are given.
- No markdown code fences, no commentary before or after the LaTeX."""


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
    """把 claim 清单渲染成给模型看的表格文本。"""
    lines = ["| claim_id | what the number means |", "|---|---|"]
    for claim_id, meaning, _pointer in claims:
        lines.append(f"| `{claim_id}` | {meaning} |")
    return "\n".join(lines)


def build_section_prompt(
    *,
    section_title: str,
    target_words: int,
    must_cite: bool,
    claim_table: str,
    citation_keys: Sequence[str],
) -> str:
    """构造某一章节的用户侧提示。"""
    parts = [
        f"Paper title: {PAPER_TITLE}",
        f"Section to write: {section_title}",
        "",
        "Available evidence claims (these are the ONLY numbers you may report):",
        claim_table,
        "",
        f"Target length: about {target_words} words.",
    ]
    if must_cite:
        parts += [
            "",
            "This section MUST report experimental results, so it MUST contain at "
            "least one \\result{claim_id} reference. Aim for 4-6 of them: the OLS "
            "and ridge means with their standard deviations, the relative "
            "improvement, the selected penalty, and the noise floor.",
        ]
    parts += [
        "",
        "Available citation keys (use \\citep{key} or \\citet{key}): "
        + ", ".join(f"`{key}`" for key in citation_keys),
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
    claim_table = build_claim_table(CLAIM_SPECS)
    citation_keys = ("fars2026", "iclr2026cfp", "hoerl1970ridge")
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
            ),
        },
    ]

    last_issues: list[str] = []
    for attempt in range(1, max_attempts + 1):
        started = time.perf_counter()
        response = client.chat(messages, temperature=0.4, max_tokens=1600)
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
        response = client.chat(messages, temperature=0.4, max_tokens=900)
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
