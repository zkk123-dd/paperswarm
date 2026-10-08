# Stanford Agentic Reviewer 评测结果

> 服务：paperreview.ai ｜ venue：**ICLR**
> 提交时间：`2026-10-08T04:05:00.851004` ｜ 出分时间：`2026-10-08T04:10:50.730685`
> access token：`uIvJ-Nrg582Mizje588GgCE-4kh4pefF1fFzCIKsvhg`
> 论文：PaperSwarm: An Evidence-Gated Multi-Agent System for Reproducible Paper Generation

## 综合分

# 5.8 / 10

（官方机制：先在 7 个维度上二值打分，再用线性回归映射到 1–10。）

## 7 维二值评分

| 维度 | 中文 | 判定 |
|---|---|---|
| `Originality` | 原创性 | — |
| `Question_Importance` | 研究问题的重要性 | **+1** ✅ |
| `Claims_Support` | 结论是否有充分依据 | — |
| `Experimental_Soundness` | 实验的严谨性 | — |
| `Writing_Clarity` | 写作清晰度 | **+1** ✅ |
| `Value_to_Community` | 对研究社区的价值 | — |
| `Prior_Work_Context` | 与既有工作的定位是否恰当 | — |

**正向 2 / 7，中性 5 / 7，负向 0 / 7。**

## 与目标档位对比

| 档位 | 分数 | 依据 | 本文 |
|---|---|---|---|
| 及格 | ≥ 4.21 | ICLR 2026 人类投稿均分 | ✅ 达到 |
| 良好 | ≥ 5.05 | FARS 100 篇均分 | ✅ 达到 |
| 优秀 | ≥ 5.39 | ICLR 2026 接收论文均分 | ✅ 达到 |

**结论：综合分 5.8，达到及格线 4.21，高出 1.59 分。**

---

## 评审正文（原文，未改写）

### Summary

This paper proposes PaperSwarm, a multi-agent manuscript-writing workflow that enforces provenance for every numeric claim through an evidence gate. Authors cannot emit digits directly; instead, they insert macros that resolve only if the referenced claim matches an immutable artifact via a JSON Pointer and passes re-verification at assembly time. The system is demonstrated on a synthetic low-rank regression experiment comparing minimum-norm OLS and ridge regression, with the headline 49.29% test-MSE reduction bound to a recorded artifact rather than free text; the authors explicitly scope the contribution to provenance (traceable reporting) rather than validity (experimental soundness).

### Strengths

- Technical novelty and innovation
  - The paper cleanly isolates a minimal, enforceable evidence gate at the manuscript boundary: numeric claims must reference immutable artifacts via JSON Pointer and are materialized only through macros that pass re-verification.
  - The approach enforces scope separation among section writers, aiming to prevent leakage and out-of-scope restatements—an under-explored failure mode in agentic paper generation.
  - The ledger design (artifacts.jsonl, claims.jsonl) is simple and auditable, with SHA-256 digests and pointer-based addressing that are easy to implement and reason about.

- Experimental rigor and validation
  - The demonstration explicitly documents a corrected evaluation protocol (penalty selected by CV within train) and acknowledges that an earlier leakage passed the gate—underscoring the stated limitation that provenance is not validity.

- Clarity of presentation
  - The manuscript is concise, transparent about scope, and frank about limitations; the claim-to-artifact mechanism and gating predicates are explained clearly.

- Significance of contributions
  - The problem—auditable numerical claims in LLM-generated manuscripts—is timely and important, as evidenced by recent benchmarks highlighting fabricated or unsupported claims in autonomous papers.
  - The work provides a concrete, minimal pattern that could be adopted in other agentic writing systems to elevate provenance without overhauling entire research loops.

### Weaknesses

- Technical limitations or concerns
  - The “no digits emitted by writers” rule is brittle and potentially easy to circumvent (e.g., numbers written in words, numerals in citations, dates, equations). The scope and enforcement of “result context” are underspecified.
  - The evidence model is narrow: only JSON-addressable artifacts and scalar/subdocument values via JSON Pointer; derived quantities (e.g., confidence intervals, relative improvements computed from multiple operands) require precomputed artifacts rather than declarative, rechecked formulas.
  - Ledger immutability is only informally claimed (append-only JSONL). There is no tamper-evident log, signing, or verifiable content-addressed storage; threat models and trust assumptions are not specified.

- Experimental gaps or methodological issues
  - The evaluation is a single, synthetic ML task with small scale and no stress-testing of the gate. There is no seeded-defect ablation (e.g., leakage, pointer misbinding, rounding drift, out-of-scope restatements) to quantify detection.
  - No comparisons with existing provenance/verification systems (e.g., typed evidence graphs, layered trace models, neurosymbolic verifiers) on detection coverage, precision, or usability.
  - No measurement of false rejects (legitimate claims blocked), overhead (tokens, latency), impact on writing quality, or user burden across attempts.

- Clarity or presentation issues
  - The gate predicate language and “result context” definition lack formalization: how literals are detected, how rounding is handled, and how macros are prevented from redefinition in edge cases (tables, math, captions) are not fully specified.
  - The structural scope-gating across sections is discussed but not technically detailed (criteria, heuristics, false positive/negative behavior).

- Missing related work or comparisons
  - The manuscript overlooks related provenance/verification threads beyond the 2026 works cited: e.g., neurosymbolic numeric verification against authoritative artifacts, layered evidence graphs for agent traces, declarative benchmark/task specifications for conceptual reproducibility, and mutation-based reproducibility checks. This weakens the positioning and the evaluation targets.

### Detailed Comments

- Technical soundness evaluation
  - The core mechanism—register claims with exact-value checks against JSON artifacts, re-verify at assembly, and refuse digits in result contexts—is sound as a provenance-binding layer. The use of SHA-256 digests and JSON Pointers is straightforward and auditable.
  - However, the trust and threat model is not explicit. Append-only JSONL without cryptographic chaining/signing does not ensure tamper-evidence; a malicious actor with file access could edit both the artifact and claim records offline. Consider a Merkle-log or transparency ledger and signatures to make “append-only” enforceable.
  - Floating-point and display concerns are acknowledged via a “display string,” but the paper does not specify rounding/tolerance rules for re-verification, or how units are normalized, which is critical for numeric integrity at scale.
  - Derived claims appear to require a precomputed value in an artifact; there is no declarative recomputation or multi-operand binding (e.g., a verified formula plan) akin to solver-checked or provenance-typed numeric constraints. This limits expressivity and invites “computation in the dark” with only the end value recorded.

- Experimental evaluation assessment
  - The single synthetic regression experiment demonstrates the mechanics but not the robustness of the gate. The admission that an earlier leakage passed the gate is instructive but would be far more compelling in a formal seeded-defect protocol: inject defects (test-set selection, pointer misbinding, rounding drift, macro redefinition, section-scope violations) and report detection rates, coverage, and failure modes.
  - A mutation-style evaluation (e.g., random seed shifts, CV folds, data splits, dependency pins) would quantify whether provenance/claim registration catches relevant classes of reproducibility-relevant mutations, even if limited to manuscript-level gates.
  - No baseline comparisons: e.g., claim-to-evidence graphs (typed evidence DAGs), layered trace dashboards, or neurosymbolic numeric verifiers demonstrate stronger detection claims and diagnosability; this paper should benchmark detection precision/recall or at least perform qualitative case studies against such systems.
  - Usability and cost metrics are absent: attempts per section before acceptance, time/token overhead, and how often sections fail due to over-strict gates (false rejects).

- Comparison with related work (using the summaries provided)
  - LEDGER focuses on layered, navigable claim-to-evidence graphs for agent sessions; compared to PaperSwarm’s minimal ledger, LEDGER provides richer lineage and diagnosability. PaperSwarm could benefit from aggregating claim registration into a layered provenance view.
  - VeriFin provides authoritative, neurosymbolic numeric verification with zero false accepts on financial filings by separating proposal and verification and checking formulas with Z3. PaperSwarm’s equality-only checks are less expressive; integrating authoritative formula checks or constraint-based verification could reduce acceptance of incorrect derived numbers and improve diagnostics.
  - Croissant Tasks formalize benchmark/task specifications for conceptual reproducibility; PaperSwarm’s manuscript-level gate could reference or embed such task manifests to strengthen specification coverage and decouple claims from specific codepaths.
  - MLReproMutate and similar mutation protocols quantify detection of reproducibility-relevant changes. PaperSwarm lacks a comparable evaluation; adopting outcome-blind seeded mutations would substantiate detection claims beyond a toy example.
  - SOP-MCP shows the value of step-level, externally enforced procedure delivery with auditable logs. PaperSwarm could externalize gate steps similarly to improve predictability and per-step audit trails.
  - CAIRN’s DAG-based coordination and Evi-Graph’s typed evidence graphs provide broader state and dependency tracking than PaperSwarm’s flat claim ledger; adopting a graph model could improve cross-claim consistency checks and section-scope enforcement.

- Discussion of broader impact and significance
  - The work squarely addresses a critical pain point: fluent, ungrounded numeric assertions in LLM-generated research. As a minimal, composable layer, PaperSwarm could be practical to adopt and integrate into existing authoring pipelines.
  - The authors are appropriately cautious: provenance is not validity. A risk is that readers (or automated reviewers) over-interpret provenanced claims as scientifically sound. Clear UI/UX cues, artifact provenance badges, and explicit disclaimers are advisable.
  - If extended with stronger verification (typed evidence graphs, authoritative formulas), tamper-evident logs, and seeded-defect evaluations, PaperSwarm could form a useful building block in trustworthy autonomous research systems.

### Questions for Authors

1. What precisely constitutes a “result context,” and how do you prevent circumvention (e.g., numbers written in words, numerals in citations or dates, math-mode expressions, tables/figures)? Do you have a formal grammar or instrumentation for detection?
2. How are derived quantities handled (e.g., relative improvements, confidence intervals)? Are multi-operand, declarative computations supported and re-verified, or must derived values be precomputed and stored as artifacts?
3. What are the rounding, tolerance, and unit-normalization policies for numeric re-verification and display? How do you prevent both spurious mismatches and silent unit errors?
4. How is “append-only” enforced for the ledgers and artifacts in adversarial settings? Do you support tamper-evident logs (Merkle chaining, signatures) or content-addressed storage beyond SHA-256 digests embedded in JSONL?
5. Can you report quantitative gate behavior: average number of writer attempts, rejection reasons (unknown claim, literal number, scope violation), false rejects, and overhead (latency/tokens) across sections?
6. Have you evaluated seeded defects (e.g., pointer misbinding, cross-section leakage, rounding drift, leakage in model selection) to quantify detection rates? If not, what prevents such an evaluation, and could you adopt a mutation-style protocol?
7. How does the section-scope gate work in practice (heuristics, templates, learned classifiers)? What are the false positive/negative rates for “writing another section’s material,” and how are edge cases handled?
8. Will you release code, artifacts, and a minimal reproducible example so the community can adopt and test the gate? If so, under what license and with what documentation?

### Overall Assessment

This paper introduces a clear, minimal, and implementable evidence-gating mechanism for numeric claims in agent-generated manuscripts. The contribution is scoped and honest: it enforces provenance, not validity, and the narrative is crisp about what the gate can and cannot do. However, the evaluation is too thin for ICLR: a single synthetic ML study does not demonstrate robustness, and there is no seeded-defect testing, no comparative baselines against richer provenance/verifier systems, and no usability or overhead measurements. Important design details—result-context detection, derived-claim handling, rounding/tolerance, tamper-evidence, and section-scope enforcement—remain underspecified. The idea is timely and potentially useful, but to be suitable for a top-tier venue it needs a more comprehensive empirical demonstration, stronger positioning against related verification/provenance work, and either formal modeling or larger-scale stress tests that quantify detection coverage and failure modes. I lean toward rejection in the current form, with the view that a strengthened revision—adding seeded-defect ablations, comparative studies, and a released implementation—could be impactful.

### Binary scores (raw)

```
```
TRIPLE_SCORES:
- Claims_Support: [0]  # Are the central claims adequately supported with evidence?
- Experimental_Soundness: [0]  # Are the experimental setup and research methodology sound?
- Writing_Clarity: [+1]  # Is the writing clear and well-organized?
- Prior_Work_Context: [0]  # Is the work properly contextualized relative to prior work?
- Question_Importance: [+1]  # Are the research questions being asked important?
- Originality: [0]  # Does the paper bring significant originality of ideas and/or execution?
- Value_to_Community: [0]  # Are the results valuable to share with the broader ICLR community?
```
```

---

## 免责（官方自述，必须一并引用）

- 评审由 AI 生成，**可能有错**。
- AI 与单个人类评审的 Spearman 相关仅 **0.42**；预测录取的 AUC **0.75**（低于人类评分的 0.84）。
- 该服务依托 arXiv 检索，**AI 领域更准，其他领域更不准**。
- 这是**可比的弱信号**，不是录用判决。
