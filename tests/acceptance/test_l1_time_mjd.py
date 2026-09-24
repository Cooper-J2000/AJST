"""L1: time ↔ MJD 写入联动（routes/lightcurves.py::_sync_time_mjd）。

契约：
  - body 里有 mjd：写 mjd；源有 T0 时用 mjd 重算 time；无 T0 时 time 保持原值/留空
  - body 里只有 time：源有 T0 时用 time 重算 mjd；无 T0 则 mjd = None
  - 两者都没给且 creating=True → ValueError；creating=False 时静默不动

背景（v2.27）：mjd 是权威时间列，time 是画图缓存列。无 T0 的源允许只录
mjd，time 留 NULL（这类点只在自定义基准下可画）。库内 2794 源目前全部有 T0，
"无 T0" 分支属防御性支持。
"""
import pytest

from routes.lightcurves import _sync_time_mjd

T0 = 60000.0


def test_mjd_recomputes_time(mk_lc):
    lc = mk_lc()
    _sync_time_mjd(T0, lc, {'mjd': 60001.0})
    assert lc.mjd == pytest.approx(60001.0)
    assert lc.time == pytest.approx(86400.0)


def test_time_recomputes_mjd(mk_lc):
    """调用方已把 body['time'] 写进 lc.time，本函数据此反算 mjd"""
    lc = mk_lc(time=3600.0)
    _sync_time_mjd(T0, lc, {'time': 3600.0})
    assert lc.mjd == pytest.approx(T0 + 3600.0 / 86400.0)


def test_no_t0_mjd_only_leaves_time_null(mk_lc):
    """源无 T0 时允许只给 mjd 入库，time 留 NULL"""
    lc = mk_lc()
    _sync_time_mjd(None, lc, {'mjd': 60001.0})
    assert lc.mjd == pytest.approx(60001.0)
    assert lc.time is None


def test_no_t0_time_only_gives_null_mjd(mk_lc):
    lc = mk_lc(time=1234.0)
    _sync_time_mjd(None, lc, {'time': 1234.0})
    assert lc.mjd is None
    assert lc.time == pytest.approx(1234.0)      # time 不被本函数改写


def test_no_t0_mjd_only_does_not_touch_existing_time(mk_lc):
    """无 T0 且只给 mjd 时，PUT 已有的 time 保持不动（不重算）"""
    lc = mk_lc(time=999.0)
    _sync_time_mjd(None, lc, {'mjd': 60001.0})
    assert lc.time == pytest.approx(999.0)


def test_explicit_null_mjd_clears_without_touching_time(mk_lc):
    lc = mk_lc(time=500.0, mjd=60000.5)
    _sync_time_mjd(T0, lc, {'mjd': None})
    assert lc.mjd is None
    assert lc.time == pytest.approx(500.0)


def test_empty_string_mjd_is_treated_as_null(mk_lc):
    lc = mk_lc(time=500.0)
    _sync_time_mjd(T0, lc, {'mjd': ''})
    assert lc.mjd is None


def test_creating_requires_at_least_one(mk_lc):
    with pytest.raises(ValueError, match='至少提供其一'):
        _sync_time_mjd(T0, mk_lc(), {}, creating=True)


def test_creating_accepts_mjd_only(mk_lc):
    lc = mk_lc()
    _sync_time_mjd(None, lc, {'mjd': 60001.0}, creating=True)     # 不应抛
    assert lc.mjd == pytest.approx(60001.0)


def test_creating_accepts_time_only(mk_lc):
    lc = mk_lc(time=600.0)
    _sync_time_mjd(T0, lc, {'time': 600.0}, creating=True)
    assert lc.mjd == pytest.approx(T0 + 600.0 / 86400.0)


def test_update_with_empty_body_is_a_noop(mk_lc):
    lc = mk_lc(time=500.0, mjd=60000.1)
    _sync_time_mjd(T0, lc, {})
    assert lc.time == pytest.approx(500.0)
    assert lc.mjd == pytest.approx(60000.1)


def test_round_trip_mjd_time_mjd(mk_lc):
    """同一 T0 下 mjd → time → mjd 必须闭合"""
    a = mk_lc()
    _sync_time_mjd(T0, a, {'mjd': 60123.456})
    b = mk_lc(time=a.time)
    _sync_time_mjd(T0, b, {'time': a.time})
    assert b.mjd == pytest.approx(60123.456, abs=1e-9)
