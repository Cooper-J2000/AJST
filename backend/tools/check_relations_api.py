#!/usr/bin/env python3
"""
relations API 冒烟验证（Flask test_client，不起服务）：
- GET /api/relations 返回 200，定义与来源统计齐全
- 各关系 source=best 点数合理，amati 打印前 2 个点人工检查
- 未知关系名返回 404
用法: python3 check_relations_api.py
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app

app = create_app()
c = app.test_client()

r = c.get('/api/relations')
assert r.status_code == 200, r.status_code
body = r.get_json()
defs = body['relations']
sources = body['sources']
print(f'GET /api/relations → 200, {len(defs)} 个关系')
for d in defs:
    print(f"  {d['name']:10s} 可用来源: {sources[d['name']]}")

print('\n各关系 source=best 点数:')
for d in defs:
    r = c.get(f"/api/relations/{d['name']}/data?source=best")
    assert r.status_code == 200, (d['name'], r.status_code)
    pts = r.get_json()['points']
    print(f"  {d['name']:10s} {len(pts)}")

r = c.get('/api/relations/amati/data?source=best')
pts = r.get_json()['points']
print('\namati best 前 2 个点:')
print(json.dumps(pts[:2], indent=1, ensure_ascii=False))

r = c.get('/api/relations/amati/data?source=konus_wind')
print(f"\namati source=konus_wind 点数: {len(r.get_json()['points'])}")

r = c.get('/api/relations/nonexistent/data')
print(f'未知关系 → {r.status_code} (期望 404)')
assert r.status_code == 404
print('\nOK')
