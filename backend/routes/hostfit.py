"""宿主星系 pcigale 拟合接口
GET    /api/hostfit/config                — 默认网格/模块/可用波段（公开）
POST   /api/hostfit/jobs                  — 提交拟合任务（需登录）
GET    /api/hostfit/jobs?transient_id=    — 任务列表（公开）
GET    /api/hostfit/jobs/<id>             — 任务详情（公开，含 parameters）
GET    /api/hostfit/jobs/<id>/files/<kind>— 产物（results | sed_png | best_model | log）
DELETE /api/hostfit/jobs/<id>             — 删除任务（仅管理员）
"""
import os

from flask import Blueprint, jsonify, request, abort, send_file

from app import get_session, require_auth, require_admin, current_username
from models import Transient, FilterDef, FittingResult
from hostfit import jobs as hostfit_jobs

hostfit_bp = Blueprint('hostfit', __name__)

_FILE_KINDS = {
    'results':    (os.path.join('out', 'results.txt'), 'text/plain', False),
    'sed_png':    ('sed.png', 'image/png', False),
    'best_model': (os.path.join('out', 'host_best_model.fits'),
                   'application/octet-stream', True),
    'log':        ('run.log', 'text/plain', False),
}

_DEFAULTS = {
    'tau_main': [1000, 3000, 5000],
    'age_main': [100, 1000, 5000],
    'Av_ISM': [0.0, 0.3, 1.0],
    'z_min': 0.0, 'z_max': 2.0, 'z_step': 0.05,
}
_MODULES_BASE = 'sfhdelayed+bc03+dustatt_modified_CF00+redshifting'
# 网页端可勾选的可选模块（config 键 → pcigale 模块名）
_OPTIONAL_MODULES = {'use_nebular': 'nebular', 'use_dl2014': 'dl2014'}

_MAG_SYSTEMS = ('ab', 'vega', 'st', 'stmag', '', None)


def _job_brief(row):
    ed = row.extra_data or {}
    return {
        'id': row.id,
        'transient_id': row.transient_id,
        'status': ed.get('status'),
        'mode': (ed.get('config') or {}).get('mode'),
        'chi_squared': row.chi_squared,
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _job_detail(row):
    ed = row.extra_data or {}
    d = _job_brief(row)
    d.update({
        'model_name': row.model_name,
        'config': ed.get('config'),
        'parameters': row.parameters or {},
        'error': ed.get('error'),
        'runtime_s': ed.get('runtime_s'),
        'warnings': ed.get('warnings') or [],
        'created_by': ed.get('created_by'),
        'files': {kind: f'/api/hostfit/jobs/{row.id}/files/{kind}'
                  for kind in (ed.get('files') or {})},
    })
    return d


@hostfit_bp.route('/config', methods=['GET'])
def get_config():
    """默认拟合网格 + 固定模块说明 + 有 pcigale_name 的可用波段列表"""
    sess = get_session()
    try:
        bands = sorted(f.id for f in sess.query(FilterDef).all()
                       if (f.extra_data or {}).get('pcigale_name'))
    finally:
        sess.close()
    return jsonify({'defaults': _DEFAULTS, 'modules': _MODULES_BASE,
                    'optional_modules': _OPTIONAL_MODULES,
                    'available_bands': bands})


def _validate(payload, sess):
    """校验提交体，返回 (config, error_response)。

    payload = {transient_id, config: {...}}；config 也可平铺（兼容）。
    """
    transient_id = payload.get('transient_id')
    if not transient_id:
        abort(400, description='缺少 transient_id')
    if sess.get(Transient, transient_id) is None:
        abort(404, description=f'暂现源不存在: {transient_id}')

    config = payload.get('config')
    if not isinstance(config, dict):
        config = {k: v for k, v in payload.items() if k != 'transient_id'}

    errors = []
    mode = config.get('mode', 'fixed')
    if mode not in ('fixed', 'photoz'):
        errors.append("mode 必须为 'fixed' 或 'photoz'")

    if mode == 'fixed':
        z = config.get('redshift')
        try:
            z = float(z)
            if z <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("mode='fixed' 时 redshift 必填且 > 0")

    grid = config.get('grid') or {}
    for key in ('tau_main', 'age_main', 'Av_ISM'):
        vals = grid.get(key)
        if (not isinstance(vals, list) or not vals
                or any(not isinstance(v, (int, float)) for v in vals)):
            errors.append(f'grid.{key} 必须是非空数值数组')
    if mode == 'photoz':
        try:
            z_min, z_max = float(grid['z_min']), float(grid['z_max'])
            z_step = float(grid['z_step'])
            if not (z_max > z_min >= 0) or z_step <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            errors.append("mode='photoz' 时 grid 需含合法 z_min/z_max/z_step"
                          '（z_max > z_min >= 0，z_step > 0）')

    photometry = config.get('photometry')
    if not isinstance(photometry, list) or not photometry:
        errors.append('photometry 必须是非空数组 [{band, mag, mag_err, mag_sys, source}]')
        photometry = []

    filters = {f.id: f for f in sess.query(FilterDef).all()}
    invalid_points = []
    seen_bands = set()
    for i, p in enumerate(photometry):
        if not isinstance(p, dict):
            invalid_points.append({'index': i, 'reason': '不是对象'})
            continue
        band = p.get('band')
        filt = filters.get(band)
        pcg = (filt.extra_data or {}).get('pcigale_name') if filt else None
        if not pcg:
            invalid_points.append({'index': i, 'band': band,
                                   'reason': '波段无 pcigale_name（不支持 pcigale 拟合）'})
            continue
        try:
            float(p.get('mag'))
        except (TypeError, ValueError):
            invalid_points.append({'index': i, 'band': band, 'reason': 'mag 缺失/非法'})
            continue
        mag_sys = p.get('mag_sys')
        if isinstance(mag_sys, str):
            mag_sys = mag_sys.strip().lower()
        if mag_sys not in _MAG_SYSTEMS:
            invalid_points.append({'index': i, 'band': band,
                                   'reason': f'mag_sys 非法: {p.get("mag_sys")!r}'})
            continue
        if p.get('mag_err') is not None:
            try:
                float(p['mag_err'])
            except (TypeError, ValueError):
                invalid_points.append({'index': i, 'band': band, 'reason': 'mag_err 非法'})
                continue
        seen_bands.add(band)
    if len(seen_bands) < 4:
        errors.append(f'有效测光波段不足 4 个（当前 {len(seen_bands)} 个）')

    if invalid_points:
        return None, (jsonify({'error': '存在非法测光点',
                               'invalid_points': invalid_points}), 400)
    if errors:
        return None, (jsonify({'error': 'config 非法', 'details': errors}), 400)
    config['mode'] = mode
    # 可选模块开关（nebular / dl2014），缺省关闭
    for key in _OPTIONAL_MODULES:
        config[key] = bool(config.get(key))
    return config, None


@hostfit_bp.route('/jobs', methods=['POST'])
@require_auth
def submit_job():
    payload = request.get_json(force=True, silent=True) or {}
    sess = get_session()
    try:
        config, err = _validate(payload, sess)
        if err:
            return err
        transient_id = payload['transient_id']
    finally:
        sess.close()
    job_id = hostfit_jobs.create_job(transient_id, config,
                                     created_by=current_username())
    return jsonify({'id': job_id, 'transient_id': transient_id,
                    'status': 'pending'}), 201


@hostfit_bp.route('/jobs', methods=['GET'])
def list_jobs():
    transient_id = request.args.get('transient_id')
    sess = get_session()
    try:
        q = (sess.query(FittingResult)
             .filter_by(model_name=hostfit_jobs.MODEL_NAME))
        if transient_id:
            q = q.filter_by(transient_id=transient_id)
        rows = q.order_by(FittingResult.id.desc()).all()
        return jsonify([_job_brief(r) for r in rows])
    finally:
        sess.close()


@hostfit_bp.route('/jobs/<int:job_id>', methods=['GET'])
def job_detail(job_id):
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name != hostfit_jobs.MODEL_NAME:
            abort(404, description='任务不存在')
        return jsonify(_job_detail(row))
    finally:
        sess.close()


@hostfit_bp.route('/jobs/<int:job_id>/files/<kind>', methods=['GET'])
def job_file(job_id, kind):
    if kind not in _FILE_KINDS:
        abort(404, description=f'未知文件类型: {kind}')
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or row.model_name != hostfit_jobs.MODEL_NAME:
            abort(404, description='任务不存在')
        transient_id = row.transient_id
        files = (row.extra_data or {}).get('files') or {}
    finally:
        sess.close()
    if kind not in files:
        abort(404, description='该产物不存在（任务未完成或生成失败）')
    rel, mimetype, as_attachment = _FILE_KINDS[kind]
    path = os.path.join(hostfit_jobs.job_dir(transient_id, job_id), rel)
    if not os.path.exists(path):
        abort(404, description='文件已丢失')
    return send_file(path, mimetype=mimetype,
                     as_attachment=as_attachment,
                     download_name=os.path.basename(rel))


@hostfit_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
@require_admin
def remove_job(job_id):
    ok, msg = hostfit_jobs.delete_job(job_id)
    if not ok:
        code = 404 if msg == '任务不存在' else 400
        return jsonify({'error': msg}), code
    return jsonify({'status': 'ok', 'message': msg})
