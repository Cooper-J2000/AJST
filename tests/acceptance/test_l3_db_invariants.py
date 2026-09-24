"""L3: 数据库不变量（**只读**）。

这些断言检查"数据本身是否自洽"，与具体行数无关 —— 行数会变，不变量不该变。
连接失败时整模块 skip（在没库的机器上跑不会误报）。

连接串取 AJST_TEST_DATABASE_URL（优先，用于对快照/副本跑）→ DATABASE_URL
（剥掉 SQLAlchemy 的 +psycopg2 方言）→ 生产配置的默认值（backend/config.py 的 DB_URL）。

安全性：会话强制 READ ONLY，并且有一条测试专门验证"写语句会被拒绝"——
这套测试在结构上不可能改到数据。
"""
import os
import re

import pytest

psycopg2 = pytest.importorskip('psycopg2')
from psycopg2.extras import RealDictCursor  # noqa: E402

from extinction import FLUX_UNIT_TO_MJY, MAG_UNITS  # noqa: E402

# MJD(1970-01-01) = 40587；transients.t0 是 timestamp（UTC），转 MJD 用
# EXTRACT(EPOCH FROM t0)/86400 + 40587（PG 对 timestamp 取 epoch 即按 UTC 解释）
MJD_EPOCH_JD = 40587.0
TIME_TOL_S = 1.0
MJD_MIN, MJD_MAX = 40000.0, 70000.0

# ── 已知偏差：确认过是现状，因此**不写成断言**（写了会让套件长期红） ────
#   1. lightcurves.band 对 X 射线/射电行取 '10keV'/'15GHz' 这类自由文本，
#      不是 filters.id；只有 112,230 条光学星等行用 filters.id。设计如此。
#   2. magnitude 行里 mag_system 为空的有 20,552 条（涉及 2,134 个源）——
#      correct_point 对 NULL 按 AB 处理，若实际是 Vega 会差 ~0.3 mag。
#   3. 完全重复的光变行 681 组（同源同波段同历元同流量）。可能是两个文献
#      报同一个测光值（合法），也可能是重复插入。
#   4. transients 里 56 个源无 ra/dec（全部是 EP/SVOM 近期目标，位置未回填）。
#   5. 12 张表从未 ANALYZE（pg_stat_user_tables.last_analyze 全为 NULL）。
#   6. 某未发布源的 16 行 '1keV' 被标 gext_corr 却无改正值（见文末第 2 条测试，
#      已用上界断言钉住，不是忽略）。
#   以上属数据质量/运维范畴，需人判断处置，不属"自洽性"不变量。


def _dsn():
    """优先 AJST_TEST_DATABASE_URL（对快照/副本跑）→ DATABASE_URL → 生产配置的默认值
    （backend/config.py 的 DB_URL）——不在测试里另写一份默认连接串。"""
    for var in ('AJST_TEST_DATABASE_URL', 'DATABASE_URL'):
        v = os.environ.get(var)
        if v:
            return re.sub(r'^postgresql\+\w+://', 'postgresql://', v)
    from config import DB_URL
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', DB_URL)


@pytest.fixture(scope='module')
def db():
    try:
        cn = psycopg2.connect(_dsn())
    except Exception as e:      # 连不上就跳过，不当失败
        pytest.skip(f'数据库不可达（{type(e).__name__}: {e}）')
    cn.set_session(readonly=True, autocommit=True)
    with cn.cursor() as c:
        c.execute("SET statement_timeout = '180s'")
    yield cn
    cn.close()


@pytest.fixture(scope='module')
def q(db):
    def _q(sql, args=None):
        with db.cursor(cursor_factory=RealDictCursor) as c:
            c.execute(sql, args)
            return c.fetchall()
    return _q


@pytest.fixture(scope='module')
def scalar(q):
    def _s(sql, args=None):
        rows = q(sql, args)
        assert len(rows) == 1, f'期望单行，得到 {len(rows)} 行'
        return list(rows[0].values())[0]
    return _s


# ── 安全性：这套测试改不到数据 ─────────────────────────────────────────

def test_connection_is_read_only(db):
    """只读会话必须真的拦截写语句（WHERE false 不碰任何行，仅验证拦截）"""
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        with db.cursor() as c:
            c.execute("UPDATE transients SET comment = comment WHERE false")


def test_read_only_flag_is_on(scalar):
    assert scalar("SHOW default_transaction_read_only") == 'on'


# ── 表非空（"被清空"护栏） ─────────────────────────────────────────────

@pytest.mark.parametrize('table', ['transients', 'lightcurves', 'filters'])
def test_core_tables_are_not_empty(scalar, table):
    assert scalar(f'SELECT count(*) FROM {table}') > 0


# ── 引用完整性 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('child,parent_col', [
    ('lightcurves', 'transient_id'),
    ('host_galaxies', 'transient_id'),
])
def test_no_orphan_rows(scalar, child, parent_col):
    n = scalar(f'SELECT count(*) FROM {child} x '
               f'LEFT JOIN transients t ON t.id = x.{parent_col} '
               f'WHERE t.id IS NULL')
    assert n == 0, f'{child} 有 {n} 行指向不存在的 transient'


# ── 时间口径：time ↔ mjd ↔ T0 必须自洽（核心不变量） ───────────────────

def test_time_agrees_with_mjd_and_t0(scalar):
    """|time − (mjd − MJD(t0))·86400| ≤ 1 s —— 验证 _sync_time_mjd 历史写入正确。

    这是整套 L3 里最有价值的一条：_sync_time_mjd 一旦回归（比如漏算 T0、
    单位写错、只更新一列），全库的 time 列就会与 mjd 脱钩，而绘图依赖 time。
    """
    n = scalar(f"""
        SELECT count(*) FROM lightcurves lc
        JOIN transients t ON t.id = lc.transient_id
        WHERE t.t0 IS NOT NULL AND lc.mjd IS NOT NULL
          AND abs(lc.time - (lc.mjd - (EXTRACT(EPOCH FROM t.t0)/86400.0 + {MJD_EPOCH_JD}))*86400.0)
              > {TIME_TOL_S}
    """)
    assert n == 0, f'{n} 行的 time 与 mjd/T0 不自洽（容差 {TIME_TOL_S}s）'


def test_source_with_t0_always_has_mjd(scalar):
    n = scalar("""
        SELECT count(*) FROM lightcurves lc
        JOIN transients t ON t.id = lc.transient_id
        WHERE t.t0 IS NOT NULL AND lc.mjd IS NULL
    """)
    assert n == 0, f'{n} 行有 T0 却没有 mjd'


def test_mjd_values_are_in_a_plausible_range(q):
    rows = q('SELECT min(mjd) lo, max(mjd) hi FROM lightcurves WHERE mjd IS NOT NULL')
    lo, hi = rows[0]['lo'], rows[0]['hi']
    assert lo is not None
    assert MJD_MIN <= lo and hi <= MJD_MAX, f'mjd 范围 [{lo}, {hi}] 越界'


def test_lightcurves_time_is_never_null(scalar):
    assert scalar('SELECT count(*) FROM lightcurves WHERE time IS NULL') == 0


# ── filters：波长与消光系数 ────────────────────────────────────────────

def test_every_filter_has_wavelength_and_coeff(scalar):
    assert scalar('SELECT count(*) FROM filters WHERE wavelength IS NULL') == 0
    assert scalar('SELECT count(*) FROM filters WHERE gext_coeff IS NULL') == 0


def test_filter_wavelengths_are_positive(scalar):
    assert scalar('SELECT count(*) FROM filters WHERE wavelength <= 0') == 0


def test_gext_coeff_only_set_inside_the_optical_window(scalar):
    """有消光系数的滤镜波长必须落在 [1000, 1e7] Å（EXTINCTION_光学窗口）"""
    n = scalar("""SELECT count(*) FROM filters
                  WHERE gext_coeff IS NOT NULL AND (wavelength < 1000 OR wavelength > 1e7)""")
    assert n == 0, f'{n} 个滤镜系数落在光学窗口外，与 extinction 的护栏矛盾'


# ── transients：坐标范围 ───────────────────────────────────────────────

def test_coordinates_are_within_range_when_present(scalar):
    """允许缺坐标（56 个 EP/SVOM 源尚未回填），但不允许越界"""
    n = scalar("""SELECT count(*) FROM transients
                  WHERE (ra IS NOT NULL AND (ra < 0 OR ra >= 360))
                     OR (dec IS NOT NULL AND (dec < -90 OR dec > 90))""")
    assert n == 0, f'{n} 个源的坐标越界'


@pytest.mark.parametrize('col,cond', [
    ('ra',  'ra < 0 OR ra >= 360'),
    ('dec', 'dec < -90 OR dec > 90'),
])
def test_host_galaxy_coordinates_within_range(scalar, col, cond):
    n = scalar(f'SELECT count(*) FROM host_galaxies WHERE {col} IS NOT NULL AND ({cond})')
    assert n == 0, f'host_galaxies.{col} 有 {n} 行越界'


# ── 数据 ↔ 代码的接口不变量 ───────────────────────────────────────────

def test_every_flux_unit_in_the_db_is_convertible(q):
    """库里出现的流量单位必须都能被代码的换算表处理，否则 obs_mag 静默返回 None"""
    known = {u.lower() for u in FLUX_UNIT_TO_MJY} | {u.lower() for u in MAG_UNITS}
    rows = q('SELECT DISTINCT lower(flux_density_unit) u FROM lightcurves')
    unknown = {r['u'] for r in rows} - known
    assert not unknown, f'库里有代码换算表不认识的单位: {unknown}（obs_mag 会返回 None）'


def test_mag_system_values_are_recognised(q):
    """非空 mag_system 只能是 ab / vega（大小写不敏感），否则 vega2ab 会被漏加"""
    rows = q("SELECT DISTINCT lower(mag_system) m FROM lightcurves WHERE mag_system IS NOT NULL")
    bad = {r['m'] for r in rows} - {'ab', 'vega'}
    assert not bad, f'出现无法识别的测光系统: {bad}'


def test_flux_density_is_finite(q):
    """不允许 NaN/Inf（会让拟合与统计静默出错）"""
    rows = q("""SELECT count(*) FILTER (WHERE flux_density IN ('NaN'::float8, 'Infinity'::float8,
                                                             '-Infinity'::float8)) bad,
                       count(*) n FROM lightcurves""")
    assert rows[0]['bad'] == 0, f"{rows[0]['bad']} 行流量是 NaN/Inf"


def test_gext_corrected_rows_have_their_values(scalar):
    """波段落在光学窗口内（=真正可改正）的行，标了 gext_corr 就必须有改正值"""
    n = scalar("""SELECT count(*) FROM lightcurves lc
                  JOIN filters f ON f.id = lc.band
                  WHERE lc.gext_corr IS TRUE
                    AND f.wavelength >= 1000 AND f.wavelength <= 1e7
                    AND (lc.flux_density_gextcor IS NULL
                         OR lc.flux_density_gextcor_unit IS NULL)""")
    assert n == 0, f'{n} 行标为已改正却没有改正后流量/单位'


def test_known_open_issue_high_energy_rows_flagged_as_corrected(scalar):
    """已知未修（钉上界，修好后可删）：高能波段行标了 gext_corr 但无改正值。

    现状 16 行，全部是同一未发布源的 '1keV' 行（2026-08-27 导入）。
    根因：1 keV = 12.4 Å，落在 extinction 的光学窗口 [1000, 1e7] Å 之外，
    _optical_alambda() 返回 None，本来就没有可改的东西，导入侧却仍置
    gext_corr = TRUE。数值上无害（X 射线尘埃消光可忽略），但标志位撒谎，
    且 to_dict() 会对外给出 gext_corr=true + flux_density_gextcor=null 的组合。
    处置建议：导入侧对窗口外波段不置该标志（或把已有 16 行清零），二者都需人确认。
    """
    n = scalar("""SELECT count(*) FROM lightcurves lc
                  WHERE lc.gext_corr IS TRUE
                    AND lc.flux_density_gextcor IS NULL
                    AND (lc.band ~ 'keV' OR lc.band ~ 'MeV' OR lc.band ~ 'GeV'
                         OR lc.band ~ 'GHz' OR lc.band ~ 'MHz')""")
    assert n <= 16, f'标为已改正但无改正值的高能波段行从 16 涨到 {n} 行，需查导入侧'
