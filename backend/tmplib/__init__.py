"""tmplib -- 「模板库 × K 改正对比」的后端引擎层（设计文档 02，S0/S1/S2）。

P0：引擎装载与守卫（engine/paths/guard）。
P1：库与叠绘（domain/mudelta/predict）。
P2：造模板向导（extract/manifest/surfaces/indomain）。

约束：ST-1（引擎调用全程不持有 DB session）；A-3（读路径永不 rebuild）；
F-50（library.json 只经 paths.write_library 原子整份重写）；
POS-3（引擎 build_template 的唯一调用点在 surfaces.py）。
"""

from . import domain, engine, extract, guard, indomain, manifest, mudelta, \
    paths, predict, surfaces

__all__ = ["domain", "engine", "extract", "guard", "indomain", "manifest",
           "mudelta", "paths", "predict", "surfaces"]
