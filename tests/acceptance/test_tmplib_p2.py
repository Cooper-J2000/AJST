"""模板库 × K 改正 P2 验收：造模板向导（S2）—— API-4/5/7/11/12、
T-18/T-19/T-22/T-23/T-24/T-26/T-43/T-45、Q-2/Q-10/Q-12 校验电池。

锚点（全部先经库/引擎实测再写进断言）：
  - EP250108a：328 总行、326 星等行、z=0.176（redshift_type='value'）、
    gext_distmod=39.718（T-18；01 §E.8）；
  - GRB260321A：redshift/gext_distmod 皆 NULL，与 EP260321a 共享日期 token
    ⇒ CA-12（T-19）；
  - 全库 gext_corr=true 且 mag_gextcor 为 NULL 恰 16 行（T-23 库侧证据）；
  - 九模板 in_domain 由 P2 迁移实测写入 library.json（T-45 加和不变式）。

建面/删除用例一律走 AJST_TMPLIB_DIR 指 tmp_path 的隔离库根（纪律：生产
tmplibrary 写盘只允许真实 API-5/7/11 请求路径）。库写操作（T-24 翻
discard、T-26 临时管理员）都在 finally 里改回。
"""
import json
import os
import shutil
import sys

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_ROOT = os.path.join(os.path.dirname(_BACKEND), 'catadata', 'tmplibrary')
_FRONTEND = os.path.join(os.path.dirname(_BACKEND), 'frontend')

from tmplib import extract, guard, indomain, paths  # noqa: E402


@pytest.fixture(scope='module')
def client():
    try:
        from app import create_app
        app = create_app()
    except Exception as e:
        pytest.skip(f'应用工厂不可用（多半库不可达）: {type(e).__name__}: {e}')
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _login(c, role='admin', username='p2-acc'):
    with c.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = username
        s['role'] = role


def _logout(c):
    with c.session_transaction() as s:
        s.clear()


#: 合法声明的基底（校验电池逐项破坏它；建面用例原样用）
def _good_decl(**over):
    d = {
        'id': 'p2acc-ep250108a',
        'transient_id': 'EP250108a',
        'object_class': 'GRB afterglow',
        'redshift': {'value': 0.176, 'source': '库内 redshift_ref（T-18 锚）'},
        'distance': {'kind': 'cosmological'},
        'rowset': 'raw',
        'null_system_policy': 'drop',
        'require_min_bands': 2,
    }
    d.update(over)
    return d


# ── tmp 库根 + 向导建面（module 级共享，建面 ~1–3 s 只付一次） ─────────────

@pytest.fixture(scope='module')
def built(client, tmp_path_factory):
    """真实走一遍 API-5：tmp 库根造 EP250108a 模板。yield (root_path, tid, 响应)。

    env 只在建面期间指向 tmp 根，yield 前恢复；用 tmp 根的测试各自再指。
    """
    root = tmp_path_factory.mktemp('tmplib-p2')
    (root / 'templates').mkdir()
    (root / 'data' / 'raw').mkdir(parents=True)
    (root / 'data' / 'surfaces').mkdir(parents=True)
    shutil.copytree(os.path.join(_ROOT, 'data', 'filters'),
                    root / 'data' / 'filters')
    old = os.environ.get('AJST_TMPLIB_DIR')
    os.environ['AJST_TMPLIB_DIR'] = str(root)
    try:
        from tmplib import engine
        engine.reset_caches()
        _login(client)
        r = client.post('/api/tmplib/templates', json=_good_decl())
        assert r.status_code == 200, r.get_data(as_text=True)[:500]
        body = r.get_json()
        assert body['code'] == 'TL_OK'
        tid = body['id']
    finally:
        if old is None:
            os.environ.pop('AJST_TMPLIB_DIR', None)
        else:
            os.environ['AJST_TMPLIB_DIR'] = old
        from tmplib import engine
        engine.reset_caches()
    return root, tid, body


@pytest.fixture
def tmp_env(built):
    """用 tmp 库根的测试：请求期间把库根指过去，函数结束即恢复（function 级，
    不泄漏给后面读真库的用例）。"""
    root, _, _ = built
    old = os.environ.get('AJST_TMPLIB_DIR')
    os.environ['AJST_TMPLIB_DIR'] = str(root)
    yield root
    if old is None:
        os.environ.pop('AJST_TMPLIB_DIR', None)
    else:
        os.environ['AJST_TMPLIB_DIR'] = old


# ── API-4 preview ─────────────────────────────────────────────────────────

def test_preview_requires_auth(client):
    _logout(client)
    r = client.get('/api/tmplib/preview?transient_id=EP250108a')
    assert r.status_code == 401


def test_preview_ep250108a_ledger(client):  # T-18
    _login(client)
    r = client.get('/api/tmplib/preview?transient_id=EP250108a')
    assert r.status_code == 200
    b = r.get_json()
    assert b['code'] == 'TL_OK'
    led = b['ledger']
    assert led['total'] == 328                       # 01 §E.8
    assert led['rejected']['nonmag'] == 2            # 326 星等行
    assert led['kept'] + sum(led['rejected'].values()) == led['total']  # 行账不变式
    tr = b['transient']
    assert tr['redshift'] == pytest.approx(0.176)
    assert tr['redshift_type'] == 'value'
    assert tr['gext_distmod'] == pytest.approx(39.718, abs=1e-3)
    assert b['bands'] and all('band' in x and 'in_registry' in x for x in b['bands'])
    assert b['indomain_estimate']['magnitude_rows'] == 326   # U-19 预估分母
    assert b['suggested_id'] and b['suggested_id'] == b['suggested_id'].lower()


def test_preview_ca12_grb260321a(client):  # T-19
    _login(client)
    r = client.get('/api/tmplib/preview?transient_id=GRB260321A')
    assert r.status_code == 200
    b = r.get_json()
    assert b['transient']['redshift'] is None        # 空壳
    ca12 = b['same_event']
    assert ca12 and ca12['code'] == 'CA-12'
    assert 'EP260321a' in ca12['message'] or 'EP260321a' in str(ca12)


def test_preview_unknown_transient_404(client):
    _login(client)
    r = client.get('/api/tmplib/preview?transient_id=NO_SUCH_TRANSIENT')
    assert r.status_code == 404
    assert r.get_json()['code'] == 'TL_TRANSIENT_UNKNOWN'


def test_preview_recompute_on_selection(client):  # U-13：改勾/改档重算账本
    _login(client)
    base = client.get('/api/tmplib/preview?transient_id=EP250108a').get_json()
    kept_all = base['ledger']['kept']
    # 只留一个波段 ⇒ kept 必须变小（服务端重算，不是前端加减）
    one_band = [b['band'] for b in base['bands']
                if b['in_registry']][0]
    r = client.get(f'/api/tmplib/preview?transient_id=EP250108a&bands={one_band}')
    b = r.get_json()
    assert b['query']['bands'] == [one_band]
    assert 0 < b['ledger']['kept'] < kept_all
    assert b['ledger']['rejected']['band_deselected'] > 0
    # gext 档重算
    r = client.get('/api/tmplib/preview?transient_id=EP250108a&rowset=gext')
    assert r.get_json()['query']['rowset'] == 'gext'
    # 非法档 400
    assert client.get('/api/tmplib/preview?transient_id=EP250108a&rowset=both'
                      ).status_code == 400
    # declare 缺声明 ⇒ E-02 口径 400
    r = client.get('/api/tmplib/preview?transient_id=EP250108a&null_system_policy=declare')
    assert r.status_code == 400 and r.get_json()['code'] == 'TL_DECLARE_MISSING'


# ── build_rowset 纯函数（T-22/T-23） ──────────────────────────────────────

def _row(band, t=1.0, mag=19.0, err=0.1, system='AB', unit='magnitude',
         gext_corr=False, mag_gextcor=None, gext_err=None,
         upperlimit=False, discard=False):
    return {'band': band, 'time': t, 'flux_density': mag,
            'flux_density_err': err, 'flux_density_unit': unit,
            'mag_system': system, 'gext_corr': gext_corr,
            'mag_gextcor': mag_gextcor, 'mag_gextcor_err': gext_err,
            'upperlimit': upperlimit, 'discard': discard}


def test_band_labels_case_strict():  # T-22：F-08 严格同名，禁 lower() 折叠
    rows = [_row('R'), _row('r')]      # 注册表只有大写 R
    csv_rows, ledger, _ = extract.build_rowset(
        rows, rowset='raw', registry_ids=frozenset({'R', 'B'}))
    assert ledger['total'] == 2 and ledger['kept'] == 1
    assert ledger['rejected']['unmapped_band'] == 1  # 'r' 落未映射，不被折叠成 'R'
    assert [r[0] for r in csv_rows] == ['R']


def test_gext_nulls_dropped_no_fallback(client):  # T-23：F-11 空值剔除不回落
    rows = [_row('B', mag=19.0, gext_corr=True, mag_gextcor=None),   # 空值 ⇒ 剔
            _row('B', t=2.0, mag=19.5, gext_corr=True, mag_gextcor=18.9)]
    csv_rows, ledger, _ = extract.build_rowset(
        rows, rowset='gext', registry_ids=frozenset({'B'}))
    assert ledger['rejected']['gext_missing'] == 1
    assert ledger['kept'] == 1
    assert csv_rows[0][2] == 18.9                    # 用的是 gext 列，不是 raw 19.5
    # 库侧证据：全库 gext_corr=true 且 mag_gextcor NULL 恰 16 行（实测值）
    from app import get_session
    from sqlalchemy import text
    sess = get_session()
    try:
        n = sess.execute(text(
            'SELECT count(*) FROM lightcurves WHERE gext_corr IS TRUE'
            ' AND mag_gextcor IS NULL')).scalar()
    finally:
        sess.close()
    assert n == 16


def test_null_system_policies():  # F-10 四档语义
    rows = [_row('B', system=None), _row('B', t=2.0), _row('R', system=None)]
    reg = frozenset({'B', 'R'})
    # drop（默认）：逐行剔
    _, led, _ = extract.build_rowset(rows, rowset='raw', registry_ids=reg)
    assert led['kept'] == 1 and led['rejected']['mag_system_missing'] == 2
    # declare：声明的波段得救
    csv_rows, led, _ = extract.build_rowset(
        rows, rowset='raw', null_system_policy='declare',
        declarations={'B': 'ab'}, registry_ids=reg)
    assert led['kept'] == 2 and csv_rows[0][4] == 'ab'
    # drop-band：沾了 NULL 的波段整带剔
    _, led, _ = extract.build_rowset(
        rows, rowset='raw', null_system_policy='drop-band', registry_ids=reg)
    assert led['kept'] == 0 and led['rejected']['mag_system_missing'] == 3
    # drop-source：整源作废
    with pytest.raises(extract.SourceRejected):
        extract.build_rowset(rows, rowset='raw',
                             null_system_policy='drop-source', registry_ids=reg)


# ── Q-2/Q-10/Q-12 校验电池（T-33：这些失败引擎零调用、零写盘） ────────────

def test_declaration_validation_battery(client, monkeypatch):
    _login(client)
    import chromashift
    calls = []
    monkeypatch.setattr(chromashift, 'build_template',
                        lambda *a, **k: calls.append((a, k)))

    def post(d):
        r = client.post('/api/tmplib/templates', json=d)
        assert r.status_code == 400, (d, r.status_code, r.get_data(as_text=True))
        return r.get_json()

    # Q-2：缺字段逐字段回显
    b = post({})
    assert b['code'] == 'TL_DECLARE_MISSING'
    assert set(b['missing']) >= {
        'id', 'transient_id', 'object_class', 'redshift.value',
        'redshift.source', 'distance.kind', 'rowset', 'null_system_policy'}
    # 缺 redshift.source
    d = _good_decl(); del d['redshift']['source']
    assert 'redshift.source' in post(d)['missing']
    # Q-12：error/error_kind 配对（两个方向都拦）
    d = _good_decl(); d['redshift']['error'] = 0.01
    assert 'redshift.error_kind' in post(d)['missing']
    d = _good_decl(); d['redshift']['error_kind'] = 'line-precision'
    assert 'redshift.error' in post(d)['missing']
    d = _good_decl(); d['redshift'].update(error=0.01, error_kind='bogus')
    assert 'redshift.error_kind' in post(d)['missing']
    # Q-10/F-47：口径旋钮显式；色标 cap 拒收
    d = _good_decl(); del d['require_min_bands']
    assert 'require_min_bands' in post(d)['missing']
    d = _good_decl(); d['require_min_bands'] = 3          # >2 必须写理由
    assert 'validity_notes' in post(d)['missing']
    d = _good_decl(); d['max_colour_term_mag'] = 0.2
    assert post(d)['code'] == 'TL_DECLARE_MISSING'
    # F-18：非 cosmological 必填 mu/d_L；低 z 宇宙学要显式 allow_low_z
    d = _good_decl(); d['distance'] = {'kind': 'measured'}
    assert 'distance.mu|d_L_Mpc' in post(d)['missing']
    d = _good_decl(); d['redshift']['value'] = 0.01
    assert 'distance.allow_low_z' in post(d)['missing']
    d['distance']['allow_low_z'] = True                   # 放行后过校验
    d['transient_id'] = 'NO_SUCH_TRANSIENT'               # ⇒ 404，证明零写盘零建面
    r = client.post('/api/tmplib/templates', json=d)
    assert r.status_code == 404
    # 词表外取值
    d = _good_decl(); d['rowset'] = 'both'
    assert 'rowset' in post(d)['missing']
    d = _good_decl(); d['null_system_policy'] = 'guess'
    assert 'null_system_policy' in post(d)['missing']
    d = _good_decl(); d['null_system_policy'] = 'declare'  # 缺 declarations
    assert 'declarations' in post(d)['missing']
    # 合法声明但源不存在 ⇒ 404（证明过校验后仍零建面）
    r = client.post('/api/tmplib/templates',
                    json=_good_decl(transient_id='NO_SUCH_TRANSIENT'))
    assert r.status_code == 404
    assert calls == []                    # T-33：以上全部引擎零调用


# ── API-5/7/12：建面（真实走一遍，tmp 库根） ──────────────────────────────

def test_build_products_and_library_entry(built):
    root, tid, body = built
    # 产物齐全：manifest + 冻结 CSV + 面 + QC sidecar
    assert (root / 'templates' / f'{tid}.yaml').is_file()
    assert (root / 'data' / 'raw' / f'{tid}.csv').is_file()
    assert (root / 'data' / 'surfaces' / f'{tid}.npz').is_file()
    assert (root / 'data' / 'surfaces' / f'{tid}.qc.json').is_file()
    # 响应：行账 + QC（含 band_admission 分区）+ 实测域内率（U-18/TXT-20）
    assert body['state'] == 'fresh'
    assert body['row_ledger']['total'] == 328
    assert isinstance(body['qc'], dict)
    assert 'excluded_bands' in body['qc'] and 'excluded_reasons' in body['qc']
    idom = body['in_domain']
    assert idom['total'] > 0 and 0 <= idom['answered'] <= idom['total']
    # library.json 条目：origin/配方/第 7 项指纹/行账/域内率
    lib = json.loads((root / 'library.json').read_text())
    e = lib['templates'][tid]
    assert e['origin'] == 'catalog' and e['transient_id'] == 'EP250108a'
    assert e['extract']['rowset'] == 'raw'
    assert len(e['rows_sha256']) == 64 and len(e['csv_sha256']) == 64
    assert e['in_domain']['total'] == idom['total']
    assert 'history' not in lib                        # 族谱已移除
    # manifest 落盘即过引擎自校验（write_manifest 内含），此处复核关键映射
    import yaml
    m = yaml.safe_load((root / 'templates' / f'{tid}.yaml').read_text())
    assert m['schema'] == 'chromashift-template-1'
    assert m['photometry']['time']['frame'] == 'observer'
    assert m['reddening']['mw_removed'] is False        # rowset=raw
    assert m['stretch']['supported'] is False
    assert m['validity']['require_min_bands'] == 2
    assert 'max_colour_term_mag' not in m.get('validity', {})   # F-47


def test_build_id_conflict_409(built, tmp_env, client):
    _login(client)
    _, tid, _ = built
    r = client.post('/api/tmplib/templates', json=_good_decl(id=tid))
    assert r.status_code == 409
    assert r.get_json()['code'] == 'TL_TEMPLATE_CONFLICT'   # F-01：重复 id 是错误


def test_rebuild_and_in_domain_endpoint(built, tmp_env, client):
    root, tid, _ = built
    _login(client)
    r = client.post(f'/api/tmplib/templates/{tid}/rebuild')   # API-7
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    b = r.get_json()
    assert b['code'] == 'TL_OK' and b['state'] == 'fresh'
    assert b['in_domain']['total'] > 0
    # API-12 公开读缓存值
    _logout(client)
    r = client.get(f'/api/tmplib/in_domain/{tid}')
    assert r.status_code == 200
    ib = r.get_json()['in_domain']
    assert ib['total'] == b['in_domain']['total']
    assert ib['answered'] == b['in_domain']['answered']
    assert r.get_json()['definition']                     # 口径随值给出（TXT-20）


# ── T-24：库行漂移 ⇒ 第 7 项指纹门 + predict 409（E-12） ──────────────────

def test_catalog_rows_drift_stales_predict(built, tmp_env, client):
    root, tid, _ = built
    _login(client)
    # 先证明新鲜时可预测（取面上第一个波段）
    lib = json.loads((root / 'library.json').read_text())
    band = sorted(lib['templates'][tid]['in_domain']['by_band'])[0]
    body = {'template_id': tid, 'band': band, 'z': 0.176, 'times_rest_days': [5.0]}
    r = client.post('/api/tmplib/predict', json=body)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]

    # 翻一行 discard ⇒ 活库行集不再等于冻结指纹（CA-11）
    from app import get_session
    from sqlalchemy import text
    sess = get_session()
    try:
        row_id = sess.execute(text(
            "SELECT id FROM lightcurves WHERE transient_id='EP250108a'"
            " AND flux_density_unit='magnitude' AND discard IS FALSE"
            " ORDER BY id LIMIT 1")).scalar()
        assert row_id is not None
        sess.execute(text('UPDATE lightcurves SET discard = TRUE WHERE id = :i'),
                     {'i': row_id})
        sess.commit()
        try:
            r = client.post('/api/tmplib/predict', json=body)
            assert r.status_code == 409
            b = r.get_json()
            assert b['code'] == 'TL_STALE'
            assert 'rows-drifted' in r.get_data(as_text=True)   # E-12 归因
            # API-3 同样亮 catalog_rows 轴
            d = client.get(f'/api/tmplib/templates/{tid}').get_json()
            assert d['stale_because']['catalog_rows']
        finally:
            sess.execute(text(
                'UPDATE lightcurves SET discard = FALSE WHERE id = :i'),
                {'i': row_id})
            sess.commit()
    finally:
        sess.close()
    # 改回后恢复 fresh（指纹门只认相等性）
    r = client.post('/api/tmplib/predict', json=body)
    assert r.status_code == 200


# ── T-45：域内率归因加和不变式（真库迁移值 + 封闭枚举） ────────────────────

def test_in_domain_invariants_shipped():
    from tmplib.indomain import REASONS
    lib = paths.read_library(_ROOT)
    for tid, e in lib['templates'].items():
        idom = e.get('in_domain')
        assert idom, tid
        n = sum(b['n'] for b in idom['by_band'].values())
        ok = sum(b['ok'] for b in idom['by_band'].values())
        assert n == idom['total'] and ok == idom['answered'], tid
        for b in idom['by_band'].values():
            for reason in b['reasons']:
                assert reason in REASONS or reason.startswith('error:'), (tid, reason)
            assert sum(b['reasons'].values()) + b['ok'] == b['n']
    # 九模板合计与引擎侧口径同量级（01 §E.6：97.5%）
    tot = sum(e['in_domain']['total'] for e in lib['templates'].values())
    ans = sum(e['in_domain']['answered'] for e in lib['templates'].values())
    assert ans / tot > 0.95


def test_in_domain_endpoint_shipped(client):  # API-12 契约（T-35 侧）
    r = client.get('/api/tmplib/in_domain/at2017gfo')
    assert r.status_code == 200
    b = r.get_json()
    lib = paths.read_library(_ROOT)
    assert b['in_domain'] == lib['templates']['at2017gfo']['in_domain']  # 逐键相等
    assert b['definition']
    assert client.get('/api/tmplib/in_domain/no-such').status_code == 404


# ── T-43：无自动建面三件套 ────────────────────────────────────────────────

def test_read_paths_leave_library_untouched(client):
    """冷启动读路径后 tmplibrary 文件 mtime 零变化（POS-3/T-43 ①）。"""
    def snapshot():
        out = {}
        for dirpath, _, files in os.walk(_ROOT):
            for f in files:
                p = os.path.join(dirpath, f)
                out[p] = os.stat(p).st_mtime_ns
        return out

    before = snapshot()
    client.get('/api/tmplib/config')
    client.get('/api/tmplib/guards')
    client.get('/api/tmplib/templates')
    client.get('/api/tmplib/templates/sn2006aj')
    client.get('/api/tmplib/in_domain/sn2006aj')
    _login(client)
    client.get('/api/tmplib/preview?transient_id=EP250108a')
    client.post('/api/tmplib/predict', json={
        'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
        'times_rest_days': [6.0]})
    assert snapshot() == before


def test_frontend_build_callsites_static():
    """T-43 ②：建面/重建的前端调用点只在点击回调里；无定时器。"""
    src = open(os.path.join(_FRONTEND, 'js', 'pages', 'tmplib.js'),
               encoding='utf-8').read()
    assert src.count('createTmplibTemplate(') == 1      # 唯一触发点 U-17
    # 重建两个调用点：单行 [重建] 与 S4 批量重建（仍都是点击回调，无自动触发）
    assert src.count('rebuildTmplibTemplate(') == 2
    assert 'setInterval' not in src and 'setTimeout(() => _doBuild' not in src
    assert '_doBuild()' in src.split("t.id === 'tlBuild'")[1].split('}')[0]
    assert '_doBatchRebuild()' in src.split("t.id === 'tlBatchRebuild'")[1].split('}')[0]
    api = open(os.path.join(_FRONTEND, 'js', 'api.js'), encoding='utf-8').read()
    for name in ('getTmplibPreview', 'createTmplibTemplate', 'rebuildTmplibTemplate',
                 'deleteTmplibTemplate', 'getTmplibInDomain'):
        assert f'export const {name}' in api or f'export function {name}' in api \
            or f'export async function {name}' in api


def test_backend_no_background_build_static():
    """T-43 ③：后端无线程/定时器建面；build_template 只在 surfaces.py。"""
    tmplib_dir = os.path.join(_BACKEND, 'tmplib')
    for fname in os.listdir(tmplib_dir):
        if not fname.endswith('.py'):
            continue
        src = open(os.path.join(tmplib_dir, fname), encoding='utf-8').read()
        assert 'Thread(' not in src and 'threading.Timer' not in src, fname
        assert 'rebuild=True' not in src, fname          # A-3：读路径永不含
        if fname != 'surfaces.py':
            assert '.build_template(' not in src, fname    # POS-3 唯一调用点
    routes = open(os.path.join(_BACKEND, 'routes', 'tmplib.py'),
                  encoding='utf-8').read()
    assert '.build_template(' not in routes
    assert 'Thread(' not in routes


# ── T-26：软删/硬删语义（放最后：消耗 built 模板） ─────────────────────────

@pytest.fixture(scope='module')
def admin_user(client):
    """临时管理员（users 表一行；yield 后删除改回）。"""
    from app import get_session
    from models import User
    sess = get_session()
    try:
        sess.query(User).filter(User.username == 'p2-acc-admin').delete()
        u = User(username='p2-acc-admin', role='admin')
        u.set_password('p2-acc-pw')
        sess.add(u)
        sess.commit()
    finally:
        sess.close()
    yield 'p2-acc-admin'
    sess = get_session()
    try:
        sess.query(User).filter(User.username == 'p2-acc-admin').delete()
        sess.commit()
    finally:
        sess.close()


def test_delete_semantics(built, tmp_env, client, admin_user):
    root, tid, _ = built
    url = f'/api/tmplib/templates/{tid}'

    _logout(client)                                       # 匿名 ⇒ 401
    assert client.delete(url).status_code == 401
    _login(client, role='user')                           # 非管理员 ⇒ 403
    assert client.delete(url).status_code == 403

    _login(client, role='admin', username=admin_user)
    # origin 保护：出厂影子条目不可删（F-19）
    libp = root / 'library.json'
    lib = json.loads(libp.read_text())
    lib['templates']['p2-fake-shipped'] = {
        'origin': 'shipped', 'state': 'fresh', 'deleted': None,
        'stale_because': {'engine_inputs': [], 'catalog_rows': None,
                          'filter_vendor': None}}
    libp.write_text(json.dumps(lib, ensure_ascii=False, indent=1))
    r = client.delete('/api/tmplib/templates/p2-fake-shipped')
    assert r.status_code == 409
    assert r.get_json()['code'] == 'TL_ORIGIN_PROTECTED'

    # 软删：文件移 .trash，索引留 deleted 标记
    r = client.delete(url)
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    b = r.get_json()
    assert b['mode'] == 'soft'
    assert not (root / 'templates' / f'{tid}.yaml').exists()
    trash = root / 'data' / '.trash' / b['trash']
    assert (trash / f'{tid}.yaml').is_file()
    lib = json.loads(libp.read_text())
    assert lib['templates'][tid]['deleted']['mode'] == 'soft'
    # 已软删再软删 ⇒ 409
    r = client.delete(url)
    assert r.status_code == 409
    assert r.get_json()['code'] == 'TL_ALREADY_DELETED'
    # 硬删：缺密码 403；错密码 403；对密码 200 且条目消失、.trash 清空
    assert client.delete(f'{url}?hard=true').status_code == 403
    r = client.delete(url, json={'hard': True, 'password': 'wrong'})
    assert r.status_code == 403 and r.get_json()['code'] == 'TL_FORBIDDEN'
    r = client.delete(url, json={'hard': True, 'password': 'p2-acc-pw'})
    assert r.status_code == 200 and r.get_json()['mode'] == 'hard'
    lib = json.loads(libp.read_text())
    assert tid not in lib['templates']
    assert not list((root / 'data' / '.trash').glob(f'{tid}-*'))
    # 硬删后 id 可重新造（F-01 的冲突只对活条目）
    assert paths.valid_id(tid)
