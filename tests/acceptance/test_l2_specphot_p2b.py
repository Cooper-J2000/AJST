"""L2: specphot P2 切片 2b —— API-3（POST /continuum）+ 宿主消光三态 + model 定标。

覆盖（02 §3.8 / §3.3 / §5.2.2 / §4.2 / F-87…F-90）：
  Q-13/Q-14/Q-15/Q-32/Q-33/Q-35  API-3 请求校验拒绝族（400/501 显式 jsonify，禁 500）
  F-87 三态        off（A_V≡0，n_par 少一）/ fit（A_V 自由 + ebv_display 只显示）/
                   prescribe（ebv 必填、A_V 钉死不进自由参数表、CA-43、n_par 少一）
  T-55             A_V↔E(B−V) 往返一致性：fit 反算 E(B−V) ⇒ prescribe 回填 ⇒
                   曲线 B 的改正因子与模型尘埃因子逐点互为倒数（相对偏差 < 1e-9）。
                   law×z 全组合 5×2=10 例**裁为 3 例**（smc 固定 R_V 线性律 × z=0.5、
                   mw P92 宽域 × z=0（无红移观测系等效分支）、mw_f99 参数化 R_V 传律
                   × z=1.2）——三种律形态（固定标称/宽域/传参）与三种 z 分支已全覆盖。
  T-56             host_ext_basis 七键全必填；rv_source 两值（lmc 3.16 vs 3.41，
                   宿主 laws.py 实测）
  T-57             派生件零写入：prescribe 全过程后 catadata/ 与 backend/ 文件集 +
                   mtime、DB 三表 COUNT/max(id) 逐项不变（与 T-47 同一快照口径）
  T-58             off/fit 带 ebv ⇒ 只回显 ebv_ignored=true，其余逐字节相同；
                   prescribe 与 fit 的 n_par 恰差 1（消光因子恰出现一次）
  §3.3 model 行    photometry mode='model'：use_model 模型曲线代替观测谱——
                   平模型（β=0，A=1 mJy）解析解 f_mjy≡A、m_AB≡C_AB_MJY_ZERO；
                   与 direct 模式对同一模型曲线谱的合成值恒等；auto 落 model 生效
不写库；上传件不落盘（T-57 快照承载）。
"""
import json
import os
import re
import sys

import numpy as np
import pytest

from app import create_app
from specphot import _result_cache, _cache_lock
from specphot.constants import C_AA_PER_S, C_AB_MJY_ZERO, C_HOST_EBV_MAX
from specphot.continuum import MODEL_SPECS, NU0_DEFAULT, av_ebv
from sedfit import laws as host_laws

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, 'backend'))   # sedfit/specphot 导入口径


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
        s['username'] = 'p2b-tester'
        s['role'] = 'user'


def _clear_cache():
    with _cache_lock:
        _result_cache.clear()


# ─── 合成谱：真值幂律 + 宿主尘埃（law/Av 已知），噪声为零 ⇒ 拟合可解析核验 ──
A_TRUE, BETA_TRUE, AV_TRUE = 1.0, 1.0, 0.8
Z_TRUE = 0.5
LAMS = [4000.0 + 67.8 * i for i in range(60)]                  # 60 px，3994–8062 Å


def _spec_body(law='smc', z=Z_TRUE, rv=None, with_err=True):
    """Fλ cgs 上传数组：Fν[mJy] = A·(ν/ν0)^-β·10^{-0.4·A_V·law(λ_rest)}。"""
    lam = np.asarray(LAMS)
    nu = C_AA_PER_S / lam
    flam = A_TRUE * (nu / NU0_DEFAULT) ** (-BETA_TRUE)
    lam_rest = lam / (1.0 + z) if z else lam
    f_dust = host_laws.extinguish(nu, flam, z or None, law, AV_TRUE, rv=rv)
    flux = f_dust * 1e-26 * C_AA_PER_S / lam ** 2              # Fλ = Fν·c/λ²
    err = (flux * (0.02 + 0.0005 * (np.arange(lam.size) % 4))).tolist()
    meta = {'z': z}
    return {'lam_aa': LAMS, 'flux': flux.tolist(),
            'flux_err': err if with_err else None, 'meta': meta}


def _cont_body(spec, **over):
    body = {'spectrum': spec, 'models': ['pl', 'pl_dust'],
            'mw': {'correct': False}}
    body.update(over)
    return body


def _post_cont(client, body):
    r = client.post('/api/specphot/continuum', json=body)
    assert r.status_code == 200, _json(r)
    return _json(r)


# ─── 1. 请求校验拒绝族（Q-13/14/15/32/33/35 + Q-23） ─────────────────────

def test_api3_validation_rejections(client):
    _login(client)
    spec = _spec_body()
    cases = [   # (body 覆盖, 期望 status, 期望 code/reason 子串)
        ({'spectrum': None}, 400, 'source_ambiguous'),                   # Q-23 双缺
        ({'spectrum': spec, 'spectrum_id': 1}, 400, 'source_ambiguous'),  # Q-23 双给
        (_cont_body(spec, models='pl'), 400, 'Q-13'),
        (_cont_body(spec, models=[]), 400, 'Q-13'),
        (_cont_body(spec, models=['powerlaw_3seg']), 400, 'powerlaw_3seg'),
        (_cont_body(spec, models=['PowerLawDust']), 400, 'Q-13'),         # 类名不是别名
        (_cont_body(spec, law='cahloo'), 400, 'Q-14'),
        (_cont_body(spec, rv_free=True), 400, 'rv_free'),
        (_cont_body(spec, host_ext_mode='auto'), 400, 'host_ext_mode'),   # Q-32 闭集
        (_cont_body(spec, host_ext_mode='prescribe'), 400, 'ebv 必填'),    # Q-32 无默认
        (_cont_body(spec, host_ext_mode='prescribe', ebv=3.0), 400, 'C_HOST_EBV_MAX'),
        (_cont_body(spec, host_ext_mode='prescribe', ebv=-0.1), 400, 'host_ext_mode'),
        (_cont_body(spec, poly_order=0), 400, 'poly_order'),              # Q-15
        (_cont_body(spec, poly_order=8), 400, 'poly_order'),
        (_cont_body(spec, n_boot=0), 400, 'n_boot'),
        (_cont_body(spec, n_boot=1001), 400, 'n_boot'),                   # C_MAX_BOOT
        (_cont_body(spec, err_seed=1.5), 400, 'err_seed'),                # Q-35
        (_cont_body(spec, sigma_policy='nope'), 400, 'sigma_policy'),
        # P2 切片 2c 起 factor=2 放行（F-108 合束算术落地，原 E-13 期次闸拆除；
        # 闭集外的 5 仍 E-14，正例归 test_l2_specphot_p2c.py）
        (_cont_body(spec, preprocess={'factor': 5}), 400, 'rebin_factor'),
        (_cont_body(spec, models=['pl'], z=-0.1), 400, 'z'),
    ]
    for over, status, needle in cases:
        body = dict(_cont_body(spec), **over) if over else _cont_body(spec)
        r = client.post('/api/specphot/continuum', json=body)
        assert r.status_code == status, (over, r.status_code, _json(r))
        d = _json(r)
        assert d['code'] in ('bad_request_state', 'feature_disabled'), d
        assert needle in json.dumps(d, ensure_ascii=False), (over, d)
        assert 'spec_phot_version' in d and isinstance(d['warnings'], list)   # A-5
    # smc 的曲线无 R_V 参数 ⇒ 任意"第三只 R_V"拒绝（F-64②，reason=rv_source）
    r = client.post('/api/specphot/continuum', json=_cont_body(spec, rv=3.3))
    assert r.status_code == 400 and _json(r).get('reason') == 'rv_source'
    # 未登录访客亦可用（2026-10-05 只读计算放开）：空体 ⇒ 400 参数校验而非 401
    with client.session_transaction() as s:
        s.clear()
    assert client.post('/api/specphot/continuum', json={}).status_code == 400
    _login(client)
    # 库内谱不存在 ⇒ 404 spectrum_not_found（E-01）
    r = client.post('/api/specphot/continuum', json={'spectrum_id': 99999,
                                                     'models': ['pl']})
    assert r.status_code == 404 and _json(r)['code'] == 'spectrum_not_found'


def test_api3_too_few_pixels_after_masks(client):
    """非正流量 + 掩膜剔除后拟合区 < 8 px ⇒ E-04 band_unusable（§3.8 窗口语义）。"""
    _login(client)
    spec = _spec_body()
    spec['flux'] = [f if i < 6 else -abs(f) for i, f in enumerate(spec['flux'])]
    r = client.post('/api/specphot/continuum', json=_cont_body(spec, models=['pl']))
    assert r.status_code == 400 and _json(r)['code'] == 'band_unusable'


# ─── 2. 三态语义 + T-55 往返一致性 + T-56/T-58 ───────────────────────────

def _fit_mode(client, law, z, rv=None):
    """fit 态拟合并回传（fit.response, pl_dust fit）。"""
    resp = _post_cont(client, _cont_body(_spec_body(law, z, rv), law=law, z=z))
    dust = next(f for f in resp['fits'] if f['model'] == 'pl_dust')
    return resp, dust


@pytest.mark.parametrize('law,z,rv', [('smc', 0.5, None), ('mw', 0.0, None),
                                      ('mw_f99', 1.2, 3.1)])
def test_t55_three_state_roundtrip_and_curve_b(client, law, z, rv):
    """T-55/F-87/F-90②：fit 拟出 A_V ⇒ 反算 E(B−V)（只显示）⇒ prescribe 回填 ⇒
    A_V 钉死值 == 拟合值（1e-9），且曲线 B 的改正因子与模型尘埃因子逐点互为
    倒数（|ratio_B·ratio_model − 1| < 1e-9：同一 law 同一静止系波长轴，恰出现
    一次、方向相反）。law×z 裁 3 例（见模块 docstring）。"""
    _login(client)
    _clear_cache()
    resp_fit, dust = _fit_mode(client, law, z, rv)
    # fit 态：A_V 自由（n_par=3），拟合值回收真值；ebv_display 只作显示（F-90）
    assert dust['n_par'] == 3 and 'Av' in dust['params']
    assert dust['params']['Av'] == pytest.approx(AV_TRUE, abs=1e-6)
    assert dust['params']['beta'] == pytest.approx(BETA_TRUE, abs=1e-6)
    assert dust['ebv_ignored'] is False
    assert dust['ebv_display'] == pytest.approx(av_ebv(av=AV_TRUE, rv=dust['host_ext_basis']['rv']),
                                                rel=1e-9)
    basis = dust['host_ext_basis']
    assert set(basis) == {'law', 'rv', 'rv_source', 'rv_source_note', 'screen_z',
                          'lam_axis', 'form'}                        # T-56 七键
    assert basis['law'] == law and basis['screen_z'] == z
    assert basis['form'] == 'A_V = R_V·E(B−V)；A_λ = A_V·[A(λ)/A(V)]'
    # prescribe 回填：A_V 钉死 == 拟合值；n_par 少一（T-58/F-87a）；CA-43 在场
    ebv_back = av_ebv(av=dust['params']['Av'], rv=basis['rv'])
    resp_pr = _post_cont(client, _cont_body(_spec_body(law, z, rv), law=law, z=z,
                                            models=['pl_dust'],
                                            host_ext_mode='prescribe', ebv=ebv_back))
    pr = next(f for f in resp_pr['fits'] if f['model'] == 'pl_dust')
    assert resp_pr['av_prescribed'] == pytest.approx(dust['params']['Av'],
                                                     rel=1e-9, abs=1e-12)
    assert resp_pr['ebv_used'] == pytest.approx(ebv_back, rel=1e-12)
    # A_V 退出自由参数表（F-87e）：n_par 少一、误差推断表无 Av；params 里作为
    # 固定值回显（钉死值原样在场，T-58 的"消光因子恰出现一次"）
    assert pr['n_par'] == dust['n_par'] - 1
    assert pr['params']['Av'] == pytest.approx(resp_pr['av_prescribed'], rel=1e-12)
    assert 'Av' not in pr['err_low'] and 'Av' not in pr['sigma_theta']
    assert pr['ebv_display'] is None and pr['ebv_display_err'] is None   # §3.9.3.1 + CA-43
    assert any(w['code'] == 'CA-43' for w in resp_pr['warnings'])
    # 曲线 B 纯派生件（F-89/T-57）：depth=1、不导出、逐点改正因子与模型尘埃因子互倒
    dr = resp_pr['de_reddened']
    assert dr['derivation_depth'] == 1 and dr['exported'] is False
    assert dr['n_points'] == len(LAMS) and dr['txt22']
    lam_obs = np.asarray(dr['lam_obs_vac_aa'])
    assert dr['curve_hash'] and re.fullmatch(r'[0-9a-f]{12}', dr['curve_hash'])
    ratio_b = np.asarray(dr['flux_after']) / np.asarray(dr['flux_before'])
    nu = C_AA_PER_S / lam_obs
    cfg = {'nu0': NU0_DEFAULT, 'z': z, 'law': law, 'rv': pr['host_ext_basis']['rv']}
    th = dict(pr['params'])
    f_dust = MODEL_SPECS['pl_dust']['fnu'](th, nu, cfg)               # 含尘埃模型（mJy）
    th_nodust = {**th, 'Av': 0.0}
    f_nodust = MODEL_SPECS['pl']['fnu'](th_nodust, nu, cfg)           # 同参数去尘埃
    ratio_model = f_dust / f_nodust                                   # = 10^{-0.4·A_V·law}
    assert np.max(np.abs(ratio_b * ratio_model - 1.0)) < 1e-9         # T-55 逐点 <1e-9


def test_t56_rv_source_both_values(client):
    """T-56③：同一律（lmc）标称/内禀两基准必须可辨且取宿主 laws.py 实测值
    （3.16 vs 3.41，F-88③）；rv_source_note 写明基准出处。"""
    _login(client)
    _clear_cache()
    assert host_laws._NOMINAL_RV['lmc'] == 3.16 and host_laws._INTRINSIC_RV['lmc'] == 3.41
    r_nom = _post_cont(client, _cont_body(_spec_body('lmc'), law='lmc',
                                          models=['pl'], rv_source='nominal'))
    r_int = _post_cont(client, _cont_body(_spec_body('lmc'), law='lmc',
                                          models=['pl'], rv_source='intrinsic'))
    b_nom = r_nom['fits'][0]['host_ext_basis']
    b_int = r_int['fits'][0]['host_ext_basis']
    assert (r_nom['rv'], b_nom['rv_source']) == (3.16, 'nominal')
    assert (r_int['rv'], b_int['rv_source']) == (3.41, 'intrinsic')
    assert '_NOMINAL_RV' in b_nom['rv_source_note'] \
        and '_INTRINSIC_RV' in b_int['rv_source_note']
    assert 'intrinsic' in json.dumps(b_int, ensure_ascii=False)
    # 重复别名去重（实现裁量）：7 个重复 'pl' ⇒ 单模型请求而非拒
    d = _post_cont(client, _cont_body(_spec_body(), models=['pl'] * 7))
    assert d['models_requested'] == ['pl']


def test_t58_off_fit_ebv_ignored_byte_identical(client):
    """T-58③/Q-32：off（与 fit）携带 ebv ⇒ 忽略并回显 ebv_ignored=true，
    其余所有键与不带 ebv 的响应逐字节相同（缓存清空后两次独立计算）。"""
    _login(client)
    spec = _spec_body()
    for mode in ('off', 'fit'):
        _clear_cache()
        kw = dict(models=['pl'], host_ext_mode=mode)
        r_with = _post_cont(client, _cont_body(spec, ebv=0.5, **kw))
        r_wo = _post_cont(client, _cont_body(spec, **kw))
        assert r_with['ebv_ignored'] is True and r_wo['ebv_ignored'] is False
        assert r_with['ebv_used'] is None and r_wo['ebv_used'] is None

        def _strip(d):
            d = json.loads(json.dumps(d))
            d.pop('ebv_ignored')
            for f in d.get('fits', []):
                f.pop('ebv_ignored', None)
            return d
        assert json.dumps(_strip(r_with), sort_keys=True) \
            == json.dumps(_strip(r_wo), sort_keys=True), mode


def test_f87_off_state_dust_free(client):
    """F-87①：off 态 pl_dust 的 A_V≡0 钉死分支 —— 不进自由参数表、恒等价于 pl，
    曲线 B 不出现（depth=0）；CA-43(off) 书面原因在场。"""
    _login(client)
    _clear_cache()
    resp = _post_cont(client, _cont_body(_spec_body(), host_ext_mode='off'))
    off = next(f for f in resp['fits'] if f['model'] == 'pl_dust')
    pl = next(f for f in resp['fits'] if f['model'] == 'pl')
    assert off['n_par'] == pl['n_par'] == 2
    assert off['params']['Av'] == 0.0 and off['host_ext_basis'] is None
    assert resp['de_reddened']['derivation_depth'] == 0
    assert resp['de_reddened']['n_points'] == 0
    assert any(w['code'] == 'CA-43' and w.get('reason') == 'host_off'
               for w in resp['warnings'])


# ─── 3. T-57：派生件零写入（与 T-47 同一快照口径） ──────────────────────────

def _dsn():
    for var in ('AJST_TEST_DATABASE_URL', 'DATABASE_URL'):
        v = os.environ.get(var)
        if v:
            return re.sub(r'^postgresql\+\w+://', 'postgresql://', v)
    from config import DB_URL
    return re.sub(r'^postgresql\+\w+://', 'postgresql://', DB_URL)


def _db_snapshot():
    import psycopg2
    cn = psycopg2.connect(_dsn(), connect_timeout=5)
    try:
        cn.set_session(readonly=True, autocommit=False)
        with cn.cursor() as c:
            c.execute("SET statement_timeout = '10s'")
            out = {}
            for t in ('spectra', 'filters', 'lightcurves'):
                c.execute(f'SELECT COUNT(*), COALESCE(MAX(id)::text, \'\') FROM {t}')
                out[t] = tuple(c.fetchone())
        cn.rollback()
        return out
    finally:
        cn.close()


def _fs_snapshot(root):
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            snap[os.path.relpath(p, root)] = os.stat(p).st_mtime_ns
    return snap


def test_t57_derived_curve_zero_write(client):
    """T-57（F-89③）：prescribe + 曲线 B 全过程后，catadata/ 与 backend/ 文件集
    与 mtime、DB 三表行数与 max(id) 逐项不变；响应不含谱数组、无子谱文件。"""
    _login(client)
    cat = os.path.join(_REPO_ROOT, 'catadata')
    bak = os.path.join(_REPO_ROOT, 'backend')
    fs0 = (_fs_snapshot(cat), _fs_snapshot(bak))
    db0 = _db_snapshot()
    spec = _spec_body()
    resp = _post_cont(client, _cont_body(spec, host_ext_mode='prescribe', ebv=0.5))
    assert resp['de_reddened']['exported'] is False
    assert 'lam_aa' not in json.dumps(resp)          # 响应不携带谱数组
    assert (_fs_snapshot(cat), _fs_snapshot(bak)) == fs0, '文件侧发生变化'
    assert _db_snapshot() == db0, 'DB 行数或 max(id) 发生变化'


# ─── 4. §3.3 model 行：photometry 的 model 定标真实现 ─────────────────────

CC = {'band': 'test-custom-r', 'lam_aa': [3990.0, 4045.0, 4100.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'unknown'}
LAMS_M = [3900.0 + 10.0 * i for i in range(100)]


def _model_spectrum():
    """观测谱 = 模型曲线本身的采样（β=0 平 Fν，A=1 mJy）：Fλ = 1e-26·c/λ²。"""
    lam = np.asarray(LAMS_M)
    flux = 1.0e-26 * C_AA_PER_S / lam ** 2
    return {'lam_aa': LAMS_M, 'flux': flux.tolist(),
            'flux_err': (flux * 0.02).tolist(), 'meta': {}}


def _um_body(client, mode, meta_extra=None, **over):
    # 先跑一次 S2（同一谱数组 ⇒ 同 spec_hash/mask_hash/preprocess_hash，Q-9/Q-34）
    spec = _model_spectrum()
    cont = _post_cont(client, _cont_body(spec, models=['pl']))
    um = {'mask_hash': cont['mask_hash'],
          'preprocess_hash': cont['preprocess']['preprocess_hash'],
          'frame': 'obs', 'flux_transform_applied': 'none', 'comparable': True,
          'model': 'pl', 'params': {'A': 1.0, 'beta': 0.0},
          'nu0': NU0_DEFAULT, 'law': 'smc', 'rv': 3.1, 'z': None}
    body = {'spectrum': spec, 'bands': ['test-custom-r'], 'custom_curves': [CC],
            'mode': mode, 'use_model': um, 'weighting': 'photon',
            'mw': {'correct': False}}
    body.update(over)
    return body


def test_model_calibration_analytic_flat_model(client):
    """§3.3 model 行：模型谱代替观测谱。β=0 ⇒ Fν 恒 A=1 mJy，解析解
    f_mjy≡1、m_AB≡C_AB_MJY_ZERO；与 direct 对同一曲线谱的合成值恒等；
    σ 族按 null 出（S2 参数误差传播不在本期，TXT-23/CA-15）。"""
    _login(client)
    _clear_cache()
    d = _json(client.post('/api/specphot/photometry', json=_um_body(client, 'model')))
    assert d['mode_requested'] == 'model' and d['mode_effective'] == 'model'
    row = d['results'][0]
    assert row['band'] == 'test-custom-r' and row['extrapolated'] is False
    assert row['f_mjy'] == pytest.approx(1.0, rel=1e-9)          # 平 Fν 解析解
    assert row['mag'] == pytest.approx(C_AB_MJY_ZERO, abs=1e-9)  # 16.4 − 2.5log10(1)
    assert row['mag_err_stat'] is None and row['f_err_mjy'] is None
    assert d['results'][0]['err_source']['mag_err_stat'] == 'none'
    assert any(w['code'] == 'CA-15' and w.get('reason') == 'model_mode_no_pixel_sigma'
               for w in row['warnings'])
    # 与 direct 对同一模型曲线谱恒等（模型谱 = 观测谱的采样 ⇒ 同一积分）
    spec = _model_spectrum()
    spec['meta'] = {'flux_unit': 'erg/s/cm^2/Angstrom'}
    direct_body = {'spectrum': spec, 'bands': ['test-custom-r'],
                   'custom_curves': [CC], 'mode': 'direct',
                   'mw': {'correct': False}}
    d2 = _json(client.post('/api/specphot/photometry', json=direct_body))
    assert d2['mode_effective'] == 'direct'
    assert d2['results'][0]['mag'] == pytest.approx(row['mag'], abs=1e-9)
    assert d2['results'][0]['f_mjy'] == pytest.approx(row['f_mjy'], rel=1e-9)


def test_model_calibration_auto_falls_through_to_model(client):
    """§3.3 auto 行：direct（非绝对）→ anchored（无锚点）→ model（use_model 在场）
    依次降落，mode_effective='model' 且结果与显式 model 一致。"""
    _login(client)
    _clear_cache()
    d = _json(client.post('/api/specphot/photometry', json=_um_body(client, 'auto')))
    assert d['mode_requested'] == 'auto' and d['mode_effective'] == 'model'
    assert d['results'][0]['f_mjy'] == pytest.approx(1.0, rel=1e-9)


def test_model_calibration_gates(client):
    """F-91⑦/Q-9 的服务端半段：mode='model' 无 use_model ⇒ 501 phase='P2'；
    use_model.comparable=false ⇒ E-14 fit_not_comparable；mask_hash 不一致 ⇒
    E-14 mask_mismatch；model 不在注册表别名集 ⇒ E-14。"""
    _login(client)
    _clear_cache()
    spec = _model_spectrum()
    # 无 use_model 的 model 路径保持 501（phase='P2'）
    r = client.post('/api/specphot/photometry',
                    json={'spectrum': spec, 'bands': ['test-custom-r'],
                          'custom_curves': [CC], 'mode': 'model'})
    d = _json(r)
    assert r.status_code == 501 and d['code'] == 'feature_disabled' and d['phase'] == 'P2'
    # comparable=false（S2 侧 CA-44）不得被消费
    body = _um_body(client, 'model')
    body['use_model']['comparable'] = False
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 400 and _json(r).get('reason') == 'fit_not_comparable'
    # mask_hash 与本次掩膜不一致
    body = _um_body(client, 'model')
    body['use_model']['mask_hash'] = '0' * 12
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 400 and _json(r).get('reason') == 'mask_mismatch'
    # poly 不可被 S1 消费（Chebyshev 基依赖拟合窗口定义）
    body = _um_body(client, 'model')
    body['use_model']['model'] = 'poly'
    body['use_model']['params'] = {'c0': 1.0}
    r = client.post('/api/specphot/photometry', json=body)
    assert r.status_code == 400 and _json(r).get('reason') == 'model_unsupported'
