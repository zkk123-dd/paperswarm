# 交付清单：你要的东西 → 对应产出 → 验证状态

> 一页看完"要了什么、给了什么、**验到什么程度**"。
> 状态只有三种：✅ 已交付并实测 ／ ⏳ 已交付待外部执行 ／ ❌ 未完成（并写明缺什么）。

---

## 1. 总览

| # | 你要的 | 产出 | 状态 |
|---|---|---|---|
| 1 | 英文 ICLR 模板论文 **PDF** | `output/PaperSwarm-ICLR2026.pdf`（4 页 / 34,729 字节 / tectonic 编译 / **6 个一级章节标题齐全**） | ✅ 实测通过 |
| 2 | 论文里的数字**真实可追溯** | `ledger/claims.jsonl`（11 条 claim ↔ `exp/metrics.json` SHA-256），**11/11 原样出现在 PDF 中** | ✅ 实测通过 |
| 3 | 论文**结构**正确 | 组装器独占一级标题 + `assert_single_section` 硬断言；书签 6 条一级、0 条越界子标题 | ✅ 实测通过（见 `bugfix-section-headings.md`） |
| 4 | openJiuwen **Agent 源码**（含注释与工程说明） | 内核 9 文件（3,329 行）+ provider 1 文件（683 行）+ 测试（399 行） | ✅ 已交付 |
| 5 | **Swarm Skill** 五文件包 | `paper-repro-swarm/`（10 文件：SKILL + 5 roles + workflow + bind + deps + scripts） | ✅ 校验器 PASS 0/0 |
| 6 | **Swarmflow** workflow | `paper-repro-swarm/scripts/workflow.py`（4 phase / 9 次 agent 调用） | ✅ 校验器 + dry-run 16/16 |
| 7 | **接入 JiuwenSwarm 源码** | 4 个 `swarm.paper_*` harness 元素（2 modified + 21 new） | ✅ 静态+替身通过；⏳ 上游 CI 未跑 |
| 8 | 运行配置 / 环境依赖 / **复现指南** | `docs/reproduction-guide.md` + `configs/model.yaml` | ✅ 命令均实测 |
| 9 | **技术文档**（架构 / 模块调用 / 创新点） | `docs/architecture-and-innovation.md` | ✅ 已交付 |
| 10 | **资源报告**（token / 时长，可追溯） | `docs/token-cost-report.md` + `reports/token_report.{json,md}` | ✅ 实测 |
| 11 | **框架贡献说明** | `docs/upstream-contribution.md`（改动清单 + 回归证据 + PR 计划） | ✅ 已交付 |
| 12 | **PR 链接** | — | ❌ 缺 GitCode 账号与写权限 |
| 13 | Stanford Reviewer **access token** | **`3xjzh0ZcSPc2uhS7lDhDH_r0r5Td3Dl-FiWgSMLAB90`**（venue=ICLR，`reports/review_submission.json`） | ✅ **已提交并拿到 token** |
| 14 | Stanford Reviewer **评分** | **2.4 / 10**（7 维中仅 1 维正向）；`reports/review_result.{json,md}` | ✅ **已出分**（未达及格线 4.21） |
| 15 | 差距分析与迭代方案 | `docs/review-result-and-gap-analysis.md` | ✅ 已交付（含一条已核实的方法学真缺陷） |

---

## 2. 关键实测数字（一眼核对）

> 数字取自**最终交付版** `runs/paper-20260929-164239/`（不是修复前的历史 run）。

| 指标 | 数值 | 出处 |
|---|---|---|
| 论文 PDF | **4 页 / 34,729 字节**（ICLR 正文页限 9；评测页限 15；体限 10 MB） | `runs/paper-20260929-164239/assembly_report.json` |
| PDF 结构 | 书签 **6 条一级章节**，越界子标题 **0 个** | 用 PyMuPDF `get_toc()` 核对 |
| 实验（真实 numpy） | OLS MSE **0.6198±0.1329** ／ Ridge **0.3041±0.0623** ／ 改进 **50.94%** ／ 噪声下限 0.1225 ⚠️ **该改进值受测试集选参偏差影响，见下行** | `exp/metrics.json` |
| ⚠️ **实验方法学缺陷** | α=0.1 是**用测试集 MSE 选出**的（`lab.py:157-160, 175`）→ `+50.94%` 被抬高 | 外部评审指出并经代码核实，`docs/review-result-and-gap-analysis.md` §2.1 |
| 论文正文手写数字 | **0 个**（全部走 `\result{claim_id}` 回填） | `output/PaperSwarm-ICLR2026.pdf` 文本抽取 |
| claim → PDF 溯源 | **11/11 原样出现** | 同上 |
| 台账校验 | **11/11 passed, 0 failed** | `run_summary.json` |
| 模型调用 | **7 次，成功率 100%，重试 0，门控拒绝 0**（本章一次过） | `runs/paper-20260929-164239/run_summary.json` |
| Token | **8,848**（prompt 6,452 / completion 2,396） | 同上 |
| 时延 | P50 **11.2 s** / P95 **15.6 s**；LLM 累计 74.5 s；实验仅 0.28 s | 同上 |
| 成本 | **$0.00**（`cost_basis=priced`，本机推理） | 同上 |
| 跨运行累计 | **12 个 run / 61 次调用 / 119,921 tokens** | `reports/token_report.md` |
| 离线回归 | **105/105**（台账 9 + 内核 96） | `reports/verification_summary.txt` |
| vendor 内核校验 | **14/14** | `reports/vendor_verify.json` |
| 替身契约测试 | **17 passed** | `reports/verification_summary.txt` |
| Swarmflow dry-run | **16/16** | `reports/swarmflow_dryrun.json` |
| Swarm Skill 校验 | **PASS 0 warning 0 error** | `reports/swarmskill_validate.txt` |
| 上游改动 | **2 modified + 21 new**，无声明外内容漂移 | `reports/upstream_audit.txt` |
| 提交前自检 | **8/8 PASS** | `reports/review_readiness.md` |
| **外部评测综合分** | **2.4 / 10**（venue=ICLR，7 维中 **1 维**正向）❌ 未达及格线 4.21 | `reports/review_result.{json,md}` |
| 全量编译 + 回归（一次性汇总） | 全通过，全部套件绿 | `reports/verification_summary.txt` |

---

## 3. 代码规模（无占位符、无 TODO）

> 计数方式：`find <dir> -name '*.py' -not -path '*__pycache__*' | xargs wc -l`，
> 含空行与注释。**不要**用 PowerShell 的 `Measure-Object -Line` 对照 —— 它不计空行，
> 会系统性少算（本表早前版本即因此低估约 30%）。

| 位置 | 文件 | 行数 |
|---|---|---|
| `paper-swarm/src/paperswarm/` | 9 | 3,969 |
| `paper-swarm/scripts/` | 11 | 2,767 |
| `paper-swarm/tests/` | 2 | 1,126 |
| `paper-swarm/integration/` | 1 | 898 |
| `jiuwenswarm/agents/paper/`（新增） | 9 | 3,886 |
| `jiuwenswarm/.../providers/paper_providers.py`（新增） | 1 | 867 |
| `jiuwenswarm/.../skills/paper-repro-swarm/`（新增） | 10 | 1,394 |
| `tests/agents/swarm/test_paper_providers.py`（新增） | 1 | 500 |

---

## 4. 文档索引

| 文档 | 读它能知道什么 |
|---|---|
| `reproduction-guide.md` | 环境 / 依赖版本 / 三条复现路径 / **11 条验收点核对表** / 故障排查 |
| `architecture-and-innovation.md` | C3 判断、分层架构、Swarmflow 四段、门控闭环、**4 条创新点**、与 FARS 对照、取舍 |
| `token-cost-report.md` | 逐运行 token/时延/成本、与 FARS 的量级对比（含三条免责）、口径说明 |
| `upstream-contribution.md` | 改动清单（带 diff 说明）、**为什么必须条件追加**、回归证据、双 PR 计划与步骤 |
| `upstream-ci-runbook.md` | 上游 CI 怎么跑这 18 个用例、本机为什么跑不了、期望输出、排障表 |
| `review-result-and-gap-analysis.md` | **外部评测 2.4 分**的逐条核实、已定位的方法学真缺陷、门控证明力边界、迭代方案 |
| `agentic-reviewer-eval.md` | **更正记录**（曾误判"无官方 API"）、官方 API 端点全清单、7 维评分机制、目标档位、本次提交留痕 |
| `bugfix-section-headings.md` | **章节标题全部丢失**的缺陷：根因（只写在注释里的分工）、修复、三层验证、4 条教训 |
| `env-recon.md` | WSL 被策略硬阻断的实测证据、Ollama 能力实测、磁盘/内存/显存水位 |
| `fars-benchmark.md` | FARS 可核实事实卡 + 5 处设计修正 + 差异化定位 |
| `source-changeset-verified.md` | 源码改造清单（含**三处推翻原方案**的发现） |
| `phase0-1-design.md` | Phase 0 需求锚定 + Phase 1 架构设计（初稿） |
| `phase2-vertical-slice-report.md` | 竖切片全过程与**6 个已修缺陷**（含 1 个真安全漏洞） |

---

## 5. 未完成项（逐条写明缺什么）

| # | 未完成 | 缺什么 | 补齐方式 |
|---|---|---|---|
| 1 | 上游 CI 执行 18 个用例 | 真实 `openjiuwen` 依赖树（本机未装） | `pip install -e ".[test]"` 后跑 `pytest tests/agents/swarm/test_paper_providers.py` |
| 2 | PR 链接 | GitCode 账号 + 仓库写权限 | 按 `upstream-contribution.md` §6.2 执行 |
| 3 | **论文未达 ICLR 水准**（外部评审 2.4/10） | ① 实验用测试集选超参 ② 论文没写系统与门控 ③ 没引已有的同类工作 ④ 措辞重复 | 7 条迭代方案见 `review-result-and-gap-analysis.md` §5（前 2 条成本极低） |
| 4 | 对抗审稿闭环（`adversary`） | 未实现 | 设计见 `phase0-1-design.md` §2.3 |
| 5 | 复现实验的执行后端（Linux 沙箱） | WSL 被本机安全策略硬阻断 | 选项与代价见 `env-recon.md` §3 |
| 6 | 论文正文质量 | 本机 7B 模型能力不足（实测观察 + **外部评审确认**） | 换前沿模型对比实测 |
| 7 | 大算力/大规模实验 | 单机算力 | 明确排除（FARS 自承处理不了这类任务） |

---

## 6. 一句话总结

**能本地验的全部验了，且有实测数字；验不了的逐条写明缺什么、怎么补——没有把没做的说成做了。**

外部评测这一环也没打折：**2.4 分照实写，比及格线差 1.81 分**，
评审点名的硬伤逐条拿代码核实（其中一条是真缺陷，已定位到 `lab.py` 具体行）。
