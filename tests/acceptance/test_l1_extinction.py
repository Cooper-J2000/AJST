"""L1: P92 消光口径（k_λ = Rv·P92(λ)，Rv=3.1）与光学窗口护栏。

口径（AGENTS.md §5 表 + docs/ops-history/extinction-and-caches.md）：银河系消光改正统一为 CSFD E(B-V) + Pei(1992) 消光律 +
Rv=3.1；A_λ = Rv·P92(λ)·E(B-V)。改正只算不写，读路径对 NULL 一律回退现算。

参照实现：dust_extinction.shapes.P92 —— 生产代码就是调它，这里独立调一次做
交叉验证（防的是 RV 改了、波长单位传错、缓存串号这类改动）。
"""
import pytest

import astropy.units as u
from dust_extinction.shapes import P92

import extinction as ex

RV = 3.1


def _ref_coeff(wl_a):
    """独立参照：直接调 dust_extinction 的 P92 形状函数"""
    return float(RV * P92()(wl_a * u.AA))


# ── k_λ = Rv·P92(λ) ────────────────────────────────────────────────────

def test_rv_is_3_1():
    assert ex.RV == RV


@pytest.mark.parametrize('wl', [1000.0, 3000.0, 5500.0, 6564.6, 12000.0, 22000.0])
def test_dust_coeff_matches_p92(wl):
    assert ex.dust_coeff(wl) == pytest.approx(_ref_coeff(wl), rel=1e-9)


def test_dust_coeff_is_cached_but_stable():
    """按波长缓存，重复调用必须给同一个值"""
    a = ex.dust_coeff(4400.0)
    b = ex.dust_coeff(4400.0)
    assert a == b


def test_p92_range_matches_the_model():
    lo, hi = ex.p92_wavelength_range()
    x = P92().x_range
    assert lo == pytest.approx(1e4 / float(x[1]))
    assert hi == pytest.approx(1e4 / float(x[0]))


def test_dust_coeff_zero_outside_p92_domain():
    """域外（射电/X 射线）尘埃消光可忽略，系数取 0 = 不做改正"""
    _, hi = ex.p92_wavelength_range()
    assert ex.dust_coeff(hi * 1.0001) == 0.0     # 长于上限（射电）
    assert ex.dust_coeff(1e9) == 0.0
    assert ex.dust_coeff(1.0) == 0.0             # 短于下限（X 射线）


@pytest.mark.parametrize('bad', [None, 0, -1.0, -5500.0, 'abc', '', []])
def test_dust_coeff_none_when_wavelength_unusable(bad):
    assert ex.dust_coeff(bad) is None


# ── A_λ = k·E(B-V) ─────────────────────────────────────────────────────

def test_compute_alambda_is_linear_in_ebv():
    ebv = 0.123
    k = ex.dust_coeff(5500.0)
    assert ex.compute_alambda(ebv, 5500.0) == pytest.approx(k * ebv, rel=1e-12)


def test_compute_alambda_prefers_persisted_coeff():
    """给了持久化的 filters.gext_coeff 时不应再去求 P92"""
    assert ex.compute_alambda(0.2, None, coeff=3.0) == pytest.approx(0.6)
    assert ex.compute_alambda(0.2, 5500.0, coeff=3.0) == pytest.approx(0.6)


def test_compute_alambda_none_when_no_coeff():
    assert ex.compute_alambda(0.1, None) is None


# ── 光学窗口护栏（v2.20） ──────────────────────────────────────────────

def test_optical_window_constants():
    assert ex.OPTICAL_WL_MIN_A == 1000.0
    assert ex.OPTICAL_WL_MAX_A == 1.0e7


def test_optical_window_guard_bounds():
    """窗口外一律 None（超窗波段曾导致 500）"""
    assert ex._optical_alambda(999.0, 0.1) is None
    assert ex._optical_alambda(1000.0, 0.1) is not None       # 含端点
    assert ex._optical_alambda(1.0e7, 0.1) is not None        # 含端点
    assert ex._optical_alambda(1.0001e7, 0.1) is None


@pytest.mark.parametrize('bad', [None, 0, -100.0, 'x'])
def test_optical_window_guard_rejects_invalid(bad):
    assert ex._optical_alambda(bad, 0.1) is None


def test_optical_alambda_agrees_with_compute_alambda_inside_window():
    assert ex._optical_alambda(5500.0, 0.07) == pytest.approx(ex.compute_alambda(0.07, 5500.0))


# ── 单点改正口径 ───────────────────────────────────────────────────────

def test_correct_point_subtracts_alambda(mk_lc):
    lc = mk_lc(flux_density=20.0, flux_density_unit='magnitude', mag_system='ab')
    assert ex.correct_point(lc, alambda=0.5) is True
    assert lc.mag_gextcor == pytest.approx(19.5)


def test_correct_point_applies_vega_to_ab_only_for_vega(mk_lc):
    """Vega 星等先加 vega2ab 转 AB，再减 A_λ；AB 星等不受 vega2ab 影响"""
    vega = mk_lc(flux_density=20.0, flux_density_unit='magnitude', mag_system='vega')
    ex.correct_point(vega, alambda=0.5, vega2ab=0.3)
    assert vega.mag_gextcor == pytest.approx(20.0 + 0.3 - 0.5)

    ab = mk_lc(flux_density=20.0, flux_density_unit='magnitude', mag_system='ab')
    ex.correct_point(ab, alambda=0.5, vega2ab=0.3)
    assert ab.mag_gextcor == pytest.approx(19.5)


def test_correct_point_records_provenance(mk_lc):
    lc = mk_lc(flux_density=20.0, flux_density_unit='magnitude')
    ex.correct_point(lc, alambda=0.25)
    assert lc.gext_corr is True
    assert lc.gext_Alambda == pytest.approx(0.25)


def test_correct_point_returns_false_when_unusable(mk_lc):
    assert ex.correct_point(mk_lc(flux_density=None), 0.1) is False
    assert ex.correct_point(mk_lc(flux_density=1.0, flux_density_unit='bogus'), 0.1) is False


def test_clear_point_resets_correction(mk_lc):
    lc = mk_lc(flux_density=20.0, flux_density_unit='magnitude')
    ex.correct_point(lc, alambda=0.25)
    ex.clear_point(lc)
    assert lc.gext_corr is False
    assert lc.gext_Alambda is None
    assert lc.mag_gextcor is None
