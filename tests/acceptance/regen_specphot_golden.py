"""specphot golden 基线重建脚本（非 pytest 收集件，文件名不以 test_ 开头）。

用途：每个期次（P1b/P2/…）收口、数值口径冻结时，对**当期代码**实跑四个
golden 请求并重写 tests/acceptance/golden_specphot_p1/*.json，作为下一期
恒等判据（T-75① 同族）的比较基准。

用法（在代码仓库根执行）：
  cd <代码仓库根>
  set -a && source ~/.config/ajst.env && set +a
  AJST_PYTHON=<conda env>/bin/python tests/acceptance/regen_specphot_golden.py

注意：重建前必须确认当期全部 T-* 绿；重建后跑 run_all.sh -k specphot 验证
基线测试自洽。只读计算，不写库、不落盘（golden JSON 除外）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, 'backend')))
from app import create_app  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'golden_specphot_p1')

CASES = ('upload_direct', 'cat83_anchored', 'cat112_anchored', 'cat116_anchored')


def _login(c):
    with c.session_transaction() as s:
        s['authenticated'] = True
        s['username'] = 'golden-regen'
        s['role'] = 'user'


def main():
    sys.path.insert(0, HERE)
    from test_l1_specphot_p1b_baseline import _golden_requests   # 请求真源在基线测试
    app = create_app()
    app.config['TESTING'] = True
    c = app.test_client()
    _login(c)
    os.makedirs(OUT, exist_ok=True)
    bodies = {name: body for name, (body, _k, _s) in _golden_requests().items()}
    for name in CASES:
        r = c.post('/api/specphot/photometry', json=bodies[name])
        assert r.status_code == 200, f'{name}: {r.status_code} {r.get_data(as_text=True)[:200]}'
        with open(os.path.join(OUT, name + '.json'), 'w', encoding='utf-8') as f:
            json.dump(r.get_json(), f, ensure_ascii=False, sort_keys=True)
        print(f'regenerated {name}.json')
    print('done — 记得跑 run_all.sh -k specphot 验证基线自洽')


if __name__ == '__main__':
    main()
