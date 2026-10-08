# Stanford Agentic Reviewer 评测结果

> 服务：paperreview.ai ｜ venue：**ICLR**
> 提交时间：`2026-09-29T09:00:00.092028` ｜ 出分时间：`2026-09-29T09:09:16.069123`
> access token：`3xjzh0ZcSPc2uhS7lDhDH_r0r5Td3Dl-FiWgSMLAB90`
> 论文：PaperSwarm: An Evidence-Gated Multi-Agent System for Reproducible Paper Generation

## 综合分

# 2.4 / 10

（官方机制：先在 7 个维度上二值打分，再用线性回归映射到 1–10。）

## 7 维二值评分

| 维度 | 中文 | 判定 |
|---|---|---|
| `Originality` | 原创性 | **-1** ❌ |
| `Question_Importance` | 研究问题的重要性 | **+1** ✅ |
| `Claims_Support` | 结论是否有充分依据 | **-1** ❌ |
| `Experimental_Soundness` | 实验的严谨性 | **-1** ❌ |
| `Writing_Clarity` | 写作清晰度 | **-1** ❌ |
| `Value_to_Community` | 对研究社区的价值 | **-1** ❌ |
| `Prior_Work_Context` | 与既有工作的定位是否恰当 | **-1** ❌ |

**正向 1 / 7**，负向 6 / 7。

## 与目标档位对比

| 档位 | 分数 | 依据 | 本文 |
|---|---|---|---|
| 及格 | ≥ 4.21 | ICLR 2026 人类投稿均分 | ❌ 未达到 |
| 良好 | ≥ 5.05 | FARS 100 篇均分 | ❌ 未达到 |
| 优秀 | ≥ 5.39 | ICLR 2026 接收论文均分 | ❌ 未达到 |

**结论：综合分 2.4，距及格线 4.21 差 1.81 分。**

---

## 评审正文（原文，未改写）

### Summary

This paper proposes PaperSwarm, an “evidence-gated” multi-agent system for generating reproducible research papers. The empirical section, however, centers almost entirely on a simple synthetic regression study comparing minimum-norm OLS to ridge regression (p > n), reporting that ridge (α = 0.1) reduces mean test MSE by 50.94% relative to OLS with lower variance across seeds. While the manuscript frames these findings as validating the proposed system, it provides no substantive description, formalization, or evaluation of the multi-agent architecture or the evidence-gating mechanism itself.

### Strengths

- Technical novelty and innovation
  - The manuscript frames an important ambition: evidence-gated multi-agent workflows for reproducible scientific writing. Emphasizing gates and variance-aware evaluation is directionally aligned with emerging best practices in autonomous research systems.
  - The empirical toy study, while basic, correctly illustrates a known statistical point: ridge regularization can materially reduce variance versus minimum-norm OLS in high-dimensional, low-sample regimes (p > n).
- Experimental rigor and validation
  - The authors report multiple seeds, test/train splits, and standard deviations of MSE, which are better than single-run reporting.
  - The use of an explicit noise floor (0.1225) as a reference is a sensible idea, if properly justified and computed.
- Clarity of presentation
  - Numerical results are consistently reported (means, standard deviations, relative improvements), making the small-scale regression comparison easy to read.
- Significance of contributions
  - The problem the paper aims to tackle—reproducible, auditable, evidence-gated AI research pipelines—is highly significant and timely for the ICLR community.

### Weaknesses

- Technical limitations or concerns
  - The core system contribution (PaperSwarm’s multi-agent architecture and the “evidence-gating” mechanism) is not specified: there is no algorithm, protocol, state representation, validator/gate design, or implementation detail. As written, the paper does not enable evaluation or replication of the claimed system.
  - The only concrete “result” pertains to ridge vs. OLS on a synthetic dataset, which does not test a multi-agent paper-generation system or evidence gates in any meaningful way.
  - Hyperparameter selection appears to have used test MSE (“selected penalty 0.1 minimized mean test MSE”), which is methodologically invalid and inflates reported performance; proper validation or cross-validation is required.
  - No theoretical or systems analysis connects the regression results to the purported evidence-gated multi-agent process; the causal link between the system design and the observed MSE differences is absent.
- Experimental gaps or methodological issues
  - No baselines or comparisons to existing multi-agent/evidence-governed systems; no ablations of gates or agent roles; no evaluations on real paper-generation tasks; no human or automated review studies of generated outputs.
  - The synthetic data generation process is unspecified (distribution, correlation structure, how the noise floor was computed), preventing reproducibility or interpretation.
  - Missing common regularized baselines (e.g., LASSO, elastic net) and proper model selection procedures (CV), making the already limited empirical claim weaker.
  - No artifacts (code, seeds, run manifests, execution traces) are released to substantiate the reproducibility claims.
- Clarity or presentation issues
  - The manuscript is repetitive, and key terms (“evidence-gating,” “multi-agent contributions,” “decentralized architecture”) are introduced but not defined or concretized.
  - Sections 2–5 restate the same ridge-vs-OLS numbers with minimal new insight; critical design details are omitted.
- Missing related work or comparisons
  - The paper omits or minimally engages with substantial, directly relevant prior work on evidence-gated, provenance-backed, or graph-structured autonomous research systems (e.g., ResearchLoop, Paper Pilot, EviGraph, XScientist, ARA, MedSci Skills, MLReplicate) and provides no empirical comparison or positioning relative to these.

### Detailed Comments

- Technical soundness evaluation
  - The statistical comparison (OLS vs. ridge) is unsurprising and correct in spirit for p > n, but it does not validate a multi-agent “evidence-gated” system. Without a formal description of the gate predicates, provenance/ledgering, agent roles, or completion criteria, the central system claim remains unsupported.
  - Hyperparameter tuning on test data is a critical flaw; selection must occur via a validation set or cross-validation, with all models evaluated only once on the held-out test set.
  - The “irreducible noise floor” is referenced but not derived or empirically validated; how it was estimated is essential for it to serve as a meaningful benchmark.
- Experimental evaluation assessment
  - The experiment does not reflect the paper’s stated aim. To evaluate an evidence-gated paper-generation pipeline, one expects artifacts and metrics such as: gate pass/fail rates, claim-to-evidence coverage, provenance completeness, run-to-run reproducibility of paper claims, validator recall/precision for injected defects, or reviewer audit outcomes on generated manuscripts.
  - Missing comparisons to more appropriate baselines: e.g., systems that implement explicit claim-evidence bindings, workspace protocols, or graph-based evidence models. No user studies or benchmark tasks (e.g., MLReplicate, ARC-Bench-ML, or curated replication tasks) are attempted.
  - Without code, manifests, or execution traces, the reproducibility claim is not credible.
- Comparison with related work
  - There is a rich ecosystem of closely aligned work:
    - ResearchLoop formalizes evidence gates and claim ledgers with repository-backed state and reports quantitative reductions in unsupported claims.
    - Paper Pilot enforces approval-gated, evidence-locked workflows, demonstrating mechanical gains (e.g., zero fabricated citations under gating).
    - EviGraph operationalizes typed evidence graphs with dependency-aware inspection/repair and reports end-to-end improvements and claim support rates.
    - XScientist packages a git-like ARA protocol with claim-to-evidence anchors and integrity checks for long-running research artifacts.
    - ARA quantifies document-level reproducibility with structured, traceable extraction and scoring across large corpora.
    - MedSci Skills demonstrates determinism-first integrity gates with seeded-defect recall experiments.
    - MLReplicate exposes systemic gaps in agent-generated manuscripts and argues for stronger evidence gates and hybrid evaluation.
  - The present paper does not engage substantively with these, nor does it demonstrate novelty over their governance, provenance, or gate mechanisms.
- Broader impact and significance
  - If delivered, an evidence-gated multi-agent system for reproducible paper generation would be impactful. However, the current submission does not provide the conceptual or empirical foundations to advance community practice and risks reinforcing the very auditability gaps highlighted in prior surveys.
  - The mismatch between title/claims and the sole experimental content (basic ridge vs. OLS) could mislead readers about what is actually validated.

### Questions for Authors

1. What is the formal definition of “evidence-gating” in PaperSwarm? Please specify gate predicates, inputs, decision rules, and failure handling.
2. How are agents instantiated, specialized, and coordinated? Provide the system architecture, agent roles, message schema, and state representation.
3. What artifacts does PaperSwarm produce to substantiate reproducibility (e.g., manifests, provenance logs, seeds, execution traces, claim-evidence bindings)? Will you release them?
4. How is a paper “completion” decision made? Is there a verifiable workspace state or validator set akin to Vcomplete (or equivalent) that must pass?
5. How was the irreducible noise floor (0.1225) computed? Please provide data-generation details and derivation or empirical estimation procedure.
6. Did you select α = 0.1 using the test set? If not, please clarify the validation protocol; if yes, please rerun with proper validation/CV and report corrected results.
7. Why is the experimental focus on ridge vs. OLS an adequate evaluation of a multi-agent paper-generation system? What additional experiments directly test PaperSwarm’s gates and governance?
8. How does PaperSwarm compare empirically to existing evidence-governed systems (e.g., ResearchLoop, Paper Pilot, EviGraph, XScientist) on appropriate benchmarks or audits?
9. Can you provide ablations isolating the contribution of evidence-gating (e.g., No-gate vs. Gate) to outcomes such as fabricated content rate, claim support rate, or reviewer-assessed soundness?
10. What safeguards or abstention rules prevent unsupported claims from entering the final manuscript when evidence is insufficient or validators fail?

### Overall Assessment

The paper targets an important and timely problem—evidence-gated, reproducible multi-agent research pipelines—but does not substantiate its central claims. The manuscript lacks a concrete system description, gating formalism, or artifacts that would allow replication or auditing; it presents only a simple regression comparison (ridge vs. OLS) that neither exercises nor validates a multi-agent evidence-governance mechanism. Methodological flaws (notably apparent test-set hyperparameter selection) further undermine the limited empirical results. The work also omits engagement with a substantial body of closely related, rigorously engineered systems that already implement evidence gates, provenance, and audit trails, and provide stronger empirical demonstrations. As such, despite the importance of the stated research question, the submission is not yet suitable for ICLR. A future version would need to fully specify the architecture and evidence gates, release reproducibility-grade artifacts, evaluate on appropriate benchmarks and audits, and compare fairly to established baselines.

### Binary scores (raw)

```
- Claims_Support: -1  # Are the central claims adequately supported with evidence?
- Experimental_Soundness: -1  # Are the experimental setup and research methodology sound?
- Writing_Clarity: -1  # Is the writing clear and well-organized?
- Prior_Work_Context: -1  # Is the work properly contextualized relative to prior work?
- Question_Importance: +1  # Are the research questions being asked important?
- Originality: -1  # Does the paper bring significant originality of ideas and/or execution?
- Value_to_Community: -1  # Are the results valuable to share with the broader ICLR community?
```
```

---

## 免责（官方自述，必须一并引用）

- 评审由 AI 生成，**可能有错**。
- AI 与单个人类评审的 Spearman 相关仅 **0.42**；预测录取的 AUC **0.75**（低于人类评分的 0.84）。
- 该服务依托 arXiv 检索，**AI 领域更准，其他领域更不准**。
- 这是**可比的弱信号**，不是录用判决。
