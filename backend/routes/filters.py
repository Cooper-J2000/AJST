"""
滤波器定义
GET    /api/filters       — 列表
POST   /api/filters       — 新建（需登录）
PUT    /api/filters/<id>  — 更新（仅管理员）
DELETE /api/filters/<id>  — 删除（仅管理员）
"""
from flask import Blueprint, jsonify, request
from app import get_session, require_auth, require_admin
from models import FilterDef
import extinction

filters_bp = Blueprint('filters', __name__)


@filters_bp.route('', methods=['GET'])
def list_filters():
    sess = get_session()
    try:
        sort = request.args.get('sort', 'wavelength')
        order = request.args.get('order', 'asc')
        sort_col = getattr(FilterDef, sort, FilterDef.wavelength)
        if order == 'desc':
            sort_col = sort_col.desc()
        filters = sess.query(FilterDef).order_by(sort_col).all()
        return jsonify([f.to_dict() for f in filters])
    finally:
        sess.close()


@filters_bp.route('', methods=['POST'])
@require_auth
def create_filter():
    sess = get_session()
    try:
        body = request.get_json(force=True)
        if not body or 'id' not in body:
            return {'error': 'id is required'}, 400
        existing = sess.query(FilterDef).filter(FilterDef.id == body['id']).first()
        if existing:
            return {'error': f'Filter {body["id"]} already exists'}, 409
        f = FilterDef(id=body['id'], wavelength=float(body.get('wavelength', 0)))
        if 'filter_type' in body: f.filter_type = body['filter_type']
        if 'vega2ab' in body: f.vega2ab = float(body['vega2ab'])
        if 'description' in body: f.description = body['description']
        if 'extra_data' in body and isinstance(body['extra_data'], dict):
            f.extra_data = body['extra_data']
        sess.add(f)
        sess.commit()
        return jsonify(f.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@filters_bp.route('/<fid>', methods=['PUT'])
@require_admin
def update_filter(fid):
    sess = get_session()
    try:
        f = sess.query(FilterDef).filter(FilterDef.id == fid).first()
        if not f:
            return {'error': 'Not found'}, 404
        body = request.get_json(force=True)
        old_wl, old_v2a = f.wavelength, f.vega2ab
        for field in ('wavelength', 'vega2ab'):
            if field in body and body[field] is not None:
                setattr(f, field, float(body[field]))
        for field in ('filter_type', 'description'):
            if field in body:
                setattr(f, field, str(body[field]) if body[field] is not None else None)
        if 'extra_data' in body and isinstance(body['extra_data'], dict):
            merged = f.extra_data or {}
            merged.update(body['extra_data'])
            f.extra_data = merged
        # 波长/Vega2AB 变动 → 该波段所有已银消改正的数据点自动重算
        if f.wavelength != old_wl or f.vega2ab != old_v2a:
            extinction.recompute_band(sess, fid)
        sess.commit()
        return jsonify(f.to_dict())
    finally:
        sess.close()


@filters_bp.route('/<fid>', methods=['DELETE'])
@require_admin
def delete_filter(fid):
    sess = get_session()
    try:
        f = sess.query(FilterDef).filter(FilterDef.id == fid).first()
        if not f:
            return {'error': 'Not found'}, 404
        sess.delete(f)
        sess.commit()
        return {'status': 'deleted', 'id': fid}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
