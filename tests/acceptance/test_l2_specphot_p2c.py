"""L2: specphot **P2 切片 2c 收口验收**（02b §3.14 / §9 P2 行：合束 + 平滑 + 化约序）。

覆盖（HTTP 半段；纯函数半段归 test_l1_specphot_preprocess_p2.py，golden 恒等归
test_l1_specphot_p1b_baseline.py）：
  F-108   factor>1 端到端：preprocess{} 合束四键真值回显、面积守恒的下游效果
          （被掩尖峰不进块值 ⇒ mag 逐字节不变）、preprocess_hash 随 factor 变
          （T-75③）、CA-06/CA-47③ 常驻、results.sigma_method='second_diff_degraded'。
  T-76⑤  CA-49③（12<n<30 ⇒ rho_lag1 不可估 ⇒ rebin_gain=null，且 ① 不并现）与
          ④（负 ρ 构造例 gain > 1.1·√k）的端到端触发；CA-49① 由门函数单测承载。
  F-108⑦ 化约序：factor>1 且 errcol_verdict≠ok ⇒ sigma_method 被
          second_diff_degraded 覆盖（降级态覆盖回落值），CA-06 与 CA-47①/② 并现。
  F-109/T-77  开/关平滑响应逐字节同（JSON 导出原文口径，T-77②③；smooth_px
          不进任何哈希 ⇒ 同缓存键，T-75③ 的"只差 U-51 必命中同一项"）。
  F-112   S2 四键：API-3 含 poly ⇒ 真值；无 poly / API-2 ⇒ null/[]（S1 无
          F-62 基线，该通路不适用）。
纪律：全链只读（上传件不落盘）；语料全部显式构造（无随机判据依赖 DB）。
"""
import json
import math

import pytest

from app import create_app

F0 = 1e-14


def _lam(n, lo=4000.0, step=2.5):
    return [lo + step * i for i in range(n)]


def _curve(hi=4110.0, lo=3990.0):
    return {'band': 'p2c-r', 'lam_aa': [lo, (lo + hi) / 2.0, hi],
            't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'}


CC = _curve()
BAND = 'p2c-r'
ANCHOR = [{'band': BAND, 'mag': 18.0, 'mag_system': 'AB', 'mag_err': 0.05}]


def _flux_sin(n):
    return [F0 * (1.0 + 0.3 * math.sin(i * 1.7)) for i in range(n)]


def _body(lams=None, flux=None, flux_err=None, anchor=True, **over):
    lams = _lam(40) if lams is None else lams
    n = len(lams)
    body = {'spectrum': {'lam_aa': list(lams),
                         'flux': list(flux if flux is not None else _flux_sin(n)),
                         'flux_err': flux_err, 'meta': {}},
            'bands': [BAND], 'custom_curves': [dict(CC)],
            'weighting': 'photon'}
    if anchor:
        body['anchor_rows'] = [dict(ANCHOR[0])]   # auto→anchored 回落需 ≥1 锚点
    body.update(over)
    return body


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(scope='module', autouse=True)
def _login(client):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'p2c-tester'
        s['role'] = 'user'


def _post(client, path, body):
    r = client.post(path, json=body)
    return r.status_code, json.loads(r.get_data(as_text=True))


# ═══ F-108：合束端到端 ═══════════════════════════════════════════════════

def test_f108_factor2_keys_warnings_and_sigma_method(client):
    code, d = _post(client, '/api/specphot/photometry',
                    _body(preprocess={'factor': 2}))
    assert code == 200, d
    pp = d['preprocess']
    assert pp['factor'] == 2
    assert pp['n_rebinned_pixels'] == 20                      # ⌊40/2⌋
    assert pp['dlam_after_aa'] == pytest.approx(5.0)          # 2×2.5（完整块）
    assert isinstance(pp['rebin_gain'], float) or pp['rebin_gain'] is None
    assert pp['px_per_fwhm_after'] is None                    # r_source='none'
    assert pp['identity'] is False
    assert pp['smooth_px'] == 0
    codes = {(w['code'], w.get('reason')) for w in d['warnings']}
    assert ('CA-06', None) in codes                           # F-108⑥
    assert ('CA-47', 'rebin_r_source_none') in codes          # CA-47③
    assert d['results'][0]['sigma_method'] == 'second_diff_degraded'
    # TXT-27 可复算面：n=40 与 k=2、n_rebinned_pixels=20 均已回显 ⇒ 丢弃 0 个
    assert d['read_stats']['n_points'] - pp['factor'] * pp['n_rebinned_pixels'] == 0


def test_f108_tail_block_discard_echo(client):
    """n=41、k=4 ⇒ 10 个完整块，丢弃 1 个像元（TXT-27 摘要行可复算）。"""
    lams = _lam(41)
    code, d = _post(client, '/api/specphot/photometry',
                    _body(lams, preprocess={'factor': 4}))
    assert code == 200, d
    pp = d['preprocess']
    assert pp['n_rebinned_pixels'] == 10
    assert d['read_stats']['n_points'] - pp['factor'] * pp['n_rebinned_pixels'] == 1


def test_t75_3_preprocess_hash_changes_with_factor_not_with_smooth(client):
    """同谱只差 factor ⇒ preprocess_hash 必不同；只差 smooth ⇒ 同缓存项
    （响应逐字节同，见下一条测试）。"""
    _c1, d1 = _post(client, '/api/specphot/photometry', _body())
    _c2, d2 = _post(client, '/api/specphot/photometry',
                    _body(preprocess={'factor': 2}))
    assert d1['preprocess']['preprocess_hash'] != d2['preprocess']['preprocess_hash']
    assert d1['preprocess']['rebin_gain'] == 1
    assert d2['preprocess']['rebin_gain'] != 1


def test_t76_1_masked_spike_block_value_byte_identical(client):
    """被掩像素不参与块均值（端到端）：带 +100 倍尖峰并掩掉尖峰所在像素的谱，
    合束后块值与"无尖峰同掩膜"的谱逐字节同 ⇒ mag 逐字节同（T-76①④）。"""
    base = _flux_sin(40)
    spiked = list(base)
    spiked[10] = base[10] + 100.0 * F0                        # 第 6 块（idx 10,11）
    seg = [4000.0 + 2.5 * 10 - 0.5, 4000.0 + 2.5 * 10 + 0.5]  # 恰罩住 idx=10
    _ca, da = _post(client, '/api/specphot/photometry',
                    _body(flux=spiked, mask=[seg], preprocess={'factor': 2}))
    _cb, db = _post(client, '/api/specphot/photometry',
                    _body(flux=base, mask=[seg], preprocess={'factor': 2}))
    assert _ca == _cb == 200, (da, db)
    assert da['results'][0]['mag'] == db['results'][0]['mag']
    # 反证：尖峰未掩时块值必然被污染
    _cc, dc = _post(client, '/api/specphot/photometry',
                    _body(flux=spiked, preprocess={'factor': 2}))
    assert dc['results'][0]['mag'] != da['results'][0]['mag']


# ═══ T-76⑤：CA-49 ③④ 端到端 ═══════════════════════════════════════════

def test_t76_5_ca49_3_no_rho_gain_null_and_no_branch1(client):
    """n=24（<30 ⇒ rho_lag1 不可估挂 CA-21）⇒ rebin_gain=null 命中 ③，
    ① 不得同时报（T-76⑤）。"""
    lams = _lam(24, step=5.0)
    body = _body(lams, preprocess={'factor': 2})
    body['custom_curves'] = [dict(_curve(hi=4130.0))]
    code, d = _post(client, '/api/specphot/photometry', body)
    assert code == 200, d
    pp = d['preprocess']
    assert pp['rebin_gain'] is None and pp['n_rebinned_pixels'] == 12
    ca49 = [w for w in d['warnings'] if w['code'] == 'CA-49']
    assert len(ca49) == 1 and ca49[0]['reason'] == 'rebin_gain_no_rho'
    assert any(w['code'] == 'CA-21' for w in d['warnings'])


def test_t76_5_ca49_4_negative_rho(client):
    """负 ρ 构造例（相位交替 + 微小不规则项 ⇒ rho_lag1≈−0.999 回显）：
    k=8 ⇒ rebin_gain 远超 1.1·√8 ⇒ ④ 触发；① 不并现（gain ≫ 1.15）。"""
    n = 80
    flux = [F0 * (1.0 + 0.3 * (1 if i % 2 else -1) + 0.01 * math.sin(0.7 * i))
            for i in range(n)]
    lams = _lam(n, step=3.0)
    cc = _curve(hi=4250.0)
    body = _body(lams, flux=flux, preprocess={'factor': 8})
    body['custom_curves'] = [dict(cc)]
    code, d = _post(client, '/api/specphot/photometry', body)
    assert code == 200, d
    pp = d['preprocess']
    assert pp['n_rebinned_pixels'] == 10
    ca49 = [w for w in d['warnings'] if w['code'] == 'CA-49']
    assert [w['reason'] for w in ca49] == ['rebin_gain_over_independent']
    assert any('负 ρ' in w['message'] for w in ca49)
    gain = pp['rebin_gain']
    assert gain is not None and gain > 1.1 * math.sqrt(8)


# ═══ F-108⑦：化约序（degraded 覆盖回落值，CA-06 与 CA-47①② 并现） ═══════

def test_f108_7_reduction_order_degraded_covers_fallback(client):
    """σ 全 0 列 ⇒ errcol_verdict='all_zero'；factor=2 ⇒ final sigma_method 被
    second_diff_degraded 覆盖，CA-06 与 CA-47 档位并现（真值唯一）。"""
    err0 = [0.0] * 40
    for choice, branch in (('as_provided', 'errcol_1'), (None, 'errcol_2')):
        pp = {'factor': 2}
        if choice:
            pp['errcol_choice'] = choice
        code, d = _post(client, '/api/specphot/photometry',
                        _body(flux_err=err0, preprocess=pp))
        assert code == 200, d
        assert d['preprocess']['errcol_verdict'] == 'all_zero'
        assert d['results'][0]['sigma_method'] == 'second_diff_degraded'
        codes = {(w['code'], w.get('reason')) for w in d['warnings']}
        assert ('CA-06', None) in codes
        assert ('CA-47', branch) in codes                    # ① 或 ② 与 CA-06 并现
    # 单独 verdict≠ok（factor=1）时无 degraded（覆盖只发生在 factor>1）
    code, d = _post(client, '/api/specphot/photometry', _body(flux_err=err0))
    assert code == 200
    assert d['results'][0]['sigma_method'] in ('second_diff', 'mad_window')
    assert d['results'][0]['sigma_method'] != 'second_diff_degraded'


# ═══ F-109 / T-77：开/关平滑响应逐字节同 + 同缓存项 ══════════════════════

def test_t77_smooth_toggle_byte_identical_response(client):
    body_off = _body()
    body_on = _body(preprocess={'smooth': 3})
    r_off = client.post('/api/specphot/photometry', json=body_off)
    r_on = client.post('/api/specphot/photometry', json=body_on)
    assert r_off.status_code == r_on.status_code == 200
    # T-77②③：JSON（导出原文口径）逐字节同 —— smooth_px 不进任何哈希与键值
    assert r_off.get_data(as_text=True) == r_on.get_data(as_text=True)
    d = json.loads(r_on.get_data(as_text=True))
    assert d['preprocess']['smooth_px'] == 0 and d['preprocess']['identity'] is True
    # Q-33：越界 E-14
    code, d = _post(client, '/api/specphot/photometry', _body(preprocess={'smooth': 8}))
    assert code == 400 and d['reason'] == 'bad_enum'


# ═══ F-112：S2 四键的通路适用性 ══════════════════════════════════════════

def _cont_body(flux=None, models=('poly',), **over):
    n = len(flux) if flux else 40
    body = {'spectrum': {'lam_aa': _lam(n), 'flux': flux or _flux_sin(n),
                         'flux_err': None, 'meta': {}},
            'models': list(models), 'law': 'smc',
            'preprocess': over.pop('preprocess', {})}
    body.update(over)
    return body


def test_f112_api3_with_poly_s2_keys_real(client):
    code, d = _post(client, '/api/specphot/continuum',
                    _cont_body(models=('poly',)))
    assert code == 200, d
    pp = d['preprocess']
    assert pp['n_high_leverage'] is not None                  # 真值（可为 0）
    assert isinstance(pp['n_high_leverage'], int)
    assert pp['suggested_edge_ranges'] == []
    assert 'baseline_edge_spread' in pp


def test_f112_api3_without_poly_and_api2_s2_keys_null(client):
    code, d = _post(client, '/api/specphot/continuum',
                    _cont_body(models=('pl',)))
    assert code == 200, d
    pp = d['preprocess']
    assert pp['n_high_leverage'] is None and pp['baseline_edge_spread'] is None
    assert pp['edge_spread_after_downgrade'] is None
    assert pp['suggested_edge_ranges'] == []
    code, d = _post(client, '/api/specphot/photometry', _body())
    assert code == 200
    pp = d['preprocess']                                      # S1 无 F-62 基线
    assert pp['n_high_leverage'] is None and pp['suggested_edge_ranges'] == []


def test_f108_api3_factor2_rebin_live(client):
    code, d = _post(client, '/api/specphot/continuum',
                    _cont_body(models=('poly',), preprocess={'factor': 2}))
    assert code == 200, d
    pp = d['preprocess']
    assert pp['factor'] == 2 and pp['n_rebinned_pixels'] == 20
    assert d['sigma_method'] == 'second_diff_degraded'
    codes = {w['code'] for w in d['warnings']}
    assert 'CA-06' in codes and 'CA-47' in codes
