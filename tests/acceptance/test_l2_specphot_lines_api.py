"""L2: specphot P3 切片 2 —— API-4（POST /line）+ Q-20/F-72③ 调和 + frame 闸门。

覆盖（02 §3.9 / §5.2.3 / §4.2 / §4.3 / §9 P3 行）：
  Q-18…Q-22/Q-23/Q-15/Q-27/Q-33/Q-35  API-4 请求校验拒绝族（显式 jsonify，禁 500）
  Q-20 vs F-72③ 调和                 wire reason='mask_conflict'（Q-20 字面）+
                                      details.mask_conflict_kind / f72_reason 承担
                                      F-72③/T-20 的类属可辨；两类 message 逐字保留
                                      F-72③ 措辞（"大气吸收带"/"天光发射线"）且互不相同、
                                      不得互相混用（W-25 末句同纪律）。**调和决定：
                                      Q-20 字面的"两类命中都拒"按 F-72③ 的分档语义
                                      实现**——发射×气辉、吸收×吸收带不拒（F-72③
                                      只拒发射×吸收带与吸收×气辉两支）。
  W-25①②（两条独立）                 velocity_output 强提交：line_frame 未核对 ⇒
                                      E-14 line_frame_unverified（线表侧文案）；
                                      谱级 lambda_frame='unknown' ⇒ E-14
                                      lambda_frame_unknown（谱级文案）；两 reason/
                                      文案互不相同且都不与 mask_conflict 混用；
                                      两者同违 ⇒ 线表侧先判（F-38 逐条系统差更根本）。
  M-6（T-51 skip 前提）               vel_fwhm_kms/vel_shift_kms/fwhm_intr_aa/z_fit
                                      恒 null + velocity_family_note 留位；
                                      frame_gates.vel_outputs_enabled=False。
  §4.2 LineResult 键集                值键与误差键同批在场（F-94⑤）；F-67 互斥空值；
                                      diagnostics.absorber_systems 恒在场（F-105①）。
  §3.9.3.1 ratio 行                   单线请求 ⇒ ratio/ratio_ref/ratio_err 恒 null +
                                      书面原因（notes）；不出现孤儿误差键。
  回收端到端                          合成高斯发射线（中心/EW/线流量）与吸收线
                                      （depth）真值回收；snr 门与 ST-3 缓存确定性。
  §4.3 谱线导出列序                   前端 export.js 的 L_COLS 与规格列序逐字一致
                                      （源码级断言，T-63⑤ 的列集冻结哨兵）。

测试只读（T-57 同一口径：上传件不落盘、不写库）；跑批以 C_MAX_BOOT 内小 n_boot
控时（ST-5 的 5 s 预算内）。
"""
import json
import os
import re
import sys

import numpy as np
import pytest

from app import create_app
from specphot import _result_cache, _cache_lock
from specphot.constants import C_BASE_ITER, C_BASE_SIDE, C_LINE_WIN, C_MASK_ABS_TABLE

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))   # sedfit/specphot 导入口径
_FE_ROOT = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _json(resp):
    return json.loads(resp.get_data(as_text=True))


def _login(client):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'p3-lines-tester'
        s['role'] = 'user'


def _clear_cache():
    with _cache_lock:
        _result_cache.clear()


# ─── 合成谱制造器 ─────────────────────────────────────────────────────────────
C0 = 1e-14          # 连续谱 Fλ cgs
RNG = np.random.default_rng(2026)


def _flat_spec(lo, hi, step=1.0, frame=None):
    lam = np.arange(lo, hi + step / 2, step)
    noise = RNG.normal(0, 0.005, lam.size)
    spec = {'lam_aa': np.round(lam, 4).tolist(),
            'flux': (C0 * (1 + noise)).tolist(),
            'flux_err': [C0 * 0.005] * lam.size,
            'meta': {'z': 0.0}}
    if frame:
        spec['meta']['lambda_frame'] = frame
    return spec


def _emission_spec(mu=5007.0, amp=3e-15, sig=5.0, lo=4400.0, hi=5600.0, **kw):
    """高斯发射线：EW 真值 = amp·√(2π)·σ / C0（λ 空间解析积分核）。"""
    lam = np.arange(lo, hi + 0.5, 1.0)
    flux = C0 + amp * np.exp(-0.5 * ((lam - mu) / sig) ** 2)
    noise = RNG.normal(0, 0.003, lam.size)
    spec = {'lam_aa': lam.tolist(), 'flux': (flux * (1 + noise)).tolist(),
            'flux_err': (flux * 0.003).tolist(), 'meta': {'z': 0.0}}
    spec.update(kw)
    return spec, amp * np.sqrt(2 * np.pi) * sig / C0


def _absorption_spec(mu=5200.0, depth=0.4, sig=6.0, lo=4400.0, hi=5600.0, **kw):
    lam = np.arange(lo, hi + 0.5, 1.0)
    prof = 1.0 - depth * np.exp(-0.5 * ((lam - mu) / sig) ** 2)
    flux = C0 * prof
    noise = RNG.normal(0, 0.003, lam.size)
    spec = {'lam_aa': lam.tolist(), 'flux': (flux * (1 + noise)).tolist(),
            'flux_err': (flux * 0.003).tolist(), 'meta': {'z': 0.0}}
    spec.update(kw)
    return spec


def _post(client, body):
    r = client.post('/api/specphot/line', json=body)
    return r, _json(r)


def _line_body(spec, lam_rest, kind, **over):
    body = {'spectrum': spec,
            'line': {'species': 'Test', 'lambda_rest_aa': lam_rest,
                     'id_table': 'spec_lines.js#h'},
            'line_kind': kind, 'n_boot': 16, 'err_seed': 7,
            'mw': {'correct': False}}
    body.update(over)
    return body


# ─── 1. 请求校验拒绝族（Q-18…Q-22/Q-23/Q-15/Q-27/Q-33/Q-35） ─────────────────

def test_api4_validation_rejections(client):
    _login(client)
    _clear_cache()
    spec, _ = _emission_spec()
    good = _line_body(spec, 5007.0, 'emission')
    cases = [   # (覆盖, 期望 http, 期望 code/reason/error 子串)
        ({'spectrum': None}, 400, 'source_ambiguous'),                    # Q-23 双缺
        ({'spectrum': spec, 'spectrum_id': 1}, 400, 'source_ambiguous'),  # Q-23 双给
        ({'line_kind': None}, 400, 'line_kind_required'),                 # Q-18/E-09
        ({'line_kind': 'both'}, 400, 'line_kind_required'),               # Q-18 词表
        ({'line': {'lambda_rest_aa': 0}}, 400, 'lambda_rest_aa'),         # F-34 线心
        ({'line': {}}, 400, 'lambda_rest_aa'),
        ({'window_halfwidth_aa': 0}, 400, 'window_halfwidth_aa'),
        ({'window_halfwidth_aa': 10 * C_LINE_WIN + 1}, 400, 'window_halfwidth_aa'),
        ({'baseline': {'order': -1}}, 400, 'baseline.order'),             # Q-15/F-62④
        ({'baseline': {'order': 8}}, 400, 'baseline.order'),
        ({'baseline': {'order': 1, 'side_px': 5}}, 400, 'C_BASE_SIDE'),   # 钉死值显式拒
        ({'baseline': {'order': 1, 'iter': 1}}, 400, 'C_BASE_ITER'),
        ({'profile': 'moffat'}, 400, 'profile'),                          # U-26 词表
        ({'profile': 'voigt'}, 400, 'voigt'),                             # Q-21/F-39/T-22
        # sky_handling='subtract' 已随 P3c 真解锁（F-86/U-31）⇒ 200 走真实现，
        # 其两态（成功扣除 / CA-37 退回 mask）由 test_l2_specphot_p3c.py 承载
        ({'sky_handling': 'ignore'}, 400, 'sky_handling'),
        ({'line_frame': 'airish'}, 400, 'line_frame'),                    # F-38 词表
        ({'z': -0.1}, 400, 'z'),                                          # Q-12
        ({'n_boot': 0}, 400, 'n_boot'),                                   # Q-15
        ({'n_boot': 1001}, 400, 'n_boot'),
        ({'err_seed': 1.5}, 400, 'err_seed'),                             # Q-35
        ({'velocity_output': 'yes'}, 400, 'velocity_output'),
        ({'diagnostics': {'absorber_ident': 'yes'}}, 400, '布尔'),        # Q-27
        # U-48 随 P3d 真实现（501 已拆）：absorber_ident=True 合法（走闸门格）；
        # 联动闸（W-35/U-48）：未开 absorber_ident 时另两项禁用 ⇒ E-14
        ({'diagnostics': {'wing_logn': True}}, 400, 'u48_requires_absorber_ident'),
        ({'diagnostics': {'metal_sat_check': True}}, 400, 'u48_requires_absorber_ident'),
        ({'mask': '4860-4870'}, 400, 'mask'),
        ({'preprocess': {'factor': 5}}, 400, 'factor'),                   # Q-33
    ]
    for over, http, needle in cases:
        body = dict(good)
        body.update(over)
        r, d = _post(client, body)
        assert r.status_code == http, (over, r.status_code, d)
        assert d['code'] in ('bad_request_state', 'metadata_required',
                             'feature_disabled'), (over, d)
        blob = json.dumps(d, ensure_ascii=False)
        assert needle in blob, (over, needle, blob[:300])
    # E-09 的 wire code = metadata_required（§5.3），reason 钉死 line_kind_required
    body = dict(good); body.pop('line_kind')
    r, d = _post(client, body)
    assert r.status_code == 400 and d['code'] == 'metadata_required' \
        and d['reason'] == 'line_kind_required'


def test_api4_rejects_unusable_sigma(client):
    """零噪声完美谱：误差列判定退化 + 代理 σ ≈ 0 ⇒ 显式拒（不得以 0 冒充，TXT-23）。"""
    _login(client)
    lam = np.arange(4900.0, 5100.0, 1.0)
    spec = {'lam_aa': lam.tolist(), 'flux': (C0 * np.ones(lam.size)).tolist(),
            'flux_err': None, 'meta': {}}
    r, d = _post(client, _line_body(spec, 5007.0, 'emission'))
    assert r.status_code == 400 and d['reason'] == 'sigma_unavailable'


# ─── 2. Q-20 vs F-72③ 调和（本切片的显式决定，三层各按其源文档断言） ─────────

def test_q20_f72_3_reconciliation_two_kinds(client):
    """调和决定：wire reason 统一 mask_conflict（Q-20 字面），类属由 details 的
    mask_conflict_kind/f72_reason 承担（F-72③/T-20 类属可辨），message 逐字保留
    F-72③ 的两类措辞且互不相同（措辞不得混用，W-25 末句同纪律）。"""
    _login(client)
    _clear_cache()
    # 发射 × 大气吸收带（O2 B 带 6867–6884）⇒ 拒（F-72③ 第一支）
    spec = _flat_spec(6200.0, 7600.0)
    r1, d1 = _post(client, _line_body(spec, 6875.0, 'emission'))
    assert r1.status_code == 400
    assert d1['code'] == 'bad_request_state' and d1['reason'] == 'mask_conflict'   # Q-20 字面
    assert d1['mask_conflict_kind'] == 'abs_band'                                  # F-72③ 类属
    assert d1['f72_reason'] == 'line_center_in_abs_band'
    assert '大气吸收带' in d1['error'] and '天光发射' not in d1['error']           # T-20 措辞
    # 吸收 × 气辉线（[O I] 5577）⇒ 拒（F-72③ 第二支）
    spec2 = _flat_spec(5000.0, 6200.0)
    r2, d2 = _post(client, _line_body(spec2, 5577.0, 'absorption'))
    assert r2.status_code == 400
    assert d2['reason'] == 'mask_conflict' and d2['mask_conflict_kind'] == 'skyline'
    assert d2['f72_reason'] == 'line_center_in_skyline'
    assert '天光发射线' in d2['error'] and '大气吸收带' not in d2['error']
    # 两类 message 互不相同（措辞不得混用），且都不等于 reason 的合并表述
    assert d1['error'] != d2['error']


def test_f72_3_non_rejected_combinations(client):
    """调和决定的分档语义（Q-20 字面的 OR 拒按 F-72③ 分档实现）：
    发射×气辉 与 吸收×吸收带 **不拒**——只可能因掩膜剔除/信噪走其它 reason。"""
    _login(client)
    _clear_cache()
    # 发射 × 气辉：线心 6420 Å，窗 [6380,6460] 与 [O I] 6363 掩膜（6323–6403）
    # 部分重叠 ⇒ 掩膜段被剔除但窗口不为空 ⇒ 正常测量（200），绝不以 mask_conflict 拒
    lam = np.arange(6100.0, 6600.0, 1.0)
    flux = C0 + 4e-15 * np.exp(-0.5 * ((lam - 6420.0) / 4.0) ** 2)
    noise = RNG.normal(0, 0.003, lam.size)
    spec = {'lam_aa': lam.tolist(), 'flux': (flux * (1 + noise)).tolist(),
            'flux_err': (flux * 0.003).tolist(), 'meta': {}}
    r, d = _post(client, _line_body(spec, 6420.0, 'emission'))
    assert r.status_code == 200, d
    assert d['lines'][0]['detected'] is True
    assert d['lines'][0]['lambda_obs_vac_aa'] == pytest.approx(6420.0, abs=0.3)
    assert all(w.get('reason') != 'mask_conflict' for w in d['warnings'])
    # 窗内确有被剔的气辉段（分档语义：剔除了但不拒测）
    assert any(s['reason'].startswith('因天光发射排除')
               for s in d['lines'][0]['excluded_segments'])
    # 发射 × 气辉（整窗被剔光）：F-72③ 不拒测，但窗口为空 ⇒ E-04 too_few_pixels
    spec2 = _flat_spec(5300.0, 5900.0)
    r2, d2 = _post(client, _line_body(spec2, 5577.0, 'emission'))
    assert r2.status_code == 400 and d2['code'] == 'band_unusable' \
        and 'too_few_pixels' in d2['reason']
    # 吸收 × 吸收带：F-72③ 只拒发射×吸收带，吸收线允许在大气吸收带内测
    lam3 = np.arange(6500.0, 7200.0, 1.0)
    prof = 1.0 - 0.3 * np.exp(-0.5 * ((lam3 - 6900.0) / 8.0) ** 2)
    noise3 = RNG.normal(0, 0.003, lam3.size)
    spec3 = {'lam_aa': lam3.tolist(), 'flux': (C0 * prof * (1 + noise3)).tolist(),
             'flux_err': (C0 * prof * 0.003).tolist(), 'meta': {}}
    r3, d3 = _post(client, _line_body(spec3, 6900.0, 'absorption'))
    assert r3.status_code == 200, d3
    assert d3['lines'][0]['line_kind'] == 'absorption'
    # 掩膜段在响应里逐段带类属措辞（F-35：因大气吸收排除/因天光发射排除）
    for seg in d3['lines'][0]['excluded_segments']:
        assert seg['reason'].startswith(('因大气吸收排除', '因天光发射排除'))


def test_q20_z_shifted_center_checked(client):
    """Q-20「含 z 平移后」：z 平移把线心推进大气吸收带 ⇒ 同样拒（F-72③ 红移支）。"""
    _login(client)
    spec = _flat_spec(6200.0, 7600.0)
    body = _line_body(spec, 6300.0, 'emission', z=0.09)   # 6300·1.09 = 6867 → O2 B 带边
    lam = np.asarray(body['spectrum']['lam_aa'])
    if not (6867.0 <= 6300.0 * 1.09 <= 6884.0):
        pytest.skip('z 平移未落带内（表值变更时调整算例）')
    r, d = _post(client, body)
    assert r.status_code == 400 and d['reason'] == 'mask_conflict' \
        and d['mask_conflict_kind'] == 'abs_band'


# ─── 3. 端到端真值回收（合成谱上传，与 API-2/3 同一装载管线） ─────────────────

def test_emission_line_recovery(client):
    _login(client)
    _clear_cache()
    mu, amp, sig = 5007.0, 3e-15, 5.0
    spec, ew_true = _emission_spec(mu=mu, amp=amp, sig=sig)
    r, d = _post(client, _line_body(spec, mu, 'emission'))
    assert r.status_code == 200, d
    row = d['lines'][0]
    assert row['detected'] is True
    assert abs(row['lambda_obs_vac_aa'] - mu) < 0.2            # 线心回收
    assert abs(row['ew_obs_aa'] - ew_true) / ew_true < 0.05    # EW 回收（闭式积分核）
    flux_true = amp * np.sqrt(2 * np.pi) * sig
    assert abs(row['line_flux'] - flux_true) / flux_true < 0.05
    assert row['line_flux_err'] and row['line_flux_err'] > 0   # F-94⑤ 值键+误差键同批
    assert row['ew_err_aa'] and row['ew_err_aa'] > 0
    assert row['ew_err_form'] == 'two_term'
    terms = row['ew_err_terms']
    assert set(terms) == {'photon', 'continuum', 'continuum_coherent'}
    assert row['err_source']['ew_obs_aa'] in ('closed_form', 'bootstrap')
    assert row['err_source']['line_flux'] in ('closed_form', 'bootstrap')
    assert d['lambda_center_obs_aa'] == pytest.approx(mu)
    # §3.9.3.1 单线请求：ratio 族恒 null + 书面原因（不得出现孤儿误差键）
    assert row['ratio'] is None and row['ratio_ref'] is None and row['ratio_err'] is None
    assert any('线比' in n for n in row['notes'])
    # 柱密度族：M-6 振子强度库未备 ⇒ null + 书面原因（F-71）
    assert row['column_density'] is None and row['f_oscillator'] is None
    assert any('柱密度' in n for n in row['notes'])


def test_absorption_line_depth_recovery(client):
    _login(client)
    spec = _absorption_spec(mu=5200.0, depth=0.4, sig=6.0)
    r, d = _post(client, _line_body(spec, 5200.0, 'absorption'))
    assert r.status_code == 200, d
    row = d['lines'][0]
    assert row['detected'] is True
    assert abs(row['depth'] - 0.4) < 0.02                       # depth 回收（比值量）
    assert row['ew_signed_aa'] < 0                              # F-67 吸收恒负
    assert abs(abs(row['ew_signed_aa']) - row['ew_obs_aa']) < 1e-12
    # F-67 互斥空值：吸收 line_flux 恒 null（写 0/负值都算实现错误）、发射 depth 恒 null
    assert row['line_flux'] is None and row['line_flux_err'] is None \
        and row['line_flux_err_terms'] is None
    em_spec, _ = _emission_spec()
    _, d2 = _post(client, _line_body(em_spec, 5007.0, 'emission'))
    row2 = d2['lines'][0]
    assert row2['depth'] is None and row2['depth_err_lo'] is None \
        and row2['depth_err_hi'] is None


def test_snr_gate_nondetect_upper_limit(client):
    """F-68/Q-19：snr_res < C_LINE_SNR_MIN ⇒ 不拟合，200 + 未检出 + 3σ 上限。"""
    _login(client)
    lam = np.arange(4800.0, 5200.0, 1.0)
    flux = C0 * (1 + RNG.normal(0, 1.0, lam.size))              # 信噪极低（≈√3×1σ）
    spec = {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': (flux * 1.0).tolist(), 'meta': {}}
    r, d = _post(client, _line_body(spec, 5007.0, 'emission'))
    assert r.status_code == 200, d
    row = d['lines'][0]
    assert row['detected'] is False
    assert row['upper_limit_3sigma'] and row['upper_limit_3sigma'] > 0
    assert row['ew_obs_aa'] is None and row['lambda_obs_vac_aa'] is None
    assert row['err_source']['upper_limit_3sigma'] == 'count'   # 免配对通道照常登记
    assert any(w.get('reason') == 'snr_res_below_gate' for w in row['warnings'])


# ─── 4. frame 闸门（W-25 两条独立）与 M-6 留位 ───────────────────────────────

def test_m6_velocity_family_null_and_gate_structure(client):
    _login(client)
    spec, _ = _emission_spec()
    r, d = _post(client, _line_body(spec, 5007.0, 'emission'))
    assert r.status_code == 200, d
    row = d['lines'][0]
    for k in ('vel_fwhm_kms', 'vel_shift_kms', 'z_fit', 'fwhm_intr_aa'):
        assert row[k] is None, k                                # M-6：恒 null（T-51 skip 前提）
    assert row['velocity_family_note']                          # 留位理由在场
    g = d['frame_gates']
    assert g['vel_outputs_enabled'] is False
    assert g['w25_1_line_side']['line_frame'] is None
    assert g['w25_1_line_side']['verified'] is False
    assert g['w25_1_line_side']['reason'] == 'line_frame_unverified'
    assert g['w25_2_spectrum_side']['blocked'] is False         # 谱级 vacuum ⇒ 不拦
    # line_frame 核对后回显（F-38 逐条）
    body = _line_body(spec, 5007.0, 'emission', line_frame='vacuum')
    _, d2 = _post(client, body)
    assert d2['lines'][0]['line_frame'] == 'vacuum'
    assert d2['frame_gates']['w25_1_line_side']['verified'] is True


def test_w25_strong_submit_two_independent_gates(client):
    """velocity_output 强提交：①线表侧 line_frame_unverified（线表文案）与
    ②谱级 lambda_frame_unknown（谱的波长轴文案）互不相同、都不与 mask_conflict
    混用；①②同违时线表侧先判（F-38 逐条系统差更根本）。"""
    _login(client)
    spec, _ = _emission_spec()
    # ①：line_frame null（默认）
    r1, d1 = _post(client, _line_body(spec, 5007.0, 'emission', velocity_output=True))
    assert r1.status_code == 400
    assert d1['reason'] == 'line_frame_unverified'
    assert '线表' in d1['error'] and 'lambda_frame' not in d1['reason']
    # ②：line_frame 已核对、谱级 unknown
    spec_u = _emission_spec()[0]
    spec_u['meta']['lambda_frame'] = 'unknown'
    r2, d2 = _post(client, _line_body(spec_u, 5007.0, 'emission',
                                      velocity_output=True, line_frame='vacuum'))
    assert r2.status_code == 400
    assert d2['reason'] == 'lambda_frame_unknown'
    # 文案主体说谱的波长轴（②谱级），并显式声明与线表侧是两道独立的门（不合并）
    assert '波长轴' in d2['error'] and '谱级' in d2['error'] \
        and '两道独立的门' in d2['error']
    # 两支 reason/文案互不相同，且都不与 mask_conflict 混用（W-25 末句）
    assert d1['reason'] != d2['reason'] and d1['error'] != d2['error']
    assert d1['reason'] != 'mask_conflict' and d2['reason'] != 'mask_conflict'
    # ①②同违 ⇒ 线表侧先判
    spec_u2 = _emission_spec()[0]
    spec_u2['meta']['lambda_frame'] = 'unknown'
    r3, d3 = _post(client, _line_body(spec_u2, 5007.0, 'emission', velocity_output=True))
    assert r3.status_code == 400 and d3['reason'] == 'line_frame_unverified'


# ─── 5. §4.2 LineResult 键集 + ST-3 缓存确定性 ───────────────────────────────

_42_KEYS = {
    # 身份与帧
    'line_kind', 'species', 'lambda_rest_vac_aa', 'lambda_obs_vac_aa', 'line_frame',
    'model', 'params', 'fwhm_obs_aa', 'fwhm_intr_aa', 'vel_fwhm_kms', 'vel_shift_kms',
    'r_source', 'line_flux', 'depth',
    # EW 族（F-94⑤ 补齐集：值键与误差键同批）
    'ew_signed_aa', 'ew_obs_aa', 'ew_rest_aa', 'ew_err_aa',
    'lambda_err_aa', 'vel_shift_err_kms', 'fwhm_obs_err_aa', 'fwhm_intr_err_aa',
    'vel_fwhm_err_kms', 'ew_rest_err_aa', 'line_flux_err', 'depth_err_lo',
    'depth_err_hi', 'ratio', 'ratio_ref', 'ratio_err', 'column_density_err',
    # 检出门与柱密度族
    'snr_res', 'snr_def', 'detected', 'upper_limit_3sigma', 'column_density',
    'column_density_lower_bound', 'f_oscillator', 'f_source',
    # 口径与簿记
    'sky_subtracted', 'baseline', 'baseline_type', 'mask_applied', 'notes',
    'err_source', 'err_scope', 'ew_err_form', 'ew_err_terms', 'engine',
    'engine_status', 'cov_method', 'rho_used', 'cond_G', 'infl',
    'sigma_theta_raw', 'err_seed', 'n_boot', 'rng_algo', 'warnings',
    # P3b/P3c 新增簿记键（恒在场）：F-86 扣除信息块（mask 路径 null）、
    # F-58 误差预算外分量下限集、F-95① 的 depth 梯度口径（voigt=fd）
    'sky_subtract', 'not_in_budget', 'depth_err_delta',
}


def test_line_result_key_set_and_top_level(client):
    _login(client)
    _clear_cache()
    spec, _ = _emission_spec()
    r, d = _post(client, _line_body(spec, 5007.0, 'emission'))
    assert r.status_code == 200, d
    row = d['lines'][0]
    missing = _42_KEYS - set(row)
    assert not missing, f'§4.2 LineResult 缺键：{sorted(missing)}'
    assert set(row['baseline']) >= {'type', 'order', 'cond_2', 'n_nodes'}
    assert set(row['mask_applied']) == {'abs_table', 'emis_table'}
    # 谱级契约：双哈希 + 框架闸门结构 + diagnostics 占位恒在场（F-105①）
    for k in ('spec_phot_version', 'spectrum_source', 'spec_hash', 'mask_hash',
              'preprocess', 'frame_gates', 'lines', 'diagnostics', 'warnings'):
        assert k in d, k
    # F-105①/T-72①②：五键恒在场（P3d 评审 P1-3 追加 absorber_system_masked，
    # §4.2 键位）；全关（未带 diagnostics）时 systems 空数组、forest_stats 恒
    # null、reasons 闭集非空、cross_link_gate 取"未启用"值、顶层掩膜并集空
    assert set(d['diagnostics']) == {'absorber_systems', 'forest_stats',
                                     'forest_stats_reasons', 'cross_link_gate',
                                     'absorber_system_masked'}
    assert d['diagnostics']['absorber_systems'] == []
    assert d['diagnostics']['absorber_system_masked'] == []
    assert d['diagnostics']['forest_stats'] is None
    assert d['diagnostics']['forest_stats_reasons'] and \
        set(d['diagnostics']['forest_stats_reasons']) <= {
            'single_sightline', 'continuum_unknown', 'resolution_below_gate',
            'lls_stochastic'}
    assert d['diagnostics']['cross_link_gate']['enabled'] is False
    assert d['mask_hash'] and d['preprocess']['preprocess_hash']
    assert 'line_requested' in d and d['line_requested']['lambda_rest_aa'] == 5007.0


def test_cache_determinism_and_seed(client):
    """ST-3：同请求逐字节命中缓存；err_seed 换值 ⇒ 数值路径同、seed 回显不同（Q-35）。"""
    _login(client)
    _clear_cache()
    spec, _ = _emission_spec()
    body = _line_body(spec, 5007.0, 'emission', n_boot=8)
    r1, d1 = _post(client, body)
    r2, d2 = _post(client, dict(body))
    assert r1.get_data() == r2.get_data()                       # 缓存逐字节同
    b3 = dict(body); b3['err_seed'] = 99
    _, d3 = _post(client, b3)
    assert d3['lines'][0]['err_seed'] == 99 and d1['lines'][0]['err_seed'] == 7
    # err_seed=null ⇒ spec_hash 派生并回显实际值（Q-35①；pcg64 构造式）
    b4 = dict(body); b4.pop('err_seed')
    _, d4 = _post(client, b4)
    assert isinstance(d4['lines'][0]['err_seed'], int)
    assert d4['lines'][0]['rng_algo'] == 'pcg64'


# ─── 6. §4.3 谱线导出列序（前端 export.js，源码级冻结断言） ──────────────────

_43_LINE_COLS = (
    'species, line_kind, lambda_rest_vac_aa, lambda_obs_vac_aa, line_frame, model, '
    'ew_signed_aa, ew_obs_aa, ew_rest_aa, ew_err_aa, line_flux, depth, fwhm_obs_aa, '
    'fwhm_intr_aa, vel_fwhm_kms, r_source, snr_res, detected, upper_limit_3sigma, '
    'column_density, column_density_lower_bound, f_oscillator, f_source, '
    'sky_subtracted, baseline_type, mask_applied_abs, mask_applied_emis, snr_def, '
    'notes, lambda_err_aa, vel_shift_err_kms, fwhm_obs_err_aa, fwhm_intr_err_aa, '
    'vel_fwhm_err_kms, ew_rest_err_aa, line_flux_err, depth_err_lo, depth_err_hi, '
    'ratio, ratio_ref, ratio_err, column_density_err, ew_err_form, ew_err_photon, '
    'ew_err_continuum, ew_err_continuum_coherent, err_source, err_scope, '
    'engine_status, cov_method, spec_phot_version'
)


def test_export_lines_csv_column_order_frozen():
    """§4.3 第二块列序逐字（发布即冻结，F-45）；TXT-11/TXT-23 头与双哈希在场。"""
    with open(os.path.join(_FE_ROOT, 'export.js'), encoding='utf-8') as fh:
        src = fh.read()
    m = re.search(r'const L_COLS = \[(.*?)\];', src, re.S)
    assert m, 'export.js 缺 L_COLS（§4.3 谱线导出块）'
    cols = re.findall(r"'([^']+)'", m.group(1))
    assert cols == [c.strip() for c in _43_LINE_COLS.split(',')], \
        '§4.3 谱线导出列序漂移（F-45：新增列只能追加在 spec_phot_version 之前）'
    assert 'exportLinesCsv' in src
    assert 'TXT11' in src and 'TXT23' in src            # 首行声明 + 表头误差口径
    assert 'mask_hash' in src and 'preprocess_hash' in src   # 双哈希进 # 头
    # W-25 两条闸门在导出件头各占一行、不得合并表述
    assert 'W-25①' in src and 'W-25②' in src


def test_frontend_s3_tab_enabled_and_wizard_present():
    """S3 页签解禁（U-03）+ 三步向导 + vel_* 控件禁用（M-6）+ E-09 闸（W-23）。"""
    with open(os.path.join(_FE_ROOT, 'workbench.js'), encoding='utf-8') as fh:
        wb = fh.read()
    assert "data-disabled" not in wb.split("item('s3'")[1].split('}')[0], \
        'S3 页签仍禁用（P3 切片 2 起解禁）'
    assert 'lines_ui.js' in wb
    with open(os.path.join(_FE_ROOT, 'lines_ui.js'), encoding='utf-8') as fh:
        ui = fh.read()
    for needle in ("'选线区'", "'定基线'", "'拟合轮廓'",      # U-23 三步
                   'line_kind_required' if 'line_kind_required' in ui else 'E-09',
                   'sp-lkind',                                # U-30 线型必选
                   'sp-lvel',                                 # vel_* 控件禁用位
                   'M-6', 'W-25', 'TXT-8', 'TXT-15',
                   'exportLinesCsv'):
        assert needle in ui, needle
    assert "disabled'> velocity_output" in ui or 'disabled" disabled' in ui \
        or re.search(r'checkbox[^>]*disabled', ui)
    # 掩膜冲突双 reason 词表在前端不得出现合并表述（mask_conflict 只属后端 wire 层）
