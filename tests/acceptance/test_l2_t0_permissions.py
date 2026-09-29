"""L2: T0 写权限契约（伪造 session，不写库）。

口径（v2.30 起锁定）：
  - 库中已存在源的 T0（及一切事件字段）的修改 = PUT /api/transients/<tid>，
    仅管理员；普通用户 403，未登录 401。
  - POST /api/transients 新建源时普通登录用户可设定 T0 —— 这是"创建"不是
    "修改已存在的 T0"，保持开放；但重复 id 一律 409，普通用户没有经新建
    路径覆盖已有源（进而改写其 T0）的旁路。

全部用例在鉴权门/冲突检查处就被拦下或放行到 404/400，没有任何一条会
真正写库（admin PUT 用不存在的 id 验证"门后可达"）。
"""
import json
import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, 'backend'))

from app import create_app  # noqa: E402


@pytest.fixture(scope='module')
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _login(client, role):
    with client.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'contract-tester'
        s['role'] = role


def _logout(client):
    with client.session_transaction() as s:
        s.clear()


def _real_tid(client):
    body = json.loads(client.get('/api/transients?per_page=1').get_data(as_text=True))
    return body['items'][0]['id']


def test_unauthenticated_put_t0_is_401(client):
    tid = _real_tid(client)
    resp = client.put(f'/api/transients/{tid}', json={'t0': '2020-01-01T00:00:00'})
    assert resp.status_code == 401


def test_unauthenticated_post_is_401(client):
    resp = client.post('/api/transients', json={'id': 'ZZnope1', 't0': '2020-01-01T00:00:00'})
    assert resp.status_code == 401


def test_normal_user_put_t0_is_403(client):
    """普通用户不可修改库中已存在的 T0（v2.30 锁定的核心契约）"""
    _login(client, 'user')
    try:
        tid = _real_tid(client)
        resp = client.put(f'/api/transients/{tid}', json={'t0': '2020-01-01T00:00:00'})
        assert resp.status_code == 403
    finally:
        _logout(client)


def test_normal_user_cannot_overwrite_existing_via_post(client):
    """POST 对重复 id 必须 409 —— 否则普通用户可经新建路径改写已有源的 T0"""
    _login(client, 'user')
    try:
        tid = _real_tid(client)
        resp = client.post('/api/transients', json={'id': tid, 't0': '2020-01-01T00:00:00'})
        assert resp.status_code == 409, f'重复 id 新建应 409，实为 {resp.status_code}'
    finally:
        _logout(client)


def test_normal_user_post_passes_auth_gate(client):
    """普通登录用户允许走新建路径（鉴权门放行）：坏请求体应得 400 而非 401/403"""
    _login(client, 'user')
    try:
        resp = client.post('/api/transients', json={'t0': '2020-01-01T00:00:00'})  # 缺 id
        assert resp.status_code == 400
        assert 'error' in json.loads(resp.get_data(as_text=True))
    finally:
        _logout(client)


def test_admin_put_passes_auth_gate(client):
    """admin 经 PUT 改 T0 的通道存在：不存在的 id 应得 404（门后），而非 401/403"""
    _login(client, 'admin')
    try:
        resp = client.put('/api/transients/__NO_SUCH_TRANSIENT__',
                          json={'t0': '2020-01-01T00:00:00'})
        assert resp.status_code == 404
    finally:
        _logout(client)
