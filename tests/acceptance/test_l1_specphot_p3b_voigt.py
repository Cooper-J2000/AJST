"""L1: specphot P3b/P3c 纯函数层——voigt 轮廓（F-39/F-40）+ 天光扣除（F-86）。

P3b · voigt（§9 P3b 行 / F-39 / F-40 / T-22）：
  - _voigt_profile 解析性质：gauss 极限（γ→0）、lorentz 极限（σ→0）、
    Olivero–Longbothum FWHM 近似对数值半高全宽逐点核对（<0.1%）、峰归一
    （线心值=1）、单位峰下 A 的语义与 gauss/lorentz 一致。
  - 闸门（本期交付形态）：全库无 R（r_source='none'，M-4 未完成）⇒ voigt
    拒绝维持（E-14，reason=voigt_disabled_no_r，文案对齐 F-39 原文 + TXT-8）；
    R 可得（合成 fixture 用户侧 R）⇒ 数值路径回收（中心/EW/FWHM）。
  - F-40：不集成 VoigtFit 本体——静态扫描 lines.py 不含 VoigtFit 字样，
    voigt 走 scipy.special（既有依赖，零新增，ST-6/ST-14）。

P3c · sky_subtract（F-86 / U-31='subtract' / CA-37 / F-58 / T-51 末支）：
  - 真解锁判定：只依赖 C_MASK_EMIS_TABLE 常量与谱数据（不依赖 M-6 线表帧/f
    值）⇒ 按原文真实现；大气吸收带永远 mask（F-72①）。
  - 合成天光线（[O I] 5577 名义中心）被成功扣除：残差 RMS 比 < C_SKY_RESID_FLOOR、
    sky_subtracted=True、被扣段脱 mask、科学线 EW 回收。
  - 无天光线/与科学线同位 ⇒ 退回 mask + reverted=True（调用方挂 CA-37，
    不得称「已扣除」）。
  - 只读纪律：输入数组逐字节不变。

元判据（§10.1）：seed = 20260927；纯函数、脱库可跑、只读。几何纪律同
test_l1_specphot_lines.py：合成谱避开大气吸收带。
"""
import math
import os
import sys

import numpy as np
import pytest

from specphot import lines as LN
from specphot.constants import (C_MASK_ABS_TABLE, C_MASK_EMIS_TABLE,
                                C_SKY_RESID_FLOOR)

SEED = 20260927


def _lam(lo=6300.0, hi=6700.0, n=2000):
    return np.linspace(lo, hi, n)


# ═══ P3b · voigt：_voigt_profile 解析性质 ═══════════════════════════════════

def test_voigt_limits_gauss_and_lorentz():
    """极限性质：γ→0 退化为高斯、σ→0 退化为洛伦兹（同峰归一口径）。"""
    x = np.linspace(-6.0, 6.0, 12001)
    v_g = LN._voigt_profile(x, 0.5, 1e-10)
    g = np.exp(-0.5 * (x / 0.5) ** 2)
    assert float(np.max(np.abs(v_g - g))) < 1e-6
    v_l = LN._voigt_profile(x, 1e-10, 1.0)
    lo = 1.0 / (1.0 + x ** 2)
    assert float(np.max(np.abs(v_l - lo))) < 1e-6


def test_voigt_fwhm_olivero_longbothum_numeric():
    """O-L 近式 FWHM vs 数值半高全宽：跨 γ/σ 比值网格逐点 <0.1%（就地可导出的
    数值代数性质，非文献值外推）。"""
    x = np.linspace(-60.0, 60.0, 240001)
    for sig, gam in ((1.0, 1e-3), (1.0, 0.3), (1.0, 1.0), (1.0, 3.0),
                     (0.2, 1.0), (3.0, 0.1)):
        v = LN._voigt_profile(x, sig, gam)
        above = x[v >= 0.5]
        fwhm_num = float(above[-1] - above[0])
        rel = abs(LN._voigt_fwhm_u(sig, gam) - fwhm_num) / fwhm_num
        assert rel < 1e-3, (sig, gam, rel)


def test_voigt_peak_normalized_and_fwhm_grad():
    """峰归一（线心=1）；O-L 梯度与中心差分一致（fwhm_err 的 delta_method
    Jacobian 口径，F-95①）。"""
    assert float(LN._voigt_profile(0.0, 0.5, 0.8)) == 1.0
    w, g, h = 0.047, 0.041, 1e-7
    fd_w = (LN._voigt_fwhm_u(w + h, g) - LN._voigt_fwhm_u(w - h, g)) / (2 * h)
    fd_g = (LN._voigt_fwhm_u(w, g + h) - LN._voigt_fwhm_u(w, g - h)) / (2 * h)
    ana_w, ana_g = LN._voigt_fwhm_u_grad(w, g)
    assert abs(ana_w - fd_w) / abs(fd_w) < 1e-6
    assert abs(ana_g - fd_g) / abs(fd_g) < 1e-6


def test_voigt_not_voitfit_integration_f40():
    """F-40：禁 VoigtFit 本体集成——lines.py 源码不含 VoigtFit 字样（scipy.special
    是既有依赖，零新增）。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    with open(os.path.join(root, 'backend', 'specphot', 'lines.py'),
              encoding='utf-8') as f:
        src = f.read()
    assert 'VoigtFit' not in src.replace('禁 VoigtFit 本体', '').replace(
        '不集成 VoigtFit', '')
    assert 'scipy.special.voigt_profile' in src


# ═══ P3b · voigt：闸门两态（F-39/T-22 后端半支） ═════════════════════════════

def _voigt_spec(seed=SEED, r_resolution=5000.0):
    """合成 voigt 发射线（σ_u=4e-4、γ_u=2e-4 @6500 Å）+ 白噪声。"""
    lam = _lam()
    rng = np.random.default_rng(seed)
    u = np.log(lam)
    prof = LN._voigt_profile(u - math.log(6500.0), 4e-4, 2e-4)
    flux = 100.0 + 20.0 * prof + rng.normal(0, 0.5, lam.size)
    return lam, flux, np.full(lam.size, 0.5)


def test_voigt_gate_no_r_reject_f39_t22():
    """闸门态：无 R ⇒ E-14 voigt_disabled_no_r（文案对齐 F-39 原文：观测宽度 +
    TXT-8）；这是全库现状（r_source='none'，M-4 未完成）下的唯一可达分支，
    也是 T-22 的后端半支。"""
    lam, flux, sg = _voigt_spec()
    with pytest.raises(LN.LineError) as ei:
        LN.measure_line(lam, flux, sg, 6500.0, 'emission', profile='voigt')
    assert ei.value.code == 'E-14'
    assert ei.value.details['reason'] == 'voigt_disabled_no_r'
    assert '观测宽度' in ei.value.reason and 'TXT-8' in ei.value.reason
    assert 'F-39' in ei.value.reason


def test_voigt_with_r_recovers_line():
    """解锁态（合成 R，fixture 而非库内数据）：中心/宽度/幅值回收；宽度仍报
    观测宽度（F-39：无去卷积——TXT-8 脚注键仍在）；θ 每成分 4 参（w+g）。"""
    lam, flux, sg = _voigt_spec()
    res = LN.measure_line(lam, flux, sg, 6500.0, 'emission', profile='voigt',
                          r_resolution=5000.0, n_boot=0)
    assert res['profile'] == 'voigt'
    assert abs(res['lambda_obs_vac_aa'] - 6500.0) < 0.5
    fwhm_true = 6500.0 * LN._voigt_fwhm_u(4e-4, 2e-4)
    assert abs(res['fwhm_obs_aa'] - fwhm_true) / fwhm_true < 0.1
    assert set(res['params']) >= {'A0', 'u00', 'w0', 'g0'}
    assert res['width_note'] == LN._TXT8            # 宽度仍是观测宽度（F-39）


# ═══ P3c · sky_subtract（F-86）：两态 + 只读 ═════════════════════════════════

def _sky_win(lam_center=5500.0, half=60.0):
    win = LN.select_window(_lam(4900.0, 6200.0, 2600), lam_center, 'emission',
                           half_width=half)
    return win


def _sky_spec(with_sky=True, sky_at_sci=False, seed=SEED):
    lam = _lam(4900.0, 6200.0, 2600)
    rng = np.random.default_rng(seed)
    sci_c = 5577.0 if sky_at_sci else 5500.0
    sci = 3e-15 * np.exp(-0.5 * ((lam - sci_c) / 12.0) ** 2)
    sky = (8e-15 * np.exp(-4 * math.log(2) * ((lam - 5577.3) / 2.2) ** 2)
           if with_sky else 0.0)
    flux = 1e-14 + sci + sky + rng.normal(0, 2e-17, lam.size)
    return lam, flux, np.full(lam.size, 2e-17) * 1.5


def test_sky_subtract_success_recovers_ew():
    """成功支：合成 [O I] 5577 天光线被扣除（RMS 比 < C_SKY_RESID_FLOOR）、
    被扣段脱 mask、科学线 EW 回收（对照 mask 路径与解析真值）。"""
    lam, flux, sg = _sky_spec()
    win = LN.select_window(lam, 5500.0, 'emission', half_width=60.0)
    flux_out, win_new, info = LN.sky_subtract(lam, flux, sg, win)
    assert info['sky_subtracted'] and not info['reverted']
    assert info['rms_ratio'] < C_SKY_RESID_FLOOR
    assert info['n_sky_lines'] == 1
    kept = {s['reason'] for s in win['excluded'] if '天光发射' in s['reason']}
    left = {s['reason'] for s in win_new['excluded'] if '天光发射' in s['reason']}
    assert left < kept                                   # 被扣段脱 mask
    # EW 对照：扣除路径回收解析真值 amp·√(2π)·σ/C0 = 9.02 Å
    res_s = LN.measure_line(lam, flux, sg, 5500.0, 'emission', half_width=60.0,
                            sky_handling='subtract', n_boot=0)
    res_m = LN.measure_line(lam, flux, sg, 5500.0, 'emission', half_width=60.0,
                            sky_handling='mask', n_boot=0)
    assert abs(res_s['ew_obs_aa'] - 9.02) < 0.5
    assert res_s['sky_subtract']['sky_subtracted'] is True
    assert abs(res_s['ew_obs_aa'] - 9.02) <= abs(res_m['ew_obs_aa'] - 9.02) + 0.2


def test_sky_subtract_no_skyline_reverts_ca37():
    """T-51 末支：无人造天光线的谱开 subtract ⇒ 无扣除对象 ⇒ 退回 mask +
    reverted=True（CA-37 由 measure_line 调用方挂），不得称「已扣除」。"""
    lam, flux, sg = _sky_spec(with_sky=False)
    win = LN.select_window(lam, 5500.0, 'emission', half_width=60.0)
    flux_out, win_new, info = LN.sky_subtract(lam, flux, sg, win)
    assert info['reverted'] and not info['sky_subtracted']
    assert np.array_equal(flux_out, flux)                # 原样退回
    res = LN.measure_line(lam, flux, sg, 5500.0, 'emission', half_width=60.0,
                          sky_handling='subtract', n_boot=0)
    codes = [w['code'] for w in res['warnings']]
    assert 'CA-37' in codes
    assert any('不得称' in w['message'] and 'CA-37' in w['message']
               for w in res['warnings'] if w['code'] == 'CA-37')


def test_sky_subtract_sci_line_coincident_kept_masked():
    """科学线心与 [O I] 名义中心 <5 Å（源/天光同位不可分）⇒ 该段保守保留
    mask，不把科学线吸进扣除模型（F-86 末句：禁用扣除结果反推源的线流量）。"""
    lam, flux, sg = _sky_spec(sky_at_sci=True)
    win = LN.select_window(lam, 5577.0, 'emission', half_width=60.0)
    _f, _w, info = LN.sky_subtract(lam, flux, sg, win)
    assert info['reverted'] and not info['sky_subtracted']


def test_sky_subtract_readonly_inputs():
    """RO 纪律：sky_subtract 不修改输入数组（成功支）。"""
    lam, flux, sg = _sky_spec()
    win = LN.select_window(lam, 5500.0, 'emission', half_width=60.0)
    f0, w0 = flux.copy(), {k: (list(v) if isinstance(v, list) else v)
                           for k, v in win.items()}
    LN.sky_subtract(lam, flux, sg, win)
    assert np.array_equal(flux, f0)
    assert all(s in w0['excluded'] for s in win['excluded'])


def test_abs_band_never_subtracted():
    """F-72①：大气吸收带段不在 sky_subtract 通路——线窗含 abs 段（无 emis 段）
    时无扣除对象退回 mask，且 abs 段原样保留在 excluded（措辞不动）。"""
    lam = _lam(6600.0, 7000.0, 800)                     # 窗 [6710,6890] 含 O₂ B 带
    flux = np.full(lam.size, 1e-14)
    sg = np.full(lam.size, 2e-17)
    win = LN.select_window(lam, 6800.0, 'emission', half_width=90.0)
    assert any('大气吸收' in s['reason'] for s in win['excluded'])
    _f, win_new, info = LN.sky_subtract(lam, flux, sg, win)
    assert info['reverted'] and not info['sky_subtracted']
    assert any('大气吸收' in s['reason'] for s in win_new['excluded'])
    assert not any('天光发射' in s['reason'] for s in win_new['excluded'])
