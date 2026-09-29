"""模板库 × K 改正 P0 验收：API-1/API-13 契约、T-13 vendor 轴、T-25 原子写、T-30 防路径泄漏。

脱库程度：带 ① 的用例不连数据库（只读 tmplibrary 文件与 tmp_path 副本）；
HTTP 契约用例走 Flask test_client（create_app 会连库做 init_db，库不可达时
整组 skip —— 与 test_l2_http.py 同一策略）。

纪律：不改真 catadata（T-13 的 vendor 变体只造在 tmp_path）、不写 tmplibrary
的任何东西（library.json 只读），tmp_path 之外零写盘。
"""
import json
import os
import re
import shutil
import sys

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from tmplib import engine, guard, paths  # noqa: E402

_ROOT = paths.default_root()
_NO_ABS = re.compile(r'/home/|/Users/|/root/')   # T-30：响应不得泄漏绝对路径


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


# ── 纯函数层（① 脱库） ──────────────────────────────────────────────────

def test_engine_available_and_deps():  # ①
    avail = engine.available(_ROOT)
    assert avail['ok'], avail['reasons']
    for mod, d in engine.deps().items():
        assert d['ok'], f'{mod} 低于引擎下限: {d}'


def test_code_sha256_matches_engine_recipe():  # ①  —— C_ENGINE_PIN 口径守卫
    """自实现的 code 指纹必须与 registry.input_hashes 的 code 项逐位一致，
    防止引擎改了 _hash_tree 配方而我们静默钉错。"""
    import chromashift
    from chromashift import registry
    spec = chromashift.TemplateSpec.from_yaml(
        _ROOT / 'templates' / 'sn2006aj.yaml')
    assert engine.code_sha256() == registry.input_hashes(spec, _ROOT)['code']


def test_id_rule():  # ①  —— C_ID_RE
    assert paths.valid_id('sn2006aj')
    assert paths.valid_id('a' * 48)
    for bad in ('Sn2006aj', '', '-x', 'a/b', '../etc', 'a' * 49, 'a b'):
        assert not paths.valid_id(bad), bad
    with pytest.raises(ValueError):
        paths.check_id('../escape')


def test_engine_axis_all_fresh():  # ①  —— 出厂 9 面与库内文件一致
    ax = guard.engine_inputs_axis(_ROOT)
    assert ax['ok'] is True
    assert len(ax['templates']) == 9
    assert ax['code_only_templates'] == []


def test_engine_axis_detects_manifest_edit(tmp_path):  # ①  —— T-13 第①轴（副本触发）
    """拷最小库根到 tmp_path，改 manifest 一字节 ⇒ engine_inputs 含 manifest。"""
    lib = tmp_path / 'lib'
    (lib / 'data' / 'raw').mkdir(parents=True)
    (lib / 'data' / 'surfaces').mkdir(parents=True)
    shutil.copytree(_ROOT / 'data' / 'filters', lib / 'data' / 'filters')
    tid = 'sn2010bh'  # 最小的出厂模板（csv 4.5K）
    (lib / 'templates').mkdir()
    shutil.copy2(_ROOT / 'templates' / f'{tid}.yaml', lib / 'templates' / f'{tid}.yaml')
    shutil.copy2(_ROOT / 'data' / 'raw' / 'sn2010bh.csv', lib / 'data' / 'raw')
    for ext in ('.npz', '.qc.json'):
        shutil.copy2(_ROOT / 'data' / 'surfaces' / f'{tid}{ext}',
                     lib / 'data' / 'surfaces')
    ax = guard.engine_inputs_axis(lib)
    assert ax['ok'] is True, ax           # 副本先自证新鲜
    with open(lib / 'templates' / f'{tid}.yaml', 'a') as fh:
        fh.write('\n# touched by test\n')
    ax = guard.engine_inputs_axis(lib)
    assert ax['templates'][tid]['stale']
    assert 'manifest' in ax['templates'][tid]['reasons']


def test_vendor_axis_real_library():  # ①  —— T-13 第③轴（阴性对照，只读真文件）
    ax = guard.vendor_axis(_ROOT)
    assert ax['ok'] is True, ax
    assert ax['recorded_source_sha256'] == ax['current_sha256']


def test_vendor_axis_detects_drift(tmp_path):  # ①  —— T-13/T-41：副本变体触发 CA-01
    """改 tmp_path 里的 filters.json 变体 ⇒ CA-01 亮起；不碰真 catadata。"""
    lib = tmp_path / 'lib'
    (lib / 'data').mkdir(parents=True)
    shutil.copytree(_ROOT / 'data' / 'filters', lib / 'data' / 'filters')
    data = tmp_path / 'catadata'
    data.mkdir()
    real = paths.default_root().parents[1] / 'catadata' / 'filters.json'
    shutil.copy2(real, data / 'filters.json')
    ax = guard.vendor_axis(lib, data_dir=data)
    assert ax['ok'] is True               # 未改 ⇒ 一致
    with open(data / 'filters.json', 'ab') as fh:
        fh.write(b' ')                    # 一字节漂移
    ax = guard.vendor_axis(lib, data_dir=data)
    assert ax['ok'] is False
    assert ax['code'] == 'CA-01'
    assert ax['recorded_source_sha256'] != ax['current_sha256']


def test_catalog_rows_axis_after_p2_stamp():  # ①  —— F-52/F-19：P2 已登记漂移探针
    ax = guard.catalog_rows_axis(_ROOT)   # 无 provider ⇒ 有登记值者 deferred，绝不静默 "ok"
    assert ax['ok'] is True
    assert len(ax['templates']) == 9
    probes = sorted(t for t, v in ax['templates'].items() if v['drift_probe'])
    assert probes == sorted(guard.CATALOG_DERIVED_IDS)
    for tid, t in ax['templates'].items():
        if t['drift_probe']:
            # 4 个 catalog-derived：P2 迁移已按 raw+drop 配方登记活库指纹
            assert t['status'] == 'deferred' and t['rows_sha256']
        else:
            # 其余 5 个出厂模板与本库无配方绑定，保持 untracked
            assert t['status'] == 'untracked' and t['rows_sha256'] is None


def test_rows_sha256_entry_point():  # ①  —— F-52 占位计算入口（纯函数，排序稳定）
    rows = [('B', 2.0, 19.0, 0.1, 'ab', False, False),
            ('B', 1.0, 18.5, 0.1, 'ab', False, False)]
    a = guard.rows_sha256(rows)
    b = guard.rows_sha256(list(reversed(rows)))
    assert a == b and len(a) == 64
    assert guard.rows_sha256(rows + [('B', 3.0, 19.5, None, None, False, False)]) != a


def test_library_json_schema_and_state():  # ①  —— §4.1 schema 契约
    lib = paths.read_library(_ROOT)
    assert lib['schema'] == 'ajst-tmplib-2'
    assert set(lib['engine']) >= {'version', 'code_sha256', 'cosmology'}
    assert lib['filters']['source_sha256']
    tpls = lib['templates']
    assert len(tpls) == 9
    derived = sorted(t for t, v in tpls.items() if v['origin'] == 'catalog-derived')
    assert derived == sorted(guard.CATALOG_DERIVED_IDS)   # F-19
    for t, v in tpls.items():
        assert v['state'] == 'fresh', t                   # state 来自实际 stale 检查
        assert v['stale_because'] == {'engine_inputs': [],
                                      'catalog_rows': None, 'filter_vendor': None}
        assert v['mu'] is None                          # P1 的 Δμ 未登记（shipped 条目）
        idom = v['in_domain']                           # P2 迁移已实测写入（F-45）
        assert idom['total'] > 0, t
        assert 0 <= idom['answered'] <= idom['total']
        n_sum = sum(b['n'] for b in idom['by_band'].values())      # T-45 加和
        ok_sum = sum(b['ok'] for b in idom['by_band'].values())
        assert n_sum == idom['total'] and ok_sum == idom['answered']
        assert set(v['inputs_sha256']) == {
            'manifest', 'photometry', 'filter_index',
            'filter_curves', 'extinction', 'code'}
        assert v['deleted'] is None


def test_library_write_atomic_and_bak_recoverable(tmp_path):  # ①  —— T-25
    root = tmp_path / 'lib'
    v1 = {'schema': paths.SCHEMA_LIB, 'templates': {'a': {'v': 1}}, 'history': []}
    v2 = {'schema': paths.SCHEMA_LIB, 'templates': {'a': {'v': 2}}, 'history': []}
    paths.write_library(v1, root)
    paths.write_library(v2, root)
    # 成功后无 tmp 残留；.bak 是上一代整份
    assert not list(root.glob('library.json.tmp-*'))
    assert json.loads(paths.backup_path(root).read_text())['templates']['a']['v'] == 1
    # 模拟中途失败：主文件丢失 ⇒ 从 .bak 恢复
    paths.library_json_path(root).unlink()
    assert paths.restore_library_backup(root) is True
    assert paths.read_library(root)['templates']['a']['v'] == 1
    # schema 不对的写入被拒，且不产生任何文件
    n = len(list(root.iterdir()))
    with pytest.raises(ValueError):
        paths.write_library({'schema': 'bogus'}, root)
    assert len(list(root.iterdir())) == n


# ── HTTP 契约层（test_client；库不可达时 skip） ─────────────────────────

def test_config_contract(client):
    r = client.get('/api/tmplib/config')
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None      # T-30
    body = r.get_json()
    assert body['code'] == 'TL_OK' and body['available'] is True
    assert body['backend'] == 'in-process'
    eng = body['engine']
    assert eng['name'] == 'chromashift' and eng['version']
    assert len(eng['code_sha256']) == 64                         # C_ENGINE_PIN
    assert eng['cosmology'] == 'Planck18'
    assert all(d['ok'] for d in body['deps'].values())
    enums = body['enums']
    assert enums['source'] == 'engine'
    assert 'line-scatter' in enums['z_error_kinds']
    assert 'cosmological' in enums['distance_kinds']
    assert 'ab' in enums['known_systems'] and 'vega' in enums['known_systems']
    lim = body['limits']
    assert lim['max_curves'] == 8 and lim['max_points'] == 512
    assert lim['low_z'] == pytest.approx(0.02)
    assert lim['max_colour_term_mag'] == pytest.approx(0.15)
    assert lim['mu_delta_alert'] == pytest.approx(               # T-46：阈值不自造
        lim['max_colour_term_mag'])
    assert body['vendor']['ok'] is True
    assert body['library']['schema'] == 'ajst-tmplib-2'
    assert body['library']['templates_registered'] == 9
    caps = body['capabilities']                    # P1 开 predict；P2 开 build/admin；P3 开 compare
    assert caps['predict'] is True
    assert caps['build'] is True and caps['library_admin'] is True
    assert caps['compare'] is True
    assert '/api/tmplib/compare' in caps['endpoints']


def test_config_engine_unavailable_still_200(client, monkeypatch):
    """A-2：库根缺失 ⇒ 200 + TL_ENGINE_UNAVAILABLE 显式 code（宿主无 501）"""
    monkeypatch.setenv('AJST_TMPLIB_DIR', '/nonexistent-tmplib')
    try:
        r = client.get('/api/tmplib/config')
        assert r.status_code == 200
        body = r.get_json()
        assert body['available'] is False
        assert body['code'] == 'TL_ENGINE_UNAVAILABLE'
        assert body['engine'] is None
        codes = [x['code'] for x in body['unavailable_reasons']]
        assert 'root-missing' in codes
        assert _NO_ABS.search(r.get_data(as_text=True)) is None  # T-30 在降级路径同样成立
    finally:
        monkeypatch.delenv('AJST_TMPLIB_DIR')


def test_guards_contract(client):
    r = client.get('/api/tmplib/guards')
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None      # T-30
    body = r.get_json()
    assert body['code'] == 'TL_OK'
    axes = body['axes']
    assert set(axes) == {'engine_inputs', 'catalog_rows', 'filter_vendor'}  # IA-13
    assert axes['engine_inputs']['ok'] is True
    assert len(axes['engine_inputs']['templates']) == 9
    assert axes['filter_vendor']['ok'] is True
    assert axes['catalog_rows']['ok'] is True
