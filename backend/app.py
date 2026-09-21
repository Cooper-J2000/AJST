"""
Flask 应用工厂
注册所有蓝图，统一错误处理 + 鉴权。
"""
from flask import Flask, session, request, jsonify, abort
from flask_cors import CORS
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from functools import wraps
import threading
import time

from config import Config
from models import Base, User


# 全局 engine + session 工厂（非 ORM 集成模式，保持简单直接）
_engine = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine
    if _engine is None:
        with _engine_lock:  # 多线程首波请求竞态保护（双重检查）
            if _engine is None:
                _engine = create_engine(Config.SQLALCHEMY_DATABASE_URI,
                                        pool_pre_ping=True)
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
        conn.execute(text(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS title TEXT"))
        conn.execute(text(
            "ALTER TABLE articles ADD COLUMN IF NOT EXISTS bibtex TEXT"))
        conn.execute(text(
            "ALTER TABLE spectra ADD COLUMN IF NOT EXISTS parent_id BIGINT"
            " REFERENCES spectra(id) ON DELETE CASCADE"))
        conn.execute(text(
            "ALTER TABLE filters ADD COLUMN IF NOT EXISTS gext_coeff DOUBLE PRECISION"))
        conn.execute(text(
            "ALTER TABLE transients ADD COLUMN IF NOT EXISTS gext_ebv DOUBLE PRECISION"))
        conn.execute(text(
            "ALTER TABLE host_galaxies ADD COLUMN IF NOT EXISTS gext_ebv DOUBLE PRECISION"))
        conn.execute(text(
            "ALTER TABLE transients ADD COLUMN IF NOT EXISTS gext_distmod DOUBLE PRECISION"))
        conn.execute(text(
            "ALTER TABLE host_galaxies ADD COLUMN IF NOT EXISTS gext_distmod DOUBLE PRECISION"))
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


# ─── 登录失败限流（进程内计数：同一 IP+账户 15 分钟内失败 5 次即锁定） ───
_login_fails = {}        # 'ip|username' → [失败时间戳]
_login_fails_lock = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_S = 15 * 60
# dict 条目数上限，防海量随机用户名撑大内存；超限时先惰性清理过期条目，
# 仍超限则全清（代价是已有锁定被重置，换取内存有界）
LOGIN_FAILS_MAX_KEYS = 10000


def _login_key(username):
    return f'{request.remote_addr}|{username}'


def _login_is_blocked(key):
    now = time.time()
    with _login_fails_lock:
        fails = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW_S]
        if fails:
            _login_fails[key] = fails
        else:
            _login_fails.pop(key, None)
        return len(fails) >= LOGIN_MAX_FAILS


def _record_login_fail(key):
    now = time.time()
    with _login_fails_lock:
        if len(_login_fails) >= LOGIN_FAILS_MAX_KEYS:
            for k in [k for k, ts in _login_fails.items()
                      if not any(now - t < LOGIN_WINDOW_S for t in ts)]:
                del _login_fails[k]
            if len(_login_fails) >= LOGIN_FAILS_MAX_KEYS:
                _login_fails.clear()
        fails = [t for t in _login_fails.get(key, []) if now - t < LOGIN_WINDOW_S]
        fails.append(now)
        _login_fails[key] = fails


def create_app():
    app = Flask(__name__, static_folder='../frontend', static_url_path='')
    app.config.from_object(Config)
    # CORS 白名单（config.py：AJST_CORS_ORIGINS 环境变量，默认本机端口），
    # 会话基于 cookie，绝不能 origins='*' + supports_credentials
    CORS(app, origins=app.config['CORS_ORIGINS'], supports_credentials=True)

    # 创建表
    init_db()

    # ── 鉴权路由 ──
    @app.route('/api/auth/login', methods=['POST'])
    def auth_login():
        data = request.get_json(force=True)
        username = (data.get('username') or '').strip() or 'admin'  # 兼容旧版仅密码登录 → admin
        password = data.get('password', '')
        key = _login_key(username)
        if _login_is_blocked(key):
            abort(429, description='登录失败次数过多，请 15 分钟后再试')
        sess = get_session()
        try:
            user = sess.query(User).filter(User.username == username).first()
            if user and user.check_password(password):
                with _login_fails_lock:
                    _login_fails.pop(key, None)
                session['authenticated'] = True
                session['user_id'] = user.id
                session['username'] = user.username
                session['role'] = user.role
                session.permanent = True
                return {'status': 'ok', 'message': '登录成功',
                        'username': user.username, 'role': user.role}
        finally:
            sess.close()
        _record_login_fail(key)
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
    # Phase 2: 宿主星系数据 + pcigale 宿主拟合
    from routes.hosts import hosts_bp
    from routes.hostfit import hostfit_bp
    # Phase 3: 暂现源 SED 分析
    from routes.sedfit import sedfit_bp

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
    # Phase 2: 宿主星系数据 + pcigale 宿主拟合
    app.register_blueprint(hosts_bp, url_prefix='/api/hosts')
    app.register_blueprint(hostfit_bp, url_prefix='/api/hostfit')
    # Phase 3: 暂现源 SED 分析
    app.register_blueprint(sedfit_bp, url_prefix='/api/sed')

    # 上次运行残留的 pending/running 拟合任务标记为 interrupted
    from fitting.jobs import mark_interrupted
    mark_interrupted()
    # Phase 2: hostfit（pcigale 宿主拟合）任务同样处理
    from hostfit.jobs import mark_interrupted as hostfit_mark_interrupted
    hostfit_mark_interrupted()
    # Phase 3: sedfit（SED 拟合）任务同样处理
    from sedfit.jobs import mark_interrupted as sedfit_mark_interrupted
    sedfit_mark_interrupted()

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

    @app.errorhandler(413)
    def too_large(e):
        return {'error': 'Payload too large',
                'message': '请求体超过大小限制（32 MB）'}, 413

    @app.errorhandler(429)
    def too_many(e):
        return {'error': 'Too many requests', 'message': str(e.description)}, 429

    @app.errorhandler(500)
    def server_error(e):
        return {'error': 'Internal server error'}, 500

    return app
