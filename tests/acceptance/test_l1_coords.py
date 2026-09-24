"""L1: 坐标解析（十进制度 ↔ 时分秒）。

契约（backend/coords.py 模块头）：
  - 纯数字（int/float/数字字符串）一律按十进制度
  - 含 h/m/s、冒号或空格分隔的串按时分秒；RA 以小时角、Dec 以度
  - RA 归一化到 [0, 360)；Dec 限 [-90, 90]
  - 空值 → None；非法 → ValueError（路由层转 400）

易错点（本文件重点盯的）：同一个 "12:34:56.7" 在 RA 下是小时角（×15），
在 Dec 下是度 —— 单位混淆会给出 15 倍偏差。
"""
import pytest

from coords import parse_dec, parse_ra


# ── RA ────────────────────────────────────────────────────────────────

def test_ra_sexagesimal_is_hour_angle():
    """12h34m56.7s = 12.5824h × 15 = 188.73625°"""
    assert parse_ra('12:34:56.7') == pytest.approx(188.73625, abs=1e-6)
    assert parse_ra('12h34m56.7s') == pytest.approx(188.73625, abs=1e-6)
    assert parse_ra('12 34 56.7') == pytest.approx(188.73625, abs=1e-6)


def test_ra_plain_number_is_degrees():
    assert parse_ra(188.73625) == pytest.approx(188.73625)
    assert parse_ra('188.73625') == pytest.approx(188.73625)
    assert parse_ra(0.0) == 0.0


def test_ra_is_normalised_into_0_360():
    assert parse_ra(-10.0) == pytest.approx(350.0)
    assert parse_ra(370.0) == pytest.approx(10.0)
    assert parse_ra(720.0) == pytest.approx(0.0)


# ── Dec ───────────────────────────────────────────────────────────────

def test_dec_sexagesimal_is_degrees():
    """+41:16:09 = 41.2691667°（不是小时角）"""
    assert parse_dec('+41:16:09') == pytest.approx(41.2691667, abs=1e-6)
    assert parse_dec('12 34 56.7') == pytest.approx(12.5824167, abs=1e-6)


def test_dec_negative():
    assert parse_dec('-05:23:28') == pytest.approx(-5.3911111, abs=1e-6)


def test_dec_limits_are_inclusive():
    assert parse_dec(90) == pytest.approx(90.0)
    assert parse_dec(-90) == pytest.approx(-90.0)
    assert parse_dec(0.0) == 0.0


@pytest.mark.parametrize('bad', [90.0001, -90.0001, 91, -180, '12h34m'])
def test_dec_out_of_range_raises(bad):
    """含小时的串按度解析会得到 >90 的数值，被范围检查拦下（报范围错）"""
    with pytest.raises(ValueError):
        parse_dec(bad)


# ── 空值与非法输入 ─────────────────────────────────────────────────────

@pytest.mark.parametrize('empty', [None, '', '   '])
def test_empty_returns_none(empty):
    assert parse_ra(empty) is None
    assert parse_dec(empty) is None


@pytest.mark.parametrize('bad', ['abc', '12:xx:34', '--5'])
def test_unparsable_raises(bad):
    with pytest.raises(ValueError):
        parse_ra(bad)
    with pytest.raises(ValueError):
        parse_dec(bad)
