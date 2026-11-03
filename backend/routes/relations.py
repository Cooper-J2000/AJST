"""
统计关系接口（基于 extra_data.params 平铺参数表）

数据源：extra_data['params'] = {"<参数key>": {"v": 数值或字符串, "err": 单值|[正,负]|null, "src": 来源短名}}
（旧的 catalog_data / derived 结构已废弃删除；参数 key 注册表见 backend/params_registry.json，
关系定义与文献系数见 backend/relations.json）

GET  /api/relations               — 关系定义列表 + coverage（每条关系当前库内可画点数）
GET  /api/relations/<name>/data   — 取数点（applies_to 过滤；log 轴剔除非正值）
POST /api/relations/<name>/fit    — 带内禀弥散的最大似然拟合（Kelly 2007 风格 NLL；需登录）
"""
import json, math, os
from types import SimpleNamespace
import numpy as np
from scipy.optimize import minimize
from sqlalchemy import select
from flask import Blueprint, jsonify, request, abort
from app import get_session, require_auth
from models import Transient

relations_bp = Blueprint('relations', __name__)

_REL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'relations.json')
with open(_REL_PATH, encoding='utf-8') as f:
    _RELATIONS = json.load(f)['relations']
_REL_BY_NAME = {r['name']: r for r in _RELATIONS}

# group_by=sn_subtype 时的已知子型表（sub_tag 与之取交集，首项为组名）
_SN_SUBTYPES = ["Ia", "Ia-pec", "II", "II-P", "IIb", "Ibn", "Icn",
                "Ib", "Ic", "Ic-BL", "Ibc", "SLSN"]


def _load_params():
    """读出所有源的 params（顺带一级列），返回 [(meta, params), ...]。
    只 SELECT 需要的列（extra_data 只取 params 子键），
    避免每次请求把全表 comment/extra_data 大字段载入内存。"""
    sess = get_session()
    try:
        rows = sess.execute(
            select(Transient.id, Transient.tags, Transient.sub_tag,
                   Transient.redshift,
                   Transient.extra_data['params'].label('params'))
        ).all()
        return [(SimpleNamespace(id=r.id, tags=r.tags, sub_tag=r.sub_tag,
                                 redshift=r.redshift), r.params)
                for r in rows if isinstance(r.params, dict) and r.params]
    finally:
        sess.close()


def _passes_applies(rel, t):
    """applies_to 过滤：tag 必匹配；sub_tag_any 非空时源 sub_tag 须有交集"""
    ap = rel.get('applies_to') or {}
    tag = ap.get('tag')
    if tag and tag not in (t.tags or []):
        return False
    subs = ap.get('sub_tag_any')
    if subs and not (set(subs) & set(t.sub_tag or [])):
        return False
    return True


def _group_of(rel, t, params):
    """分组名：grb_type 取 params.grb_type.v（I/II，其余 None）；
    sn_subtype 取 sub_tag 与已知子型表交集首项（无则 None）"""
    gb = rel.get('group_by')
    if gb == 'grb_type':
        gt = (params.get('grb_type') or {}).get('v')
        return gt if gt in ('I', 'II') else None
    if gb == 'sn_subtype':
        for s in (t.sub_tag or []):
            if s in _SN_SUBTYPES:
                return s
        return None
    return None


def _axis_ok(v, is_log):
    """轴取值检查：log 轴要求正有限值，线性轴只要求有限"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v) and (v > 0 if is_log else True)


@relations_bp.route('', methods=['GET'])
def list_relations():
    """关系定义 + coverage：每条关系当前库内可画点数
    （x、y 参数都有数值、满足 log 轴正值要求、且通过 applies_to 过滤的源数）"""
    rows = _load_params()
    coverage = {}
    for rel in _RELATIONS:
        xk, yk = rel['x']['key'], rel['y']['key']
        x_log = rel['x'].get('log', True)
        y_log = rel['y'].get('log', True)
        n = 0
        for t, params in rows:
            if not _passes_applies(rel, t):
                continue
            px, py = params.get(xk), params.get(yk)
            if not isinstance(px, dict) or not isinstance(py, dict):
                continue
            if _axis_ok(px.get('v'), x_log) and _axis_ok(py.get('v'), y_log):
                n += 1
        coverage[rel['name']] = n
    return jsonify({'relations': _RELATIONS, 'coverage': coverage})


@relations_bp.route('/<name>/data', methods=['GET'])
def relation_data(name):
    """取数点。只返回 x、y 都有数值且满足轴要求的点（applies_to 过滤后）。"""
    rel = _REL_BY_NAME.get(name)
    if rel is None:
        abort(404, description=f'未知关系: {name}')
    xk, yk = rel['x']['key'], rel['y']['key']
    x_log = rel['x'].get('log', True)
    y_log = rel['y'].get('log', True)

    points = []
    for t, params in _load_params():
        if not _passes_applies(rel, t):
            continue
        px, py = params.get(xk), params.get(yk)
        if not isinstance(px, dict) or not isinstance(py, dict):
            continue
        x, y = px.get('v'), py.get('v')
        if not _axis_ok(x, x_log) or not _axis_ok(y, y_log):
            continue
        points.append({
            'id': t.id, 'x': x, 'xerr': px.get('err'), 'y': y, 'yerr': py.get('err'),
            'group': _group_of(rel, t, params),
            'tags': t.tags or [], 'sub_tag': t.sub_tag or [],
            'z': t.redshift, 'src': {'x': px.get('src'), 'y': py.get('src')},
        })
    return jsonify({'relation': rel, 'points': points})


# ---------------- 最大似然拟合（POST /fit） ----------------

_FIT_MAX_POINTS = 5000  # 点数保护上限


def _as_num(v):
    """布尔以外的有限数值原样返回，否则 None"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) else None


def _log_err(v, err):
    """线性值误差 → log10 空间对称误差。err 为单值、[正,负] 或 None。
    不对称时分别算 log 空间上下误差再取平均；无效返回 None。"""
    if err is None:
        return None
    if isinstance(err, (list, tuple)):
        pos = err[0] if len(err) > 0 else None
        neg = err[1] if len(err) > 1 else pos
    else:
        pos = neg = err
    lv = math.log10(v)
    ds = []
    pos, neg = _as_num(pos), _as_num(neg)
    if pos and pos > 0 and v + pos > 0:
        ds.append(math.log10(v + pos) - lv)
    if neg and neg > 0 and v - neg > 0:
        ds.append(lv - math.log10(v - neg))
    return sum(ds) / len(ds) if ds else None


def _lin_err(err):
    """线性轴误差对称化：单值原样，[正,负] 取平均；无效返回 None"""
    if err is None:
        return None
    if isinstance(err, (list, tuple)):
        vals = [e for e in err[:2] if _as_num(e) and e > 0]
        return sum(vals) / len(vals) if vals else None
    e = _as_num(err)
    return e if e and e > 0 else None


def _prepare_xy(points, x_log, y_log):
    """把请求点变换到拟合空间：x/y 各按轴定义决定是否取 log10（线性轴误差对称化）；
    返回 [(X,Y,sx,sy,group), ...]"""
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        x, y = _as_num(p.get('x')), _as_num(p.get('y'))
        if x is None or y is None:
            continue
        if x_log and x <= 0:
            continue  # log 轴剔除非正值
        if y_log and y <= 0:
            continue
        if x_log:
            X, sx = math.log10(x), _log_err(x, p.get('xerr'))
        else:
            X, sx = x, _lin_err(p.get('xerr'))
        if y_log:
            Y, sy = math.log10(y), _log_err(y, p.get('yerr'))
        else:
            Y, sy = y, _lin_err(p.get('yerr'))
        gt = p.get('group')
        out.append((X, Y, sx, sy, gt if gt in ('I', 'II') else None))
    return out


def _fill_err(rows):
    """无 err 的点用该组误差中位数代替（整组都无误差则 σ=0）；返回 (X,Y,sx,sy) 数组"""
    X = np.array([r[0] for r in rows])
    Y = np.array([r[1] for r in rows])
    sxs = [r[2] for r in rows]
    sys_ = [r[3] for r in rows]
    mx = np.median([s for s in sxs if s is not None]) if any(s is not None for s in sxs) else 0.0
    my = np.median([s for s in sys_ if s is not None]) if any(s is not None for s in sys_) else 0.0
    sx = np.array([s if s is not None else mx for s in sxs])
    sy = np.array([s if s is not None else my for s in sys_])
    return X, Y, sx, sy


def _fit_group(rows, fit_sigma):
    """对一组点做带内禀弥散的最大似然拟合 Y = a·X + b。
    NLL = ½Σ[ ln(2π·V_i) + (Y_i−aX_i−b)²/V_i ]，V_i = σ_int² + σY_i² + a²·σX_i²"""
    N = len(rows)
    if N < 4:
        return {'error': 'insufficient', 'N': N}
    X, Y, sx, sy = _fill_err(rows)

    # 初值：普通 OLS 斜率截距，σ_int = 0.1×std(Y 残差)
    a0, b0 = np.polyfit(X, Y, 1)
    s0 = max(0.1 * float(np.std(Y - (a0 * X + b0))), 1e-3)

    def nll(p):
        a, b = p[0], p[1]
        s = p[2] if fit_sigma else 0.0
        V = np.maximum(s * s + sy * sy + a * a * sx * sx, 1e-12)
        r = Y - a * X - b
        return 0.5 * float(np.sum(np.log(2 * np.pi * V) + r * r / V))

    if fit_sigma:
        res = minimize(nll, [a0, b0, s0], method='L-BFGS-B',
                       bounds=[(None, None), (None, None), (0.0, 10.0)])
        a, b, s = float(res.x[0]), float(res.x[1]), float(res.x[2])
    else:
        res = minimize(nll, [a0, b0], method='L-BFGS-B')
        a, b, s = float(res.x[0]), float(res.x[1]), 0.0

    # 参数误差：最优点处对 (a,b)（σ_int 固定为最优值）中心差分数值 Hessian → 协方差
    def nll_ab(aa, bb):
        V = np.maximum(s * s + sy * sy + aa * aa * sx * sx, 1e-12)
        r = Y - aa * X - bb
        return 0.5 * float(np.sum(np.log(2 * np.pi * V) + r * r / V))

    ha, hb = 1e-4 * max(1.0, abs(a)), 1e-4 * max(1.0, abs(b))
    f0 = nll_ab(a, b)
    H00 = (nll_ab(a + ha, b) - 2 * f0 + nll_ab(a - ha, b)) / ha ** 2
    H11 = (nll_ab(a, b + hb) - 2 * f0 + nll_ab(a, b - hb)) / hb ** 2
    H01 = (nll_ab(a + ha, b + hb) - nll_ab(a + ha, b - hb)
           - nll_ab(a - ha, b + hb) + nll_ab(a - ha, b - hb)) / (4 * ha * hb)
    try:
        C = np.linalg.inv(np.array([[H00, H01], [H01, H11]]))
        a_err = float(np.sqrt(max(C[0, 0], 0.0)))
        b_err = float(np.sqrt(max(C[1, 1], 0.0)))
        cov_ab = float(C[0, 1])
    except np.linalg.LinAlgError:
        a_err = b_err = cov_ab = None
    return {'slope': a, 'intercept': b, 'slope_err': a_err, 'intercept_err': b_err,
            'cov_ab': cov_ab, 'sigma_int': s, 'N': N}


@relations_bp.route('/<name>/fit', methods=['POST'])
@require_auth
def relation_fit(name):
    """带内禀弥散的最大似然拟合。请求体:
    {"points": [{"x":.., "y":.., "xerr":.., "yerr":.., "group":..}, ...],
     "sigma_int": true|false}
    err 为单值、[正,负] 或 null；点由前端筛选后传入，服务端不读库。
    拟合空间按轴定义：log 轴取 log10、线性轴原值（返回 x_log/y_log 告知）。"""
    rel = _REL_BY_NAME.get(name)
    if rel is None:
        abort(404, description=f'未知关系: {name}')
    payload = request.get_json(force=True, silent=True) or {}
    points = payload.get('points') or []
    if not isinstance(points, list) or len(points) > _FIT_MAX_POINTS:
        return jsonify({'error': f'points 非法或超过上限 {_FIT_MAX_POINTS}'}), 400
    fit_sigma = bool(payload.get('sigma_int', True))
    x_log = rel['x'].get('log', True)
    y_log = rel['y'].get('log', True)

    rows = _prepare_xy(points, x_log, y_log)
    groups = {}
    if rel.get('group_by') == 'grb_type':
        for gt in ('I', 'II'):
            groups[gt] = _fit_group([r for r in rows if r[4] == gt], fit_sigma)
    else:
        groups['all'] = _fit_group(rows, fit_sigma)
    return jsonify({'groups': groups, 'x_log': x_log, 'y_log': y_log})
