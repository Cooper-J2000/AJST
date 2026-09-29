"""API-14 的编辑面：白名单字段校验 + manifest 直接改写。

设计纪律（用户裁决，2026-09-29）：不做保留注释的"外科改写"、不做逐次备份——
模板库随 AJST-Data 进 git（catadata/tmplibrary/），版本历史由 git 承担，网页
编辑就是普通的"载入 → 改字段 → 规范 dump → 引擎自校验 → 原子替换"。

- ``validate_edit``：白名单 + 与造模板向导同口径的合并校验（Q-2/Q-12/U-22/
  F-18/F-47），返回 (sets, deletes, citation_appends, changed)；
- ``apply_to_dict``：把 sets/deletes/appends 应用到 safe_load 的 manifest dict；
- ``write_manifest_checked``：tmp 文件先过引擎严格自校验
  （``TemplateSpec.from_yaml``，M_SCHEMA 同一道门）再 ``os.replace``——自校验
  不过则原件不动。
"""

from __future__ import annotations

import math
import os

import yaml

from . import engine, paths
from .manifest import DISTANCE_KINDS, DeclError

#: API-14 请求的顶层键白名单（其余一律拒绝并列出）
TOP_KEYS = ("label", "object_class", "redshift", "distance", "validity",
            "epoch_zero", "citation_append", "rebuild")

_EZ_KINDS = ("fixed", "first_point", "column")
_EZ_UNITS = ("s", "day", "mjd")


# ── 校验：合并后的完整声明必须过与造模板向导同口径的规则 ─────────────────────

def _finite_opt(v, name):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DeclError(f"{name} 必须是有限数值，得到 {v!r}")
    if not math.isfinite(f):
        raise DeclError(f"{name} 必须是有限数值，得到 {v!r}")
    return f


def _merged(current: dict, body: dict) -> dict:
    """current（safe_load 的 manifest）与 body 的合并视图（只合并白名单块）。"""
    out = {
        "label": current.get("label"),
        "object_class": current.get("object_class"),
        "redshift": dict(current.get("redshift") or {}),
        "distance": dict(current.get("distance") or {}),
        "validity": dict(current.get("validity") or {}),
        "epoch_zero": dict(((current.get("photometry") or {}).get("time") or {})
                           .get("epoch_zero") or {}),
    }
    for key in ("label", "object_class"):
        if key in body:
            out[key] = body[key]
    for block in ("redshift", "distance", "validity", "epoch_zero"):
        if isinstance(body.get(block), dict):
            out[block].update(body[block])
    return out


def validate_edit(body: dict, current: dict):
    """API-14 请求体校验。返回 (sets, deletes, citation_appends, changed)。

    sets/deletes 的键是 manifest 里的路径元组；changed 是给响应用的人读标签
    列表。校验不过抛 DeclError（与 Q-2 同一形态）。
    """
    if not isinstance(body, dict):
        raise DeclError("请求体必须是 JSON 对象")
    unknown = sorted(set(body) - set(TOP_KEYS))
    if unknown:
        raise DeclError(
            f"不可编辑字段：{unknown}（白名单只有 {list(TOP_KEYS)}；行集口径 "
            "rowset/policy/bands/CSV 是冻结数据，改动须删除后重新建面）",
            refused_fields=unknown)

    m = _merged(current, body)

    label = m["label"]
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise DeclError("label 必须是非空字符串", missing=["label"])
    oc = m["object_class"]
    if not isinstance(oc, str) or not oc.strip():
        raise DeclError("object_class 必填且非空", missing=["object_class"])

    red = m["redshift"]
    z = _finite_opt(red.get("value"), "redshift.value")
    if z is None or z < 0:
        raise DeclError("redshift.value 必须是 ≥0 的有限数值",
                        missing=["redshift.value"])
    if not isinstance(red.get("source"), str) or not red["source"].strip():
        raise DeclError("redshift.source 必填（无可信默认，猜了就是谎报）",
                        missing=["redshift.source"])
    z_err = _finite_opt(red.get("error"), "redshift.error")
    z_kind = red.get("error_kind")
    kinds = engine.engine_enums()["z_error_kinds"]
    if z_err is not None and z_err <= 0:
        raise DeclError("redshift.error 必须 > 0；要清空请把 error 与 "
                        "error_kind 一起置 null", missing=["redshift.error"])
    if (z_err is None) != (not z_kind):
        raise DeclError("redshift.error 与 error_kind 必须同有同无（Q-12：三者"
                        "互不等价，引擎加载时也会拒）",
                        missing=["redshift.error", "redshift.error_kind"])
    if z_kind and z_kind not in kinds:
        raise DeclError(f"error_kind 必须是 {kinds} 之一，得到 {z_kind!r}",
                        missing=["redshift.error_kind"], z_error_kinds=kinds)

    dist = m["distance"]
    dkind = dist.get("kind")
    if dkind not in DISTANCE_KINDS:
        raise DeclError(f"distance.kind 四选一 {DISTANCE_KINDS}，得到 {dkind!r}",
                        missing=["distance.kind"])
    mu = _finite_opt(dist.get("mu"), "distance.mu")
    d_l = _finite_opt(dist.get("d_L_Mpc"), "distance.d_L_Mpc")
    d_l_err = _finite_opt(dist.get("d_L_err_Mpc"), "distance.d_L_err_Mpc")
    if d_l_err is not None and d_l_err <= 0:
        raise DeclError("distance.d_L_err_Mpc 必须 > 0",
                        missing=["distance.d_L_err_Mpc"])
    if dkind != "cosmological" and mu is None and d_l is None:
        raise DeclError("非 cosmological 距离必须有 mu 或 d_L_Mpc（U-22）",
                        missing=["distance.mu|d_L_Mpc"])
    allow_low_z = bool(dist.get("allow_low_z"))
    if dkind == "cosmological" and z < engine.limits()["low_z"] \
            and not allow_low_z:
        raise DeclError(f"z={z:g} < {engine.limits()['low_z']:g} 时选 "
                        "cosmological 必须显式 allow_low_z=true（F-18/TXT-13）",
                        missing=["distance.allow_low_z"])

    val = m["validity"]
    rmb = val.get("require_min_bands")
    if not isinstance(rmb, int) or isinstance(rmb, bool) or rmb < 2:
        raise DeclError("validity.require_min_bands 必须是 ≥2 的整数（F-47）",
                        missing=["validity.require_min_bands"])
    notes = val.get("notes") or ""
    if rmb > 2 and not str(notes).strip():
        raise DeclError("require_min_bands>2 时 validity.notes 必须写理由（F-47）",
                        missing=["validity.notes"])

    ez = m["epoch_zero"]
    ez_kind = ez.get("kind")
    if ez_kind not in _EZ_KINDS:
        raise DeclError(f"epoch_zero.kind 三选一 {_EZ_KINDS}，得到 {ez_kind!r}",
                        missing=["epoch_zero.kind"])
    if ez_kind == "column":
        raise DeclError("epoch_zero.kind=column 不在网页编辑面内（需要请直接改 "
                        "YAML），fixed / first_point 可走本端点",
                        missing=["epoch_zero.kind"])
    ez_unit = ez.get("unit")
    if ez_unit is not None and ez_unit not in _EZ_UNITS:
        raise DeclError(f"epoch_zero.unit 必须是 {_EZ_UNITS} 之一（或省略继承"
                        f" time.unit），得到 {ez_unit!r}",
                        missing=["epoch_zero.unit"])
    ez_value = _finite_opt(ez.get("value"), "epoch_zero.value")
    if ez_kind == "fixed" and ez_value is None:
        raise DeclError("epoch_zero.kind=fixed 需要 value（单位 = epoch_zero.unit "
                        "或 time.unit）", missing=["epoch_zero.value"])

    cite = body.get("citation_append")
    if cite is not None and (not isinstance(cite, str) or not cite.strip()):
        raise DeclError("citation_append 必须是非空字符串（追加进 "
                        "provenance.citations）", missing=["citation_append"])

    # ── 与 current 求差，生成 sets/deletes ──
    sets, deletes, changed = {}, [], []

    def _diff(path, new, old, tag):
        if new is None and old is not None:
            deletes.append(path)
            changed.append(tag)
        elif new is not None and new != old:
            sets[path] = new
            changed.append(tag)

    _diff(("label",), label.strip() if isinstance(label, str) else label,
          current.get("label"), "label")
    _diff(("object_class",), oc.strip(), current.get("object_class"),
          "object_class")

    cur_red = current.get("redshift") or {}
    _diff(("redshift", "value"), z, cur_red.get("value"), "redshift.value")
    if isinstance(body.get("redshift"), dict) and "source" in body["redshift"]:
        _diff(("redshift", "source"), red["source"].strip(),
              cur_red.get("source"), "redshift.source")
    _diff(("redshift", "error"), z_err, cur_red.get("error"), "redshift.error")
    _diff(("redshift", "error_kind"), z_kind or None,
          cur_red.get("error_kind"), "redshift.error_kind")

    cur_dist = current.get("distance") or {}
    _diff(("distance", "kind"), dkind, cur_dist.get("kind"), "distance.kind")
    _diff(("distance", "mu"), mu, cur_dist.get("mu"), "distance.mu")
    _diff(("distance", "d_L_Mpc"), d_l, cur_dist.get("d_L_Mpc"),
          "distance.d_L_Mpc")
    _diff(("distance", "d_L_err_Mpc"), d_l_err, cur_dist.get("d_L_err_Mpc"),
          "distance.d_L_err_Mpc")
    if isinstance(body.get("distance"), dict) and "allow_low_z" in body["distance"]:
        _diff(("distance", "allow_low_z"), allow_low_z or None,
              cur_dist.get("allow_low_z"), "distance.allow_low_z")
    if isinstance(body.get("distance"), dict) and "source" in body["distance"]:
        ds = (dist.get("source") or "").strip() or None
        _diff(("distance", "source"), ds, cur_dist.get("source"),
              "distance.source")

    cur_val = current.get("validity") or {}
    _diff(("validity", "require_min_bands"), rmb,
          cur_val.get("require_min_bands"), "require_min_bands")
    if isinstance(body.get("validity"), dict) and "notes" in body["validity"]:
        _diff(("validity", "notes"), str(notes).strip() or None,
              cur_val.get("notes"), "validity.notes")

    cur_ez = (((current.get("photometry") or {}).get("time") or {})
              .get("epoch_zero") or {})
    _diff(("photometry", "time", "epoch_zero", "kind"), ez_kind,
          cur_ez.get("kind"), "epoch_zero.kind")
    _diff(("photometry", "time", "epoch_zero", "value"), ez_value,
          cur_ez.get("value"), "epoch_zero.value")
    _diff(("photometry", "time", "epoch_zero", "unit"), ez_unit,
          cur_ez.get("unit"), "epoch_zero.unit")
    if isinstance(body.get("epoch_zero"), dict) and "source" in body["epoch_zero"]:
        ezs = (ez.get("source") or "").strip() or None
        _diff(("photometry", "time", "epoch_zero", "source"), ezs,
              cur_ez.get("source"), "epoch_zero.source")

    appends = [cite.strip()] if isinstance(cite, str) and cite.strip() else []
    if appends:
        changed.append("citation_append")
    return sets, deletes, appends, changed


# ── 直接改写：应用到 dict + 自校验后落盘 ─────────────────────────────────────

def apply_to_dict(current: dict, sets, deletes, appends) -> dict:
    """把 validate_edit 的结果应用到 safe_load 的 manifest dict（返回新 dict）。"""
    import copy
    data = copy.deepcopy(current)
    for path, value in sets.items():
        cur = data
        for p in path[:-1]:
            nxt = cur.setdefault(p, {})
            if not isinstance(nxt, dict):
                raise DeclError(f"路径中段不是 mapping: {'.'.join(map(str, path))}")
            cur = nxt
        cur[path[-1]] = value
    for path in deletes:
        cur = data
        for p in path[:-1]:
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(cur, dict):
            cur.pop(path[-1], None)
    if appends:
        cites = data.setdefault("provenance", {}).setdefault("citations", [])
        cites.extend(appends)
    return data


def write_manifest_checked(root, template_id, manifest_dict):
    """tmp 写入 → 引擎严格自校验 → os.replace（自校验不过则原件不动）。"""
    cs = engine.require()
    path = paths.template_yaml(root, template_id)
    tmp = path.with_suffix(f".yaml.tmp-{os.getpid()}")
    try:
        tmp.write_text(yaml.safe_dump(manifest_dict, allow_unicode=True,
                                      sort_keys=False), encoding="utf-8")
        cs.TemplateSpec.from_yaml(tmp)     # M_SCHEMA 同一道门，先校验后替换
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path
