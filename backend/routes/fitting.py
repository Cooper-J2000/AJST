"""余辉拟合接口
GET    /api/fitting/engines                  — 引擎列表（含配置模式，无需认证）
POST   /api/fitting/jobs                     — 提交拟合任务（需登录）
GET    /api/fitting/jobs?transient_id=       — 任务列表
GET    /api/fitting/jobs/<id>                — 任务详情（config/参数/warnings/文件链接）
GET    /api/fitting/jobs/<id>/files/<kind>   — 产物文件（kind = h5 | corner | lc_model |
                                                lc_plot | lc_ratio | metrics）
DELETE /api/fitting/jobs/<id>                — 删除任务（需登录，仅 done/failed/interrupted）
"""
import os

from flask import Blueprint, jsonify, request, abort, send_file

from app import get_session, require_auth, require_admin
from models import Transient, FittingResult
from fitting.engines import get_engine, list_engines
from fitting import jobs as fitting_jobs

fitting_bp = Blueprint('fitting', __name__)

_FILE_KINDS = {
    'h5': ('chain_record.h5', 'application/octet-stream'),
    'corner': ('corner.png', 'image/png'),
    'lc_model': ('lc_model.json', 'application/json'),
    'lc_plot': ('lc_plot.png', 'image/png'),
    'lc_ratio': ('lc_ratio_plot.png', 'image/png'),
    'metrics': ('metrics.txt', 'text/plain; charset=utf-8'),
}


def _job_brief(row):
    """任务列表项"""
    ed = row.extra_data or {}
    return {
        'id': row.id,
        'transient_id': row.transient_id,
        'model_name': row.model_name,
        'status': ed.get('status'),
        'chi2': row.chi_squared,
        'runtime_s': ed.get('runtime_s'),
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _job_detail(row):
    """任务详情：config、结果参数、warnings、文件链接"""
    ed = row.extra_data or {}
    d = _job_brief(row)
    d.update({
        'engine': ed.get('engine'),
        'config': ed.get('config'),
        'parameters': row.parameters or {},
        'error': ed.get('error'),
        'dof': ed.get('dof'),
        'bic': ed.get('bic'),
        'aic': ed.get('aic'),
        'warnings': ed.get('warnings') or [],
        'created_by': ed.get('created_by'),
        'files': {kind: f'/api/fitting/jobs/{row.id}/files/{kind}'
                  for kind in (ed.get('files') or {})},
    })
    return d


@fitting_bp.route('/engines', methods=['GET'])
def engines():
    """引擎列表 + 各引擎配置模式（模型选项/先验模板/采样默认值）"""
    return jsonify([{'name': e.name, 'label': e.label, 'schema': e.config_schema(),
                     'version': getattr(e, 'version', None)}
                    for e in list_engines()])


@fitting_bp.route('/jobs', methods=['POST'])
@require_auth
def submit_job():
    """提交拟合任务：{transient_id, engine, config}"""
    payload = request.get_json(force=True, silent=True) or {}
    transient_id = payload.get('transient_id')
    engine_name = payload.get('engine', 'vegas_unified')
    config = payload.get('config') or {}
    if not transient_id:
        abort(400, description='缺少 transient_id')
    engine = get_engine(engine_name)
    if engine is None:
        abort(400, description=f'未知引擎: {engine_name}')

    sess = get_session()
    try:
        if sess.get(Transient, transient_id) is None:
            abort(404, description=f'暂现源不存在: {transient_id}')
    finally:
        sess.close()

    # 配置合法性（模型组合 + 先验）
    errors = engine.validate_config(config)
    if errors:
        return jsonify({'error': 'config 非法', 'details': errors}), 400

    # 数据校验：有红移 + 有可用数据点
    try:
        data = fitting_jobs.prepare_data(transient_id, config.get('data_selection'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if data['n_points'] == 0:
        return jsonify({'error': '无可用数据点', 'warnings': data['warnings']}), 400

    job_id = fitting_jobs.create_job(
        transient_id, engine_name, config,
        warnings=data['warnings'], created_by='local')
    return jsonify({'id': job_id})


@fitting_bp.route('/jobs', methods=['GET'])
def list_jobs():
    """任务列表，可按 transient_id 过滤（倒序）"""
    transient_id = request.args.get('transient_id')
    sess = get_session()
    try:
        q = sess.query(FittingResult)
        if transient_id:
            q = q.filter_by(transient_id=transient_id)
        else:
            # 全量列表只给拟合任务（model_name 形如 engine:...）
            q = q.filter(FittingResult.model_name.like('%:%'))
        rows = q.order_by(FittingResult.id.desc()).all()
        return jsonify([_job_brief(r) for r in rows
                        if (r.extra_data or {}).get('status')])
    finally:
        sess.close()


@fitting_bp.route('/jobs/<int:job_id>', methods=['GET'])
def job_detail(job_id):
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None or not (row.extra_data or {}).get('status'):
            abort(404, description='任务不存在')
        return jsonify(_job_detail(row))
    finally:
        sess.close()


@fitting_bp.route('/jobs/<int:job_id>/files/<kind>', methods=['GET'])
def job_file(job_id, kind):
    """产物文件下载/查看：h5（下载）、corner（PNG）、lc_model（JSON）"""
    if kind not in _FILE_KINDS:
        abort(404, description=f'未知文件类型: {kind}')
    sess = get_session()
    try:
        row = sess.get(FittingResult, job_id)
        if row is None:
            abort(404, description='任务不存在')
        transient_id = row.transient_id
        files = (row.extra_data or {}).get('files') or {}
    finally:
        sess.close()
    if kind not in files:
        abort(404, description='该产物不存在（任务未完成或生成失败）')
    fname, mimetype = _FILE_KINDS[kind]
    path = os.path.join(fitting_jobs.job_dir(transient_id, job_id), fname)
    if not os.path.exists(path):
        abort(404, description='文件已丢失')
    return send_file(path, mimetype=mimetype,
                     as_attachment=(kind == 'h5'), download_name=fname)


@fitting_bp.route('/jobs/<int:job_id>', methods=['DELETE'])
@require_admin
def remove_job(job_id):
    ok, msg = fitting_jobs.delete_job(job_id)
    if not ok:
        code = 404 if msg == '任务不存在' else 400
        return jsonify({'error': msg}), code
    return jsonify({'status': 'ok', 'message': msg})
