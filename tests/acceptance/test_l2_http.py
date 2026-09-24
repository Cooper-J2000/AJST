"""L2: HTTP 层冒烟（Flask test_client；不起端口、不写库、不联网）。

为什么用 test_client 而不是起临时端口：
  backend/app.py 有工厂函数 create_app()，test_client 直接跑完整路由栈
  （URL 映射 → 视图 → jsonify），测的正是"HTTP 契约"这一层，但没有端口冲突、
  不需要抢先起/停服务、失败时能直接看到 traceback。
  服务进程本身的健康由 scripts/preflight.sh 第 6 组负责，两者互补。

本文件只发 GET。写路径（/api/* 的 POST/PUT/DELETE）一律不碰——写路径要动生产库，
属人工/专项验收，不在冒烟范围内。覆盖范围按设计方案定为"降级版"：
**状态码 + JSON 形状 + 少量跨层不变量**，不做整库数值断言。

几条不变量是本文件的重点（都是踩过的坑的形态）：
  1. /api/* 一律返回可被严格解析的 JSON —— 历史上 closure 的 NaN/Infinity 会让
     浏览器 JSON.parse 直接失败（Flask jsonify 默认输出非法 JSON），故用
     parse_constant 钩子强校验，而不是只 json.loads（后者默认容忍 NaN）。
  2. 鉴权门的端点必须返回 401 JSON，而不是 500 或 HTML 登录页。
  3. 未登录、坏参数、不存在的 id 都必须是结构化的 JSON 错误（error 键）。
  4. /api/sed/closure_relations 必须与磁盘上的 backend/sedfit/closure_relations.json
     逐条一致 —— 防止接口缓存/另一份副本，把 L1 锁住的口径在 HTTP 层漏掉。
  5. /api/stats/overview 的 n_lightcurves / n_transients 必须等于库内实际行数。
  6. 全部 GET 端点扫描一遍，不允许出现 5xx。
"""
import json
import os

import pytest

from app import create_app

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CLOSURE_JSON = os.path.join(_REPO, 'backend', 'sedfit', 'closure_relations.json')


def _reject(const):
    raise AssertionError(
        f'响应含非法 JSON 常量 {const}：Flask jsonify 会把 NaN/Infinity 原样吐出来，'
        '浏览器 JSON.parse 会失败（历史上 closure 接口出过这个形态）'
    )


@pytest.fixture(scope='module')
def client():
    """TESTING=True：视图里未捕获的异常会直接抛进测试（带 traceback），
    而不是被吞成 500 —— 冒烟测试要的就是"炸得响"。"""
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _json(resp):
    assert resp.headers.get('Content-Type', '').startswith('application/json'), \
        f'{resp.request.path} 的 Content-Type={resp.headers.get("Content-Type")!r}，应为 JSON'
    return json.loads(resp.get_data(as_text=True), parse_constant=_reject)


# ── 公开读端点：状态码 + 顶层形状 ──────────────────────────────────────
# (路径, 顶层类型, 必备键)

PUBLIC_READS = [
    ('/api/transients?per_page=3',  dict, {'items', 'page', 'per_page', 'total'}),
    ('/api/transients/meta',        dict, {'items'}),
    ('/api/transients/tags',        dict, {'tags'}),
    ('/api/transients/sub_tags',    dict, {'sub_tags'}),
    ('/api/lightcurves?per_page=3', dict, {'items', 'total'}),
    ('/api/stats/overview',         dict, {'n_lightcurves', 'n_transients', 'n_bands'}),
    ('/api/stats/hosts',            dict, {'n_hosts', 'n_transients'}),
    ('/api/stats/bands',            list, None),
    ('/api/stats/redshifts',        dict, {'n', 'values'}),
    ('/api/stats/tags',             dict, {'tags'}),
    ('/api/filters',                list, None),
    ('/api/tags',                   list, None),
    ('/api/articles',               list, None),
    ('/api/spectra',                list, None),
    ('/api/sed/models',             dict, {'models', 'laws', 'defaults', 'series'}),
    ('/api/sed/closure_relations',  dict, {'_meta', 'relations'}),
    ('/api/relations',              dict, {'relations', 'sources'}),
    ('/api/hostfit/config',         dict, {'pcigale', 'prospector'}),
    ('/api/auth/status',            dict, {'authenticated'}),
    ('/api/extinction/status',      dict, {'available', 'dust_map'}),
    ('/api/gcn/status',             dict, {'count', 'latest_id'}),
    ('/api/gcn/ids',                dict, {'ids'}),
]


@pytest.mark.parametrize('path,kind,keys', PUBLIC_READS)
def test_public_read_endpoint(client, path, kind, keys):
    resp = client.get(path)
    assert resp.status_code == 200, f'{path} → {resp.status_code}'
    body = _json(resp)
    assert isinstance(body, kind), f'{path} 顶层应为 {kind.__name__}，实为 {type(body).__name__}'
    if keys:
        assert keys <= set(body), f'{path} 缺键 {keys - set(body)}'


def test_pagination_per_page_is_honoured(client):
    """per_page 是契约：给多少就最多回多少（防分页参数被静默忽略）"""
    for path, key in [('/api/transients?per_page=5', 'items'),
                      ('/api/lightcurves?per_page=7', 'items')]:
        body = _json(client.get(path))
        assert len(body[key]) <= int(path.split('per_page=')[1].split('&')[0])
        assert body['total'] >= len(body[key])
        assert body['total'] > 0, f'{path} 总数为 0 —— 库可能空了（preflight 也会报）'


# ── 鉴权门：未登录必须是 JSON 401 ──────────────────────────────────────

AUTH_GATED = [
    '/api/admin/users',
    '/api/export/transients',
    '/api/export/lightcurves/GRB221009A',
    '/api/export/host_photometry/GRB221009A',
    '/api/filters/pcigale_builtin',
    '/api/filters/svo_search?q=SDSS+r',
    '/api/ingest/resolve',
]


@pytest.mark.parametrize('path', AUTH_GATED)
def test_auth_gated_endpoint_returns_json_401(client, path):
    resp = client.get(path)
    assert resp.status_code == 401, f'{path} → {resp.status_code}（应为 401）'
    body = _json(resp)
    assert 'error' in body


# ── 错误形状：400 / 404 都必须是结构化 JSON ────────────────────────────

def test_missing_required_param_is_400(client):
    """/api/sed/epochs 不带 transient_id → 400 + error（不是 500）"""
    resp = client.get('/api/sed/epochs')
    assert resp.status_code == 400
    assert 'error' in _json(resp)


@pytest.mark.parametrize('path', [
    '/api/transients/__NO_SUCH_TRANSIENT__',
    '/api/hosts/__NO_SUCH_TRANSIENT__',
    '/api/spectra/99999999',
    '/api/sed/jobs/99999999',
    '/api/nope-not-a-route',
])
def test_unknown_resource_is_structured_404(client, path):
    resp = client.get(path)
    assert resp.status_code == 404, f'{path} → {resp.status_code}（应为 404）'
    assert 'error' in _json(resp), f'{path} 的 404 没有 error 键'


# ── 跨层不变量 ────────────────────────────────────────────────────────

def test_closure_relations_endpoint_matches_the_file(client):
    """HTTP 层必须与 L1 锁定的那份表逐条一致（防接口另有一份副本）"""
    served = {r['id']: r for r in _json(client.get('/api/sed/closure_relations'))['relations']}
    on_disk = {r['id']: r for r in json.load(open(_CLOSURE_JSON))['relations']}
    assert set(served) == set(on_disk)
    for rid, r in on_disk.items():
        assert served[rid]['alpha'] == r['alpha'], f'{rid} 的 alpha 系数不一致'
        assert served[rid]['beta'] == r['beta'], f'{rid} 的 beta 定义不一致'
        assert served[rid]['ref'] == r['ref'], f'{rid} 的 ref 不一致'


def test_stats_overview_counts_match_the_database(client, db_cur):
    """/api/stats/overview 是宿主统计的入口 —— 计数必须与库内实际行数一致"""
    db_cur.execute('SELECT count(*) AS n FROM lightcurves')
    n_lc = db_cur.fetchone()['n']
    db_cur.execute('SELECT count(*) AS n FROM transients')
    n_tr = db_cur.fetchone()['n']
    body = _json(client.get('/api/stats/overview'))
    assert body['n_lightcurves'] == n_lc, f"接口 {body['n_lightcurves']} ≠ 库内 {n_lc}"
    assert body['n_transients'] == n_tr, f"接口 {body['n_transients']} ≠ 库内 {n_tr}"


def test_transient_detail_round_trip(client):
    """列表里取一个真实 id → 取详情，字段与 id 必须对得上"""
    first = _json(client.get('/api/transients?per_page=1'))['items'][0]
    tid = first['id']
    detail = _json(client.get(f'/api/transients/{tid}'))
    assert detail['id'] == tid
    for k in ('ra', 'dec', 'lc_count'):
        assert k in detail, f'详情缺字段 {k}'
    assert detail['lc_count'] == first['lc_count']


def test_spectrum_detail_and_download_round_trip(client):
    """光谱详情 + 下载：详情是 JSON，下载是原始文件本体（非 JSON、非空）"""
    spectra = _json(client.get('/api/spectra'))
    if not spectra:
        pytest.skip('库里还没有光谱')
    sid = spectra[0]['id']
    detail = _json(client.get(f'/api/spectra/{sid}'))
    assert {'data', 'meta'} <= set(detail)

    dl = client.get(f'/api/spectra/{sid}/download')
    assert dl.status_code == 200
    assert dl.get_data(), '下载内容为空'
    assert not dl.headers.get('Content-Type', '').startswith('application/json'), \
        '下载接口不该返回 JSON（应回原始文本文件）'


# ── 全量 GET 扫描：任何端点都不许 5xx ─────────────────────────────────

_SUBS = {'<int:cid>': '1', '<int:spec_id>': '1', '<int:job_id>': '1',
         '<transient_id>': 'GRB221009A', '<tid>': 'GRB221009A',
         '<kind>': 'log', '<name>': 'closure_relations'}


def _all_get_paths(client):
    paths = []
    for rule in client.application.url_map.iter_rules():
        if sorted(rule.methods - {'HEAD', 'OPTIONS'}) != ['GET']:
            continue
        if rule.rule.startswith('/static') or '<path:filename>' in rule.rule:
            continue
        p = rule.rule
        for k, v in _SUBS.items():
            p = p.replace(k, v)
        paths.append((rule.rule, p))
    return paths


def test_no_get_endpoint_returns_5xx(client):
    """扫描全部只读端点（含鉴权门/不存在的 id），一个 5xx 都不许有。
    注意 TESTING=True 下未捕获异常会直接抛出，所以这条同时覆盖了'炸成 500'那种。"""
    bad = []
    for rule, path in _all_get_paths(client):
        st = client.get(path).status_code
        if st >= 500:
            bad.append((rule, path, st))
    assert not bad, f'有端点返回 5xx: {bad}'
    assert len(_all_get_paths(client)) >= 40, '只读端点少于 40 个，扫描范围可疑'


def test_api_error_paths_are_json_not_html(client):
    """2026-09 那次 /api/stats 打错路径返回的是 JSON 而不是 HTML —— 保持这个行为"""
    for path in ('/api/stats', '/api/definitely-not-here'):
        resp = client.get(path)
        assert resp.status_code == 404
        assert resp.headers.get('Content-Type', '').startswith('application/json')
        assert 'error' in json.loads(resp.get_data(as_text=True))
