"""L1: 伪玻尔兹曼光变（sedfit/bolometric.py）的常量与系数表结构。

⚠ 范围说明：Lyman, Bersier & James 2014 (arXiv:1311.1946) Eqs. 3–4 / Tables
2–4 的 BC 多项式**系数逐条未对照原文复核**，因此本文件不断言 BC 数值，只锁：
  - 全局 AB 零点一致（16.4，与 extinction / fitting 同源）
  - 系数表结构与色-波段对应关系（防转录/改键时的静默错位）
  - 出处引用仍指向该文
BC 数值的复核属"加强项"，需要时按原文逐条比对后再补断言。
"""
import pytest

from sedfit import bolometric as b

SUBSAMPLES = ('all', 'se', 'ii', 'cooling')


def test_ab_zeropoint_matches_the_global_convention():
    assert b._AB_ZP_MJY == 16.4


def test_color_priority_unchanged():
    assert b._BC_COLOR_PRIORITY == ('B-I', 'B-V', 'g-r')


def test_reference_still_points_to_lyman_2014():
    assert 'Lyman' in b._BC_REF and '1311.1946' in b._BC_REF


def test_all_subsamples_present():
    assert set(b._BC_FITS) == set(SUBSAMPLES)


@pytest.mark.parametrize('sample', SUBSAMPLES)
def test_each_subsample_has_only_known_colors(sample):
    assert set(b._BC_FITS[sample]) <= set(b._BC_COLOR_PRIORITY)


@pytest.mark.parametrize('sample', SUBSAMPLES)
def test_each_entry_shape(sample):
    """每条：('前置波段', c0, c1, c2, c3, 颜色下限, 颜色上限)，键是 'X-Y'"""
    for color, entry in b._BC_FITS[sample].items():
        assert len(entry) == 7, f'{sample}/{color} 元组长度 {len(entry)} ≠ 7'
        band = entry[0]
        assert band == color.split('-')[0], \
            f'{sample}/{color} 的前置波段 {band} 与色名不符'
        assert isinstance(band, str)
        for c in entry[1:5]:
            assert isinstance(c, float), f'{sample}/{color} 系数不是浮点数: {c!r}'
        lo, hi = entry[5], entry[6]
        assert lo < hi, f'{sample}/{color} 颜色范围倒置: {lo} >= {hi}'


def test_color_range_bounds_are_sane():
    """Lyman+2014 的拟合范围大致在 [-0.5, 3.0] mag，越界说明抽录出错"""
    for sample, entries in b._BC_FITS.items():
        for color, entry in entries.items():
            assert -0.5 <= entry[5] < entry[6] <= 3.0, f'{sample}/{color}: {entry[5]}..{entry[6]}'
