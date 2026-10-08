#!/usr/bin/env python
"""比对本次对 JiuwenSwarm 上游源码的改动与原始归档。

为什么要比对：上一轮改动里 ``config_specs.py`` 的参数字典被写成了
``= dict(`` + ``key: value`` 的字面量语法（语法非法），导致整个模块 import 失败，
而当时的测试只覆盖了 ``paper_providers`` 与 vendor 内核，没碰到 ``config_specs``，
所以这个错误一直没暴露。本脚本把改动逐行摊开，用于人工复核，并作为交付证据。

输出：
  reports/upstream_changes.diff  —— 原始归档 vs 当前工作区的统一 diff
  reports/upstream_new_files.txt —— 当前存在但归档里没有的新增文件
"""

from __future__ import annotations

import difflib
import tarfile
from pathlib import Path

ARCHIVE = Path(r"E:\存放\agent\upstream\jiuwenswarm.tar.gz")
WORKSPACE = Path(r"E:\存放\agent\upstream\jiuwenswarm-develop")
REPORTS = Path(r"E:\存放\agent\paper-swarm\reports")

#: 归档内前缀 → 工作区相对路径
PREFIX = "jiuwenswarm-develop/"


def _normalize(text: str) -> str:
    """把 CRLF 归一成 LF。

    归档里的文本是 LF，而工作区里少数文件是 CRLF（Windows 上写入造成）。若按原始字节
    比对，这类文件会整篇显示为"改动"，掩盖真正的差异；归一化后只暴露**内容**改动。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")

#: 期望被修改的既有文件（其余同名文件必须与归档逐字节一致）
MODIFIED = (
    "jiuwenswarm/agents/swarm/registry.py",
    "jiuwenswarm/agents/swarm/config_specs.py",
)

#: 只在这些目录里寻找新增文件
NEW_FILE_ROOTS = (
    "jiuwenswarm/agents/paper",
    "jiuwenswarm/agents/swarm/providers",
    "jiuwenswarm/resources/agent/workspace/skills/paper-repro-swarm",
    "tests/agents/swarm",
)


def read_archive() -> dict[str, str]:
    """把归档里的文本文件读进内存（只读 .py/.md/.yaml，避免拖入二进制）。"""
    wanted_suffixes = (".py", ".md", ".yaml", ".yml", ".toml", ".cfg")
    files: dict[str, str] = {}
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.replace("\\", "/")
            if not name.startswith(PREFIX):
                continue
            rel = name[len(PREFIX) :]
            if not rel.endswith(wanted_suffixes):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            files[rel] = _normalize(handle.read().decode("utf-8", errors="replace"))
    return files


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    original = read_archive()
    print(f"archive text files: {len(original)}")

    diff_chunks: list[str] = []
    for rel in MODIFIED:
        before = original.get(rel, "")
        after_path = WORKSPACE / rel
        after = after_path.read_text(encoding="utf-8") if after_path.exists() else ""
        diff = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        diff_chunks.append("".join(diff) if diff else f"# {rel}: NO CHANGE\n")

    # 新增文件：归档没有、工作区有。
    new_files: list[str] = []
    for root in NEW_FILE_ROOTS:
        base = WORKSPACE / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            rel = path.relative_to(WORKSPACE).as_posix()
            if rel not in original:
                new_files.append(rel)

    (REPORTS / "upstream_changes.diff").write_text(
        "\n".join(diff_chunks), encoding="utf-8"
    )
    (REPORTS / "upstream_new_files.txt").write_text(
        "\n".join(new_files) + "\n", encoding="utf-8"
    )

    print(f"modified files diffed: {len(MODIFIED)}")
    print(f"new files: {len(new_files)}")
    for rel in new_files:
        print(f"  + {rel}")

    # 兜底：确认除 MODIFIED 之外的共同文件没有被改动。
    touched: dict[str, str] = {}
    crlf_only: list[str] = []
    for rel, before in original.items():
        path = WORKSPACE / rel
        if not path.exists():
            continue
        if rel in MODIFIED:
            continue
        raw = path.read_text(encoding="utf-8", errors="replace")
        after = _normalize(raw)
        if after == before:
            if raw != before:
                crlf_only.append(rel)
            continue
        touched[rel] = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=1,
            )
        )
    if crlf_only:
        print(f"line-ending-only differences (content identical): {len(crlf_only)}")
        for rel in crlf_only:
            print(f"  ~ {rel}")
    if touched:
        print("WARNING: content drift outside the declared modified files:")
        for rel in touched:
            print(f"  ! {rel}")
        (REPORTS / "upstream_drift.diff").write_text(
            "".join(touched.values()), encoding="utf-8"
        )
    else:
        print("no content drift outside the declared modified files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
