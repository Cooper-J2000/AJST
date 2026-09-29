"""模板库 × K 改正 P4 验收：API-10 误差预算（T-34/T-39/Q-7）、API-9 导出
（T-17 同批数互比、T-40 头逐项勾全、T-41 vendor 漂移不阻断导出）、T-46 复核、
前端静态断言。

脱库程度：validate 电池与 T-46 阈值断言不连库；HTTP 契约走 Flask test_client
（create_app 连库，库不可达整组 skip —— 与 P0–P3 同一策略）。

纪律：DB 只读；不写 tmplibrary（T-41 的副本只造在 tmp_path，且只改副本里的
catadata filters.json）；tmp_path 之外零写盘。
"""
import json
import os
import shutil
import sys
from urllib.parse import quote

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from tmplib import budget as tl_budget, engine, guard, paths, surfaces  # noqa: E402
from tmplib import predict as tl_predict  # noqa: E402

_ROOT = paths.default_root()

_CURVES = quote(json.dumps([{'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05}]))
_SOURCES = quote(json.dumps([{'transient_id': 'EP250108a'}]))


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


def _export(client, fmt, curves=_CURVES, sources=_SOURCES, extra=''):
    return client.get(f'/api/tmplib/export.{fmt}?curves={curves}'
                      f'&sources={sources}{extra}')


# ── API-10：T-39 契约 + F-34/35/36/37/39 ─────────────────────────────────

def test_budget_contract_sn2006aj(client):  # T-39：四项 + 逐带增益 + warnings 透传
    r = client.post('/api/tmplib/budget',
                    json={'template_id': 'sn2006aj', 'band': 'B',
                          'z': 0.0331, 'n_draws': 3})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['code'] == 'TL_OK'
    b = d['budget']
    # 四项恒在（n_draws≥3 时）；值是 max mag
    for term in ('photometric', 'sed_residual', 'colour_term', 'distance'):
        assert b['terms_max_mag'][term] >= 0, term
    assert b['total_max_mag'] > 0 and b['dominant'] in b['terms_max_mag']
    assert b['n_draws'] == 3 and b['seed'] == tl_budget.DEFAULT_SEED
    assert d['z'] == pytest.approx(0.0331)
    # F-39：MC 只扰动对角的 warning 必须原样到达
    assert any('cross_talk_mag_per_dex' in w for w in b['warnings'])
    # F-36：逐带增益 + 来源；灰显语义标志（gain_from_unfold）逐带在
    per_band = b['components']['photometric']['per_band']
    assert len(per_band) >= 5
    for band, g in per_band.items():
        assert 'gain_mag_per_dex' in g and 'gain_source' in g
        assert isinstance(g['gain_from_unfold'], bool)
    assert any(g['gain_source'] == 'unfold fit' for g in per_band.values())
    # F-37：sed_residual 逐带表
    assert len(b['components']['sed_residual']['all_bands_mag']) >= 5
    # F-35：distance 单列（含 describe；dominant 标志是布尔）
    assert d['distance']['mag'] == pytest.approx(
        b['terms_max_mag']['distance'])
    assert d['distance']['describe']['kind'] == 'cosmological'
    assert isinstance(d['distance']['is_dominant'], bool)
    # 显形：默认值与上限写进 provenance（UNVERIFIED-2 实测值，POS-6）
    prov = d['provenance']
    assert prov['host_n_draws_default'] == 32
    assert prov['host_max_draws'] == 200


def test_budget_t34_redshift_error_kind(client):
    """T-34：只有 line-precision/line-scatter 进预算，unestablished 不进。"""
    r = client.post('/api/tmplib/budget',
                    json={'template_id': 'sn2006aj', 'band': 'B', 'n_draws': 0})
    d = r.get_json()
    # sn2006aj：z_err=0.0007 但 error_kind=unestablished ⇒ 不传播进 distance 项
    assert d['distance']['describe']['z_err'] is None
    # F-34：n_draws=0 ⇒ photometric 项缺席而非 0，且 notes 显形
    assert 'photometric' not in d['budget']['terms_max_mag']
    assert any('缺席' in n for n in d['notes'])
    r = client.post('/api/tmplib/budget',
                    json={'template_id': 'sn2010bh', 'band': 'B', 'n_draws': 0})
    d = r.get_json()
    # sn2010bh：z_err=0.0001 line-scatter ⇒ 进 distance 项
    assert d['distance']['describe']['z_err'] == pytest.approx(0.0001)
    # 含红移项 ⇒ distance 项严格大于纯本征速度地板（350 km/s 项单独的值）
    import math as _m
    from chromashift.constants import DEFAULT_PECULIAR_V_KM_S, C_M_S
    floor = 5.0 / _m.log(10.0) * DEFAULT_PECULIAR_V_KM_S * 1e3 / (C_M_S * 0.0591)
    assert d['distance']['mag'] > floor


# ── API-10：Q-7 电池 + 429 ────────────────────────────────────────────────

def test_budget_validate_battery():  # 纯函数：Q-7 逐档
    ok = {None: 32, 0: 0, 3: 3, 200: 200}
    for raw, want in ok.items():
        body = {'template_id': 'sn2006aj', 'band': 'B'}
        if raw is not None:
            body['n_draws'] = raw
        assert tl_budget.validate_request(body)['n_draws'] == want
    for bad in (1, 2, 201, -1, 2.5, '64', True):
        with pytest.raises(tl_predict.TLError) as ei:
            tl_budget.validate_request(
                {'template_id': 'sn2006aj', 'band': 'B', 'n_draws': bad})
        assert ei.value.http == 400, bad
    with pytest.raises(tl_predict.TLError):
        tl_budget.validate_request({'template_id': 'sn2006aj'})   # 缺 band
    with pytest.raises(tl_predict.TLError):
        tl_budget.validate_request({'template_id': 'Bad ID', 'band': 'B'})


def test_budget_quota_http(client):
    r = client.post('/api/tmplib/budget',
                    json={'template_id': 'sn2006aj', 'band': 'B', 'n_draws': 2})
    assert r.status_code == 400
    assert r.get_json()['code'] == 'TL_QUOTA'


def test_budget_busy_429(client):
    """ST-4：与预测/建面同一把非阻塞信号量；占用时立即 429 不排队。"""
    assert surfaces.BUILD_SEM.acquire(blocking=False)
    try:
        r = client.post('/api/tmplib/budget',
                        json={'template_id': 'sn2006aj', 'band': 'B',
                              'n_draws': 3})
        assert r.status_code == 429
        assert r.get_json()['code'] == 'TL_BUSY'
    finally:
        surfaces.BUILD_SEM.release()


# ── API-9：T-17 同批数互比 + T-40 头逐项勾全 ──────────────────────────────

def _csv_val(cell):
    if cell == '':
        return None
    if cell in ('True', 'False'):
        return cell == 'True'
    try:
        return float(cell)
    except ValueError:
        return cell


def _norm(v):
    if v is None or isinstance(v, (bool, str)):
        return v
    return float(v)


def test_export_t17_csv_json_same_rows(client):
    """T-17：CSV 与 JSON 是同一批数（各自重解析后逐单元格互比）。"""
    r_csv = _export(client, 'csv')
    assert r_csv.status_code == 200
    assert r_csv.mimetype.startswith('text/csv')
    lines = r_csv.get_data(as_text=True).splitlines()
    header_idx = next(i for i, l in enumerate(lines) if not l.startswith('#'))
    columns = lines[header_idx].split(',')
    csv_rows = [l.split(',') for l in lines[header_idx + 1:] if l]

    r_json = _export(client, 'json')
    assert r_json.status_code == 200
    dj = r_json.get_json()
    assert dj['code'] == 'TL_OK' and dj['format'] == 'ajst-tmplib-export-1'
    assert dj['columns'] == columns                       # 列序同一契约（§4.3）
    assert len(dj['rows']) == len(csv_rows) > 0
    for jrow, crow in zip(dj['rows'], csv_rows):
        assert len(crow) == len(columns)
        for jv, cv in zip(jrow, crow):
            assert _csv_val(cv) == _norm(jv)
    # 注释头也是同一批（F-56：序列化层不产生第二份口径）
    assert dj['comments'] == [l[2:] for l in lines[:header_idx]]


def test_export_t40_header_items(client):  # F-41' 逐项勾全（不许"N 项"）
    r = _export(client, 'csv')
    lines = [l[2:] for l in r.get_data(as_text=True).splitlines()
             if l.startswith('# ')]
    tpl = [l for l in lines if l.startswith('curve[0]')]
    # CLI 头模板第一行：template/band/mode/z/stretch（逐项）
    assert any('template=sn2006aj' in l for l in tpl)
    assert any('band=B' in l for l in tpl)
    assert any('mode=' in l for l in tpl)
    assert any('z=0.05' in l for l in tpl)
    assert any('stretch=' in l for l in tpl)
    # CLI 头模板第二行：DM / peak / relocated（逐项）
    assert any(l.startswith('curve[0] DM=') for l in tpl)
    assert any('peak' in l and ' AB at t_obs=' in l for l in tpl)
    assert any('relocated=True' in l for l in tpl)   # z=0.05 ≠ 模板 z ⇒ 搬移
    # F-41' 六项补充（逐项）
    assert any(l.startswith('curve[0] transient_id=') for l in tpl)
    assert any('align=time_origin' in l and 'align_reference_epoch=' in l
               for l in tpl)
    assert any('mu_engine=' in l and 'mu_catalog=' in l and 'mu_delta=' in l
               and 'mu_cause=' in l for l in tpl)
    assert any('domain.state=' in l and 'reasons=' in l for l in tpl)
    assert any(l.startswith('curve[0] epoch_answered=') for l in tpl)
    # 源曲线（F-57'：逐源时间原点；F-06'：上限单列计数行）
    src = [l for l in lines if 'transient_id=EP250108a' in l]
    assert src
    assert any('time_origin_kind=event-t0' in l for l in lines)
    assert any('counts detections=' in l and 'upper_limits=' in l
               and '上限单列不入本表' in l for l in lines)
    # 模板曲线与源曲线的 time_origin 各成一组（sn2006aj 是表首行零点）
    assert any('time_origin_kind=table-first-row' in l for l in lines)


def test_export_quota_and_shape(client):
    nine = quote(json.dumps([{'transient_id': f'X{i}'} for i in range(9)]))
    r = client.get(f'/api/tmplib/export.csv?sources={nine}')
    assert r.status_code == 400 and r.get_json()['code'] == 'TL_QUOTA'
    r = client.get('/api/tmplib/export.csv?curves=%7Bbad')
    assert r.status_code == 400
    assert r.get_json()['code'] == 'TL_INCONSISTENT_ARGS'
    r = client.get('/api/tmplib/export.csv')
    assert r.status_code == 400                       # curves/sources 至少给一个


# ── T-41：vendor 守卫触发 CA-01 且导出路径不中断 ──────────────────────────

def test_export_t41_vendor_drift_not_blocking(client, tmp_path, monkeypatch):
    """副本库 + 副本 catadata：filters.json 一字节漂移 ⇒ CA-01 亮起，
    而导出（含引擎预测）照常 —— vendor 轴只报告、不拦截（ST-15）。"""
    lib = tmp_path / 'lib'
    (lib / 'data' / 'raw').mkdir(parents=True)
    (lib / 'data' / 'surfaces').mkdir(parents=True)
    shutil.copytree(_ROOT / 'data' / 'filters', lib / 'data' / 'filters')
    tid = 'sn2010bh'
    (lib / 'templates').mkdir()
    shutil.copy2(_ROOT / 'templates' / f'{tid}.yaml', lib / 'templates')
    shutil.copy2(_ROOT / 'data' / 'raw' / f'{tid}.csv', lib / 'data' / 'raw')
    for ext in ('.npz', '.qc.json'):
        shutil.copy2(_ROOT / 'data' / 'surfaces' / f'{tid}{ext}',
                     lib / 'data' / 'surfaces')
    data = tmp_path / 'catadata'
    data.mkdir()
    real = paths.default_root().parents[1] / 'catadata' / 'filters.json'
    shutil.copy2(real, data / 'filters.json')
    with open(data / 'filters.json', 'ab') as fh:
        fh.write(b' ')                                # 一字节漂移（不碰真文件）

    monkeypatch.setenv('AJST_TMPLIB_DIR', str(lib))
    monkeypatch.setenv('AJST_DATA_DIR', str(data))
    engine.reset_caches()
    try:
        ax = guard.vendor_axis(lib, data_dir=data)    # 守卫确实亮了
        assert ax['ok'] is False and ax['code'] == 'CA-01'
        r = client.get('/api/tmplib/export.csv?curves='
                       + quote(json.dumps([{'template_id': tid, 'band': 'B'}])))
        assert r.status_code == 200                   # 导出不中断
        text = r.get_data(as_text=True)
        assert any(not l.startswith('#') and 'sn2010bh' not in l
                   for l in text.splitlines()[1:])    # 有数据行
        g = client.get('/api/tmplib/guards').get_json()
        assert g['axes']['filter_vendor']['ok'] is False
        assert g['axes']['filter_vendor']['code'] == 'CA-01'
    finally:
        monkeypatch.undo()
        engine.reset_caches()


# ── T-46 复核 + API-2 清单 + 能力面 ───────────────────────────────────────

def test_t46_mu_delta_threshold_is_engine_owned(client):
    """T-46：CA-13 阈值不是自造的 —— 必须等于引擎 build 模块的
    _MAX_COLOUR_TERM_MAG，两处分叉即失败。"""
    from chromashift.build import _MAX_COLOUR_TERM_MAG
    lim = engine.limits()
    assert lim['mu_delta_alert'] == float(_MAX_COLOUR_TERM_MAG)
    assert lim['max_colour_term_mag'] == float(_MAX_COLOUR_TERM_MAG)
    body = client.get('/api/tmplib/config').get_json()
    assert body['limits']['mu_delta_alert'] == pytest.approx(
        float(_MAX_COLOUR_TERM_MAG))


def test_templates_list_and_capabilities(client):
    r = client.get('/api/tmplib/templates')
    body = r.get_json()
    assert 'history' not in body                       # 族谱已移除
    assert isinstance(body.get('templates'), list)
    caps = client.get('/api/tmplib/config').get_json()['capabilities']
    assert caps['export'] is True and caps['budget'] is True
    assert '/api/tmplib/export.csv' in caps['endpoints']
    assert '/api/tmplib/export.json' in caps['endpoints']
    assert '/api/tmplib/budget' in caps['endpoints']


# ── 前端静态断言 ──────────────────────────────────────────────────────────

_FRONTEND = os.path.join(os.path.dirname(_BACKEND), 'frontend', 'js')


def _read(*parts):
    with open(os.path.join(_FRONTEND, *parts), encoding='utf-8') as fh:
        return fh.read()


def test_p4_frontend_static():
    api = _read('api.js')
    assert 'budgetTmplib' in api and "'/tmplib/budget'" in api
    assert 'exportTmplib' in api and '/tmplib/export.' in api
    tpl = _read('pages', 'compare_template.js')
    for bid in ('tplExpCsv', 'tplExpJson', 'tplCopyImg'):
        assert bid in tpl
    cmpjs = _read('pages', 'compare.js')
    assert 'copyCompareChart' in cmpjs and 'ClipboardItem' in cmpjs
    page = _read('pages', 'tmplib.js')
    for fid in ('tlBatchRebuild', 'tlBudgetRun', 'tlBudgetDistOnly',
                'gain_from_unfold', '_doBatchRebuild'):
        assert fid in page
    # 预算面板必须展示 F-36 的灰显语义与 F-39 的 warnings
    assert 'gain_source' in page and 'warnings' in page
