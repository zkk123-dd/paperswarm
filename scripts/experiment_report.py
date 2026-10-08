# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""跑对照实验并打印可读摘要，同时落盘 ``metrics.json``。

用法::

    export PYTHONPATH="E:/存放/agent/paper-swarm/src"
    <python> scripts/experiment_report.py --out runs/_lab_check/metrics.json

本脚本只做两件事：调用 :mod:`paperswarm.lab` 跑实验，把结果按人可读的方式打出来。
它不做任何数值改写 —— 打印出来的数字与写进 JSON 的数字来自同一份内存对象。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paperswarm import lab


def _fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="PaperSwarm 对照实验")
    parser.add_argument("--out", type=Path, default=None, help="metrics.json 落盘路径")
    parser.add_argument("--quiet", action="store_true", help="只打印关键结论")
    args = parser.parse_args()

    result = lab.run_experiment()
    res = result.results

    if args.out is not None:
        path = result.write_json(args.out)
        print(f"[write] {path}")

    ols = res["ols_mse_mean"]

    print()
    print("=" * 78)
    print("v1 协议复现检查（必须与已发表数字一致）")
    print("=" * 78)
    checks = (
        ("OLS mean (v1, 无截距)", res["v1_ols_mse_mean"], 0.6198),
        ("Ridge mean (v1, 测试集选参)", res["v1_ridge_mse_mean"], 0.3041),
        ("improvement (v1)", res["v1_improvement_pct_aggregate"], 50.94),
        ("selected alpha (v1)", float(res["v1_best_alpha"]), 0.1),
    )
    v1_ok = True
    for label, got, want in checks:
        ok = abs(got - want) < 5e-3
        v1_ok = v1_ok and ok
        print(f"  [{'OK ' if ok else 'DIFF'}] {label:<34s} got={got!r:<12} want={want!r}")
    print(f"  => v1 复现{'一致' if v1_ok else '不一致（需排查）'}")

    print()
    print("=" * 78)
    print("主表：v2 协议（训练集内 5 折交叉验证选参，测试集只评一次）")
    print("=" * 78)
    print(
        f"  {'method':<28s} {'test MSE (mean±std)':>22s} {'R^2':>8s} "
        f"{'vs OLS':>9s}  {'selected hyperparameter'}"
    )
    rows = (
        ("Null (predict train mean)", "null_mse", None, "R^2 = 0 by definition"),
        ("OLS min-norm", "ols_mse", "ols", "<- reference"),
        ("Ridge (alpha by 5-fold CV)", "ridge_mse", "ridge", f"alpha={res['best_alpha']}"),
        (
            "LASSO (lambda by 5-fold CV)",
            "lasso_mse",
            "lasso",
            f"lambda={res['best_lambda_lasso']}",
        ),
        (
            "Elastic Net (lambda, l1 by CV)",
            "enet_mse",
            "enet",
            f"lambda={res['best_lambda_enet']}, l1={res['best_l1_ratio_enet']}",
        ),
        (
            "PCR (components by 5-fold CV)",
            "pcr_mse",
            "pcr",
            f"k={res['best_n_components_pcr']}",
        ),
        (
            "Ridge, alpha by TEST (v1)",
            "v1_ridge_mse",
            "v1_ridge",
            f"alpha={res['v1_best_alpha']}  <- INVALID protocol",
        ),
        (
            "Ridge, oracle alpha on TEST",
            "oracle_ridge_mse",
            "oracle_ridge",
            f"alpha={res['oracle_best_alpha']}  <- upper bound, not a method",
        ),
    )
    for label, mse_key, r2_key, note in rows:
        mean = res[f"{mse_key}_mean"]
        std = res[f"{mse_key}_std"]
        if r2_key is None:
            r2_text = f"{0.0:>8.3f}"
        else:
            r2_text = f"{res[f'{r2_key}_r2_mean']:>8.3f}"
        rel = (ols - mean) / ols * 100.0
        print(
            f"  {label:<28s} {_fmt(mean):>10s} ± {_fmt(std):<8s} {r2_text} "
            f"{rel:>8.2f}%  {note}"
        )

    print()
    print("=" * 78)
    print("选择偏差的量化（把 1.65 个百分点拆开归因）")
    print("=" * 78)
    v1_alpha = float(res["v1_best_alpha"])
    centered_same_alpha = next(
        (p["mse_mean"] for p in res["alpha_sweep_v2_test"] if p["alpha"] == v1_alpha),
        None,
    )
    print(f"  测试集选参的 alpha           = {res['v1_best_alpha']} "
          f"(v1) ｜ CV 选出的 alpha = {res['best_alpha']} (v2)")
    print()
    print("  三档对比（only one thing changes at a time）：")
    print(
        f"    A. v1 原样：无截距 + 测试集选 alpha     "
        f"test MSE={res['v1_ridge_mse_mean']:.4f}  "
        f"improvement={res['v1_improvement_pct_aggregate']:.2f}%"
    )
    if centered_same_alpha is not None:
        imp_b = (res["ols_mse_mean"] - centered_same_alpha) / res["ols_mse_mean"] * 100.0
        print(
            f"    B. 只加截距（仍用测试集选出的 alpha）   "
            f"test MSE={centered_same_alpha:.4f}  improvement={imp_b:.2f}%"
        )
        print(
            f"       -> 截距贡献 {res['v1_improvement_pct_aggregate'] - imp_b:+.2f} 个百分点"
        )
        print(
            f"    C. 再加正确选参协议（CV）             "
            f"test MSE={res['ridge_mse_mean']:.4f}  "
            f"improvement={res['improvement_pct']:.2f}%"
        )
        print(f"       -> 选参协议贡献 {imp_b - res['improvement_pct']:+.2f} 个百分点")
    print()
    print(f"  报告 MSE 之差                = {res['selection_bias_mse_abs']:+.4f} "
          "(v1 更低，因为它偷看了测试集)")
    print(f"  improvement 之差             = {res['selection_bias_pct_points']:+.2f} "
          f"个百分点（v1 {res['v1_improvement_pct_aggregate']:.2f}% "
          f"vs v2 {res['improvement_pct']:.2f}%）")
    print(f"  CV 选参距 oracle 的相对差距   = {res['ridge_gap_to_oracle_pct']:+.2f}%"
          "（越小说明选参协议越好）")
    print()
    print("  注：本例中 CV 选出的 alpha 与测试集 argmin 恰好都是 "
          f"{res['best_alpha']}，")
    print("      所以'选择偏差'这一次的实测幅度很小。这不构成对 v1 协议的辩护——")
    print("      协议无效是协议无效，碰巧给出接近的答案属于运气，不是理由。")

    print()
    print("=" * 78)
    print("噪声下界与归一化")
    print("=" * 78)
    print(f"  noise floor (sigma^2)        = {res['noise_floor_mse']}")
    print(f"  OLS   MSE / floor            = {res['ols_excess_over_floor_ratio']}x")
    print(f"  Ridge MSE / floor            = {res['ridge_excess_over_floor_ratio']}x")
    print(f"  推导: {res['noise_floor_derivation']}")

    if not args.quiet:
        print()
        print("=" * 78)
        print("岭回归 CV 曲线（逐 alpha 的交叉验证 MSE，均值 over 8 seeds）")
        print("=" * 78)
        for point in res["alpha_sweep_cv"]:
            marker = ""
            if point["param"] == res["best_alpha"]:
                marker = "  <- CV argmin"
            print(
                f"  alpha={point['param']:<10.4g} cv_mse={point['cv_mse_mean']:.4f}"
                f" ± {point['cv_mse_std']:.4f}{marker}"
            )

        print()
        print("=" * 78)
        print("测试集上的 alpha 扫描（仅作图/诊断用，不用于选参）")
        print("=" * 78)
        for point in res["alpha_sweep_v2_test"]:
            print(
                f"  alpha={point['alpha']:<10.4g} test_mse={point['mse_mean']:.4f}"
                f" ± {point['mse_std']:.4f}"
            )

    return 0 if v1_ok else 1


if __name__ == "__main__":
    sys.exit(main())
