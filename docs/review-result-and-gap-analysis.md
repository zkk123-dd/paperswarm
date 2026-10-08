# 外部评测结果与差距分析

> 数据来源：`reports/review_result.json`（paperreview.ai 原始返回）／可读版 `reports/review_result.md`
> 提交记录：`reports/review_submission.json`
> **本文只记录接口真实返回的内容与可被代码/检索验证的事实。**

---

## 1. 结果

| 项 | 值 |
|---|---|
| 服务 | Stanford Agentic Reviewer（paperreview.ai） |
| venue | **ICLR** |
| access token | `3xjzh0ZcSPc2uhS7lDhDH_r0r5Td3Dl-FiWgSMLAB90` |
| 提交时间 | 2026-09-29T09:00:00Z |
| 出分时间 | 2026-09-29T09:09:16Z（**约 9 分钟**，比官方预警的"数小时"快得多） |
| **综合分** | **2.4 / 10** |
| 正向维度 | **1 / 7** |

### 1.1 七维二值评分

| 维度 | 中文 | 判定 |
|---|---|---|
| `Question_Importance` | 研究问题的重要性 | **+1** ✅ |
| `Originality` | 原创性 | −1 ❌ |
| `Claims_Support` | 结论是否有充分依据 | −1 ❌ |
| `Experimental_Soundness` | 实验的严谨性 | −1 ❌ |
| `Writing_Clarity` | 写作清晰度 | −1 ❌ |
| `Value_to_Community` | 对研究社区的价值 | −1 ❌ |
| `Prior_Work_Context` | 与既有工作的定位 | −1 ❌ |

### 1.2 与目标档位对比

| 档位 | 阈值 | 依据 | 本文 |
|---|---|---|---|
| 及格 | ≥ 4.21 | ICLR 2026 人类投稿均分 | ❌ **差 1.81 分** |
| 良好 | ≥ 5.05 | FARS 100 篇均分 | ❌ |
| 优秀 | ≥ 5.39 | ICLR 2026 接收均分 | ❌ |

**结论：未达到及格线。** 唯一被认可的是"问题选得对"——正好是论文最不需要证明的那一项。

---

## 2. 评审的批评：逐条核实

我把每一条都拿代码或检索对了一遍。**不预设评审是对的，也不预设它错。**

### 2.1 属实，且是真缺陷

**① 用测试集选超参（最严重，已定位到行）**

> 评审原文："Hyperparameter selection appears to have used test MSE
> ('selected penalty 0.1 minimized mean test MSE'), which is methodologically
> invalid and inflates reported performance."

**核实结果：完全属实。** `src/paperswarm/lab.py`：

```python
# lab.py:157-160  —— 每个 alpha 都在测试集上算 MSE
for alpha in alphas:
    ridge_pred = fit_ridge(x_train, y_train, x_test, float(alpha))
    ridge_score = mse(ridge_pred, y_test)          # ← y_test，测试集
    alpha_scores[float(alpha)].append(ridge_score)

# lab.py:175      —— 取测试集 MSE 最小的 alpha 作为"最优"
best = min(sweep, key=lambda item: item["mse_mean"])
```

然后 `improvement_pct = (ols_mean - ridge_mean) / ols_mean`，其中 `ridge_mean` 就是
**被测试集挑出来的那个 α 的测试集 MSE**。所以论文里的 **+50.94%** 是**被测试集偏差抬高过的**。

正确做法：在验证集（或 CV）上选 α，最后只在留出的测试集上评一次。
`alpha_sweep` 里已经有每个 α 的完整记录，重跑成本极低（纯 numpy，0.5 秒）。

**② 论文没有系统描述与门控形式化**

> 评审原文："there is no algorithm, protocol, state representation, validator/gate
> design, or implementation detail. As written, the paper does not enable evaluation
> or replication of the claimed system."

**核实结果：属实。** 论文正文 4 页，`method.tex` 只说了"有台账、有门控"，
**没有给出门控谓词、状态表征、失败处理规则**。评审在 Questions 里连问 4 条（Q1–Q4）都是问这个。

讽刺的是：这些东西在本仓库里**全都真实存在且已实测**（`tex_gate.py` 的谓词、
五重上界、`gate_strict` 的绕过通道修复、`events.jsonl` 的真实拒绝记录），
**但一个字都没写进论文**。系统做得比论文里写的多得多。

**③ 没有相关工作定位 —— 点名的那批系统是真的**

> 评审点名：ResearchLoop / Paper Pilot / EviGraph / XScientist / ARA / MedSci Skills / MLReplicate

**核实结果：至少 3 个已独立检索确认真实存在，且高度对口：**

| 系统 | 出处（已核实） | 它做了什么 |
|---|---|---|
| **EviGraph** | 中科院自动化所 + 香港浸会大学，arXiv cs.AI | 类型化证据图作为**运行时状态**；节点含 Problem/Gap/Hypothesis/Experiment/Finding/Claim；**Claim Support Rate 比最强基线 +40.19%**，实验数据一致性 87.73% |
| **ResearchLoop** | 深圳大学，arXiv 2605.28282 | 自称 **"evidence-gated control plane"**；**claim ledger**、evidence contracts、**gate predicate specification**、claim admission algorithm；V0–V9 完整实验记录 + 自举案例研究 |
| **Paper Pilot** | arXiv cs.AI（CARE 方法论） | **8 道人工审批门**；证据锁定；实测"无门控时引用伪造率最高 25%，加门控后**伪造引用为 0**" |

（XScientist / ARA / MedSci Skills / MLReplicate 未逐一核实，不据此下结论。）

**这条批评打到了要害**：论文把"claim ↔ artifact SHA-256 绑定 + 门控"当作核心创新点，
而 **ResearchLoop 已把它形式化成 `claim ledger` + `gate predicate specification` 并公开发布**，
EviGraph 还用证据图做到了我们没做的"依赖缺失检测 + 下游子图重构"。

**我们的"核心创新点"在这个轴上并不新颖**，而且我们一篇都没引。

**④ 写作重复**

**核实结果：属实，且是我们早就知道的短板。** 7B 本地模型的直接后果；
评审说"Sections 2–5 restate the same ridge-vs-OLS numbers with minimal new insight"。
这与 `architecture-and-innovation.md` 里"第 5 条是最弱项"的自我判断一致 —— 自评没错，但没解决。

**⑤ 实验不支撑论文的主张**

> "the only concrete 'result' pertains to ridge vs. OLS on a synthetic dataset,
> which does not test a multi-agent paper-generation system or evidence gates in any way."

**核实结果：属实。** 论文声称在验证 PaperSwarm，但唯一的实验是合成数据上的
岭回归 vs OLS —— 这个实验**既不触发门控、也不涉及多智能体**。
评审建议的恰当指标（门控通过/拒绝率、claim-证据覆盖率、注入缺陷的检出率、
运行间可复现性）**我们其实全都有数据**：`events.jsonl` 里的 `gate_reject`、
离线 96 条测试里的 16 条章节结构断言、篡改检测、`llm_calls.jsonl` 的逐次台账 ——
**但论文一个都没报**。

### 2.2 需要修正的批评（不能全盘照收，也不能回避）

**⑥ "未发布任何产物"**

> "No artifacts (code, seeds, run manifests, execution traces) are released."

**部分成立，但原因不是我们没做。** 本仓库确实有全部产物：
`ledger/claims.jsonl`（含 SHA-256 + JSON Pointer）、`exp/metrics.json`、
`llm_calls.jsonl`、`events.jsonl`、`run_summary.json`、种子列表。
**问题在于论文里没有任何仓库链接或附录说明**，从评审只能看到 PDF 的视角，
"没有产物"这个判断是合理的。

**⑦ 关于"数据生成过程未说明"**

> "The synthetic data generation process is unspecified (distribution, correlation
> structure, how the noise floor was computed)"

**属实。** `make_dataset()` 的参数（`N_TRAIN`/`N_TEST`/`N_FEATURES`/`N_INFORMATIVE`/`NOISE_SIGMA`）
在代码里有，在论文里没写全。

---

## 3. 最重要的发现：门控保证了"可追溯"，没保证"正确"

这一节是本项目到目前最有价值的一条认知。

**看 α 那个数字走了什么路：**

| 环节 | 结果 |
|---|---|
| 实验产出 `ridge_mse_mean = 0.3041` | ✅ |
| 登记进台账，绑定 `exp/metrics.json` 的 SHA-256 与 JSON Pointer | ✅ |
| 正文只写 `\result{...}`，不手写数字 | ✅ |
| 通过 `gate_strict`（含 `strict_literals`） | ✅ |
| 回填成真实值，11/11 出现在最终 PDF | ✅ |
| 篡改验证：改产物 → 11 条 claim 全部拦截 | ✅ |
| 提交前自检 8/8 PASS | ✅ |

**每一道关卡都是绿的。而那个数字的产生方法是错的。**

`lab.py` 用测试集选了 α，所以 `0.3041` 和 `+50.94%` 是**被选择偏差抬高过的**。
这个错误：

- 不是"编造"——数字确实来自真实计算；
- 不是"誊写错"——从产物到 PDF 的每一步都忠实；
- 是**方法学错误**，发生在**实验设计层**。

**而我们的门控根本不在那一层。** 它验证的是
`claim 值 == 产物里的值`，它无法回答"这个产物是怎么算出来的"。

> **provenance ≠ validity。可追溯不等于正确。**
>
> 一个人可以完全诚实地记录一个错误的实验，然后诚实地把它写进论文。
> 我们的系统把这条路径上的每一环都加固了，唯独没有加固实验设计本身。

**这是论文标题里"Evidence-Gated"这个词的真实边界**，而我们之前从没写出来过。
外部评审用 9 分钟就找到了它。

**还有一层**：论文里那句 `improvement_pct` 是通过门控的——
也就是说，**门控不仅没拦住它，还给它盖了章**。
在"可追溯 = 可信"的叙事下，一个方法学错误的数字反而因为"有证据"而显得更权威。
这比"编造数字"更隐蔽，也更危险。

---

## 4. 一句话总结这次外部评测

| 问题 | 答案 |
|---|---|
| 分数 | **2.4 / 10**（1/7 维正向） |
| 距离及格 | **差 1.81 分** |
| 最严重的问题 | 用测试集选超参 —— **属实，已定位到 `lab.py:157-160, 175`** |
| 最意外的发现 | 门控体系全绿，却拦住不了一个方法学错误的数字 |
| 最扎心的发现 | 核心创新点（证据台账 + 门控）**已被 ResearchLoop / EviGraph / Paper Pilot 做过，且做得更多** |
| 唯一被认可的 | 研究问题本身重要 |

**这次评测的价值不在分数，在于它暴露了两件我们自己没看见的事**：
门控的证明力边界在哪，以及我们以为的原创性其实是一个已有密集文献的方向。

---

## 5. 如果要迭代（未做，仅列方案）

按"性价比"排序，前两条能立刻做：

| # | 动作 | 成本 | 预期收益 |
|---|---|---|---|
| 1 | **改用验证集/CV 选 α**，只评一次测试集 | 极低（纯 numpy，~1 秒重跑） | 修掉被点名的硬伤；`+50.94%` 大概率回落但仍显著 |
| 2 | **把已有的门控数据写进论文**：`gate_reject` 记录、篡改检测、96 条离线断言、claim-证据覆盖率 | 低（数据全在 `runs/` 里） | 直接回应"论文没描述系统"和"实验不测门控" |
| 3 | **补相关工作**：ResearchLoop / EviGraph / Paper Pilot 等 | 中 | 回应 `Prior_Work_Context = -1` |
| 4 | 重写 method 章节，给出门控谓词与状态表征的形式化 | 中 | 回应 Q1–Q4 |
| 5 | 加消融：No-gate vs Gate 对"编造率/claim 支持率"的影响 | 中 | 回应"没有消融" |
| 6 | 换前沿模型写正文，解决措辞重复 | 高（成本） | 回应 `Writing_Clarity = -1` |
| 7 | 在正文/附录给出仓库与产物清单 | 极低 | 回应"没有发布产物" |

**注意第 1 条**：修完之后 `+50.94%` 这个数字**很可能变小**。这必须照实报告 ——
按项目铁律，负面结果不粉饰，也不因为分数不好看就换种子重跑。

---

## 6. 明确不做的事

- **不重新提交同一篇论文。** 官方明确要求不要因未收到邮件而重复提交。
- **不据单一分数下结论。** 官方自述：AI 与单个人类评审 Spearman 相关仅 0.42，
  预测录取 AUC 0.75（低于人类的 0.84），且"AI 领域更准"。**2.4 是一个弱信号。**
- **不假装这分数与本次修正无关。** 提交的是 `runs/paper-20260929-164239` 那一版的 PDF
  （sha256 `f3813537...`），就是最后一次结构修复后的交付版。
