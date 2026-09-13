"""暂现源 SED 分析接口（结构照抄 routes/hostfit.py）

GET    /api/sed/epochs?transient_id=&min_bands=&dt_frac=  — 历元建议（公开，同步）
POST   /api/sed/build                  — 同步构建 SED（公开）
GET    /api/sed/models                 — 模型清单 + 参数 schema（公开）
POST   /api/sed/jobs                   — 提交拟合任务（需登录）
GET    /api/sed/jobs?transient_id=     — 任务列表（公开）
GET    /api/sed/jobs/<id>              — 任务详情（公开，含 parameters）
GET    /api/sed/jobs/<id>/files/<kind> — 产物文件（公开）
POST   /api/sed/jobs/<id>/stop         — 中断任务（需登录，仅 pending/running）
DELETE /api/sed/jobs/<id>              — 删除任务（仅管理员）
POST   /api/sed/closure                — M3 闭包关系诊断（公开，同步；可选 q 注入指数）
POST   /api/sed/closure_plot           — M3 α–β 诊断图 PNG（公开，同步；可选 q）
GET    /api/sed/closure_relations      — M3 系数表 JSON（公开）
POST   /api/sed/bolometric             — M5 伪玻尔兹曼光变（公开，同步）
"""
import math
import os

from flask import Blueprint, jsonify, request, abort, send_file, Response

from app import get_session, require_auth, require_admin, current_username
from models import Transient, FittingResult
from sedfit import builder as sed_builder
from sedfit import jobs as sed_jobs
from sedfit import laws as sed_laws
from sedfit import models as sed_models
from sedfit import closure as sed_closure
from sedfit import bolometric as sed_bolometric

sedfit_bp = Blueprint('sedfit', __name__)

_FILE_KINDS = {
    'sed_png':    ('sed.png', 'image/png', False),
    'corner':     ('corner.png', 'image/png', False),
    'result':     ('result.json', 'application/json', False),
    'sed_csv':    ('sed.csv', 'text/csv', True),
    'series_csv': ('series.csv', 'text/csv', True),
    'trl_png':    ('trl.png', 'image/png', False),
    'series_png': ('series.png', 'image/png', False),
    'h5':         ('chain_record.h5', 'application/octet-stream', True),
    'log':        ('run.log', 'text/plain', False),
}

_SED_LIKE = 'sed\\_%'


def _sed_query(sess):
    return (sess.query(FittingResult)
            .filter(FittingResult.model_name.like(_SED_LIKE, escape='\\')))


def _job_brief(row):
    ed = row.extra_data or {}
    return {
        'id': row.id,
        'transient_id': row.transient_id,
        'model_name': row.model_name,
        'status': ed.get('status'),
        't_sel': (ed.get('config') or {}).get('t_sel'),
        'chi_squared': row.chi_squared,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _job_detail(row):
    ed = row.extra_data or {}
    d = _job_brief(row)
    d.update({
        'config': ed.get('config'),
        'parameters': row.parameters or {},
        'error': ed.get('error'),
        'runtime_s': ed.get('runtime_s'),
        'dof': ed.get('dof'), 'bic': ed.get('bic'), 'aic': ed.get('aic'),
        'warnings': ed.get('warnings') or [],
        'epochs': ed.get('epochs'),
        'lambda_coverage': ed.get('lambda_coverage'),
        'result_meta': ed.get('result_meta'),
        'derived': ed.get('derived'),
        'created_by': ed.get('created_by'),
        'files': {kind: f'/api/sed/jobs/{row.id}/files/{kind}'
                  for kind in (ed.get('files') or {})},
    })
    return d


def _get_sed_job(sess, job_id):
    row = sess.get(FittingResult, job_id)
    if row is None or not (row.model_name or '').startswith(
            sed_models.MODEL_NAME_PREFIX):
        abort(404, description='任务不存在')
    return row


@sedfit_bp.route('/epochs', methods=['GET'])
def epochs():
    """历元建议：按 (min_bands, dt_frac) 列出可用历元（§3-M1 辅助端点）"""
    transient_id = request.args.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    try:
        min_bands = int(request.args.get('min_bands', 3))
        dt_frac = float(request.args.get('dt_frac', 0.1))
        max_epochs = int(request.args.get('max_epochs', 20))
    except (TypeError, ValueError):
        abort(400, description='min_bands/dt_frac/max_epochs 参数非法')
    sess = get_session()
    try:
        if sess.get(Transient, transient_id) is None:
            abort(404, description=f'暂现源不存在: {transient_id}')
        return jsonify({'transient_id': transient_id,
                        'epochs': sed_builder.suggest_epochs(
                            sess, transient_id, min_bands=min_bands,
                            dt_frac=dt_frac, max_epochs=max_epochs)})
    finally:
        sess.close()


@sedfit_bp.route('/build', methods=['POST'])
def build():
    """M1 同步构建：body {transient_id, t_sel, dt?, mode?, bands?, use_gext?,
    upperlimits?, k_correct?, z_override?}，返回 SED 表 + 元数据"""
    payload = request.get_json(force=True, silent=True) or {}
    transient_id = payload.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    try:
        t_sel = float(payload.get('t_sel'))
    except (TypeError, ValueError):
        abort(400, description='t_sel 必填且为数值（天，相对 t0）')
    if t_sel <= 0:
        abort(400, description='t_sel 必须 > 0')
    sess = get_session()
    try:
        if sess.get(Transient, transient_id) is None:
            abort(404, description=f'暂现源不存在: {transient_id}')
        try:
            result = sed_builder.build_sed(
                sess, transient_id, t_sel, dt=payload.get('dt'),
                mode=payload.get('mode', 'window'),
                bands=payload.get('bands'),
                use_gext=payload.get('use_gext', True),
                upperlimits=payload.get('upperlimits', 'exclude'),
                k_correct=payload.get('k_correct', False),
                z_override=payload.get('z_override'))
        except ValueError as e:
            abort(400, description=str(e))
        return jsonify(result)
    finally:
        sess.close()


@sedfit_bp.route('/models', methods=['GET'])
def model_list():
    """模型清单 + 参数 schema（供前端动态渲染表单）"""
    return jsonify({
        'models': [sed_models.describe(k) for k in sed_models.MODELS],
        'series': {'key': sed_models.SERIES_KEY,
                   'model_name': sed_models.model_name_of(sed_models.SERIES_KEY),
                   'label': '多历元单黑体批量（T/R/L(t) 时间序列）',
                   'config_options': {
                       'epochs': "t_days 列表或 'auto'",
                       'n_epochs': 'auto 时对数均取历元数（默认 12）',
                       'min_bands': 'auto 时历元最少波段数（默认 3）',
                       'series_nsteps': '单历元 MCMC 步数（默认 2000）',
                       'series_nburn': '单历元 burn-in（默认 800）',
                   }},
        'laws': sed_laws.list_laws(),
        'defaults': {'nsteps': 5000, 'nburn': 2000, 'n_workers': 4,
                     'mode': 'window', 'upperlimits': 'exclude',
                     'use_gext': True, 'bandpass': True},
    })


# 采样参数上限（对齐 fitting/engines/vegas_unified.py 的 _NSTEPS_MAX/_NPOOL_MAX：
# 单 worker 串行队列，无界 nsteps 会占死队列数天）
_NSTEPS_MAX = 200000
_NWORKERS_MAX = 8


def _pos_int(config, key):
    """config[key]（若存在）必须为正整数，返回 int 或 None；非法 abort 400"""
    v = config.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) \
            or not math.isfinite(v) or int(v) != v or v < 1:
        abort(400, description=f'{key} 必须为正整数，得到 {v!r}')
    return int(v)


def _validate_sampler(config):
    """采样参数边界校验：nsteps/series_nsteps ≤ _NSTEPS_MAX，n_workers ≤ 8，
    nburn ≥100 且 < nsteps（series 短链放宽到 series_nburn ≥50）。"""
    nsteps = _pos_int(config, 'nsteps')
    nburn = _pos_int(config, 'nburn')
    n_workers = _pos_int(config, 'n_workers')
    s_nsteps = _pos_int(config, 'series_nsteps')
    s_nburn = _pos_int(config, 'series_nburn')
    if nsteps is not None and nsteps > _NSTEPS_MAX:
        abort(400, description=f'nsteps 超过上限 {_NSTEPS_MAX}'
                               '（单 worker 串行队列，过大任务会占死队列）')
    if s_nsteps is not None and s_nsteps > _NSTEPS_MAX:
        abort(400, description=f'series_nsteps 超过上限 {_NSTEPS_MAX}'
                               '（单 worker 串行队列，过大任务会占死队列）')
    if n_workers is not None and n_workers > _NWORKERS_MAX:
        abort(400, description=f'n_workers 超过上限 {_NWORKERS_MAX}')
    eff_nsteps = nsteps if nsteps is not None else 5000
    if nburn is not None:
        if nburn < 100:
            abort(400, description='nburn 须 ≥ 100（burn-in 过短样本不可信）')
        if nburn >= eff_nsteps:
            abort(400, description=f'nburn（{nburn}）须小于 nsteps'
                                   f'（{eff_nsteps}），否则 burn-in 后无样本')
    eff_s_nsteps = s_nsteps if s_nsteps is not None else 2000
    if s_nburn is not None:
        if s_nburn < 50:
            abort(400, description='series_nburn 须 ≥ 50（series 短链下限放宽）')
        if s_nburn >= eff_s_nsteps:
            abort(400, description=f'series_nburn（{s_nburn}）须小于 '
                                   f'series_nsteps（{eff_s_nsteps}）')


def _validate_job(payload, sess):
    """提交校验：源存在、模型已知、采样参数上限、历元 ≥3 有效波段、拒绝全上限历元"""
    transient_id = payload.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    if sess.get(Transient, transient_id) is None:
        abort(404, description=f'暂现源不存在: {transient_id}')

    config = payload.get('config')
    if not isinstance(config, dict):
        config = {k: v for k, v in payload.items() if k != 'transient_id'}
    config = dict(config)

    model_key = config.get('model', 'powerlaw_dust')
    if model_key != sed_models.SERIES_KEY and model_key not in sed_models.MODELS:
        abort(400, description=f'未知模型: {model_key}（可用: '
                               f'{sorted(sed_models.MODELS)} 或 {sed_models.SERIES_KEY}）')
    law = (config.get('law') or 'smc').lower()
    if law not in {l['name'] for l in sed_laws.list_laws()}:
        abort(400, description=f'未知消光律: {law}')

    _validate_sampler(config)
    # bands 显式空列表 = 用户未选任何波段，拒绝提交（区别于缺省 None=不限制）
    if config.get('bands') is not None:
        if not isinstance(config['bands'], list):
            abort(400, description='bands 必须为波段名列表')
        if not config['bands']:
            abort(400, description='至少选择一个波段')

    if model_key == sed_models.SERIES_KEY:
        epochs = config.get('epochs', 'auto')
        if epochs != 'auto':
            if (not isinstance(epochs, list) or not epochs
                    or any(not isinstance(e, (int, float)) or e <= 0
                           for e in epochs)):
                abort(400, description="epochs 必须为正数 t_days 列表或 'auto'")
            config['epochs'] = [float(e) for e in epochs]
        return model_key, config

    # 单历元：实际构建一次做数据校验（同步、毫秒级）
    try:
        t_sel = float(config.get('t_sel'))
    except (TypeError, ValueError):
        abort(400, description='单历元任务 config.t_sel 必填且为数值（天）')
    if t_sel <= 0:
        abort(400, description='t_sel 必须 > 0')
    try:
        sed = sed_builder.build_sed(
            sess, transient_id, t_sel, dt=config.get('dt'),
            mode=config.get('mode', 'window'), bands=config.get('bands'),
            use_gext=config.get('use_gext', True),
            upperlimits=config.get('upperlimits', 'exclude'),
            z_override=config.get('z_override'))
    except ValueError as e:
        abort(400, description=str(e))
    det = [p for p in sed['points'] if not p['is_ul']]
    if not det:
        abort(400, description='该历元全部为上限点，无法拟合')
    if len({p['band'] for p in det}) < 3:
        abort(400, description=f'有效探测波段不足 3 个（当前 '
                               f'{len({p["band"] for p in det})} 个）')
    # 自由度预检：探测点数必须多于自由参数数（黑体参数集随 z 有无变化，
    # 用构建结果的实际 z 组装临时 config 计算）
    cfg = dict(config)
    cfg['z'] = sed.get('z')
    n_free = len(sed_models.MODELS[model_key].param_defs(cfg))
    if len(det) < n_free + 1:
        abort(400, description=f'探测点不足：{len(det)} 个点 vs {n_free} 个自由参数'
                               '（自由度为 0，无法拟合）；请改用 GP 内插模式、'
                               '扩大时段窗口或加波段')
    return model_key, config


@sedfit_bp.route('/jobs', methods=['POST'])
@require_auth
def submit_job():
    payload = request.get_json(force=True, silent=True) or {}
    sess = get_session()
    try:
        model_key, config = _validate_job(payload, sess)
        transient_id = payload['transient_id']
    finally:
        sess.close()
    job_id = sed_jobs.create_job(transient_id, model_key, config,
                                 created_by=current_username())
    return jsonify({'id': job_id, 'transient_id': transient_id,
                    'model_name': sed_models.model_name_of(model_key),
                    'status': 'pending'}), 201


@sedfit_bp.route('/jobs', methods=['GET'])
def list_jobs():
    transient_id = request.args.get('transient_id')
    sess = get_session()
    try:
        q = _sed_query(sess)
        if transient_id:
            q = q.filter_by(transient_id=transient_id)
        rows = q.order_by(FittingResult.id.desc()).all()
        return jsonify([_job_brief(r) for r in rows])
    finally:
        sess.close()


@sedfit_bp.route('/jobs/<int:job_id>', methods=['GET'])
def job_detail(job_id):
    sess = get_session()
    try:
        return jsonify(_job_detail(_get_sed_job(sess, job_id)))
    finally:
        sess.close()


@sedfit_bp.route('/jobs/<int:job_id>/files/<kind>', methods=['GET'])
def job_file(job_id, kind):
    if kind not in _FILE_KINDS:
        abort(404, description=f'未知文件类型: {kind}')
    sess = get_session()
    try:
        row = _get_sed_job(sess, job_id)
        transient_id = row.transient_id
        files = (row.extra_data or {}).get('files') or {}
    finally:
        sess.close()
    if kind not in files:
        # run.log 在任务开始即写入（含失败任务），未登记时按磁盘存在性兜底
        if not (kind == 'log' and os.path.exists(
                os.path.join(sed_jobs.job_dir(transient_id, job_id), 'run.log'))):
            abort(404, description='该产物不存在（任务未完成或生成失败）')
    rel, mimetype, as_attachment = _FILE_KINDS[kind]
    path = os.path.join(sed_jobs.job_dir(transient_id, job_id), rel)
    if not os.path.exists(path):
        abort(404, description='文件已丢失')
    return send_file(path, mimetype=mimetype,
                     as_attachment=as_attachment,
                     download_name=os.path.basename(rel))


@sedfit_bp.route('/jobs/<int:job_id>/stop', methods=['POST'])
@require_auth
def stop_job(job_id):
    """中断进行中（pending/running）的 SED 任务：即时翻转状态为 interrupted，
    采样协程式退出，不续算、产物文件保留"""
    sess = get_session()
    try:
        _get_sed_job(sess, job_id)   # 不存在/非 sed 任务 → 404
    finally:
        sess.close()
    if not sed_jobs.stop_job(job_id):
        return jsonify({'error': '任务已结束，无法中断'}), 409
    return jsonify({'status': 'ok', 'id': job_id})


@sedfit_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
@require_admin
def remove_job(job_id):
    ok, msg = sed_jobs.delete_job(job_id)
    if not ok:
        code = 404 if msg == '任务不存在' else 400
        return jsonify({'error': msg}), code
    return jsonify({'status': 'ok', 'message': msg})


# ─── M3: 闭包关系诊断（纯计算，同步公开） ───

def _parse_closure_payload():
    """解析/校验 {alpha, alpha_err, beta, beta_err, q?}，非法即 abort 400。
    q 为可选能量注入指数（L∝t^q，0≤q<1），缺省 None（注入条目不参与）。"""
    payload = request.get_json(force=True, silent=True) or {}
    try:
        alpha = float(payload.get('alpha'))
        beta = float(payload.get('beta'))
    except (TypeError, ValueError):
        abort(400, description='alpha 与 beta 必填且为数值')
    try:
        alpha_err = float(payload.get('alpha_err', 0.0) or 0.0)
        beta_err = float(payload.get('beta_err', 0.0) or 0.0)
    except (TypeError, ValueError):
        abort(400, description='alpha_err/beta_err 必须为数值')
    if not all(math.isfinite(v) for v in (alpha, beta, alpha_err, beta_err)):
        abort(400, description='alpha/beta 及误差必须为有限数值')
    if alpha_err < 0 or beta_err < 0:
        abort(400, description='误差必须 ≥ 0')
    q = payload.get('q')
    if q is not None:
        try:
            q = float(q)
        except (TypeError, ValueError):
            abort(400, description='q（能量注入指数）必须为数值')
        if not math.isfinite(q) or not 0.0 <= q < 1.0:
            abort(400, description=f'q（能量注入指数）必须满足 0 ≤ q < 1，得到 {q}')
    return alpha, alpha_err, beta, beta_err, q


@sedfit_bp.route('/closure', methods=['POST'])
def closure_diagnose():
    """M3 闭包关系诊断：body {alpha, alpha_err, beta, beta_err, q?} → 排名表"""
    alpha, alpha_err, beta, beta_err, q = _parse_closure_payload()
    return jsonify(sed_closure.diagnose(alpha, alpha_err, beta, beta_err, q=q))


@sedfit_bp.route('/closure_plot', methods=['POST'])
def closure_plot():
    """M3 α–β 诊断图：同 closure 的 body → PNG（image/png）"""
    alpha, alpha_err, beta, beta_err, q = _parse_closure_payload()
    png = sed_closure.make_plot(alpha, alpha_err, beta, beta_err, q=q)
    return Response(png, mimetype='image/png')


@sedfit_bp.route('/closure_relations', methods=['GET'])
def closure_relations():
    """M3 系数表 JSON（供前端审校展示）"""
    return jsonify(sed_closure.load_table())


# ─── M5: 伪玻尔兹曼光变（同步公开，max_epochs 上限保护） ───

@sedfit_bp.route('/bolometric', methods=['POST'])
def bolometric():
    """M5 伪玻尔兹曼光变：body {transient_id, epochs?, mode?, dt_frac?,
    z_override?, bc_sample?, max_epochs?}。epochs 为 t_days 列表，缺省 auto。"""
    payload = request.get_json(force=True, silent=True) or {}
    transient_id = payload.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    epochs = payload.get('epochs')
    if epochs is not None:
        if (not isinstance(epochs, list) or not epochs
                or any(not isinstance(e, (int, float)) or e <= 0
                       for e in epochs)):
            abort(400, description='epochs 必须为非空正数 t_days 列表')
    try:
        dt_frac = float(payload.get('dt_frac', 0.1))
        max_epochs = int(payload.get('max_epochs', 30))
    except (TypeError, ValueError):
        abort(400, description='dt_frac/max_epochs 参数非法')
    if dt_frac <= 0:
        abort(400, description='dt_frac 必须 > 0')
    max_epochs = max(1, min(max_epochs, 30))  # 每历元含网格拟合，硬性上限 30
    sess = get_session()
    try:
        if sess.get(Transient, transient_id) is None:
            abort(404, description=f'暂现源不存在: {transient_id}')
        try:
            result = sed_bolometric.pseudo_bolometric(
                sess, transient_id, epochs=epochs,
                mode=payload.get('mode', 'window'), dt_frac=dt_frac,
                z_override=payload.get('z_override'),
                bc_sample=payload.get('bc_sample', 'all'),
                max_epochs=max_epochs)
        except ValueError as e:
            abort(400, description=str(e))
        return jsonify(result)
    finally:
        sess.close()
