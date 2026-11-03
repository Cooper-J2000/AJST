#!/usr/bin/env python3
"""
extra_data.derived → extra_data.params 迁移（params 数据层第一阶段）：
- 对每个 Transient，从 extra_data.derived.best 取各量写入平铺的 extra_data.params，
  结构 {"v":…, "err":…, "src":…}（src 沿用 best 记录的取值目录）
- spec_class（best 中为裸字符串）→ {"v": str, "src": "liang2023"}
- derived.grb_type → params.grb_type = {"v":…, "src":…}
- 只迁移 params_registry.json 中 status=active 的键；注册表外的键打印警告不迁移
- 不动 catalog_data / derived；幂等可重跑；写入时设 params_computed 为当天日期
用法:
  python3 migrate_params.py           # dry-run，只打印统计
  python3 migrate_params.py --apply   # 写入数据库
  python3 migrate_params.py --purge   # dry-run：统计将删除 catalog_data/derived 的源
  python3 migrate_params.py --purge --apply  # 实际清库（有 derived 但无 params 时拒绝执行）
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import get_session
from models import Transient

REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'params_registry.json',
)

# 预期覆盖基准（第一阶段验收锚点；dry-run 逐键对照）
EXPECTED_COV = {
    't90_rest': 571, 't90_obs': 528, 'alpha': 476, 'ep_rest': 425,
    'eiso': 412, 'epeak_obs': 399, 'lp_iso': 389, 'variability': 216,
    'spec_class': 112, 'tlag_rest': 55, 'e_gamma': 46,
}
EXPECTED_DERIVED_SOURCES = 603  # 库中有 derived 的源总数


def load_registry():
    with open(REGISTRY_PATH, encoding='utf-8') as f:
        return json.load(f)['params']


def migrate_one(derived, active, unknown_keys, dirty):
    """从 derived 结构生成 params 命名空间；无可迁移量时返回 None。
    unknown_keys/dirty 分别累计注册表外的键与脏数据（值类型不符）。"""
    params = {}
    for key, obj in (derived.get('best') or {}).items():
        meta = active.get(key)
        if meta is None:
            unknown_keys[key] += 1
            continue
        if meta['type'] == 'string':
            # best 中字符串量是裸字符串（spec_class）；兼容 dict 形态
            v = obj if isinstance(obj, str) else (
                obj.get('v') if isinstance(obj, dict) else None)
            if not isinstance(v, str):
                dirty.append((key, obj))
                continue
            src = obj.get('src') if isinstance(obj, dict) else None
            params[key] = {'v': v, 'src': src or 'liang2023'}
        else:
            if isinstance(obj, dict) and isinstance(obj.get('v'), (int, float)):
                params[key] = {'v': obj['v'], 'err': obj.get('err'),
                               'src': obj.get('src')}
            else:
                dirty.append((key, obj))

    gt = derived.get('grb_type')
    if 'grb_type' in active:
        if isinstance(gt, dict) and isinstance(gt.get('v'), str):
            params['grb_type'] = {'v': gt['v'], 'src': gt.get('src')}
        elif gt is not None:
            dirty.append(('grb_type', gt))
    return params or None


def main():
    apply = '--apply' in sys.argv

    if '--purge' in sys.argv:
        # --purge 模式：删除 extra_data 的 catalog_data/derived 键（params 迁移
        # 已全链验收后执行）。dry-run 默认，--purge --apply 才写库。
        sess = get_session()
        targets = [t for t in sess.query(Transient).all()
                   if (t.extra_data or {}).get('catalog_data')
                   or (t.extra_data or {}).get('derived')]
        n_cd = sum(1 for t in targets if (t.extra_data or {}).get('catalog_data'))
        n_dv = sum(1 for t in targets if (t.extra_data or {}).get('derived'))
        # 安全闸：有 derived 但无 params 的源（迁移漏网）必须为零才允许 --apply
        orphans = [t.id for t in targets
                   if (t.extra_data or {}).get('derived')
                   and not (t.extra_data or {}).get('params')]
        print(f'将删除 catalog_data 的源: {n_cd}，将删除 derived 的源: {n_dv}')
        print(f'有 derived 但无 params 的漏网源: {len(orphans)}'
              + (f' {orphans[:10]}...' if len(orphans) > 10 else f' {orphans}' if orphans else ''))
        if not apply:
            print('\n(dry-run，未写库。加 --purge --apply 执行)')
            sess.close()
            return
        if orphans:
            print('\n存在漏网源，拒绝执行。先补跑迁移。')
            sess.close()
            return
        for t in targets:
            ed = dict(t.extra_data or {})
            ed.pop('catalog_data', None)
            ed.pop('derived', None)
            t.extra_data = ed
        sess.commit()
        sess.close()
        print(f'\n已清库: {len(targets)} 个源删除 catalog_data/derived 键')
        return

    registry = load_registry()
    active = {k: v for k, v in registry.items() if v.get('status') == 'active'}

    sess = get_session()
    transients = sess.query(Transient).all()

    n_derived = 0       # 有 derived 的源数
    n_done = 0          # 实际迁移（有可迁移量）的源数
    cov = defaultdict(int)
    unknown_keys = defaultdict(int)
    dirty = []
    gt_dist = defaultdict(int)
    params_map = {}
    samples = []        # 抽样迁移前后对照
    for t in transients:
        derived = (t.extra_data or {}).get('derived')
        if not derived:
            continue
        n_derived += 1
        g = derived.get('grb_type')
        gt_dist[g['v'] if isinstance(g, dict) else 'unknown'] += 1
        params = migrate_one(derived, active, unknown_keys, dirty)
        if params is None:
            continue
        n_done += 1
        params_map[t.id] = params
        for key in params:
            cov[key] += 1
        if len(samples) < 3:
            samples.append((t.id, derived.get('best') or {}, params))

    print(f'有 derived 的源数: {n_derived}/{len(transients)}'
          f'（预期 {EXPECTED_DERIVED_SOURCES}）')
    print(f'可迁移源数: {n_done}')
    print(f'grb_type 分布: {dict(gt_dist)}')

    print('\n逐键覆盖与预期基准对照:')
    for key in sorted(EXPECTED_COV, key=EXPECTED_COV.get, reverse=True):
        exp = EXPECTED_COV[key]
        got = cov.get(key, 0)
        flag = 'OK' if got == exp else f'!! 差 {got - exp:+d}'
        print(f'  {key:12s} {got:5d} / 预期 {exp:5d}  {flag}')
    extra = sorted(set(cov) - set(EXPECTED_COV))
    for key in extra:  # 基准未列的键（如 grb_type）只打印计数
        print(f'  {key:12s} {cov[key]:5d} / （基准未列）')

    if unknown_keys:
        print('\n警告：derived.best 中出现注册表外的键（未迁移）:')
        for key, n in sorted(unknown_keys.items()):
            print(f'  {key:12s} 出现 {n} 次')
    if dirty:
        print(f'\n警告：脏数据（值类型不符，未迁移）共 {len(dirty)} 条:')
        for key, obj in dirty[:10]:
            print(f'  {key}: {obj!r}')

    print('\n抽样迁移前后对照（3 个源）:')
    for name, best, params in samples:
        print(f'  {name}:')
        print(f'    前 derived.best: {json.dumps(best, ensure_ascii=False)}')
        print(f'    后 params:       {json.dumps(params, ensure_ascii=False)}')

    if not apply:
        print('\n(dry-run，未写库。加 --apply 执行)')
        sess.close()
        return

    today = date.today().isoformat()
    for t in transients:
        if t.id not in params_map:
            continue
        ed = dict(t.extra_data or {})
        ed['params'] = params_map[t.id]
        ed['params_computed'] = today
        t.extra_data = ed
    sess.commit()
    sess.close()
    print(f'\n已写库: {n_done} 个源的 extra_data.params（params_computed={today}）')


if __name__ == '__main__':
    main()
