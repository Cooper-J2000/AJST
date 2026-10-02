"""specphot S1-a/S1-b/S1-d：波段平均（式 1/2）、零点与单位、响应曲线口径。

纪律（02 §3.1/3.2/3.4）：
  - 积分一律在谱的**原生 λ 网格**上梯形求积（F-5），曲线 T 只做线性插值到谱网格
    （F-46，interp='linear_T_on_spec' 固定回显；反向插值禁止）。
  - 两式对 T 的绝对归一化零阶齐次（F-1）⇒ 峰归一曲线直接用。
  - 代表波长并报 pivot/phot/iso 三个（F-18），禁止只报一个 "mean λ"；
    曲线侧波长（pivot/phot）在裁剪+掩膜后的有效通带上重算（F-53）。
  - lambda_iso_aa = sqrt(c·⟨Fν⟩/⟨Fλ⟩)：⟨Fλ⟩ 取与 ⟨Fν⟩ **同测度**的波段平均
    （photon ⇒ 测度 T·dλ/λ，energy ⇒ 测度 T·dλ/λ²），式(4) 因此是式(3) 的恒等改写
    （F-47）；拿 pivot/eff 代入 (4) 必不等（T-30）。
  - lambda_eff_aa 需要 Vega 参考谱，P1 无 ⇒ 恒 null + CA-15（F-19）。
  - 数值进积分前一律 float()；空气→真空不二次转换（reader 已保证真空，F-79）。
"""
import math

import numpy as np

from .constants import (
    C_AA_PER_S, C_AB_ZERO, C_AB_LAM_ZERO, C_AB_MJY_ZERO, C_ST_LAM_CONST,
    C_MIN_PIXELS, C_OVERLAP_REJECT, C_OVERLAP_MIN,
)
from .reader import SpecLoadError

LN10_OVER_25 = math.log(10.0) / 2.5  # d m / d ln f（星等↔流量传播因子）


def _warn(code, message):
    return {'code': code, 'message': message}


# ─── 零点与单位（S1-b，式 3/4/4'/5；常量全精度值唯一载体在 constants.py） ───

def ab_mag_from_fnu(fnu_cgs):
    """式 (3)：⟨Fν⟩[erg s⁻¹ cm⁻² Hz⁻¹] → m_AB。"""
    return -2.5 * math.log10(float(fnu_cgs)) - C_AB_ZERO


def ab_mag_from_flambda(flam_cgs, lam_iso_aa):
    """式 (4)：⟨Fλ⟩ 空间写法（仅交叉核对用，唯一计算路径是式 (3)，F-47）。"""
    return (-2.5 * math.log10(float(flam_cgs))
            - 5.0 * math.log10(float(lam_iso_aa)) - C_AB_LAM_ZERO)


def st_mag_from_flambda(flam_st, lam_eff_st_aa):
    """式 (4') 的合并常数写法；⟨Fλ⟩_ST 是 T·λ dλ 第三种加权（F-8）。

    P1 一律拒绝 ST（E-08，库内无 Vega 参考谱，F-48）；本函数仅为 T-52 自检与
    后续解锁保留，不得经 API 出数。"""
    return (-2.5 * math.log10(float(flam_st))
            - 5.0 * math.log10(float(lam_eff_st_aa)) - C_ST_LAM_CONST)


def ab_mag_to_fnu_cgs(mag):
    return 10.0 ** (-0.4 * (float(mag) + C_AB_ZERO))


def ab_mag_to_f_mjy(mag):
    """式 (5)，零点 16.4 与宿主 bands.js / extinction.py 同值（F-7）。"""
    return 10.0 ** ((C_AB_MJY_ZERO - float(mag)) / 2.5)


def f_mjy_to_ab_mag(f_mjy):
    return C_AB_MJY_ZERO - 2.5 * math.log10(float(f_mjy))


# ─── 曲线口径（S1-d：F-46 节点口径、F-2 的 σ²_lnl） ────────────────────

def curve_metrics(curve_lam, curve_tr):
    """F-46：FWHM（半高处线性插值定端点）、通带节点数（区间内采样点扣峰值点）、
    curve_px_per_fwhm = FWHM ÷ 曲线中位节点间距（< C_CURVE_PX_PER_FWHM_MIN ⇒ CA-01）。"""
    cl = np.asarray(curve_lam, dtype=float)
    ct = np.asarray(curve_tr, dtype=float)
    i_max = int(np.argmax(ct))
    half = 0.5 * float(ct[i_max])
    lo = hi = None
    for i in range(i_max, 0, -1):
        if ct[i - 1] < half <= ct[i]:
            lo = cl[i - 1] + (half - ct[i - 1]) * (cl[i] - cl[i - 1]) / (ct[i] - ct[i - 1])
            break
    for i in range(i_max, len(cl) - 1):
        if ct[i] >= half > ct[i + 1]:
            hi = cl[i] + (half - ct[i]) * (cl[i + 1] - cl[i]) / (ct[i + 1] - ct[i])
            break
    if lo is None or hi is None or hi <= lo:
        return {'fwhm_aa': None, 'curve_nodes': 0, 'curve_px_per_fwhm': 0.0}
    fwhm = float(hi - lo)
    nodes = int(np.count_nonzero((cl >= lo) & (cl <= hi))) - 1  # 扣峰值点本身（F-46）
    med_step = float(np.median(np.diff(cl)))
    px = fwhm / med_step if med_step > 0 else 0.0
    return {'fwhm_aa': fwhm, 'curve_nodes': max(nodes, 0), 'curve_px_per_fwhm': px}


def sigma2_lnl(curve_lam, curve_tr):
    """F-2 的 σ²_lnl ≡ Var_p(ln λ)，p 的测度 = T·dλ/λ（禁止用 (σ_λ/λ)² 替代）。"""
    cl = np.asarray(curve_lam, dtype=float)
    ct = np.asarray(curve_tr, dtype=float)
    w = ct / cl
    x = np.log(cl)
    den = np.trapezoid(w, cl)
    m1 = np.trapezoid(w * x, cl) / den
    m2 = np.trapezoid(w * x * x, cl) / den
    return float(max(m2 - m1 * m1, 0.0))


def _measure_t_dlam_over_lam(cl, ct, lo, hi):
    """∫T dλ/λ 于 [lo,hi]，端点线性插值（重叠比口径的分子/分母共用）。"""
    if hi <= lo:
        return 0.0
    t_lo = float(np.interp(lo, cl, ct, left=0.0, right=0.0))
    t_hi = float(np.interp(hi, cl, ct, left=0.0, right=0.0))
    sel = (cl > lo) & (cl < hi)
    xs = np.concatenate(([lo], cl[sel], [hi]))
    ts = np.concatenate(([t_lo], ct[sel], [t_hi]))
    return float(np.trapezoid(ts / xs, xs))


# ─── 波段积分（式 1/2 + 裁剪/掩膜/非正剔除 + 代表波长 + 传播权重） ────────

def band_integrals(lam_aa, flux_flambda, curve_lam, curve_tr, *,
                   weighting='photon', mask_ranges=None, allow_mono=False,
                   mono_lam_ref=None):
    """返回该波段的合成流量与口径回显键；不可用时抛 SpecLoadError('E-04', reason=…)。

    curve_lam/curve_tr = None ⇒ 无曲线：allow_mono 且有 mono_lam_ref 时走单色近似
    （band_mode='mono'，F-16），否则 E-04(curve_missing)。
    返回键含 weights_idx/weights_val（∂⟨Fν⟩/∂F_p 稀疏向量，errors.py 的 σ_stat
    传播与 F-50 波段协方差 C_syn 的唯一来源）。
    """
    if weighting not in ('photon', 'energy'):
        raise SpecLoadError('E-14', f"weighting 只能是 photon/energy，收到 {weighting!r}",
                            reason='bad_enum')
    lam = np.asarray(lam_aa, dtype=float)
    flx = np.asarray(flux_flambda, dtype=float)
    warnings = []

    if curve_lam is None or curve_tr is None:
        if not allow_mono or mono_lam_ref is None:
            raise SpecLoadError('E-04', '该波段无响应曲线；可用 mono 单色近似需显式授权',
                                reason='curve_missing')
        lam0 = float(mono_lam_ref)
        f0 = float(np.interp(lam0, lam, flx))
        fnu = f0 * lam0 * lam0 / C_AA_PER_S
        return {'fnu_cgs': fnu, 'flambda_cgs': f0, 'band_mode': 'mono',
                'weighting': weighting, 'lambda_pivot_aa': lam0, 'lambda_phot_aa': None,
                'lambda_iso_aa': lam0, 'lambda_eff_aa': None, 'sigma2_lnl': 0.0,
                'overlap': None, 'n_used_pixels': None, 'n_masked_pixels': 0,
                'n_nonpos': 0, 'nonpos_frac': 0.0, 'weights_idx': None,
                'weights_val': None, 'band_lo_aa': lam0, 'band_hi_aa': lam0,
                'warnings': warnings}

    cl = np.asarray(curve_lam, dtype=float)
    ct = np.asarray(curve_tr, dtype=float)
    plo, phi = float(cl[0]), float(cl[-1])
    total = _measure_t_dlam_over_lam(cl, ct, plo, phi)
    covered = _measure_t_dlam_over_lam(cl, ct, max(plo, float(lam[0])),
                                       min(phi, float(lam[-1])))
    overlap = covered / total if total > 0 else 0.0
    if overlap < C_OVERLAP_REJECT:
        raise SpecLoadError('E-04', f'通带与谱重叠比 {overlap:.2f} < {C_OVERLAP_REJECT}',
                            reason='no_overlap')
    if overlap < C_OVERLAP_MIN:
        warnings.append(_warn('CA-05',
                              f'通带覆盖不完整（overlap={overlap:.3f}），结果被截断'))

    sel = (lam >= plo) & (lam <= phi)
    n_band = int(np.count_nonzero(sel))
    idx = np.nonzero(sel)[0]
    if mask_ranges:
        keep = np.ones(len(idx), dtype=bool)
        for mlo, mhi in mask_ranges:
            keep &= ~((lam[idx] >= float(mlo)) & (lam[idx] <= float(mhi)))
        idx = idx[keep]
    n_masked = n_band - len(idx)
    pos = flx[idx] > 0.0
    n_nonpos = int(np.count_nonzero(~pos))
    if n_nonpos:
        warnings.append(_warn('CA-10',
                              f'通带内 {n_nonpos} 个非正流量像素已局部剔除（V-6）'))
        idx = idx[pos]
    n_used = len(idx)
    if n_used < C_MIN_PIXELS:
        raise SpecLoadError('E-04',
                            f'通带内可用像素 {n_used} < {C_MIN_PIXELS}（裁剪/掩膜/非正剔除后）',
                            reason='too_few_pixels')

    x = lam[idx]
    f = flx[idx]
    t = np.interp(x, cl, ct)          # F-46：唯一插值方向（T → 谱原生网格）
    dx = np.empty(n_used)             # 梯形单元宽（掩膜断口自然桥接）
    dx[0], dx[-1] = (x[1] - x[0]) / 2.0, (x[-1] - x[-2]) / 2.0
    if n_used > 2:
        dx[1:-1] = (x[2:] - x[:-2]) / 2.0

    if weighting == 'photon':         # 式 (1)：测度 T·dλ/λ
        kern = t / x
        fnu = float(np.sum(f * t * x * dx)) / (C_AA_PER_S * float(np.sum(kern * dx)))
        flam = float(np.sum(f * kern * dx)) / float(np.sum(kern * dx))
    else:                             # 式 (2)：测度 T·dλ/λ²
        kern = t / (x * x)
        fnu = float(np.sum(f * t * dx)) / (C_AA_PER_S * float(np.sum(kern * dx)))
        flam = float(np.sum(f * kern * dx)) / float(np.sum(kern * dx))

    dfnu_df = (t * x * dx if weighting == 'photon' else t * dx)
    dfnu_df = dfnu_df / (C_AA_PER_S * float(np.sum(kern * dx)))
    lam_iso = math.sqrt(C_AA_PER_S * fnu / flam)  # F-47：等流量波长（随谱变）

    # F-18/F-53：曲线侧代表波长在有效通带（裁剪+掩膜后的同一被积区间）上重算
    lam_pivot = math.sqrt(float(np.sum(x * t * dx)) / float(np.sum(t / x * dx)))
    lam_phot = float(np.sum(t * x * x * dx)) / float(np.sum(t * x * dx))

    return {'fnu_cgs': fnu, 'flambda_cgs': flam, 'band_mode': 'integrated',
            'weighting': weighting, 'lambda_pivot_aa': lam_pivot,
            'lambda_phot_aa': lam_phot, 'lambda_iso_aa': lam_iso,
            'lambda_eff_aa': None,      # F-19：P1 无 Vega 参考谱 ⇒ null（CA-15 由装配层挂）
            'sigma2_lnl': sigma2_lnl(cl, ct), 'overlap': overlap,
            'n_used_pixels': n_used, 'n_masked_pixels': n_masked,
            'n_nonpos': n_nonpos,
            'nonpos_frac': n_nonpos / n_band if n_band else 0.0,
            'weights_idx': idx, 'weights_val': dfnu_df,
            'band_lo_aa': float(x[0]), 'band_hi_aa': float(x[-1]),
            'warnings': warnings}


def st_band_average(lam_aa, flux_flambda, curve_lam, curve_tr):
    """式 (4') 的 ⟨Fλ⟩_ST = ∫Fλ·T·λ dλ ÷ ∫T·λ dλ（第三种加权，仅 T-52 自检用）。"""
    x = np.asarray(lam_aa, dtype=float)
    f = np.asarray(flux_flambda, dtype=float)
    t = np.interp(x, np.asarray(curve_lam, dtype=float),
                  np.asarray(curve_tr, dtype=float))
    return float(np.trapezoid(f * t * x, x)) / float(np.trapezoid(t * x, x))
