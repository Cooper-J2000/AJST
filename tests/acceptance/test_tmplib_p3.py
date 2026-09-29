"""模板库 × K 改正 P3 验收：API-8（K 改正实测点，S3）、T-27/T-28、
部分成功协议（A-6）、配额（Q-8）、kcorr 模式纯函数。

锚点（全部先经库/引擎实测再写进断言）：
  - GRB060218A：5373 行、26 discard、42 上限、1377 星等行（T-28）；
  - EP250108a：328 行、0 discard、10 上限、326 星等行；面 B 带 t_obs=12 d
    预测 mag=20.789275379464158、M=-19.169345850596393、K=0.2461682427126064、
    μ=39.712452987347945，且 mag−μ−K 与 M 逐位相等（T-27，实测差 0.0）；
  - GRB260321A：真实存在但无模板（CA-12 空壳），走 TL_NO_TEMPLATE_FOR_SOURCE。

T-34（error_budget 的 error_kind 语义）本期按任务说明跳过：预算端点属 P4，
届时再断言只有 line-precision/line-scatter 进预算。
"""
import os
import sys

import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_ROOT = os.path.join(_BACKEND, 'tmplibrary')

from tmplib import compare as tl_compare  # noqa: E402
from tmplib import engine, paths  # noqa: E402

#: T-27 golden（引擎直调实测；引擎版本/库文件变动时本测试必须先红）
GOLD_B = {'band': 'B', 't_obs_days': 12.0,
          'mag': 20.789275379464158, 'M': -19.169345850596393,
          'K': 0.2461682427126064, 'mu': 39.712452987347945}


@pytest.fixture(scope='module')
def client():
    try:
        from app import create_app
        app = create_app()
    except Exception as e:
        pytest.skip(f'应用工厂不可用（多半库不可达）: {type(e).__name__}: {e}')
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _compare(client, body):
    return client.post('/api/tmplib/compare', json=body)


# ── T-27：M_meas 往返恒等（F-21'） ─────────────────────────────────────────

def test_m_meas_roundtrip_bit_identical():  # T-27
    """令 m_meas := 面在同时刻的预测 mag ⇒ M_meas 与引擎 absolute_mag 逐位相同
    （引擎 K = mag_clean − ref − dm 且默认 reddening 下 mag == mag_clean；
    服务端 M_meas = m_AB − μ_engine − K 是同一减法，实测差 0.0）。"""
    root = paths.library_root()
    cs = engine.require()
    spec = cs.TemplateSpec.from_yaml(paths.template_yaml(root, 'ep250108a'))
    surf = engine.load_surface_cached('ep250108a', root)
    bank = engine.bank_cached(root)
    import numpy as np
    pred = cs.predict(surf, bank, spec.distance_obj(), GOLD_B['band'],
                      z=float(spec.z),
                      times_obs_days=np.asarray([GOLD_B['t_obs_days']]),
                      mode='band')
    assert float(pred.mag[0]) == GOLD_B['mag']           # golden 前置自证
    assert float(pred.absolute_mag[0]) == GOLD_B['M']

    row = {'band': 'B', 'time': GOLD_B['t_obs_days'] * 86400.0,
           'flux_density': GOLD_B['mag'], 'flux_density_err': 0.05,
           'flux_density_unit': 'magnitude', 'mag_system': 'AB',
           'gext_corr': True, 'mag_gextcor': None, 'mag_gextcor_err': None,
           'upperlimit': False, 'discard': False}
    transient = {'id': 'EP250108a', 'redshift': 0.176, 'gext_distmod': 39.718,
                 'redshift_ref': None, 'aliases': []}
    cu = tl_compare.kcorrected_measured(
        'EP250108a', 'ep250108a', transient=transient, rows=[row], root=root)
    d = cu['points']['detections']
    assert len(d) == 1 and not cu['points']['failed']
    assert d[0]['m_AB'] == GOLD_B['mag']                 # AB 系统零偏移
    assert d[0]['K'] == GOLD_B['K']
    assert d[0]['M_meas'] == GOLD_B['M']                 # 逐位（不是 approx）
    assert cu['distance_modulus_engine'] == GOLD_B['mu']


# ── T-28：discard/上限口径与计数上报（db） ─────────────────────────────────

def test_discard_and_upperlimit_accounting(client):  # T-28
    """GRB060218A（26 discard、42 上限）：API-8 点数 = 合格行 − discard − 上限，
    且计数逐键上报（F-06'）。"""
    r = _compare(client, {'sources': [{'transient_id': 'GRB060218A'}]})
    assert r.status_code == 200
    curves = [c for c in r.get_json()['curves'] if not c.get('state')]
    kc = next(c for c in curves if c['kind'] == 'kcorrected-measured')
    me = next(c for c in curves if c['kind'] == 'measured')
    for c in (kc, me):
        n = c['counts']
        assert n['discard'] == 26                        # 库实测锚点
        csv_rows = (n['detections'] + n['upper_limits'] + n['discard']
                    + n.get('clipped', 0) + n.get('failed', 0))
        # kcorrected 的 clipped/failed 也是合格行里的探测点
        assert csv_rows == n['rows_total'] - sum(
            v for k, v in n['rejected'].items() if k not in ('upperlimit', 'discard'))
        assert len(c['points']['detections']) == n['detections']
        assert len(c['points']['upper_limits']) == n['upper_limits']
    # 上限单列：不混入探测数组（F-06'）
    assert all(p['band'] for p in kc['points']['upper_limits'])


def test_split_rows_pure():  # F-06' 纯函数：剔 discard、上限单列
    rows = [('B', 1.0, 19.0, 0.1, 'ab', False, False),
            ('B', 2.0, 20.0, 0.1, 'ab', True, False),
            ('B', 3.0, 21.0, 0.1, 'ab', False, True)]
    det, ul, n_discard = tl_compare._split_rows(rows)
    assert len(det) == 1 and len(ul) == 1 and n_discard == 1


# ── API-8 契约：部分成功 / 配额 / 恒 200 ───────────────────────────────────

def test_compare_partial_success_three_states(client):  # A-6 / F-23 / W-24
    """1 条模板曲线 + 1 个有模板源 + 1 个无模板源 + 1 个不存在源 ⇒ 四态各正确、恒 200。"""
    r = _compare(client, {
        'curves': [{'template_id': 'ep250108a', 'band': 'B', 'n_times': 10},
                   {'template_id': 'no-such-template', 'band': 'B'}],
        'sources': [{'transient_id': 'EP250108a'},
                    {'transient_id': 'GRB260321A'},      # 真实存在、无模板
                    {'transient_id': 'NO_SUCH'}],
    })
    assert r.status_code == 200                          # 恒 200（A-6）
    b = r.get_json()
    assert b['code'] == 'TL_OK'
    curves = b['curves']
    by_kind = {}
    for c in curves:
        by_kind.setdefault(c['kind'], []).append(c)
    # 模板曲线：一好一 ✕
    tp = by_kind['template-prediction']
    ok = [c for c in tp if not c.get('state')]
    err = [c for c in tp if c.get('state') == 'error']
    assert len(ok) == 1 and ok[0]['points']['mag_AB']
    assert len(err) == 1 and err[0]['error']['code'] == 'TL_TEMPLATE_UNKNOWN'
    # 有模板源：kcorrected-measured 三键计数齐
    kc = next(c for c in by_kind['kcorrected-measured']
              if c.get('transient_id') == 'EP250108a')
    assert not kc.get('state')
    assert kc['counts']['detections'] > 0
    assert set(kc['points']) == {'detections', 'upper_limits', 'clipped', 'failed'}
    # 无模板源：逐条 ✕（F-23），实测点照给
    nt = next(c for c in by_kind['kcorrected-measured']
              if c.get('transient_id') == 'GRB260321A')
    assert nt['state'] == 'error'
    assert nt['error']['code'] == 'TL_NO_TEMPLATE_FOR_SOURCE'
    assert any(c['kind'] == 'measured' and c.get('transient_id') == 'GRB260321A'
               and not c.get('state') for c in curves)
    # 不存在源：measured ✕
    bad = next(c for c in by_kind['measured'] if c.get('transient_id') == 'NO_SUCH')
    assert bad['error']['code'] == 'TL_TRANSIENT_UNKNOWN'
    # provenance（§4.2）
    prov = b['provenance']
    assert prov['engine_version'] and len(prov['engine_code_sha256']) == 64
    assert prov['host_max_curves'] == 8 and prov['host_max_sources'] == 8


def test_compare_quota(client):  # Q-8 / E-04
    r = _compare(client, {'curves': [{'template_id': 'ep250108a', 'band': 'B'}] * 9})
    assert r.status_code == 400 and r.get_json()['code'] == 'TL_QUOTA'
    r = _compare(client, {'sources': [{'transient_id': 'EP250108a'}] * 9})
    assert r.status_code == 400 and r.get_json()['code'] == 'TL_QUOTA'
    r = _compare(client, {})
    assert r.status_code == 400 and r.get_json()['code'] == 'TL_INCONSISTENT_ARGS'
    r = _compare(client, {'sources': [{'template_id': 'ep250108a'}]})
    assert r.status_code == 400                        # sources[] 缺 transient_id


def test_compare_validate_pure():  # validate_request 纯函数电池
    with pytest.raises(tl_compare.tl_predict.TLError):
        tl_compare.validate_request(None)
    with pytest.raises(tl_compare.tl_predict.TLError):
        tl_compare.validate_request({'curves': [{'band': 'B'}]})
    req = tl_compare.validate_request({'sources': [{'transient_id': 'X'}]})
    assert req['include_measured'] is True
    req = tl_compare.validate_request({'sources': [{'transient_id': 'X'}],
                                       'include_measured': False})
    assert req['include_measured'] is False


def test_compare_mu_disclosure(client):  # POS-9：kcorrected 曲线带 Δμ 对照
    r = _compare(client, {'sources': [{'transient_id': 'EP250108a'}],
                          'include_measured': False})
    kc = r.get_json()['curves'][0]
    assert kc['mu'] and kc['mu']['ok']
    assert kc['mu']['engine'] == pytest.approx(39.7125, abs=1e-3)
    assert kc['mu']['catalog'] == pytest.approx(39.718, abs=1e-3)
    assert kc['distance_modulus_engine'] == pytest.approx(39.7125, abs=1e-3)


def test_compare_explicit_pair_and_no_measured(client):
    """显式 template_id+transient_id 对（无向导条目也可）+ include_measured=false。"""
    r = _compare(client, {'sources': [{'transient_id': 'EP250108a',
                                       'template_id': 'ep250108a'}],
                          'include_measured': False})
    curves = r.get_json()['curves']
    assert len(curves) == 1
    assert curves[0]['kind'] == 'kcorrected-measured'
    assert not curves[0].get('state')


def test_compare_ep250108a_counts(client):  # 行账口径与 P2 一致（F-52 同配方）
    r = _compare(client, {'sources': [{'transient_id': 'EP250108a'}],
                          'include_measured': True})
    me = next(c for c in r.get_json()['curves'] if c['kind'] == 'measured')
    n = me['counts']
    assert n['rows_total'] == 328
    assert n['rejected']['nonmag'] == 2
    # 库 10 个 upperlimit 行中 7 个缺测光系统按 F-10 先行剔除，
    # 合格口径单列 3 个（与 P2 ledger.rejected.upperlimit 一致）
    assert n['upper_limits'] == 3
    assert n['discard'] == 0
    # 合格行 = 检测 + 上限（+ discard）；measured 无 clipped/failed
    assert n['detections'] + n['upper_limits'] + n['discard'] == \
        n['rows_total'] - sum(v for k, v in n['rejected'].items()
                              if k not in ('upperlimit', 'discard'))


def test_kcorr_frontend_static():
    """kcorr 模式前端静态断言：Y 选项注册、三种线型、TXT-9、allowNonPositive、
    切换只重绘（取数仅在 key 变化时）。"""
    fe = os.path.join(os.path.dirname(_BACKEND), 'frontend')
    src = open(os.path.join(fe, 'js', 'pages', 'compare.js'),
               encoding='utf-8').read()
    assert 'value="kcorr"' in src                       # U-01：Y 下拉第三态
    assert 'TXT_ABSMAG' in src and '不含 K 改正' in src  # TXT-9
    assert "'transparent'" in src                       # IA-8 空心点
    assert 'allowNonPositive' in src                    # IA-11
    assert src.count('compareTmplib({') == 1            # 唯一取数点
    assert 'lastKcorr.key !== key' in src               # key 不变 ⇒ 只重绘
    api = open(os.path.join(fe, 'js', 'api.js'), encoding='utf-8').read()
    assert 'export const compareTmplib' in api
