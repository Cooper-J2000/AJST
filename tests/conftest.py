"""AJST 验收测试的公共装置。

把 backend/ 放进 sys.path，让测试可以用生产代码原本的导入方式
（`import extinction` / `from sedfit import closure`）—— 生产代码本身就是
以 backend/ 为工作目录运行的（见 backend/start.sh）。

这里只做路径与轻量夹具：不起服务、不联网、不主动连库。
需要 DB 的检查（L2 的计数对账、L3 的数据不变量）用下面的 db_cur 只读游标；
库连不上时夹具直接 skip，所以在没库的机器上跑不会误报。
"""
import os
import re
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BACKEND = os.path.join(_REPO_ROOT, 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


@pytest.fixture
def mk_lc():
    """构造一个最小的 Lightcurve 替身（只需要被读写的字段）。

    默认给一套"可改正的 AB 星等"值，单个字段用 kw 覆盖。
    """

    def _mk(**kw):
        base = dict(
            id=1,
            transient_id='TEST',
            # 光变点字段
            flux_density=None,
            flux_density_unit='mag',
            flux_density_err=None,
            mag_system='ab',
            # time ↔ mjd 联动字段
            time=None,
            mjd=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    return _mk


def _dsn():
    """连接串优先级（与 test_l3_db_invariants.py 一致）：
    AJST_TEST_DATABASE_URL（对快照/副本跑）→ DATABASE_URL → **生产配置的默认值**
    （backend/config.py 的 DB_URL）。测试不自带一份默认连接串，保证与后端同源：
    后端连哪个库，验收测试就连哪个库。"""
    for var in ('AJST_TEST_DATABASE_URL', 'DATABASE_URL'):
        v = os.environ.get(var)
        if v:
            return re.sub(r'^postgresql\+\w+://', 'postgresql://', v)
    from config import DB_URL                    # backend/ 已在 sys.path 上
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', DB_URL)


@pytest.fixture(scope='module')
def db_cur():
    """只读游标（会话强制 READ ONLY + 语句超时）。连不上则整模块 skip。"""
    psycopg2 = pytest.importorskip('psycopg2')
    from psycopg2.extras import RealDictCursor
    try:
        cn = psycopg2.connect(_dsn())
    except Exception as e:
        pytest.skip(f'数据库不可达（{type(e).__name__}: {e}）')
    cn.set_session(readonly=True, autocommit=True)
    cur = cn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SET statement_timeout = '180s'")
    yield cur
    cur.close()
    cn.close()
