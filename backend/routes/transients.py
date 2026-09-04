"""
暂现源 CRUD + 搜索/筛选
GET    /api/transients          — 列表（支持 search, z_min, z_max, tag, page, per_page, sort, order）
POST   /api/transients          — 新建
GET    /api/transients/<id>     — 单源详情
PUT    /api/transients/<id>     — 更新
DELETE /api/transients/<id>     — 删除
"""
from flask import Blueprint, request, jsonify
from sqlalchemy import cast, String, or_, func, select
from sqlalchemy.orm import defer
from app import get_session, require_auth, require_admin
from models import Transient, HostGalaxy, Lightcurve, Spectrum, utcnow
from coords import parse_ra, parse_dec
from datetime import datetime
import extinction

transients_bp = Blueprint('transients', __name__)

# 支持排序的列（白名单）
SORTABLE = {'id', 'ra', 'dec', 'redshift', 't0'}


def _float_arg(name):
    """安全解析浮点查询参数：缺失/空/非法 → None"""
    raw = request.args.get(name)
    if raw is None or raw.strip() == '':
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@transients_bp.route('', methods=['GET'])
def list_transients():
    """搜索 / 筛选 / 分页 / 排序 列表"""
    sess = get_session()
    try:
        q = sess.query(Transient)
        # --- 搜索 ---
        search = request.args.get('search', '').strip()
        if search:
            like = f'%{search}%'
            q = q.filter(
                or_(
                    Transient.id.ilike(like),
                    cast(Transient.aliases, String).ilike(like),
                    Transient.redshift_ref.ilike(like),
                    Transient.pos_ref.ilike(like),
                )
            )
        # --- 红移范围 ---
        z_min = _float_arg('z_min')
        z_max = _float_arg('z_max')
        if z_min is not None:
            q = q.filter(Transient.redshift >= z_min)
        if z_max is not None:
            q = q.filter(Transient.redshift <= z_max)
        # --- 标签 ---
        tag = request.args.get('tag', '').strip()
        if tag:
            q = q.filter(Transient.tags.contains([tag]))
        # --- RA/Dec 范围 ---
        ra_min = _float_arg('ra_min')
        ra_max = _float_arg('ra_max')
        dec_min = _float_arg('dec_min')
        dec_max = _float_arg('dec_max')
        if ra_min is not None: q = q.filter(Transient.ra >= ra_min)
        if ra_max is not None: q = q.filter(Transient.ra <= ra_max)
        if dec_min is not None: q = q.filter(Transient.dec >= dec_min)
        if dec_max is not None: q = q.filter(Transient.dec <= dec_max)
        # --- 是否有红移 ---
        has_z = request.args.get('has_z')
        if has_z == 'true':
            q = q.filter(Transient.redshift.isnot(None))
        elif has_z == 'false':
            q = q.filter(Transient.redshift.is_(None))
        # --- 是否有宿主星系信息 ---
        has_host = request.args.get('has_host')
        if has_host == 'true':
            q = q.filter(Transient.id.in_(
                sess.query(HostGalaxy.transient_id)))
        elif has_host == 'false':
            q = q.filter(Transient.id.notin_(
                sess.query(HostGalaxy.transient_id)))
        # --- 排序 ---
        sort = request.args.get('sort', 'id')
        if sort not in SORTABLE:
            sort = 'id'
        order = request.args.get('order', 'asc')
        sort_col = getattr(Transient, sort)
        if order == 'desc':
            sort_col = sort_col.desc().nullslast()  # NULL 值排最后
        q = q.order_by(sort_col)
        # --- 分页 ---
        try:
            page = max(1, int(request.args.get('page', 1)))
            per_page = min(10000, max(1, int(request.args.get('per_page', 50))))
        except ValueError:
            page, per_page = 1, 50
        # 列表不需要 comment/extra_data（brief 序列化）：延迟加载，避免整行
        # 8MB+ 文本从 PG 传回（详情接口单独查全量）
        q = q.options(defer(Transient.comment), defer(Transient.extra_data))
        total = q.count()
        items = q.offset((page - 1) * per_page).limit(per_page).all()
        # 批量预取当页源的光变/光谱点数与宿主存在性（3 条聚合查询代替逐行 N+1）
        ids = [t.id for t in items]
        prefetched = {}
        if ids:
            lc_counts = {tid: n for tid, n in sess.execute(
                select(Lightcurve.transient_id, func.count())
                .where(Lightcurve.transient_id.in_(ids))
                .group_by(Lightcurve.transient_id)).all()}
            sp_counts = {tid: n for tid, n in sess.execute(
                select(Spectrum.transient_id, func.count())
                .where(Spectrum.transient_id.in_(ids))
                .group_by(Spectrum.transient_id)).all()}
            host_ids = {tid for (tid,) in sess.execute(
                select(HostGalaxy.transient_id)
                .where(HostGalaxy.transient_id.in_(ids))).all()}
            prefetched = {
                tid: {'lc_count': lc_counts.get(tid, 0),
                      'spectra_count': sp_counts.get(tid, 0),
                      'has_host': tid in host_ids}
                for tid in ids
            }
        return jsonify({
            'total': total,
            'page': page,
            'per_page': per_page,
            'items': [t.to_dict(include_relations=True,
                                prefetched=prefetched.get(t.id),
                                brief=True)
                      for t in items],
        })
    finally:
        sess.close()


@transients_bp.route('/<tid>', methods=['GET'])
def get_transient(tid):
    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if not t:
            return {'error': 'Not found'}, 404
        data = t.to_dict()
        data['lc_count'] = t.lightcurves.count()
        return jsonify(data)
    finally:
        sess.close()


@transients_bp.route('', methods=['POST'])
@require_auth
def create_transient():
    """新建暂现源"""
    body = request.get_json(force=True)
    if not body or 'id' not in body:
        return {'error': 'id is required'}, 400
    sess = get_session()
    try:
        existing = sess.query(Transient).filter(Transient.id == body['id']).first()
        if existing:
            return {'error': f'Transient {body["id"]} already exists'}, 409
        t = Transient(id=body['id'])
        _apply_transient_fields(t, body)
        sess.add(t)
        sess.commit()
        return jsonify(t.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@transients_bp.route('/<tid>', methods=['PUT'])
@require_admin
def update_transient(tid):
    """更新暂现源"""
    body = request.get_json(force=True)
    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if not t:
            return {'error': 'Not found'}, 404
        old_ra, old_dec = t.ra, t.dec
        _apply_transient_fields(t, body)
        t.updated_at = utcnow()
        # 坐标变动 → 该源所有已银消改正的数据点自动重算（坐标被清除则清除改正）
        if t.ra != old_ra or t.dec != old_dec:
            extinction.recompute_transient(sess, tid)
        sess.commit()
        return jsonify(t.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@transients_bp.route('/<tid>', methods=['DELETE'])
@require_admin
def delete_transient(tid):
    sess = get_session()
    try:
        t = sess.query(Transient).filter(Transient.id == tid).first()
        if not t:
            return {'error': 'Not found'}, 404
        sess.delete(t)
        sess.commit()
        return {'status': 'deleted', 'id': tid}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


def _apply_transient_fields(t, body):
    """将请求体字段映射到模型（可扩展）。
    约定：传 null 或空字符串 = 清空该字段；不传 = 保持不变。"""
    for field in ('redshift', 'pos_error'):
        if field in body:
            v = body[field]
            setattr(t, field, float(v) if v not in (None, '') else None)
    # 坐标支持十进制度或时分秒字符串，入库统一转度（非法值抛 ValueError → 400）
    if 'ra' in body:
        t.ra = parse_ra(body['ra'])
    if 'dec' in body:
        t.dec = parse_dec(body['dec'])
    for field in ('trigger_instrument', 'redshift_type', 'redshift_ref',
                  'pos_error_unit', 'pos_ref'):
        if field in body:
            v = body[field]
            setattr(t, field, str(v) if v not in (None, '') else None)
    if 't0' in body:
        if body['t0'] is None or (isinstance(body['t0'], str) and not body['t0'].strip()):
            t.t0 = None  # 显式清空
        else:
            # 一律按 UTC 原样存储：带时区标记也只取时间字面值，不做任何转换
            try:
                t.t0 = datetime.fromisoformat(body['t0'].replace('Z', '+00:00'))
                if t.t0.tzinfo is not None:
                    t.t0 = t.t0.replace(tzinfo=None)
            except (ValueError, TypeError):
                t.t0 = None
    if 'aliases' in body:
        t.aliases = body['aliases'] if isinstance(body['aliases'], list) else [body['aliases']]
    if 'tags' in body:
        t.tags = body['tags'] if isinstance(body['tags'], list) else [body['tags']]
    if 'sub_tag' in body:
        t.sub_tag = body['sub_tag'] if isinstance(body['sub_tag'], list) else [body['sub_tag']]
    if 'comment' in body:
        t.comment = str(body['comment']) if body['comment'] is not None else None
    if 'extra_data' in body and isinstance(body['extra_data'], dict):
        # 浅合并；必须构造新 dict，否则 JSONB 原地修改不被 SQLAlchemy 追踪、不会落库
        merged = dict(t.extra_data or {})
        merged.update(body['extra_data'])
        t.extra_data = merged
