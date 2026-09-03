"""坐标解析：接受十进制度或时分秒（HMS/DMS）格式，统一输出度（float）。

约定：
- 纯数字（int/float/数字字符串）一律按十进制度处理；
- 含 h/m/s、d/m/s、冒号或空格分隔的串按时分秒解析（RA 以小时计，Dec 以度计）；
- RA 结果归一化到 [0, 360)；Dec 必须在 [-90, 90]；
- 空值返回 None；非法输入抛 ValueError（路由层转 400）。
"""
import re

from astropy.coordinates import Angle
import astropy.units as u

_NUM_RE = re.compile(r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$')


def _parse(value, unit, name):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    if _NUM_RE.match(s):
        return float(s)
    try:
        return float(Angle(s, unit=unit).degree)
    except Exception:
        raise ValueError(
            f'{name} 无法解析: {value!r}（支持十进制度或时分秒格式，'
            f'如 12h34m56.7s / 12:34:56.7 / 12 34 56.7）')


def parse_ra(value):
    """RA → 度 [0, 360)。纯数字按度；时分秒按小时角。"""
    v = _parse(value, u.hourangle, 'RA')
    return None if v is None else v % 360.0


def parse_dec(value):
    """Dec → 度 [-90, 90]。纯数字与时分秒均按度。"""
    v = _parse(value, u.deg, 'Dec')
    if v is None:
        return None
    if not (-90.0 <= v <= 90.0):
        raise ValueError(f'Dec 超出 [-90, 90] 度范围: {v}')
    return v
