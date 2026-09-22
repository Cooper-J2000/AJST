#!/usr/bin/env python3
"""一次性迁移（幂等，可重复运行）：

1. lightcurves.mjd：由 time（相对 T0 秒数）+ transients.t0 计算回填
   （MJD = T0_MJD + time/86400；无 T0 的源 MJD 留空 NULL）。
2. tags 索引表：把 transients 表现役的主 tag / 副 tag 全部登记进去
   （已存在的不动，新登记的文字说明留空待补）。

前提：后端服务已重启过一次（init_db 已补出 mjd / tags.kind 等新列）。
用法（仓库根）：
    python3 scripts/migrate_lc_mjd.py
"""
import os
import sys

# 允许从仓库根直接运行：向上找到含 backend/ 的目录
d = os.path.abspath(os.path.dirname(__file__))
while d != os.path.dirname(d) and not os.path.isdir(os.path.join(d, 'backend')):
    d = os.path.dirname(d)
sys.path.insert(0, os.path.join(d, 'backend'))

from sqlalchemy import text  # noqa: E402
import app  # noqa: E402
from etl import register_used_tags, ensure_default_tags  # noqa: E402


def main():
    sess = app.get_session()
    try:
        # ── 1. 回填 lightcurves.mjd（MJD = T0_MJD + time/86400） ──
        # MJD 0 = 1858-11-17 00:00:00 UTC；EXTRACT(EPOCH FROM 间隔) 得秒数
        r = sess.execute(text(
            "UPDATE lightcurves lc SET mjd = "
            " EXTRACT(EPOCH FROM (t.t0 - TIMESTAMP '1858-11-17 00:00:00')) / 86400.0"
            " + lc.time / 86400.0"
            " FROM transients t"
            " WHERE lc.transient_id = t.id AND t.t0 IS NOT NULL"
            "   AND lc.mjd IS NULL"
        ))
        sess.commit()
        print(f'[OK] lightcurves.mjd 回填 {r.rowcount} 行')
        n_null = sess.execute(text(
            "SELECT count(*) FROM lightcurves WHERE mjd IS NULL")).scalar()
        n_no_t0 = sess.execute(text(
            "SELECT count(*) FROM lightcurves lc JOIN transients t"
            " ON lc.transient_id = t.id WHERE t.t0 IS NULL")).scalar()
        print(f'[INFO] mjd 仍为 NULL 的行: {n_null}（其中源无 T0 的行: {n_no_t0}）')

        # ── 2. 登记现役 tag 到索引表 ──
        ensure_default_tags(sess)
        register_used_tags(sess)
        n_tags = sess.execute(text('SELECT count(*) FROM tags')).scalar()
        print(f'[OK] tags 索引表现有 {n_tags} 条（主/副 tag，含新登记的空描述条目）')
    finally:
        sess.close()


if __name__ == '__main__':
    main()
