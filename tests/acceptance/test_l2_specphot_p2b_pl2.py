"""L2: specphot P2b —— API-3 的 pl2 词表 + Davies 参数化自助装配 + W-24 回显。

覆盖条款：
  Q-13     模型词表含 pl2（别名，非宿主类名 powerlaw_2seg——用类名 ⇒ 拒）；
           powerlaw_3seg 维持拒绝（§3.8.1：不纳入 S2 模型集）
  F-29③   请求含 pl+pl2 ⇒ verdict.davies[] 就地评参数化自助（p_method=
           'parametric_bootstrap'、p_chi2mix 旁证、dk=3），该对不出 F 检验
           字段；verdict.p_method 同步回显
  W-24     定义域可见：谱延伸到 1000 Å（静止系）以下 ⇒ pl2 拟合卡回显
           law_clipped_lam_a 且顶层挂 CA-23（reason=law_clipped）；
           grid_boundary_params 回显键在场（网格端点「边界值」标注件）
  Q-32/F-87  pl2 的三态：off ⇒ A_V≡0 钉死（n_par 少一）；prescribe ⇒
           A_V=R_V·E(B−V) 钉死 + CA-43；fit ⇒ ebv_display = A_V/R_V 只显示
  CA-38    pl2 断点 λ_rest∈[2000,2300] Å（实现约定闭区间）且谱覆盖内无
           λ_obs<2400 Å 的点（近紫外阈为实现约定）⇒ 顶层 + pl2 模型卡同挂
           CA-38（reason=nuv_gap，保守文案）；覆盖含近紫外点 ⇒ 不挂

上传件不落盘；不写库。
"""
import json
import os
import sys

import numpy as np
import pytest

from app import create_app
from specphot import _result_cache, _cache_lock
from specphot.constants import C_AA_PER_S, C_FTEST_ALPHA
from specphot.continuum import NU0_DEFAULT, MODEL_SPECS

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))

SEED = 20260927
LAM = [4000.0 + 33.9 * i for i in range(120)]    # 120 px，4000–8000 Å
NUB_TRUE = C_AA_PER_S / 5500.0


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
        s['username'] = 'p2b-pl2-tester'
        s['role'] = 'user'


def _clear_cache():
    with _cache_lock:
        _result_cache.clear()


def _broken_spec_body(dbeta=2.0, noise=0.005, av=0.0, lam=None, z=0.0, nub=None):
    """上传件：真值断折幂律（Fλ cgs，A=1 mJy 归一 ν0），宿主尘埃可选。"""
    lam = np.asarray(lam if lam is not None else LAM)
    nu = C_AA_PER_S / lam
    th = {'A': 1.0, 'beta1': 0.5, 'dbeta': dbeta,
          'nu_b': nub if nub is not None else NUB_TRUE, 'Av': av}
    flam = MODEL_SPECS['pl2']['fnu'](th, nu, {'nu0': NU0_DEFAULT, 'z': z or None,
                                              'law': 'smc', 'rv': 2.74}) \
        * 1e-26 * C_AA_PER_S / lam ** 2
    rng = np.random.default_rng(SEED)
    flux = flam * (1.0 + rng.standard_normal(lam.size) * noise)
    return {'lam_aa': [float(x) for x in lam], 'flux': [float(x) for x in flux],
            'flux_err': [float(x) for x in flam * noise],
            'meta': {'z': z}}


def _cont_body(spec, **over):
    body = {'spectrum': spec, 'models': ['pl', 'pl2'], 'mw': {'correct': False}}
    body.update(over)
    return body


def _post_cont(client, body):
    _clear_cache()
    r = client.post('/api/specphot/continuum', json=body)
    assert r.status_code == 200, _json(r)
    return _json(r)


# ─── 1. Q-13：模型词表 ────────────────────────────────────────────────────

def test_q13_pl2_vocabulary(client):
    _login(client)
    # pl2 以别名入词表：正常请求可用（词表断言与点数无关 ⇒ 缩谱避让 ST-5 5s
    # 墙钟在外部 CPU 争用下的偶发越限；登记于 05 遗留清单）
    lam80 = [4000.0 + 50.0 * i for i in range(80)]
    d = _post_cont(client, _cont_body(_broken_spec_body(dbeta=1.5, lam=lam80)))
    assert {f['model'] for f in d['fits']} == {'pl', 'pl2'}
    assert d['models_requested'] == ['pl', 'pl2']
    # 宿主注册表键（类名口径）不是别名 ⇒ 拒（Q-13：键为别名不是类名）
    _clear_cache()
    r = client.post('/api/specphot/continuum', json=_cont_body(
        _broken_spec_body(), models=['pl', 'powerlaw_2seg']))
    assert r.status_code == 400
    e = _json(r)
    assert e['code'] == 'bad_request_state'
    assert 'Q-13' in json.dumps(e, ensure_ascii=False)
    assert '键为别名不是类名' in json.dumps(e, ensure_ascii=False)
    # powerlaw_3seg 维持拒绝（§3.8.1）
    _clear_cache()
    r = client.post('/api/specphot/continuum', json=_cont_body(
        _broken_spec_body(), models=['pl', 'powerlaw_3seg']))
    assert r.status_code == 400
    assert 'powerlaw_3seg' in json.dumps(_json(r), ensure_ascii=False)


# ─── 2. F-29③：verdict.davies[] 装配（参数化自助就位） ────────────────────

def test_api_davies_bootstrap_block(client):
    _login(client)
    d = _post_cont(client, _cont_body(_broken_spec_body(dbeta=2.0), n_boot=40))
    v = d['verdict']
    assert v['p_method'] == 'parametric_bootstrap'      # F-29③ 原文回显
    assert len(v['davies']) == 1
    e = v['davies'][0]
    assert e['pair'] == ['pl', 'pl2']
    assert e['p_method'] == 'parametric_bootstrap'
    assert e['stat_name'] == 'delta_chi2' and e['stat_obs'] >= 0.0
    assert e['dk'] == 3 and e['n_boot'] == 40
    assert e['p'] < C_FTEST_ALPHA and e['verdict'] == 'rich'
    assert 0.0 <= e['p_chi2mix'] <= 1.0
    assert '不得作为判定依据' in e['note']
    assert isinstance(e['err_seed'], int) and e['rng_algo'] == 'pcg64'
    # 该对不出 F 检验字段（F-29③：常规 F 高估显著性，不得作为判定出现）
    assert not any(set(p) == {'pl', 'pl2'} for p in
                   (x['pair'] for x in v['ftest']))
    # 请求只含 pl（无 pl2）⇒ 无 davies 块、p_method 维持 f_test
    _clear_cache()
    r = client.post('/api/specphot/continuum', json={
        'spectrum': _broken_spec_body(), 'models': ['pl'], 'mw': {'correct': False}})
    v2 = _json(r)['verdict']
    assert v2['p_method'] == 'f_test' and v2['davies'] == []


# ─── 3. W-24：定义域可见（law_clipped_lam_a + 边界值回显件） ───────────────

def test_w24_law_clipped_pl2(client):
    _login(client)
    lam = [1500.0 + 54.2 * i for i in range(120)]       # 1500–8000 Å，z=2 ⇒ 静止系
    d = _post_cont(client, _cont_body(
        _broken_spec_body(dbeta=1.5, lam=lam, z=2.0), law='smc'))
    assert any(w['code'] == 'CA-23' and w.get('reason') == 'law_clipped'
               for w in d['warnings'])
    f2 = next(f for f in d['fits'] if f['model'] == 'pl2')
    assert f2['law_clipped_lam_a']                     # 静止系 λ<1000 Å 的回显
    assert min(f2['law_clipped_lam_a']) < 1000.0
    # W-24「网格最优解落在端点时报告值旁标边界值」的服务端回显件
    assert 'grid_boundary_params' in f2 and 'grid_converged' in f2


# ─── 4. Q-32/F-87：pl2 的宿主消光三态 ─────────────────────────────────────

def test_pl2_host_ext_three_states(client):
    _login(client)
    # off：A_V≡0 钉死 ⇒ n_par 少一（自由态 5 → 4）
    d_fit = _post_cont(client, {'spectrum': _broken_spec_body(dbeta=1.5),
                                'models': ['pl2'], 'mw': {'correct': False}})
    f_fit = d_fit['fits'][0]
    assert f_fit['model'] == 'pl2' and f_fit['n_par'] == 5
    assert 'Av' in f_fit['params'] and 'Av' in f_fit['err_low']
    d_off = _post_cont(client, {'spectrum': _broken_spec_body(dbeta=1.5),
                                'models': ['pl2'], 'host_ext_mode': 'off',
                                'mw': {'correct': False}})
    f_off = d_off['fits'][0]
    assert f_off['n_par'] == 5 - 1 and f_off['params']['Av'] == 0.0
    assert 'Av' not in f_off['err_low']
    # prescribe：A_V = R_V·E(B−V) 钉死 + CA-43；ebv_display 按 null 出（CA-43）
    d_pre = _post_cont(client, {'spectrum': _broken_spec_body(dbeta=1.5, av=1.37),
                                'models': ['pl2'], 'host_ext_mode': 'prescribe',
                                'ebv': 0.5, 'mw': {'correct': False}})
    f_pre = d_pre['fits'][0]
    assert f_pre['n_par'] == 4
    assert f_pre['params']['Av'] == pytest.approx(d_pre['rv'] * 0.5, rel=1e-12)
    assert f_pre['ebv_display'] is None and f_pre['ebv_display_err'] is None
    assert any(w['code'] == 'CA-43' for w in d_pre['warnings'])
    assert d_pre['av_prescribed'] == pytest.approx(d_pre['rv'] * 0.5, rel=1e-12)
    # fit：ebv_display = A_V/R_V 只显示（F-88/F-90②；pl2 也是含尘埃模型）
    assert f_fit.get('ebv_display') is not None
    assert f_fit['ebv_display'] == pytest.approx(
        f_fit['params']['Av'] / d_fit['rv'], rel=1e-9)


# ─── 5. CA-38（P2b 评审落地）：断点落在 2000–2300 Å（静止系）且无近紫外点 ───

def test_ca38_break_near_2175_without_nuv(client):
    """触发例：z=1、断点真值 λ_rest=2175 Å（λ_obs=4350 Å 在覆盖内）、谱覆盖
    4000–8000 Å（无 λ_obs<2400 Å 的点——近紫外阈 2400 Å 为实现约定）⇒ 该
    "断折"与 2175 Å 凸起不可区分 ⇒ 顶层 CA-38（reason=nuv_gap）且 pl2 模型卡
    同挂同一告警（保守 verdict 文案随模型走，不得据断折单独下物理结论）。"""
    _login(client)
    d = _post_cont(client, _cont_body(
        _broken_spec_body(dbeta=1.5, z=1.0, nub=C_AA_PER_S / 4350.0),
        z=1.0, n_boot=40))
    w = next(x for x in d['warnings'] if x['code'] == 'CA-38')
    assert w.get('reason') == 'nuv_gap'
    assert '2175' in w['message'] and '保守' in w['message']
    f2 = next(f for f in d['fits'] if f['model'] == 'pl2')
    assert any(x['code'] == 'CA-38' for x in f2['warnings'])
    lam_rest_b = (C_AA_PER_S / f2['params']['nu_b']) / (1.0 + 1.0)
    assert 2000.0 <= lam_rest_b <= 2300.0        # 触发域（实现约定闭区间）


def test_ca38_not_triggered_with_nuv_coverage(client):
    """不触发例：同一静止系断点（λ_rest≈2175 Å）但谱覆盖含近紫外点
    （λ_obs 最小 2200 < 2400 Å 实现约定）⇒ 断折与 2175 Å 凸起可分辨 ⇒
    不挂 CA-38（顶层与模型卡都无）。"""
    _login(client)
    lam = [2200.0 + 48.3 * i for i in range(120)]          # 2200–8000 Å
    d = _post_cont(client, _cont_body(
        _broken_spec_body(dbeta=1.5, z=1.0, nub=C_AA_PER_S / 4350.0, lam=lam),
        z=1.0, n_boot=40))
    assert not any(x['code'] == 'CA-38' for x in d['warnings'])
    assert not any(x['code'] == 'CA-38'
                   for f in d['fits'] for x in f.get('warnings', []))
