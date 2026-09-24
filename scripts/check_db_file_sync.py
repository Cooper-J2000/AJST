#!/usr/bin/env python3
"""只读检查：数据库 ↔ catadata/ 文件 的一致性（孤儿文件 / 陈旧 CSV）。

回答一个问题：**下一次 etl.py（全量重建）或 --sync 会不会把已经删掉的源/光变点灌回库？**

  1. 孤儿文件：info/lc 里有 `<id>.json` / `<id>.csv`，但库里没有这个源
     → 全量重建或 --sync 会把它当"新源"重新导入（etl.needs_update 对库里不存在的源返回 True）。
  2. 陈旧 CSV：库里有该源但它的光变点已为 0，而 CSV 里还有数据行
     → etl.from_dump 的 `if lcs:` 会跳过写文件，旧 CSV 留在盘上，重建时被灌回。

本脚本**只读**：不写库、不改文件。清理用 `python3 backend/etl.py --dump --prune`（可逆）。

用法:
    scripts/check_db_file_sync.py [--quiet]

退出码: 0 = 一致；1 = 发现差异；2 = 环境问题（解释器/库/数据目录不可用）
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(PROJECT_ROOT, 'backend')
sys.path.insert(0, BACKEND)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--quiet', action='store_true', help='只输出汇总行')
    args = ap.parse_args()

    try:
        import etl
        from app import get_session
        from models import Transient, Lightcurve
        from sqlalchemy import func
    except Exception as e:                      # noqa: BLE001
        print(f'[ERROR] 无法导入后端模块：{type(e).__name__}: {e}')
        return 2

    data_dir = etl.DATA_DIR
    if not os.path.isdir(data_dir):
        print(f'[ERROR] 数据目录不存在：{data_dir}（可用 AJST_DATA_DIR 指定）')
        return 2

    sess = get_session()
    try:
        try:
            db_ids = {tid for (tid,) in sess.query(Transient.id).all()}
            rows = dict(sess.query(Lightcurve.transient_id, func.count(Lightcurve.id))
                        .group_by(Lightcurve.transient_id).all())
        except Exception as e:                  # noqa: BLE001
            print(f'[ERROR] 数据库查询失败：{type(e).__name__}: {e}')
            return 2
    finally:
        sess.close()

    empty_ids = {tid for tid in db_ids if rows.get(tid, 0) == 0}
    info_files, lc_files = etl.list_data_files()
    orphan_info, orphan_lc = etl.find_orphan_files(db_ids, info_files, lc_files)
    stale_lc = etl.find_stale_lc_files(empty_ids, lc_files)

    n_orph = len(orphan_info) + len(orphan_lc)
    if n_orph == 0 and not stale_lc:
        print(f'[OK] 库↔文件一致：{len(db_ids)} 源（{os.path.basename(data_dir)} 下 '
              f'{len(info_files)} info / {len(lc_files)} lc），无孤儿、无陈旧 CSV')
        return 0

    print(f'[WARN] 库↔文件有差异（重建/--sync 会把它们灌回库）：')
    print(f'       孤儿 info JSON {len(orphan_info)} 个 / 孤儿 lc CSV {len(orphan_lc)} 个'
          f' / 陈旧 CSV（库里 0 点但文件有行）{len(stale_lc)} 个')
    if not args.quiet:
        for p in (orphan_info + orphan_lc + stale_lc)[:20]:
            print('         -', os.path.relpath(p, data_dir))
        extra = n_orph + len(stale_lc) - 20
        if extra > 0:
            print(f'         …另 {extra} 个')
    print('       清理：python3 backend/etl.py --dump --prune'
          '（移到 backups/dump_prune_<时间>/，可逆）')
    return 1


if __name__ == '__main__':
    sys.exit(main())
