# Copyright (c) 2026 PaperSwarm contributors.
# SPDX-License-Identifier: Apache-2.0
"""可复现的对照实验：近共线设计下的正则化回归 vs 最小范数 OLS。

本模块同时承担两个职责：

1. 为论文提供**真实的**实验结果数值；
2. 作为证据台账的产物来源 —— 每个数值都能通过 JSON Pointer 定位到本模块写出的
   ``metrics.json``，并被 SHA-256 固化。论文正文里不允许手写这些数字。

v2 相对 v1 的两处方法论修正
----------------------------
**修正一（选择协议，最重要）。** v1 的做法是：对每个候选惩罚系数在**测试集**上算
MSE，取测试集 MSE 最小的那个当"最优"。这是选择偏差，会系统性抬高报告的性能。
v2 的做法是：在**训练集内部**做 K 折交叉验证选参，测试集**只评一次**。
两条协议都在本模块里实现并可分别复现，因此修正带来的差值可以被量化归因，
而不是"改完数字变了但说不清为什么"。

**修正二（截距）。** v1 的所有估计量都不带截距。数据生成过程本身近似零均值，
所以影响很小 —— 但"很小"必须被测量而不是被假设，因此 v2 默认中心化，
并单独报告不带中心化的 v1 协议结果作为对照。

为什么实验设计放在近共线区间
----------------------------
正则化的收益来自"用偏差换方差"。当设计矩阵病态（特征高度相关 / 低秩）时，
普通最小二乘的解方差极大、对噪声极其敏感；L2 惩罚能显著压低方差。
如果放在 n_train > n_features 的良性区间，OLS 本身就是一致的，正则化只会
引入偏差甚至更差。这不是"调参调不出好结果"，而是**实验必须落在效应真实存在
的区间**，否则结论就是噪声。

随机种子固定，结果可复现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

EXPERIMENT_NAME = "ridge_vs_ols_near_collinear"
"""实验标识，写进产物 JSON，供论文与台账交叉引用。"""

SEEDS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 7)
N_FEATURES = 120
N_INFORMATIVE = 6
N_TRAIN = 90
N_TEST = 200
NOISE_SIGMA = 0.35

#: v1 使用的冻结网格。**不要改**：它是"历史协议"的一部分，
#: 改了就无法复现已发表（且已被外部评审引用）的那组数字。
V1_ALPHAS: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)

#: v2 的岭回归网格：跨 7 个数量级的对数网格，覆盖从"几乎不正则化"到
#: "把信号全部压掉"的整个区间。选它是因为它不依赖任何需要人工判断的尺度常数。
RIDGE_ALPHA_GRID: tuple[float, ...] = (
    1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1,
    3e-1, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0,
)

#: 弹性网惩罚网格的取点数（上界由数据自适应确定，见 :func:`lasso_lambda_grid`）。
LAMBDA_GRID_POINTS = 8

#: 弹性网的 L1 占比网格。``1.0`` 即 LASSO，``0.5`` 是标准弹性网。
L1_RATIOS: tuple[float, ...] = (0.5, 1.0)

#: PCR 的主成分个数网格。
PCR_COMPONENTS: tuple[int, ...] = tuple(range(1, 21))

CV_FOLDS = 5
CV_SEED = 20260929
"""交叉验证的折划分种子。固定它，折的划分本身才是可复现的。"""

COORD_MAX_ITER = 300
COORD_TOL = 1e-6

NOISE_FLOOR_MSE = round(float(NOISE_SIGMA**2), 4)
"""不可约误差下界：见 :func:`noise_floor_derivation`。"""


def noise_floor_derivation() -> str:
    """给出不可约噪声下界的**推导**，而不是把常数直接写死。

    数据生成过程是 ``y = x @ coef + eps``，``eps ~ N(0, sigma^2)``，``sigma = 0.35``。
    令 ``f*(x) = x @ coef`` 为贝叶斯最优预测器，则对任意预测器 ``g`` 有

        E[(y - g(x))^2] = E[(y - f*(x))^2] + E[(f*(x) - g(x))^2]
                        = sigma^2 + E[(f*(x) - g(x))^2]  >=  sigma^2

    第一项是目标自带的加性噪声，与 ``g`` 无关；第二项非负。
    因此 ``sigma^2 = 0.35^2 = 0.1225`` 是任何估计量都**无法系统性低于**的下界
    （单次实现可能偶然低于它，那是采样波动，不是系统性突破）。
    """
    return (
        "y = x @ coef + eps with eps ~ N(0, sigma^2), sigma = "
        f"{NOISE_SIGMA}. For the Bayes-optimal predictor f*(x) = x @ coef, "
        "E[(y - g(x))^2] = sigma^2 + E[(f*(x) - g(x))^2] >= sigma^2 "
        f"for any g. Hence the irreducible floor is sigma^2 = {NOISE_FLOOR_MSE}."
    )


@dataclass
class ExperimentResult:
    """一次实验运行的完整结果。

    ``results`` 里的每个键都直接对应产物 JSON 中的一个 JSON Pointer，
    例如 ``results["ols_mse_mean"]`` 对应 ``/results/ols_mse_mean``。
    """

    experiment: str
    config: dict[str, Any]
    results: dict[str, Any]
    per_seed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment": self.experiment,
            "config": self.config,
            "results": self.results,
            "per_seed": self.per_seed,
        }

    def write_json(self, path: Path | str) -> Path:
        """把结果写成 JSON（证据台账的 artifact 就是它）。"""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out


# --------------------------------------------------------------------------
# 数据生成
# --------------------------------------------------------------------------


def make_dataset(seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """生成一份近共线数据划分。

    构造方式（论文里必须写全，评审点名要过这一段）：

    1. ``latents ~ N(0, I_6)`` —— 6 个隐因子；
    2. ``loadings ~ N(0, 1)``，形状 ``(6, 120)``；
    3. ``features = latents @ loadings + N(0, 0.05^2)`` —— 设计矩阵近似低秩
       （严重共线），但并非精确奇异；
    4. ``coef ~ N(0, I_120)``；
    5. ``target = features @ coef + N(0, 0.35^2)``。

    特征矩阵的秩上界只有 6 + 噪声，而列数 120、训练样本 90，
    因此 ``p > n`` 且近共线 —— 正是正则化发挥作用的区间。

    Args:
        seed: 随机种子。

    Returns:
        ``(x_train, y_train, x_test, y_test)``。
    """
    rng = np.random.default_rng(seed)
    n_total = N_TRAIN + N_TEST
    latents = rng.normal(size=(n_total, N_INFORMATIVE))
    loadings = rng.normal(size=(N_INFORMATIVE, N_FEATURES))
    features = latents @ loadings + rng.normal(scale=0.05, size=(n_total, N_FEATURES))
    coef = rng.normal(size=N_FEATURES)
    target = features @ coef + rng.normal(scale=NOISE_SIGMA, size=n_total)
    return (
        features[:N_TRAIN],
        target[:N_TRAIN],
        features[N_TRAIN:],
        target[N_TRAIN:],
    )


# --------------------------------------------------------------------------
# 估计量：全部返回权重向量 w，预测为 x @ w
# --------------------------------------------------------------------------


def ols_min_norm_weights(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """最小范数最小二乘解（伪逆），欠定/病态情形下 OLS 的标准解。"""
    weights, _residuals, _rank, _sv = np.linalg.lstsq(x, y, rcond=None)
    return weights


def ridge_weights(x: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    """闭式解岭回归：``w = (X^T X + alpha I)^-1 X^T y``。

    用 ``np.linalg.solve`` 而不是显式求逆：数值上更稳，也是正确写法。
    """
    gram = x.T @ x
    regularizer = alpha * np.eye(gram.shape[0])
    return np.linalg.solve(gram + regularizer, x.T @ y)


def _soft_threshold(value: float, gamma: float) -> float:
    """标量软阈值算子 ``S(z, gamma) = sign(z) * max(|z| - gamma, 0)``。"""
    if value > gamma:
        return value - gamma
    if value < -gamma:
        return value + gamma
    return 0.0


def elastic_net_weights(
    x: np.ndarray,
    y: np.ndarray,
    lam: float,
    l1_ratio: float,
    *,
    w0: np.ndarray | None = None,
    max_iter: int = COORD_MAX_ITER,
    tol: float = COORD_TOL,
) -> np.ndarray:
    """弹性网，坐标下降求解（``l1_ratio = 1.0`` 即 LASSO）。

    目标函数采用"平方和"约定（与 sklearn 的 ``1/(2n)`` 约定差一个 ``2n`` 因子，
    但网格是数据自适应的，所以约定只影响 ``lam`` 的读数，不影响选择结果）：

        ``min_w  0.5 * ||y - Xw||^2 + lam * (l1_ratio * ||w||_1
                  + 0.5 * (1 - l1_ratio) * ||w||_2^2)``

    列先归一化到单位范数，坐标更新为
    ``w_j = S(x_j . r_j, lam * l1_ratio) / (1 + lam * (1 - l1_ratio))``，
    最后把权重除回归一化因子。这样 ``lam`` 的尺度不随列范数漂移。

    Args:
        x: 设计矩阵 ``(n, p)``。
        y: 目标 ``(n,)``。
        lam: 惩罚强度。
        l1_ratio: L1 占比，取值 ``(0, 1]``；``1.0`` 即 LASSO。
        w0: 热启动初值。沿 ``lam`` 递减路径求解时传入上一个解可以大幅减少迭代。
        max_iter: 最大扫描次数。
        tol: 单次扫描内权重最大变化量的收敛阈值。

    Returns:
        权重向量 ``(p,)``。
    """
    if not 0.0 < l1_ratio <= 1.0:
        raise ValueError(f"l1_ratio 必须落在 (0, 1]，收到 {l1_ratio}")

    n_samples, n_features = x.shape
    if n_samples == 0:
        raise ValueError("弹性网需要至少一个样本")

    norms = np.linalg.norm(x, axis=0)
    norms = np.where(norms == 0.0, 1.0, norms)
    xs = x / norms  # 单位范数列

    weights = np.zeros(n_features) if w0 is None else np.array(w0, dtype=float, copy=True)
    residual = y.astype(float, copy=True) - xs @ weights
    l1_penalty = lam * l1_ratio
    l2_penalty = 1.0 + lam * (1.0 - l1_ratio)

    for _ in range(max_iter):
        max_delta = 0.0
        for j in range(n_features):
            column = xs[:, j]
            old = weights[j]
            # 把第 j 列的贡献加回残差，得到"去掉 j"的偏残差
            partial = float(column @ residual) + old
            new_value = _soft_threshold(partial, l1_penalty) / l2_penalty
            weights[j] = new_value
            residual -= column * (new_value - old)
            delta = abs(new_value - old)
            if delta > max_delta:
                max_delta = delta
        if max_delta < tol:
            break

    return weights / norms


def pcr_weights(x: np.ndarray, y: np.ndarray, n_components: int) -> np.ndarray:
    """主成分回归：对 ``X`` 做 SVD，只用前 ``n_components`` 个主方向做最小二乘。"""
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    k = int(min(n_components, s.size))
    if k == 0:
        return np.zeros(x.shape[1])
    coef_k = (u[:, :k].T @ y) / s[:k]
    return vt[:k].T @ coef_k


def null_weights(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """零模型：权重全零（配合中心化等价于预测训练集均值）。"""
    return np.zeros(x.shape[1])


# --------------------------------------------------------------------------
# 拟合协议：中心化 / 不中心化
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Centering:
    """中心化参数：训练集上的特征均值与目标均值。"""

    x_mean: np.ndarray
    y_mean: float

    @classmethod
    def from_train(cls, x_train: np.ndarray, y_train: np.ndarray) -> "Centering":
        return cls(x_mean=x_train.mean(axis=0), y_mean=float(y_train.mean()))

    @classmethod
    def none(cls, n_features: int) -> "Centering":
        return cls(x_mean=np.zeros(n_features), y_mean=0.0)

    def transform(self, x: np.ndarray) -> np.ndarray:
        return x - self.x_mean

    def inverse(self, pred: np.ndarray) -> np.ndarray:
        return pred + self.y_mean


def _centering(x_train: np.ndarray, y_train: np.ndarray, center: bool) -> Centering:
    return (
        Centering.from_train(x_train, y_train)
        if center
        else Centering.none(x_train.shape[1])
    )


def fit_predict(
    fit_weights: Callable[[np.ndarray, np.ndarray], np.ndarray],
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_eval: np.ndarray,
    *,
    center: bool,
) -> np.ndarray:
    """按指定协议拟合并在 ``x_eval`` 上预测。

    ``center=True`` 时先用训练集均值中心化特征与目标，预测时再加回目标均值
    （等价于带截距的线性模型）。``center=False`` 复现 v1 的无截距协议。
    """
    centering = _centering(x_train, y_train, center)
    weights = fit_weights(centering.transform(x_train), y_train - centering.y_mean)
    return centering.inverse(centering.transform(x_eval) @ weights)


def mse(pred: np.ndarray, truth: np.ndarray) -> float:
    """均方误差。"""
    diff = pred - truth
    return float(np.mean(diff * diff))


def kfold_indices(n_samples: int, n_folds: int, seed: int) -> list[np.ndarray]:
    """确定性的 K 折划分。固定 ``seed``，折的划分本身可复现。"""
    if n_folds < 2:
        raise ValueError(f"n_folds 必须 >= 2，收到 {n_folds}")
    if n_samples < n_folds:
        raise ValueError(f"样本数 {n_samples} 少于折数 {n_folds}，无法划分")
    idx = np.random.default_rng(seed).permutation(n_samples)
    return [idx[i::n_folds] for i in range(n_folds)]


#: 路径求解器签名：``(grid, x, y) -> [w, ...]``，与 ``grid`` 一一对应。
PathSolver = Callable[[Sequence[Any], np.ndarray, np.ndarray], list[np.ndarray]]


def ridge_path_solver() -> PathSolver:
    """岭回归路径求解器：每个 ``alpha`` 都有闭式解，逐个解即可。"""

    def solve(grid: Sequence[Any], x: np.ndarray, y: np.ndarray) -> list[np.ndarray]:
        return [ridge_weights(x, y, float(param)) for param in grid]

    return solve


def elastic_net_path_solver() -> PathSolver:
    """弹性网路径求解器：按 ``lam`` 递减排序后热启动，摊薄坐标下降的迭代成本。"""

    def solve(grid: Sequence[Any], x: np.ndarray, y: np.ndarray) -> list[np.ndarray]:
        params = [tuple(param) for param in grid]
        order = sorted(range(len(params)), key=lambda i: (-params[i][0], params[i][1]))
        weights: list[np.ndarray | None] = [None] * len(params)
        warm: dict[float, np.ndarray] = {}
        for index in order:
            lam, l1_ratio = params[index]
            zero = np.zeros(x.shape[1])
            start = warm.get(float(l1_ratio), zero)
            solution = elastic_net_weights(x, y, float(lam), float(l1_ratio), w0=start)
            weights[index] = solution
            warm[float(l1_ratio)] = solution
        return [w if w is not None else np.zeros(x.shape[1]) for w in weights]

    return solve


def pcr_path_solver() -> PathSolver:
    """PCR 路径求解器：一次 SVD，复用到所有主成分个数上。"""

    def solve(grid: Sequence[Any], x: np.ndarray, y: np.ndarray) -> list[np.ndarray]:
        u, s, vt = np.linalg.svd(x, full_matrices=False)
        out: list[np.ndarray] = []
        for param in grid:
            k = int(min(int(param), s.size))
            if k == 0:
                out.append(np.zeros(x.shape[1]))
                continue
            coef_k = (u[:, :k].T @ y) / s[:k]
            out.append(vt[:k].T @ coef_k)
        return out

    return solve


def cross_validate(
    solve_path: PathSolver,
    grid: Sequence[Any],
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    n_folds: int = CV_FOLDS,
    seed: int = CV_SEED,
    center: bool = True,
) -> tuple[Any, list[dict[str, Any]]]:
    """在**训练集内部**做 K 折交叉验证选参。

    ⚠️ 这里永远不接触测试集。选择集与报告集不相交，是 v2 与 v1 的根本区别。

    Args:
        solve_path: 路径求解器，一次返回 ``grid`` 上所有超参的解。
        grid: 候选超参列表。
        x_train: 训练特征。
        y_train: 训练目标。
        n_folds: 折数。
        seed: 折划分种子。
        center: 是否中心化。

    Returns:
        ``(best_param, curve)``；``curve`` 是每个候选超参的交叉验证均方误差。
    """
    if not grid:
        raise ValueError("grid 不能为空")

    folds = kfold_indices(x_train.shape[0], n_folds, seed)
    fold_scores: list[list[float]] = [[] for _ in grid]

    for k in range(n_folds):
        held_out = folds[k]
        rest = np.concatenate([folds[i] for i in range(n_folds) if i != k])
        x_fit, y_fit = x_train[rest], y_train[rest]
        centering = _centering(x_fit, y_fit, center)
        solutions = solve_path(grid, centering.transform(x_fit), y_fit - centering.y_mean)
        if len(solutions) != len(grid):
            raise RuntimeError(
                f"路径求解器返回 {len(solutions)} 个解，但网格有 {len(grid)} 个候选"
            )
        x_held = centering.transform(x_train[held_out])
        for i, weights in enumerate(solutions):
            pred = centering.inverse(x_held @ weights)
            fold_scores[i].append(mse(pred, y_train[held_out]))

    curve: list[dict[str, Any]] = []
    for i, param in enumerate(grid):
        scores = fold_scores[i]
        curve.append(
            {
                "param": param_to_json(param),
                "cv_mse_mean": float(np.mean(scores)),
                "cv_mse_std": float(np.std(scores, ddof=1)) if len(scores) > 1 else 0.0,
            }
        )

    best_index = min(range(len(curve)), key=lambda i: curve[i]["cv_mse_mean"])
    return grid[best_index], curve


# --------------------------------------------------------------------------
# 序列化辅助
# --------------------------------------------------------------------------


def param_to_json(param: Any) -> Any:
    """把超参转成可 JSON 序列化的形式（元组 -> 列表，numpy 标量 -> 内置标量）。"""
    if isinstance(param, tuple):
        return [param_to_json(item) for item in param]
    if isinstance(param, (np.integer,)):
        return int(param)
    if isinstance(param, (np.floating,)):
        return float(param)
    return param


def param_from_json(payload: Any) -> Any:
    """把 JSON 形式还原成超参（列表 -> 元组）。"""
    if isinstance(payload, list):
        return tuple(param_from_json(item) for item in payload)
    if isinstance(payload, float):
        return float(payload)
    if isinstance(payload, int):
        return int(payload)
    return payload


def lasso_lambda_grid(x_train: np.ndarray, y_train: np.ndarray) -> tuple[float, ...]:
    """数据自适应的弹性网惩罚网格。

    上界取教科书里的 ``lam_max = max_j |x_j . y|``（列已归一化），
    在这个值上所有权重都会被压成 0；下界取它的 ``1e-4``，中间对数等分。
    """
    norms = np.linalg.norm(x_train, axis=0)
    norms = np.where(norms == 0.0, 1.0, norms)
    xs = x_train / norms
    lam_max = float(np.max(np.abs(xs.T @ y_train)))
    if lam_max <= 0.0:
        return (0.0,)
    return tuple(
        float(v) for v in np.geomspace(lam_max * 1e-4, lam_max, LAMBDA_GRID_POINTS)
    )


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run_experiment(
    *,
    seeds: Sequence[int] = SEEDS,
    v1_alphas: Sequence[float] = V1_ALPHAS,
    ridge_grid: Sequence[float] = RIDGE_ALPHA_GRID,
    l1_ratios: Sequence[float] = L1_RATIOS,
    pcr_components: Sequence[int] = PCR_COMPONENTS,
    n_folds: int = CV_FOLDS,
    cv_seed: int = CV_SEED,
) -> ExperimentResult:
    """跑完整对照实验。

    每个种子上依次做：

    1. **零模型**（全零权重）与 **OLS 最小范数解**，作为参照；
    2. **v1 协议复现**：无截距 + 在测试集上选 ``alpha``。这一步存在的唯一目的
       是复现已发表的那组数字，好让修正带来的差值可以被量化归因；
    3. **v2 协议**：中心化 + 训练集内 5 折交叉验证选参，测试集只评一次。
       覆盖 Ridge / Elastic Net（含 LASSO）/ PCR；
    4. **Oracle 上界**：直接在所有 ``alpha`` 里挑测试集 MSE 最小的那个。
       **这不是一个可用方法**（它偷看了测试集），只用来标出"训练集选参
       协议距离最优还有多远"。

    Args:
        seeds: 随机种子序列，每个种子产生一份独立的数据划分。
        v1_alphas: v1 的冻结网格。
        ridge_grid: v2 的岭回归候选网格。
        l1_ratios: 弹性网的 L1 占比网格。
        pcr_components: PCR 主成分个数网格。
        n_folds: 交叉验证折数。
        cv_seed: 折划分种子。

    Returns:
        :class:`ExperimentResult`；``results`` 中的数值就是论文可引用的 claim 值。
    """
    per_seed: list[dict[str, Any]] = []

    for seed in seeds:
        x_train, y_train, x_test, y_test = make_dataset(seed)
        row: dict[str, Any] = {"seed": seed}

        # ---- 0. 参照：零模型（中心化预测训练均值）----
        null_pred = fit_predict(null_weights, x_train, y_train, x_test, center=True)
        row["null_mse"] = mse(null_pred, y_test)

        # ---- 1. OLS 最小范数（无超参，因此不受选择协议影响）----
        ols_pred = fit_predict(ols_min_norm_weights, x_train, y_train, x_test, center=True)
        row["ols_mse"] = mse(ols_pred, y_test)
        ols_pred_v1 = fit_predict(
            ols_min_norm_weights, x_train, y_train, x_test, center=False
        )
        row["ols_mse_v1_protocol"] = mse(ols_pred_v1, y_test)

        # ---- 2. v1 协议复现：无截距 + 测试集选 alpha ----
        v1_centering = _centering(x_train, y_train, center=False)
        v1_solutions = ridge_path_solver()(
            [float(a) for a in v1_alphas],
            v1_centering.transform(x_train),
            y_train - v1_centering.y_mean,
        )
        for alpha, weights in zip(v1_alphas, v1_solutions):
            pred = v1_centering.inverse(v1_centering.transform(x_test) @ weights)
            row[f"v1_ridge_mse_alpha_{alpha}"] = mse(pred, y_test)

        # ---- 3. v2 协议：固定网格上的测试集扫描（只作为图示，不用于选参）----
        centering = _centering(x_train, y_train, center=True)
        sweep_solutions = ridge_path_solver()(
            [float(a) for a in ridge_grid],
            centering.transform(x_train),
            y_train - centering.y_mean,
        )
        for alpha, weights in zip(ridge_grid, sweep_solutions):
            pred = centering.inverse(centering.transform(x_test) @ weights)
            row[f"test_sweep_ridge_mse_alpha_{alpha}"] = mse(pred, y_test)

        # ---- 4. v2 协议：交叉验证选参 ----
        best_alpha, ridge_cv_curve = cross_validate(
            ridge_path_solver(), ridge_grid, x_train, y_train,
            n_folds=n_folds, seed=cv_seed,
        )
        row["cv_alpha"] = float(best_alpha)
        row["cv_ridge_mse"] = mse(
            fit_predict(
                lambda x, y: ridge_weights(x, y, float(best_alpha)),
                x_train, y_train, x_test, center=True,
            ),
            y_test,
        )
        row["cv_ridge_curve"] = ridge_cv_curve

        lam_grid = lasso_lambda_grid(x_train, y_train)
        enet_grid = tuple(
            (float(lam), float(r)) for lam in lam_grid for r in l1_ratios
        )
        best_enet, enet_cv_curve = cross_validate(
            elastic_net_path_solver(), enet_grid, x_train, y_train,
            n_folds=n_folds, seed=cv_seed,
        )
        row["cv_enet_lambda"] = float(best_enet[0])
        row["cv_enet_l1_ratio"] = float(best_enet[1])
        row["cv_enet_mse"] = mse(
            fit_predict(
                lambda x, y: elastic_net_weights(
                    x, y, float(best_enet[0]), float(best_enet[1])
                ),
                x_train, y_train, x_test, center=True,
            ),
            y_test,
        )
        row["enet_curve"] = enet_cv_curve
        # LASSO 读作"L1 占比 = 1.0 的弹性网"，两者共享同一条路径，不重复计算。
        lasso_params = [p for p in enet_grid if abs(p[1] - 1.0) < 1e-12]
        lasso_curve = [
            point for point in enet_cv_curve
            if abs(param_from_json(point["param"])[1] - 1.0) < 1e-12
        ]
        if lasso_params and lasso_curve:
            best_lasso_point = min(lasso_curve, key=lambda p: p["cv_mse_mean"])
            best_lasso = param_from_json(best_lasso_point["param"])
            row["cv_lasso_lambda"] = float(best_lasso[0])
            row["cv_lasso_mse"] = mse(
                fit_predict(
                    lambda x, y: elastic_net_weights(
                        x, y, float(best_lasso[0]), float(best_lasso[1])
                    ),
                    x_train, y_train, x_test, center=True,
                ),
                y_test,
            )

        best_ncomp, pcr_cv_curve = cross_validate(
            pcr_path_solver(), pcr_components, x_train, y_train,
            n_folds=n_folds, seed=cv_seed,
        )
        row["cv_pcr_n_components"] = int(best_ncomp)
        row["cv_pcr_mse"] = mse(
            fit_predict(
                lambda x, y: pcr_weights(x, y, int(best_ncomp)),
                x_train, y_train, x_test, center=True,
            ),
            y_test,
        )
        row["pcr_curve"] = pcr_cv_curve

        # ---- 5. Oracle 上界（偷看测试集，不可作为方法）----
        oracle_alpha = min(
            ridge_grid, key=lambda a: row[f"test_sweep_ridge_mse_alpha_{a}"]
        )
        row["oracle_alpha"] = float(oracle_alpha)
        row["oracle_ridge_mse"] = row[f"test_sweep_ridge_mse_alpha_{oracle_alpha}"]

        # ---- 6. v1 的"最优"（测试集 argmin，冻结网格）----
        v1_best_alpha = min(v1_alphas, key=lambda a: row[f"v1_ridge_mse_alpha_{a}"])
        row["v1_alpha"] = float(v1_best_alpha)
        row["v1_ridge_mse"] = row[f"v1_ridge_mse_alpha_{v1_best_alpha}"]
        row["v1_improvement_pct"] = (
            (row["ols_mse_v1_protocol"] - row["v1_ridge_mse"])
            / row["ols_mse_v1_protocol"]
            * 100.0
        )

        per_seed.append(row)

    return _aggregate(per_seed, seeds, v1_alphas, ridge_grid, l1_ratios, pcr_components, n_folds, cv_seed)


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    """返回 ``(均值, 样本标准差)``；少于 2 个样本时标准差记 0。"""
    arr = np.asarray(list(values), dtype=float)
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    return float(np.mean(arr)), std


def _aggregate(
    per_seed: list[dict[str, Any]],
    seeds: Sequence[int],
    v1_alphas: Sequence[float],
    ridge_grid: Sequence[float],
    l1_ratios: Sequence[float],
    pcr_components: Sequence[int],
    n_folds: int,
    cv_seed: int,
) -> ExperimentResult:
    """把逐种子记录聚合成可直接被台账引用的 ``results`` 字典。"""

    def agg(key: str) -> tuple[float, float]:
        return _mean_std([row[key] for row in per_seed])

    results: dict[str, Any] = {}

    # --- 参照 ---
    results["null_mse_mean"], results["null_mse_std"] = agg("null_mse")
    results["ols_mse_mean"], results["ols_mse_std"] = agg("ols_mse")

    # --- v1 协议（错误协议，保留以便量化归因）---
    results["v1_ols_mse_mean"], results["v1_ols_mse_std"] = agg("ols_mse_v1_protocol")
    if "v1_ridge_mse" in per_seed[0]:
        results["v1_ridge_mse_mean"], results["v1_ridge_mse_std"] = agg("v1_ridge_mse")
        results["v1_best_alpha"] = _mode_of([row["v1_alpha"] for row in per_seed])
        results["v1_improvement_pct_mean"] = round(
            float(np.mean([row["v1_improvement_pct"] for row in per_seed])), 2
        )
        results["v1_improvement_pct_aggregate"] = round(
            (results["v1_ols_mse_mean"] - results["v1_ridge_mse_mean"])
            / results["v1_ols_mse_mean"]
            * 100.0,
            2,
        )

    # --- v2 协议（正确协议）：主表 ---
    results["ridge_mse_mean"], results["ridge_mse_std"] = agg("cv_ridge_mse")
    results["best_alpha"] = _mode_of([row["cv_alpha"] for row in per_seed])
    results["improvement_pct"] = round(
        (results["ols_mse_mean"] - results["ridge_mse_mean"])
        / results["ols_mse_mean"]
        * 100.0,
        2,
    )
    if "cv_lasso_mse" in per_seed[0]:
        results["lasso_mse_mean"], results["lasso_mse_std"] = agg("cv_lasso_mse")
        results["best_lambda_lasso"] = round(
            float(np.median([row["cv_lasso_lambda"] for row in per_seed])), 4
        )
    results["enet_mse_mean"], results["enet_mse_std"] = agg("cv_enet_mse")
    results["pcr_mse_mean"], results["pcr_mse_std"] = agg("cv_pcr_mse")

    results["best_lambda_enet"] = round(
        float(np.median([row["cv_enet_lambda"] for row in per_seed])), 4
    )
    results["best_l1_ratio_enet"] = _mode_of(
        [row["cv_enet_l1_ratio"] for row in per_seed]
    )
    results["best_n_components_pcr"] = _mode_of(
        [row["cv_pcr_n_components"] for row in per_seed]
    )

    # --- 选择偏差的量化 ---
    results["oracle_ridge_mse_mean"], results["oracle_ridge_mse_std"] = agg(
        "oracle_ridge_mse"
    )
    results["oracle_best_alpha"] = _mode_of([row["oracle_alpha"] for row in per_seed])
    if "v1_ridge_mse_mean" in results:
        results["selection_bias_mse_abs"] = round(
            results["v1_ridge_mse_mean"] - results["ridge_mse_mean"], 4
        )
        results["selection_bias_pct_points"] = round(
            results["v1_improvement_pct_aggregate"] - results["improvement_pct"], 2
        )
    results["ridge_gap_to_oracle_pct"] = round(
        (results["ridge_mse_mean"] - results["oracle_ridge_mse_mean"])
        / results["oracle_ridge_mse_mean"]
        * 100.0,
        2,
    )

    # --- 元信息 ---
    results["noise_floor_mse"] = NOISE_FLOOR_MSE
    results["noise_floor_derivation"] = noise_floor_derivation()
    results["ols_excess_over_floor_ratio"] = round(
        results["ols_mse_mean"] / NOISE_FLOOR_MSE, 2
    )
    results["ridge_excess_over_floor_ratio"] = round(
        results["ridge_mse_mean"] / NOISE_FLOOR_MSE, 2
    )

    # --- R^2：以零模型（预测训练集均值）为参照，比"MSE 降幅"更好解释 ---
    # R^2 = 1 - MSE_method / MSE_null，逐种子计算再聚合；
    # 零模型的 R^2 恒为 0，因此它的数值不进表。
    r2_sources = {
        "ols": "ols_mse",
        "ridge": "cv_ridge_mse",
        "enet": "cv_enet_mse",
        "pcr": "cv_pcr_mse",
        "lasso": "cv_lasso_mse",
        "v1_ridge": "v1_ridge_mse",
        "oracle_ridge": "oracle_ridge_mse",
    }
    for label, key in r2_sources.items():
        if key not in per_seed[0]:
            continue
        values = [
            1.0 - row[key] / row["null_mse"]
            for row in per_seed
            if row["null_mse"] > 0.0
        ]
        results[f"{label}_r2_mean"], results[f"{label}_r2_std"] = _mean_std(values)

    results["best_method_mse_mean"] = round(
        min(
            results["ridge_mse_mean"],
            results["enet_mse_mean"],
            results["pcr_mse_mean"],
            results.get("lasso_mse_mean", float("inf")),
        ),
        4,
    )
    results["n_seeds"] = len(seeds)
    results["n_train"] = N_TRAIN
    results["n_test"] = N_TEST
    results["n_features"] = N_FEATURES
    results["n_informative"] = N_INFORMATIVE
    results["cv_folds"] = int(n_folds)
    results["cv_seed"] = int(cv_seed)

    # --- 曲线 ---
    results["alpha_sweep_v1_test"] = [
        {
            "alpha": float(a),
            "mse_mean": float(
                np.mean([row[f"v1_ridge_mse_alpha_{a}"] for row in per_seed])
            ),
            "mse_std": float(
                np.std([row[f"v1_ridge_mse_alpha_{a}"] for row in per_seed], ddof=1)
            )
            if len(per_seed) > 1
            else 0.0,
        }
        for a in v1_alphas
    ]
    results["alpha_sweep_v2_test"] = [
        {
            "alpha": float(a),
            "mse_mean": float(
                np.mean([row[f"test_sweep_ridge_mse_alpha_{a}"] for row in per_seed])
            ),
            "mse_std": float(
                np.std([row[f"test_sweep_ridge_mse_alpha_{a}"] for row in per_seed], ddof=1)
            )
            if len(per_seed) > 1
            else 0.0,
        }
        for a in ridge_grid
    ]
    results["alpha_sweep_cv"] = _average_cv_curve(
        [row["cv_ridge_curve"] for row in per_seed]
    )
    results["pcr_sweep_cv"] = _average_cv_curve([row["pcr_curve"] for row in per_seed])
    results["enet_sweep_cv"] = _average_cv_curve([row["enet_curve"] for row in per_seed])

    return ExperimentResult(
        experiment=EXPERIMENT_NAME,
        config={
            "seeds": list(seeds),
            "v1_alphas": [float(a) for a in v1_alphas],
            "ridge_alpha_grid": [float(a) for a in ridge_grid],
            "lambda_grid_points": LAMBDA_GRID_POINTS,
            "l1_ratios": [float(r) for r in l1_ratios],
            "pcr_components": list(pcr_components),
            "cv_folds": int(n_folds),
            "cv_seed": int(cv_seed),
            "coord_max_iter": COORD_MAX_ITER,
            "coord_tol": COORD_TOL,
            "n_features": N_FEATURES,
            "n_informative": N_INFORMATIVE,
            "n_train": N_TRAIN,
            "n_test": N_TEST,
            "noise_sigma": NOISE_SIGMA,
            "selection_protocol": (
                "model selection by 5-fold CV inside the training split; "
                "the test split is evaluated exactly once"
            ),
            "center": True,
        },
        results=results,
        per_seed=per_seed,
    )


def _mode_of(values: Sequence[Any]) -> Any:
    """众数；次数相同时取 ``str`` 排序最靠前的那个，保证结果确定。"""
    counts: dict[str, tuple[Any, int]] = {}
    for value in values:
        payload = param_to_json(value)
        key = json.dumps(payload, sort_keys=True)
        stored, count = counts.get(key, (value, 0))
        counts[key] = (stored, count + 1)
    best_key = max(sorted(counts.keys()), key=lambda k: counts[k][1])
    return param_from_json(json.loads(best_key))


def _median_of(values: Sequence[float]) -> float:
    """中位数（比均值更抗单个种子的极端选参）。"""
    return float(np.median(np.asarray(list(values), dtype=float)))


def _average_cv_curve(curves: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    """把逐种子的交叉验证曲线按超参对齐后取均值。"""
    if not curves:
        return []
    buckets: dict[str, dict[str, Any]] = {}
    for curve in curves:
        for point in curve:
            key = json.dumps(param_to_json(point["param"]), sort_keys=True)
            bucket = buckets.setdefault(
                key, {"param": point["param"], "means": [], "stds": []}
            )
            bucket["means"].append(point["cv_mse_mean"])
            bucket["stds"].append(point["cv_mse_std"])
    out: list[dict[str, Any]] = []
    for key in sorted(buckets.keys()):
        bucket = buckets[key]
        out.append(
            {
                "param": param_to_json(bucket["param"]),
                "cv_mse_mean": float(np.mean(bucket["means"])),
                "cv_mse_std": float(np.std(bucket["means"], ddof=1))
                if len(bucket["means"]) > 1
                else 0.0,
            }
        )
    return out
