"""L2: specphot **P3d 收口验收**——阻尼翼与吸收系统（02b §3.13 / §9 P3d 行 /
F-98…F-105 / U-48 / §7.9 / T-69…T-74 / W-35）。

交付形态（本期裁定，登记 docs/TECHNICAL.md）：前置 M-4（仪器 R）与 M-6（线表
帧与 f 值）在库内均未满足 ⇒ 整族为可选族：代码就位 + U-48 三开关后闸——
  - 三开关全关 ⇒ API-4 数值与既有键与 P3c 基线逐字节相同（T-72①；本族恒在场
    的 absorber_systems/forest_stats/forest_stats_reasons + cross_link_gate 四键
    属判据、不属比较域）。
  - M-6 缺 ⇒ 识别/翼拟合一律闸门格（值键 null + 书面原因，F-98① 本模块不另立
    Lyα 常量 ⇒ 静止系 Lyα 无输入源）；z 显式 ⇒ user_z 路由降级（§3.9.3.1
    z_abs 行原文语义）；F-102 forest_stats 闭集空输出不依赖前置 ⇒ **真实现**。
  - 数值路径用**测试内合成 fixture**（monkeypatch registry.M6_LINE_TABLE +
    合成阻尼翼谱）驱动——fixture 是测试内合成，不是伪造库内线表。
  - CA-45 的触发通路：族 A 连续谱 = "当前选定模型"（cont_a_override 供给或
    本模块自拟合后冻结）；API-4 无 S2 模型供给 ⇒ 默认两族同源、CA-45 只能经
    注入通路验收（本文件装配级测试），S2 集成后自然可达。

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
from specphot import diagnostics as _diag
from specphot import registry as _reg
from specphot.constants import (C_ABS_CLASS_DLA, C_ABS_CLASS_LLS,
                                C_ABS_CLASS_SUBDLA, C_ABS_DETECT_SIGMA,
                                C_AOD_SAMP_MIN, C_AOD_SNR_RES_MIN,
                                C_SAT_DEPTH_FLOOR, C_WING_B_KMS,
                                C_WING_FIT_MIN_SNR, C_WING_SPREAD_DEX,
                                C_WING_Z_WIN_SIGMA, C_XLINK_MASK_MAX)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONT_SPECPHOT = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))

C0 = 1e-14
LYA_VAC = 1215.67                 # 合成 fixture 的 Lyα 静止系真空波长（仅测试内；
F_LYA = 0.4164                    #  生产代码不立此常量——F-98①）
Z_SYS = 2.0
C_KMS = 299792.458
_FOREST_CLOSED = {'single_sightline', 'continuum_unknown',
                  'resolution_below_gate', 'lls_stochastic'}


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _login(client, name='p3d-tester'):
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


def _json(resp):
    return json.loads(resp.get_data(as_text=True))


def r_ok(d):
    assert 'diagnostics' in d, d.get('error') or d
    return True


# ─── 合成工装 ────────────────────────────────────────────────────────────────

def _wing_spec(logn=20.3, z=Z_SYS, b=25.0, noise=3e-17, span_kms=2.4 * 3000.0,
               seed=11, forest_dips=0):
    """合成阻尼翼谱：Lyα(1+z) 红侧翼 + 线性连续谱（覆盖到 +span_kms，
    span≥2e4 时红侧森林判定区有数据）。forest_dips>0 ⇒ 注入窄凹陷（前景系）。"""
    lam_a = LYA_VAC * (1.0 + z)
    lam = np.arange(lam_a * 0.999, lam_a * (1.0 + span_kms / C_KMS), 1.0)
    cont = C0 * (1.0 + 0.05 * (lam - lam[0]) / 1000.0)
    flux = cont * np.exp(-_diag._voigt_tau(lam, LYA_VAC, z, b, logn, F_LYA))
    rng = np.random.default_rng(seed)
    for k in range(forest_dips):
        c = lam_a * (1.0 + (6000.0 + 900.0 * k) / C_KMS)
        flux = flux - cont * 0.5 * np.exp(
            -0.5 * ((lam - c) / 1.2) ** 2)
    flux = flux + rng.normal(0, noise, lam.size)
    return {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': (noise * (1.0 + 0.05 * (np.random.default_rng(seed + 1)
                            .random(lam.size) - 0.5))).tolist(),
            'meta': {'z': z, 'category': 'other'}}


def _m6_table():
    return [{'species': 'Ly alpha 1215', 'lambda0_rest_aa': LYA_VAC,
             'line_frame': 'vacuum', 'f_source': 'synthetic-fixture',
             'f_osc': F_LYA},
            {'species': 'CIV 1548', 'lambda0_rest_aa': 1548.204,
             'line_frame': 'vacuum', 'f_source': 'synthetic-fixture',
             'f_osc': 0.1908}]


def _metal_wing_spec(logn=20.3, z=Z_SYS, depth=0.5, noise=3e-17, seed=13):
    """翼 + 一条匹配 M-6 表的金属吸收线（CIV 1548 ⇒ λ_obs=1548.204·(1+z)），
    供 metal_lines 路由与 metal_sat_check 的被链接线行。谱覆盖翼窗与金属线。"""
    lam_a = LYA_VAC * (1.0 + z)
    lam = np.arange(lam_a * 0.999, 4790.0, 1.0)
    cont = C0 * (1.0 + 0.05 * (lam - lam[0]) / 1000.0)
    flux = cont * np.exp(-_diag._voigt_tau(lam, LYA_VAC, z, 25.0, logn, F_LYA))
    rng = np.random.default_rng(seed)
    lam_m = 1548.204 * (1.0 + z)
    line = cont * depth * np.exp(-0.5 * ((lam - lam_m) / 2.5) ** 2)
    flux = flux - line + rng.normal(0, noise, lam.size)
    return {'lam_aa': lam.tolist(), 'flux': flux.tolist(),
            'flux_err': (noise * (1.0 + 0.05 * (np.random.default_rng(seed + 1)
                            .random(lam.size) - 0.5))).tolist(),
            'meta': {'z': z, 'category': 'other'}}


def _line_body(spec, lam_rest, kind='absorption', **over):
    body = {'spectrum': spec,
            'line': {'species': 'CIV 1548', 'lambda_rest_aa': lam_rest,
                     'id_table': 'fixture'},
            'line_kind': kind, 'n_boot': 8, 'err_seed': 7,
            'mw': {'correct': False}}
    body.update(over)
    return body


# ═══ T-69：翼拟合的三条纪律（F-99①②③）════════════════════════════════════

def test_t69_pinned_zb_and_profile_interval():
    """① 自由参表不得出现 z/b（n_par=钉住后个数）；② Δχ²=1 剖面区间回收
    真值（合成 logN=20.3、b=25 钉住）。"""
    spec = _wing_spec()
    lam = np.asarray(spec['lam_aa'])
    flux = np.asarray(spec['flux'])
    sigma = np.asarray(spec['flux_err'])
    res = _diag.absorber_ident_numeric(
        lam, flux, sigma, z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC,
        f_osc=F_LYA, b_kms=C_WING_B_KMS)
    wf = res['wing_fit']
    assert wf['verdict'] == 'ok'
    for fp in (wf['free_params_a'], wf['free_params_b']):
        assert all(p not in ('z', 'b', 'z_abs', 'b_kms') for p in fp)
        assert all('z' != p.lower() and p.lower() != 'b' for p in fp)
    assert wf['n_par_a'] == 1 and wf['n_par_b'] == 3       # 钉住后的个数
    assert wf['n_par_a'] + wf['n_par_b'] == 4
    assert abs(wf['wing_logn'] - 20.3) < 0.05              # 真值回收
    lo, hi = wf['wing_logn_err_lo'], wf['wing_logn_err_hi']
    assert 0.0 < lo <= 0.5 and 0.0 < hi <= 0.5             # Δχ²=1 剖面端点在场
    assert abs(wf['wing_logn_alt'] - 20.3) < 0.05
    # ③ 回显的 wing_b_assumption_kms 与实际所用一致（调用给 25 ⇒ 拟合用 25）：
    res2 = _diag.absorber_ident_numeric(
        lam, flux, sigma, z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC,
        f_osc=F_LYA, b_kms=10.0)
    assert abs(res2['wing_fit']['wing_logn'] - 20.3) > abs(
        wf['wing_logn'] - 20.3)    # b 换值 ⇒ 拟合确实消费了新 b（偏离真值更多）
    assert res2['wing_fit']['wing_logn'] != wf['wing_logn'] or \
        res2['wing_fit']['wing_logn_err_hi'] != wf['wing_logn_err_hi']


def test_t69_b_pin_three_routes():
    """② b 钉住三路优先级（metal_cog > resolution_element >
    engineering_default），b_source 词表逐字。"""
    b, src = _diag.b_pin(b_metal_cog=18.0, r_res=5000.0)
    assert src == 'metal_cog' and b == 18.0
    b, src = _diag.b_pin(r_res=5000.0)
    assert src == 'resolution_element' and abs(b - 299792.458 / 5000.0) < 1e-9
    b, src = _diag.b_pin()
    assert src == 'engineering_default' and b == C_WING_B_KMS


def test_t69_reverse_injection_static_scan():
    """④ 反向注入：把 z/b 放进自由参表的实现必须失败——源码级扫描：
    自由参名单字面量只有 logN/连续谱系数；z_abs/b_kms 是 keyword-only 钉住入参。"""
    with open(os.path.join(_REPO_ROOT, 'backend/specphot/diagnostics.py'),
              encoding='utf-8') as f:
        src = f.read()
    body = src.split('def wing_logn_two_families', 1)[1].split(
        '\ndef ', 1)[0]
    assert "'free_params_a': ['logN']" in body
    assert "b_kms," in src.split('def wing_logn_two_families', 1)[1].split(
        ')', 1)[0] and '*, ' in src.split('def wing_logn_two_families',
                                          1)[1].split('):', 1)[0]


# ═══ T-70：三档分类的阈值闭合（F-98②③④）═══════════════════════════════════

def test_t70_threshold_closure_half_open():
    """档名只在 F-98③ 的三段半开区间翻转；sub-DLA 上界==C_ABS_CLASS_DLA
    （无缝无重叠）；低于 LLS ⇒ 空值（禁 none/other 字符串）；
    class_thresholds_dex 与 log10(C_ABS_CLASS_*) 逐项复算一致。"""
    th = _diag.class_thresholds_dex()
    assert th == {'dla': math.log10(C_ABS_CLASS_DLA),
                  'sub_dla': math.log10(C_ABS_CLASS_SUBDLA),
                  'lls': math.log10(C_ABS_CLASS_LLS)}
    cases = [(1.5e17, None), (C_ABS_CLASS_LLS, 'lls'),
             (C_ABS_CLASS_SUBDLA * (1.0 - 1e-9), 'lls'),
             (C_ABS_CLASS_SUBDLA, 'sub_dla'),
             (C_ABS_CLASS_DLA * (1.0 - 1e-9), 'sub_dla'),
             (C_ABS_CLASS_DLA, 'dla'), (3e21, 'dla')]
    for n, want in cases:
        got = _diag.classify_logn(math.log10(n))
        assert got is want, (n, got, want)
        assert got not in ('none', 'other', 'sub-DLA', '')


def test_t70_intervening_host_undetermined():
    """④ 红侧无森林线 ⇒ host；有 ⇒ intervening；红侧覆盖不足 ⇒ undetermined
    （写成 host 即失败）。"""
    base = dict(z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC, f_osc=F_LYA,
                b_kms=C_WING_B_KMS, do_fit=False)
    lam = np.asarray(_wing_spec(span_kms=21000.0)['lam_aa'])
    flux = np.asarray(_wing_spec(span_kms=21000.0, forest_dips=0)['flux'])
    sigma = np.asarray(_wing_spec(span_kms=21000.0)['flux_err'])
    assert _diag.absorber_ident_numeric(lam, flux, sigma,
                                        **base)['intervening_or_host'] == 'host'
    flux_i = np.asarray(_wing_spec(span_kms=21000.0, forest_dips=3)['flux'])
    assert _diag.absorber_ident_numeric(lam, flux_i, sigma,
                                        **base)['intervening_or_host'] == \
        'intervening'
    lam_s = np.asarray(_wing_spec(span_kms=2.2 * 3000.0)['lam_aa'])
    flux_s = np.asarray(_wing_spec(span_kms=2.2 * 3000.0)['flux'])
    sigma_s = np.asarray(_wing_spec(span_kms=2.2 * 3000.0)['flux_err'])
    r = _diag.absorber_ident_numeric(lam_s, flux_s, sigma_s, **base)
    assert r['intervening_or_host'] == 'undetermined'   # 合法终值


def test_t70_low_snr_no_extrapolation():
    """wing_snr_res < C_WING_FIT_MIN_SNR ⇒ 不拟合：两族四端点全空值 + 书面
    原因，不得出现外推值。"""
    spec = _wing_spec(noise=1.5e-15)
    lam = np.asarray(spec['lam_aa'])
    r = _diag.absorber_ident_numeric(
        lam, np.asarray(spec['flux']), np.asarray(spec['flux_err']),
        z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC, f_osc=F_LYA,
        b_kms=C_WING_B_KMS)
    assert r['verdict'] == 'wing_snr_below_gate'
    assert r['wing_snr_res'] < C_WING_FIT_MIN_SNR
    assert r['wing_fit'] is None and 'C_WING_FIT_MIN_SNR' in r['reason']


# ═══ T-71：forest_stats 闭集空输出 + 「描述 ≠ 判据」（F-102/F-101①）═════════

def test_t71_forest_closed_set_both_r_states(client):
    """① 任何输入下 forest_stats 恒 null 且 reasons 非空、⊆ 四值闭集；
    r_source='none' ⇒ resolution_below_gate 在集内，R 给出 ⇒ 不在（②类不误伤）。"""
    _login(client)
    spec = _wing_spec(span_kms=21000.0)
    body = _line_body(spec, 1253.0,
                      diagnostics={'absorber_ident': True})
    _clear_cache()
    d = _json(_post(client, body))
    assert d['diagnostics']['forest_stats'] is None
    rs = d['diagnostics']['forest_stats_reasons']
    assert rs and set(rs) <= _FOREST_CLOSED
    assert 'resolution_below_gate' in rs and 'single_sightline' in rs
    body_r = dict(body, R=5000.0)
    _clear_cache()
    d2 = _json(_post(client, body_r))
    rs2 = d2['diagnostics']['forest_stats_reasons']
    assert set(rs2) <= _FOREST_CLOSED and 'resolution_below_gate' not in rs2
    assert 'single_sightline' in rs2 and 'lls_stochastic' in rs2


def test_t71_forbidden_keys_and_fifth_reason(client):
    """②③ 响应与前端源码里不存在 x_HI/xHI、lyc_transmission、flux_pdf/
    power_spectrum/dN_dz；reasons 集合闭合（无第五值路径）。"""
    _login(client)
    spec = _metal_wing_spec()
    body = _line_body(spec, 1548.204,
                      diagnostics={'absorber_ident': True, 'wing_logn': True,
                                   'metal_sat_check': True})
    _clear_cache()
    d = _json(_post(client, body))
    assert r_ok(d)
    blob = json.dumps(d, ensure_ascii=False)
    for bad in ('x_HI', 'xHI', 'lyc_transmission', 'flux_pdf',
                'power_spectrum', 'dN_dz'):
        assert bad not in blob, bad
    assert set(d['diagnostics']['forest_stats_reasons']) <= _FOREST_CLOSED
    for path in (os.path.join(FRONT_SPECPHOT, 'lines_ui.js'),
                 os.path.join(FRONT_SPECPHOT, 'export.js')):
        with open(path, encoding='utf-8') as f:
            ui = f.read()
        for bad in ('x_HI', 'xHI', 'lyc_transmission', 'flux_pdf',
                    'power_spectrum', 'dN_dz'):
            assert bad not in ui, (path, bad)


def test_t71_document_sentinel_descriptions_not_gates():
    """④ 文档级哨兵：仓内 specphot 源与 docs/TECHNICAL.md 不得把
    "SNR∼50" 或 "R ∼ 45 000 / S/N ∼ 20–50" 这类样本描述当门槛陈述
    （L-59②/L-58② 已在 01 §四登记为描述）。"""
    roots = [os.path.join(_REPO_ROOT, 'backend', 'specphot'),
             os.path.join(_REPO_ROOT, 'docs')]
    for root in roots:
        for fn in os.listdir(root):
            if not fn.endswith(('.py', '.md')):
                continue
            with open(os.path.join(root, fn), encoding='utf-8') as f:
                txt = f.read()
            for bad in ('S/N ∼ 20', 'R ∼ 45 000', 'SNR∼50', 'SNR ∼ 50'):
                assert bad not in txt, (fn, bad)


# ═══ T-72：关掉即逐字节相同 + 列集恒定（F-105①②③）═════════════════════════

def test_t72_off_switch_byte_identity(client):
    """① 三开关全关（显式 false）与不发 diagnostics 键 ⇒ 响应逐字节相同；
    且除恒在场五键外无任何新增键（比较域 = 数值与既有键；第五键
    absorber_system_masked 为 P3d 评审 P1-3 追加、恒在场、关闭态 []）。"""
    _login(client)
    spec = _metal_wing_spec()
    _clear_cache()
    base = _json(_post(client, _line_body(spec, 1548.204)))
    assert r_ok(base)
    off = dict(_line_body(spec, 1548.204),
               diagnostics={'absorber_ident': False, 'wing_logn': False,
                            'metal_sat_check': False})
    _clear_cache()
    d1 = _json(_post(client, off))
    assert json.dumps(base, sort_keys=True) == json.dumps(d1, sort_keys=True)
    assert set(base['diagnostics']) == {'absorber_systems', 'forest_stats',
                                        'forest_stats_reasons',
                                        'cross_link_gate',
                                        'absorber_system_masked'}
    assert base['diagnostics']['absorber_systems'] == []
    assert base['diagnostics']['absorber_system_masked'] == []
    assert base['diagnostics']['forest_stats'] is None
    assert base['diagnostics']['cross_link_gate']['enabled'] is False


# §4.3 第四块列序（发布即冻结，F-45）：与规格原文逐字一致（T-72② 源码级哨兵）
_ABS_COLS_SPEC = [
    'system_id', 'class', 'class_thresholds_dex', 'intervening_or_host',
    'z_abs', 'z_err', 'z_source', 'lambda_rest_lya_vac_aa',
    'wing_logn', 'wing_logn_err_lo', 'wing_logn_err_hi',
    'wing_logn_alt', 'wing_logn_alt_err_lo', 'wing_logn_alt_err_hi',
    'wing_spread_dex', 'wing_b_assumption_kms', 'b_source',
    'cont_family_a', 'cont_family_b', 'wing_snr_res', 'wing_n_pixels',
    'metal_line_ids', 'metal_sat_flags', 'masked_ranges',
    'forest_stats', 'forest_stats_reasons', 'blue_side_igm_masked',
    'n_lines_in_masked_absorbers', 'notes', 'warnings', 'err_source',
    'err_scope', 'spec_phot_version']


def test_t72_export_column_set_frozen_and_header_always():
    """② 吸收系统 CSV 列名冻结且 U-48 关 ⇒ 表头照写、无数据行（导出函数对
    空 systems 不早退）；forest_stats 列恒空、reasons 列恒非空的载体在场。"""
    with open(os.path.join(FRONT_SPECPHOT, 'export.js'), encoding='utf-8') as f:
        src = f.read()
    seg = src.split('const ABS_COLS = [', 1)[1].split('];', 1)[0]
    cols = [c.strip().strip("'\"") for c in seg.replace('\n', ' ').split(',')
            if c.strip()]
    assert cols == _ABS_COLS_SPEC
    body = src.split('export function exportAbsorberCsv', 1)[1]
    assert 'if (!r) return;' in body                 # 仅响应整体缺失才不导
    assert 'absorber_systems' in body                # 空数组 ⇒ 只出表头行
    assert 'forest_stats_reasons' in src             # reasons 列载体在场（恒非空）


def test_t72_diag_ignored_api2_api3(client, monkeypatch):
    """④ U-48 键发给 API-2/API-3 ⇒ wing_logn/metal_sat_check 出现在
    diagnostics_ignored[] 且结果与不发相同（不得 500、不得静默采纳，F-105③）。
    absorber_ident 的 P3d 评审 P1-2 修订（登记 TECHNICAL）：在 API-2 消费为
    第五类掩膜/CA-46① 闸门（M-6 空库现状 ⇒ 无段 ⇒ 结果仍与不发逐字节同）、
    不再进 API-2 的 diagnostics_ignored[]；API-3 不变（仍忽略+回显）。"""
    _login(client)
    spec = _wing_spec(span_kms=3000.0)
    ph_body = {'spectrum': spec,
               'bands': ['test-p3d'],
               'custom_curves': [{'band': 'test-p3d',
                                  'lam_aa': [3640.0, 3680.0, 3700.0],
                                  't': [0.0, 1.0, 0.0],
                                  'curve_kind': 'unknown'}],
               'weighting': 'photon',
               'anchor_rows': [{'band': 'test-p3d', 'mag': 16.0,
                                'mag_system': 'AB', 'mag_err': 0.05,
                                'mjd': 60000.0}]}
    _clear_cache()
    base = _json(client.post('/api/specphot/photometry', json=ph_body))
    _clear_cache()
    d = _json(client.post('/api/specphot/photometry', json=dict(
        ph_body, diagnostics={'absorber_ident': True, 'wing_logn': True,
                              'metal_sat_check': True})))
    assert d['diagnostics_ignored'] == ['wing_logn', 'metal_sat_check']
    assert 'cross_link_gate' not in d['diagnostics']      # M-6 空 ⇒ 无掩膜无回显
    assert 'diagnostics_ignored' not in base
    d2 = dict(d)
    d2.pop('diagnostics_ignored')
    assert json.dumps(base, sort_keys=True) == json.dumps(d2, sort_keys=True)
    # API-3（continuum）：最小合法请求 + U-48 键 ⇒ 同纪律（ absorber_ident 仍忽略）
    c_body = {'spectrum': spec, 'models': ['pl'], 'mw': {'correct': False}}
    _clear_cache()
    cb = _json(client.post('/api/specphot/continuum', json=c_body))
    _clear_cache()
    cd = _json(client.post('/api/specphot/continuum', json=dict(
        c_body, diagnostics={'absorber_ident': True})))
    assert cd['diagnostics_ignored'] == ['absorber_ident']
    cd.pop('diagnostics_ignored')
    assert json.dumps(cb, sort_keys=True) == json.dumps(cd, sort_keys=True)


# ═══ T-73：双连续谱族对照与 CA-45（F-100）═══════════════════════════════════

def test_t73_spread_recompute_and_ca45_trigger():
    """同一份翼数据：默认（族 A=当前选定自拟后冻结、族 B=lnλ poly 联合重拟）
    两族一致（spread≈0）；族 A 连续谱安置故意偏 35% ⇒ spread>|阈| 且与
    |A−B| 复算一致、四个区间端点全在场。"""
    spec = _wing_spec()
    lam, flux, sigma = (np.asarray(spec['lam_aa']), np.asarray(spec['flux']),
                        np.asarray(spec['flux_err']))
    wf0 = _diag.wing_logn_two_families(
        lam, flux, sigma, z_abs=Z_SYS, b_kms=C_WING_B_KMS,
        lambda_rest_lya_vac_aa=LYA_VAC, f_osc=F_LYA)
    assert abs(wf0['wing_spread_dex']) < 1e-3
    ok = np.ones(lam.size, dtype=bool)
    w, m, _ = _diag._wing_windows(lam, ok, LYA_VAC, Z_SYS)
    fit = w | m
    cont = C0 * (1.0 + 0.05 * (lam[fit] - lam[0]) / 1000.0)
    wf1 = _diag.wing_logn_two_families(
        lam, flux, sigma, z_abs=Z_SYS, b_kms=C_WING_B_KMS,
        lambda_rest_lya_vac_aa=LYA_VAC, f_osc=F_LYA,
        cont_a_override=cont * 1.35)                # 族 A 安置故意偏 35%
    assert abs(wf1['wing_spread_dex']
               - abs(wf1['wing_logn'] - wf1['wing_logn_alt'])) < 1e-12
    assert wf1['wing_spread_dex'] > C_WING_SPREAD_DEX
    for k in ('wing_logn', 'wing_logn_alt', 'wing_logn_err_lo',
              'wing_logn_err_hi', 'wing_logn_alt_err_lo',
              'wing_logn_alt_err_hi'):
        assert wf1[k] is not None, k                # 两族四端点同屏


def test_t73_ca45_assembly_and_txt24_swap(monkeypatch):
    """装配级：翼拟合 spread 超阈 ⇒ 系统告警 CA-45 在场（禁 quadrature 合成、
    族 B 不是 competing 模型）；前端 TXT-24 换成"系统项"那句（源码级）。"""
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())

    ident = {'detected': True, 'detect_sigma': 40.0, 'wing_snr_res': 100.0,
             'wing_n_pixels': 60, 'intervening_or_host': 'host',
             'verdict': 'ok',
             'wing_fit': {'cont_family_a': 'selected_fixed',
                          'cont_family_b': 'poly_lnlambda_refit',
                          'verdict': 'ok', 'wing_logn': 20.3,
                          'wing_logn_err_lo': 0.01, 'wing_logn_err_hi': 0.01,
                          'wing_logn_alt': 19.9, 'wing_logn_alt_err_lo': 0.02,
                          'wing_logn_alt_err_hi': 0.02,
                          'wing_spread_dex': 0.4, 'ca44': None,
                          'n_profile_evals': 10}}
    monkeypatch.setattr(_diag, 'absorber_ident_numeric',
                        lambda *a, **k: dict(ident))
    lam = np.arange(3600.0, 4700.0, 1.0)
    from specphot import lines_api as _la
    systems, gate, _rs = _la._absorber_stage(
        lam, np.full(lam.size, C0), np.full(lam.size, 3e-17),
        {'absorber_ident': True, 'wing_logn': True, 'metal_sat_check': True},
        Z_SYS, True, None, 'none',
        {'detected': False, 'warnings': []}, {'lambda_rest_aa': 1548.204},
        budget_left=None)
    e = systems[0]
    ca45 = [w for w in e['warnings'] if w['code'] == 'CA-45']
    assert ca45 and '不得当 1σ 读' in ca45[0]['message']
    assert 'quadrature' in ca45[0]['message']
    assert 'wing_logn_alt' in e and e['wing_logn_alt'] == 19.9   # 族 B 同屏
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)
    with open(os.path.join(FRONT_SPECPHOT, 'lines_ui.js'), encoding='utf-8') as f:
        ui = f.read()
    assert 'TXT24_CA45' in ui and '主导项是连续谱安置' in ui


# ═══ T-74：交叉链接闸门与「钉住者不双计」（F-103/F-104）══════════════════════

def test_t74_band_frac_reuses_s1_arithmetic():
    """① 波段通带被吸收系统掩掉 ~25% ⇒ frac 与 S1 band_integrals 的
    n_masked/(n_masked+n_used) 同值（复用同口径，不另算）。"""
    from specphot.response import band_integrals
    lam = np.arange(4000.0, 5000.0, 1.0)
    flux = (np.arange(lam.size, dtype=float) * 0.0) + C0
    segs = [[4750.0, 4999.0]]                      # 通带 4000–5000 内 25%
    mask = (lam >= 4750.0) & (lam <= 4999.0)
    cl, ct = [4000.0, 5000.0], [1.0, 1.0]
    rows = _diag.cross_link_band_fracs(lam, mask, [('t', cl, ct)])
    integ = band_integrals(lam.tolist(), flux.tolist(), cl, ct,
                           mask_ranges=segs)
    frac_si = integ['n_masked_pixels'] / (integ['n_masked_pixels']
                                          + integ['n_used_pixels'])
    assert abs(rows[0]['bands_masked_frac'] - frac_si) < 1e-12
    assert rows[0]['bands_masked_frac'] > 0.20      # > C_XLINK_MASK_MAX ⇒ CA-46①
    assert rows[0]['n_masked_pixels'] == integ['n_masked_pixels']


def _stage_with_m6(monkeypatch, row_over=None, spec_lam=(3600.0, 4700.0),
                   flags=None):
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    lam = np.arange(spec_lam[0], spec_lam[1], 1.0)
    row = {'detected': True, 'lambda_obs_vac_aa': 1548.204 * (1.0 + Z_SYS),
           'lambda_err_aa': 0.2, 'depth': 0.5, 'depth_err_lo': 0.02,
           'depth_err_hi': 0.02, 'snr_res': 50.0, 'n_pix_per_res': 3.0,
           'lambda_center_input_aa': 1548.204 * (1.0 + Z_SYS),
           'warnings': []}
    row.update(row_over or {})
    from specphot import lines_api as _la
    out = _la._absorber_stage(
        lam, np.full(lam.size, C0), np.full(lam.size, 3e-17),
        flags or {'absorber_ident': True, 'wing_logn': True,
                  'metal_sat_check': True},
        Z_SYS, True, None, 'none', row, {'lambda_rest_aa': 1548.204},
        budget_left=None)
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)
    return row, out


def test_t74_metal_route_z_and_cross_link_line_center(monkeypatch):
    """②③ metal_lines 路由：z_abs/z_err 由被链接金属线行换元（F-70）；
    线心落蓝侧掩膜段 ⇒ 线行挂 CA-46 并计 n_lines_in_masked_absorbers；
    钉住量不双计（b_source/metadata、z_abs 只走 covariance 一条）。"""
    row, (systems, gate, _rs) = _stage_with_m6(
        monkeypatch, row_over={'lambda_center_input_aa': 3630.0})
    e = systems[0]
    assert e['z_source'] == 'metal_lines'
    assert abs(e['z_abs'] - Z_SYS) < 1e-3
    assert e['z_err'] is not None and e['z_err'] > 0
    assert e['masked_ranges'] and e['blue_side_igm_masked'] is True
    assert gate['blue_side_igm_masked'] is True
    assert gate['n_lines_in_masked_absorbers'] == 1          # 线心在掩膜段内
    assert any(w['code'] == 'CA-46' for w in row['warnings'])
    assert e['err_source']['wing_logn'] == 'profile'         # 剖面一条
    assert e['err_source'].get('wing_b_assumption_kms') == 'metadata'
    assert e['err_source']['z_abs'] == 'covariance'
    for pinned in ('wing_b_assumption_kms', 'b_source'):
        assert e['err_source'].get(pinned) == 'metadata'     # 不进 profile/cov


def test_t74_no_sat_err_columns_and_aod_notes(monkeypatch):
    """④⑥ sat_flag 只由被链接线行 depth 导出（无 sat_*_err 列）；AOD 适用性
    双支反例 ⇒ notes 点名是哪一支；双向 notes + 单云声明常驻；输入数组不被改。"""
    lam = np.arange(3600.0, 4700.0, 1.0)
    flux = np.full(lam.size, C0)
    sig = np.full(lam.size, 3e-17)
    flux0, sig0 = flux.copy(), sig.copy()
    row, (systems, _g, _rs) = _stage_with_m6(
        monkeypatch, row_over={'snr_res': 5.0, 'n_pix_per_res': 1.0,
                               'depth': 0.97},
        flags={'absorber_ident': True, 'wing_logn': False,
               'metal_sat_check': True})
    e = systems[0]
    assert e['sat_flag'] == 'saturated'                  # 1−0.97 < 0.05
    assert not [k for k in e if k.startswith('sat_') and k.endswith('_err')]
    with open(os.path.join(FRONT_SPECPHOT, 'export.js'), encoding='utf-8') as f:
        assert not [c for c in _ABS_COLS_SPEC
                    if c.startswith('sat_') and c.endswith('_err')]
    notes = '；'.join(e['notes'])
    assert 'C_AOD_SNR_RES_MIN' in notes and 'C_AOD_SAMP_MIN' in notes
    assert '偏高' in notes and '偏低' in notes            # 双向（F-104②）
    assert 'single absorbing cloud' in notes             # F-104⑤ 常驻
    # 适用性满足 ⇒ notes 不点名哪支
    row2, (systems2, _g2, _r2) = _stage_with_m6(
        monkeypatch, row_over={'depth': 0.3},
        flags={'absorber_ident': True, 'wing_logn': False,
               'metal_sat_check': True})
    e2 = systems2[0]
    assert e2['sat_flag'] == 'unsaturated'
    notes2 = '；'.join(e2['notes'])
    assert 'C_AOD_SNR_RES_MIN' in notes2                 # 适用性说明在场
    assert '不适用（' not in notes2
    assert np.array_equal(flux, flux0) and np.array_equal(sig, sig0)
    assert np.array_equal(flux, flux0) and np.array_equal(sig, sig0)


def test_t74_b_envelope_monotonic_and_budget_pool():
    """④⑤ b 包络随 b 单调（logN(b=10) ≥ logN(b=25) ≥ logN(b=50)，低 N 翼）；
    共享池余量不足 ⇒ 回落不出 + 书面原因（F-95③）。"""
    spec = _wing_spec(logn=18.7)
    lam, flux, sigma = (np.asarray(spec['lam_aa']), np.asarray(spec['flux']),
                        np.asarray(spec['flux_err']))
    rows, reason = _diag.b_envelope(
        lam, flux, sigma, z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC,
        f_osc=F_LYA, budget_left=None)
    assert reason is None and len(rows) == 3
    lns = [r['wing_logn'] for r in rows]
    assert lns[0] >= lns[1] >= lns[2] and lns[0] - lns[2] > 0.005
    rows2, reason2 = _diag.b_envelope(
        lam, flux, sigma, z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC,
        f_osc=F_LYA, budget_left=100)
    assert rows2 == [] and 'C_BOOT_N_BUDGET' in reason2 and '回落' in reason2


# ═══ 闸门态与解锁条件（U-48 + M-4/M-6；HTTP 层）══════════════════════════════

def test_u48_gate_entry_library_default(client):
    """M-6 缺（库内现状）+ absorber_ident ⇒ 200 + 闸门格：值键全 null +
    书面原因（F-94③/F-105①）；M-4/M-6 完成前的合法降级形态。"""
    _login(client)
    spec = _metal_wing_spec()
    body = _line_body(spec, 1253.0, diagnostics={'absorber_ident': True})
    _clear_cache()
    d = _json(_post(client, body))
    assert r_ok(d)
    assert d['diagnostics']['forest_stats'] is None
    e = d['diagnostics']['absorber_systems'][0]
    for k in ('z_abs', 'z_err', 'z_source', 'class', 'wing_logn',
              'wing_spread_dex', 'lambda_rest_lya_vac_aa', 'b_source'):
        assert e[k] is None, k
    assert 'M-6' in e['reason'] and 'Lyα' in e['reason']
    assert e['masked_ranges'] == [] and e['metal_line_ids'] == []


def test_u48_z_source_missing_gate(client, monkeypatch):
    """M-6 有 Lyα 但请求无显式 z、线心不匹配金属线 ⇒ z_abs 无输入源闸门格。"""
    _login(client)
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    spec = _wing_spec(span_kms=21000.0)
    spec['meta'].pop('z')                       # 谱记录也无 z ⇒ user_z 路由缺
    body = _line_body(spec, 3760.0, diagnostics={'absorber_ident': True})
    try:
        _clear_cache()
        d = _json(_post(client, body))
        assert r_ok(d)
        e = d['diagnostics']['absorber_systems'][0]
        assert 'z_abs 无输入源' in e['reason']
        assert e['z_source'] is None and e['wing_logn'] is None
    finally:
        monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


def test_u48_full_numeric_with_fixture(client, monkeypatch):
    """解锁态（合成 fixture 模拟 M-6 复核）：三开关全开 ⇒ metal_lines 路由
    z_abs、engineering_default b、两族 log N、class、蓝侧第五类掩膜（mask_hash
    变化）、TXT-25 reasons、notes 双向/单云；err_source[wing_logn]='profile'。"""
    _login(client)
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    try:
        spec = _metal_wing_spec()
        body = _line_body(spec, 1548.204,
                          diagnostics={'absorber_ident': True,
                                       'wing_logn': True,
                                       'metal_sat_check': True})
        _clear_cache()
        d = _json(_post(client, body))
        assert d['diagnostics']['forest_stats'] is None
        e = d['diagnostics']['absorber_systems'][0]
        assert e['z_source'] == 'metal_lines' and e['z_err'] is not None
        assert e['b_source'] == 'engineering_default'
        assert e['wing_b_assumption_kms'] == C_WING_B_KMS
        assert e['wing_logn'] is not None and e['wing_logn_alt'] is not None
        assert e['wing_spread_dex'] is not None
        assert e['class'] in ('dla', 'sub_dla', 'lls')
        assert e['err_source']['wing_logn'] == 'profile'
        assert e['class_thresholds_dex']['dla'] == \
            math.log10(C_ABS_CLASS_DLA)
        assert e['blue_side_igm_masked'] is True and e['masked_ranges']
        assert e['sat_flag'] in ('saturated', 'unsaturated')
        notes = '；'.join(e['notes'])
        assert 'single absorbing cloud' in notes
        # 第五类掩膜 ⇒ mask_hash 与关闭态不同（absorber 槽非空）
        _clear_cache()
        d0 = _json(_post(client, _line_body(spec, 1548.204)))
        assert d0['mask_hash'] != d['mask_hash']
        assert d0['diagnostics']['cross_link_gate']['enabled'] is False
        assert d['diagnostics']['cross_link_gate']['enabled'] is True
    finally:
        monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


# ═══ F-103① 第五类掩膜（preprocess.merge_masks 的 absorber 槽）══════════════

def test_f103_fifth_mask_category_merge_and_hash():
    from specphot import preprocess as pre
    lam = np.arange(4000.0, 5000.0, 1.0)
    m0 = pre.merge_masks(lam, [])
    m1 = pre.merge_masks(lam, [], absorber_ranges=[[4500.0, 4525.0]])
    assert m0['absorber'] == [] and m1['absorber'] == [[4500.0, 4525.0]]
    assert m1['n_masked'] == 26 and m0['n_masked'] == 0
    assert bool(np.any(m1['mask_bool'])) and not bool(np.any(m0['mask_bool']))
    assert pre.mask_hash(m0, lam, 'h') != pre.mask_hash(m1, lam, 'h')
    # V-13 过滤（越界段被滤）+ 去重：同段重复申报不改变掩膜与哈希（W-36 同族）
    m2 = pre.merge_masks(lam, [], absorber_ranges=[[4500.0, 4525.0],
                                                   [4500.0, 4525.0],
                                                   [9000.0, 9100.0]])
    assert m2['absorber'] == [[4500.0, 4525.0]]
    assert pre.mask_hash(m1, lam, 'h') == pre.mask_hash(m2, lam, 'h')


# ═══ P3d 评审修复（4 P1 + 5 P2；登记 docs/TECHNICAL.md P3d 评审修复段）═══════

def test_p1_1_cross_link_gate_keyset(client, monkeypatch):
    """P1-1：cross_link_gate 键集 = §4.2 原文七键 + 实现追加键
    {enabled, bands_ca46}（追加键登记 docs/TECHNICAL.md）；未启用态
    n_bands_over_gate=0、b_source/z_source=null（F-105① 的"未启用"值）。"""
    _login(client)
    spec = _metal_wing_spec()
    # 关闭态：未启用值
    _clear_cache()
    d0 = _json(_post(client, _line_body(spec, 1548.204)))
    g0 = d0['diagnostics']['cross_link_gate']
    assert set(g0) == {'bands_masked_frac', 'n_bands_over_gate',
                       'n_lines_in_masked_absorbers', 'blue_side_igm_masked',
                       'b_source', 'z_source', 'note', 'enabled',
                       'bands_ca46'}
    assert g0['enabled'] is False and g0['n_bands_over_gate'] == 0
    assert g0['b_source'] is None and g0['z_source'] is None
    # 开启态（M-6 fixture）：键集恒定、b/z source 回显钉住路由、无波段积分 ⇒
    # bands_masked_frac 恒空、n_bands_over_gate=0（F-103① 判定量在 API-2 侧）
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    try:
        _clear_cache()
        d = _json(_post(client, _line_body(spec, 1548.204,
                                           diagnostics={'absorber_ident': True})))
        g = d['diagnostics']['cross_link_gate']
        assert set(g) == set(g0)
        assert g['enabled'] is True
        assert g['b_source'] == 'engineering_default'
        assert g['z_source'] == 'metal_lines'
        assert g['bands_masked_frac'] == [] and g['n_bands_over_gate'] == 0
        assert isinstance(g['n_bands_over_gate'], int)
    finally:
        monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


def test_p1_3_absorber_system_masked_top_level(client, monkeypatch):
    """P1-3：顶层 diagnostics.absorber_system_masked 恒在场（§4.2 键位）；
    值 = 各系统 masked_ranges 的并集（同源，不另立掩膜通路）；U-48 关 ⇒ []。"""
    _login(client)
    spec = _metal_wing_spec()
    _clear_cache()
    d0 = _json(_post(client, _line_body(spec, 1548.204)))
    assert d0['diagnostics']['absorber_system_masked'] == []
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    try:
        _clear_cache()
        d = _json(_post(client, _line_body(
            spec, 1548.204, diagnostics={'absorber_ident': True})))
        systems = d['diagnostics']['absorber_systems']
        assert systems and systems[0]['masked_ranges']
        assert d['diagnostics']['absorber_system_masked'] == \
            systems[0]['masked_ranges']
    finally:
        monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


def test_p1_4_z_envelope_unit():
    """P1-4/F-99① 单元：z_err 不可得 ⇒ 不出 + 书面原因；正常 ⇒ 钉住值居中、
    三点 z 升序、窗宽 = C_WING_Z_WIN_SIGMA·σ_z；共享池余量不足 ⇒ 回落不出
    （F-95③）。"""
    spec = _wing_spec(logn=18.7)
    lam = np.asarray(spec['lam_aa'])
    flux = np.asarray(spec['flux'])
    sigma = np.asarray(spec['flux_err'])
    kw = dict(z_abs=Z_SYS, lambda_rest_lya_vac_aa=LYA_VAC, f_osc=F_LYA,
              b_kms=C_WING_B_KMS)
    rows, reason = _diag.z_envelope(lam, flux, sigma, z_err=None, **kw)
    assert rows == [] and 'z_err' in reason and 'F-99①' in reason
    zerr = 6.5e-5
    rows, reason = _diag.z_envelope(lam, flux, sigma, z_err=zerr, **kw)
    assert reason is None and len(rows) == 3
    zs = [r['z_abs'] for r in rows]
    assert zs == sorted(zs) and rows[1]['z_abs'] == Z_SYS
    dz = C_WING_Z_WIN_SIGMA * zerr
    assert abs(zs[0] - (Z_SYS - dz)) < 1e-12 and abs(zs[2] - (Z_SYS + dz)) < 1e-12
    rows2, reason2 = _diag.z_envelope(lam, flux, sigma, z_err=zerr,
                                      budget_left=100, **kw)
    assert rows2 == [] and 'C_BOOT_N_BUDGET' in reason2


def test_p1_4_logn_spread_over_z_assembly(monkeypatch):
    """P1-4/F-99① 装配：metal_lines 路由（z_err 可得）⇒ logn_spread_over_z
    回显（追加键、恒在场，只作灵敏度展示、不进 err_source）；user_z 路由
    （z_err 不可得）⇒ null + 书面原因（F-94③）。"""
    row, (systems, _g, _rs) = _stage_with_m6(monkeypatch)
    e = systems[0]
    assert e['z_source'] == 'metal_lines'
    assert e['logn_spread_over_z'] is not None and e['logn_spread_over_z'] >= 0
    assert 'logn_spread_over_z' not in e['err_source']     # 不进误差通路
    row2, (systems2, _g2, _r2) = _stage_with_m6(
        monkeypatch, row_over={'detected': False, 'lambda_obs_vac_aa': None,
                               'lambda_err_aa': None})
    e2 = systems2[0]
    assert e2['z_source'] == 'user_z'
    assert e2['logn_spread_over_z'] is None
    assert any('z 窗口灵敏度' in n and 'z_err' in n for n in e2['notes'])


def test_p1_2_ca46_band_rows_api2(client, monkeypatch):
    """P1-2（HTTP 层）：U-48 absorber_ident 开 + M-6 fixture 给 Lyα + 谱带 z=2
    ⇒ 吸收系统第五类掩膜并入（mask_hash 第 4 槽），通带被掩占比 >
    C_XLINK_MASK_MAX 的波段行挂 CA-46（①支）且 diagnostics.cross_link_gate
    回显；关 ⇒ 无 CA-46、无 cross_link_gate（S1 不产 absorber_systems）。"""
    _login(client)
    monkeypatch.setattr(_reg, 'M6_LINE_TABLE', _m6_table())
    try:
        lam = np.arange(3600.0, 4000.0, 1.0)
        spec = {'lam_aa': lam.tolist(),
                'flux': np.full(lam.size, C0).tolist(),
                'flux_err': np.full(lam.size, 3e-17).tolist(),
                'meta': {'z': 2.0, 'category': 'other'}}
        ph_body = {'spectrum': spec,
                   'bands': ['test-p3d'],
                   'custom_curves': [{'band': 'test-p3d',
                                      'lam_aa': [3590.0, 3640.0, 3690.0],
                                      't': [1.0, 1.0, 1.0],
                                      'curve_kind': 'unknown'}],
                   'weighting': 'photon',
                   'anchor_rows': [{'band': 'test-p3d', 'mag': 16.0,
                                    'mag_system': 'AB', 'mag_err': 0.05,
                                    'mjd': 60000.0}]}
        _clear_cache()
        d_on = _json(client.post('/api/specphot/photometry', json=dict(
            ph_body, diagnostics={'absorber_ident': True})))
        _clear_cache()
        d_off = _json(client.post('/api/specphot/photometry', json=ph_body))
        row = next(r for r in d_on['results'] if r['band'] == 'test-p3d')
        ca46 = [w for w in row['warnings'] if w['code'] == 'CA-46']
        assert ca46 and 'absorber_mask_frac_over' == ca46[0]['reason']
        assert 'C_XLINK_MASK_MAX' in ca46[0]['message']
        gate = d_on['diagnostics']['cross_link_gate']
        frac = gate['bands_masked_frac'][0]
        assert frac > C_XLINK_MASK_MAX
        assert gate['n_bands_over_gate'] == 1
        assert gate['blue_side_igm_masked'] is True
        assert 'absorber_systems' not in d_on['diagnostics']   # 家族结果只在 API-4
        assert 'diagnostics_ignored' not in d_on               # absorber_ident 已消费
        # 第五类掩膜 ⇒ mask_hash 变化（第 4 槽非空）
        assert d_on['mask_hash'] != d_off['mask_hash']
        # 关 ⇒ 无 CA-46、无 cross_link_gate
        blob = json.dumps(d_off, ensure_ascii=False)
        assert 'CA-46' not in blob
        assert 'cross_link_gate' not in d_off['diagnostics']
    finally:
        monkeypatch.setattr(_reg, 'M6_LINE_TABLE', None)


# ═══ W-35：前端走查（源码级扫描）══════════════════════════════════════════

def test_w35_frontend_source_scan():
    with open(os.path.join(FRONT_SPECPHOT, 'lines_ui.js'), encoding='utf-8') as f:
        ui = f.read()
    for k in ('absorber_ident', 'wing_logn', 'metal_sat_check'):
        assert f'data-k="{k}"' in ui, k                  # U-48 三开关渲染
    assert 'TXT24' in ui and 'TXT25' in ui and 'TXT24_CA45' in ui
    # 联动：未勾 ident 时另两项禁用；R 不可得 ⇒ wing_logn 禁用（F-69）
    assert "(!p.absorber_ident || nOr(p.R) == null || computing) ? 'disabled'" in ui
    assert '只随 API-4 提交' in ui
    # 结果卡同屏：两族 logN + 四端点 + spread 徽章 + b/z source + class + thresholds
    for needle in ('wing_logn_alt', 'wing_logn_alt_err_hi', 'wing_spread_dex',
                   'b_source', 'z_source', 'class_thresholds_dex', 'sat_flag',
                   'TXT-25', 'forest_stats_reasons', 'cross_link_gate'):
        assert needle in ui, needle
    # 导出按钮 + 请求侧只在 ident 勾选时携带另两项（不静默连带开启）
    assert 'sp-lexpabs' in ui
    assert 'if (p.absorber_ident) {' in ui and 'diag.absorber_ident = true;' in ui


# ═══ 真实 M-4/M-6 数据重跑登记（照 T-10/T-51 先例 skip）══════════════════════

@pytest.mark.skip(reason='T-69…T-74 的数值路径已按合成口径全测（本文件）；验收'
                         '原文要求在 M-4（R）与 M-6（复核线表）真实落地后重跑'
                         '全部走查——两前置未完成前生产请求恒为闸门格（F-98①），'
                         '照 T-10/T-21/T-51 先例登记 skip。')
def test_p3d_rerun_on_m4_m6_completed():
    raise AssertionError('M-4/M-6 未完成')
