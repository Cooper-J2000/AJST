#!/usr/bin/env python3
"""
POST /api/relations/<name>/fit 验证（Flask test_client，不起服务）：
1. 真实数据：GET amati/data?source=best 取点 → POST fit，打印 I/II 两组结果
   （预期 II 型斜率约 0.3–0.6，σ_int 约 0.1–0.3 dex）
2. 合成数据：已知斜率 0.5 + 内禀散射 0.2 dex + 测量噪声，验证参数恢复
3. sigma_int=false、未知关系 404、N<4 insufficient 边界检查
用法: python3 check_relations_fit.py
"""
import json, os, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app

app = create_app()
c = app.test_client()

# ---------- 1. 真实 Amati 数据 ----------
r = c.get('/api/relations/amati/data?source=best')
assert r.status_code == 200, r.status_code
pts = r.get_json()['points']
print(f'amati best 点数: {len(pts)}')

r = c.post('/api/relations/amati/fit',
           json={'points': pts, 'sigma_int': True})
assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
body = r.get_json()
print(f"x_log = {body['x_log']}")
for g, res in body['groups'].items():
    if 'error' in res:
        print(f"  组 {g}: insufficient, N={res['N']}")
        continue
    print(f"  组 {g}: N={res['N']}  slope={res['slope']:.4f}±{res['slope_err']:.4f}"
          f"  intercept={res['intercept']:.4f}±{res['intercept_err']:.4f}"
          f"  cov_ab={res['cov_ab']:.4f}  sigma_int={res['sigma_int']:.4f}")

# ---------- 2. 合成数据恢复测试 ----------
rng = np.random.default_rng(42)
N_SYN = 80
TRUE_A, TRUE_B, TRUE_S = 0.5, -23.4, 0.2   # logEp = 0.5·logEiso - 23.4, σ_int=0.2 dex
logEiso = rng.uniform(50.0, 54.0, N_SYN)
logEp_true = TRUE_A * logEiso + TRUE_B + rng.normal(0, TRUE_S, N_SYN)
sx = rng.uniform(0.02, 0.08, N_SYN)        # log 空间测量误差
sy = rng.uniform(0.02, 0.08, N_SYN)
logEiso_obs = logEiso + rng.normal(0, sx)
logEp_obs = logEp_true + rng.normal(0, sy)
syn = []
for i in range(N_SYN):
    eiso, ep = 10 ** logEiso_obs[i], 10 ** logEp_obs[i]
    syn.append({
        'x': eiso, 'y': ep,
        # 线性空间对称误差（转 log 后近似对称）
        'xerr': eiso * np.log(10) * sx[i],
        'yerr': [ep * np.log(10) * sy[i] * 1.2, ep * np.log(10) * sy[i] * 0.8],
        'grb_type': 'II',
    })
r = c.post('/api/relations/amati/fit', json={'points': syn, 'sigma_int': True})
assert r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:300])
res = r.get_json()['groups']['II']
print(f'\n合成数据 (真值 a={TRUE_A}, b={TRUE_B}, σ_int={TRUE_S}):')
print(f"  恢复: slope={res['slope']:.4f}±{res['slope_err']:.4f}"
      f"  intercept={res['intercept']:.4f}±{res['intercept_err']:.4f}"
      f"  sigma_int={res['sigma_int']:.4f}  N={res['N']}")
assert abs(res['slope'] - TRUE_A) < 0.05, '斜率恢复偏差过大'
assert abs(res['intercept'] - TRUE_B) < 1.0, '截距恢复偏差过大'
assert abs(res['sigma_int'] - TRUE_S) < 0.05, 'σ_int 恢复偏差过大'
print('  参数恢复 OK')

# sigma_int=false
r = c.post('/api/relations/amati/fit', json={'points': syn, 'sigma_int': False})
res0 = r.get_json()['groups']['II']
print(f"\nsigma_int=false: slope={res0['slope']:.4f}  intercept={res0['intercept']:.4f}"
      f"  sigma_int={res0['sigma_int']}")
assert res0['sigma_int'] == 0.0

# ---------- 3. 边界检查 ----------
r = c.post('/api/relations/nonexistent/fit', json={'points': syn})
print(f'\n未知关系 → {r.status_code} (期望 404)')
assert r.status_code == 404

r = c.post('/api/relations/amati/fit', json={'points': syn[:3]})
print(f"N=3 → {json.dumps(r.get_json()['groups'], ensure_ascii=False)}")
assert all(g.get('error') == 'insufficient' for g in r.get_json()['groups'].values())

r = c.post('/api/relations/amati/fit', json={'points': [{'x': 1, 'y': 1}] * 5001})
print(f'5001 点 → {r.status_code} (期望 400)')
assert r.status_code == 400

# ep_alpha（x 线性轴）冒烟
r = c.get('/api/relations/ep_alpha/data?source=best')
pts_ea = r.get_json()['points']
r = c.post('/api/relations/ep_alpha/fit', json={'points': pts_ea, 'sigma_int': True})
b = r.get_json()
print(f"\nep_alpha (x_log={b['x_log']}): "
      f"{json.dumps(b['groups']['all'], ensure_ascii=False)}")

print('\nOK')
