"""L1: 波长 空气 ↔ 真空 转换（Morton 1991）。

口径（backend/wavconvert.py 模块头 + docs/ops-history/subsystem-sync-rules.md）：
  - 处理路径（API 返回、P92 求值前）一律先转真空；库存文件与下载保持原始值
  - 只对 ≥ 2000 Å 转换；非 Å 单位不转换；wavelength_type='vacuum' 不转换
  - 反向（空气→真空）用不动点迭代反解，8 轮内收敛到 1e-9 Å
"""
import pytest

import wavconvert as w

# 外部事实（不是从实现里抄的）：Hα 空气波长 6562.80 Å ↔ 真空波长 6564.61 Å
HALPHA_AIR = 6562.80
HALPHA_VAC = 6564.61


def test_halpha_air_to_vacuum_matches_known_value():
    assert w.air_to_vacuum(HALPHA_AIR) == pytest.approx(HALPHA_VAC, abs=0.05)


def test_halpha_round_trip():
    assert w.vacuum_to_air(HALPHA_VAC) == pytest.approx(HALPHA_AIR, abs=0.05)


@pytest.mark.parametrize('wl', [2000.0, 3000.0, 5000.0, 6562.8, 10000.0, 20000.0])
def test_round_trip_is_identity(wl):
    assert w.vacuum_to_air(w.air_to_vacuum(wl)) == pytest.approx(wl, abs=1e-6)


@pytest.mark.parametrize('wl', [2000.0, 4000.0, 8000.0, 20000.0])
def test_vacuum_is_longer_than_air(wl):
    """折射率 n > 1，故真空波长必大于空气波长"""
    assert w.air_to_vacuum(wl) > wl
    assert w.vacuum_to_air(wl) < wl


def test_below_2000_angstrom_is_passthrough():
    assert w.CONVERT_MIN_A == 2000.0
    assert w.air_to_vacuum(1500.0) == 1500.0
    assert w.vacuum_to_air(1999.9) == 1999.9
    assert w.air_to_vacuum(2000.0) != 2000.0      # 含下限，会转换


def test_refraction_index_is_about_1_00028_in_the_visible():
    n = w._refraction_n(1e4 / 5500.0)
    assert 1.00025 < n < 1.00030


def test_air_to_vacuum_satisfies_the_fixed_point_equation():
    """收敛判据：λ_vac / λ_air 必须等于 n(1e4/λ_vac)"""
    lam = 6562.8
    v = w.air_to_vacuum(lam)
    assert v / lam == pytest.approx(w._refraction_n(1e4 / v), abs=1e-12)


# ── 批量入口的静默路径（返回原对象 + converted=False） ──────────────────

def test_to_vacuum_wavelengths_skips_when_already_vacuum():
    wavs = [4000.0, 5000.0]
    out, converted = w.to_vacuum_wavelengths(wavs, 'vacuum')
    assert out is wavs and converted is False


def test_to_vacuum_wavelengths_skips_for_non_angstrom_unit():
    wavs = [4000.0, 5000.0]
    for unit in ('nm', 'micron', 'um'):
        out, converted = w.to_vacuum_wavelengths(wavs, 'air', u_wavelengths=unit)
        assert out is wavs and converted is False


def test_to_vacuum_wavelengths_converts_for_air_and_null():
    wavs = [4000.0, 5000.0]
    for wtype in ('air', None, ''):
        out, converted = w.to_vacuum_wavelengths(wavs, wtype)
        assert converted is True
        assert out[0] == pytest.approx(w.air_to_vacuum(4000.0))
        assert out is not wavs


def test_to_vacuum_points_only_touches_the_wavelength_column():
    pts = [[4000.0, 1.5, 0.1], [5000.0, 2.5, 0.2]]
    out, converted = w.to_vacuum_points(pts, 'air')
    assert converted is True
    assert out[0][0] == pytest.approx(w.air_to_vacuum(4000.0))
    assert out[0][1:] == [1.5, 0.1]          # 流量与误差原样
    assert out[1][1:] == [2.5, 0.2]
    assert pts[0][0] == 4000.0               # 不改输入
