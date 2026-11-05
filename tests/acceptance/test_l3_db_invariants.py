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
#   6.（已消解）原「16 行 '1keV' 标 gext_corr 却无改正值」随 2026-11-06
#      gext_corr 语义重做消解：y 只表示录入权威值已改正，本库永不计算、
#      派生列恒空是定义而非缺陷；新不变量 A/B（见文末）钉住该口径。
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


# ── gext_corr 新语义（维护者 2026-11-06 裁定）──────────────────────────────
# gext_corr 只描述**录入的权威数值**是否已做银河系消光改正：
#   n/false = 录入值未改正，本库引擎可计算，产物写派生列（flag 保持 n 不变）；
#   y = 录入权威值已改正（原文/来源已改正），本库永不计算，派生列恒空，
#       且必须有 reference 或 comment 留证。
# 「本库是否算过」由派生列（gext_Alambda / mag_gextcor* / flux_density_gextcor*）
# 是否存在体现，不由 flag 体现。

def test_gext_corr_true_rows_have_no_derived_values(scalar):
    """新不变量 A：gext_corr=true（录入权威值已改正）⇒ 派生列全空——
    y 行本库永不计算，引擎/自动重算/单点 API 全跳过"""
    n = scalar("""SELECT count(*) FROM lightcurves
                  WHERE gext_corr IS TRUE
                    AND (mag_gextcor IS NOT NULL
                         OR flux_density_gextcor IS NOT NULL)""")
    assert n == 0, f'{n} 行标 gext_corr=true 却带本库派生改正值（y 行永不计算）'


def test_gext_corr_true_rows_have_provenance(scalar):
    """新不变量 B：gext_corr=true ⇒ reference 非空或 comment 非空（留证）"""
    n = scalar("""SELECT count(*) FROM lightcurves
                  WHERE gext_corr IS TRUE
                    AND (reference IS NULL OR btrim(reference) = '')
                    AND (comment   IS NULL OR btrim(comment)   = '')""")
    assert n == 0, f'{n} 行标 gext_corr=true 却无 reference/comment 留证'


def test_engine_derived_rows_are_complete(scalar):
    """新不变量 C（引擎产物完整性）：光学窗口内（wavelength 1000–1e7 Å）
    gext_corr 非真且 flux_density_gextcor 非空的行，gext_Alambda 与
    flux_density_gextcor_unit 也必须非空（correct_point 三件套同写同清）"""
    n = scalar("""SELECT count(*) FROM lightcurves lc
                  JOIN filters f ON f.id = lc.band
                  WHERE lc.gext_corr IS NOT TRUE
                    AND lc.flux_density_gextcor IS NOT NULL
                    AND f.wavelength >= 1000 AND f.wavelength <= 1e7
                    AND (lc."gext_Alambda" IS NULL
                         OR lc.flux_density_gextcor_unit IS NULL)""")
    assert n == 0, f'{n} 行有引擎改正流量却缺 gext_Alambda/单位（产物不完整）'


def test_gext_corr_true_row_count_snapshot(scalar):
    """快照钉：gext_corr=true（录入权威值已改正）行数恒为 84。

    口径：lightcurves 全表 gext_corr IS TRUE（2026-11-06 语义重做后实测；
    84 行全部满足上面不变量 A/B）。该 flag 只由人工审核的数据录入置位，
    引擎/自动重算永不触碰，故行数应稳定——变了说明有录入或迁移改动，
    需人工核对新口径后再更新本钉。"""
    n = scalar('SELECT count(*) FROM lightcurves WHERE gext_corr IS TRUE')
    assert n == 84, (f'gext_corr=true 行数从快照值 84 变为 {n}：'
                     '该 flag 只由录入决定，变动需人工核对口径')
