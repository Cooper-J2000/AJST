"""T-24 只读回归的周期级快照（session 级 autouse，02 规格 T-24）。

验收周期开始时对宿主仓库快照：catadata/ 与 backend/ 的文件数 + 逐文件
mtime_ns、DB spectra/filters/lightcurves 三表的 COUNT + MAX(id)；session 结束
（finalizer）逐项比对，任何变化 ⇒ fail 并列出差异项。禁止以 git status 作为
判据（catadata/ 被 gitignore，git status 恒为干净）。

快照与 DB 通路逻辑与 test_l2_specphot_p1_acceptance.py 的 T-47 **同源**
（_dsn/_db_snapshot/_fs_snapshot 为同一份实现，复制于此而不是 import 测试
模块，避免 conftest 在收集期牵起测试模块的导入）；DSN 解析链同 T-24 明文：
AJST_TEST_DATABASE_URL → DATABASE_URL → ajst_catalog（生产配置默认值），
事务 READ ONLY + statement_timeout，绝不写。

DB 不可达 ⇒ 记 db_snapshot_status='absent'，finalizer fail —— T-24 明文
absent 态不得宣称绿（「可脱库运行」只覆盖其余 T-*，不含本条）。
"""
import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BACKEND = os.path.join(_REPO_ROOT, 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _dsn():
    """T-24 的 DSN 链（与 T-47 的 _dsn 同源）：AJST_TEST_DATABASE_URL →
    DATABASE_URL → 生产配置默认值；postgresql+driver:// 归一为 postgresql://。"""
    for var in ('AJST_TEST_DATABASE_URL', 'DATABASE_URL'):
        v = os.environ.get(var)
        if v:
            return re.sub(r'^postgresql\+\w+://', 'postgresql://', v)
    from config import DB_URL
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', DB_URL)


def _db_snapshot():
    """(快照, 错误)。只读事务 + statement_timeout；绝不写。（与 T-47 同源）"""
    try:
        import psycopg2
        cn = psycopg2.connect(_dsn(), connect_timeout=5)
    except Exception as e:      # noqa: BLE001 - 脱库结局由 finalizer 按 T-24 处置
        return None, f'{type(e).__name__}: {e}'
    try:
        cn.set_session(readonly=True, autocommit=False)
        with cn.cursor() as c:
            c.execute("SET statement_timeout = '10s'")
            out = {}
            for t in ('spectra', 'filters', 'lightcurves'):
                c.execute(f'SELECT COUNT(*), COALESCE(MAX(id)::text, \'\') FROM {t}')
                out[t] = tuple(c.fetchone())
        cn.rollback()           # 只读事务，rollback 仅释放
        return out, None
    finally:
        cn.close()


def _fs_snapshot(root):
    """文件数 + 逐文件 mtime_ns（__pycache__ 字节码缓存不属数据域，剔除）。
    （与 T-47 同源）"""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            snap[os.path.relpath(p, root)] = os.stat(p).st_mtime_ns
    return snap


def _fs_diff(name, a, b, limit=10):
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    if not (added or removed or changed):
        return None
    return (f'{name}: 新增{added[:limit]} 删除{removed[:limit]} '
            f'mtime 变化{changed[:limit]}（共 {len(added)}/{len(removed)}/'
            f'{len(changed)} 项）')


@pytest.fixture(scope='session', autouse=True)
def t24_readonly_snapshot():
    """周期级只读哨兵：session 起点快照、终点逐项比对（T-24）。"""
    cat = os.path.join(_REPO_ROOT, 'catadata')
    bak = os.path.join(_REPO_ROOT, 'backend')
    fs0 = (_fs_snapshot(cat), _fs_snapshot(bak))
    db0, err0 = _db_snapshot()
    db_status = 'ok' if db0 is not None else 'absent'
    yield {'db_snapshot_status': db_status}
    # ── finalizer：逐项比对 ─────────────────────────────────────────────
    fs1 = (_fs_snapshot(cat), _fs_snapshot(bak))
    diffs = []
    d = _fs_diff('catadata/', fs0[0], fs1[0])
    if d:
        diffs.append(d)
    d = _fs_diff('backend/', fs0[1], fs1[1])
    if d:
        diffs.append(d)
    if diffs:
        pytest.fail('T-24 只读快照：文件侧在验收周期内发生变化（RO-1…RO-4）—— '
                    + '；'.join(diffs))
    if db_status == 'absent':
        pytest.fail(f'T-24：数据库不可达（{err0}）⇒ db_snapshot_status="absent"，'
                    '本条按未完成处理、不得宣称绿（T-24 明文）')
    db1, err1 = _db_snapshot()
    assert db1 is not None, f'T-24：DB 二次取数失败（{err1}）'
    if db1 != db0:
        moved = {t: (db0[t], db1[t]) for t in db0 if db0[t] != db1[t]}
        pytest.fail(f'T-24 只读快照：DB 行数/max(id) 变化 —— {moved}')
