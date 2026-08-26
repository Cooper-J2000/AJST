"""
Flask 应用工厂
注册所有蓝图，统一错误处理 + 鉴权。
"""
from flask import Flask, session, request, jsonify, abort
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from functools import wraps

from config import Config
from models import Base, User


# 全局 engine + session 工厂（非 ORM 集成模式，保持简单直接）
_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(Config.SQLALCHEMY_DATABASE_URI, pool_pre_ping=True)
    return _engine


def get_session():
    return Session(get_engine())


def init_db():
    """创建所有表（幂等）+ 轻量列迁移 + 管理员种子账户"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    # create_all 不会给已存在的表补列，这里做幂等的列迁移
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE lightcurves ADD COLUMN IF NOT EXISTS source VARCHAR(128)"))
    # 种子管理员：无任何 admin 账户时创建 admin，密码取 AUTH_PASSWORD
    # （环境变量 AJST_CATALOG_PASSWORD；未设置时为每次启动随机生成，
    #  请务必通过环境变量显式设置一个强密码，见 docs/TECHNICAL.md §7.1）
    sess = Session(engine)
    try:
        has_admin = sess.query(User).filter(User.role == 'admin').first()
        if not has_admin:
            admin = User(username='admin', role='admin')
            admin.set_password(Config.AUTH_PASSWORD)
            sess.add(admin)
            sess.commit()
    finally:
        sess.close()


# ─── 鉴权装饰器 ───
def require_auth(f):
    """需要写权限的操作必须已登录（管理员或普通用户）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            abort(401, description='需要登录才能执行此操作')
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """仅管理员可执行的操作（删除/修改数据、用户管理等）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            abort(401, description='需要登录才能执行此操作')
        if session.get('role') != 'admin':
            abort(403, description='仅管理员可执行此操作')
        return f(*args, **kwargs)
    return decorated


def require_export_auth(f):
    """导出权限：需要登录"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('authenticated'):
            abort(401, description='需要登录才能导出数据')
        return f(*args, **kwargs)
    return decorated


def current_username():
    """当前登录账户名（未登录为 None），用于数据来源自动记录"""
    return session.get('username')


def create_app():
    app = Flask(__name__, static_folder='../frontend', static_url_path='')
    app.config.from_object(Config)
    CORS(app, origins='*', supports_credentials=True)

    # 创建表
    init_db()

    # ── 鉴权路由 ──
    @app.route('/api/auth/login', methods=['POST'])
    def auth_login():
        data = request.get_json(force=True)
        username = (data.get('username') or '').strip() or 'admin'  # 兼容旧版仅密码登录 → admin
        password = data.get('password', '')
        sess = get_session()
        try:
            user = sess.query(User).filter(User.username == username).first()
            if user and user.check_password(password):
                session['authenticated'] = True
                session['user_id'] = user.id
                session['username'] = user.username
                session['role'] = user.role
                session.permanent = True
                return {'status': 'ok', 'message': '登录成功',
                        'username': user.username, 'role': user.role}
        finally:
            sess.close()
        abort(403, description='用户名或密码错误')

    @app.route('/api/auth/logout', methods=['POST'])
    def auth_logout():
        for k in ('authenticated', 'user_id', 'username', 'role'):
            session.pop(k, None)
        return {'status': 'ok', 'message': '已退出'}

    @app.route('/api/auth/status', methods=['GET'])
    def auth_status():
        return {
            'authenticated': bool(session.get('authenticated')),
            'username': session.get('username'),
            'role': session.get('role'),
        }

    # 注册蓝图
    from routes.transients import transients_bp
    from routes.lightcurves import lightcurves_bp
    from routes.filters import filters_bp
    from routes.tags import tags_bp
    from routes.stats import stats_bp
    from routes.export import export_bp
    from routes.extinction import extinction_bp
    from routes.relations import relations_bp
    from routes.fitting import fitting_bp
    from routes.spectra import spectra_bp
    from routes.ingest import ingest_bp
    from routes.gcn import gcn_bp
    from routes.admin import admin_bp
    from routes.articles import articles_bp

    app.register_blueprint(transients_bp, url_prefix='/api/transients')
    app.register_blueprint(lightcurves_bp, url_prefix='/api/lightcurves')
    app.register_blueprint(filters_bp, url_prefix='/api/filters')
    app.register_blueprint(tags_bp, url_prefix='/api/tags')
    app.register_blueprint(stats_bp, url_prefix='/api/stats')
    app.register_blueprint(export_bp, url_prefix='/api/export')
    app.register_blueprint(extinction_bp, url_prefix='/api/extinction')
    app.register_blueprint(relations_bp, url_prefix='/api/relations')
    app.register_blueprint(fitting_bp, url_prefix='/api/fitting')
    app.register_blueprint(spectra_bp, url_prefix='/api/spectra')
    app.register_blueprint(ingest_bp, url_prefix='/api/ingest')
    app.register_blueprint(gcn_bp, url_prefix='/api/gcn')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')
    app.register_blueprint(articles_bp, url_prefix='/api/articles')

    # 上次运行残留的 pending/running 拟合任务标记为 interrupted
    from fitting.jobs import mark_interrupted
    mark_interrupted()

    # 根路径 → SPA
    @app.route('/')
    def index():
        return app.send_static_file('index.html')

    # 管理员维护后台（独立页面）
    @app.route('/admin')
    def admin_page():
        return app.send_static_file('admin.html')

    # 全局错误处理
    @app.errorhandler(400)
    def bad_request(e):
        return {'error': 'Bad request', 'message': str(e.description)}, 400

    @app.errorhandler(401)
    def unauthorized(e):
        return {'error': 'Unauthorized', 'message': str(e.description)}, 401

    @app.errorhandler(403)
    def forbidden(e):
        return {'error': 'Forbidden', 'message': str(e.description)}, 403

    @app.errorhandler(404)
    def not_found(e):
        return {'error': 'Not found'}, 404

    @app.errorhandler(500)
    def server_error(e):
        return {'error': 'Internal server error'}, 500

    return app
