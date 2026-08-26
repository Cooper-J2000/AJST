"""
标签管理
GET  /api/tags          — 列表
POST /api/tags          — 新建
"""
from flask import Blueprint, request, jsonify
from app import get_session, require_auth
from models import Tag

tags_bp = Blueprint('tags', __name__)


@tags_bp.route('', methods=['GET'])
def list_tags():
    sess = get_session()
    try:
        tags = sess.query(Tag).order_by(Tag.name).all()
        return jsonify([t.to_dict() for t in tags])
    finally:
        sess.close()


@tags_bp.route('', methods=['POST'])
@require_auth
def create_tag():
    body = request.get_json(force=True)
    if not body or 'name' not in body:
        return {'error': 'name is required'}, 400
    sess = get_session()
    try:
        existing = sess.query(Tag).filter(Tag.name == body['name']).first()
        if existing:
            return {'error': f'Tag "{body["name"]}" already exists'}, 409
        tag = Tag(name=body['name'], description=body.get('description'), color=body.get('color'))
        sess.add(tag)
        sess.commit()
        return jsonify(tag.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
