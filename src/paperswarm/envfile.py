# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""本地密钥文件加载：把 ``.env.local`` 里的 ``KEY=VALUE`` 灌进 ``os.environ``。

为什么单独一个模块
------------------
:mod:`paperswarm.llm` 的契约是"**只从环境变量读密钥**"。这个契约必须保住，
否则密钥会顺着配置文件流进版本库。但让使用者每开一个新终端都手敲
``export PAPER_LLM_API_KEY=...`` 并不现实，于是把"把文件变成环境变量"
这一步单独抽出来，且**只由入口脚本调用**——库层永远只读 ``os.environ``。

这样分层的好处是可验证：只要 ``llm.py`` 里没有出现 ``open(``/``read_text``，
就能断言它不可能从文件里拿到密钥。

安全约定（硬性）
----------------
1. 只在变量**尚不存在**时写入——已存在的环境变量优先，便于临时覆盖和测试。
2. 函数只返回**被设置的键名**列表，返回值里绝不出现值。
3. 文件缺失不是错误：静默跳过，返回空列表。
4. 值两侧的成对单/双引号会被剥掉；``export KEY=VALUE`` 前缀也接受。

用法::

    from paperswarm.envfile import load_env_file
    load_env_file(base_dir=ROOT)        # 读 ROOT/.env.local
"""

from __future__ import annotations

import os
from pathlib import Path

#: 约定文件名。``.gitignore`` 里 ``.env.*`` 已被排除。
ENV_FILE_NAME = ".env.local"


def parse_env_text(text: str) -> dict[str, str]:
    """把 dotenv 文本解析成 ``{键: 值}``。

    支持的写法::

        KEY=value
        KEY="value with spaces"
        export KEY='value'      # shell 风格前缀
        # 注释行与空行会被忽略
        KEY2=value2   # 行尾注释不解析（值里的 # 是合法字符）

    Args:
        text: 文件全文。

    Returns:
        解析出的键值对；非法行（没有 ``=``、键名含空格）直接跳过。
    """
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 键名只允许 [A-Za-z_][A-Za-z0-9_]*，避免把奇怪的东西写进环境。
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        parsed[key] = value
    return parsed


def load_env_file(
    path: Path | str | None = None,
    *,
    base_dir: Path | str | None = None,
) -> list[str]:
    """把 ``.env.local`` 里的键值对写进 ``os.environ``（不覆盖已有值）。

    Args:
        path: 显式指定的文件路径；给了就不再拼 ``base_dir``。
        base_dir: 文件所在目录；缺省用当前工作目录。

    Returns:
        **实际被设置**的键名列表（已存在的键不计入）。值永不返回。
    """
    target = Path(path) if path is not None else Path(base_dir or Path.cwd()) / ENV_FILE_NAME
    if not target.is_file():
        return []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        # 读到一半权限变了之类的边角情况：按"没这个文件"处理，不中断主流程。
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        if key in os.environ and os.environ[key]:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied
