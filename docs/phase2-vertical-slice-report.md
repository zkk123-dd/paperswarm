# Phase 2 竖切片验证报告

> 全部数据来自 2026-09-29 真实运行，不是预估。运行目录：`runs/slice-20260929-065429/`。
> 本机模型 `qwen2.5:7b-instruct-q4_K_M`，边际成本 0 元。

---

## 1. 结论

**竖切片 PASS，且反伪造链在真实运行上被独立验证通过。**

一句话概括验证结果：一个由 numpy 算出的真实数字，被强制地从实验产物经 SHA-256 绑定、
Agent 工具调用、LaTeX 宏引用，一路送进可编译正文，**中途无法被编造**——篡改产物后
5 条 claim 全部被拦截、编译门控拒绝产出文件。

同时，本轮迭代暴露并修掉了 6 个真实缺陷（见 §5），其中 3 个是**设计层面的**，
不是笔误。

---

## 2. 端到端链路（实际执行）

```
run_experiment          真跑 numpy 对照实验，5 个随机种子 -> exp/metrics.json
   |
   v  SHA-256 固化
Ledger.register_artifact  artifact_id = a-7d185ebf
   |                      sha256 = 4f2b96ead9f19d93…
   v
AgentLoop（真实 LLM，有界）  4 步 / 6 次工具调用 / 13,401 tokens
   |   register_artifact -> add_claims(批量) -> write_latex_section
   v
write_latex_section 门控  0 个手写数字，6 处 \result{claim_id}
   |
   v
resolve 回填              6 个宏全部注入真实值 -> paper/results.resolved.tex
   |
   v
台账复核                  5 claims / 5 passed / 0 failed
```

### 2.1 实验本身是真的

```
OLS   MSE = 0.5865 ± 0.1595
Ridge MSE = 0.1502 ± 0.0200
相对改进 = 74.39%
```

实验设置在**近共线**区间（60 个特征由 3 个隐因子线性生成），这是 L2 正则化收益
最明确的经典场景。噪声下限 σ² = 0.1225，岭回归的 0.1502 已接近最优——
说明效应是数据生成过程自带的统计性质，不是调参凑出来的。

> 这一点踩过两次坑，值得记录：第一版参数取 `n_features < n_train` 的良性区间，
> 结果**岭回归反而差 1.46%**（良性区间里 OLS 本来就一致，正则化只引入偏差）；
> 第二版换成纯欠定区间，两个方法误差都被"截断掉一半信号"主导，差异仅 0.10%。
> **最终结论：实验设计必须让待验证的效应真实且主导。**

### 2.2 正文里零个手写数字

源文件（写入前，模型产出）：

```latex
In this experiment, we compare the performance of Ordinary Least Squares (OLS) and
Ridge regression on a dataset. The mean test Mean Squared Error (MSE) of OLS is
\result{c-a756c128}, with a standard deviation of \result{c-b218c646}. For Ridge
regression, the mean test MSE is \result{c-58ff86d5} ...
```

回填后（由台账注入真实值）：

```latex
... The mean test MSE of OLS is 0.586546, with a standard deviation of 0.159457.
For Ridge regression, the mean test MSE is 0.15021, and its standard deviation is
0.0200157. The relative improvement percentage is 74.3908.
```

台账记录（`ledger/claims.jsonl`）把每个数字钉到产物 + JSON Pointer：

| claim_id | 值 | 来源 locator |
|---|---|---|
| `c-a756c128` | 0.586546 | `a-7d185ebf` `/results/mse_ols_mean` |
| `c-b218c646` | 0.159457 | `a-7d185ebf` `/results/mse_ols_std` |
| `c-58ff86d5` | 0.15021 | `a-7d185ebf` `/results/mse_ridge_mean` |
| `c-29388638` | 0.0200157 | `a-7d185ebf` `/results/mse_ridge_std` |
| `c-75acf32c` | 74.3908 | `a-7d185ebf` `/results/relative_improvement_pct` |

---

## 3. 反伪造链的独立验证（这是最该被看的一条）

### 3.1 测试方法

在**真实运行**的产物文件上做篡改，而不是在单测的构造数据上：

```bash
# 备份 -> 把岭回归 MSE 从 0.1502 改成 0.0001（伪造一个漂亮结果） -> 验证 -> 还原
```

### 3.2 结果

| 检查 | 期望 | 实测 |
|---|---|---|
| 篡改前台账校验 | 通过 | `passed 5 / failed 0`，退出码 0 |
| 篡改后台账校验 | 拦下 | **`passed 0 / failed 5`，退出码 1** |
| 拦截原因 | 摘要不匹配 | 「artifact 内容已被修改（摘要不匹配，疑似篡改实验结果）」 |
| 篡改后编译门控 | 拒绝产出 | **退出码 1，输出文件未创建** |
| 还原后台账校验 | 恢复通过 | 退出码 0 |

### 3.3 一处必须说清的边界

台账里记录的是产物的**绝对路径**。所以**拷贝运行目录不会产生独立的证据集**——
副本运行的台账仍然校验原件。这是正确行为（证据应当钉在它实际所在的位置），
但意味着"把 run 目录复制一份再改副本"是无效的篡改方式，我第一轮就是这么误测的，
看到了假的"通过"。记录在此，避免后续复现者重踩。

---

## 4. 资源台账（真实数据，可追溯）

来自 `runs/slice-20260929-065429/run_summary.json`：

| 指标 | 数值 |
|---|---|
| LLM 调用次数 | 4（全部成功，成功率 100%） |
| 工具调用次数 | 6 |
| 输入 tokens | 11,648 |
| 输出 tokens | 1,753 |
| **总 tokens** | **13,401** |
| 时延 P50 / P95 | 13,782 ms / 15,059 ms |
| LLM 累计时延 | 49,735 ms |
| 实验计算耗时 | 0.0175 s |
| Agent 步数 | 4（stop_reason = `final_answer`） |
| 重试次数 | 0 |
| 成本 | **$0.00**（本机推理，`cost_basis = "priced"` 而非 `"unpriced"`） |

三条落盘轨迹，全部带 `run_id` 与时间戳，可逐条回溯：
`llm_calls.jsonl`（每次调用的 token/时延/重试明细）、`events.jsonl`（阶段事件）、
`run_summary.json`（汇总）。

> 成本口径特别说明：遥测层**不内置任何模型价格表**。价格是随时会变的外部事实，
> 硬编进代码等于埋一个会静默失真的数据源。只有调用方显式给出单价才计算成本，
> 否则标 `unpriced`。本机 Ollama 的 0 元是**显式已知**，所以记 `0.0`。

---

## 5. 迭代中发现并修掉的缺陷

这轮不是一次跑通的。**6 次运行、4 次失败**，每次失败都定位到一个真实缺陷。

| # | 现象 | 根因 | 修法 | 层级 |
|---|---|---|---|---|
| 1 | Agent 用文件名 `metrics.json` 当 artifact_id，5 条 claim 全失败后**直接放弃** | 工具报错只有"未登记的 artifact"一句话，模型无法据此修正 | 报错列出可用 id；支持按路径/basename 唯一解析；`register_artifact` 返回值显式给出 `use_this_artifact_id` | 接口设计 |
| 2 | `write_latex_section` 被门控拒绝，轨迹却记成 `ok` | 拒绝时返回 `{"written": false}`，被当作正常返回 | 改为抛 `GateRejected`——**什么都没写成，就不能显示成功** | 可观测性 |
| 3 | 正文一个 `\result{}` 都不写、数字全靠手打，门控只给 warning 就放行 | `gate_text` 只在已有引用时才校验 | 新增 `gate_strict`：要求至少 1 个引用 + 结果语境手写数字拦截，写入路径强制启用 | **安全漏洞** |
| 4 | 13 步全耗在逐条登记 claim，走不到写正文 | 单条登记 = 一轮往返，小模型步数被吃光 | 加批量 `add_claims` + 幂等复用；工具集从 7 个精简到 4 个 | **架构** |
| 5 | 模型"少做一步就宣布做完"（步骤 10 直接给最终答复） | 靠 prompt 写"你必须调用 X"是不可验证的约束 | 循环加**第 5 条上界** `required_tools`：必需工具未成功执行过就不许收尾，提醒有上限 | **架构** |
| 6 | 同一产物被脚本和 Agent 各登记一次，台账出现重复 artifact | `register_artifact` 不按 路径+摘要 去重 | 幂等：同路径同摘要直接返回已有记录 | 数据一致性 |

第 3、4、5 条是设计层面的，值得单独说：

- **第 3 条是真正的安全漏洞**。原设计认为"强制 `\result{}` 引用"就够了，但漏掉了
  "一个引用都不写"这条绕过通道——只要不引用，门控就无从校验。这与我在 Phase 0/1
  文档里的论证是同一个逻辑：**没有验证方式的约束等于没有约束**，而这次是约束本身
  留了个洞。
- **第 4、5 条指向同一个工程结论**：把"模型应该做什么"交给 prompt，不如交给代码。
  批量接口减少往返次数，`required_tools` 把完成条件变成可判定的状态。两者都不依赖
  模型自觉。

---

## 6. 测试基线

| 测试 | 条数 | 结果 | 覆盖 |
|---|---|---|---|
| `tests/test_evidence.py` | 9 | **9/9 PASS** | 台账登记/校验/篡改检测/宏重定义拦截 |
| `tests/test_kernel_offline.py` | 80 | **80/80 PASS** | 配置校验、重试语义（5 类）、遥测汇总、**循环五重上界**、危险工具 fail-closed、工具异常回灌、批量登记、幂等、**门控 5 类拒绝**、路径越权、篡改后拒绝写入 |
| 端到端竖切片 | 1 | **PASS** | 真实实验 → 证据 → Agent → 门控 → 回填 |

**离线测试全部不依赖网络与模型**，因此可在任何机器上作为回归基线——
这是 Phase 5 评测集的第一层。

代码规模：`src/` + `scripts/` + `tests/` 共 **4,456 行**（无占位符、无 TODO）。

---

## 7. 边界与未完成项（不掩饰）

1. **没有 PDF**。本机无任何 LaTeX 引擎（pdflatex/xelatex/tectonic 全缺），
   无法编译 ICLR 模板。这一步是硬缺口，见 `docs/env-recon.md` §4.2。
2. **没有 Linux 沙箱**。WSL 被安全策略硬阻断，`jiuwenbox` 用不了。
   复现实验的执行后端需要另择（见 `docs/env-recon.md` §2、§3）。
3. **本切片只覆盖"结果段"一个环节**，FARS 对标的四大模块中只落地了 Writing 的一小部分。
   Planning / Experiment / 对抗审稿 / 全文组装都还没做。
4. **7B 模型能跑通链路，但不等于能写论文**。本切片的正文质量明显低于 ICLR 水平
   （措辞重复、缺少定量对比分析）。这是**实测观察**，不是推断；"7B 与强模型的差距
   有多大"仍是待实测项。
5. **耗时 49.7 s 里 49.7 s 全是 LLM 推理**（实验只占 0.0175 s）。本地 30 tok/s 的吞吐
   是当前瓶颈，全文写作会显著更慢。批量接口与精简工具集对总时延的改善需要单独实测。
6. **`required_tools` 只在模型"试图收尾"时触发**；如果它被 max_steps 先截断，
   仍会以 `max_steps` 结束（本轮实测就遇到过）。这是刻意的——上界之间不互相掩盖失败原因。

---

## 8. 下一步

| 优先级 | 动作 | 依赖 |
|---|---|---|
| 1 | 装 LaTeX 引擎（首选 tectonic），打通 ICLR 模板编译 | 无需你介入，但需要网络 |
| 2 | 定复现实验执行后端（GitHub Actions / 远程 Linux / WSL） | **需要你决策** |
| 3 | 把 4 个 `swarm.paper_*` 元素按 §13 六步接入 JiuwenSwarm | clone 仓库（需网络） |
| 4 | 写 Swarm Skill 5 文件包 + Swarmflow `workflow.py` | 需先定角色边界 |
| 5 | 强模型对比实测，量化 7B 与前沿模型的差距 | **需要模型凭证** |
