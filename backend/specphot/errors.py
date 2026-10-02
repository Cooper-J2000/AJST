"""specphot S1-e：误差三分解（σ_stat / σ_cal / σ_resp）与代理误差（02 §3.5）。

纪律：
  - 三分量分列输出，禁止合成单一总误差入库（F-21）；not_in_budget 至少含
    wavecal / aperture / flux_calibration / sky_subtraction（F-58）。
  - 二阶差分代理必须乘一致性因子（F-54）：σ_px = C_SCALE_2ND_DIFF·median|2F_i − F_{i−j} − F_{i+j}|，
    j = C_SIGMA_STRIDE = 2（L-46 式(1) 转述的 DER_SNR 原设置）；未乘因子偏大 ~65%（T-34）。
    mad_window 支必须乘 C_MAD_SCALE；C_MAD_WIN 只用于该支。
  - AR(1) 放大（F-55）：r > C_RHO_MIN 时积分 σ 乘 √((1+r)/(1−r)) 并回显 rho_lag1
    与 corr_infl；r 不可估（点数 < 30）挂 CA-21，不得默认 r=0。
  - 次序纪律（F-97①②）：代理 σ 先按 k_proxy(ρ, j) 去偏（修估计量本身），再进
    F-55 的积分放大（修传播）；两个因子不可互相顶替，也不可只用其一。
  - σ_cal 消去规则（F-56）：参与锚定的波段 delta_m 方差乘 (1 − h_i)；未参与的
    加上 (2.5/ln10 · σ_κ/κ*)²。禁止全波段同一因子。
  - σ_resp 是下限（F-57）：默认 = 光子/能量两加权之差（resp_method='lower_bound'）。
"""
import math

import numpy as np

from .constants import (C_SCALE_2ND_DIFF, C_MAD_SCALE, C_MAD_WIN,
                        C_RHO_MIN, C_SIGMA_STRIDE)

MAG_PER_LN_FLUX = 2.5 / math.log(10.0)   # σ_m = MAG_PER_LN_FLUX · σ_f/f
NOT_IN_BUDGET = ['wavecal', 'aperture', 'flux_calibration',
                 'sky_subtraction']       # F-58 的下限集


def _warn(code, message):
    return {'code': code, 'message': message}


def usable_err_column(flux_err):
    """F-23：误差列全 0 / 全 NaN / 不可解析 ⇒ 等同缺失（代理 + CA-06）。"""
    if flux_err is None:
        return False
    e = np.asarray(flux_err, dtype=float)
    return bool(np.any(np.isfinite(e) & (e > 0.0)))


# ─── σ_stat：代理逐像素 σ 与传播 ───────────────────────────────────────

def sigma_second_diff(flux, j=C_SIGMA_STRIDE):
    """F-54 默认支（sigma_method='second_diff'）。点数 < 2j+1 ⇒ None（由装配层挂 CA-06）。"""
    f = np.asarray(flux, dtype=float)
    if f.size < 2 * j + 1:
        return None
    d = np.abs(2.0 * f[j:-j] - f[:-2 * j] - f[2 * j:])
    return float(C_SCALE_2ND_DIFF * np.median(d))


def sigma_second_diff_dual(flux):
    """F-97⑤ 双 j 对照：同一条谱同时算 j=1 与 j=C_SIGMA_STRIDE 两个 σ̂（原始估计）。

    返回 {'j1': …, 'j2': …}；某 j 点数不足 ⇒ 该键 None，由装配层置响应双 j 键为
    null（不冒充，TXT-23）。取大者与一致性判据在装配层按 |ρ| 与 C_RHO_MIN 裁决。"""
    return {'j1': sigma_second_diff(flux, j=1),
            'j2': sigma_second_diff(flux, j=C_SIGMA_STRIDE)}


def k_proxy(rho, j=C_SIGMA_STRIDE):
    """F-97① 的去偏因子：k_proxy(ρ,j) = √((6 − 8ρ^j + 2ρ^(2j))/6)。

    核 [1,−2,1] 施加在间隔 j 个像素的三点上、AR(1) 系数 ρ 时的核方差
    σ²(6 − 8ρ^j + 2ρ^(2j))（就地展开：Σa_k² = 6，两两协方差项 2[−2ρ^j − 2ρ^j +
    ρ^(2j)]）。直接把 σ̂ 当逐像素 σ 会系统性偏小，须先除以本因子再去偏：
    σ_px = σ̂ / k_proxy(ρ, j_used)。锚点（F-97① 正文）：k_proxy(0.5,1)=0.6455、
    k_proxy(0.5,2)=0.8292、k_proxy(0.9,1)=0.2646（模块级 assert 钉 1e-3）。
    ρ 不可估（None）⇒ 返回 1.0（不做去偏，也不冒充做过）。"""
    if rho is None:
        return 1.0
    return math.sqrt((6.0 - 8.0 * rho ** j + 2.0 * rho ** (2 * j)) / 6.0)


# F-97① 的三个正文数值锚点（容差 1e-3，防手滑改错公式方向）
assert abs(k_proxy(0.5, 1) - 0.6455) < 1e-3
assert abs(k_proxy(0.5, 2) - 0.8292) < 1e-3
assert abs(k_proxy(0.9, 1) - 0.2646) < 1e-3


def sigma_mad_window(flux, win=C_MAD_WIN):
    """F-54 备选支（sigma_method='mad_window'）：滑窗残差的 MAD × C_MAD_SCALE。

    残差 = F − 窗内**均值**（不是中位数）：正态减去窗均值仍是正态，MAD 一致性成立，
    实测回收偏差 −0.7%（T-34 容差 1% 内）；减窗中位数会把残差分布压离正态
    （中位数追逐中心样本），实测偏差 −2.3% 超容差，故不用。"""
    f = np.asarray(flux, dtype=float)
    win = int(win)
    if f.size < win or win < 3:
        return None
    half = win // 2
    windows = np.lib.stride_tricks.sliding_window_view(f, win)
    resid = f[half:f.size - (win - 1 - half)] - np.mean(windows, axis=1)
    return float(C_MAD_SCALE * np.median(np.abs(resid - np.median(resid))))


def rho_lag1(flux):
    """F-55 的滞后一阶自相关估计 → (rho, warning)。

    估计量：一阶差分的 lag-1 相关 ρ_d，AR(1) 下 ρ_d = −(1−r)/2 ⇒ r = 1 + 2ρ_d
    （差分顺带消去缓变连续谱趋势；F-97 的去偏与双 j 报告随预处理切片细化）。
    点数 < 30 ⇒ (None, CA-21)：不得默认 r=0 静默通过（T-35）。
    """
    f = np.asarray(flux, dtype=float)
    if f.size < 30:
        return None, _warn('CA-21', '点数 < 30，相邻像素相关性不可估，'
                                    '未做 AR(1) 放大（不等于 r=0）')
    d = np.diff(f)
    if float(np.std(d[:-1])) == 0.0 or float(np.std(d[1:])) == 0.0:
        return 0.0, None
    rho_d = float(np.corrcoef(d[:-1], d[1:])[0, 1])
    return max(min(1.0 + 2.0 * rho_d, 0.999), -0.999), None


def corr_inflation(rho):
    """F-55：r > C_RHO_MIN ⇒ √((1+r)/(1−r))；否则 1.0（r=None 不放大）。"""
    if rho is None or rho <= C_RHO_MIN:
        return 1.0
    return math.sqrt((1.0 + rho) / (1.0 - rho))


def propagate_band_sigma(weights_val, sigma_px, rho=None):
    """逐像素 σ（独立假设）经积分权重传播到 ⟨Fν⟩，再按 F-55 乘 AR(1) 放大。

    weights_val = response.band_integrals 的 ∂⟨Fν⟩/∂F_p；sigma_px 标量或与
    权重同长数组。返回 (σ_flux, corr_infl)。
    """
    w = np.asarray(weights_val, dtype=float)
    s = np.broadcast_to(np.asarray(sigma_px, dtype=float), w.shape)
    infl = corr_inflation(rho)
    return float(np.sqrt(np.sum((w * s) ** 2))) * infl, infl


def cov_syn(weights_list, sigma_px):
    """F-50：波段协方差 C_syn,ij = Σ_p (∂f_i/∂F_p)(∂f_j/∂F_p) σ_p²（共享像素 ⇒ 非对角）。

    weights_list = [(weights_idx, weights_val), …]（response.band_integrals 的稀疏权重）。
    """
    n = len(weights_list)
    C = np.zeros((n, n), dtype=float)
    w_maps = [dict(zip(np.asarray(idx, dtype=int).tolist(),
                       np.asarray(val, dtype=float).tolist()))
              for idx, val in weights_list]
    s = np.asarray(sigma_px, dtype=float)
    for i in range(n):
        for j in range(i, n):
            shared = w_maps[i].keys() & w_maps[j].keys()
            acc = 0.0
            for p in shared:
                sp = s if s.ndim == 0 else s[p]
                acc += w_maps[i][p] * w_maps[j][p] * sp * sp
            C[i, j] = C[j, i] = acc
    return C


def mag_err_from_flux(flux, sigma_flux):
    """σ_m = (2.5/ln10)·σ_f/f（线性传播，闭式）。"""
    return MAG_PER_LN_FLUX * float(sigma_flux) / float(flux)


# ─── σ_cal：anchored 的消去/传播规则（F-56） ────────────────────────────

def delta_m_var_anchored(var_in, leverage_h):
    """参与锚定的波段：delta_m 方差 = var_in · (1 − h_i)（杠杆收缩，0<h_i<1）。"""
    return float(var_in) * (1.0 - float(leverage_h))


def mag_err_cal_nonparticipant(sigma_kappa, kappa):
    """未参与锚定的波段：mag_err_cal = (2.5/ln10)·σ_κ/κ*（完全相关，不消去）。"""
    return MAG_PER_LN_FLUX * float(sigma_kappa) / float(kappa)


# ─── σ_resp：响应口径系统误差的下限（F-57） ─────────────────────────────

def sigma_resp_lower_bound(mag_photon, mag_energy):
    """resp_method='lower_bound'：光子 vs 能量两加权之差的绝对值（下限，非区间）。"""
    return abs(float(mag_photon) - float(mag_energy))
