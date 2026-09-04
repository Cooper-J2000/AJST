"""
滤波器定义
GET    /api/filters                    — 列表
POST   /api/filters                    — 新建（需登录；body 可带 curve 获取透过率曲线）
PUT    /api/filters/<id>               — 更新（仅管理员）
DELETE /api/filters/<id>               — 删除（仅管理员）
GET    /api/filters/pcigale_builtin    — pcigale 自带滤光片名列表（需登录）
POST   /api/filters/parse_curve        — 校验上传曲线文本（需登录，不落库）
GET    /api/filters/svo_search?q=      — SVO FPS 模糊搜索（需登录）
POST   /api/filters/svo_fetch          — 预览抓取 SVO 曲线（需登录，不落库不注册）
"""
from flask import Blueprint, jsonify, request
from app import get_session, require_auth, require_admin
from models import FilterDef
import extinction
import filtercurves

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
        extra = {}
        if 'extra_data' in body and isinstance(body['extra_data'], dict):
            extra = dict(body['extra_data'])
        # 可选：随创建一并获取透过率曲线（curve.kind: pcigale_builtin / upload / svo）
        warning = None
        if isinstance(body.get('curve'), dict):
            try:
                curve_ed, warning = filtercurves.build_curve_extra(body['id'], body['curve'])
            except filtercurves.CurveError as e:
                return {'error': 'curve validation failed', 'message': str(e)}, 400
            except Exception as e:
                sess.rollback()
                return {'error': 'curve fetch failed', 'message': str(e)}, 502
            extra.update(curve_ed)
        if extra:
            f.extra_data = extra
        sess.add(f)
        sess.commit()
        resp = f.to_dict()
        if warning:
            resp['warning'] = warning
        return jsonify(resp), 201
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


# ─── 透过率曲线获取（供新增/后续补充曲线使用，均不落库） ───

@filters_bp.route('/pcigale_builtin', methods=['GET'])
@require_auth
def pcigale_builtin_list():
    """pcigale 自带滤光片名列表（方式 1 的候选）。"""
    try:
        return jsonify({'names': filtercurves.list_pcigale_builtin()})
    except Exception as e:
        return {'error': 'pcigale builtin list failed', 'message': str(e)}, 500


@filters_bp.route('/parse_curve', methods=['POST'])
@require_auth
def parse_curve():
    """校验上传曲线文本（方式 2）。通过则返回归一化后的曲线，不落库。"""
    body = request.get_json(force=True) or {}
    text = body.get('text') or ''
    if not text.strip():
        return {'error': 'empty curve', 'message': '曲线文本为空'}, 400
    try:
        wl, tr_raw = filtercurves.parse_curve_text(text)
        tr = filtercurves.normalize_tr(tr_raw)
    except filtercurves.CurveError as e:
        return {'error': 'invalid curve', 'message': str(e)}, 400
    return jsonify({'npoints': len(wl), 'wl_min': wl[0], 'wl_max': wl[-1],
                    'wl': wl, 'tr': tr})


@filters_bp.route('/svo_search', methods=['GET'])
@require_auth
def svo_search():
    """SVO FPS 模糊搜索（方式 3 第一步）。"""
    q = request.args.get('q', '')
    try:
        results = filtercurves.search_svo(q)
    except filtercurves.CurveError as e:
        return {'error': 'invalid query', 'message': str(e)}, 400
    except Exception as e:
        return {'error': 'svo search failed', 'message': str(e)}, 502
    return jsonify({'results': results})


@filters_bp.route('/svo_fetch', methods=['POST'])
@require_auth
def svo_fetch():
    """预览抓取 SVO 曲线（方式 3 第二步；不落库、不注册 pcigale）。"""
    body = request.get_json(force=True) or {}
    svo_id = (body.get('svo_id') or '').strip()
    if not svo_id:
        return {'error': 'missing svo_id', 'message': 'svo_id 不能为空'}, 400
    try:
        wl, tr_raw = filtercurves.fetch_svo_transmission(svo_id)
        tr = filtercurves.normalize_tr(tr_raw)
    except filtercurves.CurveError as e:
        return {'error': 'invalid svo_id', 'message': str(e)}, 400
    except Exception as e:
        return {'error': 'svo fetch failed', 'message': str(e)}, 502
    return jsonify({'svo_id': svo_id, 'npoints': len(wl),
                    'wl_min': min(wl), 'wl_max': max(wl), 'wl': wl, 'tr': tr})
