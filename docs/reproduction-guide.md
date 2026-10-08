# 复现指南：环境、依赖、命令、验收点

> 目标：**换一台机器也能把同一套流程跑出来**，并且每一步都有可对照的期望输出。
> 本文所有命令与版本号均在 2026-09-29 于本机实测。

---

## 0. 你会得到什么

跑完本文的命令后，`runs/<run_id>/` 下会有：

```
<run_id>/
├── exp/metrics.json           实验原始产物（SHA-256 固化的对象）
├── ledger/artifacts.jsonl     产物登记（id / 路径 / sha256 / 生产者）
├── ledger/claims.jsonl        claim 登记（claim_id / 值 / 指向产物的 JSON Pointer）
├── llm_calls.jsonl            逐次 LLM 调用（tokens / 时延 / 重试 / 成本）
├── events.jsonl               阶段事件（gate_pass / gate_reject / experiment_done …）
├── run_summary.json           汇总（tokens / P50 / P95 / PDF 页数与体积 / 成本口径）
├── assembly_report.json       组装报告（每章 refs / resolved / warnings + 编译日志尾部）
└── paper/
    ├── drafts/*.tex           模型原始产出（**含 \result{} 宏，是审计证据，不能被覆盖**）
    ├── sections/*.tex         回填后的正文
    ├── main.tex               ICLR 模板骨架
    └── main.pdf               最终 PDF
```

`drafts/` 与 `sections/` **必须是两个目录**。曾经两者都是 `paper/sections/`，
导致 `resolve()` 把含宏的原始稿覆盖掉——而**原始稿正是"模型没写数字"的证明**。
这条约束写在 `scripts/full_paper.py` 的注释里，改动时别合并它。

---

## 1. 环境要求

### 1.1 Python

| 项 | 本机实测 |
|---|---|
| 解释器 | `C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe` |
| 版本 | 3.13.14 |
| 隔离方式 | 独立 venv，**不污染系统 Python** |

上游 JiuwenSwarm 要求 `>=3.11,<3.14`；本机内核只用到 stdlib + 下列第三方包，兼容面更宽。

### 1.2 Python 依赖（本机实测版本）

```bash
pip install \
  numpy==2.5.2 \
  pandas==3.0.5 \
  matplotlib==3.11.1 \
  requests==2.34.2 \
  PyYAML==6.0.2 \
  pydantic==2.11.7 \
  jsonschema==4.25.1 \
  tenacity==9.1.2 \
  pypdf==6.1.1 \
  pymupdf==1.28.2
```

| 包 | 用途 | 必需？ |
|---|---|---|
| `numpy` | 对照实验（OLS / Ridge） | ✅ 必需 |
| `requests` | LLM HTTP 客户端 | ✅ 必需 |
| `PyYAML` | 读 `configs/model.yaml` | ✅ 必需 |
| `pymupdf` | PDF 页数与体积校验 | ✅ 必需（替代缺失的 `pdfinfo`） |
| `pydantic` / `jsonschema` | 配置与工具参数校验 | ✅ 必需 |
| `pypdf` / `tenacity` | 备用 PDF 读取 / 重试工具 | ⭕ 可选 |
| `pandas` / `matplotlib` | 出表出图（后续扩展） | ⭕ 可选 |

> **pytest 相关**：本机 venv 里是 `pytest==9.1.1`。跑上游 CI 级测试还要
> `pytest-asyncio>=1.0.0` 与 `pytest-cov>=7.0.0`，但那属于上游 `[test]` extra，见 `upstream-ci-runbook.md`。

### 1.3 LaTeX 引擎：tectonic

**没有任何 TeX 发行版（pdflatex / xelatex / latexmk 全缺）时，tectonic 是唯一可行的单文件方案。**

| 项 | 本机实测 |
|---|---|
| 版本 | **Tectonic 0.16.9** |
| 可执行文件 | `E:/tools/tectonic/tectonic.exe`（49,630,208 字节 ≈ 47.3 MiB） |
| 离线 bundle | `E:/tools/tectonic/bundle/texlive2024-0312.ttb`（1,794,017,450 字节 ≈ 1.67 GiB） |
| 缓存目录 | `E:/tools/tectonic/cache` |

```bash
# 验证安装
E:/tools/tectonic/tectonic.exe --version
# → Tectonic 0.16.9
```

**为什么要 bundle 而不用在线宏包**：`full_paper.py` 用 `--bundle` 指定本地 `.ttb`，
编译过程**不访问公网**，因此在无网环境也能复现。这条对"复现指南"是硬要求。

> 三个路径（`--tectonic` / `--bundle` / `--cache-dir`）都有 CLI 参数可覆盖，见 §3.2。
> **无需把 tectonic 放 C 盘**——本机 C 盘仅剩 27 GB，且 TeX Live 默认装 C 盘会挤爆，见 `env-recon.md` §6.4。

### 1.4 模型后端（两条路径）

| 路径 | 需要什么 | 边际成本 |
|---|---|---|
| **离线路径**（`--skip-llm-check`） | 什么都不需要 | — |
| **端到端路径** | 本机 Ollama 在跑 | **0 元** |

```bash
# 检查 Ollama 是否就绪
curl -s http://127.0.0.1:11434/api/tags
# 期望：JSON 里出现 "name":"qwen2.5:7b-instruct-q4_K_M"，capabilities 含 "tools"
```

已核实（本机实测）：Ollama 0.34.4 / 端点 `http://127.0.0.1:11434/v1`（OpenAI 兼容）/
模型 `qwen2.5:7b-instruct-q4_K_M`（7.6B Q4_K_M，上下文 32768）/ **function calling 正常**（返回 `tool_calls`）。

### 1.5 环境变量

| 变量 | 用途 | 默认 |
|---|---|---|
| `PAPER_LLM_BASE_URL` | 覆盖端点 | 取自 `configs/model.yaml` 的 profile |
| `PAPER_LLM_MODEL` | 覆盖模型名 | 同上 |
| `PAPER_LLM_API_KEY` | API key（**只从环境变量读，禁止硬编**） | 本机 Ollama 不需要 |

`configs/model.yaml` 里 `api_key_env: PAPER_LLM_API_KEY` 是**间接引用**，
明文 key 不写进仓库、不进日志。

---

## 2. 三条复现路径

| 路径 | 需要网络？ | 需要模型？ | 产出 | 用途 |
|---|---|---|---|---|
| **A. 离线回归** | ❌ | ❌ | 台账 + 门控验证 | 任何机器可跑的回归基线 |
| **B. 本机端到端** | ❌ | ✅ 本机 Ollama | **ICLR PDF** | 完整链路验证 |
| **C. 上游 CI** | ✅ | ❌ | 框架装配回归 | 证明改动没弄坏上游 |

---

## 3. 逐步操作

### 3.0 通用准备

```bash
export PATH="/usr/bin:/bin:$PATH"                                  # Git Bash 必需，否则 dirname 报 not found
export PYTHONPATH="E:/存放/agent/paper-swarm/src"
PY="C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd "E:/存放/agent/paper-swarm"
```

### 3.1 路径 A：离线回归（**先跑这个**）

```bash
"$PY" tests/test_evidence.py          # 台账 + 篡改检测
"$PY" tests/test_kernel_offline.py    # 重试 / 循环上界 / 门控拒绝 / 幂等 / 章节结构
```

**期望输出**：`9/9 passed` 与 `96/96 passed`（合计 **105**）。

```bash
"$PY" scripts/verify_vendored_kernel.py
# 期望：无 stdout；退出码 0；reports/vendor_verify.json 里 "ok": true, "passed": 14, "total": 14
```

```bash
"$PY" -m pytest integration/test_paper_providers_contract.py -q
# 期望：17 passed
```

```bash
"$PY" scripts/verify_swarmflow_dryrun.py
# 期望：16/16 checks passed；reports/swarmflow_dryrun.json 里全 true
```

**这一步全部不联网、不调模型、不调 tectonic**，是最可靠的回归基线。

### 3.2 路径 B：端到端产出 PDF

**先跑无模型版验证链路**（实验 → 台账 → 组装 → 编译全走一遍，只是正文用占位符）：

```bash
"$PY" scripts/full_paper.py --skip-llm-check
```

**期望输出**（底部）：

```
编译: ok=True attempts=1
PDF : main.pdf  pages=1(<=9:True) size=0.018MB(<=10:True)
```

**再跑真模型版**：

```bash
"$PY" scripts/full_paper.py
```

**期望输出**（形态，数字会随运行变化）：

```
[1/5] 跑对照实验 ...
      OLS=... Ridge=... improve=...% (...s)
[2/5] 登记证据台账 ...
      claim cl-ols-mean    = ...
      ...（共 11 条）
      台账校验: passed=11 failed=0 ok=True
[3/5] 调用模型逐章写作（每章过证据门控）...
      introduction   ok (attempts=..., ... chars)
      ...（共 6 章 + abstract）
[4/5] 组装论文（套 ICLR 2026 模板 + 台账回填）...
[5/5] 编译 PDF ...
==================================================================
编译: ok=True attempts=1
PDF : main.pdf  pages=4(<=9:True) size=0.033MB(<=10:True)
==================================================================
```

**路径覆盖参数**（如果 tectonic 装的位置不同）：

```bash
"$PY" scripts/full_paper.py \
  --tectonic "D:/tools/tectonic/tectonic.exe" \
  --bundle   "D:/tools/tectonic/bundle/texlive2024-0312.ttb" \
  --cache-dir "D:/tools/tectonic/cache" \
  --runs-root "./runs" \
  --max-attempts 3
```

**耗时参考**：本机 7 次 LLM 调用累计 74.5 s，端到端约 **1.5 分钟**（瓶颈是本地 30 tok/s）。
`--skip-llm-check` 版本约 **10 秒**。

### 3.2b 路径 B2：从已有 run 重新组装（不调模型）

排版的迭代（标题、模板、字体）不该每次重新付费调模型。草稿
（`paper/drafts/*.tex`，含 `\result{}` 宏）与台账（`ledger/`）都是自足的中间产物：

```bash
"$PY" scripts/rebuild_from_run.py --run runs/paper-20260929-164239
# 期望底部：
#   编译: ok=True attempts=1
#   PDF : main.pdf  pages=4(<=9:True) size=0.033MB(<=10:True)
#   每章 refs == resolved
```

同时这是一条**回归通道**：`docs/bugfix-section-headings.md` 里"只修组装器"那一列，
就是用这个脚本拿历史草稿复跑得到的。

> **注意**：`--out` 会被解析为绝对路径。tectonic 的 `--outdir` 按相对路径解析且不在
> 本进程的 cwd 下，传相对路径会得到 `output directory ... does not exist`。

**校验结构是否真的对**（不要只看"编译成功"）：

```bash
"$PY" - <<'EOF'
import pymupdf
d = pymupdf.open("output/PaperSwarm-ICLR2026.pdf")
print(d.page_count, d.get_toc())   # 期望 6 条一级条目：Introduction…Conclusion
EOF
```

LaTeX 对"没有标题"完全无感——退出码 0、页数合规、体积合规都可能是绿的。
必须查书签层级这一语义层，只查构建层会漏掉整类结构性缺陷。

### 3.3 路径 C：上游 CI

见 `upstream-ci-runbook.md`。一句话版：

```bash
cd /path/to/jiuwenswarm-develop
pip install -e ".[test]"
pytest tests/agents/swarm/test_paper_providers.py -v     # 期望 18 passed
```

### 3.4 重新生成资源报告

```bash
"$PY" scripts/token_report.py
# → reports/token_report.json  +  reports/token_report.md
```

---

## 4. 验收点核对表

按你的验收习惯逐条列。**每条都给出"怎么算通过"**，不写"应该没问题"。

| # | 验收点 | 通过判据 | 命令 / 文件 |
|---|---|---|---|
| V1 | 产出真实的 ICLR 模板 PDF | `main.pdf` 存在且 `pages_ok=true` `size_ok=true` | `runs/*/assembly_report.json::pdf` |
| V2 | 论文数字全部来自台账 | 正文 `\result{` 计数为 0（已全回填） | `runs/*/paper/sections/*.tex` |
| V3 | 每条 claim 可追溯到产物 | `verify()` 的 `failed=0` | `runs/*/ledger/claims.jsonl` |
| V4 | 篡改产物会被拦下 | 篡改后 `failed>0` 且退出码 1 | `tests/test_evidence.py::test_tampered_artifact_blocks_gate` |
| V5 | 模型手写数字会被拒 | `GateRejected`，文件不落盘 | `tests/test_kernel_offline.py::gate_rejects_hardcoded_number` |
| V6 | 模型编造 claim id 会被拒 | `events.jsonl` 出现 `kind=gate_reject` | `runs/paper-20260929-161913/events.jsonl`（真实发生过） |
| V7 | 自主循环有界 | 五种上界各有测试 | `tests/test_kernel_offline.py`（`loop_*` 共 25 项） |
| V8 | 离线测试全绿 | `9/9` + `96/96` + `14/14` + `16/16` + `17 passed` | §3.1 |
| V9 | 上游源码改动最小 | 2 modified + 21 new，无声明外漂移 | `reports/upstream_audit.txt` |
| V10 | Swarm Skill 合规 | 校验器 `PASS 0 warning 0 error` | `reports/swarmskill_validate.txt` |
| V11 | 资源台账可追溯 | `llm_calls.jsonl` 每次调用有 tokens/时延/成本口径 | `scripts/token_report.py` |
| V14 | PDF **结构**正确 | `get_toc()` 返回 **6 条一级条目**（Introduction…Conclusion），越界子标题 0 个 | §3.2b 末尾的结构校验片段 |
| V15 | claim 值确实落进 PDF | 台账 11 条 claim 的值全部在 PDF 文本中 | `reports/paper_pdf_text_164239.txt` |
| V12 | 上游 CI 通过 | `18 passed` | `upstream-ci-runbook.md` §3（**待执行**） |
| V13 | 外部评测**已提交** | `GET /api/status/{token}` 返回 `venue=ICLR`、`status` 非空 | `reports/review_submission.json` |
| V16 | 外部评测**分数** | paperreview.ai 返回 1–10 分 | `reports/review_result.json` → **2.4**（1/7 维正向）❌ |
| V17 | 评审提出的缺陷被核实 | 每条批评都拿代码/检索对过 | `review-result-and-gap-analysis.md` §2（1 条为真缺陷） |

**当前状态：V1–V11、V13–V15、V17 已通过；V12 待上游 CI；V16 已出分但**未达标**（2.4 < 4.21）。**
不假装 V12 已完成，也不掩饰 V16 的分数。

> V14/V15 是补的：原清单里只有"PDF 存在"（V1）和"数字来自台账"（V2/V3），
> **没有任何一条在查论文结构**，于是"六章只有两个标题"这种缺陷能一路绿灯通过全部验收点。
> 补法见 `bugfix-section-headings.md`。

---

## 5. 目录结构

```
paper-swarm/
├── src/paperswarm/            内核（零框架依赖）
│   ├── evidence.py            台账：SHA-256 登记 + 四重校验
│   ├── tex_gate.py            \result{} 门控与回填
│   ├── lab.py                 对照实验（numpy）
│   ├── assemble.py            模板 + 回填 + 编译
│   ├── llm.py                 超时 + 有界重试
│   ├── telemetry.py           token/时延/成本台账（不内置价格表）
│   ├── agentloop.py           有界工具循环（五重上界）
│   └── tools.py               台账工具集（compact 4 / 完整 7）
├── scripts/
│   ├── full_paper.py          端到端全文生成（主入口）
│   ├── vertical_slice.py      竖切片（结果段）
│   ├── token_report.py        跨运行资源聚合
│   ├── verify_vendored_kernel.py    vendor 内核静态+功能校验
│   ├── verify_swarmflow_dryrun.py   Swarmflow 脚本替身执行
│   └── vendor_into_jiuwenswarm.py   vendor 写入上游（一次性）
├── integration/               本机替身契约测试（无需 openjiuwen）
├── tests/                     离线回归基线
├── templates/iclr2026/        ICLR 2026 官方模板（原样，不改写）
├── configs/model.yaml         模型后端 profile
├── docs/                      设计与验证文档（本目录）
├── reports/                   验证证据（校验输出、diff、聚合报告）
└── runs/<run_id>/             每次运行的全部可追溯产物
```

---

## 6. 常见故障

| 现象 | 根因 | 处理 |
|---|---|---|
| `dirname: command not found`，退出码 127 | Git Bash 未设 PATH | `export PATH="/usr/bin:/bin:$PATH"` |
| `ModuleNotFoundError: paperswarm` | 没设 `PYTHONPATH` | `export PYTHONPATH=".../paper-swarm/src"` |
| `AssemblyError`，stderr 里是字体/宏包错误 | tectonic 找不到 bundle 或模板缺文件 | 检查 `--bundle` 路径；确认 `templates/iclr2026/` 完整（含 `.sty` / `.bst` / `natbib.sty` / `fancyhdr.sty`） |
| `§ 章节 X 在 N 次尝试后仍未通过证据门控` | 模型持续写手写数字或编造 claim id | 提高 `--max-attempts`；或换更强模型；**不要放宽门控** |
| `台账自校验未通过` | 产物在登记后被改动 | 检查是否有其他进程写了 `exp/metrics.json`；这正是门控该做的事 |
| `章节 X 报"未登记的 artifact"` | 模型用了文件名当 artifact_id | 工具报错会列出可用 id；`register_artifact` 返回值里有 `use_this_artifact_id` |
| 成本列显示 `unpriced` | profile 没给单价，或调用方没透传 | 在 `configs/model.yaml` 的 profile 里填 `price_per_1k_*`；确认调用方传了（本机已修，见 `token-cost-report.md` §5.1） |
| 显存不足 | 7B 模型全量 offload 占约 5 GB 显存 | 先释放：`curl -s http://127.0.0.1:11434/api/generate -H "Content-Type: application/json" -d '{"model":"qwen2.5:7b-instruct-q4_K_M","keep_alive":0}'` |

---

## 7. 安全与合规约定

1. **凭据只从环境变量读**，不写入仓库、不写入日志、不贴进对话。
2. **台账不可回溯篡改**：历史运行的记录（包括对自己不利的口径错误）保持原样，
   只在新运行里修复。见 `token-cost-report.md` §5.1。
3. **论文显式标注 AI 生成**，不自动投稿到任何公开平台。
   `swarm.paper_review_submit` 只提交到 paperreview.ai 做**预审**，且是**危险操作，强制人工确认**。
4. **排除项**：不做绕过平台风控、批量爬取隐私数据、自动化欺诈、规避审计的用途。
