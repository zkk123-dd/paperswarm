# 外部评测：Stanford Agentic Reviewer（paperreview.ai）

> 目的：给论文一个**外部、可比、可回填**的质量信号，而不是自证"我们觉得写得不错"。

---

## 0. 更正记录（必读）

**本文 2026-09-29 早先版本的第一条结论是错的。**原文写"没有官方 REST API，公开入口只有网页表单"。

**错在哪**：当时只看了首页 HTML（那确实是个表单页），**没有探测 `/openapi.json`**。
服务端是 FastAPI/uvicorn —— 这类服务默认就自带机器可读的自描述文档。

**实测证据**（2026-09-29 16:58 GMT+8）：

```
HEAD https://paperreview.ai/   -> 405 Method Not Allowed, Allow: GET
                                  Content-Type: application/json
                                  Server: Caddy / uvicorn
GET  https://paperreview.ai/openapi.json -> 200, 8,658 bytes
     info.title = "AI Paper Reviewer API", version 1.0.0
```

**后果**：上次交付时把"外部评测"标为 ❌ 缺凭证。**这个判定本身站不住** —— 缺的不是凭证，是我没找对入口。
`/api/upload` 的返回里直接带 access token，**不需要收信邮箱就能拿到评测结果**。

**教训（已写进 `agent-evidence-gating` skill）**：判定"某服务没有 API"之前，必须探
`/openapi.json`、`/docs`、`/redoc` 三个 FastAPI 默认端点。"首页是表单" ≠ "没有 API"。

---

## 1. 官方 API（来自服务自描述的 `/openapi.json`）

| 方法 | 端点 | 用途 |
|---|---|---|
| `POST` | `/api/get-upload-url` | 请求 S3 预签名 POST 地址。请求 `{filename, venue}` |
| `POST` | `/api/upload` | **直传（legacy）**，`multipart/form-data`：`pdf` + `email` + `venue` → **返回 access token** |
| `POST` | `/api/confirm-upload` | 确认 S3 上传，建提交记录。`form-urlencoded`：`s3_key` + `email` + `venue` → **返回 access token** |
| `GET` | `/api/status/{token}` | 提交状态：`pending` → … → 完成 |
| `GET` | `/api/review/{token}` | **评测正文与分数** |
| `POST` | `/api/feedback/{token}` | 提交对评审的反馈 |
| `GET` | `/api/metrics` | 服务负载（可用作提交前的限流参考） |
| `GET` | `/health` | 健康检查 |

**鉴权**：`token` 本身即凭据，放在路径里；**无 API key、无 OAuth**。
**限流**：`get-upload-url` 与 `confirm-upload` 都可能返回 **429**，需退避重试。

### 1.1 真实提交序列（本节是实测，不是推测）

前端 `/static/upload.js` 用的就是这套三步，本仓库脚本 `scripts/submit_to_reviewer.py` 忠实复现：

```
① POST /api/get-upload-url  {filename, venue}          -> presigned_url, s3_key, presigned_fields
② POST <presigned_url>      multipart                  -> 204
      ⚠️ presigned_fields 必须排在 file 之前（S3 预签名 POST 的硬要求）
③ POST /api/confirm-upload  {s3_key, venue, email}      -> {success, token, message}
④ GET  /api/status/{token}                              -> {"status": "pending", ...}
⑤ GET  /api/review/{token}                              -> 结果就绪后返回评审
```

桶与区域：`paperuploads.s3.us-east-2.amazonaws.com`。

---

## 2. 官方事实卡（已核实）

来源：首页表单 + `/tech-overview` + `/openapi.json`（2026-09-29 实读）。

### 2.1 硬约束

| 项 | 事实 |
|---|---|
| **PDF 体积上限** | **10 MB** |
| **分析页数** | **只分析前 15 页** |
| 支持语言 | **仅英文论文** |
| 目标会议下拉 | ICLR / NeurIPS / ICML / CVPR / AAAI / IJCAI / ACL / EMNLP / OSDI / SOSP / VLDB / SIGMOD / Other |
| 费用 | 免费 |
| 联系 | `aireviewer@cs.stanford.edu` |
| 来源方 | Stanford ML Group（Yixing Jiang & Andrew Ng） |
| 处理时长 | 官方自述"**可能数小时甚至更久**（服务器负载高时）" |

> 官方页面上有一条醒目的提示：**部分邮箱收不到通知邮件**，所以要**当场保存 token**。
> 这不影响我们 —— token 是 `confirm-upload` 的直接返回值。

### 2.2 评分机制

- **不是**让 LLM 直接给总分。先在 **7 个维度**打分，再用**线性回归**映射成 1–10 总分。
- 7 个维度：**原创性 / 研究问题的重要性 / 结论是否有充分依据 / 实验的严谨性 / 写作清晰度 / 对研究社区的价值 / 与既有工作的定位是否恰当**。
- 训练数据：ICLR 2025 随机抽 300 篇投稿，剔除 3 篇无人类评分的撤稿 → 297 篇；**150 篇训练回归，147 篇测试**。

### 2.3 效力边界（官方自述，必须一并引用）

| 指标 | 数值 |
|---|---|
| 人类评审之间 Spearman 相关 | **0.41** |
| AI 与单个人类评审 Spearman 相关 | **0.42** |
| 预测"是否被接收"的 AUC：人类评分 | 0.84 |
| 预测"是否被接收"的 AUC：AI 评分 | **0.75** |

官方自己指出 AUC 那组**不是同口径比较**（人类评分本身参与录取决定）。
且明确说明：依托 arXiv 检索，**AI 领域更准，其他领域更不准**；评审由 AI 生成、**可能有错**。

**对我们的含义**：这是**可比的弱信号**，不是录用预言。当外部锚点用（对齐 FARS 的同源分数）。

### 2.4 工作流（官方描述）

```
PDF → LandingAI ADE 转 Markdown → 提取标题 + 校验"确为学术论文"
   → 生成多粒度检索 query → Tavily 搜 arXiv → 取元数据
   → 按相关性挑最相关工作 →（必要时下载全文生成聚焦摘要）
   → 结合原文 + 相关工作摘要 → 按模板产出评审 + 7 维分数 → 回归得 1–10 分
```

---

## 3. 目标档位（外部 KPI）

出自 `fars-benchmark.md` §0.3、§2 —— FARS 的 100 篇论文就是被这个服务按 ICLR 标准打的：

| 档位 | 分数 | 依据 |
|---|---|---|
| **及格** | ≥ **4.21** | ICLR 2026 人类投稿平均分 |
| **良好** | ≥ **5.05** | FARS 100 篇平均分 |
| **优秀** | ≥ **5.39** | ICLR 2026 接收论文平均分 |

**venue 必须选 ICLR**，否则拿不到可比的 1–10 分。

---

## 4. 本次实际提交（已完成）

| 项 | 值 |
|---|---|
| 提交时间 | 2026-09-29T08:59:52Z（UTC）/ 16:59 GMT+8 |
| PDF | `output/PaperSwarm-ICLR2026.pdf` |
| 体积 | 34,729 字节（上限 10 MB） |
| SHA-256 | `f3813537b9515c562df8d2d68bfdc2a8264b271e04d600b3693e83f882a116cb` |
| 提交邮箱 | `2446569392@qq.com` |
| venue | **ICLR** |
| S3 key | `uploads/20260929_085958_5ab2d0cd_PaperSwarm-ICLR2026.pdf` |
| **access token** | **`3xjzh0ZcSPc2uhS7lDhDH_r0r5Td3Dl-FiWgSMLAB90`** |
| 提交时状态 | `pending`（处理中） |

**完整留痕**：`reports/review_submission.json`（含预签名字段清单、S3 key、token、服务端 message）。
**结果**：`reports/review_result.json`（就绪后由 `scripts/poll_review.py` 落盘）。

**`pdf_sha256` 是刻意留的**：它把"提交的那一份 PDF"钉死。论文后来改过，旧分数就不能再算在这版上 —— 与证据台账同一思路。

---

## 5. 工具（本仓库，均已实测）

```bash
PY="C:/Users/周凯/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 提交前自检：8 条硬约束 + 形态检查（离线，不联网、不提交）
"$PY" scripts/review_readiness.py

# 提交：--dry-run 只走第 1 步（拿预签名地址，不落文件、不建记录）
"$PY" scripts/submit_to_reviewer.py --pdf output/PaperSwarm-ICLR2026.pdf \
    --email 2446569392@qq.com --venue ICLR --out reports/review_submission.json --dry-run

# 真提交
"$PY" scripts/submit_to_reviewer.py --pdf output/PaperSwarm-ICLR2026.pdf \
    --email 2446569392@qq.com --venue ICLR --out reports/review_submission.json

# 轮询结果（退出码 3 = 仍在处理中，非失败）
"$PY" scripts/poll_review.py --token <TOKEN> --wait-seconds 1800
```

### 5.1 提交前自检（`scripts/review_readiness.py`）

对最终版 PDF 实测 **PASS 8 / WARN 0 / FAIL 0**（`reports/review_readiness.md`）：

| 检查 | 状态 | 明细 |
|---|---|---|
| `pdf_exists` | PASS | 路径存在 |
| `size_within_10mb` | PASS | 34,729 B = 0.0331 MiB |
| `pdf_readable` | PASS | pages=4 |
| `analyzed_pages_within_15` | PASS | pages=4（上限 15 页） |
| `english_language` | PASS | ASCII 字母占比 0.943（阈值 0.60） |
| `anonymized_for_double_blind` | PASS | 命中 `anonymous` / `double-blind` / `under review` |
| `no_unresolved_result_macro` | PASS | 未回填的 `\result{` 出现 **0** 次 |
| `first_page_has_content` | PASS | 首页 453 个非数字 token（阈值 80） |

**脚本边界（不夸大）**：
- `english_language` 用 ASCII 字母占比，**粗判**，只拦"整篇不是英文"；
- `anonymized_for_double_blind` 命中关键词即过，**不保证真匿名**（作者信息可能藏在图/元数据里）；
- 它**不能替代人读一遍**。论文质量不是这 8 条能判的。

---

## 6. 交付状态

| 交付物 | 状态 |
|---|---|
| 提交就绪包 `reports/review_readiness.{json,md}` | ✅ **8/8 PASS** |
| 官方 API 端点清单（自描述来源） | ✅ §1 |
| 提交脚本 + 轮询脚本 + 渲染脚本 | ✅ 均已实测 |
| **论文实际提交** | ✅ **已完成**（token 见 §4） |
| **评测分数** | ✅ **2.4 / 10**（1/7 维正向）—— 未达及格线 4.21 |

**结果与差距分析另见** `review-result-and-gap-analysis.md`：
评审点名的硬伤已逐条核实，其中"用测试集选超参"**确认为真缺陷**（`lab.py:157-160, 175`）。

> 出分只用了 **约 9 分钟**（09:00:00 提交 → 09:09:16 出分），远快于官方预警的"数小时"。

---

## 7. 7 维自查表（自己先过一遍，事后与 AI 打分对照）

对着官方 7 个维度逐条问，**每条都要指到论文里的具体位置**。

| # | 维度 | 问自己的问题 | 我们的自评 | **AI 实际** |
|---|---|---|---|---|
| 1 | Originality | 哪段说清"此前没人这么做过"？ | 证据门控闭环 —— 见 `architecture-and-innovation.md` §5.1 | **−1** ❌ |
| 2 | 重要性 | 为什么值得解决？ | 多智能体论文的数值不可追溯 → 诚信风险 | **+1** ✅ |
| 3 | 结论是否有依据 | 每个数字都能指到产物吗？ | ✅ 11 条 claim 全部绑定 `exp/metrics.json` 的 SHA-256 + JSON Pointer | **−1** ❌ |
| 4 | 实验严谨性 | 有对照？有种子重复？ | OLS vs Ridge 对照，8 种子取均值±标准差，噪声下限 0.1225 | **−1** ❌ |
| 5 | 写作清晰度 | 前 15 页讲完贡献吗？ | 4 页（限 15）。**但 7B 模型措辞重复是已知短板** | **−1** ❌ |
| 6 | 社区价值 | 别人能复用吗？ | 内核零框架依赖、可单独测试；Swarm Skill + Swarmflow 可直接复用 | **−1** ❌ |
| 7 | 定位 | 和 FARS 比处在什么位置？ | 明确写成"低预算、高可验证性"的差异化 | **−1** ❌ |

**自评与实评落差很大**：我们自认第 3、4 条是强项（数字全可追溯、多种子取均值），AI 都给了 −1。

- 第 4 条的 −1 **是对的**：我们只看到"有多种子"，没看到"**用测试集选超参**"。
- 第 3 条的 −1 含义是：**数字可追溯 ≠ 结论被证据支持** —— 实验根本没测门控。

详见 `review-result-and-gap-analysis.md` §2、§3。

---

## 8. 分数回填结果（已完成）

`scripts/render_review.py` 把接口返回渲染成 `reports/review_result.md`。实际回填：

```json
{
  "service": "paperreview.ai",
  "venue": "ICLR",
  "submitted_at": "2026-09-29T09:00:00Z",
  "review_date": "2026-09-29T09:09:16Z",
  "pdf": "output/PaperSwarm-ICLR2026.pdf",
  "pdf_sha256": "f3813537b9515c562df8d2d68bfdc2a8264b271e04d600b3693e83f882a116cb",
  "access_token": "3xjzh0ZcSPc2uhS7lDhDH_r0r5Td3Dl-FiWgSMLAB90",
  "overall_score": 2.4,
  "dimension_scores": {
    "originality": -1, "importance": 1, "claims_supported": -1,
    "soundness_of_experiments": -1, "clarity": -1,
    "value_to_community": -1, "contextualization": -1
  },
  "review_markdown": "reports/review_result.md",
  "notes": "venue 必须是 ICLR 才出 1-10 可比分；本分数未达及格线 4.21"
}
```

**目标档位：4.21 及格 / 5.05 良好 / 5.39 优秀**（见 `fars-benchmark.md` §2）。
**实际 2.4，差 1.81 分。** 差距分析见 `review-result-and-gap-analysis.md`。

---

## 9. 伦理与合规（硬约束）

1. **这是预审，不是投稿。** 不用于规避任何会议的评审流程。
2. **论文显式标注 AI 生成**（`fars-benchmark.md` §3.5 已写进设计）。
3. **评审由 AI 生成、可能有错**；不当作录用判决，不对外宣称"通过了 XX 分评审"作为质量证明。
4. **负面结果照实报告**，不因为分数不好看就换种子重跑 —— 那正是本系统设计要防的事。
5. 提交动作涉及**对外发送**，本次由 ii 明确授权后执行。

---

## 10. 待确认清单

| 编号 | 内容 | 状态 / 核实方式 |
|---|---|---|
| E1 | 收信邮箱 | ✅ `2446569392@qq.com` |
| E2 | 对外提交授权 | ✅ 已明确同意 |
| E3 | 官方 API 端点与请求体 | ✅ **已核实**（`/openapi.json` 自描述 + 实测三步走通） |
| E4 | 官方是否开放 API | ✅ **是**，见 §1 |
| E5 | 本系统在 ICLR venue 下的实际得分 | ⏳ 已提交，等 `GET /api/review/{token}` |
| E6 | 分数与人类评分的相关性（官方 0.42） | 需多篇样本，单篇无法验证 |
| E7 | 是否需要 `POST /api/feedback/{token}` 回馈评审质量 | 拿到评审后可做，非必须 |
