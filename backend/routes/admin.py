"""
管理员维护后台 API（仅管理员）
GET    /api/admin/users        — 用户列表
POST   /api/admin/users        — 新增普通用户 {username, password}
PUT    /api/admin/users/<id>   — 修改普通用户 {username?, password?}
DELETE /api/admin/users/<id>   — 删除普通用户
"""
from flask import Blueprint, request, jsonify, session
from app import get_session, require_admin
from models import User

admin_bp = Blueprint('admin', __name__)


def _get_user(sess, uid):
    return sess.query(User).filter(User.id == uid).first()


@admin_bp.route('/users', methods=['GET'])
@require_admin
def list_users():
    sess = get_session()
    try:
        users = sess.query(User).order_by(User.id).all()
        return jsonify([u.to_dict() for u in users])
    finally:
        sess.close()


@admin_bp.route('/users', methods=['POST'])
@require_admin
def create_user():
    body = request.get_json(force=True)
    username = (body.get('username') or '').strip()
    password = body.get('password') or ''
    if not username or not password:
        return {'error': 'username 和 password 不能为空'}, 400
    if len(username) > 64:
        return {'error': 'username 过长'}, 400
    sess = get_session()
    try:
        if sess.query(User).filter(User.username == username).first():
            return {'error': f'用户名 {username} 已存在'}, 409
        # 管理后台只创建普通用户；管理员账户固定为 admin
        user = User(username=username, role='user')
        user.set_password(password)
        sess.add(user)
        sess.commit()
        return jsonify(user.to_dict()), 201
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@admin_bp.route('/users/<int:uid>', methods=['PUT'])
@require_admin
def update_user(uid):
    body = request.get_json(force=True)
    sess = get_session()
    try:
        user = _get_user(sess, uid)
        if not user:
            return {'error': 'Not found'}, 404
        if user.role == 'admin':
            return {'error': '不能通过后台修改管理员账户'}, 403
        if 'username' in body:
            new_name = (body.get('username') or '').strip()
            if not new_name:
                return {'error': 'username 不能为空'}, 400
            clash = sess.query(User).filter(
                User.username == new_name, User.id != uid).first()
            if clash:
                return {'error': f'用户名 {new_name} 已存在'}, 409
            user.username = new_name
        if 'password' in body:
            new_pwd = body.get('password') or ''
            if not new_pwd:
                return {'error': 'password 不能为空'}, 400
            user.set_password(new_pwd)
        sess.commit()
        return jsonify(user.to_dict())
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()


@admin_bp.route('/users/<int:uid>', methods=['DELETE'])
@require_admin
def delete_user(uid):
    sess = get_session()
    try:
        user = _get_user(sess, uid)
        if not user:
            return {'error': 'Not found'}, 404
        if user.role == 'admin':
            return {'error': '不能删除管理员账户'}, 403
        sess.delete(user)
        sess.commit()
        return {'status': 'deleted', 'id': uid}
    except Exception as e:
        sess.rollback()
        return {'error': str(e)}, 400
    finally:
        sess.close()
