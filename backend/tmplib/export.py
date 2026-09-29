"""API-9 (S4): API-8 同参数的文件形式 —— **同一批数据、两种渲染**（F-56：
内部只调一次 `compare.run`，CSV 与 JSON 两条通路只做序列化）。

GET query 编码（docs/TECHNICAL.md 留注）：`curves` / `sources` 各为一个
URL 编码的 JSON 数组串，元素形状与 API-8 请求体逐项相同；
`include_measured` 取 `0`/`1`（缺省 `1`）。配额与结构校验直接复用
`compare.validate_request`（Q-8/A-6 同口径）。

CSV 列序（§4.3）：见 `COLUMNS`。注释头（F-41'）以引擎 CLI 注释头为模板
（`template/band/mode/z/stretch` + `DM/peak/relocated`），之上只补引擎
没有的六项：`transient_id`、`align`/`align_reference_epoch`（POS-8：宿主
只有"逐曲线时间原点"这一个控件，没有 align 模式，故 align 恒为
`time_origin`）、`mu_engine`/`mu_catalog`/`mu_delta` 及成因、
`domain.state`+`reasons`、`epoch_answered`。

F-57'：`time_origin_mjd`/`time_origin_kind` 每条曲线一组 —— 模板曲线取
`time_origin`（用户基准优先，其次模板零点的绝对 MJD）；源曲线（measured /
kcorrected-measured）共用该源的目录 t0（kind=event-t0），逐源性原样带出。
上限点单列、不入表（F-06' 口径），其计数写进该曲线的 counts 注释行。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import compare as tl_compare
from . import engine, predict as tl_predict

#: §4.3 列序（19 列；改动即契约变更，T-40 逐项勾全）
COLUMNS = [
    "time_obs_days", "time_obs_s", "time_origin_mjd", "time_origin_kind",
    "mag_AB", "mag_AB_err", "M_abs_AB", "K_mag", "mu_engine", "mu_delta",
    "band", "mode", "z", "relocated", "band_trust",
    "absolute_mag_reference", "extinction_mag",
    "sed_extrapolated_fraction", "sed_beyond_coverage_fraction",
]

FORMAT_TAG = "ajst-tmplib-export-1"


def parse_query(args) -> dict:
    """GET query → API-8 同款 req（Q-8 配额/结构校验复用 compare）。"""
    body = {}
    for key in ("curves", "sources"):
        raw = args.get(key)
        if raw is None or raw == "":
            continue
        try:
            body[key] = json.loads(raw)
        except ValueError:
            raise tl_predict.TLError(
                "TL_INCONSISTENT_ARGS", 400,
                f"{key} 必须是 URL 编码的 JSON 数组串（元素形状同 API-8 请求体）")
    body["include_measured"] = args.get("include_measured", "1") not in ("0", "false")
    return tl_compare.validate_request(body)


def _fmt(v):
    """注释头里的标量：None → '-'；float → %.6g；其余 str。"""
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def _peak(times, mags):
    """CLI 头第二行的 peak：最亮点及其 t_obs（d）；全空 → (None, None)。"""
    best, best_t = None, None
    for t, m in zip(times, mags):
        if m is None:
            continue
        if best is None or m < best:
            best, best_t = m, t
    return best, best_t


def _mu_fields(curve):
    mu = curve.get("mu") or {}
    if mu.get("ok"):
        return (curve.get("distance_modulus") if curve.get("distance_modulus") is not None
                else mu.get("engine")), mu.get("catalog"), mu.get("delta"), mu.get("cause")
    eng = curve.get("distance_modulus")
    if eng is None:
        eng = curve.get("distance_modulus_engine")
    return eng, None, None, None


def _origin_fields(curve, source_origins):
    """(time_origin_mjd, kind, align_reference_epoch)。F-57' 逐曲线一组。"""
    kind = curve.get("kind")
    if kind == "template-prediction":
        to = curve.get("time_origin") or {}
        epoch = to.get("value_mjd")
        if epoch is None:
            epoch = to.get("t_zero_mjd")
        return to.get("t_zero_mjd"), to.get("kind"), epoch
    sid = curve.get("transient_id")
    org = (source_origins or {}).get(sid) or {}
    return org.get("value_mjd"), org.get("kind"), org.get("value_mjd")


def _curve_comments(i, curve, *, peak_mag, peak_t, answered, total,
                    to_mjd, to_kind, align_epoch, mu_eng, mu_cat, mu_d, mu_cause,
                    extra=None):
    """F-41' 注释头：CLI 两行模板 + 六项补充（逐项一行，T-40 逐项勾全）。"""
    dom = curve.get("domain") or {}
    reasons = ";".join(dom.get("reasons") or []) or "-"
    tid = curve.get("template_id") or "-"
    lines = [
        f"curve[{i}] template={tid} band={curve.get('band') or '*'} "
        f"mode={curve.get('mode') or '*'} z={_fmt(curve.get('z'))} "
        f"stretch={_fmt(curve.get('stretch') if curve.get('stretch') is not None else 1 if curve.get('kind') == 'template-prediction' else None)}",
        f"curve[{i}] DM={_fmt(mu_eng)} mag | peak {_fmt(peak_mag)} AB at "
        f"t_obs={_fmt(peak_t)} d | relocated={_fmt(curve.get('relocated'))}",
        f"curve[{i}] transient_id={curve.get('transient_id') or '-'}",
        f"curve[{i}] align=time_origin align_reference_epoch={_fmt(align_epoch)} "
        f"time_origin_kind={to_kind or '-'}",
        f"curve[{i}] mu_engine={_fmt(mu_eng)} mu_catalog={_fmt(mu_cat)} "
        f"mu_delta={_fmt(mu_d)} mu_cause={mu_cause or '-'}",
        f"curve[{i}] domain.state={dom.get('state') or '-'} reasons={reasons}",
        f"curve[{i}] epoch_answered={answered}/{total}",
    ]
    if extra:
        lines.extend(extra)
    return lines


def _template_rows(curve):
    p = curve.get("points") or {}
    meta = curve.get("meta") or {}
    times = p.get("time_obs_days") or []
    mu_eng, _cat, mu_d, _c = _mu_fields(curve)
    ext = meta.get("extinction_mag")
    const_tail = [
        curve.get("band"), curve.get("mode"), curve.get("z"),
        curve.get("relocated"),
        meta.get("band_trust"), meta.get("absolute_mag_reference"),
        None,  # extinction_mag 是逐点数组，逐行取（见下）
        meta.get("sed_extrapolated_fraction"),
        meta.get("sed_beyond_coverage_fraction"),
    ]
    to_mjd, to_kind, align_epoch = _origin_fields(curve, None)
    rows = []
    for j, t_d in enumerate(times):
        tail = list(const_tail)
        tail[6] = ext[j] if isinstance(ext, list) and j < len(ext) else ext
        rows.append([
            t_d, (p.get("time_obs_s") or [None] * len(times))[j],
            to_mjd, to_kind,
            (p.get("mag_AB") or [])[j], (p.get("mag_AB_err") or [None] * len(times))[j],
            (p.get("M_abs_AB") or [None] * len(times))[j],
            (p.get("K_mag") or [None] * len(times))[j],
            mu_eng, mu_d,
            *tail,
        ])
    return rows, times, (p.get("mag_AB") or [])


def run_export(req: dict, *, source_data: dict, catalogs: dict,
               rows_hashes: dict, source_origins: dict, root=None) -> dict:
    """F-56：调一次 compare.run，产出 {comments, columns, rows} 单一中间表。

    `source_origins`: transient_id -> {'value_mjd', 'kind'}（路由层用已关闭
    session 的目录 t0 构造，ST-1）。
    """
    result = tl_compare.run(req, source_data=source_data, catalogs=catalogs,
                            rows_hashes=rows_hashes,
                            root=Path(root) if root is not None else None)
    comments = [
        f"ajst-tmplib export ({FORMAT_TAG}; API-9 = API-8 同批数据的文件渲染, F-56)",
        f"engine {result['provenance'].get('engine_version')} "
        f"code_sha256={str(result['provenance'].get('engine_code_sha256'))[:12]} "
        f"cosmology={result['provenance'].get('cosmology')}",
    ]
    rows: list[list] = []
    for i, curve in enumerate(result["curves"]):
        if curve.get("state") == "error":
            err = curve.get("error") or {}
            comments.append(
                f"curve[{i}] state=error code={err.get('code')} "
                f"id={curve.get('transient_id') or curve.get('template_id') or '-'} "
                f"message={err.get('message')}")
            continue
        kind = curve.get("kind")
        to_mjd, to_kind, align_epoch = _origin_fields(curve, source_origins)
        mu_eng, mu_cat, mu_d, mu_cause = _mu_fields(curve)
        if kind == "template-prediction":
            trows, times, mags = _template_rows(curve)
            rows.extend(trows)
            peak_mag, peak_t = _peak(times, mags)
            answered = sum(1 for m in mags if m is not None)
            comments.extend(_curve_comments(
                i, curve, peak_mag=peak_mag, peak_t=peak_t,
                answered=answered, total=len(times),
                to_mjd=to_mjd, to_kind=to_kind, align_epoch=align_epoch,
                mu_eng=mu_eng, mu_cat=mu_cat, mu_d=mu_d, mu_cause=mu_cause))
            continue
        # measured / kcorrected-measured：探测点入行；上限单列（F-06'）
        pts = curve.get("points") or {}
        dets = pts.get("detections") or []
        counts = curve.get("counts") or {}
        kcorr = kind == "kcorrected-measured"
        for p in dets:
            t_s = p.get("t_obs_s")
            rows.append([
                None if t_s is None else t_s / 86400.0, t_s,
                to_mjd, to_kind,
                p.get("m_AB"), p.get("m_AB_err"),
                p.get("M_meas") if kcorr else None,
                p.get("K") if kcorr else None,
                mu_eng if kcorr else None,
                mu_d if kcorr else None,
                p.get("band"), p.get("mode") if kcorr else None,
                curve.get("z") if kcorr else None,
                None, None, None, None, None, None,
            ])
        mags = [p.get("m_AB") for p in dets]
        times = [None if p.get("t_obs_s") is None else p["t_obs_s"] / 86400.0
                 for p in dets]
        peak_mag, peak_t = _peak(times, mags)
        answered = sum(1 for m in mags if m is not None)
        total = len(dets)
        if kcorr:
            total += len(pts.get("failed") or []) + len(pts.get("clipped") or [])
        extra = [
            f"curve[{i}] counts detections={counts.get('detections', len(dets))} "
            f"upper_limits={counts.get('upper_limits', 0)}（上限单列不入本表, F-06'） "
            f"discard={counts.get('discard', 0)} clipped={counts.get('clipped', 0)} "
            f"failed={counts.get('failed', 0)}",
        ]
        comments.extend(_curve_comments(
            i, curve, peak_mag=peak_mag, peak_t=peak_t,
            answered=answered, total=total,
            to_mjd=to_mjd, to_kind=to_kind, align_epoch=align_epoch,
            mu_eng=mu_eng if kcorr else None, mu_cat=mu_cat,
            mu_d=mu_d if kcorr else None, mu_cause=mu_cause,
            extra=extra))
    return {
        "format": FORMAT_TAG,
        "comments": comments,
        "columns": list(COLUMNS),
        "rows": rows,
        "provenance": result.get("provenance"),
        "notes": result.get("notes"),
    }


def _cell(v):
    """CSV 单元：None → 空；float → repr（round-trip 精确，T-17 重解析互比）。"""
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    return str(v)


def render_csv(table: dict) -> str:
    lines = [f"# {c}" for c in table["comments"]]
    lines.append(",".join(table["columns"]))
    for row in table["rows"]:
        lines.append(",".join(_cell(v) for v in row))
    return "\n".join(lines) + "\n"


def render_json(table: dict) -> dict:
    """与 CSV 同一张中间表的 JSON 形态（F-56：不重算，只序列化）。"""
    return {
        "code": "TL_OK",
        "format": table["format"],
        "comments": table["comments"],
        "columns": table["columns"],
        "rows": table["rows"],
        "provenance": table.get("provenance"),
        "notes": table.get("notes"),
    }
