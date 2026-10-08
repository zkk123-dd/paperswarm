# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""探测运行校验器所需的依赖是否可用。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

MODULES = ("numpy", "yaml", "requests", "pymupdf", "fitz", "pytest", "paperswarm")
OUT = Path(r"E:\存放\agent\paper-swarm\reports\dep_probe.json")


def main() -> int:
    found = {name: bool(importlib.util.find_spec(name)) for name in MODULES}
    payload = {"python": sys.version, "executable": sys.executable, "modules": found}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
