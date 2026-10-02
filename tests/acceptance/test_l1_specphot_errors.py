"""L1: specphot errors.py（S1-e 误差三分解 + 代理误差 + AR(1) 放大）。

覆盖条款：
  T-32  σ_cal 分支纪律：参与锚定方差 ×(1−h_i)，未参与加 (2.5/ln10·σ_κ/κ*)²（F-56）
  T-34  一致性因子：j=1/j=2 二阶差分与 mad_window 都回收 σ 到 1%；未乘因子偏 ~65%
  T-35  AR(1) 放大：r=0.31 ⇒ corr_infl=1.38（1%）；点数 <30 ⇒ CA-21 不默认 r=0
  F-23  误差列全 0/全 NaN/缺失 ⇒ 等同缺失
  F-50  C_syn 共享像素协方差（与 fluxcal 的 T-29 联用）
  F-57  σ_resp 下限 = 两加权之差

随机化判据 seed 一律 20260927（§10.1 元判据）；纯函数、脱库可跑。
"""
import math

import numpy as np
import pytest

from specphot import errors as ER
from specphot import fluxcal as FC
from specphot.constants import C_SCALE_2ND_DIFF, C_MAD_SCALE, C_RHO_MIN, C_SIGMA_STRIDE

SEED = 20260927


# ─── T-34：一致性因子（白噪声 σ 已知） ──────────────────────────────────

def test_t34_second_diff_recovers_sigma_both_strides():
    rng = np.random.default_rng(SEED)
    noise = rng.normal(0.0, 1.3, 60000)
    for j in (1, C_SIGMA_STRIDE):
        est = ER.sigma_second_diff(noise, j=j)
        assert abs(est / 1.3 - 1.0) < 0.01
    assert C_SIGMA_STRIDE == 2


def test_t34_unfactored_median_overestimates_by_65pct():
    rng = np.random.default_rng(SEED)
    noise = rng.normal(0.0, 1.0, 60000)
    j = 2
    raw = float(np.median(np.abs(2.0 * noise[j:-j] - noise[:-2 * j] - noise[2 * j:])))
    assert raw / 1.0 == pytest.approx(1.0 / C_SCALE_2ND_DIFF, rel=0.01)  # ≈1.652


def test_t34_mad_window_recovers_sigma():
    rng = np.random.default_rng(SEED)
    noise = rng.normal(0.0, 1.3, 60000)
    est = ER.sigma_mad_window(noise)
    assert abs(est / 1.3 - 1.0) < 0.01
    # 与二阶差分支同谱互洽（都回收 ⇒ 互差必然也小）
    assert abs(est / ER.sigma_second_diff(noise) - 1.0) < 0.02


def test_sigma_estimators_degenerate_inputs():
    assert ER.sigma_second_diff([1.0, 2.0, 3.0]) is None       # 点数 < 2j+1
    assert ER.sigma_mad_window(np.ones(10)) is None            # 点数 < 窗


# ─── T-35：AR(1) 放大 ──────────────────────────────────────────────────

def _ar1(rho, n, seed=SEED):
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 1.0, n)
    from scipy.signal import lfilter
    return lfilter([math.sqrt(1.0 - rho * rho)], [1.0, -rho], eps)


def test_t35_ar1_inflation_recovers_138():
    series = _ar1(0.31, 400000)
    rho, warn = ER.rho_lag1(series)
    assert warn is None
    assert rho == pytest.approx(0.31, abs=0.02)
    assert ER.corr_inflation(rho) == pytest.approx(1.38, rel=0.01)


def test_t35_short_series_ca21_not_silent_zero():
    rho, warn = ER.rho_lag1(np.arange(29.0))
    assert rho is None and warn is not None and warn['code'] == 'CA-21'
    # 不可估 ⇒ 不放大，但 CA-21 必须跟着走（不得默认 r=0 静默通过）
    assert ER.corr_inflation(rho) == 1.0


def test_t35_below_threshold_no_inflation():
    assert ER.corr_inflation(C_RHO_MIN - 0.01) == 1.0
    assert ER.corr_inflation(C_RHO_MIN) == 1.0
    assert ER.corr_inflation(None) == 1.0


# ─── F-23：误差列退化 ───────────────────────────────────────────────────

def test_f23_err_column_degenerate_equals_missing():
    assert ER.usable_err_column(None) is False
    assert ER.usable_err_column([0.0, 0.0, 0.0]) is False
    assert ER.usable_err_column([float('nan')] * 5) is False
    assert ER.usable_err_column([0.0, 0.1, 0.2]) is True


# ─── 传播与协方差 ───────────────────────────────────────────────────────

def test_propagate_band_sigma_linear_weights():
    w = np.array([0.1, -0.2, 0.3, 0.15])
    sigma, infl = ER.propagate_band_sigma(w, 2.0)
    assert sigma == pytest.approx(math.sqrt(np.sum((w * 2.0) ** 2)), rel=1e-12)
    assert infl == 1.0
    sigma2, infl2 = ER.propagate_band_sigma(w, 2.0, rho=0.5)
    assert infl2 == pytest.approx(math.sqrt(1.5 / 0.5), rel=1e-12)
    assert sigma2 == pytest.approx(sigma * infl2, rel=1e-12)


def test_f50_cov_syn_shared_pixels_only():
    idx1 = np.array([0, 1, 2])
    idx2 = np.array([2, 3, 4])
    w1 = np.array([0.5, 0.5, 0.5])
    w2 = np.array([0.25, 0.25, 0.25])
    C = ER.cov_syn([(idx1, w1), (idx2, w2)], 1e-16)
    assert C[0, 1] == pytest.approx(0.5 * 0.25 * 1e-32, rel=1e-12)  # 仅像素 2 共享
    assert C[0, 0] == pytest.approx(3 * 0.25 * 1e-32, rel=1e-12)
    idx3 = np.array([5, 6])
    C2 = ER.cov_syn([(idx1, w1), (idx3, w1[:2])], 1e-16)
    assert C2[0, 1] == 0.0                                           # 无共享像素


# ─── T-32：σ_cal 分支纪律（F-56） ───────────────────────────────────────

def test_t32_sigma_cal_two_branches_not_one_factor():
    f = np.array([1e-15, 2e-15, 3e-15])
    sig = np.array([0.05, 0.02, 0.08]) * 2.5e-15      # 不等权重 ⇒ h_i 互不相同
    g = 2.5 * f + np.array([0.01, -0.02, 0.015]) * 2.5e-15
    res = FC.anchored_fit(f, g, np.diag(sig**2))
    h = res['leverage']
    assert len(h) == 3
    assert all(0.0 < hi < 1.0 for hi in h)
    assert len(set(round(hi, 12) for hi in h)) == 3   # 权重不等 ⇒ 杠杆互异
    var_in = 0.04
    vars_part = [ER.delta_m_var_anchored(var_in, hi) for hi in h]
    for hi, v in zip(h, vars_part):
        assert v == pytest.approx(var_in * (1.0 - hi), rel=1e-12)
    # 未参与锚定的波段：加 (2.5/ln10·σ_κ/κ*)²（完全相关，不消去）
    e_cal = ER.mag_err_cal_nonparticipant(res['sigma'], res['value'])
    assert e_cal == pytest.approx(
        (2.5 / math.log(10.0)) * res['sigma'] / res['value'], rel=1e-12)
    # 对全波段套同一因子的实现过不了：参与波段间因子互异，且与未参与支不等价
    assert not all(v == pytest.approx(vars_part[0], rel=1e-9) for v in vars_part[1:])
    assert e_cal**2 != pytest.approx(var_in * (1.0 - h[0]), rel=0.5)


# ─── F-57：σ_resp 下限 ──────────────────────────────────────────────────

def test_f57_resp_lower_bound_is_two_weighting_diff():
    assert ER.sigma_resp_lower_bound(20.010, 19.990) == pytest.approx(0.020,
                                                                      rel=1e-12)
    assert ER.NOT_IN_BUDGET == ['wavecal', 'aperture', 'flux_calibration',
                                'sky_subtraction']
