#!/usr/bin/env python
"""从原始 tar.gz 中取出未经修改的 config_specs.py，用于比对本次改动。"""
from __future__ import annotations

import tarfile
from pathlib import Path

ARCHIVE = Path(r"E:\存放\agent\upstream\jiuwenswarm.tar.gz")
OUT = Path(r"E:\存放\agent\paper-swarm\reports\config_specs_original.py")
TARGET_SUFFIX = "agents/swarm/config_specs.py"

with tarfile.open(ARCHIVE, "r:gz") as tar:
    members = [m for m in tar.getmembers() if m.name.replace("\\", "/").endswith(TARGET_SUFFIX)]
    print(f"matched members: {[m.name for m in members]}")
    if not members:
        raise SystemExit("target not found in archive")
    handle = tar.extractfile(members[0])
    assert handle is not None
    text = handle.read().decode("utf-8")

OUT.write_text(text, encoding="utf-8")
lines = text.splitlines()
for i, line in enumerate(lines, 1):
    if "_RAIL_PARAM_BUILDERS" in line or "_TOOL_PARAM_BUILDERS" in line:
        lo = max(1, i - 1)
        hi = min(len(lines), i + 24)
        print(f"--- around line {i} ---")
        for j in range(lo, hi + 1):
            print(f"{j:5d}| {lines[j - 1]}")
