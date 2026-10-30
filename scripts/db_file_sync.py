#!/usr/bin/env python3
"""库 ↔ 文件 双向同步：报告差异、执行同步；删源必须先登记 audit/denied.tsv。

为什么需要它：`backend/etl.py` 只有「文件 → 库」这一个方向，触发条件是**文件存在**。
于是有两种情况它既看不见也修不了：

  1. 仓库里被删掉的源（审核否证 → git rm）——库里如果还留着，网页端照旧可见；
  2. 合并进仓库但从未进库的审核数据——2026-10-30 实测：143 个源库里仍停在 bot
     最初导入的 1 行，而文件里已经有 56–116 行（含 #527 合并的 GRB240229A 30 行）。

用法
  python3 scripts/db_file_sync.py                    # 只读报告（默认）
  python3 scripts/db_file_sync.py --id GRB240229A    # 只看指定源
  python3 scripts/db_file_sync.py --apply            # 执行同步
  python3 scripts/db_file_sync.py --apply --force    # 放宽删除阈值

三类差异
  DB_ONLY    库里有、文件里没有 → 应删库。**必须已在 audit/denied.tsv 登记**，
             否则只报告不删：删源是维护者裁定，denied.tsv 那行就是裁定凭据。
  FILE_ONLY  文件里有、库里没有 → 应建库。若该源在 denied.tsv 里（文件被误恢复，
             见 Issue #183）→ 跳过并报警，绝不重建。
  DRIFT      两边都有但内容不同（info 关键字段 / lc 行内容）→ 从文件重建该源。

安全闸
  * 删除动作一律要求 denied.tsv 有条目；
  * 删除数 > 20 个或 > 库内源数 1% 时拒绝执行，需显式 --force；
  * --apply 先把将要删除/改写的源的库内内容导出到 backups/sync_<时间>/（可回滚）；
  * 建源前再查一次 denied（防止同步过程中名单被改动）。

退出码
  0 = 一致，或同步成功
  1 = 存在差异（report 模式）
  2 = 环境问题 / 安全闸拦截
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(PROJECT_ROOT, 'backend')
sys.path.insert(0, BACKEND)

try:                                    # 后端模块：库连接 + 模型 + ETL 函数
    import etl                                            # noqa: E402
    from app import get_session                            # noqa: E402
    from models import Lightcurve, Transient               # noqa: E402
    _IMPORT_ERR = None
except Exception as _e:                                    # noqa: BLE001
    _IMPORT_ERR = _e

# 比对用的 lc 列（值 + 上下文，不含派生消光列与 comment 长文本）
LC_KEYS = ('time', 'time_err', 'mjd', 'band', 'flux_density',
           'flux_density_err', 'flux_density_unit', 'upperlimit', 'telescope',
           'instrument', 'reference', 'discard')
# 比对用的 info 字段（文件键 → 模型属性）
INFO_KEYS = (('ra', 'ra'), ('dec', 'dec'), ('T0', 't0'), ('T0_ref', 't0_ref'),
             ('Trigger_Instrument', 'trigger_instrument'),
             ('pos_error', 'pos_error'), ('pos_error_confidence', 'pos_error_confidence'),
             ('pos_ref', 'pos_ref'), ('redshift', 'redshift'), ('tag', 'tags'),
             ('sub_tag', 'sub_tag'), ('alias', 'aliases'), ('comment', 'comment'))


def norm_num(v):
    """数值列归一化：文件是字符串、库是 float，统一成 6 位小数（≈0.09 ms/0.09 s 视量纲）。"""
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, (int, float)):
        return f'{float(v):.6f}'
    s = str(v).strip()
    if not s or s.lower() in ('null', 'none'):
        return ''
    try:
        return f'{float(s):.6f}'
    except ValueError:
        return s


def norm_flag(v):
    """y/n 布尔列（upperlimit / discard）。空值等价于 n——库里存 False、文件里留空都是「未设」。"""
    if v is None or isinstance(v, bool):
        return 'y' if v is True else 'n'
    s = str(v).strip().lower()
    if s in ('y', 'true', '1', 't'):
        return 'y'
    if s in ('n', 'false', '0', 'f', '', 'null', 'none'):
        return 'n'
    return s


def norm_text(v):
    if v is None:
        return ''
    s = str(v).strip()
    return '' if s.lower() in ('null', 'none') else s


def norm_t0_iso(v):
    """info 里的 T0 是 ISO 字符串，库里是 naive datetime（UTC），统一到同一形态。"""
    if not v:
        return ''
    s = str(v).strip().replace('Z', '')
    try:
        return datetime.fromisoformat(s).strftime('%Y-%m-%dT%H:%M:%S.%f')
    except ValueError:
        return s


def norm_list(v):
    if not v:
        return ''
    return '|'.join(sorted(str(x).strip() for x in v))


LC_NUM = {'mjd', 'flux_density', 'flux_density_err'}     # 高精度数值列
LC_FLAG = {'upperlimit', 'discard'}                       # y/n 布尔列
LC_TIME = {'time', 'time_err'}                            # 按物理时长（秒）带容差比
# ETL 入库时把 time 统一折算成秒（time_unit='s'），文件里保留原文单位（常见 d）。
# 比对必须按物理时长，否则「-1.297 d」与「-112061 s」会被当成漂移。
UNIT_SEC = {'s': 1.0, 'sec': 1.0, 'secs': 1.0, 'second': 1.0, 'seconds': 1.0,
            'min': 60.0, 'mins': 60.0, 'minute': 60.0, 'minutes': 60.0, 'm': 60.0,
            'h': 3600.0, 'hr': 3600.0, 'hrs': 3600.0, 'hour': 3600.0, 'hours': 3600.0,
            'd': 86400.0, 'day': 86400.0, 'days': 86400.0}
# 原文用「日」给时刻时会四舍五入到 6–7 位小数，换成秒后残留 1–50 ms 噪声（实测最大 43 ms）；
# 容差取 0.15 s，既压掉噪声，又能检出 ≥1 s 的真实重锚。
TIME_TOL = 0.15


def _to_sec(v, unit):
    """时间列 → 秒（float）；空值返回 None。"""
    txt = norm_text(v)
    if not txt:
        return None
    try:
        f = float(txt)
    except ValueError:
        return None
    return f * UNIT_SEC.get(norm_text(unit).lower(), 1.0)


def _row(row):
    """CSV 行 / 模型对象 → (身份键, time 秒, time_err 秒)。

    身份键 = 除 time/time_err 外全部比对列的归一化值，用来在两边做排序对齐；
    时间单独用数值容差比，避免 d↔s 折算噪声被误判成漂移。
    """
    unit = row.get('time_unit')
    rest = []
    for k in LC_KEYS:
        if k in LC_TIME:
            continue
        v = row.get(k)
        if k in LC_FLAG:
            rest.append(norm_flag(v))
        elif k in LC_NUM:
            rest.append(norm_num(v))
        else:
            rest.append(norm_text(v))
    return (tuple(rest), _to_sec(row.get('time'), unit), _to_sec(row.get('time_err'), unit))


def lc_rows_csv(path):
    with open(path, encoding='utf-8', newline='') as fh:
        return [_row(r) for r in csv.DictReader(fh)]


def lc_rows_db(sess, tid):
    rows = []
    for lc in sess.query(Lightcurve).filter(Lightcurve.transient_id == tid).all():
        d = {k: getattr(lc, k, None) for k in LC_KEYS}
        d['time_unit'] = getattr(lc, 'time_unit', None)
        rows.append(_row(d))
    return rows


def _sort_key(r):
    return (r[0], r[1] if r[1] is not None else 0.0)


def lc_diff(file_rows, db_rows, tol=TIME_TOL):
    """→ None 表示一致；否则返回一句差异描述。"""
    if len(file_rows) != len(db_rows):
        return f'行数 文件 {len(file_rows)} / 库 {len(db_rows)}'
    for i, (x, y) in enumerate(zip(sorted(file_rows, key=_sort_key),
                                   sorted(db_rows, key=_sort_key)), 1):
        if x[0] != y[0]:
            return f'第 {i} 行非时间列不同'
        for name, u, v in (('time', x[1], y[1]), ('time_err', x[2], y[2])):
            if (u is None) != (v is None):
                return f'第 {i} 行 {name} 一侧为空'
            if u is not None and abs(u - v) > tol:
                return f'第 {i} 行 {name} 差 {abs(u - v):.2f} s'
    return None


def info_fp_file(path):
    with open(path, encoding='utf-8') as fh:
        d = json.load(fh)
    out = {}
    for fk, _ in INFO_KEYS:
        v = d.get(fk)
        if fk == 'T0':
            out[fk] = norm_t0_iso(v)
        elif fk in ('tag', 'sub_tag', 'alias'):
            out[fk] = norm_list(v)
        elif fk in ('ra', 'dec', 'pos_error', 'redshift'):
            out[fk] = norm_num(v)
        else:
            out[fk] = norm_text(v)
    return out


def info_fp_db(t):
    out = {}
    for fk, attr in INFO_KEYS:
        v = getattr(t, attr, None)
        if fk == 'T0':
            out[fk] = norm_t0_iso(v.isoformat() if isinstance(v, datetime) else v)
        elif fk in ('tag', 'sub_tag', 'alias'):
            out[fk] = norm_list(v)
        elif fk in ('ra', 'dec', 'pos_error', 'redshift'):
            out[fk] = norm_num(v)
        else:
            out[fk] = norm_text(v)
    return out


def info_field_diffs(a, b):
    """返回差异字段名列表（a=文件，b=库）。"""
    return [k for k in a if a[k] != b.get(k)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='执行同步（默认只报告）')
    ap.add_argument('--force', action='store_true', help='放宽删除数阈值')
    ap.add_argument('--id', nargs='+', metavar='TID', help='只处理指定源')
    ap.add_argument('--max-delete', type=int, default=20, help='单次允许删除的源数上限（默认 20）')
    ap.add_argument('--quiet', action='store_true', help='只输出汇总')
    args = ap.parse_args()

    if _IMPORT_ERR is not None:
        print(f'[ERROR] 无法导入后端模块：{type(_IMPORT_ERR).__name__}: {_IMPORT_ERR}')
        return 2

    data_dir = etl.DATA_DIR
    if not os.path.isdir(data_dir):
        print(f'[ERROR] 数据目录不存在：{data_dir}（可用 AJST_DATA_DIR 指定）')
        return 2

    info_dir, lc_dir = os.path.join(data_dir, 'info'), os.path.join(data_dir, 'lc')
    denied = etl.denied_ids(refresh=True)

    file_ids = set()
    for d, ext in ((info_dir, '.json'), (lc_dir, '.csv')):
        if os.path.isdir(d):
            file_ids |= {f[:-len(ext)] for f in os.listdir(d) if f.endswith(ext)}
    if args.id:
        file_ids &= set(args.id)

    sess = get_session()
    try:
        db_transients = {t.id: t for t in sess.query(Transient).all()}
        db_ids = set(db_transients)
        if args.id:
            db_ids &= set(args.id)

        db_only = sorted(db_ids - file_ids)
        file_only = sorted(file_ids - db_ids)
        drift = []
        for tid in sorted(db_ids & file_ids):
            d = {}
            ip = os.path.join(info_dir, f'{tid}.json')
            if os.path.exists(ip):
                f = info_field_diffs(info_fp_file(ip), info_fp_db(db_transients[tid]))
                if f:
                    d['info'] = f
            lp = os.path.join(lc_dir, f'{tid}.csv')
            if os.path.exists(lp):
                why = lc_diff(lc_rows_csv(lp), lc_rows_db(sess, tid))
                if why:
                    d['lc'] = why
            if d:
                drift.append((tid, d))

        # ── 报告 ──
        print(f'=== 库 ↔ 文件 一致性（{datetime.now():%Y-%m-%d %H:%M}）===')
        print(f'文件源 {len(file_ids)} 个 / 库源 {len(db_ids)} 个 / denied {len(denied)} 条')
        print(f'\n[DB_ONLY ] 库里有、文件里没有：{len(db_only)} 个')
        for tid in db_only:
            why = 'denied ✓ 可删' if tid in denied else '**未登记 denied，不删**'
            print(f'    {tid}  ({why})')
        print(f'\n[FILE_ONLY] 文件里有、库里没有：{len(file_only)} 个')
        for tid in file_only:
            why = '**在 denied 里，拒绝重建**' if tid in denied else '可建'
            print(f'    {tid}  ({why})')
        print(f'\n[DRIFT   ] 两边都有但内容不同：{len(drift)} 个')
        for tid, d in drift:
            bits = []
            if 'info' in d:
                bits.append('info: ' + ','.join(d['info'][:6]) + ('' if len(d['info']) <= 6 else '…'))
            if 'lc' in d:
                bits.append('lc: ' + d['lc'])
            print(f'    {tid}  ' + '; '.join(bits))

        n_del = sum(1 for t in db_only if t in denied)
        blocked = [t for t in db_only if t not in denied]
        print(f'\n汇总：可删 {n_del}（已登记 denied）/ 待裁定 {len(blocked)} / '
              f'可建 {len([t for t in file_only if t not in denied])} / '
              f'拒绝重建 {len([t for t in file_only if t in denied])} / 需更新 {len(drift)}')

        if not args.apply:
            any_diff = db_only or file_only or drift
            return 1 if any_diff else 0

        # ── 安全闸 ──
        if n_del > args.max_delete or n_del > max(1, int(0.01 * len(db_ids))):
            if not args.force:
                print(f'\n[BLOCK] 本次要删 {n_del} 个源，超过阈值（--max-delete {args.max_delete}'
                      f' 或库内源数 1%）；确认无误后加 --force 重跑。')
                return 2

        # ── 备份 ──
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        bdir = os.path.join(PROJECT_ROOT, 'backups', f'sync_{stamp}')
        os.makedirs(bdir, exist_ok=True)
        touched = sorted({t for t in db_only if t in denied} |
                         {t for t, _ in drift} |
                         {t for t in file_only if t not in denied})
        for tid in touched:
            t = db_transients.get(tid)
            rec = {'id': tid}
            if t is not None:
                rec.update(info_fp_db(t))
                rec['t0'] = t.t0.isoformat() if t.t0 else None
                rec['aliases'] = t.aliases
                rec['tags'] = t.tags
                rec['sub_tags'] = t.sub_tag
                rec['comment'] = t.comment
            rec['lc'] = [dict(zip(LC_KEYS, row)) for row in lc_fp_db(sess, tid)]
            with open(os.path.join(bdir, f'{tid}.json'), 'w', encoding='utf-8') as fh:
                json.dump(rec, fh, ensure_ascii=False, indent=1)
        print(f'\n[OK] 库内将被改动的 {len(touched)} 个源已导出到 {bdir}')

        # ── 执行 ──
        deleted = built = updated = 0
        for tid in [t for t in db_only if t in denied]:
            t = db_transients[tid]
            sess.query(Lightcurve).filter(Lightcurve.transient_id == tid).delete()
            sess.delete(t)
            deleted += 1
            print(f'  [DEL ] {tid}（denied.tsv 登记）')

        sess.commit()

        for tid in file_only:
            if tid in denied:
                print(f'  [SKIP] {tid}: 在 denied.tsv（文件像是被误恢复，拒绝重建）')
                continue
            etl.import_one_transient(sess, tid)
            etl.import_one_lightcurve(sess, tid)
            built += 1
            print(f'  [NEW ] {tid}')

        for tid, _ in drift:
            if tid in denied:
                print(f'  [SKIP] {tid}: 在 denied.tsv（拒绝重建）')
                continue
            etl.import_one_transient(sess, tid)
            etl.import_one_lightcurve(sess, tid)
            updated += 1
            print(f'  [UPD ] {tid}')

        sess.commit()
        print(f'\n同步完成：删除 {deleted} / 新建 {built} / 更新 {updated}；备份见 {bdir}')
        if blocked:
            print(f'[!] 仍有 {len(blocked)} 个源「库里有、文件里没有」但未登记 denied，未处理：'
                  f'{blocked[:5]}{" …" if len(blocked) > 5 else ""}')
            print('    先按《数据审核提示词》§3.10 走删源流程（三处齐动）再重跑本脚本。')
        return 0
    finally:
        sess.close()


if __name__ == '__main__':
    sys.exit(main())
