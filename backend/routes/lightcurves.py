"""
光变曲线 CRUD
GET    /api/lightcurves?transient_id=X    — 列表（支持筛选）
POST   /api/lightcurves/fit_model         — 光变时变函数拟合（需登录）
POST   /api/lightcurves                    — 批量添加
PUT    /api/lightcurves/<id>               — 更新单条
DELETE /api/lightcurves/<id>               — 删除单条
DELETE /api/lightcurves?transient_id=X     — 删除某源所有光变
"""
from flask import Blueprint, request, jsonify, session
from app import get_session, require_auth, require_admin, current_username
from models import Lightcurve
import extinction

lightcurves_bp = Blueprint('lightcurves', __name__)

# 支持排序的列（白名单）
LC_SORTABLE = {'id', 'time', 'time_err', 'band', 'flux_density', 'flux_density_err',
               'telescope', 'instrument', 'created_at', 'updated_at'}


@lightcurves_bp.route('/fit_model', methods=['POST'])
@require_auth
def fit_model():
    """光变曲线时变函数拟合（线性 mJy 空间加权最小二乘）

    请求体: {"model": "pl"|"bpl"|"sbpl"|"fred", "points": [{"t":.., "f":.., "ferr":..|null}, ...],
             "bounds": {"tb": [lo, hi]}  — 可选，bpl/sbpl 拐点预设范围（秒，与数据范围取交集）}
      pl : F(t) = A * t^(-alpha)
      bpl: F(t) = A * t^(-alpha1)                (t <= tb)
                  A * tb^(alpha2-alpha1) * t^(-alpha2)  (t > tb)，tb 处连续
      sbpl: F(t) = Fb * [ (t/tb)^(n*alpha1) + (t/tb)^(n*alpha2) ]^(-1/n)
                  平滑断裂幂律，n>0 为平滑因子（越大越尖锐；n→∞ 退化为 bpl），
                  A = Fb * tb^alpha1 与 bpl 归一化约定一致
      fred: F(x) = A * exp(2μ) * exp(-τ1/(x+μ-x1) - (x+μ-x1)/τ2)，μ=(τ1/τ2)^(1/2)
                  Norris et al. 2005 脉冲形（A 即峰值强度，峰值位于 x = x1+(τ1τ2)^(1/2)-μ）；
                  定义域 x+μ-x1>0，域外取 0；A 为振幅，τ1/τ2 为上升/下降时标，x1 为起始时间（秒）
    返回: {"params": {...}, "param_errors": {...}|null,
           "param_cov": {"keys": [...], "matrix": [[...]]}|null, "N": n}
      param_cov 为输出参数基下的完整协方差（键序见 keys），供前端传播 1σ 置信带
    """
    import numpy as np
    from scipy.optimize import least_squares

    body = request.get_json(force=True) or {}
    model = body.get('model')
    if model not in ('pl', 'bpl', 'sbpl', 'fred'):
        return {'error': 'model must be "pl", "bpl", "sbpl" or "fred"'}, 400
    raw_points = body.get('points') or []
    t, f, sig = [], [], []
    for p in raw_points:
        try:
            ti = float(p.get('t'))
            fi = float(p.get('f'))
        except (TypeError, ValueError, AttributeError):
            continue
        if not (np.isfinite(ti) and np.isfinite(fi)) or ti <= 0 or fi <= 0:
            continue
        fe = p.get('ferr')
        try:
            fe = float(fe) if fe is not None else None
        except (TypeError, ValueError):
            fe = None
        t.append(ti)
        f.append(fi)
        # 有 ferr 的点按 1/ferr 加权，无 ferr 的点 sigma=1（不加权）
        sig.append(fe if (fe is not None and np.isfinite(fe) and fe > 0) else 1.0)
    need = 3 if model == 'pl' else (6 if model == 'sbpl' else 5)
    if len(t) < need:
        return {'error': 'insufficient'}, 400
    t = np.asarray(t)
    f = np.asarray(f)
    sig = np.asarray(sig)

    # 拐点 tb 的预设范围（可选），与数据范围取交集
    tb_lo, tb_hi = float(t.min()), float(t.max())
    req_bounds = body.get('bounds') or {}
    if model in ('bpl', 'sbpl') and isinstance(req_bounds, dict):
        tb_rng = req_bounds.get('tb')
        if isinstance(tb_rng, (list, tuple)) and len(tb_rng) == 2:
            try:
                lo, hi = float(tb_rng[0]), float(tb_rng[1])
                if np.isfinite(lo) and np.isfinite(hi) and lo < hi:
                    tb_lo, tb_hi = max(tb_lo, lo), min(tb_hi, hi)
            except (TypeError, ValueError):
                pass

    # 以拐点/参考时刻为锚的再参数化（避免 A=F(t=1s) 与 tb 的巨大动态范围导致病态）：
    #   pl : F = Fref * (t/tref)^(-alpha)          —— Fref 为 tref 处流量
    #   bpl: F = Fb * (t/tb)^(-a1) (t<=tb)；Fb * (t/tb)^(-a2) (t>tb) —— Fb 为拐点流量
    #   sbpl: F = Fb * [(t/tb)^(n*a1) + (t/tb)^(n*a2)]^(-1/n)，对数空间求和保证数值稳定
    # 与返回给前端的 A·t^(-a) 形式数学等价，最后换算回 A 即可
    tref = float(np.sqrt(t.min() * t.max()))  # 几何中点

    def pl_flux_r(p, x):
        Fref, alpha = p
        return Fref * np.power(x / tref, -alpha)

    def bpl_flux_r(p, x):
        Fb, a1, a2, tb = p
        r = np.empty_like(x)
        m1 = x <= tb
        r[m1] = Fb * np.power(x[m1] / tb, -a1)
        r[~m1] = Fb * np.power(x[~m1] / tb, -a2)
        return r

    def sbpl_flux_r(p, x):
        Fb, a1, a2, tb, n = p
        lnx = np.log(x / tb)
        # log( e^(n*a1*lnx) + e^(n*a2*lnx) ) = logaddexp，避免幂运算上溢
        return Fb * np.exp(-np.logaddexp(n * a1 * lnx, n * a2 * lnx) / n)

    # fred：μ=(τ1/τ2)^(1/2) 时 exp(2μ) 归一化使 A 即峰值强度（Norris 2005 eq.1），
    # 指数合并为一项求值；定义域 u=x+μ-x1>0，域外取 0
    def fred_flux_r(p, x):
        A, t1, t2, x1 = p
        mu = np.sqrt(t1 / t2)
        u = x + mu - x1
        r = np.zeros_like(x)
        m = u > 0
        r[m] = A * np.exp(2.0 * mu - t1 / u[m] - u[m] / t2)
        return r

    flux_func = {'pl': pl_flux_r, 'bpl': bpl_flux_r, 'sbpl': sbpl_flux_r, 'fred': fred_flux_r}[model]

    def resid(p):
        with np.errstate(over='ignore', invalid='ignore'):
            r = (flux_func(p, t) - f) / sig
        # 探索过程中产生的 inf/nan 残差替换为大有限值并钳幅，避免求解器卡死/协方差溢出
        return np.clip(np.where(np.isfinite(r), r, 1e60), -1e60, 1e60)

    if model == 'pl':
        fref0 = float(np.interp(np.log(tref), np.log(np.sort(t)), f[np.argsort(t)]))
        x0 = [max(fref0, 1e-300), 1.0]
        bounds = ([1e-300, -30.0], [np.inf, 30.0])
        keys = ('A', 'alpha')
    elif model == 'bpl':
        ipeak = int(np.argmax(f))
        tb0 = min(max(float(t[ipeak]), tb_lo), tb_hi)
        x0 = [float(f[ipeak]), -1.0, 1.0, tb0]
        bounds = ([1e-300, -30.0, -30.0, tb_lo],
                  [np.inf, 30.0, 30.0, tb_hi])
        keys = ('A', 'alpha1', 'alpha2', 'tb')
    elif model == 'sbpl':
        ipeak = int(np.argmax(f))
        tb0 = min(max(float(t[ipeak]), tb_lo), tb_hi)
        x0 = [float(f[ipeak]), -1.0, 1.0, tb0, 3.0]
        bounds = ([1e-300, -30.0, -30.0, tb_lo, 0.05],
                  [np.inf, 30.0, 30.0, tb_hi, 100.0])
        keys = ('A', 'alpha1', 'alpha2', 'tb', 'n')
    else:
        # fred 初值启发：A≈最大流量（A 即峰值强度），x1 使峰值落在流量最大点，
        # τ1/τ2 取峰前/峰后时间跨度的 1/2 量级
        span = max(float(t.max() - t.min()), 1e-6)
        tau_hi = max(100.0 * span, 10.0)
        ipeak = int(np.argmax(f))
        tpeak = float(t[ipeak])
        tau1_0 = min(max((tpeak - float(t.min())) / 2.0, 1e-6), tau_hi)
        tau2_0 = min(max((float(t.max()) - tpeak) / 2.0, 1e-6), tau_hi)
        # 峰值位于 x = x1 + (τ1τ2)^(1/2) - (τ1/τ2)^(1/2)，反推 x1 使峰值=t_peak
        x1_0 = tpeak - (float(np.sqrt(tau1_0 * tau2_0)) - float(np.sqrt(tau1_0 / tau2_0)))
        x0 = [float(f[ipeak]), tau1_0, tau2_0, x1_0]
        bounds = ([1e-300, 1e-6, 1e-6, float(t.min()) - 10.0 * span],
                  [np.inf, tau_hi, tau_hi, float(t.max())])
        keys = ('A', 'tau1', 'tau2', 'x1')
    try:
        res = least_squares(resid, x0, bounds=bounds, max_nfev=20000, x_scale='jac')
    except Exception as e:
        return {'error': f'fit failed: {e}'}, 400
    if not res.success:
        return {'error': f'fit failed: {res.message}'}, 400

    # 换算回前端约定的形式：F = A * t^(-alpha)；bpl/sbpl 为 A = Fb * tb^a1；fred 无需换算
    if model == 'pl':
        Fref, alpha = res.x
        conv = [Fref * tref ** alpha, alpha]
    elif model == 'bpl':
        Fb, a1, a2, tb = res.x
        conv = [Fb * tb ** a1, a1, a2, tb]
    elif model == 'sbpl':
        Fb, a1, a2, tb, n = res.x
        conv = [Fb * tb ** a1, a1, a2, tb, n]
    else:
        conv = list(res.x)  # fred：内部参数即输出参数
    params = dict(zip(keys, (float(v) for v in conv)))
    # Jacobian 近似协方差：cov = (J^T J)^-1 * chi2/dof，再经链式法则变换到输出参数
    errors = None
    param_cov = None
    try:
        J = np.atleast_2d(res.jac)
        _, svals, VT = np.linalg.svd(J, full_matrices=False)
        if svals.size and svals[0] > 0:
            threshold = np.finfo(float).eps * max(J.shape) * svals[0]
            good = svals > threshold
            if good.all():
                cov = np.dot(VT.T / svals ** 2, VT)
                dof = max(1, len(res.fun) - len(res.x))
                cov = cov * (2.0 * res.cost / dof)
                # 内部参数 → 输出参数 的变换 Jacobian G：cov_out = G·cov·Gᵀ
                if model == 'pl':
                    A_out = conv[0]
                    G = np.array([[tref ** alpha, A_out * np.log(tref)],
                                  [0.0, 1.0]])
                elif model == 'bpl':
                    A_out, a1_, a2_, tb_ = conv
                    G = np.array([[tb_ ** a1_, A_out * np.log(tb_), 0.0, Fb * a1_ * tb_ ** (a1_ - 1.0)],
                                  [0.0, 1.0, 0.0, 0.0],
                                  [0.0, 0.0, 1.0, 0.0],
                                  [0.0, 0.0, 0.0, 1.0]])
                elif model == 'sbpl':
                    A_out, a1_, a2_, tb_, n_ = conv
                    G = np.array([[tb_ ** a1_, A_out * np.log(tb_), 0.0, Fb * a1_ * tb_ ** (a1_ - 1.0), 0.0],
                                  [0.0, 1.0, 0.0, 0.0, 0.0],
                                  [0.0, 0.0, 1.0, 0.0, 0.0],
                                  [0.0, 0.0, 0.0, 1.0, 0.0],
                                  [0.0, 0.0, 0.0, 0.0, 1.0]])
                else:
                    G = np.eye(4)  # fred：无再参数化
                cov_out = G @ cov @ G.T
                if np.isfinite(cov_out).all():
                    param_cov = {'keys': list(keys), 'matrix': cov_out.tolist()}
                errs = np.sqrt(np.diag(cov_out))
                if np.isfinite(errs).all():
                    errors = dict(zip(keys, (float(v) for v in errs)))
    except Exception:
        errors = None
        param_cov = None
    return jsonify({'params': params, 'param_errors': errors, 'param_cov': param_cov, 'N': int(len(t))})


@lightcurves_bp.route('', methods=['GET'])
def list_lightcurves():
    sess = get_session()
    try:
        q = sess.query(Lightcurve)
        tid = request.args.get('transient_id')
        if tid:
            q = q.filter(Lightcurve.transient_id == tid)
        band = request.args.get('band')
        if band:
            q = q.filter(Lightcurve.band == band)
        telescope = request.args.get('telescope')
        if telescope:
            q = q.filter(Lightcurve.telescope.ilike(f'%{telescope}%'))
        # 排序（白名单列，防止 getattr 取到关系等非列属性导致 500）
        sort = request.args.get('sort', 'time')
        if sort not in LC_SORTABLE:
            sort = 'time'
        order = request.args.get('order', 'asc')
        col = getattr(Lightcurve, sort)
        q = q.order_by(col.desc() if order == 'desc' else col)
        # 分页（非法值回退默认；per_page 上限与 transients 列表一致）
        try:
            page = max(1, int(request.args.get('page', 1)))
            per_page = min(10000, max(1, int(request.args.get('per_page', 500))))
        except ValueError:
            page, per_page = 1, 500
        total = q.count()
        items = q.offset((page - 1) * per_page).limit(per_page).all()
        return jsonify({
            'total': total,
            'page': page,
            'per_page': per_page,
            'items': [r.to_dict() for r in items],
        })
    finally:
        sess.close()


@lightcurves_bp.route('/batch', methods=['POST'])
@require_auth
def batch_create():
    """批量添加光变数据；source 自动记录为当前登录账户"""
    body = request.get_json(force=True)
    if not isinstance(body, list):
        body = [body]
    sess = get_session()
    try:
        records = []
        for item in body:
            if 'transient_id' not in item:
                return {'error': 'transient_id is required for each item'}, 400
            lc = Lightcurve(transient_id=item['transient_id'])
            _apply_lc_fields(lc, item)
            lc.source = current_username()  # 网页录入：自动记录提交账户
            sess.add(lc)
            records.append(lc)
        sess.commit()
        return jsonify({'created': len(records), 'items': [r.to_dict() for r in records]}), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@lightcurves_bp.route('/<int:lc_id>', methods=['PUT'])
@require_auth
def update_lightcurve(lc_id):
    sess = get_session()
    body = request.get_json(force=True)
    try:
        lc = sess.query(Lightcurve).filter(Lightcurve.id == lc_id).first()
        if not lc:
            return {'error': 'Not found'}, 404
        # 权限：管理员可改任意记录；普通用户可改自己录入的记录（source = 本账户），
        # 对他人录入的记录仅可切换 discard（扣点）
        if session.get('role') != 'admin':
            own = lc.source is not None and lc.source == current_username()
            if not own and set(body.keys()) - {'discard'}:
                return {'error': '普通用户仅可修改自己录入的记录；他人记录仅可切换 discard（扣点）'}, 403
        _apply_lc_fields(lc, body)
        # 已做银消改正的数据点随变动自动重算（条件不满足则清除）
        extinction.recompute_point(sess, lc)
        sess.commit()
        return jsonify(lc.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@lightcurves_bp.route('/<int:lc_id>', methods=['DELETE'])
@require_admin
def delete_lightcurve(lc_id):
    sess = get_session()
    try:
        lc = sess.query(Lightcurve).filter(Lightcurve.id == lc_id).first()
        if not lc:
            return {'error': 'Not found'}, 404
        sess.delete(lc)
        sess.commit()
        return {'status': 'deleted', 'id': lc_id}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@lightcurves_bp.route('', methods=['DELETE'])
@require_admin
def delete_by_transient():
    """通过 transient_id 删除所有光变数据"""
    tid = request.args.get('transient_id')
    if not tid:
        return {'error': 'transient_id required'}, 400
    sess = get_session()
    try:
        deleted = sess.query(Lightcurve).filter(Lightcurve.transient_id == tid).delete()
        sess.commit()
        return {'status': 'deleted', 'transient_id': tid, 'count': deleted}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


# 必填字段（数据库 NOT NULL）不允许清空；其余字段传 null/空字符串即清空
LC_REQUIRED_FLOATS = ('time', 'flux_density')
LC_NULLABLE_FLOATS = ('time_err', 'flux_density_err', 'gext_Alambda',
                      'mag_gextcor', 'mag_gextcor_err',
                      'flux_density_gextcor', 'flux_density_gextcor_err', 'weights')
LC_REQUIRED_STRS = ('band', 'flux_density_unit')
LC_NULLABLE_STRS = ('time_unit', 'mag_system', 'flux_density_gextcor_unit',
                    'telescope', 'instrument', 'reference', 'comment')


def _apply_lc_fields(lc, body):
    """约定：传 null 或空字符串 = 清空（仅限可空字段）；不传 = 保持不变。"""
    for field in LC_REQUIRED_FLOATS + LC_NULLABLE_FLOATS:
        if field in body:
            v = body[field]
            if v in (None, ''):
                if field in LC_NULLABLE_FLOATS:
                    setattr(lc, field, None)
            else:
                setattr(lc, field, float(v))
    for field in LC_REQUIRED_STRS + LC_NULLABLE_STRS:
        if field in body:
            v = body[field]
            if v in (None, ''):
                if field in LC_NULLABLE_STRS:
                    setattr(lc, field, None)
            else:
                setattr(lc, field, str(v))
    for bool_field in ('gext_corr', 'upperlimit', 'discard', 'host_subtracted'):
        if bool_field in body:
            v = body[bool_field]
            # host_subtracted 允许传 null 表示「未知」；其余布尔列传 null = 保持不变
            if v is None:
                if bool_field == 'host_subtracted':
                    lc.host_subtracted = None
            else:
                setattr(lc, bool_field, bool(v))
    if 'extra_data' in body and isinstance(body['extra_data'], dict):
        # 浅合并；必须构造新 dict，否则 JSONB 原地修改不被 SQLAlchemy 追踪、不会落库
        merged = dict(lc.extra_data or {})
        merged.update(body['extra_data'])
        lc.extra_data = merged
