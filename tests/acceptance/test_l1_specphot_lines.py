"""L1: specphot lines.py（S3 谱线测量纯函数层，02 §3.9 + §3.9.3，P3 切片 1）。

覆盖条款：
  T-19  gauss1 合成高斯：中心/宽度/流量回收；EW 闭式（解析 Gauss 核）与数值
        积分一致 < 1%；lorentz/gauss2 同法回收
  T-20  两类掩膜分别生效：线心落在 C_MASK_ABS_TABLE / C_MASK_EMIS_TABLE ⇒ 拒；
        窗内命中段措辞「因大气吸收排除」/「因天光发射排除」不混用
  T-21  空气/真空（一律调宿主 wavconvert）：6300.21(vac)⇒6298.468(air)、
        0.9247@3200 / 2.1998@8000、往返 < 1e-6 Å、<2000 Å 原样返回；
        线心表对 NIST ASD 的逐条核对属 M-6 ⇒ skip（理由见测试体）
  T-65  EW 项分解与两法对照：ew_err_form='two_term'；三键齐全；continuum 项
        随 (Δλ−W) 单调增（窗加倍 > 1.4×）；缺基线项且无自助 ⇒ E-15；
        基线系数进同一 θ（注入侧带安置协方差反向核查 CA-44⑤ 可达）；
        闭式 vs 自助 > 2× ⇒ CA-44⑥ 两个都列
  T-66-lite  同输入+同 seed 逐字节可复现；换 seed 自助列变而闭式列不变
  CA-44 各线侧档：①收敛 ④snr 门 ⑤coherent>3× ⑥两法差（②触界/秩亏经
        _cov_from_jac 复用已由 continuum 侧测试覆盖，此处验 EW 回落路径）
  F-67  吸收线 line_flux 恒 null（误差键同 null）、发射线 depth 恒 null
  F-68  snr_res 门：不拟合 + 3σ 上限 + CA-44④ + 误差键 null
  F-71  生长曲线：线性域 N 回收与 σ_N/N=σ_W/W；饱和 ⇒ 下限 + CA-27；
        系数 1.1296e17 由 scipy.constants 就地复算钉值（F-71「不抄二手数值」）；
        EW=0 线不进柱密度族（除零守卫）
  P0    _sideband_idx 方向回归：侧带严格在窗外；粗采样（Δλ≈4 Å）+宽线
        （gauss FWHM=30 / lorentz FWHM=40）EW 回收；E-04 侧带守门真触发
  P1-3  F-41/F-55 相关放大：AR(1) 谱（ρ≈0.5）闭式 photon/总项 ×√((1+ρ)/(1−ρ))
  P1-4  未检出上限 = 3·√(photon²+continuum²)（§3.9.3.1 总项）
  P1-5  F-62③：cond_2 超限自动降阶 + CA-20；降到 0 阶仍超 ⇒ E-14
  P2    CA-44 reason 签（closed_fallback_bootstrap / depth_err_unavailable）、
        fwhm/depth delta_method 非有限守卫、CA-27∈误差族源码钉

元判据（§10.1）：随机化判据 seed = 20260927；纯函数、脱库可跑、只读。
几何纪律：合成谱波长轴避开 C_MASK_ABS_TABLE / C_MASK_EMIS_TABLE（除掩膜用
例本身）；线窗与侧带不得撞天光/大气掩膜，否则 select_window 的 F-72③ 会
（正确地）拒绝。
"""
import json
import math

import numpy as np
import pytest

from specphot import lines as LN
from specphot.constants import (C_LINE_SNR_MIN, C_LINE_WIN, C_MAX_COMPONENTS)

SEED = 20260927
ENGINE_KEYS = {'scipy_version', 'x_scale', 'diff_step', 'nfev', 'njev',
               'optimality', 'status', 'active_mask', 'n_starts'}   # F-91⑦ 九键


def _axis(lo=6380.0, hi=6620.0, n=900):
    return np.linspace(lo, hi, n)


def _gauss_spec(lam, center=6500.0, c0=100.0, A=20.0, s_lnl=8e-4, noise=1.0,
                seed=SEED, kind='emission', depth=0.4):
    rng = np.random.default_rng(seed)
    u = np.log(lam)
    shape = np.exp(-0.5 * ((u - math.log(center)) / s_lnl) ** 2)
    if kind == 'emission':
        clean = c0 + A * shape
    else:
        clean = c0 * (1.0 - depth * shape)
    sig = np.full(lam.size, noise)
    return clean + rng.normal(0.0, noise, lam.size), sig


# ─── ① T-19：三线型回收 + EW 闭式 vs 数值积分 ─────────────────────────────

def test_t19_gauss1_recovery_and_ew_closed_vs_numeric():
    lam = _axis()
    flux, sig = _gauss_spec(lam)
    r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                        err_seed=SEED)
    assert abs(r['lambda_obs_vac_aa'] / 6500.0 - 1.0) < 0.001    # 中心回收
    fwhm_true = 6500.0 * 2.3548200700252868 * 8e-4               # 2.3548·c·σ_u 的 λ 空间形
    assert abs(r['fwhm_obs_aa'] / fwhm_true - 1.0) < 0.05        # 宽度回收 <5%
    # T-19：EW 闭式（解析 Gauss 核）与数值积分（逐像素模型积分）一致 <1%
    # （axis 用响应里回显的拟合区归一化轴，与 u0/w 同一坐标系）
    axis = r['baseline']['axis']
    u0 = float(r['params']['u00'])
    w = float(r['params']['w0'])
    A_fit = float(r['params']['A0'])
    analytic = A_fit * LN._ew_analytic_gauss(u0, w, axis)
    dl = LN._grad(lam)
    win = LN.select_window(lam, 6500.0, 'emission')
    widx = np.flatnonzero(LN._window_mask(lam, win))
    u_norm = LN._u_of(lam[widx], axis)
    numeric = float(np.sum(A_fit * LN._shape_gauss(u_norm, u0, w) * dl[widx]))
    assert abs(numeric / analytic - 1.0) < 0.01
    # EW/流量回收（数据式，噪声容差）
    ew_true = 20.0 * 6500.0 * math.sqrt(2.0 * math.pi) * 8e-4 / 100.0
    assert abs(r['ew_obs_aa'] / ew_true - 1.0) < 0.10
    lf_true = ew_true * 100.0
    assert abs(r['line_flux'] / lf_true - 1.0) < 0.10
    assert r['engine'] == 'least_squares.trf'
    assert ENGINE_KEYS <= set(r['engine_status'])                # F-91⑦ 九键
    assert r['ew_err_form'] == 'two_term'
    assert set(r['ew_err_terms']) == {'photon', 'continuum',
                                      'continuum_coherent'}      # T-65 三键齐全
    assert r['detected'] and r['depth'] is None                  # F-67 发射侧


def test_t19_lorentz_and_gauss2_recovery():
    lam = _axis()
    u = np.log(lam)
    rng = np.random.default_rng(SEED)
    sig = np.full(lam.size, 1.0)
    # lorentz：FWHM_lnλ = 2γ
    fl = 100.0 + 20.0 / (1.0 + ((u - math.log(6500.0)) / 4e-4) ** 2) \
        + rng.normal(0.0, 1.0, lam.size)
    r = LN.measure_line(lam, fl, sig, 6500.0, 'emission', profile='lorentz',
                        n_boot=0, err_seed=SEED)
    assert abs(r['lambda_obs_vac_aa'] / 6500.0 - 1.0) < 0.002
    assert abs(r['fwhm_obs_aa'] / (6500.0 * 2.0 * 4e-4) - 1.0) < 0.10
    # gauss2：双成分
    fl2 = 100.0 + 15.0 * np.exp(-0.5 * ((u - math.log(6470.0)) / 1e-4) ** 2) \
        + 10.0 * np.exp(-0.5 * ((u - math.log(6530.0)) / 1e-4) ** 2) \
        + rng.normal(0.0, 1.0, lam.size)
    r2 = LN.measure_line(lam, fl2, sig, 6500.0, 'emission', profile='gauss2',
                         n_boot=0, err_seed=SEED)
    assert r2['detected']
    centers = sorted((float(r2['params']['u00']), float(r2['params']['u01'])))
    lam_lo, lam_hi = (math.exp(_u_inv(cn, r2)) for cn in centers)
    assert abs(lam_lo / 6470.0 - 1.0) < 0.02 and abs(lam_hi / 6530.0 - 1.0) < 0.03


def _u_inv(u_norm, res):
    """归一 u → lnλ（测试侧就地反演 axis；axis 存于 baseline）。"""
    a = res['baseline']['axis'][0]
    b = res['baseline']['axis'][1]
    return a + (u_norm + 1.0) * b / 2.0


def test_t19_absorption_depth_recovery_f67_nulls():
    lam = _axis()
    flux, sig = _gauss_spec(lam, kind='absorption', depth=0.4)
    r = LN.measure_line(lam, flux, sig, 6500.0, 'absorption', n_boot=20,
                        err_seed=SEED)
    assert abs(r['depth'] / 0.4 - 1.0) < 0.10                    # 深度回收
    assert r['depth_err_lo'] is not None and r['depth_err_hi'] is not None
    assert r['line_flux'] is None and r['line_flux_err'] is None  # F-67 吸收侧
    assert r['err_source']['line_flux'] == 'none'
    assert r['ew_signed_aa'] < 0 and abs(r['ew_obs_aa'] - abs(r['ew_signed_aa'])) \
        < 1e-12                                                  # F-67 符号口径
    assert r['depth'] is not None                                # 吸收侧 depth 在场
    # 发射线侧镜像断言在 ①：depth is None
    ew_abs_true = 0.4 * 6500.0 * math.sqrt(2.0 * math.pi) * 8e-4   # ∫r dλ
    assert abs(r['ew_obs_aa'] / ew_abs_true - 1.0) < 0.15


# ─── ② T-20：双掩膜措辞不混用 + F-72③ 拒测 ───────────────────────────────

def test_t20_mask_wording_and_rejection():
    lam = _axis(6200.0, 6950.0, 1500)
    flux, sig = _gauss_spec(lam, center=6363.0, noise=1.0)
    # 天光发射措辞：窗（6363±60）与 [O I] 气辉段相交、线心在段外不拒
    win = LN.select_window(lam, 6363.0, 'emission', half_width=60.0)
    reasons = [s['reason'] for s in win['excluded']]
    assert reasons and all(r.startswith('因天光发射排除') for r in reasons)
    # 大气吸收措辞：窗（6820±60）与 O2 A/B 带相交、线心在段外不拒
    win2 = LN.select_window(lam, 6820.0, 'emission', half_width=60.0)
    reasons2 = [s['reason'] for s in win2['excluded']]
    assert reasons2 and all(r.startswith('因大气吸收排除') for r in reasons2)
    for a, b in zip(reasons, reasons2):
        assert not a.startswith(b.split('：')[0])  # 两类措辞互不混用（T-20）
    # F-72③：发射线线心落在大气吸收带内 ⇒ E-14
    with pytest.raises(LN.LineError) as ei:
        LN.select_window(lam, 6875.0, 'emission', half_width=20.0)
    assert ei.value.code == 'E-14'
    assert ei.value.details['reason'] == 'line_center_in_abs_band'
    assert '大气吸收' in str(ei.value)
    # F-72③：吸收线线心落在气辉线上 ⇒ E-14（措辞与上者可辨）
    with pytest.raises(LN.LineError) as ei2:
        LN.select_window(lam, 6363.0, 'absorption', half_width=20.0)
    assert ei2.value.details['reason'] == 'line_center_in_skyline'
    assert '天光发射' in str(ei2.value)
    # measure_line 全链同样可达（emission × 吸收带）
    with pytest.raises(LN.LineError):
        LN.measure_line(lam, flux, sig, 6875.0, 'emission', n_boot=0)


# ─── ③ T-21：空气/真空（宿主 wavconvert 唯一通路） ────────────────────────

def test_t21_air_vac_via_host_wavconvert():
    import wavconvert as WV                      # 宿主模块（只读调用）
    # 规格数：λ_vac=6300.21 ⇒ λ_air=6298.468（偏移 1.742 Å ≡ 82.9 km/s）
    got = WV.vacuum_to_air(6300.21)
    assert abs(got - 6298.468) < 0.02            # T-21 容差 0.02 Å
    assert abs((6300.21 - got) / 6300.21 * 299792.458 - 82.9) < 0.5   # ≡82.9 km/s
    d3200 = 3200.0 - WV.vacuum_to_air(3200.0)
    d8000 = 8000.0 - WV.vacuum_to_air(8000.0)
    assert abs(d3200 - 0.9247) < 0.01 and abs(d8000 - 2.1998) < 0.01
    rt = WV.air_to_vacuum(WV.vacuum_to_air(6300.21))
    assert abs(rt - 6300.21) < 1e-6              # 往返残差 < 1e-6 Å
    assert WV.vacuum_to_air(1999.0) == 1999.0    # < 2000 Å 两向原样返回
    assert WV.air_to_vacuum(1999.0) == 1999.0


@pytest.mark.skip(reason='T-21 的线心表逐条对 NIST ASD 核对属 M-6（线表帧复核）：'
                         '宿主 29 组固定线表均为整数 Å、未标帧，M-6 未完成前该'
                         '核对不具判据效力（F-38②）；vel_*/z_from_lines 随之在 '
                         'P3c 前禁用，lines.py 只留接口位')
def test_t21_nist_line_table_crosscheck_m6_pending():
    raise AssertionError('M-6 未完成')


# ─── ④ T-65：项分解、窗口因子、E-15、CA-44⑤/⑥ ───────────────────────────

def _clean_line(seed=3, order=0):
    lam = _axis()
    u = np.log(lam)
    sig = np.full(lam.size, 1.0)
    line = 20.0 * np.exp(-0.5 * ((u - math.log(6500.0)) / 8e-4) ** 2)
    rng = np.random.default_rng(seed)
    return lam, 100.0 + line + rng.normal(0.0, 1.0, lam.size), sig


def test_t65_window_factor_monotone():
    # continuum 项随 (Δλ−W) 单调增：积分窗加倍 ⇒ > 1.4×（窗内 continuum 与
    # coherent 同式同值，order=0 时二者恒一致，交叉印证窗口因子落地）
    prev = None
    for hw in (30.0, 60.0):
        lam, flux, sig = _clean_line()
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                            err_seed=SEED, baseline_order=0, half_width=hw)
        t = r['ew_err_terms']
        assert t['continuum'] is not None and t['continuum'] > 0
        assert abs(t['continuum'] - t['continuum_coherent']) \
            <= 0.05 * t['continuum']             # order=0：逐像素≈整体平移
        if prev is not None:
            assert t['continuum'] / prev > 1.4   # T-65 原文阈
        prev = t['continuum']


def test_t65_photon_term_matches_analytic_propagation():
    # 纯光子噪声谱（连续谱精确已知）：photon 项 = 解析传播 √(Σ(Δλσ/C)²)
    lam = _axis()
    u = np.log(lam)
    rng = np.random.default_rng(SEED)
    noise = 1.0
    fl = 100.0 + 20.0 * np.exp(-0.5 * ((u - math.log(6500.0)) / 8e-4) ** 2) \
        + rng.normal(0.0, noise, lam.size)
    sig = np.full(lam.size, noise)
    r = LN.measure_line(lam, fl, sig, 6500.0, 'emission', n_boot=0,
                        err_seed=SEED, baseline_order=0)
    win = LN.select_window(lam, 6500.0, 'emission')
    widx = np.flatnonzero(LN._window_mask(lam, win))
    dl = LN._grad(lam)[widx]
    analytic = math.sqrt(float(np.sum((dl * noise / 100.0) ** 2)))
    assert abs(r['ew_err_terms']['photon'] / analytic - 1.0) < 0.05
    # photon 主导（弱基线噪声下联合项不被光子项淹没的秩序校验）
    assert r['ew_err_aa'] >= r['ew_err_terms']['photon']


def test_t65_ca445_coherent_dominance_reachable():
    # CA-44⑤（coherent/continuum > 3×）：F-96③ 明文「两者之比的期望量级是
    # O(1)」⇒ > 3 是病态档，自然谱例不可靠构造；按 T-67③ 同款「反向注入」
    # 手法，从 _ew_terms 缝隙注入病态项分解，验证触发分支全链可达且文案带
    # baseline.order / Δλ / W（F-96③/TXT-23）。
    lam, flux, sig = _clean_line()
    orig = LN._ew_terms

    def patched(win, lam_, flux_, sigma_, base, dlam):
        t = orig(win, lam_, flux_, sigma_, base, dlam)
        t['continuum'] = 0.01 * t['continuum']   # 病态：逐像素项被压扁
        return t

    LN._ew_terms = patched
    try:
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                            err_seed=SEED)
    finally:
        LN._ew_terms = orig
    t = r['ew_err_terms']
    assert t['continuum_coherent'] / t['continuum'] > 3.0
    msg = [w for w in r['warnings']
           if w['code'] == 'CA-44' and 'coherent' in w['message']]
    assert msg and '3' in msg[0]['message']
    assert str(r['baseline']['order']) in msg[0]['message']
    assert 'Δλ' in msg[0]['message'] and 'W=' in msg[0]['message']


def test_t65_missing_baseline_term_e15():
    # 缺连续谱/基线项且自助不可用 ⇒ E-15 的 d) 类（F-94③/F-96⑥）
    lam, flux, sig = _clean_line()
    orig = LN.baseline_fit

    def patched(*a, **k):
        b = orig(*a, **k)
        b['cov'] = None                          # 模拟侧带协方差秩亏/不可得
        return b

    LN.baseline_fit = patched
    try:
        with pytest.raises(LN.LineError) as ei:
            LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                            err_seed=SEED)
        assert ei.value.code == 'E-15'
        assert ei.value.details['reason'] == 'ew_err_baseline_term_missing'
        # 自助可用时改为回落而非 E-15（F-95② 主路径升格）
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=10,
                            err_seed=SEED)
        assert r['err_source']['ew_obs_aa'] == 'bootstrap'
        assert r['ew_err_aa'] is not None and r['ew_err_aa'] > 0
    finally:
        LN.baseline_fit = orig


def test_t65_closed_vs_bootstrap_ca446_both_listed():
    lam, flux, sig = _clean_line()
    orig_boot = LN._bootstrap

    def patched(*a, **k):                        # 反向注入：自助散布压到 1/10
        out = orig_boot(*a, **k)
        th = 0.5 * (float(np.percentile(out['ew'], 84))
                    - float(np.percentile(out['ew'], 16)))
        mid = 0.5 * (float(np.percentile(out['ew'], 84))
                     + float(np.percentile(out['ew'], 16)))
        out['ew'] = np.array([mid - 0.05 * th, mid + 0.05 * th])
        return out

    LN._bootstrap = patched
    try:
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=12,
                            err_seed=SEED)
    finally:
        LN._bootstrap = orig_boot
    msgs = [w for w in r['warnings']
            if w.get('reason') == 'closed_vs_bootstrap']
    assert len(msgs) == 1 and msgs[0]['code'] == 'CA-44'
    # 两个都列：闭式 σ_W 与自助分位端点同屏
    assert r['ew_err_aa'] is not None
    assert r['ew_boot_lo_aa'] is not None and r['ew_boot_hi_aa'] is not None
    assert '闭式' in msgs[0]['message'] and '自助' in msgs[0]['message']


def test_t65_reproducibility_and_seed_isolation():
    # T-66-lite：同输入+同 seed ⇒ 逐字节相同；换 seed ⇒ 自助列变、闭式列不变
    lam, flux, sig = _clean_line(seed=SEED)
    kw = dict(n_boot=12, err_seed=SEED)
    r1 = LN.measure_line(lam, flux, sig, 6500.0, 'emission', **kw)
    r2 = LN.measure_line(lam, flux, sig, 6500.0, 'emission', **kw)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    assert r1['rng_algo'] == 'pcg64' and r1['n_boot'] == 12
    r3 = LN.measure_line(lam, flux, sig, 6500.0, 'emission',
                         n_boot=12, err_seed=SEED + 1)
    assert r3['ew_boot_lo_aa'] != r1['ew_boot_lo_aa']      # 自助列变
    for k in ('ew_err_aa', 'ew_err_terms', 'lambda_obs_vac_aa', 'fwhm_obs_aa'):
        assert r1[k] == r3[k]                              # 闭式/协方差列不变


# ─── ⑤ CA-44 线侧档：④ snr 门 / ① 收敛 / 预算回落 ────────────────────────

def test_ca44_4_snr_gate_nondetection():
    lam = _axis()
    u = np.log(lam)
    rng = np.random.default_rng(SEED)
    sig = np.full(lam.size, 100.0)               # SNR_res ≈ 1.7 < 门限 3
    fl = 100.0 + 20.0 * np.exp(-0.5 * ((u - math.log(6500.0)) / 8e-4) ** 2) \
        + rng.normal(0.0, 100.0, lam.size)
    r = LN.measure_line(lam, fl, sig, 6500.0, 'emission', n_boot=0,
                        err_seed=SEED)
    assert r['detected'] is False                # 不拟合（F-68）
    assert r['snr_res'] < C_LINE_SNR_MIN
    assert r['upper_limit_3sigma'] > 0           # 3σ 上限（count 免配对行）
    # §3.9.3.1「σ_W 取 F-41/F-96 的总项」：nodetect 时 continuum 可得 ⇒
    # 上限 = 3·√(photon² + continuum²)（不再是 3·photon 单项）
    win = LN.select_window(lam, 6500.0, 'emission')
    t0 = LN._ew_terms(win, lam, fl, sig,
                      LN.baseline_fit(lam, fl, sig,
                                      LN._sideband_idx(lam, win),
                                      np.arange(lam.size), 1),
                      LN._grad(lam))
    ul_expect = 3.0 * math.sqrt(t0['photon'] ** 2 + t0['continuum'] ** 2)
    assert abs(r['upper_limit_3sigma'] / ul_expect - 1.0) < 1e-12
    assert r['upper_limit_3sigma'] > 3.0 * t0['photon']   # 总项 > 单 photon 项
    assert r['err_source']['upper_limit_3sigma'] == 'count'
    for k in ('ew_obs_aa', 'ew_err_aa', 'lambda_obs_vac_aa', 'line_flux'):
        assert r[k] is None                      # 误差键/值键 null + CA-44④
    assert r['err_source']['ew_obs_aa'] == 'none'
    assert any(w['code'] == 'CA-44' and w.get('reason') == 'snr_res_below_gate'
               for w in r['warnings'])
    assert r['null_reason'] and '未检出' in r['null_reason']


def test_ca44_1_nonconvergence_flagged():
    # 撞 max_nfev ⇒ status=0 ⇒ CA-44①（把 C_NFEV_MAX 压到 1 反向注入）
    lam, flux, sig = _clean_line()
    orig = LN.C_NFEV_MAX
    LN.C_NFEV_MAX = 1
    try:
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                            err_seed=SEED)
        assert r['engine_status']['status'] == 0
        assert any(w['code'] == 'CA-44' and '未收敛' in w['message']
                   for w in r['warnings'])
    finally:
        LN.C_NFEV_MAX = orig


def test_budget_fallback_boot_budget_applied():
    lam, flux, sig = _clean_line()
    r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=10 ** 6,
                        budget_left=12, err_seed=SEED)
    assert r['n_boot'] == 12 and r['n_boot'] < 10 ** 6
    assert r['boot_budget_applied'] is True       # F-95③ 回落 + CA-44③
    assert any(w.get('reason') == 'boot_budget' for w in r['warnings'])


def test_voigt_disabled_and_bad_kind():
    lam, flux, sig = _clean_line()
    with pytest.raises(LN.LineError) as ei:
        LN.measure_line(lam, flux, sig, 6500.0, 'emission', profile='voigt',
                        n_boot=0)
    assert ei.value.details['reason'] == 'voigt_disabled_no_r'   # F-39/T-22
    with pytest.raises(LN.LineError):
        LN.measure_line(lam, flux, sig, 6500.0, None, n_boot=0)  # F-66 必填
    assert C_MAX_COMPONENTS >= 2                                 # gauss2 可容纳


def test_velocity_family_slots_p3c():
    lam, flux, sig = _clean_line()
    r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                        err_seed=SEED)
    for k in ('vel_fwhm_kms', 'vel_shift_kms', 'fwhm_intr_aa', 'z_fit'):
        assert r[k] is None                       # M-6：速度族 P3c 留位
    assert 'M-6' in r['velocity_family_note']
    assert 'TXT-8' in r['width_note']             # 观测宽度非本征宽度脚注


# ─── ⑥ F-71 生长曲线 ─────────────────────────────────────────────────────

def test_f71_n_coef_derived_not_copied():
    # 「就地导出、不抄二手数值」：由 scipy.constants 复算 m_e c²/(π e²)（cgs）
    # × Å↔cm 换算（W_mÅ→1e-11 cm、λ0²→1e-16 cm²）钉 N_COEF_CGS
    from scipy.constants import c, e, m_e
    me_g, c_cms = m_e * 1e3, c * 1e2
    e_esu = e * 2.99792458e9                      # C → statC（c_m/s ×10）
    k_cgs = me_g * c_cms ** 2 / (math.pi * e_esu ** 2)   # cm⁻¹
    assert abs(k_cgs * 1e5 / LN.N_COEF_CGS - 1.0) < 1e-3


def test_f71_linear_regime_and_sigma_scaling():
    ew, ew_err = 1.0, 0.1
    g = LN.growth_curve([{'species': 'mg_ii', 'lambda0_rest_aa': 2796.0,
                          'f_osc': 0.3, 'ew_obs_aa': ew, 'ew_err_aa': ew_err}])
    n_expect = LN.N_COEF_CGS * ew * 1e3 / (2796.0 ** 2 * 0.3)
    assert abs(g['column_density'] / n_expect - 1.0) < 1e-12
    assert abs(g['column_density_err'] / (n_expect * ew_err / ew) - 1.0) < 1e-12
    assert g['err_source'] == 'delta_method' and not g['saturated']
    # f 缺 ⇒ 只报 EW（note 书面原因，不冒充）
    g2 = LN.growth_curve([{'species': 'x', 'lambda0_rest_aa': 3000.0,
                           'f_osc': None, 'ew_obs_aa': 0.5,
                           'ew_err_aa': 0.05}])
    assert g2['column_density'] is None and g2['err_source'] == 'none'
    assert '振子强度' in g2['per_line'][0]['note']


def test_f71_saturation_lower_bound_ca27():
    # 同离子两线推出的 N 互差 > C_COG_TOL_RATIO ⇒ 饱和：只报下限、σ null
    lines = [{'species': 'mg_ii', 'lambda0_rest_aa': 2796.0, 'f_osc': 0.3,
              'ew_obs_aa': 1.0, 'ew_err_aa': 0.05},
             {'species': 'mg_ii', 'lambda0_rest_aa': 2803.0, 'f_osc': 0.15,
              'ew_obs_aa': 2.4, 'ew_err_aa': 0.05}]
    g = LN.growth_curve(lines)
    assert g['saturated'] is True
    assert g['column_density'] is None and g['column_density_err'] is None
    n1 = LN.N_COEF_CGS * 1.0e3 / (2796.0 ** 2 * 0.3)
    n2 = LN.N_COEF_CGS * 2.4e3 / (2803.0 ** 2 * 0.15)
    assert (max(n1, n2) - min(n1, n2)) / min(n1, n2) > 0.3
    assert abs(g['column_density_lower_bound'] / min(n1, n2) - 1.0) < 1e-12
    assert any(w['code'] == 'CA-27' for w in g['warnings'])
    assert g['ca27'] and '下限' in g['ca27']


def test_f71_bootstrap_quantiles_curved_branch():
    # 弯曲段禁对称 ⇒ 自助 16/84 分位（boot_ews 复本接口）
    lines = [{'species': 'mg_ii', 'lambda0_rest_aa': 2796.0, 'f_osc': 0.3,
              'ew_obs_aa': 1.0, 'ew_err_aa': 0.1}]
    boot = [np.random.default_rng(SEED).normal(1.0, 0.1, 200)]
    g = LN.growth_curve(lines, boot_ews=boot)
    assert g['err_source'] == 'bootstrap'
    assert g['column_density_err_lo'] >= 0 and g['column_density_err_hi'] > 0


# ─── ⑧ P0 回归：_sideband_idx 侧带方向（F-36「线区两侧各 C_BASE_SIDE 点」） ──

def _coarse_wide_spec(profile, fwhm_aa, center=6700.0, A=20.0, c0=100.0,
                      dlam=4.0, seed=SEED):
    """粗采样（Δλ≈4 Å）+ 宽线合成谱。波长轴 6540–6848 Å：两侧窗外各有
    ≥C_BASE_SIDE 个未掩膜点（避开 C_MASK_ABS_TABLE 的 6867+ 与气辉表）。

    返回 (lam, flux, sig, ew_trunc_true)：ew_trunc_true = A/c0·∫_win shape dλ
    （截断到线窗的解析真值——F-36 的测度就是窗内积分，无穷翼解析 EW 不是
    可测目标，F-96④ 明文保留窗口宽度依赖行为）。"""
    lam = np.arange(6540.0, 6852.0, dlam)
    u = np.log(lam)
    if profile == 'gauss':
        s = fwhm_aa / (2.3548200700252868 * center)
        shape = np.exp(-0.5 * ((u - math.log(center)) / s) ** 2)
    else:
        g = fwhm_aa / (2.0 * center)
        shape = 1.0 / (1.0 + ((u - math.log(center)) / g) ** 2)
    rng = np.random.default_rng(seed)
    flux = c0 + A * shape + rng.normal(0.0, 1.0, lam.size)
    sig = np.full(lam.size, 1.0)
    win = LN.select_window(lam, center, 'emission')
    widx = np.flatnonzero(LN._window_mask(lam, win))
    ew_trunc = (A / c0) * float(np.sum(shape[widx] * LN._grad(lam)[widx]))
    return lam, flux, sig, ew_trunc


def test_p0_sideband_idx_strictly_outside_window():
    # 方向回归的单点钉：lo 边侧带全部在窗外（λ < win.lo）、hi 边全部 λ > win.hi
    lam = _axis()
    win = LN.select_window(lam, 6500.0, 'emission')
    idx = LN._sideband_idx(lam, win, n_side=10)   # 返回拼接列表：前 10 lo、后 10 hi
    lo_idx, hi_idx = idx[:10], idx[-10:]
    assert np.all(lam[lo_idx] < win['lo']) and np.all(lam[hi_idx] > win['hi'])


def test_p0_coarse_sampling_wide_line_ew_recovery():
    # 修正前（lo 边向窗内扫、侧带压在线翼上）：gauss FWHM=30 ⇒ EW −52%、
    # lorentz FWHM=40 ⇒ −84%（本仓库复现实测）；修正后 gauss 回收 <5%。
    lam, flux, sig, ew_true = _coarse_wide_spec('gauss', 30.0)
    r = LN.measure_line(lam, flux, sig, 6700.0, 'emission', profile='gauss1',
                        n_boot=0, err_seed=SEED)
    assert abs(r['fwhm_obs_aa'] / 30.0 - 1.0) < 0.10      # 宽度回收
    assert abs(r['ew_obs_aa'] / ew_true - 1.0) < 0.05     # 修正后实测 −1.4%

    # lorentz FWHM=40：修正后 vs 截断窗真值残差 −14%（本仓库实测）。根因
    # （§10.1 元判据登记，非拍脑袋）：lorentz 宽翼（γ=20 Å）延伸出 F-36 线窗、
    # 混入固定侧带（窗沿处翼余量 +4.0 ≈ 4σ、侧带均值 +1.29 ≈ 1.3σ），侧带
    # poly 基线被系统性抬高 ~1.5% ⇒ 经 (Δλ−W) 杠杆放大成 −14%。这是 F-36
    # 固定侧带设计对洛伦兹翼的估计量物理极限（F-96④ 明文保留窗口依赖行为），
    # 不是方向缺陷；容差按残差 ×1.5 取 20%。无穷翼解析 EW（12.57 Å）不是
    # 本测度的可测目标，不作比较基准。
    lam2, flux2, sig2, ew_true2 = _coarse_wide_spec('lorentz', 40.0)
    r2 = LN.measure_line(lam2, flux2, sig2, 6700.0, 'emission', profile='lorentz',
                         n_boot=0, err_seed=SEED)
    assert abs(r2['fwhm_obs_aa'] / 40.0 - 1.0) < 0.10
    assert abs(r2['ew_obs_aa'] / ew_true2 - 1.0) < 0.20   # 实测 −14.0%


def test_p0_sideband_gate_e04_true_triggers():
    # E-04 侧带点数不足守门恢复真触发（修正前 lo 边扫窗内永不触发该侧）：
    # 每侧窗外 < C_BASE_SIDE 个点 ⇒ E-04
    lam = np.arange(6440.0, 6562.0, 4.0)   # 窗 6460–6540 外仅 ~5+5 点
    u = np.log(lam)
    flux = 100.0 + 20.0 * np.exp(-0.5 * ((u - math.log(6500.0)) / 1e-4) ** 2)
    sig = np.full(lam.size, 1.0)
    with pytest.raises(LN.LineError) as ei:
        LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0)
    assert ei.value.code == 'E-04'
    assert '基线侧带点数不足' in str(ei.value)


# ─── ⑨ P1-3：F-41/F-55 相邻像素相关放大 ──────────────────────────────────

def test_f41_correlated_pixels_inflate_closed_form():
    # AR(1)(ρ≈0.5) 合成谱 vs 白噪声同谱：闭式 photon 与总项须乘
    # √((1+ρ)/(1−ρ))（F-41「相邻像素相关时按 F-55 放大」；F-55 既有口径：
    # ρ 不可估或 ≤C_RHO_MIN 不放大）。同一噪声实现、只改其相关结构。
    from specphot.constants import C_RHO_MIN
    lam = _axis()
    u = np.log(lam)
    shape = 20.0 * np.exp(-0.5 * ((u - math.log(6500.0)) / 8e-4) ** 2)
    rng = np.random.default_rng(SEED)
    eps = rng.normal(0.0, 1.0, lam.size)
    rho_true = 0.5
    ar = np.empty_like(eps)
    ar[0] = eps[0] / math.sqrt(1.0 - rho_true ** 2)
    for i in range(1, lam.size):                 # AR(1)：σ 恒 1、lag-1 ρ=0.5
        ar[i] = rho_true * ar[i - 1] + eps[i]
    sig = np.full(lam.size, 1.0)
    r_w = LN.measure_line(lam, 100.0 + shape + eps, sig, 6500.0, 'emission',
                          n_boot=0, err_seed=SEED, baseline_order=0)
    r_a = LN.measure_line(lam, 100.0 + shape + ar, sig, 6500.0, 'emission',
                          n_boot=0, err_seed=SEED, baseline_order=0)
    infl_expect = math.sqrt((1.0 + rho_true) / (1.0 - rho_true))
    assert r_a['rho_used'] is not None and r_a['rho_used'] > C_RHO_MIN
    # 放大倍率对回显 rho_used 自洽（rho_lag1 在含线谱上估计、含线信号的
    # 相关贡献——与白化路 F-92 同一估计量，故对回显值断言而非真值 0.5）
    infl_echo = math.sqrt((1.0 + r_a['rho_used']) / (1.0 - r_a['rho_used']))
    # photon 项放大可见（同噪声功率 ⇒ 白噪声 photon 为基线；C 差 <0.1%）
    assert abs(r_w['ew_err_terms']['photon']
               / (r_a['ew_err_terms']['photon'] / infl_echo) - 1.0) < 0.02
    assert infl_echo > infl_expect * 0.9               # 放大方向与量级正确
    assert r_a['ew_err_aa'] > r_w['ew_err_aa'] * 1.2      # 总项放大可见
    assert 'F-55' in (r_a.get('ew_err_note') or '')       # note 注明放大
    # ρ ≤ C_RHO_MIN（白噪声谱）⇒ 不放大、无 note
    assert r_w['rho_used'] is None or r_w['rho_used'] <= C_RHO_MIN
    assert 'ew_err_note' not in r_w
    # line_flux（发射线）同式放大
    assert r_a['line_flux_err'] > r_w['line_flux_err'] * 1.2


# ─── ⑩ P1-5：F-62③ 基线 cond_2 超限降阶 / 拒测 ──────────────────────────

def _dup_lambda_sideband_spec():
    """病态侧带构造：侧带 λ 大量重复 ⇒ Chebyshev 设计矩阵数值秩亏
    （cond=inf > C_COND_MAX）；降到满秩阶数后恢复。窗 6499.5–6500.5 不撞
    掩膜表。"""
    lam = np.sort(np.concatenate([
        np.repeat(6499.5 - np.arange(5) * 1e-3, 10),   # 左侧带仅 5 个不同 λ
        np.linspace(6499.5, 6500.5, 30),
        np.repeat(6500.5 + np.arange(5) * 1e-3, 10)]))
    flux = np.full(lam.size, 100.0)
    sig = np.full(lam.size, 1.0)
    return lam, flux, sig


def test_f62_cond_max_order_reduction_ca20():
    from specphot.constants import C_COND_MAX
    lam, flux, sig = _dup_lambda_sideband_spec()
    r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', baseline_order=7,
                        half_width=0.5, n_boot=0, err_seed=SEED)
    assert r['baseline']['order'] < 7                   # 自动降阶发生
    assert r['baseline']['cond_2'] <= C_COND_MAX        # 降阶后 cond 达标
    ca20 = [w for w in r['warnings'] if w['code'] == 'CA-20']
    assert ca20 and all('降阶' in w['message'] for w in ca20)
    assert r['detected'] and r['ew_err_aa'] is not None  # 降阶不阻断测量


def test_f62_cond_max_order_zero_still_over_rejects_e14():
    # 降到 0 阶（常数基线，cond 恒 1）仍超 ⇒ 拒测 E-14（F-62③ 末支）。
    # 0 阶 cond 数值上不可超 1e8，按 CA-44① 同款压阈注入手法验证分支可达。
    lam, flux, sig = _clean_line()
    orig = LN.C_COND_MAX
    LN.C_COND_MAX = 0.5
    try:
        with pytest.raises(LN.LineError) as ei:
            LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0)
    finally:
        LN.C_COND_MAX = orig
    assert ei.value.code == 'E-14'
    assert ei.value.details['reason'] == 'baseline_cond_max'


# ─── ⑪ P2 择要：CA-44 reason 签 / NaN 守卫 / EW=0 守卫 / 分档源码钉 ──────

def test_p2_ca44_reason_signs_and_nonfinite_guards():
    # ① 闭式回落自助：CA-44 带 reason='closed_fallback_bootstrap'
    lam, flux, sig = _clean_line()
    orig_bf = LN.baseline_fit

    def patched_bf(*a, **k):
        b = orig_bf(*a, **k)
        b['cov'] = None                          # 模拟侧带协方差不可得
        return b

    LN.baseline_fit = patched_bf
    try:
        r = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=10,
                            err_seed=SEED)
    finally:
        LN.baseline_fit = orig_bf
    w1 = [x for x in r['warnings']
          if x.get('reason') == 'closed_fallback_bootstrap']
    assert len(w1) == 1 and w1[0]['code'] == 'CA-44'

    # ② fwhm_obs_err_aa 的 delta_method 非有限 ⇒ null + CA-44②
    # （注入非对角 NaN 的协方差：对角有限让链式通路可达，二次型出 NaN）
    orig_cfj = LN._cov_from_jac

    def patched_cfj(M, dof):
        cov = np.eye(M.shape[1])
        if cov.shape[0] >= 2:
            cov[0, 1] = cov[1, 0] = np.nan
        return cov, None

    LN._cov_from_jac = patched_cfj
    try:
        r2 = LN.measure_line(lam, flux, sig, 6500.0, 'emission', n_boot=0,
                             err_seed=SEED)
    finally:
        LN._cov_from_jac = orig_cfj
    assert r2['fwhm_obs_err_aa'] is None        # NaN 不出户（F-93③）
    assert any(x.get('reason') == 'derived_err_nonfinite'
               for x in r2['warnings'])

    # ③ depth 的 delta_method 非有限（σ NaN）⇒ null + CA-44②
    #    （depth_err_unavailable reason 签，P2b②）
    orig_dg = LN._depth_grad
    LN._depth_grad = lambda theta, *a, **k: np.full(np.asarray(theta).size,
                                                    np.nan)
    try:
        r3 = LN.measure_line(lam, flux, sig, 6500.0, 'absorption', n_boot=0,
                             err_seed=SEED)
    finally:
        LN._depth_grad = orig_dg
    assert r3['depth'] is not None              # 值键在场，误差键 null
    assert r3['depth_err_lo'] is None and r3['depth_err_hi'] is None
    w3 = [x for x in r3['warnings']
          if x.get('reason') == 'depth_err_unavailable']
    assert len(w3) == 1 and w3[0]['code'] == 'CA-44'


def test_f71_zero_ew_line_family_guard():
    # EW=0（未检出）线不进柱密度族：修正前 (max−min)/min 除零崩溃、
    # 逆方差 1/σ_N² 除零；修正后该线只报 EW + 书面原因，家族照常出数
    lines = [{'species': 'mg_ii', 'lambda0_rest_aa': 2796.0, 'f_osc': 0.3,
              'ew_obs_aa': 0.0, 'ew_err_aa': 0.05},
             {'species': 'mg_ii', 'lambda0_rest_aa': 2803.0, 'f_osc': 0.15,
              'ew_obs_aa': 1.0, 'ew_err_aa': 0.1}]
    g = LN.growth_curve(lines)
    assert g['per_line'][0]['column_density'] is None
    assert '不进柱密度族' in g['per_line'][0]['note']
    assert g['saturated'] is False
    n_expect = LN.N_COEF_CGS * 1.0e3 / (2803.0 ** 2 * 0.15)
    assert abs(g['column_density'] / n_expect - 1.0) < 1e-12


def test_p1_ca27_grouped_as_err_in_results_js():
    # §5.4 末分档行：误差族 = CA-06/21/22/27/44/47 ⇒ CA-27 ∈ 'err'
    # （P1-2 评审修正：原归 'approx' 违反分档权威行）
    import os
    p = os.path.join(os.path.dirname(__file__), '..', '..', 'frontend',
                     'js', 'specphot', 'results.js')
    with open(p, encoding='utf-8') as f:
        rs = f.read()
    assert "'CA-27': 'err'" in rs
    assert "'CA-27': 'approx'" not in rs


# ─── ⑦ 常量与引擎静态纪律（T-79①/T-59 同款源码扫描，S3 侧） ──────────────

def test_engine_discipline_static_scan():
    # T-59/T-79① 同款纪律（S3 侧）：全部 least_squares 调用为 trf、
    # 无 loss=/f_scale/method='lm'/dogbox/curve_fit 调用点（AST 级）
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(LN))
    n_ls = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', '') \
                == 'least_squares':
            n_ls += 1
            kws = {kw.arg for kw in node.keywords}
            assert 'method' in kws or any(
                isinstance(kw.value, ast.Constant) and kw.value.value == 'trf'
                for kw in node.keywords) or _has_trf_kwarg(node)
            assert 'loss' not in kws and 'f_scale' not in kws
    assert n_ls >= 2
    src = inspect.getsource(LN)
    assert "method='trf'" in src
    for bad in ("method='lm'", "method='dogbox'", 'curve_fit('):
        assert bad not in src


def _has_trf_kwarg(node):
    return any(kw.arg == 'method' and getattr(kw.value, 'value', None) == 'trf'
               for kw in node.keywords)


def test_default_window_constant_echo():
    assert C_LINE_WIN == 40.0          # §7.10 默认半宽（F-35）
    lam = _axis()
    win = LN.select_window(lam, 6500.0, 'emission')
    assert win['lo'] == 6500.0 - C_LINE_WIN and win['hi'] == 6500.0 + C_LINE_WIN
