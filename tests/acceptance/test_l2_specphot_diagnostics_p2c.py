"""L2: specphot **P2c 收口验收**——诊断增强 S1/S2 侧三键（02b §3.12 / §9 P2c 行）。

  T-49  resp_perturb（F-83）：perturb_n 60 → 240 ⇒ mag_err_resp 相对变化 < 20%
        （纯函数层，seed 按 (spec_hash, band, 组号) 派生 ⇒ 确定性）；resp_method
        开关关闭恒 'lower_bound'、开启为 'perturbation' 并回显组数；开启后
        mag_err_resp ≥ 下限值（扰动不得让误差变小，CA-39 可达）
  T-50  beta_matrix + anchor_reinsert（F-82/F-84）：纯幂律 + 单波段离群锚点 ⇒
        该格 β_ij 超 3σ_β 且 CA-40 可达、矩阵旁常驻 causal_use='diagnostic_only'；
        m_syn_local 与主列 mag 不同值但主列不变（局部改正不得回流，F-11）
  T-48  后半（本期可测部分）：三键各单独开启 ⇒ 只增 diagnostics{} 子键与
        warnings[]（resp_perturb 按 T-49 原文另有 mag_err_resp/resp_method 两列
        换口径；anchor_reinsert 另有 m_syn_local 列从 null 变值——均为 §4.3 的
        列集追加位/明文例外），其余主结果列名与数值不变；全关恒等（六键全 false
        与基线逐字节同）已由 test_l2_specphot_p1_acceptance.py 的 T-48① 承载
  W-32  前半服务端可自动部分：恒等 + 单开增量 + CA-39/CA-40 分档可达；前端
        半段的源码级扫描（P3c 徽章禁用 + tooltip 指期次 + 每项旁的新增列/规模
        文案 + groupOf 分档）在本文件末尾
  P2c 收口评审（P1-1/P1-2/P2-1）：σ_β 的 κ* 协方差按参与分档（+2/0/−2·
        Var(κ)_mag，纯函数 + GLS 蒙特卡洛 + API 三层量级/方向断言）；
        color_err_cal 改 §3.9.3.1 杠杆式（同一 κ* 精确定消 0.0 / null+TXT-23
        书面原因）；CA-02 文案随 resp_method 分档
  Q-27  P3c 三键仍 501 phase='P3c'（分键闸）；diagnostics 只收布尔

元判据（§10.1）：随机判据 seed = 20260927；合成谱为无噪声纯幂律（T-50 的
「纯幂律」前提），离群量 +0.6 mag 经扫描定位在 |β| > 3σ_β 与 CA-40 双可达
窗口内。测试只读：上传件不落盘、无 DB 写（T-24 快照口径不变）。
"""
import json
import math
import os
import sys
import time

import numpy as np
import pytest

from app import create_app

import specphot
from specphot import diagnostics as DIAG
from specphot import fluxcal
from specphot.constants import C_AA_PER_S, C_SHAPE_PERT_EPS, C_SHAPE_PERT_P

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONT_SPECPHOT = os.path.join(_REPO_ROOT, 'frontend', 'js', 'specphot')
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))

SEED = 20260927
P2C_KEYS = ('beta_matrix', 'resp_perturb', 'anchor_reinsert')
P3C_KEYS = ('z_from_lines', 'frame_probe', 'sky_subtract')


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _json(resp):
    return json.loads(resp.get_data(as_text=True))


def _login(client, name='p2c-diagnostics-tester'):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = name
        s['role'] = 'user'


def _clear_result_cache():
    with specphot._cache_lock:
        specphot._result_cache.clear()


# ─── 合成工装：无噪声纯幂律（Fν cgs，AB 星等落在合理域内）+ 三条 60 节点
# 高斯通带（resp_perturb 需要：3 节点三角曲线对形状扰动退化，见 T-49 用例注） ──
A_FNU = 10 ** (-(16.0 + 48.6) / 2.5)          # pivot 处 m_AB ≈ 16（sane 域内）
BETA_TRUE, Z_TRUE = 0.8, 0.0
LAMS = [4000.0 + 5.0 * i for i in range(260)]  # 4000–5295 Å
MJD = 60000.0
BANDS = {'d-b1': (4100.0, 250.0), 'd-b2': (4600.0, 250.0), 'd-b3': (5100.0, 250.0)}


def _powerlaw_flux():
    lam = np.asarray(LAMS, dtype=float)
    nu = C_AA_PER_S / lam
    fnu = A_FNU * (nu / (C_AA_PER_S / 4650.0)) ** (-BETA_TRUE)
    return (fnu * C_AA_PER_S / lam ** 2).tolist()      # Fλ cgs（观测系，z=0）


def _custom_curves():
    out = []
    for b, (c, w) in BANDS.items():
        x = np.linspace(c - 2.5 * w, c + 2.5 * w, 60)
        t = np.exp(-0.5 * ((x - c) / w) ** 2)
        out.append({'band': b, 'lam_aa': x.tolist(), 't': t.tolist(),
                    'curve_kind': 'unknown'})
    return out


def _anchors(outlier=None):
    """三波段手加锚点（AB、带 mjd ⇒ F-61 容差内成格），星等严格按谱的纯幂律
    色彩取值：Fν ∝ ν^−β = λ^+β ⇒ m 随 λ 递减，mag_i = C − 2.5β·log10(λ_i/λ_ref)
    （干净格 ΔC_obs − ΔC_syn ≈ 0 ⇒ CA-40 的可达性只来自离群锚点）；
    outlier 波段整体加 +0.6 mag 偏移。"""
    rows = []
    for b in BANDS:
        mag = 16.0 - 2.5 * BETA_TRUE * math.log10(BANDS[b][0] / BANDS['d-b1'][0])
        rows.append({'band': b, 'mag': mag + (0.6 if b == outlier else 0.0),
                     'mag_system': 'AB', 'mag_err': 0.05, 'mjd': MJD})
    return rows


def _body(anchor_rows, **over):
    body = {'spectrum': {'lam_aa': list(LAMS), 'flux': _powerlaw_flux(),
                         'flux_err': None,
                         'meta': {'mjd': MJD, 'category': 'other'}},
            'bands': list(BANDS), 'custom_curves': _custom_curves(),
            'weighting': 'photon', 'anchor_rows': anchor_rows}
    body.update(over)
    return body


def _post(client, body):
    _clear_result_cache()
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 200, _json(r)
    return _json(r)


def _main_part(resp):
    """T-48 比较域：diagnostics{}（恒在场、不在域内）与 warnings[]（只增）除外。"""
    return {k: v for k, v in resp.items() if k not in ('diagnostics', 'warnings')}


# ═══ T-48①（本期回归）：全关仍逐字节同 + P3c 分键闸 ═══════════════════════

def test_t48_all_off_still_byte_identical(client):
    _login(client)
    body = _body(_anchors())
    base = _post(client, body)
    assert base['diagnostics'] == {}
    for dg in ({}, {k: False for k in P2C_KEYS + P3C_KEYS}):
        r2 = _post(client, dict(body, diagnostics=dg))
        assert json.dumps(_main_part(base), sort_keys=True) == \
            json.dumps(_main_part(r2), sort_keys=True)
        assert r2['diagnostics'] == {}
        assert r2['warnings'] == base['warnings']


def test_q27_p3c_gate_state(client):
    """P3c 后 Q-27 分键闸定态：sky_subtract 的消费点在 S3 步 1 的 U-31（F-86），
    本 S1/S2 测光路径不接受 ⇒ 仍 501（指路不冒充）；z_from_lines/frame_probe
    走真实现 + M-6 闸（null 块），由 test_l2_specphot_p3c.py 两态承载。"""
    _login(client)
    _clear_result_cache()
    r = client.post('/api/specphot/photometry',
                    json=dict(_body(_anchors()), diagnostics={'sky_subtract': True}))
    d = _json(r)
    assert r.status_code == 501 and d['phase'] == 'P3c'
    assert d.get('reason') == 'sky_subtract_use_u31'


# ═══ T-49：resp_perturb（F-83） ═══════════════════════════════════════════

def test_t49_resp_perturb_api_contract(client):
    """开关关闭恒 lower_bound；开启换 perturbation + 回显组数 + CA-39；开启后
    mag_err_resp ≥ 下限值；除 mag_err_resp/resp_method/diagnostics/warnings 外
    与基线逐项相同（T-48 的明文例外域）。"""
    _login(client)
    body = _body(_anchors())
    base = _post(client, body)
    assert all(r['resp_method'] == 'lower_bound' for r in base['results'])
    on = _post(client, dict(body, diagnostics={'resp_perturb': True}))
    rp = on['diagnostics']['resp_perturb']
    assert rp['resp_method'] == 'perturbation'
    assert rp['perturb_n'] == C_SHAPE_PERT_P and rp['perturb_n_requested'] == C_SHAPE_PERT_P
    assert rp['downscaled'] is False and rp['eps'] == C_SHAPE_PERT_EPS
    assert all(r['resp_method'] == 'perturbation' for r in on['results'])
    for rb, ro in zip(base['results'], on['results']):
        assert ro['mag_err_resp'] >= rb['mag_err_resp'], (rb['band'], 'CA-39')
    # CA-02 文案随 resp_method 分档（§5.4；P2c 评审 P2-1）
    for r in base['results']:
        ca02 = next(w for w in r['warnings'] if w['code'] == 'CA-02')
        assert ca02['message'] == 'σ_resp 为两加权之差的下限（F-57）'
    for r in on['results']:
        ca02 = next(w for w in r['warnings'] if w['code'] == 'CA-02')
        assert ca02['message'] == 'σ_resp 为扰动散布与两加权之差下限的较大者（F-83）'
    # 其余全部键（含主量 mag/f_mjy/mag_err_stat/mag_err_cal/delta_m*）不变
    b2 = json.loads(json.dumps(_main_part(base)))
    o2 = json.loads(json.dumps(_main_part(on)))
    for rb, ro in zip(b2['results'], o2['results']):
        rb.pop('mag_err_resp'), ro.pop('mag_err_resp')
        rb.pop('resp_method'), ro.pop('resp_method')
        # 行级 CA-02 文案随 resp_method 分档（P2-1，§5.4）⇒ 与 resp_method
        # 同属一次「换口径」行为，一并出比较域
        rb['warnings'] = [w for w in rb['warnings'] if w['code'] != 'CA-02']
        ro['warnings'] = [w for w in ro['warnings'] if w['code'] != 'CA-02']
    assert b2 == o2
    # warnings 只增：前缀 = 基线，新增含 CA-39
    assert on['warnings'][:len(base['warnings'])] == base['warnings']
    assert any(w['code'] == 'CA-39' for w in on['warnings'][len(base['warnings']):])


def test_t49_perturb_n_60_vs_240_within_20pct():
    """T-49 前半（纯函数层）：perturb_n 60 → 240 ⇒ mag_err_resp 相对变化 < 20%。
    60 节点高斯通带（3 节点曲线对低频形状扰动退化：峰值单点缩放被 F-1 零阶
    齐性吸收）；种子按 (spec_hash, band, 组号) 派生 ⇒ 确定性可复算。"""
    lam = np.asarray(LAMS, dtype=float)
    flux = np.asarray(_powerlaw_flux())
    defs = {c['band']: {'lam': c['lam_aa'], 't': c['t']} for c in _custom_curves()}
    s60, _i60 = DIAG.resp_perturb(lam, flux, defs, list(defs), weighting='photon',
                                  mask_ranges=None,
                                  sigma_lnl={b: 0.02 for b in defs},
                                  seed_base=SEED, deadline=time.monotonic() + 30)
    s240, _i240 = DIAG.resp_perturb(lam, flux, defs, list(defs), weighting='photon',
                                    mask_ranges=None,
                                    sigma_lnl={b: 0.02 for b in defs},
                                    seed_base=SEED, deadline=time.monotonic() + 30,
                                    p_req=240)
    for b in defs:
        assert s60[b] is not None and s240[b] is not None, b
        assert abs(s240[b] - s60[b]) < 0.2 * s60[b], b
    # 确定性：同 seed 两次逐位相同（F-93⑤ 同族）
    s60b, _ = DIAG.resp_perturb(lam, flux, defs, list(defs), weighting='photon',
                                mask_ranges=None, sigma_lnl={b: 0.02 for b in defs},
                                seed_base=SEED, deadline=time.monotonic() + 30)
    assert s60b == s60
    # 量级合理：C_SHAPE_PERT_EPS=0.05 的形状误差给 1e-3 mag 量级色项（F-17 文案）
    assert 0.0 < s60['d-b1'] < 0.05


# ═══ T-50：beta_matrix + anchor_reinsert（F-82/F-84） ═════════════════════

def test_t50_outlier_anchor_flags_cell_and_ca40(client):
    """纯幂律 + 单波段离群锚点 ⇒ 含离群波段的格 |β| > 3σ_β、CA-40 可达、
    causal_use='diagnostic_only' 常驻；m_syn_local 与主列 mag 不同值但主列不变。"""
    _login(client)
    clean = _post(client, _body(_anchors()))          # 干净锚点：无诊断、无 CA-40
    assert clean['diagnostics'] == {}
    body_out = _body(_anchors(outlier='d-b2'))
    out = _post(client, dict(body_out, diagnostics={'beta_matrix': True,
                                                    'anchor_reinsert': True}))
    bm = out['diagnostics']['beta_matrix']
    assert bm['causal_use'] == 'diagnostic_only'
    assert '不得作为物理' in bm['note'] and '发现不一致' in bm['note']
    cells = {(c['band_i'], c['band_j']): c for c in bm['cells']}
    assert set(cells) == {('d-b1', 'd-b2'), ('d-b1', 'd-b3'), ('d-b2', 'd-b3')}
    # 含离群波段的格 |β| 超 3σ_β；干净格 (d-b1,d-b3) 不超
    hot = [c for c in cells.values()
           if 'd-b2' in (c['band_i'], c['band_j'])]
    assert hot and all(abs(c['beta']) > 3.0 * c['sigma_beta'] for c in hot)
    cool = cells[('d-b1', 'd-b3')]
    assert abs(cool['beta']) <= 3.0 * cool['sigma_beta']
    assert any(w['code'] == 'CA-40' for w in out['warnings'])
    # anchor_reinsert：离群波段的 m_syn_local = 局部改正值 ≠ 主列 mag
    ar = out['diagnostics']['anchor_reinsert']
    assert ar['causal_use'] == 'diagnostic_only'
    by_band = {c['band']: c for c in ar['cells']}
    row_b2 = next(r for r in out['results'] if r['band'] == 'd-b2')
    assert row_b2['m_syn_local'] is not None
    assert row_b2['m_syn_local'] != row_b2['mag']          # T-50：不同值
    assert abs(row_b2['m_syn_local'] - row_b2['m_obs']) < 1e-9   # 局部改正到位
    assert 0.0 < by_band['d-b2']['shrink_1_minus_h'] < 1.0       # (1−h_i) 并排
    # 主列不变：同一离群输入、诊断全关 vs 开，逐项相同（m_syn_local 列除外）
    base = _post(client, body_out)
    bc = json.loads(json.dumps(_main_part(base)))
    oc = json.loads(json.dumps(_main_part(out)))
    for rb, ro in zip(bc['results'], oc['results']):
        rb.pop('m_syn_local'), ro.pop('m_syn_local')
    assert bc == oc
    assert out['warnings'][:len(base['warnings'])] == base['warnings']


def test_t50_anchor_reinsert_requires_anchored(client):
    """非 anchored（direct）下 F-84 前提不成立 ⇒ m_syn_local 恒 null + 书面
    原因（TXT-23 同族），主结果不变、不 500。"""
    _login(client)
    body = _body(_anchors(), mode='direct',
                 spectrum={'lam_aa': list(LAMS), 'flux': _powerlaw_flux(),
                           'flux_err': None,
                           'meta': {'mjd': MJD, 'category': 'other',
                                    'flux_unit': 'erg/s/cm^2/Angstrom'}},
                 diagnostics={'anchor_reinsert': True})
    out = _post(client, body)
    assert out['mode_effective'] == 'direct'
    ar = out['diagnostics']['anchor_reinsert']
    assert ar['causal_use'] == 'diagnostic_only'
    assert all(c['m_syn_local'] is None for c in ar['cells'])
    assert 'mode_effective=direct' in ar['note']
    assert all(r['m_syn_local'] is None for r in out['results'])


def test_t50_beta_matrix_without_eligible_pairs(client):
    """锚点无时刻（F-77 no_time 支）⇒ 无格可成：cells=[]、note 在场、不 500。"""
    _login(client)
    rows = [{'band': b, 'mag': 16.0, 'mag_system': 'AB', 'mag_err': 0.05}
            for b in BANDS]                          # 无 mjd
    out = _post(client, _body(rows, diagnostics={'beta_matrix': True}))
    bm = out['diagnostics']['beta_matrix']
    assert bm['cells'] == [] and bm['causal_use'] == 'diagnostic_only'
    assert not any(w['code'] == 'CA-40' for w in out['warnings'])


# ═══ P2c 评审收口（P1-1/P1-2）：σ_β 的 κ* 协方差按参与分档 + ═══════════════
# ═══ color_err_cal 改 §3.9.3.1 杠杆式（同一 κ* 精确定消） ══════════════════

def test_p1_sigma_beta_cov_split_by_participation():
    """P1-1 纯函数层：κ* 协方差项按两行参与分档（F-94⑤「同波段对的 −2Cov」
    一阶精确形式）——都参与 +2Var(κ)_mag（GLS 残差反相关）、恰一行参与 0、
    都不参与 −2Var(κ)_mag（两行 delta_m_err 各含 +Var(κ)，恰好抵消）。
    同一物理配置（var_in=2.5e-3、Var(κ)_mag=1e-3 ⇒ h=0.4，由 h·var_in=Var(κ)
    恒等式）三种记账给同一真值 σ_β²·lg² = 5e-3；旧实现对参与对写 −2 ⇒ 1e-3
    （系统性低估）。color_err_cal：两行 σ_cal 在场 ⇒ 精确定消 0.0；任一行
    null ⇒ null + note 书面原因（TXT-23）。"""
    lg = math.log10(4600.0 / 4100.0)
    v_kappa, var_in = 1.0e-3, 2.5e-3
    h = v_kappa / var_in                       # F-56 恒等式：h_i·var_in = Var(κ)_mag
    dm2 = {'part': var_in * (1.0 - h), 'non': var_in + v_kappa}

    def row(band, part):
        return {'band': band, 'pivot_aa': 4100.0 if band == 'a' else 4600.0,
                'mag': 16.0, 'm_obs': 16.0, 'm_obs_mjd': 60000.0,
                'mag_err_stat': None, 'mag_err_resp': None,
                'mag_err_cal': math.sqrt(v_kappa),
                'delta_m_err': math.sqrt(dm2['part' if part else 'non']),
                'participating': part}

    cells, cross_expect = {}, {'pp': 2.0 * v_kappa, 'mx': 0.0, 'nn': -2.0 * v_kappa}
    for name, pa, pb in (('pp', True, True), ('mx', True, False),
                         ('nn', False, False)):
        block, _ = DIAG.beta_matrix([row('a', pa), row('b', pb)], 60000.0, 3.0,
                                    var_kappa_mag=v_kappa)
        cells[name] = block['cells'][0]
    for name, c in cells.items():
        dm_sum = (dm2['part' if name != 'nn' else 'non']
                  + (dm2['part'] if name == 'pp' else dm2['non']))
        # 三种记账都精确回到同一真值（评审：参与对旧值 0.00135 型 ⇒ 真 0.00405 型）
        assert c['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(
            dm_sum + cross_expect[name], rel=1e-12), name
        # 方向断言：cross 的符号/取值逐档核对
        assert c['sigma_beta'] ** 2 * lg ** 2 - dm_sum \
            == pytest.approx(cross_expect[name], rel=1e-12), name
    # 两行 σ_cal 在场 ⇒ 同一 κ* 在色中精确定消（P1-2，闭式 0.0）
    assert all(c['color_err_cal'] == 0.0 for c in cells.values())
    # 任一行 mag_err_cal=null ⇒ color_err_cal=null + TXT-23 书面原因（P1-2）
    r = row('b', True)
    r['mag_err_cal'] = None
    block_nocal, _ = DIAG.beta_matrix([row('a', True), r], 60000.0, 3.0,
                                      var_kappa_mag=v_kappa)
    assert block_nocal['cells'][0]['color_err_cal'] is None
    assert 'TXT-23' in block_nocal['note'] and '精确定消' in block_nocal['note']


def test_p1_sigma_beta_matches_gls_monte_carlo():
    """P1-1 实证层：三波段等权全参与（评审判例）——GLS 蒙特卡洛的 Var(ΔC)
    与 DIAG.beta_matrix 的 σ_β²·lg² 一致（rel 5%）。真值 = 2·var_in·(1−1/3)
    + 2·Var(κ)_mag = 0.005；旧口径（−2Var(κ)）给 0.001667（√3 倍低估，
    与评估 0.00135 vs 0.00405 的三倍比值同型）。种子固定 ⇒ 确定性。"""
    mag_per_lnf = 2.5 / math.log(10.0)
    sig_mag, n, ntrial = 0.05, 3, 40000
    f = np.ones(n)
    c_mat = np.diag((f * sig_mag / mag_per_lnf) ** 2)
    rng = np.random.default_rng(SEED)
    dms = np.empty((ntrial, n))
    for t in range(ntrial):
        g = f * np.exp(rng.normal(0.0, sig_mag / mag_per_lnf, size=n))
        k = fluxcal.anchored_fit(f.tolist(), g.tolist(), c_mat)['value']
        # dm_i = m_obs − (m_syn − MAG·ln κ*)：κ* 由同一批 GLS 权重实时解出
        dms[t] = -mag_per_lnf * np.log(g) + mag_per_lnf * math.log(k)
    rows = [{'band': f'b{i}', 'pivot_aa': p, 'mag': 16.0, 'm_obs': 16.0,
             'm_obs_mjd': 60000.0, 'mag_err_stat': None, 'mag_err_resp': None,
             'mag_err_cal': math.sqrt(sig_mag ** 2 / 3.0),
             'delta_m_err': math.sqrt(sig_mag ** 2 * (1.0 - 1.0 / 3.0)),
             'participating': True}
            for i, p in enumerate((4100.0, 4600.0, 5100.0))]
    block, _ = DIAG.beta_matrix(rows, 60000.0, 3.0,
                                var_kappa_mag=sig_mag ** 2 / 3.0)
    c12 = next(c for c in block['cells'] if c['band_i'] == 'b0' and c['band_j'] == 'b1')
    lg = math.log10(4600.0 / 4100.0)
    emp = float(np.var(dms[:, 0] - dms[:, 1]))
    assert c12['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(emp, rel=0.05)
    assert c12['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(0.005, rel=0.01)


def test_t50_sigma_beta_magnitude_and_color_cal_anchored(client):
    """P1-1/P1-2 API 层：三波段等权、全参与、锚点 σ=0.05、flux_err=None
    （σ_stat=null、C 对角）⇒ 每格 σ_β²·lg² = δm_i²+δm_j²+resp²+2·Var(κ)_mag，
    其中等权恒等式给 h_i=1/3、Var(κ)_mag=0.05²/3 ⇒ σ_β(d-b1,d-b2)≈1.41
    （旧口径 ≈0.82，低估 √3 倍）；color_err_cal = 0.0（同一 κ* 精确定消）。"""
    _login(client)
    out = _post(client, _body(_anchors(), diagnostics={'beta_matrix': True}))
    rows = {r['band']: r for r in out['results']}
    bm = out['diagnostics']['beta_matrix']
    me2 = 0.05 ** 2
    assert len(bm['cells']) == 3
    for c in bm['cells']:
        ri, rj = rows[c['band_i']], rows[c['band_j']]
        lg = math.log10(rj['lambda_pivot_aa'] / ri['lambda_pivot_aa'])
        # Var(κ)_mag = me²/3 仅是等权理想值：锚点星等（解析幂律式）与合成星等
        # （截断高斯通带积分）给逐波段 κ_i = g_i/f_i 一阶离散 ⇒ h_i/Var(κ) 有
        # ~1e-4 级相对偏离，断言取 5e-3（精确性由纯函数/MC 两层守）
        pred = (ri['delta_m_err'] ** 2 + rj['delta_m_err'] ** 2
                + (ri['mag_err_resp'] or 0.0) ** 2
                + (rj['mag_err_resp'] or 0.0) ** 2 + 2.0 * (me2 / 3.0))
        assert c['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(pred, rel=5e-3)
        assert c['color_err_cal'] == 0.0
        # 方向：参与对的 κ* 协方差项为正（放大 σ_β），不是旧口径的 −2Var(κ)
        assert c['sigma_beta'] ** 2 * lg ** 2 > (ri['delta_m_err'] ** 2
                                                 + rj['delta_m_err'] ** 2)
    c12 = next(c for c in bm['cells']
               if (c['band_i'], c['band_j']) == ('d-b1', 'd-b2'))
    lg12 = math.log10(rows['d-b2']['lambda_pivot_aa']
                      / rows['d-b1']['lambda_pivot_aa'])
    assert c12['sigma_beta'] == pytest.approx(math.sqrt(0.005) / lg12, rel=5e-3)


def test_t50_two_band_pair_kappa_cancels_to_noise_residual(client):
    """P1-1 API 层（评审判例）：两波段等权参与对——n=2、h=1/2 ⇒ κ 项在色中
    完全相消，σ_β²·lg² = 2·var_in + resp²（纯逐波段噪声残余，≈0.005）；
    旧口径（−2Var(κ)）在此恰给 0。"""
    _login(client)
    out = _post(client, _body(_anchors()[:2], diagnostics={'beta_matrix': True}))
    rows = {r['band']: r for r in out['results']}
    bm = out['diagnostics']['beta_matrix']
    assert len(bm['cells']) == 1
    c = bm['cells'][0]
    ri, rj = rows[c['band_i']], rows[c['band_j']]
    lg = math.log10(rj['lambda_pivot_aa'] / ri['lambda_pivot_aa'])
    var_in = 0.05 ** 2
    # κ_i 一阶离散 ⇒ h_i/Var(κ) 对等权理想值有 ~1e-4 级相对偏离（精确性由
    # 纯函数/MC 两层守），断言取 5e-3
    pred = (ri['delta_m_err'] ** 2 + rj['delta_m_err'] ** 2
            + (ri['mag_err_resp'] or 0.0) ** 2
            + (rj['mag_err_resp'] or 0.0) ** 2 + 2.0 * (var_in / 2.0))
    assert c['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(pred, rel=5e-3)
    assert c['sigma_beta'] ** 2 * lg ** 2 == pytest.approx(2.0 * var_in, rel=5e-3)


def test_t50_color_err_cal_null_with_reason_when_not_anchored(client):
    """P1-2 API 层：非 anchored（direct）下 σ_cal 无法评估 ⇒ 每格
    color_err_cal=null + note 书面原因（TXT-23），β/σ_β/color_err_stat 照出、
    不 500。"""
    _login(client)
    body = _body(_anchors(), mode='direct',
                 spectrum={'lam_aa': list(LAMS), 'flux': _powerlaw_flux(),
                           'flux_err': None,
                           'meta': {'mjd': MJD, 'category': 'other',
                                    'flux_unit': 'erg/s/cm^2/Angstrom'}},
                 diagnostics={'beta_matrix': True})
    out = _post(client, body)
    assert out['mode_effective'] == 'direct'
    bm = out['diagnostics']['beta_matrix']
    assert bm['cells'] and all(c['color_err_cal'] is None for c in bm['cells'])
    assert 'TXT-23' in bm['note'] and '精确定消' in bm['note']
    assert all(c['sigma_beta'] is not None for c in bm['cells'])


# ═══ W-32 前半（服务端可自动部分 + 前端源码级扫描） ═══════════════════════

def test_w32_frontend_switch_panel_source_scan():
    """前端半段可自动部分：U-44 前三项（P2c）可勾选（sp-diag 事件通路）、每项
    旁写明新增列与新增计算规模（W-32 前半走查文案）；results.js 只携带勾选键 +
    CA-39/CA-40 分档。后三键的 P3c 定态（M-6/U-31 徽章与 tooltip、M-6 闸门两态）
    由 test_l2_specphot_p3c.py 的 W-32 后半扫描承载。视觉层（禁用态渲染、
    tooltip 悬停）见人工清单（W-32 前半）。"""
    with open(os.path.join(FRONT_SPECPHOT, 'workbench.js'), encoding='utf-8') as f:
        wb = f.read()
    for k in P2C_KEYS:
        assert f"['{k}', 'P2c'," in wb, k
    assert "locked || computing ? 'disabled'" in wb and "E-15/A-3" in wb
    for k in P2C_KEYS:                       # 每项旁的新增列与新增计算规模
        seg = wb.split(f"['{k}', 'P2c',", 1)[1].split(']', 1)[0]
        assert '新增' in seg and '规模' in seg, k
    assert 'sp-diag' in wb and 'diagFlags' in wb
    with open(os.path.join(FRONT_SPECPHOT, 'results.js'), encoding='utf-8') as f:
        rs = f.read()
    assert 'S.diagFlags' in rs and "'CA-39': 'approx'" in rs \
        and "'CA-40': 'interp'" in rs
    assert 'renderDiagnostics' in rs         # 诊断卡渲染 diagnostics{} 内容


def test_w32_off_response_untouched_by_other_params(client):
    """W-32 前半走查的自动收口：三开全关 + 其余参数改动（weighting 能量侧）
    ⇒ diagnostics 仍为空对象 —— 开关组的关闭态与任何其他参数正交。"""
    _login(client)
    out = _post(client, _body(_anchors(), weighting='energy',
                              diagnostics={k: False for k in P2C_KEYS}))
    assert out['diagnostics'] == {}
