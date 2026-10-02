"""L1: specphot 常量表（02 §7.1/7.2/7.7）与注册表（M-1）。

覆盖条款：
  T-21  空气↔真空换算锚点（一律调宿主 wavconvert，不自写公式）
  T-39  常量单调性与自洽（本切片已落地常量的子集；其余随各自切片补）
  T-44  与宿主同值的常量（MIN_POINTS / WL_MIN / WL_MAX）
  M-1   registry：29 条有曲线波段全部登记 curve_kind，现网一律 'transmission'
"""
import math
import re

import pytest

import wavconvert
from specphot import constants as C
from specphot import registry


# ─── T-21：空气/真空（wavconvert 锚点，容差按规格） ────────────────────

def test_t21_vacuum_to_air_6300_anchor():
    """6300.21 Å(vac) ⇒ 6298.468 Å(air)，偏移 1.742 Å ≡ 82.9 km/s，容差 0.02 Å"""
    air = wavconvert.vacuum_to_air(6300.21)
    assert air == pytest.approx(6298.468, abs=0.02)
    assert 6300.21 - air == pytest.approx(1.742, abs=0.02)


def test_t21_shift_anchors_3200_and_8000():
    """按 Morton 式实算偏移：0.9247 Å@3200 → 2.1998 Å@8000（规格引值为四位舍入，
    实算 0.924887 / 2.200387；锚点容差 1e-3，另钉宿主实算值防漂移）"""
    assert wavconvert.air_to_vacuum(3200.0) - 3200.0 == pytest.approx(0.9247, abs=1e-3)
    assert wavconvert.air_to_vacuum(8000.0) - 8000.0 == pytest.approx(2.1998, abs=1e-3)
    assert wavconvert.air_to_vacuum(3200.0) - 3200.0 == pytest.approx(0.9248867142018753, abs=1e-9)
    assert wavconvert.air_to_vacuum(8000.0) - 8000.0 == pytest.approx(2.2003871454162436, abs=1e-9)


@pytest.mark.parametrize('wl', [3200.0, 5500.0, 6300.21, 8000.0, 25000.0])
def test_t21_round_trip_below_1e_minus_6(wl):
    """往返残差 < 1e-6 Å。注意 2000.0 边界不对称（vacuum_to_air(2000) < 2000
    后回程不转换），不在往返语料内。"""
    assert wavconvert.air_to_vacuum(wavconvert.vacuum_to_air(wl)) == pytest.approx(wl, abs=1e-6)
    assert wavconvert.vacuum_to_air(wavconvert.air_to_vacuum(wl)) == pytest.approx(wl, abs=1e-6)


@pytest.mark.parametrize('wl', [1000.0, 1500.0, 1999.9])
def test_t21_below_2000_passthrough_both_ways(wl):
    assert wavconvert.air_to_vacuum(wl) == wl
    assert wavconvert.vacuum_to_air(wl) == wl


def test_t21_convert_window_constant():
    assert wavconvert.CONVERT_MIN_A == 2000.0


# ─── T-39（本切片子集）：常量单调性与自洽 ──────────────────────────────

def test_t39_overlap_ordering():
    assert C.C_OVERLAP_REJECT < C.C_OVERLAP_MIN


def test_t39_pixel_bounds():
    assert C.C_MIN_PIXELS <= C.C_MAX_SPECPOINTS
    assert C.C_MIN_PIXELS == 8
    assert C.C_MAX_SPECPOINTS == 20000


def test_t39_nfev_matches_host():
    """C_NFEV_MAX 与宿主 lightcurves 的 max_nfev=20000 同值（源码级核对）"""
    assert C.C_NFEV_MAX == 20000
    import routes.lightcurves as host_lc
    src = open(host_lc.__file__, encoding='utf-8').read()
    m = re.search(r'max_nfev=(\d+)', src)
    assert m and int(m.group(1)) == C.C_NFEV_MAX


def test_t39_max_starts():
    assert C.C_MAX_STARTS == 4


def test_t39_diff_step_is_sqrt_eps_and_below_fd_rel():
    assert C.C_DIFF_STEP == pytest.approx(1.4901161193847656e-08, abs=1e-16)
    assert C.C_DIFF_STEP == pytest.approx(math.sqrt(2.220446049250313e-16), abs=1e-16)
    assert C.C_DIFF_STEP < C.C_FD_REL


def test_t39_upload_gates():
    assert C.C_MAX_UPLOAD_BYTES == 2_000_000
    assert C.C_MAX_BODY_MB == 32
    assert C.C_MIN_UPLOAD_POINTS == 10


def test_t39_gap_and_flux_band():
    assert C.C_GAP_FACTOR == 5.0
    assert C.C_FLUX_MEDIAN_MAX_CGS == 1e-13


# ─── T-39（切片 3 余项）：§7.3/§7.4 常量自洽 ──────────────────────────

def test_t39_mag_sanity_ordering():
    assert C.C_MAG_SANITY_LO < C.C_MAG_SANITY_HI
    assert (C.C_MAG_SANITY_LO, C.C_MAG_SANITY_HI, C.C_MAG_SANITY_WINDOW) \
        == (12.0, 27.0, 3.0)


def test_t39_dt_tol_fallback_below_table_min():
    assert C.C_DT_TOL_D <= min(C.C_DT_TOL_TABLE.values())
    assert C.C_DT_TOL_TABLE == {'grb_early': 1.0, 'grb_afterglow': 3.0,
                                'sn_platform': 10.0, 'other': 3.0}
    assert C.C_DT_TOL_D == 0.5
    assert C.C_DT_INTERP_D == 15.0
    assert C.C_EXT_BANDAVG_EPS == 0.005


def test_t39_sigma_consistency_factors():
    """C_SCALE_2ND_DIFF = C_MAD_SCALE/√6（1e-7）；就地可导出（1/Φ⁻¹(0.75) 与核方差 √6）"""
    assert C.C_SCALE_2ND_DIFF == pytest.approx(C.C_MAD_SCALE / math.sqrt(6.0),
                                             rel=1e-7)
    assert C.C_SCALE_2ND_DIFF == 0.6052698
    assert C.C_MAD_SCALE == 1.4826022
    assert C.C_MAD_WIN == 51
    assert C.C_SIGMA_STRIDE >= 2
    assert C.C_RHO_MIN == 0.2


def test_t39_cond_max_shared():
    assert C.C_COND_MAX == 1e8


def test_st_lam_const_is_one_step_derivable():
    """C_ST_LAM_CONST ≡ 21.10 − 5·log10(5556)（机器精度 diff=0，T-52 断言值的载体）"""
    assert C.C_ST_LAM_CONST == pytest.approx(
        C.C_ST_ZERO - 5.0 * math.log10(5556.0), abs=1e-12)
    # 误标 ST 的恒定偏差（显示 0.0318）：全精度载体 = 两常量之差
    assert C.C_AB_LAM_ZERO - C.C_ST_LAM_CONST == pytest.approx(0.03175943, abs=1e-8)


def test_zero_point_full_precision_values():
    """§7.1 全精度载体（显示值代入 T-30/T-52 必红 ⇒ 这里钉死字面量）"""
    assert C.C_AB_ZERO == 48.60
    assert C.C_AB_MJY_ZERO == 16.4
    assert C.C_AB_LAM_ZERO == 2.4079482426801846
    assert C.C_ST_ZERO == 21.10
    assert C.C_ST_LAM_CONST == 2.376188814672112
    assert C.C_AA_PER_S == 2.99792458e18
    # C_AB_LAM_ZERO = 48.60 − 2.5·log10(c[Å/s])（一步可复算）
    assert C.C_AB_LAM_ZERO == pytest.approx(48.60 - 2.5 * math.log10(C.C_AA_PER_S),
                                            rel=1e-12)


# ─── T-44（常量部分）：与宿主同值 ─────────────────────────────────────

def test_t44_constants_match_host():
    from routes.spectra import MIN_POINTS, WL_MIN, WL_MAX
    assert C.C_MIN_UPLOAD_POINTS == MIN_POINTS
    from specphot import reader
    assert (reader.WL_MIN, reader.WL_MAX) == (WL_MIN, WL_MAX)


# ─── M-1：registry ────────────────────────────────────────────────────

# 2026-09-30 只读查询生产库：filters 共 81 行，29 行 extra_data 含 transmission
GOLDEN_CURVE_BANDS = frozenset([
    'fuv', 'nuv', 'uvot-uvw2', 'uvot-uvm2', 'uvot-uvw1', 'uvot-u', 'uvot-b', 'uvot-v',
    'U', 'B', 'V', 'R', 'I', 'u', 'g', 'r', 'i', 'z', 'J', 'H', 'Ks',
    'wise-w1', 'wise-w2', 'wise-w3', 'wise-w4',
    'Spitzer-IRAC.I1', 'Spitzer-IRAC.I2', 'Spitzer-IRAC.I3', 'Spitzer-IRAC.I4',
])


def test_m1_registry_covers_exactly_the_29_curve_bands():
    assert frozenset(registry.registered_filter_ids()) == GOLDEN_CURVE_BANDS
    assert len(registry.CURVE_REGISTRY) == 29


def test_m1_all_entries_are_transmission_with_source_note():
    """M-1 判定：三条来源分支（pcigale 自带/用户上传/SVO）解析的都只是透过率"""
    for fid, entry in registry.CURVE_REGISTRY.items():
        assert entry['curve_kind'] == 'transmission', fid
        assert entry['source_note'], fid


def test_m1_query_interface():
    assert registry.has_curve('V') is True
    assert registry.has_curve('no-such-band') is False
    assert registry.curve_kind('V') == 'transmission'
    assert registry.curve_kind('no-such-band') is None
    assert registry.source_note('V')
    assert registry.source_note('no-such-band') is None
