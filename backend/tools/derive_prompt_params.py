#!/usr/bin/env python3
"""
静止系派生量计算：
- 从 extra_data.catalog_data（各目录 params）+ Transient.redshift 计算统一派生量
- 结果写入 extra_data.derived（不动 catalog_data），幂等可重跑
- 网页端手动修改受保护：带 "manual": true 的条目在重跑时保留，
  "_manual_deleted" 记录的被删路径不会被重新算出（见 preserve_manual）
用法:
  python3 derive_prompt_params.py          # dry-run，只打印统计
  python3 derive_prompt_params.py --apply  # 写入数据库
"""
import os, sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import get_session
from models import Transient

# best 取值优先级（越靠前越优先，缺失顺延）
BEST_PRIORITY = {
    'ep_rest':     ['konus_wind', 'liang2023', 'wang2022', 'minaev2020',
                    'fermi_gbm', 'swift_bat', 'batse'],
    'eiso':        ['konus_wind', 'liang2023', 'wang2022', 'minaev2020',
                    'fermi_lat', 'heasarc_grbcat'],
    'lp_iso':      ['konus_wind', 'liang2023', 'guidorzi2025'],
    'tlag_rest':   ['konus_wind'],
    'variability': ['guidorzi2025'],
    'e_gamma':     ['konus_wind'],
    't90_rest':    ['fermi_gbm', 'swift_bat', 'liang2023', 'konus_wind',
                    'batse', 'minaev2020'],
    'alpha':       ['liang2023', 'fermi_gbm', 'swift_bat', 'batse'],
    # 说明未给 epeak_obs 优先级，为 ep_alpha 关系补充（按 epeak 覆盖数排序）
    'epeak_obs':   ['konus_wind', 'fermi_gbm', 'liang2023', 'swift_grb',
                    'batse', 'agile_mcal', 'swift_bat'],
    # 说明未给 t90_obs 优先级，为 grb_type 的 t90 判别补充（观测系 t90 多的目录在前）
    't90_obs':     ['fermi_gbm', 'swift_bat', 'swift_grb', 'liang2023',
                    'batse', 'agile_mcal', 'uvot_grb', 'heasarc_grbcat'],
}


def scale_err(err, f):
    """误差同比缩放（单值或 [正,负] 原样保留结构）"""
    if err is None:
        return None
    if isinstance(err, list):
        return [e * f if isinstance(e, (int, float)) else None for e in err]
    return err * f if isinstance(err, (int, float)) else None


def _num(params, key):
    """取数值型参数对象（要求 v 为数值）"""
    o = params.get(key)
    if isinstance(o, dict) and isinstance(o.get('v'), (int, float)):
        return o
    return None


def derive_catalog(params, z):
    """单目录派生量。z 为 Transient.redshift（无红移则跳过需 z 的换算）"""
    d = {}
    has_z = z is not None and -1 < z < 20

    # ep_rest：优先文献发表值，否则 epeak(obs)×(1+z)
    ep = _num(params, 'ep_rest')
    eo = _num(params, 'epeak')
    if ep:
        d['ep_rest'] = {'v': ep['v'], 'err': ep.get('err')}
    elif eo and has_z:
        d['ep_rest'] = {'v': eo['v'] * (1 + z), 'err': scale_err(eo.get('err'), 1 + z)}

    # 直接拷贝的静止系/观测量
    for key in ('eiso', 'lp_iso', 'e_gamma', 'variability', 'alpha'):
        o = _num(params, key)
        if o:
            d[key] = {'v': o['v'], 'err': o.get('err')}

    # epeak_obs：观测系峰值能量原样拷贝
    if eo and eo.get('frame') != 'rest':
        d['epeak_obs'] = {'v': eo['v'], 'err': eo.get('err')}

    # t90：frame:"rest" 直接作 t90_rest；否则记 t90_obs 并有 z 时换算 t90_rest
    t90 = _num(params, 't90')
    if t90:
        if t90.get('frame') == 'rest':
            d['t90_rest'] = {'v': t90['v'], 'err': t90.get('err')}
        else:
            d['t90_obs'] = {'v': t90['v'], 'err': t90.get('err')}
            if has_z:
                f = 1.0 / (1 + z)
                d['t90_rest'] = {'v': t90['v'] * f, 'err': scale_err(t90.get('err'), f)}

    # tlag：frame:"rest" 直接用；obs 有 z 时 /(1+z)
    tl = _num(params, 'tlag')
    if tl:
        if tl.get('frame') == 'rest':
            d['tlag_rest'] = {'v': tl['v'], 'err': tl.get('err')}
        elif has_z:
            f = 1.0 / (1 + z)
            d['tlag_rest'] = {'v': tl['v'] * f, 'err': scale_err(tl.get('err'), f)}

    # 字符串分类原样拷贝
    for key in ('spec_class', 'grb_type'):
        o = params.get(key)
        if isinstance(o, dict) and isinstance(o.get('v'), str):
            d[key] = o['v']
    return d


def compute_derived(t):
    """计算单个 Transient 的 derived 结构；无可计算量时返回 None"""
    cd = (t.extra_data or {}).get('catalog_data')
    if not cd:
        return None
    z = t.redshift
    sources = {}
    for cat, entry in cd.items():
        d = derive_catalog(entry.get('params') or {}, z)
        if d:
            sources[cat] = d
    if not sources:
        return None

    # best：按优先级取各量
    best = {}
    for key, prio in BEST_PRIORITY.items():
        for cat in prio:
            q = sources.get(cat, {}).get(key)
            if q is not None:
                best[key] = {'v': q['v'], 'err': q.get('err'), 'src': cat}
                break
    # spec_class 进 best（仅 liang2023 有）
    for cat in ('liang2023',):
        sc = sources.get(cat, {}).get('spec_class')
        if sc:
            best['spec_class'] = sc
            break

    # grb_type：目录参数 > sub_tag > best t90_obs 2s 分界
    gt = gs = None
    for cat in ('minaev2020', 'liang2023'):
        v = sources.get(cat, {}).get('grb_type')
        if v in ('I', 'II'):
            gt, gs = v, cat
            break
    if gt is None:
        st = t.sub_tag or []
        if 'S' in st:
            gt, gs = 'I', 'sub_tag'
        elif 'L' in st:
            gt, gs = 'II', 'sub_tag'
    if gt is None and best.get('t90_obs'):
        gt = 'I' if best['t90_obs']['v'] < 2 else 'II'
        gs = f"t90_obs:{best['t90_obs']['src']}"

    derived = {'sources': sources, 'best': best, 'computed': date.today().isoformat()}
    if gt:
        derived['grb_type'] = {'v': gt, 'src': gs}
    return derived


def _is_manual(obj):
    """手动标记条目（网页端编辑/新增的量会带 "manual": true）"""
    return isinstance(obj, dict) and obj.get('manual') is True


def _has_manual(derived):
    """旧 derived 是否含任何手动标记条目"""
    if _is_manual(derived.get('grb_type')):
        return True
    for obj in (derived.get('best') or {}).values():
        if _is_manual(obj):
            return True
    for quants in (derived.get('sources') or {}).values():
        if isinstance(quants, dict) and any(_is_manual(o) for o in quants.values()):
            return True
    return False


def preserve_manual(new, old):
    """把 old derived 中的手动修改合并进新计算的 new：
    - 带 "manual": true 的条目（best.<k>、sources.<cat>.<k>、grb_type）覆盖计算值
    - old['_manual_deleted'] 记录的被删路径（如 'best.ep_rest'、'sources.kw.eiso'、
      'grb_type'、'sources.<cat>'）在 new 中同样删除，并保留墓碑供下次重跑
    同一路径手动条目与墓碑冲突时，手动条目优先（墓碑丢弃）。"""
    if not old:
        return new, 0, 0
    n_kept = 0
    deleted = set(old.get('_manual_deleted') or [])

    # 1) 手动条目优先保留
    for k, obj in (old.get('best') or {}).items():
        if _is_manual(obj):
            new.setdefault('best', {})[k] = obj
            deleted.discard(f'best.{k}')
            n_kept += 1
    for cat, quants in (old.get('sources') or {}).items():
        if not isinstance(quants, dict):
            continue
        for k, obj in quants.items():
            if _is_manual(obj):
                new.setdefault('sources', {}).setdefault(cat, {})[k] = obj
                deleted.discard(f'sources.{cat}.{k}')
                n_kept += 1
    if _is_manual(old.get('grb_type')):
        new['grb_type'] = old['grb_type']
        deleted.discard('grb_type')
        n_kept += 1

    # 2) 应用删除墓碑
    kept_tombstones = []
    for path in sorted(deleted):
        parts = path.split('.')
        if parts[0] == 'best' and len(parts) == 2:
            new.get('best', {}).pop(parts[1], None)
            kept_tombstones.append(path)
        elif parts[0] == 'grb_type':
            new.pop('grb_type', None)
            kept_tombstones.append(path)
        elif parts[0] == 'sources' and len(parts) == 2:
            new.get('sources', {}).pop(parts[1], None)
            kept_tombstones.append(path)
        elif parts[0] == 'sources' and len(parts) == 3:
            quants = new.get('sources', {}).get(parts[1])
            if isinstance(quants, dict):
                quants.pop(parts[2], None)
                if not quants:
                    new['sources'].pop(parts[1], None)
            kept_tombstones.append(path)
    if kept_tombstones:
        new['_manual_deleted'] = kept_tombstones
    return new, n_kept, len(kept_tombstones)


def main():
    apply = '--apply' in sys.argv
    sess = get_session()
    transients = sess.query(Transient).all()

    n_done = 0
    n_manual = 0
    n_tomb = 0
    best_cov = defaultdict(int)
    gt_dist = defaultdict(int)
    derived_map = {}
    for t in transients:
        d = compute_derived(t)
        old = (t.extra_data or {}).get('derived')
        if d is None:
            # 无可计算量：仅当旧 derived 含手动标记时才写（保住手动内容）
            if not (old and (old.get('_manual_deleted') or _has_manual(old))):
                continue
            d = {'sources': {}, 'best': {}, 'computed': date.today().isoformat()}
        d, k, b = preserve_manual(d, old)
        n_manual += k
        n_tomb += b
        n_done += 1
        derived_map[t.id] = d
        for key in d['best']:
            best_cov[key] += 1
        g = d.get('grb_type')
        gt_dist[g['v'] if g else 'unknown'] += 1

    print(f'处理源数（有 catalog_data 且算出量）: {n_done}/{len(transients)}')
    print(f'保留手动条目: {n_manual}, 删除墓碑: {n_tomb}')
    print('\nbest 各量覆盖:')
    for key in sorted(best_cov, key=best_cov.get, reverse=True):
        print(f'  {key:12s} {best_cov[key]}')
    print(f'\ngrb_type 分布: {dict(gt_dist)}')

    if not apply:
        print('\n(dry-run，未写库。加 --apply 执行)')
        sess.close()
        return

    for t in transients:
        if t.id not in derived_map:
            continue
        ed = dict(t.extra_data or {})
        ed['derived'] = derived_map[t.id]
        t.extra_data = ed
    sess.commit()
    sess.close()
    print(f'\n已写库: {n_done} 个源的 extra_data.derived')


main()
