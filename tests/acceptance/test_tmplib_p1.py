"""模板库 × K 改正 P1 验收：API-2/3/6 契约、T-05..T-17、T-30..T-33。

锚点（全部先经引擎直调实测，再写进断言）：
  - golden：sn2006aj B z=0.05 rest[6,9]d ⇒ mag_AB [18.968803668841545,
    18.945926115416185]（与 chromashift USAGE_zh.md §3.2 逐位一致）；
  - sn2006aj 面 t_valid ≈ [0.0026, 34.81] rest-day（load_surface 实测）；
  - at2017gfo Ks z=0.4 ⇒ mode=band、absolute_mag_reference=out-of-coverage、
    mag 有限、M/K=NaN（引擎直调实测）；
  - sn2006aj F098M z=0.05 auto ⇒ mode=mono 降级（引擎直调实测）；
  - Δμ 七值：psql + 引擎复核（sn2002ap +0.2920 / at2017gfo −0.1530 超 CA-13）。

HTTP 契约用例走 Flask test_client（create_app 连库做 init_db，库不可达时
整组 skip —— 与 test_tmplib_p0.py 同一策略）。T-14/T-15 的 Δμ/offset 数字
依赖库内对应源行（gext_distmod/t0），同样只在 client 可用时断言。
T-26（并发下 stale 语义）预留给 P2 建面开放后；T-29（既有两模式行为不变）
由全量旧测试绿证明。
"""
import os
import re
import sys

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_ROOT = os.path.join(os.path.dirname(_BACKEND), 'catadata', 'tmplibrary')
_NO_ABS = re.compile(r'/home/|/Users/|/root/')   # T-30：响应不得泄漏绝对路径

#: golden 锚点（引擎直调逐位一致；改动引擎版本/库文件时本测试必须先红）
GOLDEN = {
    'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
    'times_rest_days': [6, 9],
    'mag_AB': [18.968803668841545, 18.945926115416185],
    'M_abs_AB': [-17.90735642861005, -17.932602852203793],
    'K_mag': [0.0686482186014743, 0.07101708876985668],
    'distance_modulus': 36.80751187885012,
}

#: Δμ 对账锚点（01 §E.1；db 用例）: tid → (delta, 是否 CA-13)
MU_EXPECT = {
    'sn2002ap': (0.2920, True),
    'at2017gfo': (-0.1530, True),
    'sn2006aj': (-0.0406, False),
    'sn1998bw': (-0.0355, False),
    'sn2010bh': (-0.0344, False),
    'ep250108a': (-0.0055, False),
    'ep260321a': (0.0061, False),
}

#: 九模板 time_origin.kind 分类（API-3 time_origin_defaults；逐一实测过）
ORIGIN_KIND = {
    'epoch-zero-value': {'at2018cow', 'sn2010bh'},
    'table-first-row': {'sn2002ap', 'sn2006aj'},
    'epoch-zero-source': {'at2017gfo', 'at2025ulz', 'ep250108a',
                          'ep260321a', 'sn1998bw'},
}


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


def _post(client, body):
    return client.post('/api/tmplib/predict', json=body)


# ── API-2：模板列表 ─────────────────────────────────────────────────────

def test_templates_list_contract(client):
    r = client.get('/api/tmplib/templates')
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None       # T-30
    body = r.get_json()
    assert body['code'] == 'TL_OK'
    tpls = body['templates']
    assert len(tpls) == 9
    for t in tpls:
        assert t['state'] == 'fresh', t['id']
        assert t['stale_because']['engine_inputs'] == [], t['id']
        assert set(t['stale_because']) == {'engine_inputs', 'catalog_rows',
                                           'filter_vendor'}
        assert t['bands'] and t['t_valid_days'] and t['z'] is not None
        # IA-4：列表不含现算 sha、不含 mu（详情端点才给）
        assert 'mu' not in t and 'rows_sha256' not in t
    ids = {t['id'] for t in tpls}
    assert ids == {'at2017gfo', 'at2018cow', 'at2025ulz', 'ep250108a',
                   'ep260321a', 'sn1998bw', 'sn2002ap', 'sn2006aj', 'sn2010bh'}


# ── API-3：模板详情 ─────────────────────────────────────────────────────

def test_template_detail_contract(client):
    r = client.get('/api/tmplib/templates/sn2006aj')
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None       # T-30
    b = r.get_json()
    assert b['code'] == 'TL_OK' and b['id'] == 'sn2006aj'
    assert b['state'] == 'fresh'
    assert b['manifest']['schema'] == 'chromashift-template-1'
    assert b['manifest']['z'] == pytest.approx(0.0331)
    assert b['manifest']['time']['epoch_zero']['kind'] == 'first_point'
    assert b['qc'] is not None                                    # QC 全文透传
    assert b['surface']['bands']                                  # 面可读 ⇒ 节点清单
    assert b['surface']['t_valid_days'][0] < 0.01
    assert b['surface']['t_valid_days'][1] == pytest.approx(34.81, abs=0.01)
    mu = b['mu']
    assert mu['ok'] is True and mu['counterpart_id'] == 'GRB060218A'
    assert b['time_origin_defaults']['kind'] == 'table-first-row'
    # IA-1：详情端点不因 stale 拒绝（此处验证新鲜路径的字段完整性）
    assert set(b['stale_because']) == {'engine_inputs', 'catalog_rows',
                                       'filter_vendor'}


def test_template_detail_unknown_404(client):
    r = client.get('/api/tmplib/templates/nope123')
    assert r.status_code == 404
    b = r.get_json()
    assert b['code'] == 'TL_TEMPLATE_UNKNOWN'
    assert 'available' in b
    r = client.get('/api/tmplib/templates/Bad%20Id')
    assert r.status_code == 404
    assert r.get_json()['code'] == 'TL_TEMPLATE_UNKNOWN'


def test_time_origin_classification(client):  # T-14
    """九模板的 time_origin.kind 恰为三类既定划分；sn2002ap 对库 t0 的偏移
    ≈ +1.17 d（表首行 52304.17 − transients.t0 52303.0，db 用例）。"""
    seen = {}
    for tid in sorted({t for v in ORIGIN_KIND.values() for t in v}):
        r = client.get(f'/api/tmplib/templates/{tid}')
        assert r.status_code == 200, tid
        to = r.get_json()['time_origin_defaults']
        seen[tid] = to['kind']
        # ST-17：建议值必须带来源标注，且绝不回填进 value_mjd
        if to['suggested_mjd'] is not None:
            assert to['suggested_source']
        assert to['value_mjd'] is None
    for kind, tids in ORIGIN_KIND.items():
        assert {t for t, k in seen.items() if k == kind} == tids, (kind, seen)
    r = client.get('/api/tmplib/templates/sn2002ap')
    off = r.get_json()['time_origin_defaults'].get('offset_vs_catalog_t0_days')
    assert off == pytest.approx(1.17, abs=0.05)                   # db 对账


def test_mu_reconciliation(client):  # T-15
    """Δμ = μ_engine − μ_catalog 七值对账；超 0.15（引擎色标 cap，T-46）者
    带 CA-13；无对应源的两模板 ok=False 且绝不报 0（E-25）。"""
    for tid, (delta, big) in MU_EXPECT.items():
        r = client.get(f'/api/tmplib/templates/{tid}')
        assert r.status_code == 200, tid
        mu = r.get_json()['mu']
        assert mu['ok'] is True, (tid, mu)
        assert mu['delta'] == pytest.approx(delta, abs=0.002), tid
        assert ('alert' in mu and mu['alert'] == 'CA-13') is big, tid
        assert mu['cause'] and mu['engine'] is not None and mu['catalog'] is not None
    for tid in ('at2018cow', 'at2025ulz'):
        mu = client.get(f'/api/tmplib/templates/{tid}').get_json()['mu']
        assert mu['ok'] is False
        assert mu['code'] == 'TL_NO_COUNTERPART'
        assert 'delta' not in mu                                  # 无对照 ≠ 0


# ── API-6：golden 锚点与点值口径 ────────────────────────────────────────

def test_predict_golden_anchor(client):  # T-06 + golden
    """sn2006aj B z=0.05 rest[6,9]d：与引擎直调/USAGE_zh §3.2 逐位一致；
    观测系时刻 = rest × (1+z)（F-22）。"""
    r = _post(client, {k: v for k, v in GOLDEN.items()
                       if k in ('template_id', 'band', 'z', 'times_rest_days')})
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None       # T-30
    b = r.get_json()
    assert b['code'] == 'TL_OK'
    c = b['curve']
    assert c['points']['mag_AB'] == GOLDEN['mag_AB']              # 逐位
    assert c['points']['M_abs_AB'] == GOLDEN['M_abs_AB']
    assert c['points']['K_mag'] == GOLDEN['K_mag']
    assert c['distance_modulus'] == GOLDEN['distance_modulus']
    for got, rest in zip(c['points']['time_obs_days'], [6, 9]):
        assert got == pytest.approx(rest * 1.05, abs=1e-9)        # T-06
    assert len(c['meta']) == 21                                   # T-10（实测 21 键）
    # sn2006aj 的表消光史 unknown（CA-05）且零点是表首行（CA-14）⇒ F-25 判外推级
    d = c['domain']
    assert d['state'] == 'extrapolated'
    assert 'absolute-mag-not-intrinsic' in d['reasons'] and 'CA-05' in d['alerts']
    assert 'time-origin-not-event' in d['reasons'] and 'CA-14' in d['alerts']
    assert c['time_origin']['kind'] == 'table-first-row'
    assert 'time_rel_s' not in c['points']                        # 未给基准 ⇒ 不出该键
    prov = b['provenance']
    assert prov['engine_version'] and len(prov['engine_code_sha256']) == 64
    assert prov['cosmology'] == 'Planck18'


def test_predict_explicit_obs_times_clip(client):  # T-08 / F-27
    """显式 times_obs_days 含出窗点 ⇒ 200：重叠段与引擎直调逐点等值，
    出窗点列入 clipped_epochs（CA-03），不整次拒绝。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
                       'times_obs_days': [-50.0, 6.3, 9.45, 200.0]})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['points']['time_obs_days'] == [pytest.approx(6.3),
                                            pytest.approx(9.45)]
    assert c['points']['mag_AB'] == GOLDEN['mag_AB']              # 重叠段逐位
    assert c['domain']['clipped_epochs'] == [-50.0, 200.0]
    assert 'clipped-epochs' in c['domain']['reasons']
    assert 'CA-03' in c['domain']['alerts']
    # 裁剪不再额外降级：状态仍由 CA-05/CA-14（表消光史 unknown + 首行零点）决定
    assert c['domain']['state'] == 'extrapolated'
    # 全部出窗 ⇒ 409（不是裁到空数组的 200）
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
                       'times_obs_days': [500.0, 600.0]})
    assert r.status_code == 409
    assert r.get_json()['code'] == 'TL_OUT_OF_DOMAIN'


def test_predict_out_of_coverage_edge_band(client):  # T-07 / CA-04
    """at2017gfo Ks z=0.4：面的边缘波段 —— m 可给、M/K 按定义全 null，
    domain 报 out-of-coverage（不是程序错误）。"""
    r = _post(client, {'template_id': 'at2017gfo', 'band': 'Ks', 'z': 0.4,
                       'n_times': 20})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['mode'] == 'band'
    assert all(v is not None for v in c['points']['mag_AB'])      # F-55 后有限
    assert all(v is None for v in c['points']['M_abs_AB'])
    assert all(v is None for v in c['points']['K_mag'])
    d = c['domain']
    assert d['state'] == 'extrapolated'
    assert 'out-of-coverage' in d['reasons']
    assert 'CA-04' in d['alerts']
    assert d['absolute_mag_reference'] == 'out-of-coverage'


def test_predict_auto_downgrades_to_mono(client):  # T-09 / CA-02
    """sn2006aj F098M（面上无曲线的波段）auto ⇒ mono 降级 + CA-02。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'F098M', 'z': 0.05,
                       'n_times': 20})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['mode'] == 'mono'
    d = c['domain']
    assert d['state'] == 'extrapolated'
    assert 'mode-downgraded-to-mono' in d['reasons']
    assert 'CA-02' in d['alerts']


def test_predict_low_z_requires_distance(client):  # T-05 / F-24 / E-11
    """z=0.01 < 0.02 且 relocated ⇒ 409 TL_LOW_Z_DISTANCE；
    显式 mu 接管距离 ⇒ 200 且 distance_modulus == mu。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.01,
                       'n_times': 10})
    assert r.status_code == 409
    b = r.get_json()
    assert b['code'] == 'TL_LOW_Z_DISTANCE'
    assert b['z'] == pytest.approx(0.01) and b['low_z'] == pytest.approx(0.02)
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.01,
                       'mu': 31.0, 'n_times': 10})
    assert r.status_code == 200
    assert r.get_json()['curve']['distance_modulus'] == pytest.approx(31.0)
    # allow_low_z=true 显式放行 ⇒ 200，距离模数 = 引擎宇宙学在 z 处的 distmod
    # （放行标志必须随距离对象进引擎，否则宿主侧校验是死开关）
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.01,
                       'allow_low_z': True, 'n_times': 10})
    assert r.status_code == 200
    from astropy.cosmology import Planck18
    assert r.get_json()['curve']['distance_modulus'] == \
        pytest.approx(Planck18.distmod(0.01).value, abs=1e-6)
    # 模板自身 z 且声明 cosmological 时同样要声明（z=0.0331 不触发，对照组）
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'n_times': 10})
    assert r.status_code == 200


def test_predict_non_node_band(client):  # 目标波段不限于实测节点
    """sn2006aj 从未在 g 观测，但 g 的静止频率落在面覆盖内 ⇒ 可算（面上插值），
    mode=band（g 有实测透过率曲线）、ref=ok。超域波段仍整次拒绝（对照）。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'g', 'z': 0.05,
                       'times_rest_days': [6, 9]})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['mode'] == 'band'
    assert c['domain']['absolute_mag_reference'] == 'ok'
    r = _post(client, {'template_id': 'sn1998bw', 'band': 'g', 'z': 0.3,
                       'times_rest_days': [6, 9]})
    assert r.status_code == 409
    assert r.get_json()['code'] == 'TL_OUT_OF_DOMAIN'


def test_predict_quota_refusal(client):  # T-11 / Q-5
    """513 个显式时刻 ⇒ 400 TL_QUOTA（不截断）；引擎零调用由 T-33 覆盖。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
                       'times_rest_days': list(range(1, 514))})
    assert r.status_code == 400
    b = r.get_json()
    assert b['code'] == 'TL_QUOTA'
    assert b['limit'] == 512 and b['got'] == 513


def test_predict_n_times_clamped_with_note(client):  # F-44
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
                       'n_times': 5000})
    assert r.status_code == 200
    b = r.get_json()
    assert 0 < len(b['curve']['points']['time_obs_days']) <= 1200  # 引擎网格再裁到窗内
    assert any('n_times' in n for n in b['notes'])                # 钳制显形
    assert b['provenance']['host_n_times_applied'] == 1200        # 宿主钳制生效


def test_predict_non_finite_params(client):  # T-12 / Q-3
    for bad in ('nan', 'inf', '-inf', float('nan'), float('inf')):
        r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': bad})
        assert r.status_code == 400, bad
        assert r.get_json()['code'] == 'TL_PARAM_NOT_FINITE', bad
    # 同样的解析器管 time_origin / mu / times 列表
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B',
                       'time_origin': 'nan'})
    assert r.get_json()['code'] == 'TL_PARAM_NOT_FINITE'
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B',
                       'times_rest_days': [1.0, 'inf']})
    assert r.get_json()['code'] == 'TL_PARAM_NOT_FINITE'


def test_predict_time_origin_event_refusal(client):  # Q-11 / E-24
    """first_point 模板 + 显式要求 event-t0 ⇒ 409 带替代清单；留空则 200。"""
    r = _post(client, {'template_id': 'sn2002ap', 'band': 'B',
                       'require_time_origin_kind': 'event-t0', 'n_times': 5})
    assert r.status_code == 409
    b = r.get_json()
    assert b['code'] == 'TL_TIME_ORIGIN_UNAVAILABLE'
    assert b['epoch_zero_kind'] == 'first_point'
    assert 'sn2002ap' not in b['alternatives'] and b['alternatives']
    # 留空 ⇒ 200，time_origin.kind=table-first-row + CA-14
    r = _post(client, {'template_id': 'sn2002ap', 'band': 'B', 'n_times': 5})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['time_origin']['kind'] == 'table-first-row'
    assert 'CA-14' in c['domain']['alerts']


def test_predict_user_time_origin_rel_seconds(client):  # ST-9 / F-05'
    """给基准 MJD ⇒ time_rel_s = (t_obs − (user − t_zero))·86400，服务端算好。"""
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
                       'times_rest_days': [6, 9], 'time_origin': 53793.0})
    assert r.status_code == 200
    c = r.get_json()['curve']
    assert c['time_origin']['kind'] == 'user-supplied'
    tz = c['time_origin']['t_zero_mjd']
    assert tz is not None
    rel = c['points']['time_rel_s']
    for t_obs, rs in zip(c['points']['time_obs_days'], rel):
        assert rs == pytest.approx((t_obs - (53793.0 - tz)) * 86400.0, abs=1e-3)


def test_predict_deterministic(client):  # T-17
    body = {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
            'times_rest_days': [6, 9]}
    a = _post(client, body).get_data(as_text=True)
    b = _post(client, body).get_data(as_text=True)
    assert a == b                                                 # 逐位相等


# ── 错误码互不借用（T-16/T-32）与防泄漏（T-30） ─────────────────────────

def test_error_codes_distinct_and_scrubbed(client):  # T-16 / T-32 / T-30
    cases = [
        ({'template_id': 'nope123', 'band': 'B'}, 404, 'TL_TEMPLATE_UNKNOWN'),
        ({'template_id': 'sn2006aj', 'band': 'b'}, 422, 'TL_BAND_UNKNOWN'),
        ({'template_id': 'at2017gfo', 'band': 'Ks', 'z': 0.05, 'n_times': 10},
         409, 'TL_OUT_OF_DOMAIN'),   # Ks 在该面仅 z∈[0.293,1.512]（引擎实测）
        ({'template_id': 'sn2006aj', 'band': 'B', 'z': 0.01, 'n_times': 10},
         409, 'TL_LOW_Z_DISTANCE'),
        ({'template_id': 'sn2006aj', 'band': 'B', 'z': 'nan'},
         400, 'TL_PARAM_NOT_FINITE'),
        ({'template_id': 'sn2006aj', 'band': 'B',
          'times_rest_days': list(range(1, 514))}, 400, 'TL_QUOTA'),
        ({'template_id': 'sn2006aj', 'band': 'B',
          'extrapolation': 'linear'}, 400, 'TL_INCONSISTENT_ARGS'),
        ({'template_id': 'sn2006aj', 'band': 'B',
          'reddening': {'law': 'powerlaw'}}, 400, 'TL_DECLARE_MISSING'),
    ]
    seen = set()
    for body, http, code in cases:
        r = _post(client, body)
        assert r.status_code == http, (code, r.status_code, r.get_json())
        b = r.get_json()
        assert b['code'] == code, (code, b)
        assert isinstance(b['error'], str) and b['error']
        assert _NO_ABS.search(r.get_data(as_text=True)) is None   # T-30
        seen.add(code)
    assert len(seen) == len(cases)                                # T-32：互不借用


def test_validation_failures_never_touch_engine(client, monkeypatch):  # T-33
    """请求形状/配额/声明类校验失败必须在引擎调用之前拒绝（ST-2）。
    （波段成员资格是引擎 filter bank 的封闭词汇，E_FILTER 由引擎抛，
    不在本用例的零调用断言内。）"""
    import chromashift
    calls = []
    def spy(*a, **kw):
        calls.append((a, kw))
        raise AssertionError('校验失败路径不得触达引擎 predict_template')
    monkeypatch.setattr(chromashift, 'predict_template', spy)
    bad = [
        {'template_id': 'nope123', 'band': 'B'},                  # 未知模板
        {'template_id': 'sn2006aj', 'band': 'B', 'z': 0.01},      # 低 z
        {'template_id': 'sn2006aj', 'band': 'B', 'z': 'nan'},     # 非有限
        {'template_id': 'sn2006aj', 'band': 'B',
         'times_rest_days': list(range(1, 514))},                 # 配额
        {'template_id': 'sn2006aj', 'band': 'B',
         'times_rest_days': [1], 'times_obs_days': [1]},          # 互斥
        {'template_id': 'sn2006aj', 'band': 'B',
         'extrapolation': 'linear'},                              # 未 ack
        {'template_id': 'sn2006aj', 'band': 'B',
         'reddening': {'law': 'powerlaw'}},                       # 缺 beta
        {'template_id': 'sn2002ap', 'band': 'B',
         'require_time_origin_kind': 'event-t0'},                 # Q-11
    ]
    for body in bad:
        r = _post(client, body)
        assert r.status_code in (400, 404, 409), r.get_json()
    assert calls == []


def test_predict_busy_429(client):  # T-31 / ST-4
    """信号量被占 ⇒ 立即 429 TL_BUSY（不排队）；429 路径不消费信号量。"""
    from routes.tmplib import _PREDICT_SEM
    assert _PREDICT_SEM.acquire(blocking=False)
    try:
        r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'n_times': 5})
        assert r.status_code == 429
        assert r.get_json()['code'] == 'TL_BUSY'
    finally:
        _PREDICT_SEM.release()
    # 释放后立即可用（429 分支没有多释放/少释放）
    r = _post(client, {'template_id': 'sn2006aj', 'band': 'B', 'n_times': 5})
    assert r.status_code == 200


def test_manifest_error_kinds_disclosed(client):  # T-33 后半 / F-54 旁证
    """九模板 manifest 的 redshift.error_kind：声明了 error 的必带封闭枚举内的
    kind；未声明 error 的 kind 同样为空（不 fabricated）。经 API-3 原样透传。"""
    with_err = {'at2017gfo', 'ep250108a', 'sn2002ap', 'sn2006aj', 'sn2010bh'}
    for tid in sorted({t for v in ORIGIN_KIND.values() for t in v}):
        b = client.get(f'/api/tmplib/templates/{tid}').get_json()
        rz = b['manifest'].get('redshift') or {}
        if tid in with_err:
            assert rz.get('error') is not None, tid
            assert rz.get('error_kind') in ('line-precision', 'line-scatter',
                                            'unestablished'), (tid, rz)
        else:
            assert rz.get('error') is None and rz.get('error_kind') is None, \
                (tid, rz)
