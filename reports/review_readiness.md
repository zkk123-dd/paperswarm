# 外部评测提交前自检（本地，无网络）

> 文件：`runs\paper-20261008-115954\paper\main.pdf`
> 时间：2026-10-08T12:04:30
> 本检查不联网、不提交，仅判定官方硬约束与本地可判定的质量门槛。

| 检查 | 状态 | 明细 |
|---|---|---|
| `pdf_exists` | **PASS** | runs\paper-20261008-115954\paper\main.pdf |
| `size_within_10mb` | **PASS** | bytes=38979, mib=0.0372, limit_bytes=10485760 |
| `pdf_readable` | **PASS** | pages=4 |
| `analyzed_pages_within_15` | **PASS** | pages=4, limit=15 |
| `english_language` | **PASS** | ascii_letter_ratio=0.95, threshold=0.6 |
| `anonymized_for_double_blind` | **PASS** | markers_found=['anonymous', 'double-blind', 'under review'], expected_any_of=['anonymous', 'double-blind', 'under review'] |
| `no_unresolved_result_macro` | **PASS** | occurrences=0 |
| `first_page_has_content` | **PASS** | first_page_tokens=517, threshold=80 |

**汇总**：PASS 8 / WARN 0 / FAIL 0

首页题名（前 14 个 token，仅供人工核对）：

> Under review as a conference paper at ICLR PaperSwarm: An Evidence-Gated Multi-Agent System for
