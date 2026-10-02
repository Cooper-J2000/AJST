"""L2: specphot **P1b 收口验收**（02b §3.14 / §9 P1b 行：预处理与稳健性 · 读侧两段）。

覆盖（T-75/T-78/T-79 的 P1b 可测半段 + Q-33/W-36/W-37 的服务端半段）：
  T-75①  由 test_l1_specphot_p1b_baseline.py 承载（golden 逐字节，比较域按
          F-113③ + 掩膜/离群新行为族裁量），本文件不重复。
  T-75②  preprocess_hash 可复算：同一编码规则两次独立计算得同一串、非空、
          恒等态不随请求变化；preprocess.identity=true。
  T-75③  缓存键双哈希（F-107③/ST-3）：键序列含 mask_hash 与 preprocess_hash；
          同谱只差 factor ⇒ 键必不同；smooth_px 不在任何哈希键集。
  T-78   errcol_verdict 六值闭集、判定序唯一命中即停（五种注入各归其位，
          constant 与 flat_relative 不互串）；has_err 与 verdict 独立回显；
          非	ok ⇒ 回落/坚持二选一（CA-47①②），errcol_accepted 如实回显。
  T-79① 静态扫描（AST）：backend/specphot 全部源码的 least_squares 调用无
          loss=/f_scale（F-111①）；③ 离群候选只回显 n_outlier_flagged，
          未经 U-52 确认前 n_masked_pixels 与 mask_hash 不变。
  Q-33   preprocess{} 形态校验（factor 闭集/ranges/smooth/errcol_choice）；
          factor>1 与 smooth ⇒ E-13(501 phase='P2')。
  W-36   U-13 框选与 U-50 手输同段 ⇒ 布尔并集后 n_masked_pixels 不翻倍；
          自动表（F-72①②）默认剔除并入掩膜与 mask_hash。
纪律：全链只读；seed 无随机判据（语料全部显式构造）；上传件不落盘。
"""
import ast
import json
import math
import os
import re

import pytest

from app import create_app
import specphot
from specphot import preprocess as pre
from specphot.constants import C_ERRCOL_REL_SPREAD_TOL, C_MASK_ABS_TABLE
from specphot.reader import SpecLoadError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ERRCOL_VERDICTS = ('ok', 'all_zero', 'constant', 'nonfinite', 'negative',
                   'flat_relative')

# ─── 语料（40 点 ≥ 30：相对散布步可判域；λ 避开全部掩膜表覆盖区） ──────────
LAMS = [4000.0 + 2.5 * i for i in range(40)]
F0 = 1e-14
CC = {'band': 'p1b-r', 'lam_aa': [3990.0, 4050.0, 4110.0],
      't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'}
ANCHOR = [{'band': 'p1b-r', 'mag': 18.0, 'mag_system': 'AB', 'mag_err': 0.05}]


def _flux40():
    return [F0 * (1.0 + 0.3 * math.sin(i * 1.7)) for i in range(40)]      # 非平谱


def _body(flux=None, flux_err=None, **over):
    body = {'spectrum': {'lam_aa': list(LAMS), 'flux': list(flux or _flux40()),
                         'flux_err': flux_err, 'meta': {}},
            'bands': ['p1b-r'], 'custom_curves': [dict(CC)],
            'weighting': 'photon', 'anchor_rows': [dict(ANCHOR[0])]}
    body.update(over)
    return body


def _body_lam(lams, flux=None, flux_err=None, curve=None, **over):
    """等长换轴语料：flux/flux_err 随 lams 截齐，通带随之给出（避免 E-04）。"""
    n = len(lams)
    body = _body(flux=(flux or _flux40())[:n], flux_err=flux_err, **over)
    body['spectrum']['lam_aa'] = list(lams)
    if curve is not None:
        body['custom_curves'] = [dict(curve)]
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
        s['username'] = 'p1b-tester'
        s['role'] = 'user'


def _json(resp):
    return json.loads(resp.get_data(as_text=True))


def _post(client, body):
    return _json(client.post('/api/specphot/photometry', json=body))


# ═══ T-75②：preprocess_hash 可复算 + 恒等态不随请求变化 ═══════════════════

def _f80(v):
    """F-80①–④ 的**独立**测试实现（与 preprocess.f80_val 同规则、异实现）。"""
    if v is None:
        return 'null'
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return '"NaN"'
        if math.isinf(v):
            return '"Infinity"' if v > 0 else '"-Infinity"'
        return repr(float('%.12g' % v))
    if isinstance(v, str):
        return '"' + v.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return '[' + ','.join(_f80(x) for x in v) + ']'


def test_t75_2_preprocess_hash_recomputable_identity_true(client):
    from specphot import SPEC_PHOT_VERSION
    d = _post(client, _body())
    pp = d['preprocess']
    assert pp['identity'] is True and pp['errcol_verdict'] == 'ok' \
        and pp['errcol_accepted'] is False
    # 独立复算：键序 = {factor, mask_hash, errcol_verdict, errcol_accepted,
    # spec_hash, 版本戳}（§4.2 preprocess{} 行 = 唯一载体）
    seq = '[' + ','.join(_f80(x) for x in
                         [1, d['mask_hash'], 'ok', False, d['spec_hash'],
                          SPEC_PHOT_VERSION]) + ']'
    import hashlib
    h = hashlib.sha256(seq.encode('utf-8')).hexdigest()[:12]
    assert pp['preprocess_hash'] == h and h
    # 恒等态不随请求变化：同谱换一个通带（不同 ckey、独立复算）⇒ 同串
    cc2 = dict(CC, band='p1b-b', lam_aa=[4000.0, 4060.0, 4120.0])
    d2 = _post(client, _body(bands=['p1b-b'], custom_curves=[cc2],
                             anchor_rows=[{'band': 'p1b-b', 'mag': 18.0,
                                           'mag_system': 'AB', 'mag_err': 0.05}]))
    assert d2['preprocess']['preprocess_hash'] == h


def test_t75_3_cache_key_contains_both_hashes(client):
    """缓存键双哈希（F-107③）：键序列含 mask_h 与 pp_h 两项（源码级）；只差
    factor 的两个 preprocess_hash 必不同（⇒ 缓存键必不同），smooth_px 不进
    preprocess_hash 键集（F-109⑤；其字面不在 preprocess_hash 输入序列里）。"""
    src = open(os.path.join(_REPO_ROOT, 'backend', 'specphot', 'photometry.py'),
               encoding='utf-8').read()
    assert re.search(r'mask_h,\s*pp_h,', src), 'ST-3 缓存键缺双哈希项'
    # smooth_px 不进 preprocess_hash 键集（F-109⑤）：取该函数的 AST 片段判词
    pp_src = open(os.path.join(_REPO_ROOT, 'backend', 'specphot', 'preprocess.py'),
                  encoding='utf-8').read()
    tree = ast.parse(pp_src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == 'preprocess_hash')
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert 'smooth' not in ast.unparse(ret.value), \
        'smooth_px 不得进入 preprocess_hash'
    # 结构级：只差 factor ⇒ 两串必不同（同谱两次这样的请求不得命中同一缓存项）
    mask_h, sh = 'abcdef123456', '0123456789abcdef'
    assert pre.preprocess_hash(mask_h, 1, 'ok', False, sh) \
        != pre.preprocess_hash(mask_h, 2, 'ok', False, sh)
    # mask_hash 变 ⇒ preprocess_hash 随之变（四类并集是它的输入）
    assert pre.preprocess_hash('a' * 12, 1, 'ok', False, sh) \
        != pre.preprocess_hash('b' * 12, 1, 'ok', False, sh)


# ═══ T-78：误差列退化六值闭集、判定序、二选一确认 ══════════════════════════

def test_t78_1_verdict_closed_set_six_values():
    assert pre.ERRCOL_VERDICTS == ERRCOL_VERDICTS
    assert len(ERRCOL_VERDICTS) == 6
    assert pre.ERRCOL_CHOICES == ('auto', 'proxy', 'as_provided')


def test_t78_2_all_zero_column_verdict_and_has_err_independent(client):
    """三列齐但全 0 ⇒ has_err=true 且 verdict='all_zero'（第 3 步先命中，
    0/0 从不被评估、不得是 constant）；两键同时回显互不顶替（T-78③）。"""
    d = _post(client, _body(flux_err=[0.0] * 40))
    assert d['read_stats']['has_err'] is True
    assert d['preprocess']['errcol_verdict'] == 'all_zero'
    assert d['preprocess']['errcol_accepted'] is False
    # 非 ok ⇒ 回落 F-54 代理（F-110②）+ CA-47②；sigma_method 回 V-8 词表
    assert d['results'][0]['sigma_method'] in ('second_diff', 'mad_window', 'mixed')
    ca47 = [w for w in d['warnings'] if w['code'] == 'CA-47']
    assert ca47 and '2' in ca47[0].get('reason', '') and 'all_zero' in ca47[0]['message']


def test_t78_2_negative_first_even_with_half_nonfinite(client):
    """半数非有限且含一个负值 ⇒ negative（第 1 步先命中，不落 nonfinite）。"""
    ferr = [None] * 20 + [-1e-15] + [1e-15] * 19
    d = _post(client, _body(flux_err=ferr))
    assert d['preprocess']['errcol_verdict'] == 'negative'


def test_t78_2_nonfinite_over_half(client):
    ferr = [None] * 30 + [1e-15] * 10
    d = _post(client, _body(flux_err=ferr))
    assert d['preprocess']['errcol_verdict'] == 'nonfinite'


def test_t78_2_constant_column(client):
    """σ ≡ c（c>0）⇒ constant；verdict=ok 之外的坚持/回落两支都可达。"""
    d = _post(client, _body(flux_err=[1e-15] * 40))
    assert d['preprocess']['errcol_verdict'] == 'constant'
    assert d['results'][0]['sigma_method'] in ('second_diff', 'mad_window')
    # 用户坚持（as_provided）⇒ sigma_method='spec_err' + CA-47① 常驻 +
    # errcol_accepted=true（F-110③）
    d2 = _post(client, _body(flux_err=[1e-15] * 40,
                             preprocess={'errcol_choice': 'as_provided'}))
    assert d2['preprocess']['errcol_verdict'] == 'constant'
    assert d2['preprocess']['errcol_accepted'] is True
    assert d2['results'][0]['sigma_method'] == 'spec_err'
    ca47 = [w for w in d2['warnings'] if w['code'] == 'CA-47']
    assert ca47 and '1' in ca47[0].get('reason', '')
    assert d2['results'][0]['err_source']['mag_err_stat'] == 'closed_form'


def test_t78_2_flat_relative_column(client):
    """σ = 0.05·|F|（F 平滑）⇒ flat_relative（第 4 步不中：σ 自身散布大；
    第 5 步 r 的相对散布 < 容差）。"""
    flux = [F0 * (1.0 + 0.01 * i) for i in range(40)]
    ferr = [0.05 * f for f in flux]
    d = _post(client, _body(flux=flux, flux_err=ferr))
    assert d['preprocess']['errcol_verdict'] == 'flat_relative'


def test_t78_2_flat_flux_constant_sigma_is_constant_not_flat_relative(client):
    """F 平坦且 σ ≡ c ⇒ constant（固定序，保守；与 flat_relative 不互串）。"""
    flux = [F0] * 40
    d = _post(client, _body(flux=flux, flux_err=[1e-15] * 40))
    assert d['preprocess']['errcol_verdict'] == 'constant'


def test_t78_2_ok_column_varied(client):
    """正常离散误差列 ⇒ ok；每个注入只回显一个 verdict 值（单值枚举键）。"""
    ferr = [1e-15 * (1.0 + 0.7 * math.sin(i * 2.3)) for i in range(40)]
    d = _post(client, _body(flux_err=ferr))
    assert d['preprocess']['errcol_verdict'] == 'ok'
    assert d['preprocess']['errcol_accepted'] is False
    # 值集恰为闭集：响应里的枚举键是单值字符串
    assert d['preprocess']['errcol_verdict'] in ERRCOL_VERDICTS


def test_t78_2_n_err_below_30_ok_still_needs_ca21(client):
    """n_err<30：相对散布步跳过、末位 ok 同挂 CA-21（点数总则，F-110①）。"""
    lams = [4000.0 + 10.0 * i for i in range(12)]
    ferr = [1e-15 * (1.0 + 0.5 * math.sin(i * 2.1)) for i in range(12)]
    d = _post(client, _body_lam(lams, flux_err=ferr))
    assert d['preprocess']['errcol_verdict'] == 'ok'
    ca21 = [w for w in d['warnings'] if w['code'] == 'CA-21']
    assert any('误差列' in w['message'] and '< 30' in w['message'] for w in ca21)


def test_t78_2_judgment_order_all_zero_before_constant(client):
    """判定序本身可测（函数级直注）：全 0 列在第 3 步命中；容差与常量表同值。"""
    import numpy as np
    flux = [1e-14] * 40
    out = pre.errcol_verdict(flux, [0.0] * 40)
    assert out['verdict'] == 'all_zero'
    out = pre.errcol_verdict(flux, [1e-15] * 40)
    assert out['verdict'] == 'constant'
    # 半 NaN + 负值（构造直入，不经读侧的 V-9 σ>0 闸）
    out = pre.errcol_verdict(flux, [None] * 20 + [-1e-15] + [1e-15] * 19)
    assert out['verdict'] == 'negative'
    # median(|F|) = 0（半数零流量）⇒ 第 5 步不命中、直接落 ok（不得假判退化；
    # σ 离散使第 4 步也不命中）
    flux_half0 = [0.0] * 20 + [1e-14 * (1.0 + 0.01 * i) for i in range(20)]
    ferr_var = [1e-15 * (1.0 + 0.5 * math.sin(i * 1.3)) for i in range(40)]
    out = pre.errcol_verdict(flux_half0, ferr_var)
    assert out['verdict'] == 'ok'
    # 无列 ⇒ ok（F-110⑤：代理本来就是默认通路）
    assert pre.errcol_verdict(flux, None) == {'verdict': 'ok', 'n_err': 0,
                                              'ca21': False}
    assert C_ERRCOL_REL_SPREAD_TOL == 1e-3


def test_t78_3_as_provided_rejected_when_verdict_ok(client):
    """Q-33：仅当判非 ok 才允许 as_provided；verdict=ok 时 ⇒ E-14。"""
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'errcol_choice': 'as_provided'}))
    assert r.status_code == 400 and _json(r)['reason'] == 'errcol_choice'


def test_t78_3_errcol_choice_closed_set(client):
    for bad in ('keep_column', 'force_proxy', 'yes'):
        r = client.post('/api/specphot/photometry',
                        json=_body(preprocess={'errcol_choice': bad}))
        assert r.status_code == 400 and _json(r)['reason'] == 'bad_enum', bad


# ═══ Q-33：preprocess{} 形态校验 + 期次闸 ═════════════════════════════════

def test_q33_factor_closed_set_then_phase_gate(client):
    """P2 切片 2c 起合束算术落地（F-108）：factor=2 放行（原 E-13 期次闸拆除）；
    闭集外的 5 仍 E-14 rebin_factor。factor>1 的数值正确性由
    test_l1_specphot_preprocess_p2.py / test_l2_specphot_p2c.py 承载。"""
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'factor': 5}))
    assert r.status_code == 400 and _json(r)['reason'] == 'rebin_factor'
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'factor': 2}))
    assert r.status_code == 200, _json(r)
    d = _json(r)
    assert d['preprocess']['factor'] == 2
    assert d['preprocess']['n_rebinned_pixels'] == 20      # ⌊40/2⌋
    assert d['preprocess']['identity'] is False


def test_q33_ranges_shape_and_limit(client):
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'ranges': [[4100.0, 4000.0]]}))
    assert r.status_code == 400 and _json(r)['reason'] == 'preprocess_range'
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'ranges': [['a', 'b']]}))
    assert r.status_code == 400 and _json(r)['reason'] == 'preprocess_range'
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'ranges': [[4000.0, 4001.0]] * 33}))
    assert r.status_code in (400, 413) and _json(r)['reason'] == 'too_many_masks'


def test_q33_smooth_shape_and_phase_gate(client):
    """P2 切片 2c 起平滑放行（F-109）：越界 8 仍 E-14；布尔 true 不在 Q-33 闭集
    {false} ∪ [1, 7] 内 ⇒ E-14（原 E-13 期次闸拆除；开启值只做形态校验，
    服务端不消费，响应逐字节同）。"""
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'smooth': 8}))
    assert r.status_code == 400 and _json(r)['reason'] == 'bad_enum'
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'smooth': True}))
    assert r.status_code == 400 and _json(r)['reason'] == 'bad_enum'
    r = client.post('/api/specphot/photometry',
                    json=_body(preprocess={'smooth': 3}))
    assert r.status_code == 200, _json(r)
    d = _json(r)
    assert d['preprocess']['smooth_px'] == 0          # 服务端不消费（实现登记）
    assert d['preprocess']['identity'] is True        # 平滑不改数值（F-109⑤）


# ═══ W-36：掩膜归并（U-13/U-50 同数组、自动表默认剔除） ═══════════════════

def test_w36_user_mask_counts_pixels_once(client):
    """U-50 手输（preprocess.ranges）与 U-13 框选（body.mask）同一段 ⇒
    布尔并集后 n_masked_pixels 不翻倍（F-107①③）。"""
    seg = [4040.0, 4050.0]                       # λ 步 2.5 ⇒ i=16..20，恰 5 像素
    d1 = _post(client, _body(mask=[seg]))
    d2 = _post(client, _body(mask=[seg], preprocess={'ranges': [seg]}))
    assert d1['preprocess']['n_masked_pixels'] == 5
    assert d2['preprocess']['n_masked_pixels'] == 5       # 不翻倍
    assert d1['mask_hash'] == d2['mask_hash']


def test_w36_auto_tables_enter_mask_and_hash(client):
    """F-72①②/ F-107①：自动表覆盖区默认剔除并入掩膜（用户未动作 ⇒ 用户段
    为空而 mask_ranges 含表段；mask_hash 与 preprocess_hash 随之改变）。"""
    clean = _post(client, _body())
    lams = [6800.0 + 2.5 * i for i in range(40)]          # 撞 O₂ B 带 6867–6884
    curve = {'band': 'p1b-r', 'lam_aa': [6790.0, 6840.0, 6900.0],
             't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'}
    d = _post(client, _body_lam(lams, curve=curve))
    pp = d['preprocess']
    assert [6867.0, 6884.0] in pp['mask_ranges']
    n_abs = sum(1 for x in lams if 6867.0 <= x <= 6884.0)
    assert pp['n_masked_pixels'] == n_abs
    assert pp['mask_ranges'] != [] and pp['n_masked_pixels'] > 0
    assert pp['mask_hash'] != clean['preprocess']['mask_hash']
    assert pp['preprocess_hash'] != clean['preprocess']['preprocess_hash']
    assert [6867.0, 6884.0] in [s[:2] for s in C_MASK_ABS_TABLE]


def test_w36_user_mask_changes_hash_and_band_counts(client):
    """用户掩膜 ⇒ mask_hash/preprocess_hash 变、行级 n_masked_pixels 如实回显
    （F-107④：第三方能从导出头重建参与积分的像素集）。"""
    d = _post(client, _body(mask=[[4040.0, 4050.0]]))
    pp = d['preprocess']
    assert pp['n_masked_pixels'] == 5
    assert d['results'][0]['n_masked_pixels'] == 5
    assert pp['mask_ranges'] == [[4040.0, 4050.0]]
    assert pp['identity'] is False


# ═══ T-79①③：稳健损失禁用扫描 + 离群点只标记 ════════════════════════════

def test_t79_1_static_scan_no_loss_or_f_scale():
    """backend/specphot 全部源码：least_squares 调用无 loss=/f_scale 关键字
    （AST 判据，F-111①；唯一合法形态 = F-91① 白名单关键字）。"""
    root = os.path.join(_REPO_ROOT, 'backend', 'specphot')
    hits = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith('.py'):
            continue
        with open(os.path.join(root, fn), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fname = node.func
            name = fname.attr if isinstance(fname, ast.Attribute) else \
                (fname.id if isinstance(fname, ast.Name) else '')
            if name != 'least_squares':
                continue
            for kw in node.keywords:
                if kw.arg in ('loss', 'f_scale'):
                    hits.append((fn, kw.arg))
    assert not hits, hits


def test_t79_3_outliers_flagged_not_removed(client):
    """+100σ 尖峰（400 点，占比 < C_CLIP_MAX_FRAC）⇒ n_outlier_flagged ≥ 1，
    但 n_masked_pixels=0、用户段为空、掩膜不被自动改变（T-79③）；
    候选段随 CA-10/outlier_flagged 回显（F-111② "摆在告警区"，U-52 的载体）。"""
    n = 400
    lams = [4000.0 + 2.5 * i for i in range(n)]
    flux = [F0 * (1.0 + 0.3 * math.sin(i * 0.17)) for i in range(n)]
    flux[200] = flux[200] + 100.0 * 1e-14
    curve = {'band': 'p1b-r', 'lam_aa': [3990.0, 4500.0, 5010.0],
             't': [0.0, 1.0, 0.0], 'curve_kind': 'transmission'}
    d = _post(client, _body_lam(lams, flux=flux, curve=curve))
    assert d['preprocess']['n_outlier_flagged'] >= 1
    assert d['preprocess']['n_outlier_flagged'] * 20 <= n    # ≤ 5% ⇒ 按钮在
    assert d['preprocess']['n_masked_pixels'] == 0
    assert d['preprocess']['mask_ranges'] == []
    assert d['preprocess']['identity'] is True
    w = next(x for x in d['warnings'] if x.get('reason') == 'outlier_flagged')
    assert w['segments'] and all(seg[0] <= seg[1] for seg in w['segments'])
    assert any(4000.0 <= seg[0] <= 5000.0 for seg in w['segments'])


def test_t79_3_outlier_over_frac_no_button_ca47_4(client):
    """候选占比 > C_CLIP_MAX_FRAC ⇒ CA-47④（不提供剔除按钮、无 segments）。
    40 点谱上一支 +100σ 尖峰在二阶差分域占 ≥3 像素（>5%）⇒ 必然超阈。"""
    flux = _flux40()
    flux[20] = flux[20] + 100.0 * 1e-14
    d = _post(client, _body(flux=flux))
    assert d['preprocess']['n_outlier_flagged'] * 20 > 40
    ca47 = [w for w in d['warnings'] if w['code'] == 'CA-47'
            and w.get('reason') == 'outlier_frac_over']
    assert ca47 and 'CA-47④' in ca47[0]['message']
    assert not any(w.get('reason') == 'outlier_flagged' and w.get('segments')
                   for w in d['warnings'])


# ═══ P1b 结构位与 V-22 读侧落位 ═══════════════════════════════════════════

def test_preprocessed_spectrum_structure_p1b():
    """F-106④ 的 P1b 字段集：display_smoothed 是 P2 结构位（构造即 None，
    T-77① 的 AST 扫描域内只以写入位置出现在 preprocess.py）。"""
    import dataclasses
    names = {f.name for f in dataclasses.fields(pre.PreprocessedSpectrum)}
    assert {'lam_aa', 'flux', 'flux_err', 'mask_bool', 'factor', 'fit_array',
            'display_smoothed', 'hashes'} <= names
    import numpy as np
    ps = pre.build_spectrum({'lam_aa': [4000.0, 4001.0], 'flux_err': None},
                            [1e-14, 2e-14],
                            {'mask_bool': np.array([False, True])})
    assert ps.display_smoothed is None and ps.factor == 1
    assert ps.mask_bool.tolist() == [False, True]


def test_v22_parse_response_carries_errcol_verdict(client):
    """V-22/W-37：上传解析即回显 errcol_verdict ⇒ U-53 两选一在算之前给出。"""
    text = ('# wavelength_unit: angstrom\n'
            + '\n'.join(f'{4000 + i * 10} 1e-15 0' for i in range(12)))
    r = client.post('/api/specphot/parse', json={'text': text})
    assert r.status_code == 200
    assert _json(r)['errcol_verdict'] == 'all_zero'
    text_ok = ('# wavelength_unit: angstrom\n'
               + '\n'.join(f'{4000 + i * 10} 1e-15' for i in range(12)))
    r2 = client.post('/api/specphot/parse', json={'text': text_ok})
    assert _json(r2)['errcol_verdict'] == 'ok'


def test_v9_dup_lambda_sigma_zero_column_preserved():
    """reader 级直测（P1b 复审登记项）：重复 λ 组内 σ≤0 的列值保留进 ferr
    （不参与逆方差权重，但 all_zero/negative 判定域可见），不再被抹成"无列"。"""
    from specphot.reader import load_upload_text
    text = ('# wavelength_unit: angstrom\n'
            '4000 1.2e-15 0\n'          # 重复 λ 组首行，σ=0 ⇒ 走保留支（组内取首行原值）
            '4000 1e-15 1e-16\n'
            + '\n'.join(f'{4010 + i * 10} 1e-15 1e-16' for i in range(11)))
    ls = load_upload_text(text)
    ferr = ls['flux_err']
    assert ferr is not None and any(v is not None and float(v) == 0.0 for v in ferr), \
        'σ=0 的列值被读侧抹掉 ⇒ all_zero 判定域不可达（T-78②③ 失效）'
    assert ls['meta']['u_fluxes'] or ls['spec_hash']
