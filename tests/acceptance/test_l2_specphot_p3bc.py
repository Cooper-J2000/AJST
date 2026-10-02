"""L2: specphot **P3b/P3c 收口验收**——voigt 闸两态 + 线侧诊断三件（02 §9
P3b/P3c 行 / F-39 / F-81 / F-85 / F-86 / T-22 / T-51 / W-32 后半）。

交付形态（本期裁定，登记 docs/TECHNICAL.md）：M-4（仪器 R）与 M-6（线表帧与
f 值）两个前置在库内数据上均未满足 ⇒
  - voigt（F-39）：代码就位、闸门后——无 R ⇒ E14 voigt_disabled_no_r（前端
    U-26 保持禁用 + tooltip 前置 M-4）；R 可得（测试合成 R，非库内数据）⇒
    数值路径可达。
  - z_from_lines / frame_probe（F-81/F-85）：代码就位、闸门后——M-6 复核表
    缺位（registry.M6_LINE_TABLE / M6_FRAME_FEATURES = None，库内现状）⇒
    200 + null 闸门块（键恒在场，F-94③ none 语义）；lambda_frame='unknown'
    ⇒ E-14（T-51）。数值路径用**测试内合成 fixture**（monkeypatch registry
    两键）模拟「M-6 复核态」驱动——fixture 是测试内合成，不是伪造库内线表。
  - sky_subtract（F-86）：**真解锁**——只依赖 C_MASK_EMIS_TABLE 常量与谱数据
    （不依赖 M-6 线表帧/f 值，F-72②/U-31），T-51 末支两态全测。

T-22（原文：无 R 时 voigt 前端禁用且后端返回 E-14）——两半全测、不 skip。
T-51（原文四支）——逐支按合成口径全测、不 skip；真实 M-6 复核线表落地后的
全文重跑登记为 skip 占位（test_t51_rerun_on_m6_reviewed_table）。
W-32 后半：恒等（单开只增 diagnostics 子键）、前端源码级扫描（M-6/U-31 徽章
与 tooltip、U-26/U-31 定态）。

测试只读（上传件不落盘、无 DB 写）；缓存逐例清理（ST-3）。
"""
import json
import math
import os
import sys

import numpy as np
import pytest

from app import create_app

import specphot
from specphot import registry as _reg

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONT_SPECPHOT = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))

import wavconvert

C0 = 1e-14
SEED = 20260927

# ─── photometry 请求工装（anchored 单自定义波段；诊断块不依赖测光数值） ──────
_CC = {'band': 'test-p3bc', 'lam_aa': [5450.0, 6200.0, 6950.0],
       't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}


def _phot_body(spec, diag):
    return {'spectrum': spec, 'bands': ['test-p3bc'],
            'custom_curves': [dict(_CC)], 'weighting': 'photon',
            'anchor_rows': [{'band': 'test-p3bc', 'mag': 16.0,
                             'mag_system': 'AB', 'mag_err': 0.05,
                             'mjd': 60000.0}],
            'diagnostics': diag}



@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _json(resp):
    return json.loads(resp.get_data(as_text=True))


def _login(client, name='p3bc-tester'):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = name
        s['role'] = 'user'


def _clear_cache():
    with specphot._cache_lock:
        specphot._result_cache.clear()


def _post(client, body):
    _clear_cache()
    return client.post('/api/specphot/line', json=body)


# ─── 合成谱工装 ───────────────────────────────────────────────────────────────

def _emission_spec(mu=5007.0, amp=3e-15, sig=5.0, lo=4400.0, hi=5600.0,
                   step=1.0, seed=SEED, snr_factor=0.003, **meta):
    lam = np.arange(lo, hi + step / 2, step)
    rng = np.random.default_rng(seed)
    flux = C0 + amp * np.exp(-0.5 * ((lam - mu) / sig) ** 2)
    flux = flux * (1 + rng.normal(0, snr_factor, lam.size))
    m = {'z': 0.0}
    m.update(meta)
    return {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': (flux * snr_factor).tolist(), 'meta': m}


def _line_body(spec, lam_rest, kind='emission', **over):
    body = {'spectrum': spec,
            'line': {'species': 'Test', 'lambda_rest_aa': lam_rest,
                     'id_table': 'spec_lines.js#h'},
            'line_kind': kind, 'n_boot': 8, 'err_seed': 7,
            'mw': {'correct': False}}
    body.update(over)
    return body


# ═══ T-22：无 R 时 voigt 前端禁用且后端返回 E-14（F-39）══════════════════════

def test_t22_backend_voigt_without_r_e14(client):
    _login(client)
    spec = _emission_spec()
    r = _post(client, _line_body(spec, 5007.0, profile='voigt'))
    d = _json(r)
    assert r.status_code == 400 and d['code'] == 'bad_request_state'
    assert d['reason'] == 'voigt_disabled_no_r'
    assert 'F-39' in d['error'] and 'TXT-8' in d['error']
    # 词表内的 voigt + 合成 R（fixture，非库内数据）⇒ 数值路径可达（解锁态）
    spec_v = _emission_spec(lo=6300.0, hi=6700.0)
    lam = np.asarray(spec_v['lam_aa'])
    rng = np.random.default_rng(3)
    u = np.log(lam)
    from specphot import lines as LN
    prof = LN._voigt_profile(u - math.log(6500.0), 4e-4, 2e-4)
    fv = (C0 * (1 + 0.003 * rng.normal(0, 1, lam.size))
          + 3e-15 * prof)
    spec_v = {'lam_aa': lam.tolist(), 'flux': fv.tolist(),
              'flux_err': (abs(fv) * 0.003).tolist(), 'meta': {'z': 0.0}}
    r2 = _post(client, _line_body(spec_v, 6500.0, profile='voigt', R=5000.0))
    d2 = _json(r2)
    assert r2.status_code == 200, d2
    row = d2['lines'][0]
    assert row['model'] == 'voigt' and row['r_source'] == 'user'
    assert abs(row['lambda_obs_vac_aa'] - 6500.0) < 2.0
    assert {'A0', 'u00', 'w0', 'g0'} <= set(row['params'])


def test_t22_frontend_voigt_disabled_m4_tooltip():
    """T-22 前端半支（源码级）：U-26 的 voigt 单选渲染但 disabled，tooltip 写明
    前置 M-4 与 F-39 拒绝语义。"""
    with open(os.path.join(FRONT_SPECPHOT, 'lines_ui.js'), encoding='utf-8') as f:
        ui = f.read()
    assert 'const PROFILES' in ui and 'voigt' not in \
        ui.split('const PROFILES', 1)[1].split(';', 1)[0]      # 可选集无 voigt
    i = ui.index('<input type="radio" disabled>voigt')          # 渲染但禁用
    label = ui[max(0, i - 400):i]
    assert 'M-4' in label and 'F-39' in label and 'disabled' in label


# ═══ F-81 z_from_lines：M-6 闸两态 + CA-36（API-2/3 photometry 路径）═════════

Z_TRUE = 0.031
_REST_LINES = [4861.33, 4958.91, 5006.84, 5875.60, 6562.80]


def _z_spec(z=Z_TRUE, meta_z=None, lo=4200.0, hi=7200.0, step=1.0, seed=SEED,
            noise=3e-16):
    """meta_z 与线位 z 分离：CA-36 用例 = 线位在 z、所用 z（meta）在别处。"""
    lam = np.arange(lo, hi + step / 2, step)
    rng = np.random.default_rng(seed)
    flux = np.full(lam.size, C0)
    for r in _REST_LINES:
        flux = flux + 5e-15 * np.exp(-0.5 * ((lam - r * (1 + z)) / 2.5) ** 2)
    flux = flux + rng.normal(0, noise, lam.size)
    return {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': None, 'meta': {'z': (z if meta_z is None else meta_z),
                                       'category': 'other',
                                       'mjd': 60000.0}}


def _m6_line_table():
    return [{'species': 'L%d' % i, 'lambda0_rest_aa': r, 'line_frame': 'vacuum',
             'f_source': 'synthetic-fixture'} for i, r in enumerate(_REST_LINES)]


def test_f81_gate_block_library_default(client):
    """闸门态（库内现状，M-6 未复核）：200 + null 闸门块，键恒在场；z 未被
    改写（U-37/CA-36 的「绝不自动改写」在闸门态同样成立）。"""
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_phot_body(_z_spec(), {'z_from_lines': True}))
    d = _json(r)
    assert r.status_code == 200, d.get('error')
    blk = d['diagnostics']['z_from_lines']
    assert blk['z_fit'] is None and blk['sigma_z'] is None
    assert blk['n_lines_used'] is None and blk['lines'] == []
    assert blk['verdict'] == 'disabled_line_frame_unverified'
    assert 'M-6' in blk['reason']
    assert d['meta']['z'] == Z_TRUE and d['frame']['x'] == 1.0 + Z_TRUE


def test_f81_numeric_with_synthetic_fixture(client, monkeypatch):
    """数值路径（合成 fixture = M-6 复核态模拟，非伪造库内线表）：z_fit 回收、
    逐线采信；CA-36：z_used 与 z_fit 差 > C_Z_TOL ⇒ verdict + CA-36 告警，且
    **不自动改写 z**；命中线 < C_LINE_MATCH_MIN ⇒ z_fit=null（T-51 第一支）。"""
    _login(client)
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_line_table())
    spec_bad = _z_spec(z=0.15, meta_z=0.0)     # 线位 z=0.15，所用 z=0（矛盾谱）
    body = _phot_body(spec_bad, {'z_from_lines': True})
    _clear_cache()
    r = client.post('/api/specphot/photometry', json=body)
    d = _json(r)
    assert r.status_code == 200, d.get('error')
    blk = d['diagnostics']['z_from_lines']
    assert blk['verdict'] == 'ca36_mismatch' and blk['ca36']
    assert abs(blk['z_fit'] - 0.15) < 0.005          # 反推回谱线的真 z
    assert blk['n_lines_used'] >= 3
    statuses = {r_['status'] for r_ in blk['lines']}
    assert 'accepted' in statuses
    assert any(w['code'] == 'CA-36' for w in d['warnings'])
    assert d['meta']['z'] == 0.0 and d['frame']['x'] == 1.0     # z 未被改写
    # 命中线不足：单线线表 ⇒ z_fit=null（T-51 第一支）
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_line_table()[:1])
    _clear_cache()
    r2 = client.post('/api/specphot/photometry', json=body)
    blk2 = _json(r2)['diagnostics']['z_from_lines']
    assert blk2['verdict'] == 'too_few_lines' and blk2['z_fit'] is None
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


def test_f81_frame_unknown_e14(client):
    """T-51 第二支（z_from_lines 半）：lambda_frame='unknown' ⇒ E-14
    lambda_frame_unknown（F-81②）。"""
    _login(client)
    spec = _z_spec()
    spec['meta']['lambda_frame'] = 'unknown'
    _clear_cache()
    r = client.post('/api/specphot/photometry',
                    json=_phot_body(spec, {'z_from_lines': True}))
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'lambda_frame_unknown'


# ═══ F-85 frame_probe：M-6 闸两态 + air 判定 + inconclusive ══════════════════

_FEATURES = [{'name': 'OI5577', 'kind': 'emis', 'lambda_vac_aa': 5577.339},
             {'name': 'OI6300', 'kind': 'emis', 'lambda_vac_aa': 6300.304},
             {'name': 'O2B', 'kind': 'abs', 'lambda_vac_aa': 6871.0}]


def _frame_spec(frame='air', lo=5400.0, hi=7000.0, step=0.5, noise=0.5,
                seed=SEED):
    lam = np.arange(lo, hi + step / 2, step)
    flux = np.full(lam.size, 100.0)
    for ft in _FEATURES:
        c = (wavconvert.vacuum_to_air(ft['lambda_vac_aa'])
             if frame == 'air' else ft['lambda_vac_aa'])
        if ft['kind'] == 'emis':
            flux = flux + 20.0 * np.exp(-4 * math.log(2) * ((lam - c) / 2.0) ** 2)
        else:
            flux = flux - 30.0 * np.exp(-4 * math.log(2) * ((lam - c) / 4.0) ** 2)
    rng = np.random.default_rng(seed)
    flux = flux + rng.normal(0, noise, lam.size)
    return {'lam_aa': lam.tolist(), 'flux': flux.tolist(), 'flux_err': None,
            'meta': {'z': 0.0, 'category': 'other', 'mjd': 60000.0,
                     'lambda_frame': 'vacuum'}}    # 上传件默认假定 vacuum（CA-34）


def test_f85_gate_block_library_default(client):
    _login(client)
    r = client.post('/api/specphot/photometry',
                    json=_phot_body(_frame_spec(), {'frame_probe': True}))
    d = _json(r)
    assert r.status_code == 200, d.get('error')
    blk = d['diagnostics']['frame_probe']
    assert blk['frame_suggestion'] == 'inconclusive'
    assert blk['n_support'] is None and blk['log_lik_ratio'] is None
    assert 'M-6' in blk['reason']
    assert '不是默认值' in blk['note']


def test_f85_air_spectrum_suggests_air(client, monkeypatch):
    """T-51 第三支：合成**空气波长**谱（上传件按默认 vacuum 处理）⇒
    frame_suggestion='air'（支持点数、似然比在场）；不自动改写 lambda_frame
    （F-79②/F-85，响应键仍 vacuum）。低信噪谱 ⇒ inconclusive（禁止沉默为
    vacuum，T-51 第四支）。"""
    _login(client)
    monkeypatch.setattr(_reg, 'M6_FRAME_FEATURES', _FEATURES)
    body = _phot_body(_frame_spec(frame='air'), {'frame_probe': True})
    _clear_cache()
    r = client.post('/api/specphot/photometry', json=body)
    d = _json(r)
    assert r.status_code == 200, d.get('error')
    blk = d['diagnostics']['frame_probe']
    assert blk['frame_suggestion'] == 'air'
    assert blk['n_support'] >= 1 and blk['log_lik_ratio'] < 0     # 负 ⇒ 偏 air
    assert d['lambda_frame'] == 'vacuum'              # 只进 diagnostics{}，不改写
    assert len(blk['features']) == len(_FEATURES)
    # 低信噪 ⇒ inconclusive
    _clear_cache()
    r2 = client.post('/api/specphot/photometry', json=dict(
        body, spectrum=_frame_spec(frame='air', noise=60.0)))
    blk2 = _json(r2)['diagnostics']['frame_probe']
    assert blk2['frame_suggestion'] == 'inconclusive' and blk2['n_support'] == 0
    monkeypatch.setattr(_reg, 'M6_FRAME_FEATURES', None)


def test_f85_frame_unknown_e14(client):
    """T-51 第二支（frame_probe 半）：lambda_frame='unknown' ⇒ E-14。"""
    _login(client)
    spec = _frame_spec()
    spec['meta']['lambda_frame'] = 'unknown'
    _clear_cache()
    r = client.post('/api/specphot/photometry',
                    json=_phot_body(spec, {'frame_probe': True}))
    d = _json(r)
    assert r.status_code == 400 and d['reason'] == 'lambda_frame_unknown'


# ═══ F-86 sky_subtract（U-31，真解锁）：T-51 末支 + 成功支 ═══════════════════

def test_f86_revert_no_skyline_ca37_not_in_budget(client):
    """T-51 末支：人造无天光线的谱开 sky_subtract ⇒ 退回 mask + CA-37 +
    not_in_budget 含 sky_subtraction（F-58/F-72②）。"""
    _login(client)
    spec = _emission_spec()
    r = _post(client, _line_body(spec, 5007.0, sky_handling='subtract'))
    d = _json(r)
    assert r.status_code == 200, d
    row = d['lines'][0]
    assert row['sky_handling'] == 'subtract'
    assert row['sky_subtracted'] is False
    assert row['sky_subtract']['reverted'] is True
    assert any(w['code'] == 'CA-37' and '不得称' in w['message']
               for w in row['warnings'])
    assert 'sky_subtraction' in row['not_in_budget']


def test_f86_effective_subtract_recovers_ew(client):
    """成功支：线窗内命中 [O I] 5577 天光段 ⇒ 联合线性扣除生效（RMS 比下降、
    sky_subtracted=true、被扣段脱 mask、大气带不受影响），EW 回收优于 mask。"""
    _login(client)
    lam = np.arange(4900.0, 6200.0, 1.0)
    rng = np.random.default_rng(SEED)
    sci = 3e-15 * np.exp(-0.5 * ((lam - 5500.0) / 12.0) ** 2)
    sky = 8e-15 * np.exp(-4 * math.log(2) * ((lam - 5577.3) / 2.2) ** 2)
    flux = C0 + sci + sky + rng.normal(0, 2e-17, lam.size)
    spec = {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': (flux * 0.003).tolist(), 'meta': {'z': 0.0}}
    r1 = _post(client, _line_body(spec, 5500.0, sky_handling='subtract',
                                  window_halfwidth_aa=60, n_boot=1))
    d1 = _json(r1)
    assert r1.status_code == 200, d1
    row1 = d1['lines'][0]
    assert row1['sky_subtracted'] is True
    assert row1['sky_subtract']['rms_ratio'] < 0.7
    assert row1['sky_subtract']['n_sky_lines'] == 1
    kept = {s['reason'] for s in row1['excluded_segments']
            if '天光发射' in s['reason']}
    r2 = _post(client, _line_body(spec, 5500.0, sky_handling='mask',
                                  window_halfwidth_aa=60, n_boot=1))
    row2 = _json(r2)['lines'][0]
    masked = {s['reason'] for s in row2['excluded_segments']
              if '天光发射' in s['reason']}
    assert kept < masked                              # 被扣段脱 mask
    assert abs(row1['ew_obs_aa'] - 9.02) < abs(row2['ew_obs_aa'] - 9.02) + 0.3
    assert abs(row1['lambda_obs_vac_aa'] - 5500.0) < 1.0
    assert 'sky_subtraction' in row1['not_in_budget']
    # 缓存键区分两态（ST-3：sky_handling 进键）
    assert row1['ew_obs_aa'] != row2['ew_obs_aa']


# ═══ T-48/T-51 恒等回归：单开只增 diagnostics 子键，主结果不变 ═══════════════

def _main_part(resp):
    return {k: v for k, v in resp.items() if k not in ('diagnostics', 'warnings')}


def test_t48_p3c_single_switch_identity(client):
    """W-32 后半（自动收口）：z_from_lines/frame_probe 单开 ⇒ 只增 diagnostics
    子键，其余键逐字节同（闸门块是 null 块，不进任何数值路径）。"""
    _login(client)
    spec = _z_spec()
    body = _phot_body(spec, {})
    _clear_cache()
    r0 = client.post('/api/specphot/photometry', json=body)
    base = _json(r0)
    assert base['diagnostics'] == {}
    for k in ('z_from_lines', 'frame_probe'):
        _clear_cache()
        r1 = client.post('/api/specphot/photometry',
                         json=dict(body, diagnostics={k: True}))
        d1 = _json(r1)
        assert json.dumps(_main_part(base), sort_keys=True) == \
            json.dumps(_main_part(d1), sort_keys=True), k
        assert list(d1['diagnostics'].keys()) == [k]


# ═══ W-32 后半：前端源码级扫描 ═══════════════════════════════════════════════

def test_w32_second_half_frontend_source_scan():
    with open(os.path.join(FRONT_SPECPHOT, 'workbench.js'), encoding='utf-8') as f:
        wb = f.read()
    for k in ('z_from_lines', 'frame_probe'):
        seg = wb.split(f"['{k}', 'M6',", 1)[1].split(']', 1)[0]
        assert 'M-6' in seg and '解锁条件' in seg, k   # tooltip 写明前置与解锁条件
    assert "['sky_subtract', 'U31'," in wb and 'U-31' in \
        wb.split("['sky_subtract', 'U31',", 1)[1].split(']', 1)[0]
    badge = wb.split('const badge =', 1)[1].split(';', 1)[0]
    assert 'M-6' in badge and 'U-31' in badge
    with open(os.path.join(FRONT_SPECPHOT, 'lines_ui.js'), encoding='utf-8') as f:
        ui = f.read()
    # U-31 subtract 可用（真解锁）：可交互 radio（sp-lsky class）+ F-86 说明
    assert ui.count("class=\"sp-lsky\" value=\"subtract\"") == 1
    assert 'F-86' in ui and 'CA-37' in ui
    # U-26 voigt 禁用 + M-4 tooltip（T-22 前端半支）
    assert '<input type="radio" disabled>voigt' in ui and 'M-4' in ui


# ═══ T-51 真实 M-6 数据重跑登记（照 T-10/T-21 先例 skip） ════════════════════

@pytest.mark.skip(reason='T-51 四支已按合成口径全测（本文件 + '
                         'test_l1_specphot_p3b_voigt.py）；验收原文要求的 M-6 '
                         '复核线表（registry 复核项，逐条对 NIST ASD 登记 '
                         'line_frame/f_source）尚未落地，全文重跑待 M-6 完成'
                         '（F-38②，照 T-10/T-21 先例登记）')
def test_t51_rerun_on_m6_reviewed_table():
    """T-51 的四支已按**合成口径**全测（本文件 + test_l1_specphot_p3b_voigt）；
    但验收原文要求在 M-6 复核线表（registry 复核项落地、逐条对 NIST ASD 登记
    line_frame 与 f_source）上重跑全部走查——M-6 未完成前不具判据效力（F-38②），
    照 T-10/T-21 先例登记 skip。"""
    raise AssertionError('M-6 未完成')


# ═══ F-94 键集：新增簿记键恒在场（sky_subtract / not_in_budget / depth 口径） ═

def test_line_row_new_bookkeeping_keys(client):
    _login(client)
    spec = _emission_spec()
    r = _post(client, _line_body(spec, 5007.0))
    row = _json(r)['lines'][0]
    assert row['not_in_budget'] == ['wavecal', 'aperture', 'flux_calibration',
                                    'sky_subtraction']
    assert row['sky_subtract'] is None                # mask 路径：块在场恒 null
    assert row['depth_err_delta'] == 'analytic'
