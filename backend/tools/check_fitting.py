#!/usr/bin/env python3
"""余辉拟合子系统端到端验证脚本。

流程：
  1. prepare_data(GRB201103B) 数据摘要
  2. config_schema / validate_config 合法与非法组合
  3. test_client 走 API 全流程：登录 → engines → 提交小规模拟合
     （tophat+ism、关 rvs/magnetar、extinction=smc、nsteps=400 nburn=200 npool=4）
     → 轮询 → 详情 → 三个产物文件 → DELETE
  4. 物理合理性检查：E_iso ∈ [1e50, 1e55]，p ∈ [2, 3]，chi2 有限

用法: python tools/check_fitting.py [--skip-fit]
"""
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRANSIENT = 'GRB201103B'
FIT_CONFIG = {
    'jet': 'tophat', 'medium': 'ism',
    'rvs_shock': False, 'magnetar': False,
    'extinction': 'smc',
    'sampler': {'nsteps': 400, 'nburn': 200, 'top_k': 10, 'npool': 4},
}
POLL_INTERVAL = 10
POLL_TIMEOUT = 1800  # 30 分钟上限

failures = []


def check(name, cond, detail=''):
    tag = 'PASS' if cond else 'FAIL'
    print(f'[{tag}] {name}' + (f'  {detail}' if detail else ''))
    if not cond:
        failures.append(name)


def main():
    skip_fit = '--skip-fit' in sys.argv

    # ── 1. prepare_data ──
    print('== 1. prepare_data ==')
    from fitting.jobs import prepare_data
    d = prepare_data(TRANSIENT)
    print(f"z={d['z']}  n_points={d['n_points']}  bands={len(d['bands'])}")
    for b in d['bands']:
        print(f"  {b['band']:8s} nu={b['nu']:.3e}  n={len(b['t'])} "
              f"(UL {sum(b['is_ul'])})")
    print('warnings:', d['warnings'])
    check('红移为 1.105', abs(d['z'] - 1.105) < 1e-9)
    check('数据点数 > 50', d['n_points'] > 50, f"n={d['n_points']}")
    check('多波段', len(d['bands']) >= 5)
    check('含上限点', any(any(b['is_ul']) for b in d['bands']))
    check('探测点流量为正', all(f > 0 for b in d['bands']
                              for f, ul in zip(b['f'], b['is_ul']) if not ul))
    check('上限点 f=0', all(f == 0 for b in d['bands']
                          for f, ul in zip(b['f'], b['is_ul']) if ul))

    # ── 2. schema / 校验 ──
    print('== 2. config_schema / validate_config ==')
    from fitting.engines import get_engine
    eng = get_engine('vegas_fs')
    check('引擎已注册', eng is not None)
    schema = eng.config_schema()
    json.dumps(schema)
    check('schema JSON 可序列化', True)
    check('schema 含默认先验', 'E_iso' in schema['default_config']['priors'])
    check('合法组合 tophat+ism+smc',
          eng.validate_config(dict(FIT_CONFIG)) == [])
    check('合法组合 powerlaw+wind+rvs+magnetar',
          eng.validate_config({'jet': 'powerlaw', 'medium': 'wind',
                               'rvs_shock': True, 'magnetar': True,
                               'extinction': 'lmc'}) == [])
    errs = eng.validate_config({'jet': 'tophat', 'medium': 'ism',
                                'priors': {'p_r': {'min': 2.1, 'max': 2.8,
                                                   'scale': 'linear'}}})
    check('rvs 参数塞进关 rvs 的配置 → 报错', len(errs) > 0, errs[0][:60])
    errs = eng.validate_config({'jet': 'tophat', 'medium': 'ism',
                                'priors': {'L0': {'min': -1, 'max': 1e50,
                                                  'scale': 'log'}}})
    check('log 先验 min<=0 → 报错', len(errs) > 0)
    # magnetar=true 但先验缺 L0：直接交给包内规则验证
    from VegasAfterglow import Fitter, ParamDef, Scale
    f = Fitter(z=1.0, lumi_dist=1e28, jet='tophat', medium='ism', magnetar=True)
    try:
        f.validate_parameters([ParamDef('E_iso', 1e51, 1e54, Scale.log)])
        check('magnetar 缺 L0 → 报错', False)
    except ValueError as e:
        check('magnetar 缺 L0 → 报错', 'L0' in str(e))

    # ── 3. API 全流程 ──
    print('== 3. API 路由 ==')
    from app import create_app
    app = create_app()
    client = app.test_client()

    r = client.get('/api/fitting/engines')
    check('GET /engines', r.status_code == 200)
    check('engines 含 vegas_fs',
          any(e['name'] == 'vegas_fs' for e in r.get_json()))

    r = client.post('/api/fitting/jobs', json={
        'transient_id': TRANSIENT, 'engine': 'vegas_fs', 'config': FIT_CONFIG})
    check('未登录提交 → 401', r.status_code == 401)

    with client.session_transaction() as s:
        s['authenticated'] = True
    r = client.post('/api/fitting/jobs', json={
        'transient_id': 'NO_SUCH_GRB', 'engine': 'vegas_fs', 'config': FIT_CONFIG})
    check('不存在的源 → 404', r.status_code == 404)
    r = client.post('/api/fitting/jobs', json={
        'transient_id': TRANSIENT, 'engine': 'vegas_fs',
        'config': {'jet': 'warp', 'medium': 'ism'}})
    check('非法 config → 400', r.status_code == 400)

    if skip_fit:
        print('== 4. 端到端拟合（--skip-fit 跳过） ==')
        _report_failures()
        return

    print('== 4. 端到端小规模拟合（分钟级，请耐心等待） ==')
    t0 = time.time()
    r = client.post('/api/fitting/jobs', json={
        'transient_id': TRANSIENT, 'engine': 'vegas_fs', 'config': FIT_CONFIG})
    check('提交任务', r.status_code == 200, r.get_data(as_text=True)[:200])
    if r.status_code != 200:
        _report_failures()
        sys.exit(1)
    job_id = r.get_json()['id']
    print(f'job_id = {job_id}，开始轮询…')

    status = None
    while time.time() - t0 < POLL_TIMEOUT:
        time.sleep(POLL_INTERVAL)
        r = client.get(f'/api/fitting/jobs/{job_id}')
        detail = r.get_json()
        status = detail.get('status')
        print(f'  [{time.time()-t0:6.0f}s] status={status}')
        if status in ('done', 'failed', 'interrupted'):
            break
    check('任务完成', status == 'done',
          f"status={status} error={detail.get('error')}")
    if status != 'done':
        _report_failures()
        sys.exit(1)

    # 详情字段
    check('详情含 config', detail.get('config', {}).get('jet') == 'tophat')
    check('详情含 files 链接', set(detail.get('files', {})) >=
          {'h5', 'corner', 'lc_model'}, str(detail.get('files')))
    check('warnings 字段存在', isinstance(detail.get('warnings'), list))

    # 物理合理性
    params = detail['parameters']
    e_iso = params['E_iso']['v']
    p_val = params['p']['v']
    chi2 = detail.get('chi2')
    print(f"E_iso = {e_iso:.3e} erg, p = {p_val:.3f}, chi2 = {chi2:.3g}, "
          f"dof = {detail.get('dof')}, runtime = {detail.get('runtime_s'):.1f}s")
    for k, v in params.items():
        print(f"  {k:10s} = {v['v']:.4g} ± {v['err']:.3g}")
    check('E_iso ∈ [1e50, 1e55]', 1e50 <= e_iso <= 1e55)
    check('p ∈ [2, 3]', 2.0 <= p_val <= 3.0)
    check('chi2 有限', chi2 is not None and math.isfinite(chi2))

    # 产物文件
    r = client.get(f'/api/fitting/jobs/{job_id}/files/h5')
    check('h5 下载', r.status_code == 200 and len(r.data) > 1000,
          f'{len(r.data)} bytes')
    r = client.get(f'/api/fitting/jobs/{job_id}/files/corner')
    check('corner.png', r.status_code == 200 and r.data[:4] == b'\x89PNG')
    r = client.get(f'/api/fitting/jobs/{job_id}/files/lc_model')
    check('lc_model.json', r.status_code == 200)
    lc = r.get_json()
    check('lc_model 结构', lc.get('unit') == 'mJy' and
          all({'band', 't', 'f_med', 'f_lo', 'f_hi'} <= set(b)
              for b in lc.get('bands', [])))
    check('lc_model 每波段 60 点', all(len(b['t']) == 60 for b in lc['bands']))
    check('lc_model 中值为正', all(f > 0 for b in lc['bands'] for f in b['f_med']))

    # 任务列表
    r = client.get(f'/api/fitting/jobs?transient_id={TRANSIENT}')
    check('任务列表含该任务', any(j['id'] == job_id for j in r.get_json()))

    # 删除（done 状态可删）
    r = client.delete(f'/api/fitting/jobs/{job_id}')
    check('DELETE 已完成的任务', r.status_code == 200, r.get_data(as_text=True))
    r = client.get(f'/api/fitting/jobs/{job_id}')
    check('删除后详情 → 404', r.status_code == 404)

    _report_failures()


def _report_failures():
    print()
    if failures:
        print(f'共 {len(failures)} 项失败: {failures}')
        sys.exit(1)
    print('全部检查通过')


if __name__ == '__main__':
    main()
