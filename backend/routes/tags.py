"""
标签索引表（主 tag / 副 tag 统一登记）
GET  /api/tags              — 列表（可选 ?kind=main|sub 过滤；公开，首页标签分布说明用）
POST /api/tags              — 新建（需登录；description 必填）
PUT  /api/tags/<id>         — 更新说明/颜色（需登录）
POST /api/tags/register     — 内部用：按 [{name, kind}] 幂等登记缺失条目（需登录）

transients 写入（POST/PUT /api/transients）也会自动登记其中出现的未知 tag
（description 留空，调用 register_tags()），保证索引表覆盖所有在用 tag。
"""
from flask import Blueprint, request, jsonify
from app import get_session, require_auth
from models import Tag

tags_bp = Blueprint('tags', __name__)

TAG_KINDS = ('main', 'sub')


def register_tags(sess, names, kind):
    """把 names 中尚未登记的 tag 幂等写入索引表（description 留空）。只 add，不 commit。"""
    if kind not in TAG_KINDS:
        return
    seen = set()
    for raw in names or []:
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        exists = sess.query(Tag).filter(Tag.name == name, Tag.kind == kind).first()
        if not exists:
            sess.add(Tag(name=name, kind=kind))


@tags_bp.route('', methods=['GET'])
def list_tags():
    sess = get_session()
    try:
        q = sess.query(Tag)
        kind = (request.args.get('kind') or '').strip()
        if kind in TAG_KINDS:
            q = q.filter(Tag.kind == kind)
        tags = q.order_by(Tag.kind, Tag.name).all()
        return jsonify([t.to_dict() for t in tags])
    finally:
        sess.close()


@tags_bp.route('', methods=['POST'])
@require_auth
def create_tag():
    body = request.get_json(force=True)
    if not body or not (body.get('name') or '').strip():
        return {'error': 'name is required'}, 400
    name = body['name'].strip()
    kind = (body.get('kind') or 'main').strip()
    if kind not in TAG_KINDS:
        return {'error': f'kind must be one of {TAG_KINDS}'}, 400
    description = (body.get('description') or '').strip()
    if not description:
        return {'error': 'description is required（新建 tag 必须填写文字说明）'}, 400
    sess = get_session()
    try:
        existing = sess.query(Tag).filter(Tag.name == name, Tag.kind == kind).first()
        if existing:
            return {'error': f'Tag "{name}" ({kind}) already exists'}, 409
        tag = Tag(name=name, kind=kind, description=description,
                  color=body.get('color'))
        sess.add(tag)
        sess.commit()
        return jsonify(tag.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@tags_bp.route('/<int:tag_id>', methods=['PUT'])
@require_auth
def update_tag(tag_id):
    body = request.get_json(force=True) or {}
    sess = get_session()
    try:
        tag = sess.query(Tag).filter(Tag.id == tag_id).first()
        if not tag:
            return {'error': 'Not found'}, 404
        if 'description' in body:
            tag.description = (body['description'] or '').strip() or None
        if 'color' in body:
            tag.color = (body['color'] or '').strip() or None
        sess.commit()
        return jsonify(tag.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@tags_bp.route('/<int:tag_id>', methods=['DELETE'])
@require_auth
def delete_tag(tag_id):
    """删除索引条目（不影响源上已挂的同名 tag 字符串）"""
    sess = get_session()
    try:
        tag = sess.query(Tag).filter(Tag.id == tag_id).first()
        if not tag:
            return {'error': 'Not found'}, 404
        sess.delete(tag)
        sess.commit()
        return {'status': 'deleted', 'id': tag_id}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@tags_bp.route('/register', methods=['POST'])
@require_auth
def register_tags_api():
    body = request.get_json(force=True) or {}
    items = body.get('tags') or []
    if not isinstance(items, list):
        return {'error': 'tags must be a list of {name, kind}'}, 400
    sess = get_session()
    try:
        for item in items:
            if isinstance(item, dict):
                register_tags(sess, [item.get('name')], item.get('kind'))
        sess.commit()
        return {'status': 'ok'}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
