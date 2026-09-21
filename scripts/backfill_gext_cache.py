#!/usr/bin/env python3
"""回填派生缓存列（幂等，可重复运行）：

  filters.gext_coeff      = A_λ/E(B-V) = Rv·P92(λ)（P92 域外波段，如射电/毫米，为 0）
  transients.gext_ebv     = 该坐标 CSFD 尘图 E(B-V)
  host_galaxies.gext_ebv  = 同上（宿主自身坐标）

各写入口（filters/transients/hosts API、ETL 导入）已自动维护这些列，
本脚本用于存量数据或直接改库后的重新同步。用法（仓库根）：
    python3 scripts/backfill_gext_cache.py
"""
import os
import sys
import time

# 允许从仓库根直接运行：向上找到含 backend/ 的目录
d = os.path.abspath(os.path.dirname(__file__))
while d != os.path.dirname(d) and not os.path.isdir(os.path.join(d, 'backend')):
    d = os.path.dirname(d)
sys.path.insert(0, os.path.join(d, 'backend'))

import app  # noqa: E402
from models import FilterDef, Transient, HostGalaxy  # noqa: E402
import extinction  # noqa: E402


def main():
    sess = app.get_session()
    t0 = time.time()

    # 依赖不可用时直接放弃：dust_coeff 会因算不出系数而返回 None，
    # 继续跑会把已有的 filters.gext_coeff 清成 NULL（写坏缓存）
    if not extinction._load():
        print('!! dustmaps 依赖不可用:', extinction._import_error)
        print('   跳过回填，未修改任何缓存列')
        return

    # 1) filters → 消光系数 k
    n = 0
    for f in sess.query(FilterDef).all():
        c = extinction.dust_coeff(f.wavelength)
        if f.gext_coeff != c:
            f.gext_coeff = c
            n += 1
    sess.commit()
    print('filters.gext_coeff   : %d/%d updated (%.2fs)'
          % (n, sess.query(FilterDef).count(), time.time() - t0))

    # 2) 无坐标行清空 E(B-V) 缓存
    k = 0
    for model in (Transient, HostGalaxy):
        for col in (model.ra, model.dec):
            k += sess.query(model).filter(col.is_(None)).update(
                {model.gext_ebv: None}, synchronize_session=False)
    sess.commit()
    print('coordless NULLs      : %d rows' % k)

    # 3) E(B-V) 缓存
    for label, model in (('transients.gext_ebv  ', Transient),
                         ('hosts.gext_ebv       ', HostGalaxy)):
        rows = sess.query(model).filter(
            model.ra.isnot(None), model.dec.isnot(None)).all()
        n, t1 = 0, time.time()
        for r in rows:
            before = r.gext_ebv
            extinction.refresh_ebv(r)
            if r.gext_ebv != before:
                n += 1
        sess.commit()
        print('%s: %d/%d updated (%.2fs)'
              % (label, n, len(rows), time.time() - t1))


if __name__ == '__main__':
    main()
