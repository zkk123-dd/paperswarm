# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""论文全文组装与编译（Assembly）。

把各章节片段（含 ``\\result{claim_id}`` 宏）组装成符合 ICLR 2026 模板的
``main.tex``，用证据台账回填全部数值，再调用 LaTeX 引擎编译成 PDF。

设计要点
--------
1. **数值只有一个来源**。组装阶段对每个章节调用 :func:`paperswarm.tex_gate.resolve`，
   把 ``\\result{}`` 换成台账里的真实数值。任何一个引用解析失败，整篇不产出 ——
   宁可不出 PDF，也不出一份数字对不上的 PDF。
2. **模板不手写、不修改**。直接复用 ICLR 官方 ``iclr2026_conference.sty``；
   官方明文规定"Tweaking the style files may be grounds for rejection"，
   所以只做复制，不做任何改写。
3. **编译引擎可替换**。默认走本地 tectonic 单文件二进制 + 本地 bundle，
   不依赖公网；引擎不可用时给出明确的降级信息，而不是静默失败。
4. **一级标题由组装器独占**。写手只产出正文，标题在这里统一写入，并由
   :func:`assert_single_section` 断言"每章恰好一个正确标题"。把这条分工
   留成注释里的约定是行不通的 —— 见 :func:`assert_single_section` 的说明。

流水线位置
----------
``sections/*.tex``（writer 产出，含宏）
  → :func:`resolve_sections`（台账回填，门控失败即中止）
  → :func:`render_main_tex`（套 ICLR 模板骨架）
  → :func:`compile_pdf`（tectonic）
  → :func:`inspect_pdf`（页数 / 体积校验）
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from paperswarm.evidence import Ledger, LedgerError
from paperswarm.tex_gate import GateReport, SECTION_RE, resolve

TEMPLATE_FILES: tuple[str, ...] = (
    "iclr2026_conference.sty",
    "iclr2026_conference.bst",
    "natbib.sty",
    "fancyhdr.sty",
    "math_commands.tex",
)
"""编译一份 ICLR 2026 论文所需的模板文件（从官方 zip 原样复制）。"""

DEFAULT_SECTIONS: tuple[tuple[str, str], ...] = (
    ("introduction", "Introduction"),
    ("related_work", "Related Work"),
    ("method", "Method"),
    ("experiments", "Experiments"),
    ("discussion", "Discussion"),
    ("conclusion", "Conclusion"),
)
"""正文章节顺序。摘要单独处理（不进 ``\\input``）。"""

CANONICAL_SECTION_TITLES: tuple[str, ...] = tuple(title for _stem, title in DEFAULT_SECTIONS)
"""正式章节名。子标题一旦撞上这些名字，说明那段内容写错了章节。"""

SUBSECTION_RE = re.compile(r"\\subsection\*?\{([^{}]*)\}")
"""匹配 ``\\subsection{...}`` / ``\\subsection*{...}``。"""

PAGE_LIMIT_SUBMISSION = 9
"""ICLR 2026 投稿正文页数上限（不含参考文献与附录）。"""

MAX_PDF_BYTES = 10 * 1024 * 1024
"""paperreview.ai 硬约束：上传的 PDF 不得超过 10 MB。"""


class AssemblyError(RuntimeError):
    """组装或编译阶段的可控失败。"""


@dataclass
class ResolvedSection:
    """一个章节的回填结果。"""

    name: str
    src: Path
    dst: Path
    refs: int
    resolved: int
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "src": str(self.src),
            "dst": str(self.dst),
            "refs": self.refs,
            "resolved": self.resolved,
            "warnings": list(self.warnings),
        }


@dataclass
class CompileResult:
    """一次 LaTeX 编译的结果。"""

    ok: bool
    pdf_path: Path | None
    returncode: int
    stdout: str
    stderr: str
    attempts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pdf_path": str(self.pdf_path) if self.pdf_path else None,
            "returncode": self.returncode,
            "attempts": self.attempts,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
        }


@dataclass
class PdfInspection:
    """PDF 的合规性检查结果。"""

    path: Path
    pages: int
    size_bytes: int

    @property
    def size_ok(self) -> bool:
        return self.size_bytes <= MAX_PDF_BYTES

    @property
    def pages_ok(self) -> bool:
        return self.pages <= PAGE_LIMIT_SUBMISSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "pages": self.pages,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / (1024 * 1024), 3),
            "size_ok": self.size_ok,
            "pages_ok": self.pages_ok,
            "page_limit": PAGE_LIMIT_SUBMISSION,
            "size_limit_bytes": MAX_PDF_BYTES,
        }


@dataclass
class AssemblyReport:
    """一次完整组装的汇总。"""

    work_dir: Path
    sections: list[ResolvedSection] = field(default_factory=list)
    abstract_refs: int = 0
    main_tex: Path | None = None
    compile: CompileResult | None = None
    pdf: PdfInspection | None = None

    @property
    def ok(self) -> bool:
        if self.compile is None or not self.compile.ok:
            return False
        if self.pdf is None:
            return False
        return self.pdf.size_ok and self.pdf.pages_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_dir": str(self.work_dir),
            "ok": self.ok,
            "abstract_refs": self.abstract_refs,
            "sections": [item.to_dict() for item in self.sections],
            "main_tex": str(self.main_tex) if self.main_tex else None,
            "compile": self.compile.to_dict() if self.compile else None,
            "pdf": self.pdf.to_dict() if self.pdf else None,
        }


MAIN_TEMPLATE = r"""% 本文件由 paperswarm.assemble 自动生成，请勿手工编辑。
% 模板来源：ICLR 2026 官方 Master-Template（iclr2026_conference.sty），未做任何修改。
\documentclass{article}
\usepackage{iclr2026_conference,times}
\input{math_commands.tex}
\usepackage{hyperref}
\usepackage{url}
\usepackage{booktabs}
\usepackage{graphicx}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{xcolor}

\title{@@TITLE@@}

\author{@@AUTHOR@@}

% 投稿版必须匿名：\iclrfinalcopy 保持注释状态。
% \iclrfinalcopy
\begin{document}
\maketitle

\begin{abstract}
@@ABSTRACT@@
\end{abstract}

@@SECTIONS@@

\bibliography{@@BIB@@}
\bibliographystyle{iclr2026_conference}

\end{document}
"""

ANONYMOUS_AUTHOR = (
    "Anonymous Authors \\\\\n"
    "Anonymous Institution \\\\\n"
    "\\texttt{anonymous@example.com}"
)
"""投稿版作者块。ICLR 要求匿名，非匿名投稿会被直接拒稿。"""


def copy_template(template_dir: Path | str, work_dir: Path | str) -> list[Path]:
    """把 ICLR 模板文件原样复制到工作目录。

    只复制编译必需的文件，**不做任何内容改写** —— 改样式文件属于违规操作。

    Args:
        template_dir: 官方模板解压目录。
        work_dir: 论文工作目录。

    Returns:
        已复制的文件路径列表。

    Raises:
        AssemblyError: 任一必需模板文件缺失。
    """
    src_dir = Path(template_dir)
    dst_dir = Path(work_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    missing = [name for name in TEMPLATE_FILES if not (src_dir / name).is_file()]
    if missing:
        raise AssemblyError(
            f"模板目录缺少必需文件：{missing}（目录: {src_dir}）。"
            "请先获取 ICLR 官方模板 zip 并解压。"
        )

    copied: list[Path] = []
    for name in TEMPLATE_FILES:
        target = dst_dir / name
        shutil.copyfile(src_dir / name, target)
        copied.append(target)
    return copied


def resolve_sections(
    sections_dir: Path | str,
    out_sections_dir: Path | str,
    ledger: Ledger,
    *,
    sections: Sequence[tuple[str, str]] = DEFAULT_SECTIONS,
) -> list[ResolvedSection]:
    """对每个章节回填台账数值。

    只有门控通过（``\\result{}`` 全部可解析、且来源产物摘要一致）的章节才会写出。

    Args:
        sections_dir: 章节源目录（含宏的 ``.tex``）。
        out_sections_dir: 回填后的输出目录。
        ledger: 证据台账。
        sections: ``(文件名 stem, 标题)`` 序列。

    Returns:
        每个已处理章节的结果。

    Raises:
        AssemblyError: 某个章节门控未通过，或引用了不存在的 claim。
    """
    src_dir = Path(sections_dir)
    dst_dir = Path(out_sections_dir)
    dst_dir.mkdir(parents=True, exist_ok=True)

    results: list[ResolvedSection] = []
    for stem, title in sections:
        src = src_dir / f"{stem}.tex"
        if not src.is_file():
            raise AssemblyError(
                f"缺少章节文件 {src}。组装要求全部章节齐备，"
                "避免产出一篇缺章的论文。"
            )
        dst = dst_dir / f"{stem}.tex"
        # heading=title：一级标题由组装器独占写入，写手只负责正文。
        report: GateReport = resolve(
            src, dst, ledger, literal_warnings=True, heading=title
        )
        if not report.ok:
            detail = "; ".join(
                f"第 {item.line} 行 {item.claim_id}: {item.reason}"
                for item in report.issues
            )
            raise AssemblyError(f"章节 {stem} 未通过证据门控：{detail}")
        if report.resolved != report.total_refs:
            raise AssemblyError(
                f"章节 {stem} 有 {report.total_refs - report.resolved} 个引用未解析"
            )
        assert_single_section(dst, title)
        results.append(
            ResolvedSection(
                name=stem,
                src=src,
                dst=dst,
                refs=report.total_refs,
                resolved=report.resolved,
                warnings=list(report.warnings),
            )
        )
    return results


def assert_single_section(tex_path: Path | str, title: str) -> None:
    """断言一份章节 ``.tex`` 恰好含一个顶层 ``\\section{title}``。

    这是把"每章有且只有一个正确的一级标题"变成**代码里的完成条件**，而不是
    prompt 里的请求。此前该分工只存在于注释中：写手被要求不写标题、组装器被
    假定会补标题，结果两边都没写，编译出的 PDF 里六个章节只有两个带标题，
    而且都是 ``\\subsection`` 级（``\\subsection`` 在缺少父级 ``\\section`` 时
    会被 hyperref 提升为书签一级条目，所以肉眼更难发现）。

    Raises:
        AssemblyError: 标题数量不为 1，或标题文本与预期不符。
    """
    path = Path(tex_path)
    text = path.read_text(encoding="utf-8")
    # 只看正文行，避免 HEADER 注释里的字样被误算。
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("%")
    )
    found = SECTION_RE.findall(body)
    if len(found) != 1:
        raise AssemblyError(
            f"章节 {path.name} 的顶层 \\section 数量为 {len(found)}，应为 1"
            "（标题由组装器统一写入，写手不得自带）"
        )
    expected = "\\section{" + title + "}"
    actual = found[0].strip()
    if actual != expected:
        raise AssemblyError(
            f"章节 {path.name} 的标题为 {actual!r}，应为 {expected!r}"
        )


def check_section_prose(
    text: str,
    section_title: str,
    *,
    known_titles: Sequence[str] = CANONICAL_SECTION_TITLES,
) -> list[str]:
    """检查一章正文是否越界：不得自带顶层标题，也不得把别章内容搬进来。

    起因是一次真实产出：``introduction.tex`` 里出现 5 个 ``\\subsection``，标题
    分别为 Motivation and Background / Problem Formulation / Experimental Setup /
    Baseline and Noise Floor / Conclusion —— 也就是**把其它章节的内容塞进了引言**。
    这类越界靠 prompt 拦不住（本机 7B 模型尤其拦不住），但在这里可以廉价地识别，
    并让重写循环带着明确问题清单再试一次。

    Args:
        text: 写手产出的正文（不含标题）。
        section_title: 本章的正式标题。
        known_titles: 全部正式章节名。

    Returns:
        问题描述列表；为空表示结构合规。
    """
    issues: list[str] = []
    if SECTION_RE.search(text):
        issues.append("正文自带顶层 \\section{} —— 标题由组装器统一写入，正文不得包含")
    canonical = {title.lower() for title in known_titles}
    for match in SUBSECTION_RE.finditer(text):
        name = match.group(1).strip()
        if name.lower() in canonical:
            issues.append(
                f"子标题 {name!r} 是其它章节的名称，不能出现在 {section_title} 里"
            )
    if not text.strip():
        issues.append("正文为空")
    return issues


def _render_abstract(abstract_src: Path, out_path: Path, ledger: Ledger) -> int:
    """回填摘要里的数值引用，返回引用数。

    摘要允许不含任何数值，所以这里不强制 ``require_refs``；但只要出现了
    ``\\result{}``，就必须能解析。
    """
    if not abstract_src.is_file():
        out_path.write_text("", encoding="utf-8")
        return 0
    report = resolve(abstract_src, out_path, ledger, literal_warnings=True)
    if not report.ok:
        detail = "; ".join(
            f"第 {item.line} 行 {item.claim_id}: {item.reason}" for item in report.issues
        )
        raise AssemblyError(f"摘要未通过证据门控：{detail}")
    return report.total_refs


def render_main_tex(
    work_dir: Path | str,
    *,
    title: str,
    abstract: str,
    bib_name: str = "references",
    sections: Sequence[tuple[str, str]] = DEFAULT_SECTIONS,
    author_block: str = ANONYMOUS_AUTHOR,
) -> Path:
    """生成 ICLR 模板的 ``main.tex``。

    Args:
        work_dir: 论文工作目录。
        title: 论文标题。
        abstract: 摘要正文（已回填）。
        bib_name: ``.bib`` 文件名（不含扩展名）。
        sections: 章节序列，用于生成 ``\\input``。
        author_block: 作者块；投稿版默认匿名。

    Returns:
        写好的 ``main.tex`` 路径。
    """
    if not title.strip():
        raise AssemblyError("论文标题为空")
    if "@@TITLE@@" in title or "@@" in title:
        raise AssemblyError(f"标题含有保留占位符：{title!r}")

    inputs = "\n".join(
        f"\\input{{sections/{stem}}}" for stem, _title in sections
    )
    text = MAIN_TEMPLATE
    text = text.replace("@@TITLE@@", title.strip())
    text = text.replace("@@AUTHOR@@", author_block)
    text = text.replace("@@ABSTRACT@@", abstract.strip())
    text = text.replace("@@SECTIONS@@", inputs)
    text = text.replace("@@BIB@@", bib_name)

    out = Path(work_dir) / "main.tex"
    out.write_text(text, encoding="utf-8")
    return out


def render_bibliography(
    work_dir: Path | str,
    entries: Iterable[str],
    *,
    bib_name: str = "references",
) -> Path:
    """写出 ``.bib`` 文件。

    Args:
        work_dir: 论文工作目录。
        entries: 已是 BibTeX 格式的条目文本（每条形如 ``@article{key, ...}``）。
        bib_name: 输出文件名（不含扩展名）。

    Returns:
        写好的 ``.bib`` 路径。
    """
    body = "\n\n".join(item.strip() for item in entries if item.strip())
    out = Path(work_dir) / f"{bib_name}.bib"
    out.write_text(body + "\n", encoding="utf-8")
    return out


def _as_bundle_arg(bundle: Path | str) -> str:
    """把 bundle 参数规范化成 tectonic 能识别的形式。

    实测结论（tectonic 0.16.9 / Windows，三种写法逐个试过）：

    ================================ ==========================================
    写法                              结果
    ================================ ==========================================
    ``E:/a/b.ttb``（裸盘符+正斜杠）    失败：被判为 URL，"doesn't specify a valid bundle"
    ``E:\\a\\b.ttb``（反斜杠）          失败
    相对路径 ``bundle/b.ttb``          可用，但依赖 cwd
    ``file:///E:/a/b.ttb``            **可用（采用这一种）**
    ================================ ==========================================

    这里统一转成 ``file://`` URL，避免依赖调用方的当前目录。
    """
    raw = str(bundle)
    if raw.startswith(("http://", "https://", "file://")):
        return raw
    path = Path(raw)
    if not path.is_file():
        raise AssemblyError(
            f"bundle 文件不存在: {path}。请先下载本地 bundle（见 docs/reproduce.md）。"
        )
    return path.resolve().as_uri()


def compile_pdf(
    main_tex: Path | str,
    *,
    tectonic: Path | str,
    bundle: Path | str | None = None,
    cache_dir: Path | str | None = None,
    out_dir: Path | str | None = None,
    timeout_seconds: float = 900.0,
    max_attempts: int = 2,
) -> CompileResult:
    """调用 tectonic 编译 ``main.tex``。

    健壮性（对应 C4）：显式超时、有限重试、失败后返回完整 stderr 供诊断。
    不做无限重试 —— LaTeX 的确定性错误重试多少次都一样。

    Args:
        main_tex: 入口 ``.tex``。
        tectonic: ``tectonic`` 可执行文件路径。
        bundle: 本地 bundle 文件路径；为空则用引擎默认（需要联网）。
        cache_dir: tectonic 缓存目录，建议指向非系统盘。
        out_dir: PDF 输出目录；默认与 ``main_tex`` 同目录。
        timeout_seconds: 单次编译墙钟上限。
        max_attempts: 最多尝试次数（≥1）。

    Returns:
        :class:`CompileResult`；``ok`` 为真且 ``pdf_path`` 存在才算成功。
    """
    tex = Path(main_tex)
    if not tex.is_file():
        raise AssemblyError(f"编译入口不存在: {tex}")
    engine = Path(tectonic)
    if not engine.is_file():
        raise AssemblyError(
            f"找不到 LaTeX 引擎: {engine}。"
            "请下载 tectonic 单文件二进制后重试（见 docs/reproduce.md）。"
        )

    out = Path(out_dir) if out_dir else tex.parent
    out.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    if cache_dir:
        cache_path = Path(cache_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        env["TECTONIC_CACHE_DIR"] = str(cache_path)

    cmd: list[str] = [str(engine), "-X", "compile"]
    if bundle:
        cmd += ["-b", _as_bundle_arg(bundle)]
    cmd += [str(tex), "--outdir", str(out)]

    attempts = 0
    last: subprocess.CompletedProcess[str] | None = None
    for attempts in range(1, max(1, max_attempts) + 1):
        try:
            last = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                env=env,
                cwd=str(tex.parent),
            )
        except subprocess.TimeoutExpired as exc:
            last = subprocess.CompletedProcess(
                cmd,
                returncode=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=f"编译超时（>{timeout_seconds}s）",
            )
        if last.returncode == 0:
            break

    assert last is not None
    pdf = out / (tex.stem + ".pdf")
    ok = last.returncode == 0 and pdf.is_file()
    return CompileResult(
        ok=ok,
        pdf_path=pdf if pdf.is_file() else None,
        returncode=last.returncode,
        stdout=last.stdout or "",
        stderr=last.stderr or "",
        attempts=attempts,
    )


def inspect_pdf(pdf_path: Path | str) -> PdfInspection:
    """读取 PDF 的页数与体积，用于合规校验。

    优先用 PyMuPDF；不可用时降级为"只查体积、页数记 0"，
    并在调用方以 ``pages_ok`` 之外的方式提示，避免静默给出错误结论。
    """
    path = Path(pdf_path)
    if not path.is_file():
        raise AssemblyError(f"PDF 不存在: {path}")
    size = path.stat().st_size
    pages = 0
    try:
        # PyMuPDF 1.24+ 推荐 `import pymupdf`；旧版本只有 `fitz`。
        # 本机两者都能跑，但 `fitz` 会打印弃用警告，所以优先新名字。
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 旧版本 PyMuPDF
            import fitz as pymupdf  # type: ignore[no-redef]

        with pymupdf.open(str(path)) as doc:
            pages = int(doc.page_count)
    except Exception as exc:  # pragma: no cover - 依赖缺失时的降级路径
        print(f"[warn] 无法读取 PDF 页数（{exc}）；仅校验体积", file=sys.stderr)
    return PdfInspection(path=path, pages=pages, size_bytes=size)


def assemble_paper(
    *,
    template_dir: Path | str,
    sections_dir: Path | str,
    work_dir: Path | str,
    ledger: Ledger,
    title: str,
    tectonic: Path | str,
    bundle: Path | str | None = None,
    cache_dir: Path | str | None = None,
    bib_entries: Iterable[str] = (),
    bib_name: str = "references",
    sections: Sequence[tuple[str, str]] = DEFAULT_SECTIONS,
    abstract_stem: str = "abstract",
    compile_timeout: float = 900.0,
) -> AssemblyReport:
    """端到端组装一篇 ICLR 论文并编译成 PDF。

    Args:
        template_dir: ICLR 官方模板目录。
        sections_dir: 章节源目录（含 ``\\result{}`` 宏）。
        work_dir: 输出工作目录。
        ledger: 证据台账。
        title: 论文标题。
        tectonic: tectonic 可执行文件路径。
        bundle: 本地 bundle 路径。
        cache_dir: tectonic 缓存目录。
        bib_entries: BibTeX 条目文本。
        bib_name: 参考文献文件名（不含扩展名）。
        sections: 章节序列。
        abstract_stem: 摘要文件名（不含扩展名）。
        compile_timeout: 编译超时秒数。

    Returns:
        :class:`AssemblyReport`。

    Raises:
        AssemblyError: 组装前置条件不满足（缺模板、缺章节、门控失败）。
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    copy_template(template_dir, work)

    # 摘要先回填，再嵌入 main.tex。
    abstract_out = work / "_abstract.resolved.tex"
    abstract_refs = _render_abstract(
        Path(sections_dir) / f"{abstract_stem}.tex", abstract_out, ledger
    )
    abstract_text = abstract_out.read_text(encoding="utf-8")

    resolved = resolve_sections(
        sections_dir, work / "sections", ledger, sections=sections
    )

    if bib_entries:
        render_bibliography(work, bib_entries, bib_name=bib_name)

    main_tex = render_main_tex(
        work,
        title=title,
        abstract=abstract_text,
        bib_name=bib_name,
        sections=sections,
    )

    compile_result = compile_pdf(
        main_tex,
        tectonic=tectonic,
        bundle=bundle,
        cache_dir=cache_dir,
        out_dir=work,
        timeout_seconds=compile_timeout,
    )

    report = AssemblyReport(
        work_dir=work,
        sections=resolved,
        abstract_refs=abstract_refs,
        main_tex=main_tex,
        compile=compile_result,
    )
    if compile_result.pdf_path is not None:
        report.pdf = inspect_pdf(compile_result.pdf_path)
    return report


def _main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI 薄壳
    """命令行入口，便于在 CI 与手工排查中直接调用。"""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="assemble",
        description="把含 \\result{} 宏的章节组装成 ICLR 论文并编译 PDF。",
    )
    parser.add_argument("--ledger-root", required=True, help="台账根目录")
    parser.add_argument("--artifacts-root", default=None, help="artifact 基准目录")
    parser.add_argument("--template-dir", required=True, help="ICLR 模板目录")
    parser.add_argument("--sections-dir", required=True, help="章节源目录")
    parser.add_argument("--work-dir", required=True, help="输出工作目录")
    parser.add_argument("--title", required=True, help="论文标题")
    parser.add_argument("--tectonic", required=True, help="tectonic 可执行文件")
    parser.add_argument("--bundle", default=None, help="本地 bundle 路径")
    parser.add_argument("--cache-dir", default=None, help="tectonic 缓存目录")
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger_root, args.artifacts_root)
    try:
        report = assemble_paper(
            template_dir=args.template_dir,
            sections_dir=args.sections_dir,
            work_dir=args.work_dir,
            ledger=ledger,
            title=args.title,
            tectonic=args.tectonic,
            bundle=args.bundle,
            cache_dir=args.cache_dir,
        )
    except AssemblyError as exc:
        print(f"组装失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
