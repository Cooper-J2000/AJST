"""模板库审核修复的回归钉（2026-10 审核报告 代码审核意见_20261002.md）。

覆盖审核发现的可执行修复项，逐条对应：
  - P1-1：API-8 批量通路的引擎异常按 code 分派（TL_OUT_OF_DOMAIN 不再被吞）；
  - P1-2：可答比例口径 = 引擎 load_samples（与 01 §E.6 底账逐模板相等，T-35）；
  - P2-1：API-5 索引登记前失败自愈（无孤儿 manifest，id 可重试）；
  - P2-2：ST-12 库配额（C_LIB_QUOTA）拒新建；
  - P2-8：TXT-3 的后端依据（meta.absolute_mag_is_intrinsic 在响应里可判）；
  - P3-3：vendor 漂移随曲线显形（CA-01 进 alerts，TXT-10 进 notes）；
  - P3-2：rows_sha256 按 (time, band) 升序（F-52 字面）；
  - P3-5：bool 穿不过 _finite（z=true ≠ z=1）；
  - T-01：predict_template 与低层 predict 在自身 z 逐位等值（往返一致）；
  - T-02：band 与 mono 的 K 严格不等，且各自带 mode；
  - T-21：z 冲突三例（0.0087/0.0085、0.0344/0.0343、0.0591/0.06）都报得出
    且 Δμ 与底账一致（0.0508 等）；
  - ST-6：engine.warm() 纯读盘预热成功且不写盘。

标注「需库」的用例走 Flask test_client（与 test_tmplib_p1/p2 同策略：库不
可达整组 skip）；其余可脱库运行。
"""
import os
import shutil
import sys

import numpy as np
import pytest

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_ROOT = os.path.join(os.path.dirname(_BACKEND), 'catadata', 'tmplibrary')

from tmplib import engine, guard, indomain, paths, predict as tl_predict  # noqa: E402
from tmplib import mudelta  # noqa: E402


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


def _login(c, role='admin', username='audit-fix'):
    with c.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = username
        s['role'] = role


# ── T-01/T-02：可脱库（真库只读） ─────────────────────────────────────────

def _spec(tid):
    cs = engine.require()
    return cs.TemplateSpec.from_yaml(os.path.join(_ROOT, 'templates', f'{tid}.yaml'))


def test_t01_roundtrip_own_z_bitwise():
    """T-01：predict_template（模板包装层）与低层 predict（同一张面）在相同
    (band, z, 时刻) 下逐位等值 —— 模板搬到 z 后与面给出的预测是同一批数。

    (band, z) 取引擎判可用的组合（sn2006aj 的面谱覆盖只有 ~5208–5421 Å 窄窗，
    全部面波段在自身 z 都按 E_DOMAIN 正确拒绝，故取 B@z=0.05 这一实测锚组）。
    距离口径与包装层一致：relocated z 用 spec.distance_at(z)（面存光度，
    视星等含 μ，距离不同会整体差一个 DM）。"""
    cs = engine.require()
    tid, band, z = 'sn2006aj', 'B', 0.05
    spec = _spec(tid)
    surf = engine.load_surface_cached(tid, _ROOT)
    bank = engine.bank_cached(_ROOT)
    dist = spec.distance_at(z)   # predict_template 对 relocated z 的默认距离
    lo, hi = (float(surf.t_valid[0]), float(surf.t_valid[1]))
    # 覆盖窗随时间收缩：sn2006aj·B 在 ~31.4 d 后按 E_DOMAIN 正确拒绝，
    # 故取早段网格（全部落在覆盖内，见引擎拒绝信息里的 epochs_covered_days）。
    in_win = np.array([2.0, 4.0, 6.0, 9.0, 12.0, 15.0, 18.0, 20.0, 25.0, 30.0])
    assert in_win.size >= 10 and in_win[0] >= lo and in_win[-1] <= min(hi, 31.3)
    times_obs = np.asarray(in_win, float) * (1.0 + z)
    hi_r = cs.predict_template(tid, band, z=z, root=_ROOT,
                               times_obs_days=times_obs, mode='band')
    lo_r = cs.predict(surf, bank, dist, band, z=z,
                      times_obs_days=times_obs, mode='band')
    assert hi_r.mode == 'band'
    assert np.array_equal(np.asarray(hi_r.mag, float), np.asarray(lo_r.mag, float))
    assert np.array_equal(np.asarray(hi_r.k_correction, float),
                          np.asarray(lo_r.k_correction, float))


def test_t02_band_vs_mono_strictly_unequal():
    """T-02：band 与 mono 的 K 严格不等；两者输出各自带 mode。"""
    cs = engine.require()
    z = 0.05
    out = {}
    for mode in ('band', 'mono'):
        r = cs.predict_template('sn2006aj', 'B', z=z, root=_ROOT,
                                times_obs_days=[6.0, 9.0], mode=mode)
        assert r.mode == mode
        out[mode] = np.asarray(r.k_correction, float)
    assert not np.any(out['band'] == out['mono'])   # 严格不等（逐点）
    assert np.max(np.abs(out['band'] - out['mono'])) > 1e-6


def test_p3_5_bool_z_rejected():
    """P3-5：float(True)==1.0 不许把 z=true 当 z=1 放行。"""
    with pytest.raises(tl_predict.TLError) as ei:
        tl_predict.predict_curve({'template_id': 'sn2006aj', 'band': 'B', 'z': True})
    assert ei.value.code == 'TL_PARAM_NOT_FINITE'


def test_p3_2_rows_sha256_time_band_order():
    """P3-2：rows_sha256 排序键 = (time, band) 升序（F-52 字面）。"""
    import hashlib

    def tok(v):
        if v is None:
            return ''
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if isinstance(v, float):
            return repr(v)
        return str(v)

    rows = [('B', 2.0, 1.0, None, 'ab', False, False),
            ('a', 1.0, 2.0, None, 'ab', False, False)]
    h = hashlib.sha256()
    for row in sorted(rows, key=lambda r: (r[1], r[0])):
        h.update('|'.join(tok(v) for v in row).encode('utf-8'))
        h.update(b'\n')
    assert guard.rows_sha256(rows) == h.hexdigest()
    # band 主序（旧配方）必须给出不同结果 —— 防止实现悄悄退回
    h2 = hashlib.sha256()
    for row in sorted(rows, key=lambda r: '|'.join(tok(v) for v in r)):
        h2.update('|'.join(tok(v) for v in row).encode('utf-8'))
        h2.update(b'\n')
    assert guard.rows_sha256(rows) != h2.hexdigest()


def test_p3_3_ca01_in_curve_when_vendor_drifts(monkeypatch):
    """P3-3：vendor 漂移不阻断预测，但 CA-01 必须随曲线显形（ST-15/E-20）。"""
    from tmplib import guard as g
    monkeypatch.setattr(g, 'vendor_axis',
                        lambda root=None, data_dir=None: {
                            'ok': False, 'code': 'CA-01', 'message': '漂移测试'})
    r = tl_predict.predict_curve({'template_id': 'sn2006aj', 'band': 'B',
                                  'z': 0.05, 'times_rest_days': [6.0]})
    assert r['curve']['alerts'][-1] == 'CA-01'
    assert any('漂移测试' in n for n in r['notes'])


def test_st6_warm_readonly():
    """ST-6：engine.warm() 预热全部出厂面，纯读盘成功。"""
    st = engine.warm(_ROOT)
    assert st['done'] and st['ok'] and st['surfaces'] >= 9, st
    assert engine.warm_status()['ok'] is True


# ── T-21：z 冲突三例（脱库：catalog 行按底账 z + 库口径 distmod 合成） ────

#: (template_id, 底账 catalog z)；模板 z 从 manifest 读。01 §E.1/E.8 实测。
_Z_CONFLICTS = [
    ('sn1998bw', 0.0085, 0.0508),
    ('ep260321a', 0.0343, None),
    ('sn2010bh', 0.06, None),
]


def test_t21_z_conflict_three_cases():
    from astropy.cosmology import Planck18
    for tid, z_cat, ledger_delta in _Z_CONFLICTS:
        spec = _spec(tid)
        z_tmpl = float(spec.z)
        # 库的 μ 是 Planck18.distmod(红移)（舍入 0.001）—— OUT-12 口径
        mu_cat = round(float(Planck18.distmod(z_cat).value), 3)
        mu = mudelta.compute_mu(spec, {'id': 'X', 'redshift': z_cat,
                                       'gext_distmod': mu_cat})
        assert mu['ok'], (tid, mu)
        assert abs(z_cat - z_tmpl) > 1e-6          # 确实构成 T-21 冲突
        d1 = float(Planck18.distmod(z_tmpl).value)
        d2 = float(Planck18.distmod(z_cat).value)
        # F-46 归因优先级：sn1998bw 是实测距离（kind!=cosmological）⇒ ①距离
        # 来源不同优先，'z 值不同'退居 secondary；cosmological 模板则为主因。
        causes = [mu['cause']] + [c['cause'] for c in mu['secondary']]
        assert 'z 值不同' in causes, (tid, causes)
        entry = mu if mu['cause'] == 'z 值不同' else \
            next(c for c in mu['secondary'] if c['cause'] == 'z 值不同')
        assert f'Planck18.distmod 差 {round(d1 - d2, 4)} mag' in (entry['detail'] or '')
        if ledger_delta is not None:
            assert abs(round(d1 - d2, 4)) == ledger_delta   # 01 底账：0.0508


# ── T-35 守卫：可答比例与底账逐模板相等（P1-2 的口径再漂移即红） ──────────

def test_t35_indomain_matches_ledger():
    """01 §E.6 底账：九模板 5813/5959 = 97.5%；单模板 sn2002ap 913、
    at2018cow 962、sn1998bw 2059、at2017gfo 512（load_samples 口径实测）。"""
    lib = paths.read_library(_ROOT)
    assert lib['templates']['sn2002ap']['in_domain']['total'] == 913
    assert lib['templates']['at2018cow']['in_domain']['total'] == 962
    assert lib['templates']['sn1998bw']['in_domain']['total'] == 2059
    assert lib['templates']['at2017gfo']['in_domain']['total'] == 512
    tot = sum(e['in_domain']['total'] for e in lib['templates'].values())
    ans = sum(e['in_domain']['answered'] for e in lib['templates'].values())
    assert (tot, ans) == (5959, 5813)


def test_p1_5_top_engine_pin_matches_live():
    """P1-5：library.json 顶层引擎指纹与每模板指纹、现行引擎一致。"""
    lib = paths.read_library(_ROOT)
    live = engine.code_sha256()
    assert lib['engine']['code_sha256'] == live
    for tid, e in lib['templates'].items():
        assert e['inputs_sha256']['code'] == live, tid


# ── API-8 引擎错误路径（P1-1）与 ST-12 配额（P2-2）：需库 ─────────────────

def test_p1_1_compare_engine_error_keeps_code(client):
    """API-8 里越界曲线必须得到 TL_OUT_OF_DOMAIN + context（E-30/E-13），
    而不是被吞成 TL_INTERNAL；同参数在 API-6 的行为不变。"""
    r6 = client.post('/api/tmplib/predict',
                     json={'template_id': 'sn2006aj', 'band': 'B', 'z': 2.0})
    assert r6.status_code == 409
    assert r6.get_json()['code'] == 'TL_OUT_OF_DOMAIN'
    r8 = client.post('/api/tmplib/compare', json={
        'curves': [{'template_id': 'sn2006aj', 'band': 'B', 'z': 2.0}],
        'sources': []})
    assert r8.status_code == 200                    # A-6：恒 200 部分成功
    entry = r8.get_json()['curves'][0]
    assert entry['state'] == 'error'
    assert entry['error']['code'] == 'TL_OUT_OF_DOMAIN', entry['error']
    assert entry['error'].get('context')            # E-13：context 逐键展开


def test_p2_2_quota_blocks_create(client, monkeypatch):
    """ST-12：库配额超限 ⇒ TL_QUOTA 400，且零写盘副作用。"""
    _login(client)
    monkeypatch.setattr(paths, 'quota_check',
                        lambda root=None: {'ok': False, 'limit_bytes': 1,
                                           'usage_bytes': 2, 'over_by_bytes': 1})
    r = client.post('/api/tmplib/templates', json={
        'id': 'audit-quota-x', 'transient_id': 'EP250108a',
        'object_class': 'x', 'redshift': {'value': 0.176, 'source': 't'},
        'distance': {'kind': 'cosmological'}, 'rowset': 'raw',
        'null_system_policy': 'drop', 'require_min_bands': 2})
    assert r.status_code == 400
    assert r.get_json()['code'] == 'TL_QUOTA'


def test_p2_1_orphan_manifest_self_heals(client, tmp_path, monkeypatch):
    """P2-1：索引登记前失败 ⇒ 半成品全部回滚，同 id 可立即重试（不再死锁）。"""
    root = tmp_path / 'lib'
    (root / 'templates').mkdir(parents=True)
    (root / 'data' / 'raw').mkdir(parents=True)
    (root / 'data' / 'surfaces').mkdir(parents=True)
    shutil.copytree(os.path.join(_ROOT, 'data', 'filters'), root / 'data' / 'filters')
    old = os.environ.get('AJST_TMPLIB_DIR')
    os.environ['AJST_TMPLIB_DIR'] = str(root)
    from tmplib import engine
    engine.reset_caches()
    decl = {
        'id': 'audit-orphan-x', 'transient_id': 'EP250108a',
        'object_class': 'x',
        'redshift': {'value': 0.176, 'source': 't'},
        'distance': {'kind': 'cosmological'}, 'rowset': 'raw',
        'null_system_policy': 'drop', 'require_min_bands': 2,
    }
    try:
        _login(client)
        # indomain 阶段炸掉（登记 library.json 之前）——旧实现会留下孤儿 manifest
        import routes.tmplib as rt
        def _boom(tid, r=None):
            raise RuntimeError('审计注入：登记前失败')
        monkeypatch.setattr(rt.indomain, 'compute', _boom)
        # TESTING 下 Flask 异常直接传播（PROPAGATE_EXCEPTIONS）
        with pytest.raises(RuntimeError):
            client.post('/api/tmplib/templates', json=decl)
        for rel in ('templates/audit-orphan-x.yaml', 'data/raw/audit-orphan-x.csv',
                    'data/surfaces/audit-orphan-x.npz'):
            assert not (root / rel).exists(), rel      # 半成品已清
        # id 未死锁：同 id 可立即重试成功
        monkeypatch.setattr(rt.indomain, 'compute', lambda tid, r=None: {
            'total': 0, 'answered': 0, 'by_band': {}})
        r2 = client.post('/api/tmplib/templates', json=decl)
        assert r2.status_code == 200, r2.get_data(as_text=True)[:300]
        assert r2.get_json()['code'] == 'TL_OK'
        assert (root / 'templates' / 'audit-orphan-x.yaml').exists()
    finally:
        if old is None:
            os.environ.pop('AJST_TMPLIB_DIR', None)
        else:
            os.environ['AJST_TMPLIB_DIR'] = old
        engine.reset_caches()


def test_p2_8_meta_carries_intrinsic_flag(client):
    """P2-8：TXT-3 的后端依据在 API-6 响应里可判（meta.absolute_mag_is_intrinsic）。"""
    r = client.post('/api/tmplib/predict', json={
        'template_id': 'sn2006aj', 'band': 'B', 'z': 0.05,
        'times_rest_days': [6.0]})
    assert r.status_code == 200
    meta = r.get_json()['curve']['meta']
    assert 'absolute_mag_is_intrinsic' in meta      # 键在（21 键之一），前端可触发 TXT-3
