# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""``paperswarm.envfile`` 的行为验证。

不依赖 pytest：直接 ``python tests/test_envfile.py`` 即可运行全部断言，
也兼容 pytest（每个 ``test_*`` 函数都可被收集）。

这里要钉住的是三条"安全属性"，它们比功能本身更重要：

1. **已存在的环境变量必须优先** —— 否则 `.env.local` 会静默盖掉命令行
   临时导出的 key，测试与生产会跑成两套凭据。
2. **返回值里不能出现密钥值** —— 这个列表是要打日志的。
3. **缺文件不是错误** —— 本机 Ollama 路径下根本不该要求建 `.env.local`。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from paperswarm.envfile import load_env_file, parse_env_text  # noqa: E402

SECRET = "sk-unit-test-secret-do-not-log"


def test_parse_basic_forms() -> None:
    """``KEY=VALUE`` / ``export`` 前缀 / 引号 / 注释 / 非法行。"""
    text = "\n".join(
        [
            "# 注释行",
            "",
            "A=1",
            "B = 2 ",
            "export C=three",
            "D='quoted value'",
            'E="double quoted"',
            "F",  # 没有 = → 跳过
            "=z",  # 空键 → 跳过
            "1BAD=x",  # 键以数字开头 → 跳过
            "G=has#hash",  # 值里的 # 是合法字符，不截断
        ]
    )
    parsed = parse_env_text(text)
    assert parsed == {
        "A": "1",
        "B": "2",
        "C": "three",
        "D": "quoted value",
        "E": "double quoted",
        "G": "has#hash",
    }, parsed


def test_load_reads_file_and_returns_only_key_names() -> None:
    """核心路径：文件存在 → 写进 os.environ，返回值只有键名。"""
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env.local"
        env_file.write_text(f"PAPERSWARM_TEST_KEY={SECRET}\n", encoding="utf-8")

        os.environ.pop("PAPERSWARM_TEST_KEY", None)
        applied = load_env_file(env_file)

        assert applied == ["PAPERSWARM_TEST_KEY"], applied
        # 返回值里绝不能带值——这个列表是要进日志的。
        assert SECRET not in repr(applied)
        assert os.environ["PAPERSWARM_TEST_KEY"] == SECRET
        os.environ.pop("PAPERSWARM_TEST_KEY", None)


def test_existing_environment_wins() -> None:
    """已存在的环境变量优先，文件不得覆盖。"""
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env.local"
        env_file.write_text("PAPERSWARM_TEST_KEY=from-file\n", encoding="utf-8")

        os.environ["PAPERSWARM_TEST_KEY"] = "from-env"
        applied = load_env_file(env_file)

        assert applied == [], applied  # 没设置任何东西
        assert os.environ["PAPERSWARM_TEST_KEY"] == "from-env"
        os.environ.pop("PAPERSWARM_TEST_KEY", None)


def test_missing_file_is_not_an_error() -> None:
    """缺文件静默返回空列表，不抛异常。"""
    with tempfile.TemporaryDirectory() as tmp:
        assert load_env_file(Path(tmp) / ".env.local") == []


def test_base_dir_resolution() -> None:
    """``base_dir`` 下应拼出 ``.env.local``。"""
    with tempfile.TemporaryDirectory() as tmp:
        os.environ.pop("PAPERSWARM_TEST_KEY", None)
        assert load_env_file(base_dir=tmp) == []  # 空目录，什么都没设
        (Path(tmp) / ".env.local").write_text(
            "PAPERSWARM_TEST_KEY=via-base-dir\n", encoding="utf-8"
        )
        assert load_env_file(base_dir=tmp) == ["PAPERSWARM_TEST_KEY"]
        assert os.environ["PAPERSWARM_TEST_KEY"] == "via-base-dir"
        os.environ.pop("PAPERSWARM_TEST_KEY", None)


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        else:
            print(f"ok    {fn.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
