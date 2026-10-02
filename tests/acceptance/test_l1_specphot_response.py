"""L1: specphot response.py（S1-a 波段平均 + S1-b 零点单位 + S1-d 曲线口径）。

覆盖条款：
  T-1   F_ν=const ⇒ 式(1)=式(2)（F-2 边界）
  T-2   F_λ=const × top-hat [1,2] ⇒ 比值 1.08202、差 0.0856 mag（F-3 反例）
  T-3   AB 零点记账口径（m_AB(3631 Jy) = −6.5622e-5，禁写 ==0）+ f_mjy 往返
  T-4   曲线整体乘常数 ⇒ 所有输出不变（F-1）
  T-5   窄带极限（Δλ/λ₀=1e-4 top-hat @5500 Å，F_ν∝ν⁻¹）
  T-27  精确式 vs 数值积分 <1e-9；二阶近似偏差单调增、r=2 时 <0.13%（F-2/F-3）
  T-30  式(3)⇔式(4) 交叉核对 1e-12；pivot 代入 (4) 必不等（F-47）
  T-52  ST 合并常数逐项相等；误用 C_AB_LAM_ZERO 偏差恰为 §7.1 钉值（F-8）
  T-12  掩膜覆盖整个通带 ⇒ E-04，无 −∞ 星等
  T-13  非正流量像素局部剔除（V-6），残余 < C_MIN_PIXELS ⇒ E-04
  F-46  曲线节点口径（FWHM 端点线性插值、扣峰值点、px_per_fwhm）
  F-16  无曲线 ⇒ E-04(curve_missing) / 显式授权走 mono
  F-53  overlap 闸（<0.5 ⇒ E-04 no_overlap；[0.5,0.95) ⇒ CA-05 且出结果）

全部纯函数、脱库可跑（禁 db_cur）；golden 一律内联。
"""
import math

import numpy as np
import pytest

from specphot import response as R
from specphot.constants import (C_AA_PER_S, C_AB_ZERO, C_AB_LAM_ZERO,
                                C_ST_ZERO, C_ST_LAM_CONST, C_MIN_PIXELS)
from specphot.reader import SpecLoadError

C_CGS = C_AA_PER_S  # 2.99792458e18 Å/s


def _tophat(lo, hi, n=20001):
    lam = np.linspace(lo, hi, n)
    return lam, np.ones(n)


def _flat_flambda_spec(lo, hi, n=20001, value=1e-16):
    lam = np.linspace(lo, hi, n)
    return lam, np.full(n, value)


# ─── T-1：F_ν=const ⇒ 两式相等（相对差 < 1e-12） ────────────────────────

def test_t1_constant_fnu_photon_equals_energy():
    cl, ct = _tophat(4000.0, 7000.0)
    lam = np.linspace(4000.0, 7000.0, 40001)
    fl = C_CGS * 1e-15 / lam**2          # F_ν = 1e-15 ⇒ F_λ = c·F_ν/λ²
    rp = R.band_integrals(lam, fl, cl, ct, weighting='photon')
    re_ = R.band_integrals(lam, fl, cl, ct, weighting='energy')
    assert abs(rp['fnu_cgs'] / re_['fnu_cgs'] - 1.0) < 1e-12
    assert rp['fnu_cgs'] == pytest.approx(1e-15, rel=1e-9)


# ─── T-2 / T-27：F_λ=const × top-hat 的精确比值与二阶近似 ────────────────

def _ratio_on_tophat(r, n=200001):
    """F_λ=const × top-hat [1,r]：光子/能量两加权的 ⟨Fν⟩ 比值（数值积分）。"""
    cl, ct = _tophat(1.0, r, n)
    lam = np.linspace(1.0, r, n)
    fl = np.ones(n)
    rp = R.band_integrals(lam, fl, cl, ct, weighting='photon')
    re_ = R.band_integrals(lam, fl, cl, ct, weighting='energy')
    return rp['fnu_cgs'] / re_['fnu_cgs']


def test_t2_flat_flambda_tophat_12_ratio_and_mag():
    ratio = _ratio_on_tophat(2.0)
    assert ratio == pytest.approx(1.08202, rel=1e-5)
    assert 2.5 * math.log10(ratio) == pytest.approx(0.0856, abs=1e-4)


@pytest.mark.parametrize('r', [1.2, 1.5, 2.0, 3.0])
def test_t27_exact_formula_matches_numeric_integration(r):
    exact = (r * r - 1.0) / (2.0 * r * math.log(r))
    assert _ratio_on_tophat(r) == pytest.approx(exact, rel=1e-9)


def test_t27_second_order_approx_deviation_monotonic():
    """exp(α·σ²_lnl)（α=2）对精确式的偏差随 r 单调增大；r=2 时 < 0.13%（不等式形式）"""
    devs = []
    for r in (1.2, 1.5, 2.0, 3.0):
        exact = (r * r - 1.0) / (2.0 * r * math.log(r))
        sigma2 = (math.log(r))**2 / 12.0     # top-hat 的 σ²_lnl 闭式
        devs.append(math.exp(2.0 * sigma2) / exact - 1.0)
    assert all(d2 > d1 for d1, d2 in zip(devs, devs[1:]))
    assert devs[2] < 0.0013                  # r=2：0.12454% ⇒ < 0.13%


def test_t27_sigma2_lnl_tophat_closed_form():
    """σ²_lnl 必须按 Var_p(ln λ)（测度 T·dλ/λ）定义：top-hat [1,r] ⇒ (ln r)²/12"""
    cl, ct = _tophat(1.0, 2.0, 200001)
    assert R.sigma2_lnl(cl, ct) == pytest.approx((math.log(2.0))**2 / 12.0,
                                                 rel=1e-6)


# ─── T-3：AB 零点与 f_mjy 往返 ─────────────────────────────────────────

def test_t3_ab_zero_point_bookkeeping_value():
    """按 C_AB_ZERO=48.60 计算 m_AB(3631 Jy) = −6.5622e-5（记账口径，禁止 == 0）"""
    m = R.ab_mag_from_fnu(3631e-23)
    assert m == pytest.approx(-6.5622e-5, abs=1e-9)
    assert m != 0.0


def test_t3_f_mjy_round_trip():
    for m in (15.0, 20.0, 25.7):
        f = R.ab_mag_to_f_mjy(m)
        assert R.f_mjy_to_ab_mag(f) == pytest.approx(m, abs=1e-9)
    # 零点 16.4：m=16.4 ⇒ 1 mJy（与宿主 bands.js / extinction.py 同值，F-7）
    assert R.ab_mag_to_f_mjy(16.4) == pytest.approx(1.0, rel=1e-12)


# ─── T-4：曲线整体乘常数 ⇒ 输出不变（F-1） ──────────────────────────────

def test_t4_curve_constant_factor_invariance():
    cl, ct = _tophat(5000.0, 6000.0)
    lam = np.linspace(4500.0, 6500.0, 20001)
    fl = 1e-16 * (lam / 5500.0)**0.7     # 非平谱，防退化
    a = R.band_integrals(lam, fl, cl, ct, weighting='photon')
    b = R.band_integrals(lam, fl, cl, 7.3 * ct, weighting='photon')
    assert (R.ab_mag_from_fnu(a['fnu_cgs'])
            == pytest.approx(R.ab_mag_from_fnu(b['fnu_cgs']), abs=1e-12))
    for k in ('fnu_cgs', 'flambda_cgs', 'lambda_pivot_aa', 'lambda_phot_aa',
              'lambda_iso_aa', 'overlap'):
        assert a[k] == pytest.approx(b[k], rel=1e-12)


# ─── T-5：窄带极限 ─────────────────────────────────────────────────────

def test_t5_narrowband_limit_tophat_5500():
    """Δλ/λ₀ = 1e-4 top-hat @5500 Å，F_ν ∝ ν⁻¹ ⇒ 与 F_ν(λ₀) 相对差 < 1e-6"""
    lam0 = 5500.0
    half = lam0 * 1e-4 / 2.0
    cl, ct = _tophat(lam0 - half, lam0 + half, 2001)
    lam = np.linspace(lam0 - 5 * half, lam0 + 5 * half, 20001)
    fl = 1.0 / lam                       # F_ν ∝ ν⁻¹ ⇒ F_λ = F_ν·c/λ² ∝ 1/λ
    r = R.band_integrals(lam, fl, cl, ct, weighting='photon')
    fnu0 = lam0 / C_CGS                  # F_ν(λ₀) = A·λ₀/c，A=1
    assert abs(r['fnu_cgs'] / fnu0 - 1.0) < 1e-6


# ─── T-30：式(3)⇔式(4)；λ_iso 与 pivot/eff 可区分 ──────────────────────

def test_t30_eq3_eq4_cross_check_and_lambda_iso_distinct():
    cl, ct = _tophat(5000.0, 7000.0)
    lam = np.linspace(4500.0, 7500.0, 60001)
    fl = 1e-16 * (lam / 6000.0)**1.3     # 非平谱：iso ≠ pivot
    for w in ('photon', 'energy'):
        r = R.band_integrals(lam, fl, cl, ct, weighting=w)
        m3 = R.ab_mag_from_fnu(r['fnu_cgs'])
        m4 = R.ab_mag_from_flambda(r['flambda_cgs'], r['lambda_iso_aa'])
        assert abs(m3 - m4) < 1e-12                       # 恒等改写（F-47）
        # λ_iso = sqrt(c⟨Fν⟩/⟨Fλ⟩) 由两个波段平均互推（同测度配对）
        assert r['lambda_iso_aa'] == pytest.approx(
            math.sqrt(C_CGS * r['fnu_cgs'] / r['flambda_cgs']), rel=1e-12)
        # 拿 pivot 代入 (4) 必不等（三者可区分；本谱下差 ~1e-3 mag 量级）
        m4_wrong = R.ab_mag_from_flambda(r['flambda_cgs'], r['lambda_pivot_aa'])
        assert abs(m4_wrong - m3) > 1e-5


# ─── T-52：ST 合并常数自检（P1 API 拒 ST，公式层仍须自洽） ───────────────

def test_t52_st_merged_constant_consistency_and_mislabel_bias():
    cl, ct = _tophat(5000.0, 7000.0)
    lam = np.linspace(4500.0, 7500.0, 60001)
    fl = 1e-16 * (lam / 6000.0)**0.4
    fst = R.st_band_average(lam, fl, cl, ct)   # ⟨Fλ⟩_ST = ∫FλTλdλ/∫Tλdλ（第三种加权）
    lam_ref = 6000.0
    m_merged = R.st_mag_from_flambda(fst, lam_ref)
    m_split = (-2.5 * math.log10(fst)
               - 5.0 * math.log10(lam_ref / 5556.0) - C_ST_ZERO)
    assert m_merged == pytest.approx(m_split, rel=1e-9)
    # 把 C_AB_LAM_ZERO 误当 ST 常数 ⇒ 偏移恰为 §7.1 钉值（C_AB_LAM_ZERO−C_ST_LAM_CONST）
    m_wrong = (-2.5 * math.log10(fst) - 5.0 * math.log10(lam_ref) - C_AB_LAM_ZERO)
    bias = C_AB_LAM_ZERO - C_ST_LAM_CONST
    assert abs((m_merged - m_wrong) - bias) < 1e-12
    assert bias == pytest.approx(0.03175943, abs=1e-8)   # 显示 0.0318


# ─── T-12/T-13：掩膜与非正像素 ─────────────────────────────────────────

def test_t12_mask_covering_whole_band_raises_e04_no_neg_inf_mag():
    cl, ct = _tophat(5000.0, 6000.0)
    lam, fl = _flat_flambda_spec(4500.0, 6500.0)
    with pytest.raises(SpecLoadError) as ei:
        R.band_integrals(lam, fl, cl, ct, mask_ranges=[[4500.0, 6500.0]])
    assert ei.value.code == 'E-04' and ei.value.reason == 'too_few_pixels'


def test_t13_nonpos_pixels_excluded_locally():
    cl, ct = _tophat(5000.0, 6000.0)
    lam, fl = _flat_flambda_spec(4500.0, 6500.0, n=2001)
    fl[800:810] = -1e-16                 # 通带内 10 个非正像素
    r = R.band_integrals(lam, fl, cl, ct)
    assert r['n_nonpos'] == 10
    assert any(w['code'] == 'CA-10' for w in r['warnings'])
    assert r['fnu_cgs'] > 0.0            # 局部剔除后正常出数（不整谱作废）
    # 剔除到残余 < C_MIN_PIXELS ⇒ E-04，且不得输出 −∞ 星等
    fl2 = np.full(2001, 1e-16)
    fl2[:] = -1e-16
    fl2[900:905] = 1e-16                 # 通带内只剩 5 个正像素 < 8
    with pytest.raises(SpecLoadError) as ei:
        R.band_integrals(lam, fl2, cl, ct)
    assert ei.value.code == 'E-04' and ei.value.reason == 'too_few_pixels'
    assert C_MIN_PIXELS == 8


# ─── F-53：overlap 闸 ─────────────────────────────────────────────────

def test_f53_overlap_reject_below_half():
    cl, ct = _tophat(5000.0, 6000.0)
    lam, fl = _flat_flambda_spec(5800.0, 7000.0)   # 只覆盖通带尾段 ~20%
    with pytest.raises(SpecLoadError) as ei:
        R.band_integrals(lam, fl, cl, ct)
    assert ei.value.code == 'E-04' and ei.value.reason == 'no_overlap'


def test_f53_partial_overlap_keeps_result_with_ca05():
    cl, ct = _tophat(5000.0, 6000.0)
    lam, fl = _flat_flambda_spec(5100.0, 7000.0)   # 覆盖 ~90% ⇒ CA-05 但出结果
    r = R.band_integrals(lam, fl, cl, ct)
    assert 0.5 <= r['overlap'] < 0.95
    assert any(w['code'] == 'CA-05' for w in r['warnings'])
    assert r['band_lo_aa'] >= 5100.0 and r['fnu_cgs'] > 0.0


# ─── F-46：曲线节点口径 ────────────────────────────────────────────────

def test_f46_curve_metrics_triangle_curve():
    """三角曲线：底 [5000,6000]、峰 5500 ⇒ FWHM=500（端点线性插值）；
    网格 10 Å ⇒ 半高宽区间内采样点 51 个、扣峰值点 ⇒ 50；px_per_fwhm = 500/10"""
    cl = np.arange(5000.0, 6001.0, 10.0)
    ct = np.maximum(0.0, 1.0 - np.abs(cl - 5500.0) / 500.0)
    m = R.curve_metrics(cl, ct)
    assert m['fwhm_aa'] == pytest.approx(500.0, rel=1e-9)
    assert m['curve_nodes'] == 50
    assert m['curve_px_per_fwhm'] == pytest.approx(50.0, rel=1e-9)


def test_f46_sparse_curve_px_per_fwhm_low():
    """V 带先例的缩影：半高宽内只有 3 个节点（扣峰）⇒ px_per_fwhm = FWHM/节点间距"""
    cl = np.array([5000.0, 5300.0, 5500.0, 5700.0, 6000.0])
    ct = np.array([0.0, 0.5, 1.0, 0.5, 0.0])
    m = R.curve_metrics(cl, ct)
    assert m['curve_nodes'] == 2           # 半高宽内采样点 {5300,5500,5700} 扣峰 ⇒ 2
    assert m['curve_px_per_fwhm'] < 8      # ⇒ CA-01（装配层按 C_CURVE_PX_PER_FWHM_MIN 判）


# ─── F-16：无曲线波段 ─────────────────────────────────────────────────

def test_f16_missing_curve_requires_explicit_mono():
    lam, fl = _flat_flambda_spec(4500.0, 6500.0)
    with pytest.raises(SpecLoadError) as ei:
        R.band_integrals(lam, fl, None, None)
    assert ei.value.code == 'E-04' and ei.value.reason == 'curve_missing'
    r = R.band_integrals(lam, fl, None, None, allow_mono=True,
                         mono_lam_ref=5500.0)
    assert r['band_mode'] == 'mono'
    assert r['fnu_cgs'] == pytest.approx(1e-16 * 5500.0**2 / C_CGS, rel=1e-12)


# ─── 传播权重（errors.py 的接口契约） ───────────────────────────────────

def test_weights_reconstruct_fnu_linearly():
    """∂⟨Fν⟩/∂F_p 稀疏权重：对同一条谱重建 ⟨Fν⟩ = Σ w_p F_p（线性性自检）"""
    cl, ct = _tophat(5000.0, 6000.0)
    lam = np.linspace(4500.0, 6500.0, 4001)
    fl = 1e-16 * (lam / 5500.0)**0.9
    r = R.band_integrals(lam, fl, cl, ct, weighting='photon')
    recon = float(np.sum(r['weights_val'] * fl[r['weights_idx']]))
    assert recon == pytest.approx(r['fnu_cgs'], rel=1e-12)
    assert len(r['weights_idx']) == r['n_used_pixels']
