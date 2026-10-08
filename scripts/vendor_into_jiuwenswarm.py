# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""把 PaperSwarm 内核 vendor 进 JiuwenSwarm 源码树。

为什么需要这一步
----------------
赛题要求"必须修改 JiuwenSwarm 源码"。把内核以「独立 pip 包 + 可选导入」接入，
会把能力变成"装了才有"，且上游 review 时无法判断行为；因此选择**自包含 vendor**：

* 目标目录：``jiuwenswarm/agents/paper/``（与 ``agents/harness``、``agents/swarm`` 平级，
  作为一项新能力包，而不是塞进 swarm 子包）；
* 零新增第三方依赖：内核只用到 stdlib + numpy（JiuwenSwarm 已声明）+ requests/yaml（已在依赖树内），
  重量级可选项（pymupdf / tectonic）保持惰性导入；
* 唯一改动是把包内绝对导入 ``paperswarm.x`` 改写为 ``jiuwenswarm.agents.paper.x``。

本脚本是**幂等**的：重复执行覆盖同名前缀写入，产出完全一致。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SOURCE_DIR = Path(r"E:\存放\agent\paper-swarm\src\paperswarm")
TARGET_DIR = Path(
    r"E:\存放\agent\upstream\jiuwenswarm-develop\jiuwenswarm\agents\paper"
)
REPORT_PATH = Path(r"E:\存放\agent\paper-swarm\reports\vendor_report.json")

_IMPORT_REWRITE = re.compile(r"\bpaperswarm\b")
"""匹配包名本身（不带点），覆盖两种形态：

* ``from paperswarm.x import y`` / ``:func:`paperswarm.y.z```（点号形态）；
* ``from paperswarm import x, y``（裸包名形态，第一版只改了点号形态，漏掉了这种）。
"""


def _rewrite(text: str) -> str:
    """把包内引用重写为 vendor 后的点路径。

    只替换 ``paperswarm`` 这个标识符，不动 ``PAPER_LLM_*`` 之类的环境变量名，
    也不动 ``paper-swarm`` 这类仓库路径字符串（它们不含该标识符）。
    """
    return _IMPORT_REWRITE.sub("jiuwenswarm.agents.paper", text)


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"source missing: {SOURCE_DIR}", file=sys.stderr)
        return 1

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "source": str(SOURCE_DIR),
        "target": str(TARGET_DIR),
        "files": [],
    }

    for src in sorted(SOURCE_DIR.glob("*.py")):
        original = src.read_text(encoding="utf-8")
        rewritten = _rewrite(original)
        dst = TARGET_DIR / src.name
        dst.write_text(rewritten, encoding="utf-8")
        report["files"].append(
            {
                "name": src.name,
                "bytes": len(rewritten.encode("utf-8")),
                "rewritten_refs": len(_IMPORT_REWRITE.findall(original)),
            }
        )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
