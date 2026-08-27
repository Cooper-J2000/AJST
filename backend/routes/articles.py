"""
相关研究文章（每个源可多条：简称 + 标题 + 链接 + BibTeX；以自增 id 定位，简称不唯一）
GET    /api/articles?transient_id=X   — 列表（公开）
POST   /api/articles                   — 添加（登录即可，source 自动记录为当前账户）
PUT    /api/articles/<id>              — 修改（仅管理员）
DELETE /api/articles/<id>              — 删除（仅管理员）
"""
from flask import Blueprint, request, jsonify
from app import get_session, require_auth, require_admin, current_username
from models import Article

articles_bp = Blueprint('articles', __name__)


@articles_bp.route('', methods=['GET'])
def list_articles():
    sess = get_session()
    try:
        q = sess.query(Article)
        tid = request.args.get('transient_id')
        if tid:
            q = q.filter(Article.transient_id == tid)
        return jsonify([a.to_dict() for a in q.order_by(Article.id).all()])
    finally:
        sess.close()


@articles_bp.route('', methods=['POST'])
@require_auth
def create_article():
    body = request.get_json(force=True) or {}
    tid = (body.get('transient_id') or '').strip()
    name = (body.get('name') or '').strip()
    url = (body.get('url') or '').strip()
    if not tid or not name or not url:
        return {'error': 'transient_id / name / url 均为必填'}, 400
    # 可选字段：标题、BibTeX（空串存 None）
    title = (body.get('title') or '').strip() or None
    bibtex = (body.get('bibtex') or '').strip() or None
    sess = get_session()
    try:
        a = Article(transient_id=tid, name=name, url=url, title=title, bibtex=bibtex,
                    source=current_username())  # 网页录入：自动记录提交账户
        sess.add(a)
        sess.commit()
        return jsonify(a.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@articles_bp.route('/<int:aid>', methods=['PUT'])
@require_admin
def update_article(aid):
    body = request.get_json(force=True) or {}
    sess = get_session()
    try:
        a = sess.query(Article).filter(Article.id == aid).first()
        if not a:
            return {'error': 'Not found'}, 404
        if 'name' in body:
            name = (body['name'] or '').strip()
            if not name:
                return {'error': 'name 不能为空'}, 400
            a.name = name
        if 'url' in body:
            url = (body['url'] or '').strip()
            if not url:
                return {'error': 'url 不能为空'}, 400
            a.url = url
        # 可选字段可改可清空（空串 → None）
        if 'title' in body:
            a.title = (body['title'] or '').strip() or None
        if 'bibtex' in body:
            a.bibtex = (body['bibtex'] or '').strip() or None
        sess.commit()
        return jsonify(a.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@articles_bp.route('/<int:aid>', methods=['DELETE'])
@require_admin
def delete_article(aid):
    sess = get_session()
    try:
        a = sess.query(Article).filter(Article.id == aid).first()
        if not a:
            return {'error': 'Not found'}, 404
        sess.delete(a)
        sess.commit()
        return {'status': 'deleted', 'id': aid}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
