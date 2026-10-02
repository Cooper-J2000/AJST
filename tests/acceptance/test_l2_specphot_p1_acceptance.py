"""L2: specphot **P1 收口验收**补遗（02 §9 P1 行 / §10 审计后缺口的集中落地）。

本文件只补既有 specphot 测试（test_l1_specphot_*、test_l2_specphot_*）审计后
**缺失**的 T-* 条目；已覆盖条目不在此重复（映射表见 P1 收口汇报）：

  T-8   前端 buildPhotometryRequest 默认值表 vs 服务端 photometry.py 默认表逐项比对
  T-10  ST 零点口径：按 §10 原文明文标记 skip（M-5 前不运行，原因内联）
  T-11  FZ-1 的 P1 可测半段：S1 恒观测系积分 ⇒ 改 z 只回显不改数（CA-07 /
        frame.x / kcorr / flux_transform_applied 回显；T-36 的 P1 回显半段同在此）
  T-23  错误/告警码：源码级无表外码 + E 码 wire 映射与中文文案 + P1 可触发的
        CA 码逐例触发且文案非空（§5.3/§5.4 的 P1 子集）
  T-25  并发：闸门占用 ⇒ 慢方 E-12(429)；双线程同时提交 ⇒ 无 500、无死锁
  T-38  IA-8 分档：后端可发的每个 CA 码在前端 groupOf 都有分档（不落 'other'）
  T-41  上传闸门补遗：字节闸精确边界（恰 2,000,000 B 收 / 2,000,001 B 拒）+
        HTTP 层 413/400 细分（§5.3：body_too_big/too_many_points ⇒ 413，其余 400）
  T-42  AnchorSet 服务端半段：同波段冲突 ⇒ E-14(band_conflict) 列两行；取消勾选
        ⇒ 不进 n_bands_used 且不触发 E-10；只留 1 个勾选波段 ⇒ CA-14
  T-43  只拿 hash 不带数组 ⇒ 显式 4xx（不 500、不静默出数）；不同 hash 不互命中缓存
  T-47  上传件零残留：catadata/ + backend/ 文件数与逐文件 mtime、DB 三表行数与
        max(id) 前后逐项相同（T-24 同一快照口径、自带 READ ONLY 连接）；进程内
        结果缓存不含谱数组（ST-3/ST-12/RO-5 代码级断言）
  T-48  诊断六开关：任一开启 ⇒ 501 feature_disabled；全关（{} 与六键全 false）
        与不带 diagnostics 键的基线响应逐字节相同（清缓存后各自独立复算再比）
  T-61  代理去偏＝估计量本身：σ̂/σ_true ≈ k_proxy(ρ,j)（1%），去偏后回收 1%；
        k_proxy 文献锚点 0.6455/0.8292/0.2646（F-97①）
  T-62  双 j 稳定性的估计量层判据：白噪声 j=1/j=2 不可区分（<3%）；AR(1) 下
        j=C_SIGMA_STRIDE 的估计系统性更大（取大者语义，F-97⑤）
  T-63/T-64 的 P1 结构扫描：err_source{} 词表、err_scope{} 词表、'none' ⇒ 键值
        为 JSON null + 书面原因（不得 0/''/NaN 冒充）、有误差键必有配对量值键

纪律：一律不用 db_cur 夹具；T-47 自带 psycopg2 只读连接（READ ONLY +
statement_timeout，连不上按 T-24 明文判未完成而非 skip）；随机判据 seed =
20260927（§10.1 元判据）；全链只读，上传件不落盘。
"""
import json
import math
import os
import re
import threading

import numpy as np
import pytest
from scipy.signal import lfilter

from app import create_app

import specphot
from specphot import errors as ER
from specphot import reader
from specphot.constants import C_MAX_UPLOAD_BYTES, C_SIGMA_STRIDE
from specphot.reader import SpecLoadError, load_upload_text

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONT_SPECPHOT = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')

ERR_SEED = 20260927          # §10.1 元判据②

# §3.12 / U-44：六项诊断增强开关（F-81…F-86），P1 一律渲染但禁用
DIAG_SIX = ['z_from_lines', 'beta_matrix', 'resp_perturb',
            'anchor_reinsert', 'frame_probe', 'sky_subtract']

E_UNIVERSE = {f'E-{i:02d}' for i in range(1, 16)}
CA_UNIVERSE = {f'CA-{i:02d}' for i in range(1, 50)}

# F-94① err_source 词表（免配对通道 count/metadata/none 之外的量值通道）
ERR_SOURCE_WORDS = {'covariance', 'covariance_approx', 'delta_method',
                    'bootstrap', 'closed_form', 'profile', 'proxy'}


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


def _login(client, name='p1-acceptance-tester'):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = name
        s['role'] = 'user'


def _clear_result_cache():
    with specphot._cache_lock:
        specphot._result_cache.clear()


def _has_cjk(s):
    return bool(re.search(r'[\u4e00-\u9fff]', s or ''))


# ─── 合成数据（golden 内联，与 test_l2_specphot_photometry 同款口径） ─────

LAMS = [4000.0 + 10.0 * i for i in range(12)]                  # 12 点：<30 ⇒ CA-21
F0 = 1e-14
NOISE = [0.5, 1.3, 0.8, 1.6, 0.9, 1.4, 0.7, 1.2, 1.0, 0.6, 1.1, 0.75]
FLUX = [F0 * m for m in NOISE]
CC = {'band': 'test-custom-r', 'lam_aa': [3990.0, 4045.0, 4100.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}           # pivot ≈ 4045 Å
CB = {'band': 'test-custom-b', 'lam_aa': [4010.0, 4065.0, 4120.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}           # pivot ≈ 4065 Å
NL = {'band': 'test-narrow-r', 'lam_aa': [4010.0, 4060.0, 4110.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}           # pivot ≈ 4060 Å
ANCHOR18 = [{'band': 'test-custom-r', 'mag': 18.0, 'mag_system': 'AB',
             'mag_err': 0.05}]


def _upload_body(flux=None, meta=None, anchor_rows=None, bands=None,
                 custom_curves=None, **over):
    body = {'spectrum': {'lam_aa': list(LAMS), 'flux': list(flux or FLUX),
                         'flux_err': None, 'meta': meta or {}},
            'bands': list(bands or ['test-custom-r']),
            'custom_curves': [dict(c) for c in (custom_curves or [CC])],
            'weighting': 'photon'}
    if anchor_rows is not None:
        body['anchor_rows'] = anchor_rows
    body.update(over)
    return body


def _post(client, body):
    return _json(client.post('/api/specphot/photometry', json=body))


def _up_text(n=12):
    return '# wavelength_unit: angstrom\n' + '\n'.join(
        f'{4000 + i * 10} 1e-15' for i in range(n))


# ═══ T-8：前端默认值表 ⇔ API-2 服务端默认表逐项比对 ═══════════════════════

def _js_source(name):
    with open(os.path.join(FRONT_SPECPHOT, name), encoding='utf-8') as f:
        return f.read()


def test_t8_frontend_defaults_match_server_defaults():
    """T-8：前端默认值（workbench.js params 初值 + results.js 请求体装配）与
    服务端 photometry.py 的 body.get(..., 默认) 逐项相同；前端所发键集 ⊆ 服务端
    认的键集（缺的键服务端有默认）。golden 行号以本仓现状注明。"""
    wb = _js_source('workbench.js')
    m = re.search(r"params: \{([^}]*)\}", wb)
    assert m, 'workbench.js 的 params 初值未找到'
    pairs = dict(re.findall(r"(\w+):\s*('(?:[^']*)'|true|false|[\d.]+)", m.group(1)))
    mono = pairs['allow_mono'] == 'true'
    fe = {
        'weighting': pairs['weighting'].strip("'"),
        'band_mode': 'mono' if mono else 'integrated',   # results.js: band_mode = allow_mono ? 'mono' : 'integrated'
        'allow_mono': mono,
        'mag_system': pairs['mag_system'].strip("'"),
        'mode': pairs['mode'].strip("'"),
        'err_policy': pairs['err_policy'].strip("'"),
        'mask': [],                                       # workbench.js:40 maskRanges: []
        'redshift': None,                                 # results.js: redshift = nOr(...) ⇒ 空表单即 null
        'dt_tol_d': None if pairs['dt_tol_d'] == "''" else float(pairs['dt_tol_d']),
        'ebv_override': None if pairs['ebv_override'] == "''" else float(pairs['ebv_override']),
        'diagnostics': {},                                # results.js:56 diagnostics: {}
        'preprocess': {},                                 # results.js:57 preprocess: {}
        'mw_correct': pairs['mw_correct'] == 'true',
    }
    # 服务端默认表 golden（backend/specphot/photometry.py，行号为本仓现状）：
    server = {
        'weighting': 'photon',        # :437 body.get('weighting', 'photon')
        'band_mode': 'integrated',    # :437 body.get('band_mode', 'integrated')
        'allow_mono': False,          # :458 mono 仅在显式 allow_mono is True 时放行
        'mag_system': 'AB',           # :438
        'mode': 'auto',               # :438
        'err_policy': 'auto',         # :439
        'mask': [],                   # :461 body.get('mask') or []
        'redshift': None,             # :472 body.get('redshift', None)
        'dt_tol_d': None,             # :474
        'ebv_override': None,         # :471 mwq.get('ebv') is None ⇒ None
        'diagnostics': {},            # :419 diag = diag or {}（恒在场、未开启为空对象）
        'preprocess': {},             # P1b：前端缺省不带键 ⇒ 服务端恒等态（Q-33）
        'mw_correct': True,           # mwq.get('correct', True)：缺键默认 true（§5.2.1 规范默认）
    }
    # 键集：前端 buildPhotometryRequest 所发键 ⊆ 服务端读取的键（缺键服务端有默认）
    rs = _js_source('results.js')
    assert re.search(r"band_mode:\s*S\.params\.allow_mono \? 'mono' : 'integrated'", rs)
    # P1b：preprocess 从恒等字面量改为按非默认键装配（errcol_choice 等；缺省 =
    # 不带键 = 服务端恒等态），Q-33 形态与 T-8 默认值语义不变
    # P2c：diagnostics 从恒等字面量改为按 diagFlags 只装配勾选项（Q-27 只收
    # 布尔；默认全关 ⇒ 空对象，与服务端 diag = diag or {} 的缺省态同形）
    assert "Object.entries(S.diagFlags" in rs
    assert re.search(r"body\.preprocess = pp;", rs)
    assert re.search(r"if \(S\.params\.errcol_choice\) pp\.errcol_choice", rs)
    assert re.search(r"mask:\s*S\.maskRanges\.map", rs)
    assert re.search(r"redshift:\s*nOr\(S\.metaForm\.z\)", rs)
    assert re.search(r"mw:\s*\{\s*correct:\s*S\.params\.mw_correct === true", rs, re.S)
    src = open(os.path.join(_REPO_ROOT, 'backend', 'specphot', 'photometry.py'),
               encoding='utf-8').read()
    for k in ('bands', 'weighting', 'band_mode', 'allow_mono', 'mag_system', 'mode',
              'err_policy', 'mask', 'mw', 'redshift', 'dt_tol_d', 'diagnostics',
              'preprocess', 'anchor_rows', 'anchor_bands', 'spectrum_id', 'spectrum'):
        assert f"body.get('{k}'" in src, k
    for k, v in server.items():
        assert fe[k] == v, k


# ═══ T-10：ST 零点口径（按 §10 原文明文 skip） ═══════════════════════════

@pytest.mark.skip(reason='T-10 待 M-5（Vega 参考谱入库）后运行：P1 库内无 Vega 谱，'
                         'ST 请求已被 E-08/400 显式拒绝（见 '
                         'test_l2_specphot_photometry.test_f48_mag_system_st_rejected '
                         '与 test_l1_specphot_fluxcal.test_f48_st_rejected_in_p1）。'
                         'Vega 谱入库后本例才可断言 m_ST ≈ 0.03（容差 0.05）。')
def test_t10_st_zero_point_with_vega_reference():
    raise AssertionError('M-5 完成前不应运行（见 skip 原因）')


# ═══ T-11（FZ-1 的 P1 半段）＋ T-36 的回显半段 ════════════════════════════

def test_t11_p1_observatory_frame_invariance_and_frame_echo(client):
    """P1 的 S1 恒观测系积分（photometry.py Q-12 注记）：改 z 只进回显与 CA-07，
    不得改动任何合成测光数（FZ-1 的静止系平移等价属 P1 余项，余项未做前
    「z 不改数」就是本期的可判形式）。同时钉 T-36 的 P1 回显半段：
    kcorr='none' 与 flux_transform_applied='none'。"""
    _login(client)
    d0 = _post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    dz = _post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom', 'z': 0.5}))
    assert dz['frame']['x'] == pytest.approx(1.5)            # (1+z) 逐值回显
    assert dz['frame']['z_source'] == 'user'
    assert d0['results'] == dz['results']                    # z 不改任何结果数值
    assert dz['kcorr'] == 'none' and dz['flux_transform_applied'] == 'none'   # T-36 P1 半段
    ca07 = [w for w in dz['warnings'] if w['code'] == 'CA-07'
            and w.get('reason') == 'band_shift_only']
    assert ca07 and '0.5' in ca07[0]['message']
    assert not any(w['code'] == 'CA-07' for w in d0['warnings'])


# ═══ T-23：契约覆盖的 P1 子集（无表外码 + wire 映射 + 逐例触发 + 中文文案） ══

def _emitted_codes():
    pat = re.compile(r"'((?:E|CA)-\d{2})'")
    out = set()
    root = os.path.join(_REPO_ROOT, 'backend', 'specphot')
    for fn in sorted(os.listdir(root)):
        if fn.endswith('.py'):
            with open(os.path.join(root, fn), encoding='utf-8') as f:
                out |= set(pat.findall(f.read()))
    return out


def test_t23_no_codes_outside_the_closed_universe():
    """T-23 反向抽查的服务端形式：backend/specphot 发出的每个码都落在
    §5.3 的 E-01…E-15 与 §5.4 的 CA-01…CA-49 闭集内（不得发明新码）。"""
    emitted = _emitted_codes()
    assert emitted <= (E_UNIVERSE | CA_UNIVERSE), emitted - (E_UNIVERSE | CA_UNIVERSE)


def test_t23_e_wire_mapping_http_and_chinese_wording(client):
    """§5.3 E-01…E-15（P1 已上线的十个 wire 码）：code/HTTP 映射正确且文案为
    非空中文。E-07 按 reason 细分 413/400（body_too_big/too_many_points ⇒ 413）。"""
    app = client.application
    cases = [
        ('E-01', 'spectrum_not_found', 404, None),
        ('E-02', 'wavelength_not_sorted', 400, None),
        ('E-03', 'not_absolute_flux', 400, 'flux_scale_suspect'),
        ('E-04', 'band_unusable', 400, 'curve_missing'),
        ('E-07', 'input_over_limit', 413, 'body_too_big'),
        ('E-07', 'input_over_limit', 413, 'too_many_points'),
        ('E-07', 'input_over_limit', 400, 'too_many_masks'),
        ('E-08', 'vega_or_st_unavailable', 400, 'st_unavailable'),
        ('E-10', 'anchor_unavailable', 409, None),
        ('E-11', 'compute_timeout', 504, None),
        ('E-13', 'feature_disabled', 501, 'phase_pending'),
        ('E-14', 'bad_request_state', 400, 'bad_enum'),
    ]
    with app.test_request_context():
        for code, wire, http, reason in cases:
            kw = {} if reason is None else {'reason': reason}
            e = SpecLoadError(code, f'测试注入的中文错误文案（{code}）', **kw)
            resp, st = specphot._load_error_response(e)
            d = json.loads(resp.get_data(as_text=True))
            assert st == http and d['code'] == wire, (code, reason)
            assert _has_cjk(d['error']), code


def test_t23_ca_codes_triggered_with_nonempty_chinese(client):
    """T-23 的 P1 逐例触发：P1 实现可发的每个 CA 码至少一例触发，且 message
    非空中文（§5.4）。CA-30 走库内谱读侧（reader 级，不写库）。"""
    _login(client)
    seen = {}

    def collect(d):
        for w in list(d.get('warnings', [])):
            seen.setdefault(w['code'], w['message'])
        for r in d.get('results', []):
            for w in r.get('warnings', []):
                seen.setdefault(w['code'], w['message'])

    # 1. 基础 anchored（自定义曲线 12 点）：CA-01/02/06/14/15/21/34
    collect(_post(client, _upload_body(anchor_rows=ANCHOR18)))
    # 2. 完全不重叠的行：CA-05（row_rejected_no_overlap）
    far = {'band': 'test-far-uv', 'lam_aa': [9000.0, 9050.0, 9100.0],
           't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}
    collect(_post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom'},
                                       bands=['test-custom-r', 'test-far-uv'],
                                       custom_curves=[CC, far])))
    # 3. m_syn 越出合理域：CA-03（量级 1e-20 ⇒ m_syn≈29.6 > C_MAG_SANITY_HI，
    #    mag 族按 null 出）。定标双键矛盾的 CA-03 在 L1 已触发
    #    （test_t33_uncal_kind_matrix）；上传件路径 flux_type 恒文件侧读，
    #    HTTP 层造不出矛盾对（T-54③ 的纪律本身）。
    collect(_post(client, _upload_body(flux=[1e-20] * 12,
                                       meta={'flux_unit': 'erg/s/cm^2/Angstrom'})))
    # 4. 单色近似 CA-04 的**前提分支**：无曲线 + allow_mono ⇒ band_integrals
    #    走 band_mode='mono'（F-16）。当前库态 29/29 注册波段全有曲线、
    #    未注册波段先被 E-04(curve_missing) 拒 ⇒ CA-04 经 API 不可达（数据态
    #    而非代码态）；此处钉函数级分支 + 源码级文案在场（见下方断言）。
    import numpy as np
    import specphot.response as R
    out = R.band_integrals(np.asarray(LAMS), np.asarray([1e-14] * 12),
                           None, None, weighting='photon', mask_ranges=[],
                           allow_mono=True, mono_lam_ref=4045.0)
    assert out['band_mode'] == 'mono' and out['fnu_cgs'] > 0
    # 5. 读侧丢弃：CA-10
    flux = list(FLUX)
    flux[3] = None
    collect(_post(client, _upload_body(flux=flux, anchor_rows=ANCHOR18)))
    # 6. z>0.05 未做静止系改正：CA-07(band_shift_only)
    collect(_post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom',
                                            'z': 0.5})))
    # 7. 缺坐标且要求银消改正：CA-23(ext_no_coords)
    collect(_post(client, _upload_body(anchor_rows=ANCHOR18, mw={'correct': True})))
    # 8. κ* 外推：CA-19（锚点波段 pivot 4060 在锚点色域内，目标波段 4045 在域外）
    collect(_post(client, _upload_body(bands=['test-custom-r', 'test-narrow-r'],
                                       custom_curves=[CC, NL],
                                       anchor_rows=[{'band': 'test-narrow-r', 'mag': 18.0,
                                                     'mag_system': 'AB', 'mag_err': 0.05}])))
    expected = {'CA-01', 'CA-02', 'CA-03', 'CA-05', 'CA-06', 'CA-07',
                'CA-10', 'CA-14', 'CA-15', 'CA-19', 'CA-21', 'CA-23', 'CA-34'}
    missing = expected - set(seen)
    assert not missing, missing
    for c, msg in seen.items():
        assert _has_cjk(msg), c
    # CA-04：源码级在场且文案非空中文（API 级触发受当前库态限制，见上）
    src = open(os.path.join(_REPO_ROOT, 'backend', 'specphot', 'photometry.py'),
               encoding='utf-8').read()
    m_ca04 = re.search(r"'code': 'CA-04', 'message': '([^']+)'", src)
    assert m_ca04 and _has_cjk(m_ca04.group(1))
    # CA-30：gext 二级谱（V-16 / W-22 的服务端证据）
    payload = {'meta': {'id': 9, 'transient_id': 'T1', 'wavelength_type': None,
                        'gext_corr': True, 'gext_ebv': '0.0247', 'gext_rv': '3.1',
                        'parent_filename': 'parent.dat'},
               'data': {'O': {'spectra': {'u_wavelengths': 'Angstrom',
                                          'data': [[4000.0, 1e-15]]}}}}
    ls = reader.load_catalog_spectrum(payload)
    ca30 = next(w for w in ls['warnings'] if w['code'] == 'CA-30')
    assert _has_cjk(ca30['message'])


# ═══ T-25：并发（闸门慢方 E-12；无 500、无死锁） ══════════════════════════

def test_t25_gate_held_slow_request_gets_e12(client):
    _login(client)
    assert specphot._compute_gate.acquire(blocking=False)
    try:
        r = client.post('/api/specphot/photometry',
                        json=_upload_body(anchor_rows=ANCHOR18))
        assert r.status_code == 429
        d = _json(r)
        assert d['code'] == 'server_busy' and 'retry_after_s' in d   # E-12：不排队
    finally:
        specphot._compute_gate.release()


def test_t25_two_concurrent_computes_no_500_no_deadlock(client):
    """S1 × S1 同时提交：状态 ∈ {200, 429}、至少一方成功、双方都返回
    （join 超时 = 死锁判据）。与 sedfit 的跨端点并发属宿主侧行为，P1 以本模块
    闸门自身验证（收口汇报裁量项）。"""
    _login(client)
    results = {}

    def worker(i):
        with client.application.test_client() as c:
            with c.session_transaction() as s:
                s['authenticated'] = True
                s['username'] = f'conc-{i}'
                s['role'] = 'user'
            body = _upload_body(flux=[F0 * (1.0 + 1e-7 * i) * m for m in NOISE],
                                anchor_rows=ANCHOR18)
            r = c.post('/api/specphot/photometry', json=body)
            results[i] = r.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), '并发请求未返回（疑似死锁）'
    assert set(results.values()) <= {200, 429}, results      # 无 500
    assert 200 in results.values(), results


# ═══ T-38：IA-8 分档完备（P1 可发码 × 前端 groupOf） ══════════════════════

def test_t38_frontend_grouping_covers_every_emitted_ca_code():
    """P1 期间前端是 CA 码唯一的呈现层（IA-8）：后端 specphot 源码可发的每个
    CA 码都必须在 results.js 的 groupOf 有明确分档（CA-07 按 reason 分流，
    亦须显式在场），不得落 'other' 兜底；分档键集 ⊆ GROUPS 定义的档。"""
    rs = _js_source('results.js')
    gmap = dict(re.findall(r"'(CA-\d{2})':\s*'(\w+)'", rs))
    assert set(gmap) <= CA_UNIVERSE
    emitted = {c for c in _emitted_codes() if c.startswith('CA-')}
    ungrouped = {c for c in emitted if c not in gmap and c != 'CA-07'}
    assert not ungrouped, ungrouped
    assert "w.code === 'CA-07'" in rs                        # reason 分流支在场
    groups = set(re.findall(r"\['(\w+)',\s*'[^']*',\s*'\w+'\]", rs))
    assert {'input', 'anchor', 'approx', 'cover', 'err', 'readq', 'time',
            'z', 'interp', 'other'} <= groups
    assert set(gmap.values()) <= groups


# ═══ T-41（补遗）：字节闸精确边界 + HTTP 层 413/400 细分 ══════════════════

def test_t41_byte_gate_exact_boundary():
    """恰 C_MAX_UPLOAD_BYTES 字节收下；2,000,001 B ⇒ E-07(body_too_big)。"""
    head = '# wavelength_unit: angstrom\n'
    rows = ''.join(f'{4000 + i * 10} 1e-15\n' for i in range(12))
    pad_len = C_MAX_UPLOAD_BYTES - len(head) - len(rows) - len('# pad: \n')
    assert pad_len > 0
    text = head + rows + '# pad: ' + 'x' * pad_len + '\n'
    assert len(text.encode('utf-8')) == C_MAX_UPLOAD_BYTES
    ls = load_upload_text(text)
    assert ls['read_stats']['n_points'] == 12
    with pytest.raises(SpecLoadError) as ei:
        load_upload_text(text + 'x')                         # 2,000,001 B
    assert ei.value.code == 'E-07' and ei.value.reason == 'body_too_big'


def test_t41_http_413_vs_400_split(client):
    """§5.3 E-07：body_too_big / too_many_points ⇒ 413；ragged 列数 ⇒ 400
    （E-14 族 upload_unparseable，附 first_bad_line）。"""
    _login(client)
    too_many = '# wavelength_unit: angstrom\n' + '\n'.join(
        f'{4000 + i} 1e-15' for i in range(20001))           # <2 MB ⇒ 点数闸先行
    r = client.post('/api/specphot/parse', json={'text': too_many})
    d = _json(r)
    assert r.status_code == 413 and d['code'] == 'input_over_limit'
    assert d['reason'] == 'too_many_points'
    ragged = ('# wavelength_unit: angstrom\n' + '4000 1e-15\n' * 5
              + '4050 1e-15 1e-16\n' + '4060 1e-15\n' * 6)
    r2 = client.post('/api/specphot/parse', json={'text': ragged})
    d2 = _json(r2)
    assert r2.status_code == 400 and d2['code'] == 'bad_request_state'
    assert d2['reason'] == 'upload_unparseable'


# ═══ T-42（服务端半段）：AnchorSet ════════════════════════════════════════

def test_t42_band_conflict_lists_both_rows(client):
    """F-78①：源表/手加（此处两条手加同形）同波段 ⇒ E-14(reason=band_conflict)
    且列出冲突两行；服务端不得替用户择一。前端 CA-33 已在 GUI 走查覆盖。"""
    _login(client)
    rows = [{'band': 'test-custom-r', 'mag': 18.0, 'mag_system': 'AB', 'mag_err': 0.05},
            {'band': 'test-custom-r', 'mag': 19.0, 'mag_system': 'AB', 'mag_err': 0.05}]
    r = client.post('/api/specphot/photometry',
                    json=_upload_body(anchor_rows=rows))
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'band_conflict'
    assert len(d['conflict_rows']) == 2
    assert {row['mag'] for row in d['conflict_rows']} == {18.0, 19.0}


def test_t42_deselected_band_excluded_without_e10(client):
    """F-78②：取消勾选的行不进 n_bands_used、仍留在表里计入
    n_anchor_excluded，且不触发 E-10（与配对失败是两件事）。"""
    _login(client)
    rows = [{'band': 'test-custom-r', 'mag': 18.0, 'mag_system': 'AB', 'mag_err': 0.05},
            {'band': 'test-custom-b', 'mag': 19.0, 'mag_system': 'AB', 'mag_err': 0.05}]
    d = _post(client, _upload_body(bands=['test-custom-r', 'test-custom-b'],
                                   custom_curves=[CC, CB], anchor_rows=rows,
                                   anchor_bands=['test-custom-r']))
    assert d['mode_effective'] == 'anchored'                 # 200 ⇒ 无 E-10
    assert d['s_anchor']['n_bands_used'] == 1
    assert d['n_anchor_excluded'] == 1
    used = {r['band']: r['used'] for r in d['anchor_rows']}
    assert used == {'test-custom-r': True, 'test-custom-b': False}


def test_t42_single_checked_band_gives_ca14_not_pairing_failure(client):
    _login(client)
    d = _post(client, _upload_body(bands=['test-custom-r', 'test-custom-b'],
                                   custom_curves=[CC, CB],
                                   anchor_rows=[{'band': 'test-custom-b', 'mag': 18.0,
                                                 'mag_system': 'AB', 'mag_err': 0.05}],
                                   anchor_bands=['test-custom-b']))
    assert d['mode_effective'] == 'anchored'                 # 不是配对失败（E-10）
    assert d['s_anchor']['n_bands_used'] == 1
    assert d['s_anchor']['chi2'] is None                     # F-12：单波段无一致性检验
    assert any(w['code'] == 'CA-14' for w in d['warnings'])


# ═══ T-43（补遗）：hash-only 拒绝 + 不同 hash 不互命中缓存 ═════════════════

def test_t43_hash_without_arrays_explicitly_rejected(client):
    """A-7「spec_hash 不是身份」：只拿 hash 不带数组来算 ⇒ 永远 E-01（404
    spectrum_not_found），不 500、不静默出数（T-43 原文口径）。"""
    _login(client)
    body = _upload_body()
    body['spectrum'] = {'spec_hash': '0123456789abcdef'}
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 404
    d = _json(r)
    assert d['code'] == 'spectrum_not_found'


def test_t43_distinct_hash_never_cross_hits_cache(client):
    _login(client)
    d1 = _post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    d2 = _post(client, _upload_body(
        flux=[F0 * (m + 0.017) for m in NOISE],
        meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    assert d1['spec_hash'] != d2['spec_hash']
    assert abs(d1['results'][0]['mag'] - d2['results'][0]['mag']) > 1e-3


# ═══ T-47：上传件零残留（T-24 同一快照口径） ══════════════════════════════

def _dsn():
    """T-24 的 DSN 链：AJST_TEST_DATABASE_URL → DATABASE_URL → 生产配置默认值
    （与 test_l3_db_invariants 同链，不另写连接串）。"""
    import os as _os
    for var in ('AJST_TEST_DATABASE_URL', 'DATABASE_URL'):
        v = _os.environ.get(var)
        if v:
            return re.sub(r'^postgresql\+\w+://', 'postgresql://', v)
    from config import DB_URL
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', DB_URL)


def _db_snapshot():
    """(快照, 错误)。只读事务 + statement_timeout；绝不写。"""
    try:
        import psycopg2
        cn = psycopg2.connect(_dsn(), connect_timeout=5)
    except Exception as e:      # noqa: BLE001 - 脱库结局由调用方按 T-24 处置
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
    """文件数 + 逐文件 mtime_ns（__pycache__ 字节码缓存不属数据域，剔除）。"""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            snap[os.path.relpath(p, root)] = os.stat(p).st_mtime_ns
    return snap


def test_t47_upload_leaves_zero_residue(client):
    """RO-5 / ST-12：API-7 解析 + API-2 计算各一遍后，catadata/ 与 backend/
    的文件数与逐文件 mtime、DB 三表行数与 max(id) 逐项不变；进程内结果缓存
    不含谱数组（ST-3 只缓存结果）。DB 连不上 ⇒ 按 T-24 明文判未完成（不 skip）。"""
    _login(client)
    cat = os.path.join(_REPO_ROOT, 'catadata')
    bak = os.path.join(_REPO_ROOT, 'backend')
    fs0 = (_fs_snapshot(cat), _fs_snapshot(bak))
    db0, err0 = _db_snapshot()
    if db0 is None:
        pytest.fail(f'T-47：数据库不可达（{err0}）⇒ db_snapshot_status="absent"，'
                    '本条按未完成处理、不得宣称绿（T-24 明文）')
    # 走一遍：API-7 解析 + API-2 计算（上传件路径）
    r = client.post('/api/specphot/parse', json={'text': _up_text()})
    assert r.status_code == 200
    d = _post(client, _upload_body(anchor_rows=ANCHOR18))
    assert d['spectrum_source'] == 'upload'
    # 快照逐项比对
    fs1 = (_fs_snapshot(cat), _fs_snapshot(bak))
    db1, err1 = _db_snapshot()
    assert db1 is not None, err1
    assert fs1 == fs0, 'catadata/ 或 backend/ 的文件集或 mtime 发生变化'
    assert db1 == db0, 'DB 行数或 max(id) 发生变化'
    # 响应与结果缓存均不携带谱数组（ST-3/ST-12：数组只在请求周期内持有）
    assert 'lam_aa' not in d and 'flux' not in d
    forbidden = {'lam_aa', 'flux', 'flux_err', 'flux_flambda_cgs'}

    def _scan(o):
        if isinstance(o, dict):
            hit = set(o) & forbidden
            assert not hit, f'结果缓存含谱数组键 {hit}'
            for v in o.values():
                _scan(v)
        elif isinstance(o, list):
            for v in o:
                _scan(v)

    with specphot._cache_lock:
        cached = list(specphot._result_cache.values())
    for body in cached:
        _scan(body)
    h = _json(client.get('/api/specphot/health'))
    assert h['cache']['entries'] == len(specphot._result_cache)


# ═══ T-48：诊断开关总则（U-44） ═══════════════════════════════════════════

def test_t48_all_off_is_byte_identical_to_baseline(client):
    """六项全关（diagnostics={} 与六键全 false）的响应与「从未实现这六项」的
    基线响应逐字节相同。diagnostics{} 恒在场（未开启为空对象）；为排除结果
    缓存短路，每次请求前清缓存、各自独立复算后再比。"""
    _login(client)
    body = _upload_body(anchor_rows=ANCHOR18)
    _clear_result_cache()
    base = client.post('/api/specphot/photometry', json=body)
    assert base.status_code == 200
    assert _json(base)['diagnostics'] == {}
    _clear_result_cache()
    off_empty = client.post('/api/specphot/photometry',
                            json=dict(body, diagnostics={}))
    _clear_result_cache()
    off_false = client.post('/api/specphot/photometry',
                            json=dict(body, diagnostics={k: False for k in DIAG_SIX}))
    assert off_empty.status_code == off_false.status_code == 200
    assert off_empty.get_data(as_text=True) == base.get_data(as_text=True)
    assert off_false.get_data(as_text=True) == base.get_data(as_text=True)


def test_t48_each_switch_on_phase_gates(client):
    """P2c 起前三键（F-82/F-83/F-84）解锁走真实现（200，diagnostics 出对应子键，
    详见 test_l2_specphot_diagnostics_p2c.py 的 T-49/T-50）。后三键 P3c 定态：
    z_from_lines/frame_probe 走真实现但 M-6 闸门后（200 + null 闸门块，键恒
    在场，F-94③；两态与数值详见 test_l2_specphot_p3bc.py）；sky_subtract 的
    消费点在 S3 步 1 的 U-31（F-86）⇒ 本路径 501 phase='P3c'（指路不冒充）。"""
    _login(client)
    for k in DIAG_SIX:
        r = client.post('/api/specphot/photometry',
                        json=dict(_upload_body(anchor_rows=ANCHOR18),
                                  diagnostics={k: True}))
        d = _json(r)
        if k in ('beta_matrix', 'resp_perturb', 'anchor_reinsert'):
            assert r.status_code == 200, k
            assert d['diagnostics'].get(k) is not None, k
        elif k == 'sky_subtract':
            assert r.status_code == 501, k
            assert d['code'] == 'feature_disabled' and d.get('phase') == 'P3c', k
        else:
            assert r.status_code == 200, k
            blk = d['diagnostics'].get(k)
            assert blk is not None and blk['verdict'] if k == 'z_from_lines' \
                else blk is not None and blk['frame_suggestion'] == 'inconclusive', k


# ═══ T-61 / T-62：代理去偏（F-97①）与双 j（F-97⑤）的估计量层判据 ═════════
# 口径注记（P1 收口汇报裁量项）：F-97① 的「管线级去偏」（σ̂/k_proxy 进 F-55
# 传播之前生效）与响应层双 j 回显键（两个 σ_px 并回显所取者）在当前实现中
# 随预处理切片细化（errors.rho_lag1 docstring 明示）；本组测试把**估计量本身**
# 与文献锚点钉死——管线去偏落地时这两组判据必须同时保持成立。

def _k_proxy(rho, j):
    """F-97①：k_proxy(ρ,j) = sqrt((6 − 8ρ^j + 2ρ^{2j})/6)（规格原文公式）。"""
    return math.sqrt((6.0 - 8.0 * rho ** j + 2.0 * rho ** (2 * j)) / 6.0)


def _ar1(rho, n, seed=ERR_SEED):
    rng = np.random.default_rng(seed)
    eps = rng.normal(0.0, 1.0, n)
    return lfilter([math.sqrt(1.0 - rho * rho)], [1.0, -rho], eps)


@pytest.mark.parametrize('rho,j', [(0.2, 1), (0.2, 2), (0.2, 3),
                                   (0.5, 1), (0.5, 2), (0.5, 3),
                                   (0.9, 1), (0.9, 2), (0.9, 3)])
def test_t61_second_diff_estimator_matches_k_proxy(rho, j):
    """T-61：合成 AR(1) 序列（σ_true 已知）上 σ̂/σ_true 与 k_proxy(ρ,j) 一致到
    1%；去偏（σ̂/k_proxy）后回收 σ_true 到 1% 以内。"""
    sigma_true = 1.3
    series = _ar1(rho, 400_003) * sigma_true
    est = ER.sigma_second_diff(series, j=j)
    assert est is not None
    assert est / sigma_true == pytest.approx(_k_proxy(rho, j), rel=0.01)
    assert est / _k_proxy(rho, j) == pytest.approx(sigma_true, rel=0.01)   # 去偏后回收


def test_t61_k_proxy_literature_anchors():
    """F-97① 钉死的三个数值锚点（4 位小数）。"""
    assert abs(_k_proxy(0.5, 1) - 0.6455) < 1e-4
    assert abs(_k_proxy(0.5, 2) - 0.8292) < 1e-4
    assert abs(_k_proxy(0.9, 1) - 0.2646) < 1e-4


def test_t62_white_noise_dual_j_indistinguishable():
    """T-62 的白噪声半段（与 T-34 配对）：真白噪声下 j=1 与 j=C_SIGMA_STRIDE
    两个估计不可区分（<3%），任取其一都回收 σ。"""
    rng = np.random.default_rng(ERR_SEED)
    series = rng.normal(0.0, 1.3, 400_003)
    j1 = ER.sigma_second_diff(series, j=1)
    j2 = ER.sigma_second_diff(series, j=C_SIGMA_STRIDE)
    assert abs(j1 - j2) < 0.03 * max(j1, j2)
    assert abs(j1 - 1.3) < 0.01 * 1.3


def test_t62_correlated_stride_estimate_systematically_larger():
    """T-62 的相关半段：|ρ|>C_RHO_MIN 时较大步长的估计系统性更大（实现取
    较大者语义的估计量层证据）；比值 ≈ k_proxy(ρ,2)/k_proxy(ρ,1)。"""
    series = _ar1(0.5, 400_003)
    j1 = ER.sigma_second_diff(series, j=1)
    j2 = ER.sigma_second_diff(series, j=C_SIGMA_STRIDE)
    assert j2 > j1
    assert j2 / j1 == pytest.approx(_k_proxy(0.5, 2) / _k_proxy(0.5, 1), rel=0.03)


# ─── F-97①⑤ 的管线级判据（响应层；估计量层判据见上两例） ─────────────────

def _body_n(n, series):
    # flux_unit 声明绝对刻度 ⇒ F-14 双闸判 direct（否则 auto 无锚点落到 model ⇒ 501）
    body = _upload_body(flux=[F0 * (5.0 + s) for s in series],
                        meta={'flux_unit': 'erg/s/cm^2/Angstrom'})
    body['spectrum']['lam_aa'] = [4000.0 + 5.0 * i for i in range(n)]
    return body


def test_f97_pipeline_correlated_debias_dual_j_echo_and_ca06(client):
    """F-97①⑤ 管线级（API-2 响应层）：|ρ̂|>C_RHO_MIN 的相关上传件——
    ① σ̂ 先按 k_proxy(ρ̂, j_used) 去偏再进 F-55 传播（去偏后双 j 都回收 σ_true）；
    ⑤ 顶层回显 sigma_px_j1/sigma_px_j2/sigma_stride_used，CA-06 呈现两值与所取者。"""
    _login(client)
    n = 120
    d = _post(client, _body_n(n, _ar1(0.5, n, seed=ERR_SEED)))
    assert d['spectrum_source'] == 'upload'
    rho = d['results'][0]['rho_lag1']
    assert rho is not None and abs(rho) > 0.2            # 相关支（AR(1) ρ=0.5）
    assert d['sigma_stride_used'] == 2                   # T-62：相关 ⇒ 取较大者=stride
    j1, j2 = d['sigma_px_j1'], d['sigma_px_j2']
    assert j1 is not None and j2 is not None
    assert j1 == pytest.approx(F0, rel=0.05)             # 去偏后回收 σ_true=F0
    assert j2 == pytest.approx(F0, rel=0.05)
    # 回显值是去偏后的：还原原始估计后 stride 支仍系统性更大（T-62 的管线侧证据）
    assert j2 * _k_proxy(rho, 2) > j1 * _k_proxy(rho, 1)
    w = next(w for w in d['results'][0]['warnings'] if w['code'] == 'CA-06')
    assert 'j=1:' in w['message'] and 'j=2:' in w['message'] and '取 j=2' in w['message']


def test_f97_pipeline_white_noise_j1_exemption(client):
    """T-62 白噪声豁免支（|ρ̂|≤C_RHO_MIN ⇒ 允许 j=1）：stride_used=1，双 j 回显
    在场且同谱一致。精度回收（<1%）是 400k 点估计量层判据（T-34/T-62 上两例）；
    n=120 的中位数估计量采样散布 ~15%，本例只判机制与量级。"""
    _login(client)
    rng = np.random.default_rng(ERR_SEED + 1)
    n = 120
    d = _post(client, _body_n(n, rng.normal(0.0, 1.0, n)))
    rho = d['results'][0]['rho_lag1']
    assert rho is not None and abs(rho) <= 0.2
    assert d['sigma_stride_used'] == 1
    j1, j2 = d['sigma_px_j1'], d['sigma_px_j2']
    assert j1 is not None and j2 is not None
    assert abs(j1 - j2) < 0.10 * max(j1, j2)     # 同谱双 j（去偏后）一致
    assert j1 == pytest.approx(F0, rel=0.25)


# ═══ T-63 / T-64（P1 结构扫描）：F-94 全覆盖契约的 S1 侧 ══════════════════

def test_t63_err_source_scope_word_list_and_pairing(client):
    """F-94①②（S1 结果行的机械扫描）：err_source{} 值 ⊆ 词表；err_scope{}
    值 ⊆ {stat, stat+cal}；每个 err_source/err_scope 键都有同名的量值键在场
    （禁止只出误差列）；非免配对通道的误差键在本次响应中必须有非 null 值。"""
    _login(client)
    d = _post(client, _upload_body(anchor_rows=ANCHOR18))
    assert d['results']
    for row in d['results']:
        es, sc = row['err_source'], row['err_scope']
        assert set(es) == set(sc)                            # 两映射同键集
        for k, v in es.items():
            assert v in (ERR_SOURCE_WORDS | {'count', 'metadata', 'none'}), (k, v)
            assert k in row, f'误差键 {k} 无配对量值键'
            if v in ERR_SOURCE_WORDS:
                assert row[k] is not None, (k, v)            # ② 值域键不得缺席
        for k, v in sc.items():
            assert v in ('stat', 'stat+cal'), (k, v)
        # 簿记键不得冒充量值通道
        for book in ('sigma_method', 'weighting', 'band_mode', 'mag_system'):
            assert book not in es


def test_t64_none_is_never_impersonated(client):
    """F-94③ / TXT-23：err_source[k]='none' ⇒ k_err 是 JSON null（不得 0、
    空串、NaN）且同处有书面原因警告。"""
    _login(client)
    d = _post(client, _upload_body(meta={'flux_unit': 'erg/s/cm^2/Angstrom'}))
    assert d['mode_effective'] == 'direct'
    row = d['results'][0]
    assert row['err_source']['mag_err_cal'] == 'none'
    assert row['mag_err_cal'] is None                        # 不是 0 / '' / NaN
    assert any('书面原因' in w.get('message', '') for w in row['warnings'])
    # NaN 冒充在 JSON 序列化层就不可能成立：响应可无歧义地 json 解析且该键为 null
    raw = json.loads(json.dumps(row, allow_nan=False))
    assert raw['mag_err_cal'] is None


# ═══ 收口自检：specphot 模块自身不产生对宿主数据的写路径 ══════════════════

def test_p1_specphot_source_has_no_write_calls():
    """代码级哨兵：backend/specphot 源码不得出现 commit/INSERT/UPDATE/DELETE/
    session.add 等写调用点（RO-1…RO-5 的静态半段；动态半段由 T-47 快照承载）。"""
    pat = re.compile(r"\.(commit|rollback|add|merge|delete)\(|"
                     r"INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM")
    root = os.path.join(_REPO_ROOT, 'backend', 'specphot')
    for fn in sorted(os.listdir(root)):
        if not fn.endswith('.py'):
            continue
        with open(os.path.join(root, fn), encoding='utf-8') as f:
            hits = [ln for ln, line in enumerate(f.read().splitlines(), 1)
                    if pat.search(line)]
        assert not hits, (fn, hits)
