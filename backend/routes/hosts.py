"""宿主星系数据接口
GET    /api/hosts/<transient_id>   — 查询宿主（公开；无记录 404）
PUT    /api/hosts/<transient_id>   — upsert 宿主数据（需登录；transient 必须存在）
DELETE /api/hosts/<transient_id>   — 删除宿主记录（仅管理员）
"""
from flask import Blueprint, jsonify, request, abort

from app import get_session, require_auth, require_admin, current_username
from models import Transient, HostGalaxy
from coords import parse_ra, parse_dec

hosts_bp = Blueprint('hosts', __name__)

# PUT 可写字段（photometry 整体替换；derived 浅合并；其余直接赋值；ra/dec 单独走坐标解析）
_SCALAR_FIELDS = ('redshift', 'redshift_err', 'comment')
_REDSHIFT_TYPES = ('spec', 'phot', None)


@hosts_bp.route('/<transient_id>', methods=['GET'])
def get_host(transient_id):
    sess = get_session()
    try:
        host = (sess.query(HostGalaxy)
                .filter_by(transient_id=transient_id).first())
        if host is None:
            return jsonify({'error': 'no host data'}), 404
        return jsonify(host.to_dict())
    finally:
        sess.close()


@hosts_bp.route('/<transient_id>', methods=['PUT'])
@require_auth
def upsert_host(transient_id):
    body = request.get_json(force=True, silent=True) or {}

    if 'redshift_type' in body and body['redshift_type'] not in _REDSHIFT_TYPES:
        abort(400, description="redshift_type 必须为 'spec' / 'phot' / null")
    if 'photometry' in body and not isinstance(body['photometry'], list):
        abort(400, description='photometry 必须是数组 [{band, mag, mag_err, mag_sys, source, upperlimit, gext_corr}]')
    if isinstance(body.get('photometry'), list):
        for i, p in enumerate(body['photometry']):
            if not isinstance(p, dict) or not p.get('band') or p.get('mag') is None:
                abort(400, description='photometry 每项必须含 band 与 mag')
            # gext_corr（该行是否已做银河系消光改正）不可为空，必须显式 true/false；
            # 库中缺失该键的存量行在下游一律按 false（未改正）对待
            if not isinstance(p.get('gext_corr'), bool):
                abort(400, description=(
                    f"photometry 第 {i + 1} 行（band={p.get('band')}）缺少 gext_corr："
                    '必须显式标记该行是否已做银河系消光改正（true/false）'))
            # upperlimit 缺省 false；mag_err 允许为空（后续处理按 0.2 mag，不落库）
            p['upperlimit'] = bool(p.get('upperlimit'))
            if p.get('mag_err') is not None:
                try:
                    p['mag_err'] = float(p['mag_err'])
                except (TypeError, ValueError):
                    abort(400, description=f"波段 {p.get('band')}: mag_err 非法")
    if 'derived' in body and not isinstance(body['derived'], dict):
        abort(400, description='derived 必须是对象')

    sess = get_session()
    try:
        if sess.get(Transient, transient_id) is None:
            abort(404, description=f'暂现源不存在: {transient_id}')
        host = (sess.query(HostGalaxy)
                .filter_by(transient_id=transient_id).first())
        created = host is None
        if created:
            host = HostGalaxy(transient_id=transient_id)
            sess.add(host)

        for field in _SCALAR_FIELDS:
            if field in body:
                setattr(host, field, body[field])
        # 坐标支持十进制度或时分秒字符串，入库统一转度
        try:
            if 'ra' in body:
                host.ra = parse_ra(body['ra'])
            if 'dec' in body:
                host.dec = parse_dec(body['dec'])
        except ValueError as e:
            abort(400, description=str(e))
        if 'redshift_type' in body:
            host.redshift_type = body['redshift_type']
        if 'photometry' in body:
            host.photometry = body['photometry']          # 整体替换
        if 'derived' in body:
            merged = dict(host.derived or {})             # 浅合并
            merged.update(body['derived'])
            host.derived = merged
        host.source = current_username()

        sess.commit()
        return jsonify(host.to_dict()), (201 if created else 200)
    finally:
        sess.close()


@hosts_bp.route('/<transient_id>', methods=['DELETE'])
@require_admin
def delete_host(transient_id):
    sess = get_session()
    try:
        host = (sess.query(HostGalaxy)
                .filter_by(transient_id=transient_id).first())
        if host is None:
            return jsonify({'error': 'no host data'}), 404
        sess.delete(host)
        sess.commit()
        return jsonify({'status': 'ok', 'message': '已删除'})
    finally:
        sess.close()
