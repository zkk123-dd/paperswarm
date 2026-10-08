# PaperSwarm

面向 ICLR 模板英文论文的**可验证**自动生成系统。基于 JiuwenSwarm（openJiuwen）扩展。

**公开工件仓库：<https://github.com/zkk123-dd/paperswarm>**
产物清单与「论文数字 → 文件」映射见 [`ARTIFACTS.md`](ARTIFACTS.md)。

核心主张：**论文里的每个数字都必须能追溯到一份内容未被篡改的实验产物。**

不是靠 prompt 劝模型"不要编造"（那是不可验证的约束），而是让数字**根本没有手写入口**——
正文只能写 `\result{claim_id}`，编译前由证据台账回填真实值。数字被事后改写，
SHA-256 校验会拦下。

---

## 当前状态（2026-09-29 实测）

| 项 | 状态 |
|---|---|
| 端到端产出 ICLR 模板 PDF | ✅ **4 页 / 34 KB**（`output/PaperSwarm-ICLR2026.pdf`） |
| 论文结构正确（标题/章节） | ✅ 书签 **6 条一级章节**，越界子标题 **0 个**（见 `docs/bugfix-section-headings.md`） |
| 论文数字全部来自台账 | ✅ 正文 **0 个手写数字**，11 条 claim 全部 SHA-256 绑定，**11/11 原样出现在 PDF** |
| 门控在端到端环境里生效 | ✅ 模型编造 claim id 被真实拦下并重写通过 |
| 接入 JiuwenSwarm 源码 | ✅ 4 个 `swarm.paper_*` 元素（2 modified + 21 new） |
| Swarm Skill 五文件包 | ✅ 官方校验器 `PASS 0 warning 0 error` |
| Swarmflow workflow | ✅ 静态校验 + 替身 dry-run **16/16** |
| 离线回归 | ✅ **105/105**（台账 9 + 内核 96） |
| 单篇成本 | **$0.00 / 8,848 tokens**（本机 Ollama，零边际成本） |
| 上游 CI | ⏳ 待在上游执行（本机无 `openjiuwen`） |
| 外部评测 | ✅ **已提交并出分** paperreview.ai（venue=ICLR）—— **2.4 / 10**，未达及格线 4.21 |
| ⚠️ 已定位的方法学缺陷 | α 用**测试集**选出（`lab.py:157-160,175`），`+50.94%` 被抬高 —— 外部评审指出、代码核实 |

---

## 快速开始

```bash
# 1. 环境（Git Bash 需要显式 PATH；跑 -m 需要 PYTHONPATH）
export PATH="/usr/bin:/bin:$PATH"
export PYTHONPATH="E:/存放/agent/paper-swarm/src"
PY="C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 2. 离线回归（不依赖网络、模型、LaTeX，任何机器可跑）
"$PY" tests/test_evidence.py              # 9/9    台账 + 篡改检测
"$PY" tests/test_kernel_offline.py        # 96/96  重试/循环上界/门控拒绝/幂等/章节结构
"$PY" scripts/verify_vendored_kernel.py   # 14/14  vendor 内核
"$PY" -m pytest integration/test_paper_providers_contract.py -q   # 17 passed
"$PY" scripts/verify_swarmflow_dryrun.py  # 16/16  Swarmflow 替身执行

# 3. 只跑实验 + 台账 + 组装 + 编译，不调 LLM
"$PY" scripts/full_paper.py --skip-llm-check

# 4. 端到端产出 PDF（需本机 Ollama 在跑）
"$PY" scripts/full_paper.py

# 5. 用已有 run 的草稿+台账重新组装编译（排版迭代不重复付费调模型）
"$PY" scripts/rebuild_from_run.py --run runs/paper-20260929-164239

# 6. 资源报告 + 提交前自检
"$PY" scripts/token_report.py
"$PY" scripts/review_readiness.py
```

模型配置见 `configs/model.yaml`，现有 profile：

| profile | 端点 | 模型 | 用途 |
|---|---|---|---|
| `local-ollama` | `http://127.0.0.1:11434/v1` | `qwen2.5:7b-instruct-q4_K_M` | 零边际成本、离线回归 |
| `cloud-deepseek` | `https://api.deepseek.com/v1` | `deepseek-v4-pro` | 正文写作（语言质量） |

```bash
"$PY" scripts/full_paper.py --profile cloud-deepseek   # 云端出全文
```

密钥只从环境变量读：`PAPER_LLM_BASE_URL` / `PAPER_LLM_MODEL` / `PAPER_LLM_API_KEY`。
也可以写进 `.env.local`（已被 `.gitignore` 排除，入口脚本自动加载；已存在的环境变量优先）：

```bash
cp .env.example .env.local    # 然后填真实 key
```

**完整复现步骤、依赖版本与 11 条验收点见 [`docs/reproduction-guide.md`](docs/reproduction-guide.md)。**

---

## 目录

```
src/paperswarm/                  内核（零框架依赖，可脱离 JiuwenSwarm 测试）
  evidence.py      台账：artifact SHA-256 登记 + claim 登记 + verify() 四重校验
  tex_gate.py      \result{claim_id} 门控与数值回填（gate_text / gate_strict / resolve）
  llm.py           OpenAI 兼容客户端：显式超时 + 有界重试（指数退避 + 全抖动）
  envfile.py       .env.local 加载（只由入口脚本调用；库层只认 os.environ）
  telemetry.py     Token / 时延 / 成本追加式 JSONL 台账（不内置价格表）
  agentloop.py     有界工具调用循环（五重上界）
  tools.py         把台账包成模型可调工具（compact 4 个 / 完整 7 个）
  lab.py           真实对照实验（numpy）        assemble.py  ICLR 模板 + 编译
scripts/
  full_paper.py           端到端全文生成（主入口）
  rebuild_from_run.py     用已有 run 的草稿+台账重装（不调模型）
  vertical_slice.py       竖切片（结果段）
  token_report.py         跨运行 tokens/时延/成本聚合
  review_readiness.py     外部评测提交前自检（离线，不提交）
  submit_to_reviewer.py   提交到 paperreview.ai 官方 API（--dry-run 可只验连通性）
  poll_review.py          轮询评测状态并落盘结果
  verify_vendored_kernel.py     vendor 内核静态 + 功能校验
  verify_swarmflow_dryrun.py    Swarmflow 脚本替身真实执行
  vendor_into_jiuwenswarm.py    vendor 写入上游（一次性）
integration/                  本机替身契约测试（无需 openjiuwen）
tests/                        离线回归基线
templates/iclr2026/           ICLR 2026 官方模板（原样，不改写）
configs/model.yaml            模型后端 profile
docs/                         设计与验证文档
reports/                      验证证据（校验输出、diff、聚合报告）
output/                       最终交付版 PDF
runs/<run_id>/                每次运行的全部可追溯产物
```

---

## 接入 JiuwenSwarm 的部分

| 元素 | 类型 | 职责 |
|---|---|---|
| `swarm.paper_ledger_tools` | TOOL | 台账读写：`register_artifact` / `add_claims` / `write_latex_section`（含硬门控） |
| `swarm.paper_assembler_tools` | TOOL | 回填 `\result{}` + 套模板 + 编译 + 页数/体积校验 |
| `swarm.paper_protocol_prompt` | RAIL | 注入证据协议（`\result{claim_id}` 原样保留） |
| `swarm.paper_gate_audit` | RAIL | **只读**事后审计：发现绕过台账写 `.tex` 且含手写数值 → 留痕，**不改文件** |

- **全部自门控**：解析不到 run_dir 或内核缺失即返回 `[]`/`None`，不需要调用方判断。
- **未 opt-in 的团队拿到与改动前完全一致的元素集合**——paper 元素**不加进** `_COMMON_*_NAMES`
  默认元组，只在 `paper_swarm.enabled` 为真时条件追加（`test_manifest_catalog.py` 断言 catalog 与 registry 精确相等）。
- **强制点在工具边界，不在 prompt、不在 hook**：`write_latex_section` 在下列任一情况下
  抛 `GateRejected` 且**不落盘**——零 `\result{}` 引用 / 结果语境手写数字 / 引用不存在的
  claim_id / 引用的 claim 校验失败（含产物被篡改）/ 正文重定义 `\result` 宏。

Swarm Skill 包：`resources/agent/workspace/skills/paper-repro-swarm/`
（5 角色：planner / evidence-steward / section-writer / assembler / evidence-auditor）。

**改动清单与回归证据见 [`docs/upstream-contribution.md`](docs/upstream-contribution.md)。**

---

## 五重上界（任何自主循环都必须有界）

| 上界 | 触发后 |
|---|---|
| `max_steps` | 步数用尽即停（硬上限 64，构造时拒绝更大值） |
| `token_budget` | 累计 token 超预算即停 |
| `wall_clock_seconds` | 超墙钟即停 |
| `repeat_limit` | 连续 N 次完全相同的工具调用判定死循环 |
| `required_tools` | 必需工具未成功执行过，不许收尾；提醒次数同样有上限 |

危险工具（`dangerous=True`）必须有 `confirm` 回调，**缺少回调即拒绝执行**（fail-closed）。
上界之间**不互相掩盖失败原因**。

---

## 门控拒绝的五种情况

写入路径（`write_latex_section`）在下列任一情况下**不落盘**并抛 `GateRejected`：

1. 正文一个 `\result{}` 引用都没有；
2. 结果语境下出现手写数字；
3. 引用了不存在的 `claim_id`；
4. 引用的 claim 未通过校验（含**产物被篡改**）；
5. 正文里重新定义了 `\result` 宏。

---

## 已验证（2026-09-29）

- 端到端 **PASS**：真实 numpy 实验 → 证据登记 → Agent 写正文 → 门控 → 回填 → tectonic 编译
- 正文 **0 个手写数字**，结论段落全部由台账回填真实值；**11/11 claim 值原样出现在 PDF 中**
- **PDF 结构实测**：书签 6 条一级章节、0 条越界子标题（此前 6 章只有 2 个标题，已修复，
  见 `docs/bugfix-section-headings.md`）
- **反伪造实测**：篡改产物后 11 条 claim 全部拦截、退出码 1、门控拒绝产出文件
- **门控真实拦下模型**：一章里模型编造了不存在的 claim id `cl-std`，被拒后带问题清单重写通过
  （这条不是构造的测试，是运行中真实发生的事，见 `runs/paper-20260929-161913/events.jsonl`）
- 离线测试 **105/105 + 14/14 + 17 + 16/16** 全绿

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/DELIVERABLES.md`](docs/DELIVERABLES.md) | **交付清单**：你要的东西 → 产出 → 验证状态 |
| [`docs/reproduction-guide.md`](docs/reproduction-guide.md) | 环境 / 依赖版本 / 三条复现路径 / 验收点核对表 / 故障排查 |
| [`docs/architecture-and-innovation.md`](docs/architecture-and-innovation.md) | C3 判断、架构、模块调用链、4 条创新点、与 FARS 对照、取舍 |
| [`docs/token-cost-report.md`](docs/token-cost-report.md) | Token / 时延 / 成本实测与量级对比 |
| [`docs/upstream-contribution.md`](docs/upstream-contribution.md) | 改动清单 + 回归证据 + 双 PR 计划 |
| [`docs/upstream-ci-runbook.md`](docs/upstream-ci-runbook.md) | 上游 CI 测试怎么跑 |
| [`docs/review-result-and-gap-analysis.md`](docs/review-result-and-gap-analysis.md) | **外部评测 2.4 分的逐条核实** + 已定位的方法学真缺陷 + 门控证明力边界 + 迭代方案 |
| [`docs/agentic-reviewer-eval.md`](docs/agentic-reviewer-eval.md) | **更正记录** + 官方 API 全清单 + 7 维评分机制 + 提交自检与留痕 |
| [`docs/bugfix-section-headings.md`](docs/bugfix-section-headings.md) | 章节标题丢失缺陷：根因、修复、三层验证、教训 |
| [`docs/env-recon.md`](docs/env-recon.md) | 环境勘察（WSL 阻断、Ollama 实测、资源水位） |
| [`docs/fars-benchmark.md`](docs/fars-benchmark.md) | FARS 对标与差异化定位 |
| [`docs/phase2-vertical-slice-report.md`](docs/phase2-vertical-slice-report.md) | 竖切片过程与 6 个已修缺陷 |

---

## 已知缺口（不掩饰）

1. **上游 CI 未执行** —— 18 个用例需要真实 `openjiuwen`，本机未装。见 `docs/upstream-ci-runbook.md`。
2. **无 Linux 沙箱** —— WSL 被本机安全策略硬阻断，`jiuwenbox` 不可用；复现实验执行后端未定。
3. **对抗审稿闭环未实现**（`adversary` 角色只有设计，没有代码）。
4. **论文质量远低于 ICLR 水平 —— 已被外部评审确认：2.4 / 10**（7 维仅 1 维正向）。
   评审点名的 4 条硬伤已逐条拿代码核实，其中"用测试集选超参"是**真缺陷**（`lab.py:157-160, 175`）。
   见 `docs/review-result-and-gap-analysis.md`。
5. **只跑通 1 篇完整论文**，单位成本是单次观测，不构成分布。
6. **无 Git 基线** —— 上游目录不是 git 仓库，"零漂移"是与原始归档比对得出，
   证明强度弱于 `git diff`。
7. **外部评测 2.4 分，未达及格线** —— 这是**真实返回的分**，不是估计。官方自述
   AI 与单个人类评审 Spearman 相关仅 0.42，故这是**弱信号**，但方向明确。见
   `docs/review-result-and-gap-analysis.md`。
8. **核心创新点不新颖** —— 评审点名并已独立检索确认：**ResearchLoop** 自称
   "evidence-gated control plane"、有完整 `claim ledger` + `gate predicate specification`；
   **EviGraph** 用类型化证据图做运行时状态，Claim Support Rate 比最强基线 +40.19%；
   **Paper Pilot** 实测加门控后伪造引用为 0。**我们一篇都没引。**
9. **门控的证明力边界** —— 门控保证"数字可追溯到产物"，**不保证产物算得对**。
   本次最有价值的发现：α 那个数字走完了全部关卡（台账、门控、回填、11/11 溯源、篡改检测、
   提交前 8/8）**每一步都是绿的，而它的产生方法是错的**。`provenance ≠ validity`。
10. **曾误判"paperreview.ai 无官方 API"** —— 因为只看了首页表单、没探 `/openapi.json`。
    已更正并由真实提交验证（见 `docs/agentic-reviewer-eval.md` §0）。
