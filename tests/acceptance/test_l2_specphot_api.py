"""L2: specphot HTTP 契约（Flask test_client；只读路径 + 伪造 session 的 POST 契约）。

覆盖（02 §5）：
  A-3/A-5     未到期端点显式 jsonify（501/E-13）；所有响应带 spec_phot_version + warnings[]
  API-1       meta：无 spectrum_id ⇒ requires_spectrum + 谱级 null；带 id ⇒ 口径两键/
              flux_median_cgs/lambda_frame/read_stats/n_catalog_anchors；坏 id ⇒ 404
  API-5       curve/<id>：点集峰值归一、Å 升序、curve_kind 走 registry；未知/无曲线波段 ⇒ E-14/E-04
  API-6       health：deps 三键 + 闸门余量 + 缓存条目数（M-7）
  API-7       parse：形状/单位判定/format_hint 闸（F-76）/闸门忙 ⇒ 429（ST-1/ST-12）
  API-8       ebv：非法坐标 ⇒ E-14 bad_coordinates 且文案含示例；图不可用 ⇒ 200 + CA-23（A-8）；
              T-46：HMS 与十进制度同 E(B−V)（4 位小数）
  鉴权        2026-10-05 起五个 POST 端点（parse/ebv/photometry/continuum/line）对
              未登录访客放开（全链只读计算）；访客打到的是参数校验而非 401
              （API-2 契约主体见 test_l2_specphot_photometry.py）
不写库；ebv 只走宿主 extinction 的只读查询（Q-30）。
"""
import json

import pytest

from app import create_app

import extinction
import specphot
from specphot import registry


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _json(resp):
    assert resp.headers.get('Content-Type', '').startswith('application/json'), \
        f'{resp.request.path} 不是 JSON 响应'
    return json.loads(resp.get_data(as_text=True))


def _login(client):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'contract-tester'
        s['role'] = 'user'


def _logout(client):
    """module 级 client 的登录态会跨用例残留，访客（未登录）用例前必须显式清掉。"""
    with client.session_transaction() as s:
        s.clear()


VALID_TEXT = ('# wavelength_unit: angstrom\n# redshift: 0.5\n'
              + '\n'.join(f'{4000 + i * 10} {1e-15 * (1 + i * 0.01)}' for i in range(12)))


# ─── A-5 / T-26（子集）：响应恒带 spec_phot_version 与 warnings ─────────

def test_a5_every_response_carries_version_and_warnings(client):
    _login(client)
    responses = [
        client.get('/api/specphot/health'),
        client.get('/api/specphot/meta'),
        client.get('/api/specphot/curve/V'),
        client.get('/api/specphot/meta?spectrum_id=99999'),          # 404 错误响应同要求
        client.post('/api/specphot/parse', json={'text': VALID_TEXT}),
        client.post('/api/specphot/parse', json={'text': '1 2'}),    # 400 错误响应同要求
        client.post('/api/specphot/photometry', json={}),            # 400 校验错误同要求
    ]
    for r in responses:
        d = _json(r)
        assert 'spec_phot_version' in d, r.request.path
        assert isinstance(d.get('warnings'), list), r.request.path


# ─── API-6：health ──────────────────────────────────────────────────────

def test_api6_health_shape(client):
    d = _json(client.get('/api/specphot/health'))
    assert d['spec_phot_version']
    # M-7：deps 三键必须与实测环境逐键一致（本机 burst_advocate 三者皆可导入）
    assert set(d['deps']) == {'astropy', 'dust_extinction', 'dustmaps'}
    assert all(isinstance(v, bool) for v in d['deps'].values())
    assert d['gate']['max'] == 1 and d['gate']['free'] in (0, 1)
    assert d['cache']['entries'] >= 0 and d['cache']['max'] == 200


# ─── API-1：meta ────────────────────────────────────────────────────────

def test_api1_meta_without_spectrum_id(client):
    d = _json(client.get('/api/specphot/meta'))
    assert d['requires_spectrum'] is True
    assert d['spectrum'] is None
    assert d['bands'] and isinstance(d['bands'], list)
    for b in d['bands']:
        assert {'id', 'wavelength', 'has_curve', 'curve_kind'} <= set(b)
    cov = d['curve_coverage']
    assert cov['n_bands'] == len(d['bands'])
    assert cov['n_with_curve'] == sum(1 for b in d['bands'] if b['has_curve'])
    # M-1 自洽：registry 的 29 个 id 与 DB 有曲线波段一一对应
    assert cov['n_registered'] == len(registry.CURVE_REGISTRY)
    assert cov['unregistered_with_curve'] == []
    assert cov['registry_fingerprint'] == registry.registry_fingerprint()


def test_api1_meta_with_spectrum_id(client):
    sid = _json(client.get('/api/spectra?per_page=1'))[0]['id']
    d = _json(client.get(f'/api/specphot/meta?spectrum_id={sid}'))
    assert d['requires_spectrum'] is False
    sp = d['spectrum']
    assert sp['spectrum_id'] == sid
    assert sp['lambda_frame'] == 'vacuum'                  # F-79①：库内谱已是真空
    assert sp['lambda_frame_converted'] == 'host'
    assert sp['flux_median_cgs'] is not None               # V-17 回显
    assert 'flux_type' in sp and 'u_fluxes' in sp          # 口径两键（M-3 文件侧）
    assert sp['read_stats']['n_points'] == sp['n_points']
    assert isinstance(sp['n_catalog_anchors'], int) and sp['n_catalog_anchors'] >= 0
    assert sp['spec_hash']


def test_api1_meta_unknown_spectrum_id_is_404(client):
    r = client.get('/api/specphot/meta?spectrum_id=99999999')
    d = _json(r)
    assert r.status_code == 404
    assert d['code'] == 'spectrum_not_found'


# ─── API-5：curve/<id> ─────────────────────────────────────────────────

def test_api5_curve_shape_and_normalization(client):
    d = _json(client.get('/api/specphot/curve/V'))
    assert d['curve_kind'] == registry.curve_kind('V') == 'transmission'
    assert d['source_note']
    lam, t = d['lam_aa'], d['t']
    assert d['n_points'] == len(lam) == len(t)
    assert all(lam[i + 1] > lam[i] for i in range(len(lam) - 1))   # Å 升序
    assert max(t) == pytest.approx(1.0)                            # 峰值归一
    assert all(x >= 0 for x in t)


def test_api5_curve_unknown_filter(client):
    r = client.get('/api/specphot/curve/no-such-filter')
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'bad_request_state' and d['reason'] == 'curve_invalid'


def test_api5_curve_filter_without_curve(client):
    meta = _json(client.get('/api/specphot/meta'))
    no_curve = next(b['id'] for b in meta['bands'] if not b['has_curve'])
    r = client.get(f'/api/specphot/curve/{no_curve}')
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'band_unusable' and d['reason'] == 'curve_missing'


# ─── API-7：parse ───────────────────────────────────────────────────────

def test_api7_open_to_guest(client):
    """2026-10-05 放开：未登录访客可解析上传/粘贴件（只读计算链第一环）。"""
    _logout(client)
    r = client.post('/api/specphot/parse', json={'text': VALID_TEXT})
    d = _json(r)
    assert r.status_code == 200 and d['ok'] is True


def test_api7_parse_shape(client):
    _login(client)
    d = _json(client.post('/api/specphot/parse', json={'text': VALID_TEXT}))
    assert d['ok'] is True
    for k in ('spec_hash', 'n_points', 'lam_aa', 'flux', 'flux_err', 'columns',
              'flux_unit', 'flux_unit_assumed', 'meta', 'meta_provenance',
              'read_stats', 'flux_median_cgs'):
        assert k in d, k
    assert d['n_points'] == 12 == len(d['lam_aa']) == len(d['flux'])
    assert d['columns'] == 2 and d['flux_err'] is None
    assert d['flux_unit_assumed'] is True
    assert d['meta']['z'] == 0.5 and d['meta_provenance']['z'] == 'file'
    assert d['read_stats']['n_points'] == 12


def test_api7_three_columns(client):
    _login(client)
    text = '# wavelength_unit: angstrom\n' + '\n'.join(
        f'{4000 + i * 10} {1e-15} {1e-16}' for i in range(12))
    d = _json(client.post('/api/specphot/parse', json={'text': text}))
    assert d['columns'] == 3
    assert d['flux_err'] == [1e-16] * 12
    assert d['read_stats']['has_err'] is True


def test_api7_format_hint_gate(client):
    """F-76 P2+ 后闭集 {txt, fits, ecsv}；表外值仍 E-14 upload_format_unsupported。"""
    _login(client)
    r = client.post('/api/specphot/parse', json={'text': VALID_TEXT, 'format_hint': 'hdf5'})
    d = _json(r)
    assert r.status_code == 400
    assert d['reason'] == 'upload_format_unsupported'


# ─── API-7：FITS/ECSV 分支（P2+，F-76/T-81；载体现造于内存，不落盘） ───

_UP_ROWS = [[4000.0 + i * 10, 1e-15 * (1 + i * 0.01), 1e-16] for i in range(12)]


def _up_fits_b64():
    import io
    import base64
    import numpy as np
    from astropy.io import fits
    hdu = fits.BinTableHDU.from_columns(
        [fits.Column(name='wavelength', format='D', unit='Angstrom',
                     array=np.array([r[0] for r in _UP_ROWS])),
         fits.Column(name='flux', format='D',
                     array=np.array([r[1] for r in _UP_ROWS])),
         fits.Column(name='flux_err', format='D',
                     array=np.array([r[2] for r in _UP_ROWS]))], name='SPECTRUM')
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(buf)
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _up_ecsv_text():
    import io
    import numpy as np
    import astropy.units as u
    from astropy.table import Table
    t = Table({'wavelength': np.array([r[0] for r in _UP_ROWS]) * u.AA,
               'flux': np.array([r[1] for r in _UP_ROWS]),
               'flux_err': np.array([r[2] for r in _UP_ROWS])})
    t.meta['redshift'] = 0.5
    buf = io.StringIO()
    t.write(buf, format='ascii.ecsv')
    return buf.getvalue()


def test_api7_fits_upload_via_content_b64(client):
    _login(client)
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits', 'content_b64': _up_fits_b64()})
    d = _json(r)
    assert r.status_code == 200 and d['ok'] is True
    assert d['format'] == 'fits'
    assert d['n_points'] == 12 and d['columns'] == 3
    assert d['flux_unit_assumed'] is True
    assert d['flux'] == [r[1] for r in _UP_ROWS]
    assert d['read_stats']['has_err'] is True


def test_api7_fits_sniffed_without_hint(client):
    _login(client)
    r = client.post('/api/specphot/parse', json={'content_b64': _up_fits_b64()})
    d = _json(r)
    assert r.status_code == 200 and d['format'] == 'fits'      # magic bytes 探测


def test_api7_ecsv_upload_via_text(client):
    _login(client)
    r = client.post('/api/specphot/parse', json={'text': _up_ecsv_text()})
    d = _json(r)
    assert r.status_code == 200 and d['format'] == 'ecsv'      # '# %ECSV' 头探测
    assert d['n_points'] == 12 and d['meta']['z'] == 0.5
    assert d['meta_provenance']['z'] == 'file'
    assert d['flux_err'] == [r[2] for r in _UP_ROWS]


def test_api7_format_mismatch_is_rejected(client):
    _login(client)
    r = client.post('/api/specphot/parse',
                    json={'text': VALID_TEXT, 'format_hint': 'ecsv'})
    d = _json(r)
    assert r.status_code == 400
    assert d['reason'] == 'upload_format_unsupported'           # 探测结果与 hint 不符
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits', 'text': VALID_TEXT})
    d = _json(r)
    assert r.status_code == 400
    assert d['reason'] == 'upload_unparseable'                  # fits 缺 content_b64


def test_api7_bad_base64_and_bad_magic(client):
    _login(client)
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits', 'content_b64': '!!!not-b64!!!'})
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'upload_unparseable'
    import base64
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits',
                          'content_b64': base64.b64encode(b'NOTAFITS').decode()})
    d = _json(r)
    assert r.status_code == 400
    assert d['reason'] == 'upload_format_unsupported'           # 缺 SIMPLE magic


def test_api7_fits_ambiguous_hdu_names_candidates(client):
    """T-81② 经 HTTP：多扩展 ⇒ E-14 且错误点名候选扩展，不代选。"""
    import io
    import base64
    import numpy as np
    from astropy.io import fits
    cols = [fits.Column(name='wavelength', format='D', unit='Angstrom',
                        array=np.array([r[0] for r in _UP_ROWS])),
            fits.Column(name='flux', format='D',
                        array=np.array([r[1] for r in _UP_ROWS]))]
    buf = io.BytesIO()
    fits.HDUList([fits.PrimaryHDU(),
                  fits.BinTableHDU.from_columns(cols, name='OBS'),
                  fits.BinTableHDU.from_columns(cols, name='MODEL')]).writeto(buf)
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits',
                          'content_b64': base64.b64encode(buf.getvalue()).decode()})
    d = _json(r)
    assert r.status_code == 400
    assert d['reason'] == 'ambiguous_hdu'
    assert 'OBS' in d['error'] and 'MODEL' in d['error'] and 'hdu' in d['error']
    # 用户指定 hdu 后通过
    r = client.post('/api/specphot/parse',
                    json={'format_hint': 'fits',
                          'content_b64': base64.b64encode(buf.getvalue()).decode(),
                          'hdu': 'MODEL'})
    d = _json(r)
    assert r.status_code == 200 and d['ok'] is True and d['n_points'] == 12


def test_api7_too_few_points_e14(client):
    _login(client)
    text = '# wavelength_unit: angstrom\n' + '\n'.join(
        f'{4000 + i * 10} 1e-15' for i in range(9))
    r = client.post('/api/specphot/parse', json={'text': text})
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'bad_request_state' and d['reason'] == 'too_few_points'


def test_api7_gate_busy_is_429(client):
    _login(client)
    assert specphot._compute_gate.acquire(blocking=False)
    try:
        r = client.post('/api/specphot/parse', json={'text': VALID_TEXT})
        d = _json(r)
        assert r.status_code == 429
        assert d['code'] == 'server_busy'                       # ST-1/ST-12：不排队
    finally:
        specphot._compute_gate.release()


# ─── API-8：ebv ─────────────────────────────────────────────────────────

def test_api8_open_to_guest(client):
    """2026-10-05 放开：未登录访客可查 E(B−V)（尘埃图不可用也回 200+CA-23，A-8）。"""
    _logout(client)
    r = client.post('/api/specphot/ebv', json={'ra': '12:34:56.7', 'dec': '-05:06:07'})
    d = _json(r)
    assert r.status_code == 200 and 'available' in d


def test_api8_bad_coordinates_e14_with_example(client):
    _login(client)
    r = client.post('/api/specphot/ebv', json={'ra': 'garbage', 'dec': 'also-bad'})
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'bad_request_state' and d['reason'] == 'bad_coordinates'
    assert '12:34:56.7' in d['error']                           # 文案含示例（T-46）


def test_api8_map_unavailable_is_200_ca23(client, monkeypatch):
    _login(client)
    monkeypatch.setattr(extinction, 'status',
                        lambda: {'available': False, 'error': 'test-forced'})
    r = client.post('/api/specphot/ebv', json={'ra': '12:34:56.7', 'dec': '-05:06:07'})
    d = _json(r)
    assert r.status_code == 200                                 # A-8：不是 500
    assert d['available'] is False and d['ebv'] is None
    assert 'CA-23' in [w['code'] for w in d['warnings']]


_DUST_OK = extinction.status()['available']


@pytest.mark.skipif(not _DUST_OK, reason='尘埃图不可用（M-7 环境缺 dustmaps 数据）')
def test_api8_hms_and_decimal_give_same_ebv(client):
    """T-46：合法 HMS 与十进制度各一例 ⇒ 同 E(B−V)（4 位小数，宿主 _ebv_cache 口径）"""
    _login(client)
    d1 = _json(client.post('/api/specphot/ebv', json={'ra': '12:34:56.7', 'dec': '-05:06:07'}))
    d2 = _json(client.post('/api/specphot/ebv', json={'ra': '188.7362', 'dec': '-5.102'}))
    assert d1['available'] is True
    assert d1['ebv'] == d2['ebv']
    assert d1['rv'] == 3.1 and d1['law'] == 'P92'
    assert isinstance(d1['ebv'], float) and d1['ebv'] >= 0
    assert 'V' in (d1['alambda_ref'] or {})
    assert isinstance(d1['cold_start_ms'], int)


# ─── 计算端点鉴权（2026-10-05 起对未登录访客放开，全链只读计算） ──────────
# 历史注：API-3/4 的 501 占位契约测试已随真实现退役（P2 2b 起 API-3 见
# backend/specphot/continuum_api.py，P3 切片 2 起 API-4 见 lines_api.py；
# API-4 契约测试在 test_l2_specphot_lines_api.py，U-48 诊断三键仍走
# E-13/501 phase='P3d'）。

def test_compute_endpoints_open_to_guest(client):
    """三个计算端点对未登录访客不再 401；空体 ⇒ 400 参数校验（E-14 族）。"""
    _logout(client)
    for path in ('/api/specphot/photometry', '/api/specphot/continuum', '/api/specphot/line'):
        r = client.post(path, json={})
        assert r.status_code == 400, (path, r.status_code)
