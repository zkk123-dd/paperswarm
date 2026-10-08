# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""校验 vendor 进 JiuwenSwarm 源码树的 ``jiuwenswarm.agents.paper`` 内核。

为什么需要这个校验器
--------------------
完整跑 JiuwenSwarm 的测试套件需要装好它与 openjiuwen 的整棵依赖树（含 fastapi、
playwright 等），属于 CI 的活。但"vendor 有没有做对"这件事**不依赖框架**：

* 改写是否正确 → 静态审计（语法 + AST 导入解析 + 依赖白名单）；
* std 包能否真实导入 → 用**合成父包**把 ``jiuwenswarm.agents.paper`` 直接装进
  ``sys.modules``，绕开 ``jiuwenswarm/__init__.py`` 的重依赖，只加载本包；
* 功能是否与上游一致 → 真实跑一遍实验 → 登账 → 门控 → 回填，并额外验证
  **产物被篡改后台账必须报错**（SHA-256 锚点是否真的在起作用）。

产出 ``reports/vendor_verify.json``，逐项给出 passed/failed 与证据。
退出码 0 表示全部通过。
"""

from __future__ import annotations

import ast
import importlib
import json
import py_compile
import sys
import tempfile
import types
from pathlib import Path

UPSTREAM_ROOT = Path(r"E:\存放\agent\upstream\jiuwenswarm-develop")
PKG_DIR = UPSTREAM_ROOT / "jiuwenswarm" / "agents" / "paper"
REPORT_PATH = Path(r"E:\存放\agent\paper-swarm\reports\vendor_verify.json")

PACKAGE_NAME = "jiuwenswarm.agents.paper"

#: 允许出现在本包里的第三方顶层依赖。全部是 JiuwenSwarm 已声明或依赖树内已有的包；
#: pymupdf 只在 PDF 页数校验时惰性导入。
ALLOWED_THIRD_PARTY = frozenset({"numpy", "yaml", "requests", "pymupdf", "fitz"})
#: 允许引用的自身命名空间根。
ALLOWED_FIRST_PARTY = frozenset({"jiuwenswarm"})

STDLIB = frozenset(sys.stdlib_module_names)


class _Checks:
    """收集检查结果，任一失败即整体失败。"""

    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def add(self, name: str, ok: bool, detail: object) -> None:
        self.items.append({"check": name, "passed": bool(ok), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(bool(item["passed"]) for item in self.items)


def _module_roots(tree: ast.AST) -> list[str]:
    """收集文件里所有绝对导入的顶层模块名（相对导入与自身导入不计）。"""
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入，跳过
                continue
            if node.module:
                roots.append(node.module.split(".")[0])
    return roots


def _intra_package_refs(tree: ast.AST) -> list[str]:
    """收集指向本包子模块的绝对导入目标（形如 ``jiuwenswarm.agents.paper.x``）。"""
    refs: list[str] = []
    prefix = PACKAGE_NAME + "."
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(prefix):
                refs.append(node.module[len(prefix):])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    refs.append(alias.name[len(prefix):])
    return refs


def run_static_audit(checks: _Checks) -> dict[str, ast.AST]:
    sources = sorted(PKG_DIR.glob("*.py"))
    checks.add("package_files_found", bool(sources), [p.name for p in sources])

    trees: dict[str, ast.AST] = {}
    syntax_ok = True
    syntax_detail: list[str] = []

    for src in sources:
        try:
            py_compile.compile(str(src), doraise=True)
        except py_compile.PyCompileError as exc:
            syntax_ok = False
            syntax_detail.append(f"{src.name}: {exc}")
            continue
        trees[src.name] = ast.parse(src.read_text(encoding="utf-8"))

    checks.add("syntax_compiles", syntax_ok, syntax_detail or "all files compile")

    # 依赖白名单：任何越界的第三方顶层依赖都要报出来。
    offenders: dict[str, list[str]] = {}
    for name, tree in trees.items():
        for root in _module_roots(tree):
            if root in STDLIB or root in ALLOWED_THIRD_PARTY:
                continue
            if root in ALLOWED_FIRST_PARTY:
                continue
            if root in {PACKAGE_NAME}:
                continue
            offenders.setdefault(root, []).append(name)
    checks.add(
        "imports_within_allowlist",
        not offenders,
        offenders or sorted(ALLOWED_THIRD_PARTY),
    )

    # 包内引用必须指向真实存在的模块文件。
    known = {p.stem for p in sources}
    dangling: list[str] = []
    total_refs = 0
    for name, tree in trees.items():
        for ref in _intra_package_refs(tree):
            total_refs += 1
            tail = ref.split(".")[0]
            if tail not in known:
                dangling.append(f"{name} -> {ref}")
    checks.add(
        "intra_package_refs_resolve",
        not dangling,
        {"refs": total_refs, "dangling": dangling},
    )

    # 断言没有任何指向旧包名的代码级导入（文档里提到是允许的）。
    legacy: list[str] = []
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "paperswarm"
            ):
                legacy.append(f"{name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("paperswarm"):
                        legacy.append(f"{name}: import {alias.name}")
    checks.add("no_legacy_package_imports", not legacy, legacy or "none")

    return trees


def install_synthetic_parents() -> None:
    """把真 ``jiuwenswarm`` / ``jiuwenswarm.agents`` 换成只带 __path__ 的壳。

    这样 ``import jiuwenswarm.agents.paper`` 会通过正常的包查找加载本包，
    又不执行 ``jiuwenswarm/__init__.py``（那会牵出 fastapi / playwright 等整棵依赖）。
    """
    for name, path in (
        ("jiuwenswarm", UPSTREAM_ROOT / "jiuwenswarm"),
        ("jiuwenswarm.agents", UPSTREAM_ROOT / "jiuwenswarm" / "agents"),
    ):
        shell = types.ModuleType(name)
        shell.__path__ = [str(path)]
        shell.__package__ = name
        sys.modules[name] = shell


def run_import_check(checks: _Checks) -> object:
    install_synthetic_parents()
    try:
        module = importlib.import_module(PACKAGE_NAME)
    except Exception as exc:  # noqa: BLE001 - 校验器就是要报告任何导入失败
        checks.add("package_imports", False, f"{type(exc).__name__}: {exc}")
        raise

    exported = sorted(getattr(module, "__all__", []))
    checks.add(
        "package_imports",
        True,
        {"version": getattr(module, "__version__", None), "exports": exported},
    )
    checks.add(
        "kernel_modules_present",
        {"evidence", "tex_gate", "assemble", "lab", "tools", "agentloop"}
        <= set(exported),
        exported,
    )
    return module


def run_functional_smoke(checks: _Checks, module: object) -> dict[str, object]:
    """真实跑一遍：实验 → 登账 → 门控 → 回填，并验证防篡改。"""
    lab = module.lab
    evidence = module.evidence
    tex_gate = module.tex_gate

    facts: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="paper-vendor-verify-") as tmp:
        root = Path(tmp)

        # 1. 实验（真实数值）
        experiment = lab.run_experiment()
        metrics_path = experiment.write_json(root / "exp" / "metrics.json")
        results = experiment.results
        facts["experiment"] = {
            "ols_mse_mean": results["ols_mse_mean"],
            "ridge_mse_mean": results["ridge_mse_mean"],
            "improvement_pct": results["improvement_pct"],
            "metrics_path": str(metrics_path),
        }
        checks.add("experiment_ran", metrics_path.is_file(), str(metrics_path))

        # 2. 台账：登记产物 + claim
        ledger = evidence.Ledger(root / "ledger")
        artifact = ledger.register_artifact(
            metrics_path, producer="verify", artifact_id="artifact.metrics"
        )
        claim_specs = (
            ("cl-ols-mean", "Mean OLS test MSE.", "/results/ols_mse_mean"),
            ("cl-improve", "Relative improvement over OLS (percent).", "/results/improvement_pct"),
        )
        registered = []
        for claim_id, text, pointer in claim_specs:
            record = ledger.add_claim(
                text=text,
                artifact_id=artifact.artifact_id,
                locator=pointer,
                claim_id=claim_id,
                run_id="vendor-verify",
            )
            registered.append({"claim_id": record.claim_id, "display": record.display})
        facts["claims"] = registered

        verify = ledger.verify()
        checks.add("ledger_self_check", verify.ok, {
            "passed": verify.passed,
            "failed": verify.failed,
        })

        # 3. 门控：合规正文通过
        good = (
            "Ridge reduces the mean test MSE to \\result{cl-improve} percent of the "
            "OLS error, whose mean is \\result{cl-ols-mean}."
        )
        good_report = tex_gate.gate_text(
            good, ledger, tex_path="<good>", strict_literals=True
        )
        checks.add("gate_accepts_macro_section", good_report.ok, {
            "total_refs": good_report.total_refs,
            "issues": [item.to_dict() for item in good_report.issues],
        })

        # 4. 门控：手写数字被拒（这才是"不可编造"的机制本身）
        bad = "Ridge reduces the mean test MSE by 50.9 percent improvement over OLS."
        bad_report = tex_gate.gate_text(
            bad, ledger, tex_path="<bad>", strict_literals=True
        )
        checks.add("gate_rejects_handwritten_number", not bad_report.ok, {
            "issues": [item.to_dict() for item in bad_report.issues],
        })

        # 5. 回填：宏被真实数值替换
        src = root / "sections" / "experiments.tex"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(good + "\n", encoding="utf-8")
        out = root / "resolved" / "experiments.tex"
        resolve_report = tex_gate.resolve(src, out, ledger)
        resolved = out.read_text(encoding="utf-8")
        facts["resolved_text"] = resolved.strip()
        checks.add(
            "resolve_backfills_values",
            "\\result{" not in resolved and str(results["improvement_pct"]) in resolved,
            {
                "resolved": resolve_report.resolved,
                "no_macro_left": "\\result{" not in resolved,
            },
        )

        # 6. 防篡改：产物被改写后，台账校验必须失败
        tampered = json.loads(metrics_path.read_text(encoding="utf-8"))
        tampered["results"]["improvement_pct"] = 999.0
        metrics_path.write_text(
            json.dumps(tampered, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tamper_report = ledger.verify(check_artifacts=True)
        checks.add("ledger_detects_tampered_artifact", not tamper_report.ok, {
            "total": tamper_report.total,
            "failed": tamper_report.failed,
            "results": [item.to_dict() for item in tamper_report.results][:5],
        })

    return facts


def main() -> int:
    checks = _Checks()
    facts: dict[str, object] = {}

    if not PKG_DIR.is_dir():
        checks.add("package_dir_exists", False, str(PKG_DIR))
    else:
        checks.add("package_dir_exists", True, str(PKG_DIR))
        try:
            run_static_audit(checks)
        except Exception as exc:  # noqa: BLE001
            checks.add("static_audit_completed", False, f"{type(exc).__name__}: {exc}")
        try:
            module = run_import_check(checks)
        except Exception:  # noqa: BLE001 - 已记录为失败项
            module = None
        if module is not None:
            # 功能烟测里任何未预期异常都必须变成一个失败项而不是终止进程——
            # 校验器最不该有的行为就是"什么都没说就挂了"。
            try:
                facts = run_functional_smoke(checks, module)
            except Exception as exc:  # noqa: BLE001
                checks.add(
                    "functional_smoke_completed",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )

    payload = {
        "package": PACKAGE_NAME,
        "package_dir": str(PKG_DIR),
        "ok": checks.ok,
        "passed": sum(1 for item in checks.items if item["passed"]),
        "total": len(checks.items),
        "checks": checks.items,
        "facts": facts,
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return 0 if checks.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
