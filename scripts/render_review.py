#!/usr/bin/env python
"""把 ``reports/review_result.json`` 渲染成人可读的 Markdown。

分数与 7 维二值分放在顶部，评审正文按小节展开。**只做搬运，不解释、不改写** ——
评审原文是外部证据，任何改写都会污染它。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# 官方 7 维（key 为接口里的名字），顺序照官方说明
DIMENSIONS = [
    ("Originality", "原创性"),
    ("Question_Importance", "研究问题的重要性"),
    ("Claims_Support", "结论是否有充分依据"),
    ("Experimental_Soundness", "实验的严谨性"),
    ("Writing_Clarity", "写作清晰度"),
    ("Value_to_Community", "对研究社区的价值"),
    ("Prior_Work_Context", "与既有工作的定位是否恰当"),
]

TARGETS = [("及格", 4.21, "ICLR 2026 人类投稿均分"),
           ("良好", 5.05, "FARS 100 篇均分"),
           ("优秀", 5.39, "ICLR 2026 接收论文均分")]

SECTION_TITLES = [
    ("summary", "Summary"),
    ("strengths", "Strengths"),
    ("weaknesses", "Weaknesses"),
    ("detailed_comments", "Detailed Comments"),
    ("questions", "Questions for Authors"),
    ("assessment", "Overall Assessment"),
]


def parse_binary(raw: str | None) -> dict[str, int]:
    """从 ``binary_scores`` 文本里抽出 维度 -> +1/-1。

    两种回传格式都见过，必须都认：

    * ``- Claims_Support: 0``       （裸数字）
    * ``- Claims_Support: [0]``     （方括号；2026-10-08 那次回传用的这种）

    只认方括号外的形式会在第二种格式下静默解析出空字典，把 7 维表画成一片 "—"，
    所以这里把括号设为可选。
    """
    out: dict[str, int] = {}
    if not raw:
        return out
    for line in raw.splitlines():
        m = re.match(r"\s*-?\s*([A-Za-z_]+)\s*:\s*\[?\s*([+-]?\d+)\s*\]?", line)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="reports/review_result.json")
    ap.add_argument("--out", dest="dst", default="reports/review_result.md")
    args = ap.parse_args()

    data = json.loads(Path(args.src).read_text(encoding="utf-8"))
    rev = data["review"]
    score = rev.get("numerical_score")
    binary = parse_binary(rev.get("sections", {}).get("binary_scores"))
    positive = sum(1 for v in binary.values() if v > 0)
    negative = sum(1 for v in binary.values() if v < 0)
    neutral = len(binary) - positive - negative

    lines: list[str] = []
    a = lines.append

    a("# Stanford Agentic Reviewer 评测结果")
    a("")
    a(f"> 服务：paperreview.ai ｜ venue：**{rev.get('venue')}**")
    a(f"> 提交时间：`{rev.get('submission_date')}` ｜ 出分时间：`{rev.get('review_date')}`")
    a(f"> access token：`{data.get('token')}`")
    a(f"> 论文：{rev.get('title')}")
    a("")
    a("## 综合分")
    a("")
    a(f"# {score} / 10")
    a("")
    a("（官方机制：先在 7 个维度上二值打分，再用线性回归映射到 1–10。）")
    a("")
    a("## 7 维二值评分")
    a("")
    a("| 维度 | 中文 | 判定 |")
    a("|---|---|---|")
    for key, zh in DIMENSIONS:
        raw = binary.get(key)
        mark = "**+1** ✅" if raw == 1 else ("**-1** ❌" if raw == -1 else "—")
        a(f"| `{key}` | {zh} | {mark} |")
    a("")
    a(f"**正向 {positive} / 7，中性 {neutral} / 7，负向 {negative} / 7。**")
    a("")
    a("## 与目标档位对比")
    a("")
    a("| 档位 | 分数 | 依据 | 本文 |")
    a("|---|---|---|---|")
    for name, th, why in TARGETS:
        ok = "✅ 达到" if isinstance(score, (int, float)) and score >= th else "❌ 未达到"
        a(f"| {name} | ≥ {th} | {why} | {ok} |")
    if isinstance(score, (int, float)):
        a("")
        if score >= 4.21:
            a(f"**结论：综合分 {score}，达到及格线 4.21，高出 {round(score - 4.21, 2)} 分。**")
        else:
            a(f"**结论：综合分 {score}，距及格线 4.21 差 {round(4.21 - score, 2)} 分。**")
    a("")
    a("---")
    a("")
    a("## 评审正文（原文，未改写）")
    a("")
    secs = rev.get("sections", {})
    for key, title in SECTION_TITLES:
        body = secs.get(key)
        if body:
            a(f"### {title}")
            a("")
            a(body.strip())
            a("")
    if secs.get("binary_scores"):
        a("### Binary scores (raw)")
        a("")
        a("```")
        a(secs["binary_scores"].strip())
        a("```")
        a("")
    a("---")
    a("")
    a("## 免责（官方自述，必须一并引用）")
    a("")
    a("- 评审由 AI 生成，**可能有错**。")
    a("- AI 与单个人类评审的 Spearman 相关仅 **0.42**；预测录取的 AUC **0.75**（低于人类评分的 0.84）。")
    a("- 该服务依托 arXiv 检索，**AI 领域更准，其他领域更不准**。")
    a("- 这是**可比的弱信号**，不是录用判决。")

    Path(args.dst).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写入 {args.dst}")
    print(f"numerical_score = {score}  正向 {positive}/7  负向 {negative}/7  中性 {neutral}/7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
