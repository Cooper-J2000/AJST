"""
统计关系接口（基于 extra_data.derived 派生量）
GET  /api/relations               — 关系定义列表 + 各关系可用来源目录
GET  /api/relations/<name>/data   — 取数点（source=best 或目录短名）
POST /api/relations/<name>/fit    — 带内禀弥散的最大似然拟合（linmix / Kelly 2007 风格）
"""
import json, math, os
import numpy as np
from scipy.optimize import minimize
from flask import Blueprint, jsonify, request, abort
from app import get_session
from models import Transient

relations_bp = Blueprint('relations', __name__)

_REL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'relations.json')
with open(_REL_PATH, encoding='utf-8') as f:
    _RELATIONS = json.load(f)['relations']
_REL_BY_NAME = {r['name']: r for r in _RELATIONS}


def _load_derived():
    """读出所有源的 derived（顺带一级列），返回 [(t, derived), ...]"""
    sess = get_session()
    try:
        rows = []
        for t in sess.query(Transient).all():
            d = (t.extra_data or {}).get('derived')
            if d:
                rows.append((t, d))
        return rows
    finally:
        sess.close()


@relations_bp.route('', methods=['GET'])
def list_relations():
    """关系定义 + 每个关系可用的来源目录（扫描库内 derived 统计）"""
    rows = _load_derived()
    sources = {}
    for rel in _RELATIONS:
        xk, yk = rel['x']['key'], rel['y']['key']
        cats = set()
        for _, d in rows:
            if d.get('best', {}).get(xk) and d.get('best', {}).get(yk):
                cats.add('best')
            for cat, sd in (d.get('sources') or {}).items():
                if sd.get(xk) and sd.get(yk):
                    cats.add(cat)
        sources[rel['name']] = sorted(cats, key=lambda c: (c != 'best', c))
    return jsonify({'relations': _RELATIONS, 'sources': sources})


@relations_bp.route('/<name>/data', methods=['GET'])
def relation_data(name):
    """取数点。source=best（默认）用 derived.best，否则用 derived.sources[source]。
    只返回 x、y 都有值的点；log 轴要求对应值为正。"""
    rel = _REL_BY_NAME.get(name)
    if rel is None:
        abort(404, description=f'未知关系: {name}')
    source = request.args.get('source', 'best')
    xk, yk = rel['x']['key'], rel['y']['key']
    x_log = rel.get('x_log', True)
    y_log = rel.get('y_log', True)

    points = []
    for t, d in _load_derived():
        if source == 'best':
            bx, by = d.get('best', {}).get(xk), d.get('best', {}).get(yk)
            if not bx or not by:
                continue
            x, y = bx.get('v'), by.get('v')
            xerr, yerr = bx.get('err'), by.get('err')
            src = {'x': bx.get('src'), 'y': by.get('src')}
        else:
            sd = (d.get('sources') or {}).get(source)
            if not sd:
                continue
            qx, qy = sd.get(xk), sd.get(yk)
            if not isinstance(qx, dict) or not isinstance(qy, dict):
                continue
            x, y = qx.get('v'), qy.get('v')
            xerr, yerr = qx.get('err'), qy.get('err')
            src = source
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        # log 轴剔除非正值
        if x_log and x <= 0:
            continue
        if y_log and y <= 0:
            continue
        gt = (d.get('grb_type') or {}).get('v')
        points.append({
            'id': t.id, 'x': x, 'xerr': xerr, 'y': y, 'yerr': yerr,
            'grb_type': gt, 'tags': t.tags or [], 'sub_tag': t.sub_tag or [],
            'z': t.redshift, 'src': src,
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


def _prepare_xy(points, x_log):
    """把请求点变换到拟合空间：y 一律 log10，x 按 x_log 决定；返回 [(X,Y,sx,sy,grb_type), ...]"""
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        x, y = _as_num(p.get('x')), _as_num(p.get('y'))
        if x is None or y is None or y <= 0:
            continue
        if x_log and x <= 0:
            continue  # log 轴剔除非正值
        Y = math.log10(y)
        sy = _log_err(y, p.get('yerr'))
        if x_log:
            X = math.log10(x)
            sx = _log_err(x, p.get('xerr'))
        else:
            X = x
            sx = _lin_err(p.get('xerr'))
        gt = p.get('grb_type')
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
def relation_fit(name):
    """带内禀弥散的最大似然拟合。请求体:
    {"points": [{"x":.., "y":.., "xerr":.., "yerr":.., "grb_type":..}, ...],
     "sigma_int": true|false}
    err 为单值、[正,负] 或 null；点由前端筛选后传入，服务端不读库。"""
    rel = _REL_BY_NAME.get(name)
    if rel is None:
        abort(404, description=f'未知关系: {name}')
    payload = request.get_json(force=True, silent=True) or {}
    points = payload.get('points') or []
    if not isinstance(points, list) or len(points) > _FIT_MAX_POINTS:
        return jsonify({'error': f'points 非法或超过上限 {_FIT_MAX_POINTS}'}), 400
    fit_sigma = bool(payload.get('sigma_int', True))
    x_log = rel.get('x_log', True)

    rows = _prepare_xy(points, x_log)
    groups = {}
    if rel.get('group_by_type'):
        for gt in ('I', 'II'):
            groups[gt] = _fit_group([r for r in rows if r[4] == gt], fit_sigma)
    else:
        groups['all'] = _fit_group(rows, fit_sigma)
    return jsonify({'groups': groups, 'x_log': x_log})
