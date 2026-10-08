# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""证据层端到端验证。

不依赖 pytest：直接 ``python tests/test_evidence.py`` 即可运行全部断言。
也兼容 pytest（每个 ``test_*`` 函数都可被收集）。

覆盖的场景就是我们要对评审宣称的能力：
- 正常登记 → 校验通过；
- 声明值与产物不一致 → 直接拒收；
- 正文引用已登记 claim → 门控通过并回填数值；
- **实验结果被事后改写 → 门控失败**（防伪造的核心）；
- 引用不存在的 claim → 门控失败；
- 正文自行重定义 ``\\result`` 宏 → 门控失败；
- 产物文件丢失 → 门控失败。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from paperswarm.evidence import Ledger, LedgerError  # noqa: E402
from paperswarm.tex_gate import gate_text, resolve  # noqa: E402

METRICS = {
    "results": {
        "seed42": {"val_ppl": 16.81, "baseline_ppl": 17.34},
        "seed43": {"val_ppl": 16.79, "baseline_ppl": 17.31},
    },
    "config": {"window": 256, "model": "gemma-3-4b"},
}

TEX_OK = r"""
\section{Results}
Our method achieves a validation perplexity of \result{c-ppl} at $W{=}256$,
compared with \result{c-base} for the baseline.
"""


def _setup(tmp: Path) -> tuple[Ledger, Path]:
    """建一个最小可用的台账与一个 JSON 实验产物。"""
    artifacts = tmp / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    metrics = artifacts / "metrics.json"
    metrics.write_text(json.dumps(METRICS), encoding="utf-8")

    ledger = Ledger(tmp / "ledger", artifacts_root=artifacts)
    artifact = ledger.register_artifact(metrics, producer="engineer/run-train.py")
    ledger.add_claim(
        text="GateTune 在 W=256 下把验证集困惑度降到 16.81",
        artifact_id=artifact.artifact_id,
        locator="/results/seed42/val_ppl",
        claim_id="c-ppl",
        run_id="run-demo-001",
    )
    ledger.add_claim(
        text="同配置下基线的验证集困惑度为 17.34",
        artifact_id=artifact.artifact_id,
        locator="/results/seed42/baseline_ppl",
        claim_id="c-base",
        run_id="run-demo-001",
    )
    return ledger, metrics


def test_register_and_verify_pass() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, _ = _setup(Path(raw))
        report = ledger.verify()
        assert report.total == 2, report.total
        assert report.ok, report.to_dict()


def test_claim_value_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, _ = _setup(Path(raw))
        artifact_id = ledger.artifacts()[0].artifact_id
        try:
            ledger.add_claim(
                text="错误声明值应被拒收",
                artifact_id=artifact_id,
                locator="/results/seed42/val_ppl",
                value=1.234,
                claim_id="c-bad",
            )
        except LedgerError as exc:
            assert "不一致" in str(exc), exc
            return
        raise AssertionError("声明值与产物不一致时应当拒收")


def test_claim_requires_registered_artifact() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, _ = _setup(Path(raw))
        try:
            ledger.add_claim(
                text="无来源数字",
                artifact_id="a-not-exist",
                locator="/results/seed42/val_ppl",
                claim_id="c-ghost",
            )
        except LedgerError as exc:
            assert "未登记" in str(exc), exc
            return
        raise AssertionError("引用未登记 artifact 时应当拒收")


def test_gate_passes_and_injects_values() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        ledger, _ = _setup(tmp)
        report = gate_text(TEX_OK, ledger)
        assert report.total_refs == 2, report.to_dict()
        assert report.ok, report.to_dict()

        src = tmp / "paper.tex"
        out = tmp / "paper.resolved.tex"
        src.write_text(TEX_OK, encoding="utf-8")
        resolved = resolve(src, out, ledger)
        assert resolved.ok, resolved.to_dict()
        rendered = out.read_text(encoding="utf-8")
        assert "16.81" in rendered and "17.34" in rendered, rendered
        assert "\\result{" not in rendered, rendered


def test_tampered_artifact_blocks_gate() -> None:
    """核心场景：实验结果被事后改写，门控必须拦住。"""
    with tempfile.TemporaryDirectory() as raw:
        ledger, metrics = _setup(Path(raw))
        assert gate_text(TEX_OK, ledger).ok

        tampered = json.loads(metrics.read_text(encoding="utf-8"))
        tampered["results"]["seed42"]["val_ppl"] = 1.02
        metrics.write_text(json.dumps(tampered), encoding="utf-8")

        report = gate_text(TEX_OK, ledger)
        assert not report.ok, "产物被改写后门控必须失败"
        reasons = " ".join(issue.reason for issue in report.issues)
        assert "已被修改" in reasons, reasons


def test_unknown_claim_id_blocks_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, _ = _setup(Path(raw))
        report = gate_text(r"Value is \result{c-does-not-exist}.", ledger)
        assert not report.ok
        assert "不存在" in report.issues[0].reason, report.issues[0].reason


def test_macro_redefinition_blocks_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, _ = _setup(Path(raw))
        tex = r"\newcommand{\result}[1]{42}" + TEX_OK
        report = gate_text(tex, ledger)
        assert not report.ok
        assert report.macro_redefined, report.to_dict()


def test_missing_artifact_file_blocks_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        ledger, metrics = _setup(Path(raw))
        metrics.unlink()
        report = gate_text(TEX_OK, ledger)
        assert not report.ok
        assert "不存在" in report.issues[0].reason, report.issues[0].reason


def test_literal_number_warning_is_not_blocking() -> None:
    """手写数字只告警不拦截：它的定位是提示人工复核，硬约束靠 \\result 强制引用。"""
    from paperswarm.tex_gate import _check_literal_numbers

    hits = _check_literal_numbers("Our method improves accuracy by 3.7 percent.")
    assert hits, "应当检出结果语境附近的手写数字"


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, func in tests:
        try:
            func()
        except Exception as exc:  # noqa: BLE001 - 这是测试驱动器，需要打印全部失败
            failed += 1
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS  {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
