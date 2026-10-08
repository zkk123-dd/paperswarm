# 缺陷记录：PDF 里章节标题全部丢失

> 状态：**已修复并回归验证**。发现方式——编译交付前核对 PDF 书签，而不是只看"编译成功"。
> 影响范围：所有已产出的 PDF（`runs/paper-*/paper/main.pdf`）。

---

## 1. 症状

一次"把论文做成 PDF"的交付核对中，对编译产物做了书签（outline）检查：

```
pages=4  toc_entries=7
   L1 Motivation and Background   p1
   L1 Problem Formulation         p1
   L1 Experimental Setup          p1
   L1 Baseline and Noise Floor    p2
   L1 Conclusion                  p2
   L1 Experiments                 p3
   L1 Discussion                  p3
```

六个正式章节（Introduction / Related Work / Method / Experiments / Discussion /
Conclusion）里，**只有两个有标题，而且都是 `\subsection` 级**；Introduction、
Related Work、Method、Conclusion **完全没有标题**。那 5 条"Motivation and
Background / Problem Formulation / …"根本不是正式章节，是引言正文里的子标题。

更迷惑的是：`tectonic` 退出码 0、页数合规、体积合规、证据门控全过、11/11 claim
全部解析成功 —— **所有自动检查都是绿的**。编译成功不等于文档结构正确。

## 2. 根因

**同一个分工被两边都当成了对方的责任。**

代码里明确写了这个分工：

- `scripts/full_paper.py::strip_section_heading` 的 docstring 说
  「去掉模型可能自己加的 `\section{...}`（**标题由组装器统一加**）」，并且
  `SYSTEM_PROMPT` 明确要求模型「Do NOT output ... the `\section{...}` heading
  itself」；
- `paperswarm/assemble.py::resolve_sections` 的签名接受
  `sections: Sequence[tuple[str, str]]`，也就是 `(stem, title)`。

但 `resolve_sections` 的实现体写的是：

```python
for stem, _title in sections:          # ← title 拿到手就扔了
    ...
    report = resolve(src, dst, ledger, literal_warnings=True)   # ← 没传标题
```

它只做 `\result{}` 回填，**从来没写过 `\section{}`**。于是：

| 环节 | 以为标题由谁写 | 实际 |
|---|---|---|
| 写手（模型） | 组装器 | 按要求没写 |
| 组装器 | —（代码里根本没这段） | 没写 |

**这是一条只存在于注释和 docstring 里的约定，没有任何代码去实现或校验它。**
`_title` 这个带下划线的未使用变量本身就是信号：它被静默丢弃，而 Python 不会为此报错。

## 3. 为什么之前没被发现

1. **所有断言都在查"数字"，没有一个在查"结构"。** 门控解决的是"数字能不能追溯到产物"，
   它压根不关心文档长什么样。`assembly_report.json` 里 `sections` 的 `refs` /
   `resolved` 全对，因为回填确实做对了。
2. **`\subsection` 缺父级 `\section` 时会被 hyperref 提升为书签一级条目。**
   所以引言里那 5 个子标题在书签里看起来"像是"章节，肉眼扫一眼书签并不会立刻发现
   少了 Introduction。
3. **PDF 文本抽取里，`\subsection{Experiments}` 和 `\section{Experiments}` 长得几乎一样。**
   只有查书签层级（`get_toc()`）才能区分。

## 4. 修复

### 4.1 组装器独占标题（`src/paperswarm/`）

- `tex_gate.py` 新增 `SECTION_RE`，`resolve()` 增加 `heading: str | None` 参数：
  给出 `heading` 时，**先剥离正文自带的全部顶层 `\section{}`**，再在正文最前面写入
  `\section{<heading>}`。剥离动作会记进 `GateReport.warnings`，保证可观测
  （静默丢弃正是这次事故的成因，不能再犯）。
- `assemble.py::resolve_sections` 改为 `resolve(..., heading=title)`。
- `assemble.py` 新增 `assert_single_section(tex_path, title)`：断言每章**恰好一个**
  顶层 `\section`，且标题文本完全相符，否则抛 `AssemblyError`。
  把"标题对不对"变成**代码里的完成条件**，而不是注释里的约定。
  它只看非 `%` 开头的行，避免把 HEADER 注释里的字样算进去。

### 4.2 写手不得越界（`scripts/full_paper.py`）

同一批产物还暴露了第二个问题：`introduction.tex` 里有 5 个 `\subsection`，标题分别是
Motivation and Background / Problem Formulation / **Experimental Setup** /
Baseline and Noise Floor / **Conclusion** —— 也就是把其它章节的内容塞进了引言。
（`experiments.tex` 里还有个 `\subsection{Experiments}`，标题与本章同名，纯冗余。）

修复：

- `SYSTEM_PROMPT` 去掉「You MAY use `\subsection{...}` for sub-structure」，
  改为**明确禁止 `\subsection`**，并要求"只写本章内容，引言里不得出现实验设置或
  Conclusion 段落"。
- 内核新增 `assemble.py::check_section_prose(text, section_title)`：检测
  ①正文自带 `\section`、②子标题撞上正式章节名、③正文为空。
  返回的问题清单与证据门控的问题清单**合并进同一个重写循环** —— 两者都意味着
  "这一章不能用"。

## 5. 验证

### 5.1 回归测试（`tests/test_kernel_offline.py`，新增 `test_section_heading_structure`）

16 条新断言，覆盖：标题注入、恰好一个、不是 `\subsection`、正文自带 `\section` 被剥离
且留痕、标题排在正文之前、`assert_single_section` 对缺标题 / 错标题 / 双标题**确实会拒绝**
（一个从不拒绝的断言和没有断言等价）、越界检测的四种情形。
离线总数 **80 → 96**。

### 5.2 真实产物复跑（不调用模型）

新增 `scripts/rebuild_from_run.py`：拿历史 run 的草稿 + 台账重新组装编译。用同一份
**真实草稿**复跑，证明修复在真实链路上生效，而不是只在合成样本上生效：

| | 修复前 | 只修组装器（旧草稿重装） | 修正提示词后重新生成 |
|---|---|---|---|
| 书签一级条目 | 7 条，5 条是假章节 | **6 条正确** | **6 条正确** |
| Introduction 标题 | 无 | 有 | 有 |
| Method 标题 | 无 | 有 | 有 |
| 残留越界子标题 | 5 个 | 5 个（旧草稿） | **0 个** |
| claim 全部解析 | 11/11 | 11/11 | 11/11 |

产出：

- 修复前 PDF（归档）：`runs/paper-20260929-161913/paper/main.pdf`
  → 也另存为 `output/PaperSwarm-ICLR2026-before-heading-fix.pdf`
- 只修组装器的中间验证：`runs/paper-20260929-161913/paper_rebuilt/main.pdf`
- **最终交付版**：`runs/paper-20260929-164239/paper/main.pdf`
  → 也另存为 `output/PaperSwarm-ICLR2026.pdf`

### 5.3 数值溯源未被破坏

修复只动标题与结构，不碰数值。核对最终 PDF 的正文文本：台账 11 条 claim 的值
**11/11 原样出现**在 PDF 中（`0.6198 / 0.1329 / 0.3041 / 0.0623 / 50.94 / 0.1 /
0.1225 / 8 / 120 / 90 / 200`）。

### 5.4 全量回归

见 `reports/verification_summary.txt`：py_compile 全通过、台账 9/9、内核 **96/96**、
vendor 14/14、替身契约 17 passed、Swarmflow dry-run 16/16、提交前自检 8/8。
新增的 `assert_single_section` 在真实组装路径上生效（6 章全部通过断言）。

## 6. 教训（已写入 `agent-evidence-gating` skill）

1. **注释里的约定等于没有约定。** 分工写在哪一方都不重要，重要的是**有一方在代码里
   强制它**。带下划线的未使用参数（`_title`）是这种"约定无人实现"的典型信号。
2. **门控查什么，就只保证什么。** 证据门控保证了数字可信，于是结构烂掉也没人发现。
   每一类产物属性（数字、结构、引用、页数）都需要各自的断言，不能指望一类断言覆盖。
3. **"编译成功"不是"文档正确"。** LaTeX 对缺标题完全无感 —— 退出码 0、页数合规、
   体积合规。必须查语义层（书签层级），不能只查构建层。
4. **修一处结构要同时修生成端和组装端。** 只修组装器能让旧草稿产出正确标题，但
   越界内容还在（因为那是写手的问题）。两端都要堵。
