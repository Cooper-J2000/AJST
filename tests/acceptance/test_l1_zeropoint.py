"""L1: 星等 ↔ 流量 零点口径（跨模块一致性）。

AJST 全局唯一约定：**AB 零点 16.4（mJy 制）**。历史上 hostfit 出过 1000 倍
零点错误（Jy 制 8.90 → mJy 制 16.4，见 docs/ops-history/code-review-2026-09.md），本文件
把这条口径锁死，任何一处漂移都会在这里失败。

两个模块用两种写法表达同一个恒等式（数学上完全等价，差值仅来自 3.631 的
四位有效数字，相对偏差 ~2e-6）：
    extinction.correct_point :  flux_mJy = 3.631 * 10**(6 - mag/2.5)
    fitting/jobs.py          :  flux_mJy = 10**((16.4 - mag)/2.5)
"""
import math

import pytest

import extinction
from sedfit import bolometric

AB_ZP = 16.4


# ── 零点本身 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('mag', [10.0, 16.4, 20.0, 22.5, 25.0])
def test_zero_point_twenty_six_four_is_one_mjy(mk_lc, mag):
    """flux_mJy = 10**((16.4 - mag)/2.5)，cross-check 用 extinction 的实现"""
    lc = mk_lc(flux_density=mag, flux_density_unit='magnitude', mag_system='ab')
    assert extinction.correct_point(lc, alambda=0.0) is True
    assert lc.flux_density_gextcor == pytest.approx(10 ** ((AB_ZP - mag) / 2.5), rel=1e-4)
    assert lc.flux_density_gextcor_unit == 'mJy'
    assert lc.mag_gextcor == pytest.approx(mag, abs=1e-12)


def test_mag_16_4_is_exactly_1_mjy(mk_lc):
    lc = mk_lc(flux_density=AB_ZP, flux_density_unit='magnitude')
    extinction.correct_point(lc, alambda=0.0)
    assert lc.flux_density_gextcor == pytest.approx(1.0, rel=1e-4)


def test_mag_zero_is_3631_jy(mk_lc):
    lc = mk_lc(flux_density=0.0, flux_density_unit='magnitude')
    extinction.correct_point(lc, alambda=0.0)
    assert lc.flux_density_gextcor == pytest.approx(3631.0e3, rel=1e-4)   # mJy


@pytest.mark.parametrize('mag', [12.0, 16.4, 19.3, 23.0, 27.5])
def test_the_two_written_forms_agree(mag):
    """3.631×10^(6−m/2.5) 与 10^((16.4−m)/2.5) 必须给出同一个流量"""
    f_ext = 3.631 * 10 ** (6 - mag / 2.5)
    f_fit = 10 ** ((AB_ZP - mag) / 2.5)
    assert f_ext == pytest.approx(f_fit, rel=1e-4)


def test_zeropoint_constant_is_shared_with_bolometric():
    """sedfit/bolometric.py 的 _AB_ZP_MJY 必须与全局口径一致"""
    assert bolometric._AB_ZP_MJY == AB_ZP


# ── 反向：流量 → 星等（obs_mag） ────────────────────────────────────────

def test_obs_mag_identity_for_magnitude_units(mk_lc):
    lc = mk_lc(flux_density=21.34, flux_density_unit='mag', flux_density_err=0.05)
    assert extinction.obs_mag(lc) == (21.34, 0.05)


def test_obs_mag_of_one_mjy_is_the_zeropoint(mk_lc):
    mag, err = extinction.obs_mag(mk_lc(flux_density=1.0, flux_density_unit='mJy'))
    assert mag == pytest.approx(AB_ZP, abs=1e-12)
    assert err is None


def test_obs_mag_unit_scaling_is_consistent(mk_lc):
    """1 Jy = 1000 mJy = 1e6 uJy，三种写法必须给同一星等"""
    m_jy = extinction.obs_mag(mk_lc(flux_density=2.5, flux_density_unit='jy'))[0]
    m_mjy = extinction.obs_mag(mk_lc(flux_density=2500.0, flux_density_unit='mJy'))[0]
    m_ujy = extinction.obs_mag(mk_lc(flux_density=2.5e6, flux_density_unit='uJy'))[0]
    assert m_jy == pytest.approx(m_mjy)
    assert m_mjy == pytest.approx(m_ujy)


def test_obs_mag_accepts_cgs_units(mk_lc):
    """1 mJy = 1e-26 erg/s/cm²/Hz，cgs 与 mJy 必须换算一致"""
    m_cgs = extinction.obs_mag(mk_lc(flux_density=1e-26, flux_density_unit='cgs'))[0]
    m_mjy = extinction.obs_mag(mk_lc(flux_density=1.0, flux_density_unit='mJy'))[0]
    assert m_cgs == pytest.approx(m_mjy)


def test_obs_mag_error_propagation(mk_lc):
    """σ_mag = (2.5/ln10)·σ_f/f"""
    lc = mk_lc(flux_density=0.5, flux_density_unit='mJy', flux_density_err=0.05)
    _, err = extinction.obs_mag(lc)
    assert err == pytest.approx((2.5 / math.log(10)) * 0.05 / 0.5)


@pytest.mark.parametrize('kw', [
    dict(flux_density=None),
    dict(flux_density=-1.0, flux_density_unit='mJy'),      # 流量非正
    dict(flux_density=1.0, flux_density_unit='bogus'),     # 未知单位
])
def test_obs_mag_returns_none_when_unusable(mk_lc, kw):
    assert extinction.obs_mag(mk_lc(**kw)) is None


def test_correct_point_flux_error_propagation(mk_lc):
    """改正后流量的误差：σ_f = (ln10/2.5)·f·σ_mag"""
    lc = mk_lc(flux_density=20.0, flux_density_unit='magnitude', flux_density_err=0.1)
    extinction.correct_point(lc, alambda=0.25)
    assert lc.flux_density_gextcor_err == pytest.approx(
        (math.log(10) / 2.5) * lc.flux_density_gextcor * 0.1)
