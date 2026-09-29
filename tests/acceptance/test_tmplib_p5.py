"""模板库 × K 改正 P5 验收：API-14 元数据编辑 + API-2 的 T0 口径列。

锚点（全部先经实机复验再写进断言）：
  - at2017gfo.yaml 是出厂 manifest；API-14 直接改写（整文重写，历史由 AJST-Data
    的 git 承担），改写后 label 变、其余字段不动；
  - sn2006aj 是 first_point 零点；at2017gfo 是固定相对零点 + 库内对应源
    GRB170817A 的 t0；
  - 编辑校验与造模板向导同口径（Q-12 配对、U-22、F-18/F-47），全部先在
    tmplib.edit.validate_edit 上直调验证过。

写盘用例一律走 AJST_TMPLIB_DIR 指 tmp_path 的隔离库根（与 P2 同一纪律：
生产 tmplibrary 只允许真实 API 请求路径写）。编辑+重建用例真实走一遍
API-14（含建面，~2 s）；API-2 的 T0 列用例只读真实库。
"""
import json
import os
import re
import shutil
import sys

import pytest
import yaml

_BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

_ROOT = os.path.join(os.path.dirname(_BACKEND), 'catadata', 'tmplibrary')
_NO_ABS = re.compile(r'/home/|/Users/|/root/')   # T-30


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


def _login(c, role='admin', username='p5-acc'):
    with c.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = username
        s['role'] = role


def _logout(c):
    with c.session_transaction() as s:
        s.clear()


# ── tmp 库根：真实 API-5 造一个向导模板 + 拷一个带注释的出厂 manifest ──────

@pytest.fixture(scope='module')
def built(client, tmp_path_factory):
    """tmp 库根：API-5 造 EP250108a 模板 + 拷入 at2017gfo.yaml（带注释）及其
    索引条目。yield (root_path, 向导模板 id)。"""
    root = tmp_path_factory.mktemp('tmplib-p5')
    (root / 'templates').mkdir()
    (root / 'data' / 'raw').mkdir(parents=True)
    (root / 'data' / 'surfaces').mkdir(parents=True)
    shutil.copytree(os.path.join(_ROOT, 'data', 'filters'),
                    root / 'data' / 'filters')
    old = os.environ.get('AJST_TMPLIB_DIR')
    os.environ['AJST_TMPLIB_DIR'] = str(root)
    try:
        from tmplib import engine
        engine.reset_caches()
        _login(client)
        decl = {
            'id': 'p5acc-ep250108a', 'transient_id': 'EP250108a',
            'object_class': 'GRB afterglow',
            'redshift': {'value': 0.176, 'source': '库内 redshift_ref（P5 锚）'},
            'distance': {'kind': 'cosmological'},
            'rowset': 'raw', 'null_system_policy': 'drop',
            'require_min_bands': 2,
        }
        r = client.post('/api/tmplib/templates', json=decl)
        assert r.status_code == 200, r.get_data(as_text=True)[:500]
        tid = r.get_json()['id']
        # 带注释出厂 manifest：文件 + 索引条目（shadow entry，origin=shipped）
        shutil.copy2(os.path.join(_ROOT, 'templates', 'at2017gfo.yaml'),
                     root / 'templates' / 'at2017gfo.yaml')
        lib = json.loads((root / 'library.json').read_text(encoding='utf-8'))
        shipped_entry = json.loads(
            open(os.path.join(_ROOT, 'library.json'), encoding='utf-8')
            .read())['templates']['at2017gfo']
        lib['templates']['at2017gfo'] = shipped_entry
        (root / 'library.json').write_text(
            json.dumps(lib, ensure_ascii=False, indent=1), encoding='utf-8')
    finally:
        if old is None:
            os.environ.pop('AJST_TMPLIB_DIR', None)
        else:
            os.environ['AJST_TMPLIB_DIR'] = old
        from tmplib import engine
        engine.reset_caches()
    return root, tid


@pytest.fixture
def tmp_env(built):
    """请求期间把库根指到 tmp 根，函数结束即恢复（与 P2 同纪律）。"""
    root, _ = built
    old = os.environ.get('AJST_TMPLIB_DIR')
    os.environ['AJST_TMPLIB_DIR'] = str(root)
    yield root
    if old is None:
        os.environ.pop('AJST_TMPLIB_DIR', None)
    else:
        os.environ['AJST_TMPLIB_DIR'] = old
    from tmplib import engine
    engine.reset_caches()


# ── API-14：权限门 ─────────────────────────────────────────────────────

def test_edit_requires_auth(client, tmp_env):
    _logout(client)   # built fixture 登录过，显式登出保证匿名
    r = client.patch('/api/tmplib/templates/p5acc-ep250108a',
                     json={'label': 'x'})
    assert r.status_code == 401


def test_edit_requires_admin(client, tmp_env):
    _login(client, role='user', username='p5-nonadmin')
    try:
        r = client.patch('/api/tmplib/templates/p5acc-ep250108a',
                         json={'label': 'x'})
        assert r.status_code == 403
    finally:
        _logout(client)


# ── API-14：校验电池（同 Q-2/Q-12/U-22/F-18/F-47 口径；全部 400 零写盘） ──

def test_edit_validation_battery(client, tmp_env):
    _login(client)
    try:
        tid = 'p5acc-ep250108a'
        before = open(os.path.join(
            str(tmp_env), 'templates', f'{tid}.yaml'), encoding='utf-8').read()
        cases = [
            ({'redshift': {'value': -0.5}}, 'z<0'),
            ({'redshift': {'error': 0.001}}, 'error 无 kind（Q-12）'),
            ({'distance': {'kind': 'measured'}}, 'measured 无 mu/d_L（U-22）'),
            ({'validity': {'require_min_bands': 1}}, 'rmb<2（F-47）'),
            ({'validity': {'require_min_bands': 3, 'notes': ''}}, 'rmb>2 且清空 notes（F-47）'),
            ({'bands': ['g']}, '白名单外字段'),
            ({'epoch_zero': {'kind': 'column'}}, 'column 零点不走网页编辑'),
            ({'epoch_zero': {'unit': 'hour'}}, '非法 epoch_zero.unit'),
        ]
        for body, tag in cases:
            r = client.patch(f'/api/tmplib/templates/{tid}', json=body)
            assert r.status_code == 400, f'{tag}: {r.status_code} {r.get_json()}'
            assert r.get_json()['code'] == 'TL_DECLARE_MISSING', tag
        after = open(os.path.join(
            str(tmp_env), 'templates', f'{tid}.yaml'), encoding='utf-8').read()
        assert before == after          # 全部校验失败 ⇒ 一字节未写
    finally:
        _logout(client)


def test_edit_no_change_400(client, tmp_env):
    _login(client)
    try:
        tid = 'p5acc-ep250108a'
        cur = yaml.safe_load(open(os.path.join(
            str(tmp_env), 'templates', f'{tid}.yaml'), encoding='utf-8'))
        r = client.patch(f'/api/tmplib/templates/{tid}',
                         json={'label': cur['label']})
        assert r.status_code == 400
        assert r.get_json()['code'] == 'TL_INCONSISTENT_ARGS'
    finally:
        _logout(client)


# ── API-14：出厂 manifest 直接改写（rebuild=false 不建面） ──────────────────

def test_edit_direct_rewrite_shipped(client, tmp_env):
    _login(client)
    try:
        path = os.path.join(str(tmp_env), 'templates', 'at2017gfo.yaml')
        r = client.patch('/api/tmplib/templates/at2017gfo', json={
            'label': 'AT 2017gfo / GW170817（kilonova，直改写测试）',
            'rebuild': False})
        assert r.status_code == 200, r.get_data(as_text=True)[:500]
        body = r.get_json()
        assert body['code'] == 'TL_OK'
        assert body['rebuilt'] is False and body['state'] == 'stale'
        assert body['changed'] == ['label']
        assert _NO_ABS.search(r.get_data(as_text=True)) is None
        after = open(path, encoding='utf-8').read()
        data = yaml.safe_load(after)
        assert data['label'].endswith('直改写测试）')
        assert data['redshift']['value'] == pytest.approx(0.009783)  # 其余不动
        # 直接改写：不生成 templates/.bak 备份层
        assert not os.path.isdir(os.path.join(str(tmp_env), 'templates', '.bak'))
        # 索引 label 同步
        lib = json.loads(open(os.path.join(str(tmp_env), 'library.json'),
                              encoding='utf-8').read())
        assert lib['templates']['at2017gfo']['label'].endswith('直改写测试）')
        assert lib['templates']['at2017gfo']['state'] == 'stale'
        assert 'history' not in lib                    # 族谱已移除
    finally:
        _logout(client)


# ── API-14：编辑 + 立即重建（真实建面），预测跟随新 z ────────────────────────

def test_edit_and_rebuild_then_predict(client, tmp_env):
    _login(client)
    try:
        tid = 'p5acc-ep250108a'
        r = client.patch(f'/api/tmplib/templates/{tid}', json={
            'redshift': {'value': 0.18},
            'epoch_zero': {'source': 'EP250108a 触发时刻（P5 编辑锚）'},
            'citation_append': 'P5 验收追加引用',
            'rebuild': True})
        assert r.status_code == 200, r.get_data(as_text=True)[:600]
        body = r.get_json()
        assert body['code'] == 'TL_OK' and body['rebuilt'] is True
        assert body['state'] == 'fresh', body.get('stale_because')
        assert set(body['changed']) == {'redshift.value', 'epoch_zero.source',
                                        'citation_append'}
        data = yaml.safe_load(open(os.path.join(
            str(tmp_env), 'templates', f'{tid}.yaml'), encoding='utf-8'))
        assert data['redshift']['value'] == pytest.approx(0.18)
        assert data['photometry']['time']['epoch_zero']['source'] == \
            'EP250108a 触发时刻（P5 编辑锚）'
        assert data['provenance']['citations'][-1] == 'P5 验收追加引用'
        # 预测默认 z 跟随新声明（Q-4：不传 z = 模板自身 z）；波段取面实有节点
        rl = client.get('/api/tmplib/templates')
        bands = [t for t in rl.get_json()['templates']
                 if t['id'] == tid][0]['bands']
        r2 = client.post('/api/tmplib/predict',
                         json={'template_id': tid, 'band': bands[0],
                               'n_times': 8})
        assert r2.status_code == 200, r2.get_data(as_text=True)[:400]
        assert r2.get_json()['curve']['z'] == pytest.approx(0.18)
    finally:
        _logout(client)


def test_edit_lowz_guard(client, tmp_env):
    """z<0.02 + cosmological 无放行 ⇒ 400；勾选 allow_low_z 后放行（F-18）。"""
    _login(client)
    try:
        tid = 'p5acc-ep250108a'
        r = client.patch(f'/api/tmplib/templates/{tid}', json={
            'redshift': {'value': 0.01}, 'rebuild': False})
        assert r.status_code == 400
        assert 'distance.allow_low_z' in r.get_json().get('missing', [])
        r = client.patch(f'/api/tmplib/templates/{tid}', json={
            'redshift': {'value': 0.01},
            'distance': {'allow_low_z': True}, 'rebuild': False})
        assert r.status_code == 200, r.get_data(as_text=True)[:400]
        data = yaml.safe_load(open(os.path.join(
            str(tmp_env), 'templates', f'{tid}.yaml'), encoding='utf-8'))
        assert data['distance']['allow_low_z'] is True
        assert data['redshift']['value'] == pytest.approx(0.01)
    finally:
        _logout(client)


# ── API-2：T0 口径列（只读真实库） ─────────────────────────────────────────

def test_templates_list_t0_fields(client):
    r = client.get('/api/tmplib/templates')
    assert r.status_code == 200
    assert _NO_ABS.search(r.get_data(as_text=True)) is None
    tpls = {t['id']: t for t in r.get_json()['templates']}
    for tid, t in tpls.items():
        assert 'epoch_zero' in t and 'catalog_t0' in t and 'counterpart' in t, tid
        if t['epoch_zero'] is not None:
            assert t['epoch_zero']['kind'] in ('fixed', 'first_point', 'column')
    assert tpls['sn2006aj']['epoch_zero']['kind'] == 'first_point'
    assert tpls['at2017gfo']['epoch_zero']['kind'] == 'fixed'
    assert tpls['at2017gfo']['counterpart'] == 'GRB170817A'
    assert tpls['at2017gfo']['catalog_t0']            # GRB170817A 库内有 t0
