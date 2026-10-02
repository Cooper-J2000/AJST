"""L2: specphot API-2（POST /photometry，S1 合成测光）接线与契约测试。

覆盖（02 §5.2.1 / §3.2 / §3.3 / §3.5 / §7.6 / §8.2）：
  Q-23/Q-1/Q-5/Q-6/Q-25/Q-27/Q-31   请求校验（400/501 显式 jsonify，禁 500）
  A-7/F-77/V-3a/V-20/F-79②          上传数组路径：数值规范化、λ 合法域、坐标、波长帧
  §3.5/TXT-23                       非 anchored：mag_err_cal=null + err_source='none' +
                                    书面原因（不以 0 冒充），err_scope 对齐 'stat'
  F-49/F-11                         单波段 anchored 闭式解析解：输出 mag == 锚点星等
                                    （κ=g/f ⇒ mag = m_syn − 2.5log10 κ = m_obs，就地可导）
  F-47                              平谱（Fλ=const）下 λ_iso 退化为 λ_pivot（解析恒等式：
                                    λ_iso ≡ sqrt(∫Tλdλ/∫T dλ/λ) = λ_pivot）
  式(5)/F-7                         f_mjy = 10^((C_AB_MJY_ZERO − mag)/2.5)（零点 16.4）
  ST-3/F-80①                        同请求两次命中同一缓存；anchor_rows 的值变化
                                    （dict 序列化按排序 (key,value) 对）⇒ 缓存键不同
  Q-31                              custom_curves 对未登记波段可用；回显
                                    curve_source='upload' + 节点数（len(lam)，非谱像素数）
不写库；上传件不落盘；断言不依赖库内具体数值（波段 id 与曲线内联 golden）。
"""
import json

import pytest

from app import create_app
from specphot.constants import C_AB_MJY_ZERO, C_MAX_BANDS, C_MIN_UPLOAD_POINTS


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
        s['username'] = 'photometry-tester'
        s['role'] = 'user'


# ─── 合成数据（golden 内联；全部断言可就地推导） ──────────────────────────

LAMS = [4000.0 + 10.0 * i for i in range(12)]                  # 12 点 ≥ C_MIN_UPLOAD_POINTS
F0 = 1e-14                                                     # median|F| ≪ 1e-13（V-17 低簇）
NOISE = [0.5, 1.3, 0.8, 1.6, 0.9, 1.4, 0.7, 1.2, 1.0, 0.6, 1.1, 0.75]
FLUX = [F0 * m for m in NOISE]                                 # 非线性模式 ⇒ 二阶差分代理 > 0
CC = {'band': 'test-custom-r', 'lam_aa': [3990.0, 4045.0, 4100.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}           # 三角通带（3 节点）
ANCHOR18 = [{'band': 'test-custom-r', 'mag': 18.0, 'mag_system': 'AB', 'mag_err': 0.05}]


def _upload_body(flux=None, meta=None, anchor_rows=None, **over):
    body = {'spectrum': {'lam_aa': list(LAMS), 'flux': list(flux or FLUX),
                         'flux_err': None, 'meta': meta or {}},
            'bands': ['test-custom-r'], 'custom_curves': [dict(CC)],
            'weighting': 'photon'}
    if anchor_rows is not None:
        body['anchor_rows'] = anchor_rows
    body.update(over)
    return body


def _post(client, body):
    return _json(client.post('/api/specphot/photometry', json=body))


# ─── 1. 上传数组路径 200 + 平谱解析解（F-47 / 式(5)） ─────────────────────

def test_upload_flat_spectrum_direct_and_analytic_iso(client):
    """Fλ=const + 无 flux_unit（kind=absolute，upload 单键）⇒ direct；
    λ_iso ≡ λ_pivot（平谱解析恒等式，F-47）；f_mjy 与 mag 满足式(5)。"""
    _login(client)
    d = _post(client, _upload_body(flux=[F0] * 12,                      # 严格平谱
                                   meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    assert d['spectrum_source'] == 'upload' and d['spectrum_id'] is None
    assert d['mode_effective'] == 'direct'
    r = d['results'][0]
    assert r['curve_source'] == 'upload'
    assert r['curve_nodes'] == len(CC['lam_aa']) == 3        # Q-31：节点数非谱像素数
    assert r['lambda_iso_aa'] == pytest.approx(r['lambda_pivot_aa'], rel=1e-12)
    assert r['f_mjy'] == pytest.approx(10.0 ** ((C_AB_MJY_ZERO - r['mag']) / 2.5),
                                       rel=1e-12)            # 式(5)，零点 16.4（F-7）
    assert 12.0 <= r['mag'] <= 27.0                          # m_syn 合理域内


def test_upload_anchored_single_band_mag_equals_anchor(client):
    """单波段 anchored 闭式解析解：κ = g/f ⇒ mag = m_syn − 2.5log10 κ = 锚点星等
    （F-49 缩放方向 scales_spectrum + F-11 流量空间 GLS，就地可导）。"""
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18))
    assert d['mode_effective'] == 'anchored'
    assert d['results'][0]['mag'] == pytest.approx(18.0, rel=1e-9)
    assert d['results'][0]['mag_err_cal'] is not None        # anchored：σ_cal 可评
    assert d['results'][0]['err_source']['mag_err_cal'] == 'closed_form'
    assert d['s_anchor']['n_bands_used'] == 1
    assert d['s_anchor']['chi2'] is None                     # F-12：单波段无一致性检验


def test_upload_curve_kind_unknown_warns_ca01(client):
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18))
    r = d['results'][0]
    assert r['curve_kind'] == 'unknown'
    assert any(w.get('reason') == 'kind_unknown' for w in r['warnings'])   # TXT-2


def test_manual_anchor_without_mag_err_is_used(client):
    """U-41 手加行编辑器无 mag_err 字段 ⇒ 手加锚点必须可参与锚定（无 σ_obs
    行只经 C_syn 约束，F-51）；κ=g/f ⇒ mag==锚点星等恒等式仍精确成立。
    delta_m_err 只含合成侧一项并挂 CA-15 书面说明（§4.2 delta_m 行 / TXT-23）。"""
    _login(client)
    anchor = [{'band': 'test-custom-r', 'mag': 18.0, 'mag_system': 'AB',
               'mjd': 53000.0}]                     # 无 mag_err 键
    # 非平谱（σ_syn 可估）：平谱 + 无 σ_obs 会让 C 全奇异 ⇒ E-10 退化拒绝
    d = _post(client, _upload_body(flux=[F0 * 1.3 * m for m in NOISE],
                                   anchor_rows=anchor))
    assert d['mode_effective'] == 'anchored'
    assert d['results'][0]['mag'] == pytest.approx(18.0, rel=1e-9)
    assert d['results'][0]['delta_m_err'] is not None
    assert any(w.get('reason') == 'anchor_mag_err_absent'
               for w in d['results'][0]['warnings'])


def test_row_level_no_overlap_others_return(client):
    """V-7：谱与某波段完全不重叠 ⇒ 该行不出结果、其余正常返回（行级 E-04）；
    被弃波段的告警挂 CA-05 族并注明 E-04 reason。"""
    _login(client)
    far = {'band': 'test-far-uv', 'lam_aa': [9000.0, 9050.0, 9100.0],
           't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}
    body = _upload_body(flux=[F0] * 12, meta={'flux_unit': 'erg/s/cm^2/Angstrom'},
                        bands=['test-custom-r', 'test-far-uv'],
                        custom_curves=[dict(CC), far])
    d = _post(client, body)
    assert [r['band'] for r in d['results']] == ['test-custom-r']
    ca05 = [w for w in d['warnings'] if w.get('code') == 'CA-05'
            and w.get('reason') == 'row_rejected_no_overlap']
    assert ca05 and 'test-far-uv' in ca05[0]['message']


# ─── 2. 非 anchored：σ_cal = null + 书面原因（§3.5 / TXT-23，P0-4） ───────

def test_direct_mode_mag_err_cal_is_null_with_reason(client):
    _login(client)
    d = _post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    r = d['results'][0]
    assert r['mag_err_cal'] is None                          # 不是 0 冒充
    assert r['err_source']['mag_err_cal'] == 'none'          # TXT-23 词表
    assert r['err_scope']['f_err_mjy'] == 'stat'             # total 只含 stat 项
    assert r['err_scope']['delta_m_err'] == 'stat'
    assert any('书面原因' in w.get('message', '') for w in r['warnings'])


# ─── 3. 校验拒绝（二选一 / 上限 / 闸门 / 枚举；400/501 显式，禁 500） ──────

def test_q23_source_ambiguous(client):
    _login(client)
    body = _upload_body()
    body['spectrum_id'] = 1
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 400 and _json(r)['reason'] == 'source_ambiguous'
    r = client.post('/api/specphot/photometry', json={})
    assert r.status_code == 400 and _json(r)['reason'] == 'source_ambiguous'


def test_q1_bands_limits(client):
    _login(client)
    r = client.post('/api/specphot/photometry', json=_upload_body(bands=[]))
    assert r.status_code == 400 and _json(r)['reason'] == 'too_many_bands'
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(bands=[f'b{i}' for i in range(C_MAX_BANDS + 1)]))
    assert r.status_code == 400 and _json(r)['reason'] == 'too_many_bands'


def test_q5_mono_requires_explicit_allow(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, band_mode='mono'))
    assert r.status_code == 400 and _json(r)['reason'] == 'curve_missing'


def test_q27_diagnostics_true_is_501_p3c(client):
    """P3c 起 z_from_lines 走真实现 + M-6 闸（上传件 lambda_frame 缺省按 vacuum
    处理、非 unknown ⇒ 不触 E-14；M-6 复核表缺位 ⇒ 200 + null 闸门块）。
    501 定态与两态数值由 test_l2_specphot_p3c.py 承载，这里只验闸门块在场。"""
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, diagnostics={'z_from_lines': True}))
    d = _json(r)
    assert r.status_code == 200
    assert d['diagnostics']['z_from_lines']['verdict'] == 'disabled_line_frame_unverified'
    assert d['diagnostics']['z_from_lines']['z_fit'] is None
    assert 'M-6' in d['diagnostics']['z_from_lines']['reason']


def test_q27_diagnostics_non_bool_rejected(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18,
                                      diagnostics={'z_from_lines': 'yes'}))
    assert r.status_code == 400 and _json(r)['reason'] == 'bad_enum'


def test_preprocess_p2_options_are_live_p2(client):
    """P2 切片 2c：合束/平滑算术已落地（F-108/F-109）⇒ smooth=3 放行、V-21
    "抽后再判"生效：12 点谱合束 k=2 ⇒ 6 个完整块 < C_MIN_PIXELS ⇒ 行级
    E-04 too_few_pixels（400，V-7/V-13）；factor 表外值仍 E-14(rebin_factor)
    （F-108① 闭集）。factor=2 的 200 正例（40 点语料）与数值判据归
    test_l1_specphot_preprocess_p2.py / test_l2_specphot_p2c.py。"""
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, preprocess={'factor': 2}))
    d = _json(r)
    assert r.status_code == 400 and 'too_few_pixels' in json.dumps(d, ensure_ascii=False)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, preprocess={'smooth': 3}))
    assert r.status_code == 200 and _json(r)['preprocess']['identity'] is True
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, preprocess={'factor': 5}))
    assert r.status_code == 400 and _json(r)['reason'] == 'rebin_factor'


def test_mode_model_is_501_p2(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, mode='model'))
    d = _json(r)
    assert r.status_code == 501 and d['phase'] == 'P2'


def test_f48_mag_system_st_rejected(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, mag_system='ST'))
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'vega_or_st_unavailable' and d['reason'] == 'st_unavailable'


def test_spectrum_id_bad_type_is_400_not_500(client):
    _login(client)
    for bad in ('abc', 1.5, '1.5', True):
        r = client.post('/api/specphot/photometry', json={'spectrum_id': bad, 'bands': ['r']})
        d = _json(r)
        assert r.status_code == 400, bad
        assert d['code'] == 'bad_request_state', bad


def test_input_type_defenses(client):
    """bands 非 list / mask 非 list / mw 非 dict / custom_curves 非 list /
    anchor_bands 非 list / dt_tol_d 负数 ⇒ 400（E-14 族），不 500。"""
    _login(client)
    cases = [
        (_upload_body(anchor_rows=ANCHOR18, bands='r'), 'bad_enum'),
        (_upload_body(anchor_rows=ANCHOR18, mask='4000-4100'), 'bad_enum'),
        (_upload_body(anchor_rows=ANCHOR18, mw=[1]), 'bad_enum'),
        (_upload_body(anchor_rows=ANCHOR18, custom_curves='x'), 'bad_enum'),
        (_upload_body(anchor_rows=ANCHOR18, anchor_bands='test-custom-r'), 'bad_enum'),
        (_upload_body(anchor_rows=ANCHOR18, dt_tol_d=-1.0), 'bad_enum'),
    ]
    for body, reason in cases:
        r = client.post('/api/specphot/photometry', json=body)
        d = _json(r)
        assert r.status_code == 400, d
        assert d['reason'] == reason, d


def test_q31_custom_curve_invalid_rejected(client):
    _login(client)
    bad = dict(CC, t=[0.0, 1.0])                 # lam/t 等长违反
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, custom_curves=[bad]))
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'curve_invalid'


def test_q31_unregistered_band_via_custom_curve_ok(client):
    """Q-31 语义（本实现裁量）：custom_curves 先并入后查缺失 ⇒ 未登记波段可用；
    覆盖已登记波段亦允许但由 curve_source='upload' 回显可辨（非静默）。"""
    _login(client)
    body = _upload_body(anchor_rows=ANCHOR18)
    d = _post(client, body)
    assert d['results'][0]['curve_source'] == 'upload'
    # 覆盖已登记波段 'V'：回显 upload 且节点数 = 请求体携带值
    body_v = {'spectrum': dict(body['spectrum']), 'bands': ['V'],
              'custom_curves': [dict(CC, band='V')],
              'anchor_rows': [{'band': 'V', 'mag': 18.0, 'mag_system': 'AB',
                               'mag_err': 0.05}]}
    d_v = _post(client, body_v)
    assert d_v['results'][0]['curve_source'] == 'upload'
    assert d_v['results'][0]['curve_nodes'] == 3


def test_q6_vega_with_unregistered_vega2ab_rejected(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=ANCHOR18, mag_system='Vega'))
    d = _json(r)
    assert r.status_code == 400
    assert d['code'] == 'vega_or_st_unavailable' and d['reason'] == 'vega_unavailable'


# ─── 4. 上传件规范化（V-3a / V-20 / F-77 / F-79②） ───────────────────────

def test_v3a_string_numbers_normalized_same_result(client):
    """JSON 字符串数字逐值 _to_float 规范化（V-3a）：与数值输入同解析解。"""
    _login(client)
    d_num = _post(client, _upload_body(anchor_rows=ANCHOR18))
    body_str = _upload_body(anchor_rows=ANCHOR18)
    body_str['spectrum']['lam_aa'] = [str(x) for x in LAMS]
    body_str['spectrum']['flux'] = [repr(f) for f in FLUX]
    d_str = _post(client, body_str)
    assert d_str['results'][0]['mag'] == pytest.approx(18.0, rel=1e-9)
    assert d_str['results'][0]['mag'] == pytest.approx(d_num['results'][0]['mag'], rel=1e-9)


def test_upload_nonnumeric_row_counted_not_500(client):
    """含 1 个不可解析 flux 行 ⇒ 按读侧纪律计数丢弃（CA-10 / n_dropped），不 500。"""
    _login(client)
    flux = list(FLUX)
    flux[3] = 'garbage'
    d = _post(client, _upload_body(flux=flux, anchor_rows=ANCHOR18))
    assert d['read_stats']['n_dropped'] == 1
    assert any(w['code'] == 'CA-10' for w in d['warnings'])
    assert d['results'][0]['mag'] == pytest.approx(18.0, rel=1e-9)


def test_upload_too_few_parseable_points_rejected(client):
    """丢弃后 < C_MIN_UPLOAD_POINTS ⇒ E-14（Q-26），不 500。"""
    _login(client)
    flux = list(FLUX)
    for i in range(12 - C_MIN_UPLOAD_POINTS + 1):
        flux[i] = None
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(flux=flux, anchor_rows=ANCHOR18))
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'too_few_points'


def test_upload_meta_bad_types_rejected(client):
    _login(client)
    for meta in ({'mjd': 'abc'}, {'z': 'abc'}, {'ra_deg': 'garbage'},
                 {'dec_deg': 95.0}):
        r = client.post('/api/specphot/photometry',
                        json=_upload_body(anchor_rows=ANCHOR18, meta=meta))
        d = _json(r)
        assert r.status_code == 400, meta
        assert d['code'] == 'bad_request_state', meta
        assert d['reason'] in ('bad_enum', 'bad_coordinates'), meta


def test_upload_coords_valid_and_ca34_default_frame(client):
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18,
                                   meta={'ra_deg': 188.7362, 'dec_deg': -5.102}))
    assert d['meta']['ra_deg'] == pytest.approx(188.7362)
    assert d['meta']['dec_deg'] == pytest.approx(-5.102)
    assert d['meta_provenance']['ra_deg'] == 'user'
    # lambda_frame 缺省 ⇒ 按 vacuum 处理且必挂 CA-34（假定值，TXT-21）
    assert any(w['code'] == 'CA-34' for w in d['warnings'])


def test_upload_lambda_frame_air_converted_once(client):
    """F-79②：air ⇒ wavconvert 换真空恰好一次，lambda_frame 回显 vacuum。"""
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18,
                                   meta={'lambda_frame': 'air'}))
    assert d['lambda_frame_converted'] == 'air_to_vac'
    assert d['meta']['lambda_frame'] == 'vacuum'
    assert d['n_below_convert'] == 0               # 全部点 ≥ CONVERT_MIN_A=2000


def test_upload_lambda_frame_unknown_ca34(client):
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18,
                                   meta={'lambda_frame': 'unknown'}))
    assert d['meta']['lambda_frame'] == 'unknown'
    assert any(w['code'] == 'CA-34' for w in d['warnings'])


# ─── 5. 缓存（ST-3 / F-80① dict 序列化） ─────────────────────────────────

def test_cache_hit_consistent_and_anchor_value_changes_key(client):
    """同请求两次 ⇒ 同一缓存响应；anchor_rows 的 mag 值变化 ⇒ 键不同 ⇒ mag 不同
    （dict 须按排序 (key,value) 对序列化，否则值变化不敏感、命中错误缓存）。"""
    _login(client)
    d1 = _post(client, _upload_body(anchor_rows=ANCHOR18))
    d2 = _post(client, _upload_body(anchor_rows=ANCHOR18))
    assert d1 == d2
    anchor19 = [dict(ANCHOR18[0], mag=19.0)]
    d3 = _post(client, _upload_body(anchor_rows=anchor19))
    assert d3['results'][0]['mag'] == pytest.approx(19.0, rel=1e-9)
    assert d3['results'][0]['mag'] != pytest.approx(d1['results'][0]['mag'])


# ─── 6. 常量接线（§7.6） ─────────────────────────────────────────────────

def test_constants_wired():
    from specphot import constants
    assert constants.C_MAX_BANDS == 16                       # §7.6 / Q-1
    assert constants.C_ENDPOINT_TIMEOUT_S == 2.0             # §7.6 轻档（S1）


# ─── 7. ST-5 硬超时（E-11 / 504，A-3 显式 jsonify） ───────────────────────

def test_st5_timeout_returns_504_and_releases_gate(client, monkeypatch):
    """deadline 强制过期 ⇒ 504 + code=compute_timeout（非 500）；闸门已释放
    （后续同请求正常返回，不再 429）。monkeypatch 只替换 photometry 模块
    命名空间里的 time 绑定，不影响进程内其它 time 消费者。"""
    import types
    _login(client)
    import specphot.photometry as pm
    # 独特通量 ⇒ spec_hash 不同 ⇒ 绕开其它测试已写入的结果缓存（ST-3）
    uniq = [F0 * (1.0 + 1e-9)] * 12
    # 假时钟每次调用 +1e6 s：第一次（设 deadline）后，任何后续相位检查必超时
    clock = {'t': 0.0}
    monkeypatch.setattr(pm, 'time', types.SimpleNamespace(
        monotonic=lambda: (clock.update(t=clock['t'] + 1.0e6) or clock['t'])))
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(flux=uniq,
                                      meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    assert r.status_code == 504
    d = _json(r)
    assert d['code'] == 'compute_timeout'
    monkeypatch.undo()
    d2 = _post(client, _upload_body(flux=uniq,
                                    meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    assert d2['spectrum_source'] == 'upload'    # 闸门已释放 ⇒ 非 429


# ─── 8. 间隙谱回归（W-22 P0：read_stats.gaps 是 [lo,hi] 对，不是 dict） ───

def test_gapped_spectrum_gap_bridged_not_500(client):
    """两段谱中间留大空档，通带横跨空档 ⇒ 200 且 gap_bridged=true；
    回归钉死 gaps 元素形状（[lo,hi] 对，曾误用 g.get('lam0') 致 500）。"""
    _login(client)
    lams = [4000.0 + 40.0 * i for i in range(10)] + [6000.0 + 40.0 * i for i in range(10)]
    noise20 = NOISE + [1.1, 0.7, 1.3, 0.9, 1.2, 0.8, 1.0, 0.75]
    flux = [F0 * m for m in noise20]                    # 非平谱 ⇒ 代理 σ 可估
    cc = {'band': 'test-span-gap', 'lam_aa': [4200.0, 5300.0, 6400.0],
          't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}
    body = _upload_body(flux=flux, bands=['test-span-gap'],
                        custom_curves=[cc],
                        meta={'flux_unit': 'erg/s/cm^2/Angstrom'})
    body['spectrum']['lam_aa'] = lams                    # _upload_body 默认 12 点，这里换 20 点双段
    d = _post(client, body)
    assert d['spectrum_source'] == 'upload'
    r = d['results'][0]
    assert r['gap_bridged'] is True
    assert r['n_used_pixels'] >= 8                       # C_MIN_PIXELS
