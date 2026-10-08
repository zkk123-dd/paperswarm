# 实验选择协议的修正（v1 → v2）与归因

> 触发：Stanford Agentic Reviewer 在 2026-09-29 的评测里点名
> "Hyperparameter selection appears to have used test MSE ... methodologically
> invalid and inflates reported performance"（见 `review-result-and-gap-analysis.md` §2.1①）。
> 本文记录修正内容、修正后的数字、以及修正带来的差值**具体来自哪一步**。
>
> 产物：`src/paperswarm/lab.py`（v2）、`scripts/experiment_report.py`、
> 运行记录 `reports/lab_v2_report.txt`、`runs/_lab_check/metrics.json`。

---

## 1. 缺陷是什么

v1 的代码（`lab.py` 旧版）在**测试集**上遍历惩罚系数并按测试集 MSE 取最小值：

```python
for alpha in alphas:
    ridge_pred = fit_ridge(x_train, y_train, x_test, float(alpha))
    ridge_score = mse(ridge_pred, y_test)      # ← 用测试集评估
    alpha_scores[float(alpha)].append(ridge_score)
best = min(sweep, key=lambda item: item["mse_mean"])   # ← 用测试集 argmin
```

然后 `improvement_pct` 直接由这个"被测试集挑出来的 α"的测试集 MSE 算出。
这是选择偏差：报告的性能被同一个数据集同时用于**选择**和**评估**，
系统性偏乐观。

**评审的判断是对的，不是误报。**

---

## 2. 修正了什么

| # | 修正 | 内容 |
|---|---|---|
| 1 | **选择协议** | 改为在**训练集内部**做 5 折交叉验证选参（`cross_validate()`），测试集**只评一次**。选择集与报告集不相交。 |
| 2 | **截距** | v1 所有估计量都不带截距（`center=False`）。v2 默认先用训练集均值中心化特征与目标（等价于带截距），预测时加回。 |
| 3 | **基线补齐** | 评审点名缺 LASSO / 弹性网与正确选参流程 → 新增 LASSO、弹性网、PCR、零模型四个基线，全部用同一套 CV 协议选参。 |
| 4 | **网格去手调** | v1 的 α 网格 `(0.01…100)` 是手工定的。v2 的岭回归网格跨 7 个数量级（`1e-4…1e3`，15 点）；弹性网网格由数据自适应给出（`λ_max = max_j |x_j·y|`，对数取 8 点）。 |
| 5 | **噪声下界给出推导** | 评审要求解释 0.1225 怎么来的 → `noise_floor_derivation()` 给出分解式推导（不再是写死的常数）。 |
| 6 | **数据生成过程写全** | `make_dataset()` 的五步构造（隐因子数、载荷分布、特征噪声 0.05、系数分布、目标噪声 0.35）在 docstring 里写全，论文可直接引用。 |

### 2.1 一个必须保留的回归检查

`experiment_report.py` 启动后**第一件事**是复现 v1 的数字，不一致就报错退出：

```
[OK ] OLS mean (v1, 无截距)          got=0.619796117040762  want=0.6198
[OK ] Ridge mean (v1, 测试集选参)     got=0.30409954577864745 want=0.3041
[OK ] improvement (v1)              got=50.94              want=50.94
[OK ] selected alpha (v1)           got=0.1                want=0.1
=> v1 复现一致
```

**为什么必须做这件事**：v1 的代码路径要在同一次运行里保留，否则"修正带来的
差值"就无从归因 —— 我们会得到一个新数字，然后只能猜它为什么变了。
这条检查同时保证了重构没有意外改变历史协议。

---

## 3. 修正后的数字

8 个种子，`n_train=90 / n_test=200 / n_features=120 / n_informative=6`，
5 折交叉验证折划分种子 `20260929`。

| 方法 | 选参 | 测试 MSE（mean ± std） | R² | 相对 OLS 降幅 |
|---|---|---|---|---|
| 零模型（预测训练集均值） | — | 843.0842 ± 365.9549 | 0.000 | — |
| OLS 最小范数解 | 无超参 | 0.6176 ± 0.1293 | 0.999 | 参照 |
| **Ridge** | **α 由 5 折 CV 选出（α=0.1）** | **0.3132 ± 0.0656** | **1.000** | **49.29%** |
| LASSO | λ 由 CV（λ≈0.5896） | 0.5365 ± 0.0799 | 0.999 | 13.13% |
| Elastic Net | (λ, l1_ratio) 由 CV（λ≈0.0952, l1=0.5） | 0.4657 ± 0.0586 | 0.999 | 24.59% |
| PCR | 主成分数由 CV（k=20） | 0.4287 ± 0.0601 | 0.999 | 30.58% |
| ~~Ridge（α 由测试集选）~~ | **v1 协议，无效** | 0.3041 ± 0.0623 | 1.000 | 50.76% |
| ~~Ridge（oracle α）~~ | **偷看测试集，不是方法** | 0.3073 ± 0.0656 | 1.000 | 50.24% |

**结论：修正后 ridge 相对 OLS 的降幅是 49.29%，不是 50.94%。**

补充口径：

- 不可约噪声下界 σ² = 0.1225。**OLS 是下界的 5.04 倍，ridge 是 2.56 倍** ——
  这比"改进了 50%"更能说明离最优还有多远。
- CV 选参的结果距离 oracle（测试集 argmin）只差 **1.90%**，
  说明选参协议本身是有效的，问题只在 v1 用错了集合。

---

## 4. 差值的归因（+50.94% → +49.29%，共 1.65 个百分点）

一次只改一个变量：

| 档 | 选择协议 | 截距 | 测试 MSE | improvement |
|---|---|---|---|---|
| **A（v1 原样）** | 测试集选 α | 无 | 0.3041 | **50.94%** |
| **B** | 测试集选 α | 有 | 0.3073 | **50.24%** |
| **C（v2）** | 5 折 CV 选 α | 有 | 0.3132 | **49.29%** |

- **加截距贡献 +0.70 个百分点**（A → B）
- **改用正确的选参协议贡献 +0.95 个百分点**（B → C）

### 4.1 必须如实说的一件事

本例中 **CV 选出的 α 与测试集 argmin 恰好都是 0.1**。所以"选参集合用错"
这一次的实测代价只有 0.95 个百分点。

这不构成对 v1 协议的辩护：

1. 协议无效是协议无效 —— 它**允许**选择偏差进入报告数字，
   这一次没被放大不等于下次不会；
2. 五个候选 α 上，测试集 argmin 与"最好情况的偏差"之间没有任何机制约束；
3. 换个数据生成过程（更小的 n_train、更宽的网格）偏差会立刻变大。

**照实报告负面结果**（本项目铁律）：修正后数字变小，不粉饰，也不换种子重跑。

---

## 5. 这次修正**没有**解决的问题

必须写清楚边界，否则会重复"门控全绿但结论错"的覆辙：

- **唯一实验仍然不检验系统本身。** 岭回归 vs OLS 的对照既不触发证据门控、
  也不涉及多智能体。评审的这一条批评**依然成立** —— 修好选参协议只是
  让这个实验本身变严谨了，没有让它变得**相关**。
- `Claims_Support`、`Originality`、`Prior_Work_Context`、`Value_to_Community`
  四维不受本次修正影响。
- 论文正文里的数字（`\result{cl-improve}` 等）**目前仍是 v1 的 50.94%**。
  重跑论文、更新台账、重编译 PDF 是下一步；在那之前，
  仓库里存在"论文写 50.94、实验模块算 49.29"的不一致，
  这是一个**待消除的已知缺口**，不是被忽略的。

---

## 6. 复现命令

```bash
export PATH="/usr/bin:/bin:$PATH"
export PYTHONPATH="E:/存放/agent/paper-swarm/src"
PY="C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 实验 + 报告（约 45 秒，纯 numpy，不调模型、不联网）
$PY scripts/experiment_report.py --out runs/_lab_check/metrics.json

# 只跑关键结论，不打印曲线
$PY scripts/experiment_report.py --quiet

# 写作重复度审计（把"重复"这条主观批评变成数）
$PY scripts/repetition_audit.py runs/paper-20260929-164239/paper/drafts \
    --json reports/repetition_audit_v1.json
```

---

## 7. 附：同批产出的第二个测量工具

`scripts/repetition_audit.py` —— 评审的另一条主观批评
（"Sections 2–5 restate the same ridge-vs-OLS numbers with minimal new insight"）
现在有了可测量的定义：跨章节 4-gram 包含率、同一 claim 的跨章节出现次数、
章节 type-token ratio。

对**已提交的那一版**跑，结果（`reports/repetition_audit_v1.txt`）：

| 指标 | 值 |
|---|---|
| 章节数 | 7 |
| `\result{}` 出现总次数 | 70 |
| 不同 claim 数 | **11** |
| 重复引用的多余次数 | **59** |
| 每个 claim 出现的章节数 | **5–7 / 7** |

11 个数字被讲了 70 遍，平均每个数字讲 6.4 遍 —— 评审说的"restate"
不是主观印象，是 59 次冗余。同一套阈值可以直接当门控用
（脚本退出码 0/1），这样"下次不许再重复"就是一条可验证的约束，
而不是提示词里的一句请求。
