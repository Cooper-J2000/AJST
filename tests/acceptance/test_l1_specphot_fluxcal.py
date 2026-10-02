"""L1: specphot fluxcal.py（S1-c 定标模式 + anchored 闭式解 + AnchorSet）。

覆盖条款：
  T-6   anchored 蒙特卡洛回收（1000 次，seed=20260927）：中位数/σ_κ/覆盖率
  T-7   单波段 ⇒ chi2_anchor=null + CA-14；两波段完全一致 ⇒ χ² 按浮点判零
  T-28  κ* 方向（f×10 ⇒ κ*/10，χ²/mag 不变）+ 加权内积正交性
  T-29  重叠通带 ⇒ 波段协方差非对角；对角近似的 σ_κ 系统性偏小（F-50）
  T-33  双键判定四组合 + 缺键 + 矛盾 + 表外值（F-13）
  T-37  量级闸门：第一道 median(|F|)（V-17）+ 第二道闭序选带规则唯一（F-14）
  T-42  AnchorSet：同波段冲突两行 ⇒ E-14 且列出；取消勾选不进 n_bands_used
  F-9/F-48  Vega 三态 / ST 在 P1 一律 E-08

全部纯函数、脱库可跑（禁 db_cur）；golden 一律内联。
"""
import numpy as np
import pytest

from specphot import fluxcal as FC
from specphot import response as R
from specphot import errors as ER
from specphot.reader import SpecLoadError


def _fit(f, g, sig):
    return FC.anchored_fit(np.asarray(f, float), np.asarray(g, float),
                           np.diag(np.asarray(sig, float)**2))


# ─── T-6：anchored 蒙特卡洛（seed 见 §10.1 元判据：20260927） ────────────

def test_t6_anchored_monte_carlo_recovery():
    rng = np.random.default_rng(20260927)
    f = np.array([1.0, 2.0, 3.0, 4.0]) * 1e-15
    s_true = 2.5
    sig = 0.03 * s_true * f                     # 已知 σ（C 固定 ⇒ σ_κ 逐次相同）
    C = np.diag(sig**2)
    kappas, sigmas = [], []
    for _ in range(1000):
        g = s_true * f + rng.normal(0.0, sig)
        res = FC.anchored_fit(f, g, C)
        kappas.append(res['value'])
        sigmas.append(res['sigma'])
    kappas = np.asarray(kappas)
    sigma_k = sigmas[0]
    assert all(s == pytest.approx(sigma_k, rel=1e-12) for s in sigmas[::97])
    assert abs(float(np.median(kappas)) / s_true - 1.0) < 0.01
    assert abs(sigma_k / float(np.std(kappas)) - 1.0) < 0.05
    cover = float(np.mean(np.abs(kappas - s_true) < sigma_k))
    assert 0.62 <= cover <= 0.74


# ─── T-7：单波段退化与两波段完全一致 ─────────────────────────────────────

def test_t7_single_band_chi2_null_ca14():
    res = _fit([1e-15], [2.5e-15], [0.03 * 2.5e-15])
    assert res['chi2'] is None and res['dof'] == 0
    assert any(w['code'] == 'CA-14' for w in res['warnings'])
    assert res['n_bands_used'] == 1


def test_t7_two_bands_exactly_consistent_chi2_float_zero():
    f = [1e-15, 2e-15]
    g = [2.5e-15, 5e-15]
    sig = [0.03 * 2.5e-15, 0.03 * 5e-15]
    res = _fit(f, g, sig)
    assert abs(res['chi2']) < 1e-12 * max(1, res['dof'])
    assert res['dof'] == 1


# ─── T-28：κ* 方向与正交性 ──────────────────────────────────────────────

def test_t28_kappa_direction_and_orthogonality():
    f = np.array([1.0, 2.0, 3.0])
    sig = 0.03 * 2.5 * f
    g = 2.5 * f + np.array([0.010, -0.020, 0.015])
    C = np.diag(sig**2)
    r1 = FC.anchored_fit(f, g, C)
    r2 = FC.anchored_fit(10.0 * f, g, C)
    assert r2['value'] == pytest.approx(r1['value'] / 10.0, rel=1e-12)
    assert r2['chi2'] == pytest.approx(r1['chi2'], rel=1e-12)
    # mag_err_cal 与 m_syn(anchored) 不变（绝对差 < 1e-12 mag）；
    # f×10 ⇒ m_syn 自身也 −2.5 mag，与 κ*/10 的 +2.5 抵消（方向写反则此项立即红）
    m_syn = 20.0
    m1 = FC.anchored_mag(m_syn, r1['value'])
    m2 = FC.anchored_mag(m_syn - 2.5, r2['value'])
    assert abs(m1 - m2) < 1e-12
    e1 = ER.mag_err_cal_nonparticipant(r1['sigma'], r1['value'])
    e2 = ER.mag_err_cal_nonparticipant(r2['sigma'], r2['value'])
    assert abs(e1 - e2) < 1e-12
    # 加权内积正交性：fᵀC⁻¹(g − κ*f) = 0（写反 f/g 的实现过不了这条）
    resid = g - r1['value'] * f
    assert abs(float(f @ np.linalg.solve(C, resid))) < 1e-10


# ─── T-29：波段协方差非对角（F-50） ─────────────────────────────────────

def test_t29_overlapping_passbands_cov_not_diagonal():
    lam = np.linspace(4500.0, 7000.0, 25001)
    fl = np.full(lam.size, 1e-15)
    cl1 = np.linspace(5000.0, 6000.0, 2001)
    cl2 = np.linspace(5500.0, 6500.0, 2001)
    r1 = R.band_integrals(lam, fl, cl1, np.ones(2001), weighting='photon')
    r2 = R.band_integrals(lam, fl, cl2, np.ones(2001), weighting='photon')
    sigma_px = 1e-17
    C_syn = ER.cov_syn([(r1['weights_idx'], r1['weights_val']),
                        (r2['weights_idx'], r2['weights_val'])], sigma_px)
    assert C_syn[0, 1] != 0.0
    assert C_syn[0, 1] > 0.3 * np.sqrt(C_syn[0, 0] * C_syn[1, 1])  # 强正相关
    f = [r1['fnu_cgs'], r2['fnu_cgs']]
    g = [1.02 * f[0], 0.98 * f[1]]
    full = FC.anchored_fit(f, g, C_syn)
    diag = FC.anchored_fit(f, g, np.diag(np.diag(C_syn)))
    assert diag['sigma'] < full['sigma']          # 对角近似系统性偏小
    assert full['cov_method'] == 'chol' and full['n_bands_used'] == 2


def test_f50_pinv_fallback_on_ill_conditioned():
    """cond(C) > C_COND_MAX ⇒ cov_method='pinv'（F-50；值域 {chol, pinv} 不得互写）"""
    eps = 1e-12
    C = np.array([[1.0, 1.0 - eps], [1.0 - eps, 1.0]]) * 1e-30
    res = FC.anchored_fit([1e-15, 1.1e-15], [2.5e-15, 2.6e-15], C)
    assert res['cov_method'] == 'pinv'
    assert np.linalg.cond(C) > 1e8


# ─── T-33：双键判定（F-13） ─────────────────────────────────────────────

@pytest.mark.parametrize('ft,uf,kind', [
    ('absolute', 'erg/s/cm^2/Angstrom', 'absolute'),
    ('Absolute', 'ERG/S/CM^2/ANGSTROM', 'absolute'),     # 类型规范化（V-3a）
    ('absolute', 'Uncalibrated', 'contradictory'),
    (None, 'Uncalibrated', 'uncalibrated'),
    (None, 'erg/s/cm^2/Angstrom', 'unknown'),            # catalog 单键不足为凭
    ('absolute', None, 'unknown'),
    (None, None, 'unknown'),
    ('absolute', 'erg/s/cm2/A', 'contradictory'),        # 表外同义写法：不猜
    ('normalized', 'erg/s/cm^2/Angstrom', 'contradictory'),
])
def test_t33_uncal_kind_matrix(ft, uf, kind):
    out = FC.classify_flux_calibration(ft, uf, source='catalog')
    assert out['uncal_kind'] == kind
    if kind == 'contradictory':
        assert any(w['code'] == 'CA-03' for w in out['warnings'])
        assert out['raw']['u_fluxes'] == uf              # 回显原值


def test_t33_upload_path_single_key_can_be_absolute():
    """上传件：flux_unit 头键充当 u_fluxes、flux_type 视为缺失 ⇒ 可判 absolute（F-77）"""
    out = FC.classify_flux_calibration(None, 'erg/s/cm^2/Angstrom', source='upload')
    assert out['uncal_kind'] == 'absolute'
    out2 = FC.classify_flux_calibration(None, None, source='upload')
    assert out2['uncal_kind'] == 'unknown'


# ─── T-37：量级闸门与闭序选带（F-14/V-17） ───────────────────────────────

def test_t37_first_gate_and_direct_rejection():
    # 库内实测分布回归基线：81 条低簇（≤1e-13）/ 32 条高簇（1e-3…4e2），中间空 ~10 个量级
    assert FC.first_gate_suspect(1e-11) is True          # 高簇
    assert FC.first_gate_suspect(1e-14) is False         # 低簇
    assert FC.first_gate_suspect(None) is False
    with pytest.raises(SpecLoadError) as ei:
        FC.resolve_mode('direct', 'absolute', flux_scale_suspect=True)
    assert ei.value.code == 'E-03' and ei.value.reason == 'flux_scale_suspect'
    with pytest.raises(SpecLoadError) as ei2:
        FC.resolve_mode('direct', 'uncalibrated')
    assert ei2.value.code == 'E-03' and ei2.value.reason == 'label_not_absolute'
    # 同一谱走 anchored 可用（不静默降级，是用户改选）
    out = FC.resolve_mode('anchored', 'uncalibrated', anchors_available=2)
    assert out['mode_effective'] == 'anchored'


def test_t37_second_gate_band_selection_closed_order():
    more = {'band': 'g', 'n_pairable': 3, 'lambda_pivot_aa': 4770.0}
    bluer = {'band': 'u', 'n_pairable': 1, 'lambda_pivot_aa': 3560.0}
    assert FC.pick_mag_sanity_band([bluer, more])['band'] == 'g'   # 点数多者胜
    tie_blue = {'band': 'u', 'n_pairable': 3, 'lambda_pivot_aa': 3560.0}
    assert FC.pick_mag_sanity_band([more, tie_blue])['band'] == 'u'  # 并列取 λ 最小
    assert FC.pick_mag_sanity_band([]) is None


def test_mode_auto_order_and_anchor_unavailable():
    assert FC.resolve_mode('auto', 'absolute')['mode_effective'] == 'direct'
    out = FC.resolve_mode('auto', 'uncalibrated', anchors_available=1)
    assert out['mode_effective'] == 'anchored'
    ok = FC.resolve_mode('direct', 'absolute', flux_scale_suspect=False)
    assert ok['mode_effective'] == 'direct'
    with pytest.raises(SpecLoadError) as ei:
        FC.resolve_mode('anchored', 'uncalibrated', anchors_available=0)
    assert ei.value.code == 'E-10'
    with pytest.raises(SpecLoadError) as ei3:
        FC.resolve_mode('bogus', 'absolute')
    assert ei3.value.code == 'E-14' and ei3.value.reason == 'bad_enum'


# ─── T-42：AnchorSet（F-78） ─────────────────────────────────────────────

def _row(band, origin, used=True):
    return {'band': band, 'mag': 20.0, 'mag_err': 0.05, 'anchor_origin': origin,
            'used': used}


def test_t42_band_conflict_lists_both_rows():
    rows = [_row('r', 'catalog'), _row('r', 'manual')]
    with pytest.raises(SpecLoadError) as ei:
        FC.AnchorSet(rows)
    assert ei.value.code == 'E-14' and ei.value.reason == 'anchor_set_invalid'
    d = ei.value.details
    assert d['band_conflict'] is True and len(d['conflict_rows']) == 2
    assert {r['anchor_origin'] for r in d['conflict_rows']} == {'catalog', 'manual'}


def test_t42_deselected_band_not_counted_and_single_band_ok():
    rows = [_row('r', 'catalog'), _row('g', 'manual', used=False)]
    aset = FC.AnchorSet(rows)
    assert aset.used_bands() == ['r']                    # 取消勾选不进 n_bands_used
    res = _fit([1e-15], [2.5e-15], [0.03 * 2.5e-15])     # 单勾选波段 ⇒ CA-14 而非配对失败
    assert any(w['code'] == 'CA-14' for w in res['warnings'])


def test_anchor_set_row_cap_and_required_keys():
    with pytest.raises(SpecLoadError):
        FC.AnchorSet([_row(f'b{i}', 'manual') for i in range(25)])   # > C_MAX_ANCHORS
    with pytest.raises(SpecLoadError):
        FC.AnchorSet([{'band': 'r', 'mag': 20.0}])                   # 缺 anchor_origin


# ─── F-9/F-48：星等制三态 ────────────────────────────────────────────────

def test_f9_vega_three_states():
    fnu, warns = FC.anchor_mag_to_fnu_cgs(20.0, 'AB')
    assert R.ab_mag_from_fnu(fnu) == pytest.approx(20.0, abs=1e-12)
    with pytest.raises(SpecLoadError) as ei:
        FC.anchor_mag_to_fnu_cgs(20.0, 'Vega', vega2ab=None)
    assert ei.value.code == 'E-08' and ei.value.reason == 'vega_unavailable'
    fnu0, warns0 = FC.anchor_mag_to_fnu_cgs(20.0, 'Vega', vega2ab=0.0)
    assert any(w['code'] == 'CA-01' for w in warns0)     # 0.0 可能是缺省值，仍算
    assert fnu0 == pytest.approx(fnu, rel=1e-12)
    fnu2, _ = FC.anchor_mag_to_fnu_cgs(20.0, 'Vega', vega2ab=1.86)
    assert R.ab_mag_from_fnu(fnu2) == pytest.approx(21.86, abs=1e-12)


def test_f48_st_rejected_in_p1():
    with pytest.raises(SpecLoadError) as ei:
        FC.anchor_mag_to_fnu_cgs(20.0, 'ST')
    assert ei.value.code == 'E-08' and ei.value.reason == 'st_unavailable'
    with pytest.raises(SpecLoadError) as ei2:
        FC.anchor_mag_to_fnu_cgs(20.0, 'X')
    assert ei2.value.code == 'E-14' and ei2.value.reason == 'bad_enum'
