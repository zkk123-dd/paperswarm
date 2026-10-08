# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 竖切片：真实实验 -> 证据登记 -> Agent 写正文 -> 门控 -> 回填。

这条链路刻意做完整而不是做漂亮：它要回答的唯一问题是
**"一个真实数字，能不能被强制地从实验产物一路送进可编译的 LaTeX，中途无法被编造"**。

链路
----
1. ``run_experiment`` 用 numpy 真跑一个对照实验（两个方法 × 5 个随机种子），
   把指标写成 JSON 产物——数字是算出来的，不是敲进去的；
2. ``Ledger.register_artifact`` 固化产物 SHA-256；
3. Agent（真实 LLM 调用）通过工具登记 claim 并写出 ``\\result{...}`` 正文；
4. ``gate`` 复核台账 + 校验正文引用；
5. ``resolve`` 把 claim 的真实值注入正文，产出可编译的 ``.tex``；
6. 全过程 token / 时延落进遥测台账。

运行::

    export PAPER_LLM_BASE_URL=http://127.0.0.1:11434/v1
    export PAPER_LLM_MODEL=qwen2.5:7b-instruct-q4_K_M
    python scripts/vertical_slice.py

退出码：0 全链路通过；1 有环节失败（失败原因写进 stderr 与 run_summary.json）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paperswarm.agentloop import AgentLoop, LoopResult  # noqa: E402
from paperswarm.envfile import load_env_file  # noqa: E402
from paperswarm.evidence import Ledger  # noqa: E402
from paperswarm.llm import ChatClient, LLMConfig, LLMError  # noqa: E402
from paperswarm.telemetry import Telemetry  # noqa: E402
from paperswarm.tex_gate import resolve  # noqa: E402
from paperswarm.tools import build_ledger_tools  # noqa: E402

SEEDS = (0, 1, 2, 3, 4)
N_LATENT = 3
N_FEATURES = 60
N_TRAIN = 80
N_TEST = 400
LOADING_NOISE = 0.02
NOISE_SIGMA = 0.35
RIDGE_ALPHA = 1.0
"""实验刻意放在**近共线**（低秩因子）区间：60 个特征由 3 个隐因子线性生成，
因此 ``XᵀX`` 只有 3 个主导特征值，其余 57 个方向近乎为零。

这是正则化收益最明确、也最经典的场景：OLS 会在近乎零特征值的方向上把噪声
放大 1/λ 倍，测试误差爆炸；岭回归给这些方向加 α 后直接压平。选择该区间而不是
``n_features < n_train`` 的良性区间，是因为后者 OLS 本来就一致，岭回归只会
引入偏差——第一版参数就踩了这个坑（跑出来岭回归反而差 1.46%），第二版换成
纯欠定区间后两个方法误差都被"截断掉一半信号"主导，差异同样不显著（0.10%）。
结论：实验设计必须让待验证的效应真实且主导，而不是靠调参凑好看的数字。
"""


# ---------------------------------------------------------------------------
# 1. 真实实验
# ---------------------------------------------------------------------------


def _make_split(seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按种子生成一份固定的训练/测试划分。

    数据生成过程：先抽 ``N_LATENT`` 个隐因子，再用一组固定载荷把它线性映射到
    ``N_FEATURES`` 维特征空间，最后叠加一个极小的独立噪声。这样得到的特征矩阵
    近乎共线（列空间只有 latent 维），OLS 的方差问题才会真实出现——
    我们要验证的效应是数据生成过程自带的统计性质，而不是随机波动。
    """
    rng = np.random.default_rng(seed)
    n_total = N_TRAIN + N_TEST
    latent = rng.normal(size=(n_total, N_LATENT))
    loadings = rng.normal(size=(N_LATENT, N_FEATURES))
    features = latent @ loadings + rng.normal(
        scale=LOADING_NOISE, size=(n_total, N_FEATURES)
    )
    true_coef = rng.normal(size=N_FEATURES)
    target = features @ true_coef + rng.normal(scale=NOISE_SIGMA, size=n_total)
    return (
        features[:N_TRAIN],
        target[:N_TRAIN],
        features[N_TRAIN:],
        target[N_TRAIN:],
    )


def _ridge_fit_predict(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    alpha: float,
) -> np.ndarray:
    """岭回归闭式解。

    ``alpha > 0``：``w = (XᵀX + αI)⁻¹Xᵀy``，用 ``np.linalg.solve`` 而非显式求逆
    （数值上更稳，也是正确写法）。

    ``alpha == 0``：退化为 OLS。此处在 ``n_features > n_train`` 的欠定区间，
    ``XᵀX`` 奇异，``solve`` 会直接抛 ``LinAlgError``；而且欠定问题有无穷多解，
    必须指定选哪一个。这里用 ``lstsq`` 取**最小范数最小二乘解**——这是 NumPy
    文档中该函数在秩亏情形下的既定行为，也是与"未正则化 OLS"相符的唯一定义。
    """
    if alpha <= 0.0:
        weights, _residuals, _rank, _singular = np.linalg.lstsq(x_train, y_train, rcond=None)
        return x_test @ weights
    gram = x_train.T @ x_train
    regularizer = alpha * np.eye(gram.shape[0])
    weights = np.linalg.solve(gram + regularizer, x_train.T @ y_train)
    return x_test @ weights


def run_experiment(out_dir: Path) -> dict:
    """跑一遍对照实验并把结果写成 JSON 产物。

    Returns:
        指标字典（同时已落盘到 ``out_dir/metrics.json``）。
    """
    started = time.perf_counter()
    per_seed: list[dict] = []
    for seed in SEEDS:
        x_train, y_train, x_test, y_test = _make_split(seed)
        pred_ols = _ridge_fit_predict(x_train, y_train, x_test, 0.0)
        pred_ridge = _ridge_fit_predict(x_train, y_train, x_test, RIDGE_ALPHA)
        per_seed.append(
            {
                "seed": seed,
                "mse_ols": float(np.mean((pred_ols - y_test) ** 2)),
                "mse_ridge": float(np.mean((pred_ridge - y_test) ** 2)),
            }
        )

    ols = np.array([item["mse_ols"] for item in per_seed], dtype=float)
    ridge = np.array([item["mse_ridge"] for item in per_seed], dtype=float)
    improvement = float((ols.mean() - ridge.mean()) / ols.mean() * 100.0)

    metrics = {
        "experiment": "ridge_vs_ols_synthetic",
        "description": (
            "在近共线（60 个特征由 3 个隐因子线性生成）的线性回归任务上比较 OLS 与"
            "岭回归（alpha=1.0）的测试集 MSE，每个设置跑 5 个随机种子。"
            "该区间内 OLS 在近零特征值方向上放大噪声，用于检验 L2 正则化的实际收益。"
        ),
        "config": {
            "seeds": list(SEEDS),
            "n_train": N_TRAIN,
            "n_test": N_TEST,
            "n_features": N_FEATURES,
            "n_latent": N_LATENT,
            "loading_noise": LOADING_NOISE,
            "noise_sigma": NOISE_SIGMA,
            "ridge_alpha": RIDGE_ALPHA,
        },
        "per_seed": per_seed,
        "results": {
            "mse_ols_mean": float(ols.mean()),
            "mse_ols_std": float(ols.std(ddof=1)),
            "mse_ridge_mean": float(ridge.mean()),
            "mse_ridge_std": float(ridge.std(ddof=1)),
            "relative_improvement_pct": improvement,
        },
        "wall_seconds": round(time.perf_counter() - started, 4),
        "numpy_version": np.__version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


# ---------------------------------------------------------------------------
# 2. Agent 侧
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 PaperSwarm 的 writer 角色，负责把实验产物写成论文正文片段。

这个系统的硬规则：**论文正文里禁止出现任何手写数值**。每一个数字都必须写成 LaTeX 宏
\\result{claim_id}，其中 claim_id 必须是台账里已登记且校验通过的 claim。
系统会在写入时校验，违规的写入会被直接拒绝。

整个任务只有三步，请一次做完：

第 1 步：调用 register_artifact 登记产物。
  参数：path = 用户消息里给的产物路径，producer = "writer"。
  它返回 artifact_id（形如 a-1a2b3c4d）。

第 2 步：调用 add_claims **一次性**登记全部 claim。
  参数只填两项：artifact_id = 第 1 步返回的那个短标识；
  claims = 数组，每项只填 text 和 locator。例如：
  {
    "artifact_id": "a-1a2b3c4d",
    "claims": [
      {"text": "The mean test MSE of OLS.", "locator": "/results/mse_ols_mean"},
      {"text": "The mean test MSE of ridge regression.", "locator": "/results/mse_ridge_mean"}
    ]
  }
  注意：不要在每项里填 value，让系统从产物读真实值；不要拆成多次调用。

第 3 步：调用 write_latex_section 写正文。
  relative_path 固定填 "sections/results.tex"。
  content 是 LaTeX 片段，所有数值写成第 2 步返回的 latex_macro，例如 \\result{c-1a2b3c4d}。
  绝对不能在正文里写具体数字。

遇到工具报错时：读懂错误、修正参数、重新调用同一个工具，不要跳过，也不要提前结束。
比如报错说缺少参数，就把缺的参数补上再调一次。

正文要求：英文、学术风格、包含实验设置与结果两段、不写 \\documentclass。
不要把手写数字和 achieve / improve / accuracy / score 这类词写在一起。
"""


def build_task(artifact_path: Path, metrics: dict) -> str:
    """构造交给模型的用户侧任务描述。"""
    fields = "\n".join(
        f"  {name}  ->  locator: /results/{name}    (实际值 {value!r})"
        for name, value in metrics["results"].items()
    )
    return f"""实验产物路径（第 1 步用它做 path 参数）：
{artifact_path}

产物 JSON 的 results 段包含这些字段，请全部登记成 claim：
{fields}

请依次完成三步：register_artifact -> add_claims（一次登记全部）-> write_latex_section。
正文写到 sections/results.tex，写完后任务结束。
"""


def run_agent(
    *,
    client: ChatClient,
    ledger: Ledger,
    telemetry: Telemetry,
    run_id: str,
    artifact_path: Path,
    metrics: dict,
    work_dir: Path,
    max_steps: int,
) -> LoopResult:
    """跑一次有界的 Agent 循环。"""
    tools = build_ledger_tools(
        ledger, tex_dir=work_dir / "paper", run_id=run_id, compact=True
    )
    loop = AgentLoop(
        client=client,
        tools=tools,
        telemetry=telemetry,
        run_id=run_id,
        max_steps=max_steps,
        token_budget=120_000,
        wall_clock_seconds=900.0,
        repeat_limit=3,
        max_tool_result_chars=3000,
        # 完成条件写成代码而不是提示词：正文没写出来，这次运行就不算成功。
        required_tools=("write_latex_section",),
        max_completion_nudges=3,
    )
    return loop.run(
        task=build_task(artifact_path, metrics),
        system_prompt=SYSTEM_PROMPT,
        span="writer.results",
        role="writer",
        max_tokens=1500,
        temperature=0.2,
    )


# ---------------------------------------------------------------------------
# 3. 主流程
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """竖切片入口。"""
    # 必须在 argparse 之前：下面的 --base-url / --model 默认值要读 os.environ。
    applied = load_env_file(base_dir=REPO_ROOT)
    if applied:
        print(f"[env] 载入 {REPO_ROOT / '.env.local'}：{', '.join(applied)}")
    parser = argparse.ArgumentParser(description="PaperSwarm Phase 2 竖切片")
    parser.add_argument("--runs-dir", default=str(REPO_ROOT / "runs"), help="运行根目录")
    parser.add_argument("--max-steps", type=int, default=12, help="Agent 最大步数")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PAPER_LLM_BASE_URL", "http://127.0.0.1:11434/v1"),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("PAPER_LLM_MODEL", "qwen2.5:7b-instruct-q4_K_M"),
    )
    parser.add_argument("--skip-agent", action="store_true", help="只跑实验与台账，不调 LLM")
    args = parser.parse_args(argv)

    run_id = datetime.now(timezone.utc).strftime("slice-%Y%m%d-%H%M%S")
    work_dir = Path(args.runs_dir).resolve() / run_id
    work_dir.mkdir(parents=True, exist_ok=True)
    telemetry = Telemetry(work_dir, run_id)
    ledger = Ledger(work_dir / "ledger")

    print(f"[run] {run_id}  ->  {work_dir}")

    # --- 1. 真实实验 ------------------------------------------------------
    metrics = run_experiment(work_dir / "exp")
    artifact_path = work_dir / "exp" / "metrics.json"
    result_facts = metrics["results"]
    print(
        "[exp] OLS MSE = {mse_ols_mean:.4f} ± {mse_ols_std:.4f} | "
        "Ridge MSE = {mse_ridge_mean:.4f} ± {mse_ridge_std:.4f} | "
        "改进 {relative_improvement_pct:.2f}%".format(**result_facts)
    )
    telemetry.record_event(
        span="experiment",
        kind="experiment_done",
        ok=True,
        detail=f"wall={metrics['wall_seconds']}s",
        payload={"artifact": str(artifact_path), "results": result_facts},
    )

    # --- 2. 登记产物 ------------------------------------------------------
    artifact = ledger.register_artifact(artifact_path, producer="scripts/vertical_slice.py")
    print(f"[ledger] artifact {artifact.artifact_id} sha256={artifact.sha256[:16]}…")
    telemetry.record_event(
        span="evidence",
        kind="artifact_registered",
        ok=True,
        detail=artifact.artifact_id,
        payload={"sha256": artifact.sha256},
    )

    if args.skip_agent:
        telemetry.write_summary({"mode": "skip-agent"})
        print("[done] 已跳过 Agent 环节")
        return 0

    # --- 3. Agent 写正文 --------------------------------------------------
    config = LLMConfig(
        base_url=args.base_url,
        model=args.model,
        api_key=os.environ.get("PAPER_LLM_API_KEY") or "not-needed",
        read_timeout=300.0,
        max_retries=3,
        # 本机 Ollama 边际成本为 0，这是显式已知而非"未定价"
        price_per_1k_prompt=0.0,
        price_per_1k_completion=0.0,
    )
    print(f"[llm] {config.base_url}  model={config.model}")
    client = ChatClient(config)
    try:
        probe = client.probe()
        print(
            f"[llm] 连通性 OK（{probe['latency_ms']}ms，"
            f"{probe['prompt_tokens']}+{probe['completion_tokens']} tokens）"
        )
    except LLMError as exc:
        print(f"[llm] 连通性探测失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        telemetry.record_event(span="llm", kind="probe_failed", ok=False, detail=str(exc))
        telemetry.write_summary({"mode": "agent", "failed_at": "llm_probe"})
        return 1

    try:
        loop_result = run_agent(
            client=client,
            ledger=ledger,
            telemetry=telemetry,
            run_id=run_id,
            artifact_path=artifact_path,
            metrics=metrics,
            work_dir=work_dir,
            max_steps=args.max_steps,
        )
    except LLMError as exc:
        print(f"[agent] LLM 调用失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        telemetry.record_event(span="writer.results", kind="llm_error", ok=False, detail=str(exc))
        telemetry.write_summary({"mode": "agent", "failed_at": "llm_call"})
        return 1

    print(
        f"[agent] stop_reason={loop_result.stop_reason} steps={loop_result.steps} "
        f"tokens={loop_result.total_tokens} tool_calls={len(loop_result.tool_trace)}"
    )
    for item in loop_result.tool_trace:
        flag = "ok " if item.ok else "ERR"
        print(f"        [{flag}] {item.name} {json.dumps(item.arguments, ensure_ascii=False)[:110]}")

    # --- 4. 门控 + 回填 ---------------------------------------------------
    src_tex = work_dir / "paper" / "sections" / "results.tex"
    gate_ok = False
    resolved_tex = work_dir / "paper" / "results.resolved.tex"
    produced = sorted((work_dir / "paper").rglob("*.tex")) if (work_dir / "paper").is_dir() else []
    if not produced:
        print("[gate] 没有任何正文产出", file=sys.stderr)
    for candidate in produced:
        print(f"[gate] 产出: {candidate.relative_to(work_dir)}")
    if src_tex.is_file():
        report = resolve(src_tex, resolved_tex, ledger, literal_warnings=True)
        gate_ok = report.ok
        print(
            f"[gate] ok={report.ok} refs={report.total_refs} resolved={report.resolved} "
            f"issues={len(report.issues)}"
        )
        for issue in report.issues:
            print(f"        [FAIL] 第 {issue.line} 行 {issue.claim_id}: {issue.reason}")
        for warning in report.warnings:
            print(f"        [WARN] {warning}")
    else:
        print(f"[gate] 期望的正文未生成: {src_tex}", file=sys.stderr)

    # --- 5. 台账复核 ------------------------------------------------------
    verify = ledger.verify(check_artifacts=True)
    print(
        f"[verify] claims={verify.total} passed={verify.passed} failed={verify.failed} "
        f"ok={verify.ok}"
    )

    # --- 6. 遥测汇总 ------------------------------------------------------
    summary = telemetry.summary()
    summary_path = telemetry.write_summary(
        {
            "mode": "agent",
            "stop_reason": loop_result.stop_reason,
            "agent_steps": loop_result.steps,
            "gate_ok": gate_ok,
            "claims_total": verify.total,
            "claims_passed": verify.passed,
            "experiment_wall_seconds": metrics["wall_seconds"],
            "artifact_sha256": artifact.sha256,
        }
    )
    print(
        "[telemetry] calls={calls} tokens={total_tokens} "
        "wall={latency_ms_total}ms cost=${cost}".format(
            cost=summary["cost_usd_total"], **summary
        )
    )
    print(f"[telemetry] {summary_path}")

    ok = gate_ok and verify.ok and loop_result.ok
    print(f"[done] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
