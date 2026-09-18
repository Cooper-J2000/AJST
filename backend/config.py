"""数据库及应用配置"""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
# 数据目录：默认 <项目根>/catadata（将 AJST-Data 仓库克隆到该位置即可），
# 也可用环境变量 AJST_DATA_DIR 指向任意位置
DATA_DIR = os.environ.get('AJST_DATA_DIR', os.path.join(PROJECT_ROOT, 'catadata'))

# GCN 通告存档（catadata/gcn/archive/<circularId>.json）
GCN_ARCHIVE_DIR = os.path.join(DATA_DIR, 'gcn', 'archive')

# PostgreSQL 连接。默认走本机 Unix socket、以当前 OS 用户连接（peer auth），
# 生产/远程部署请用环境变量 DATABASE_URL 覆盖，例如
# postgresql+psycopg2://user:pass@host:5432/ajst_catalog
DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql+psycopg2:///ajst_catalog'
)

# 鉴权密码（首次自动生成，可通过环境变量覆盖）
AUTH_PASSWORD = os.environ.get('AJST_CATALOG_PASSWORD') or secrets.token_urlsafe(12)
SECRET_KEY = os.environ.get('AJST_SECRET_KEY') or secrets.token_hex(32)

# 数据接入（ingest）API 的 Bearer token；未设置则 ingest 接口整体不可用（503）
AJST_INGEST_TOKEN = os.environ.get('AJST_INGEST_TOKEN')


# CORS 允许来源白名单：环境变量 AJST_CORS_ORIGINS（逗号分隔）覆盖；
# 默认仅本机常见端口（本地单人部署够用）。显式设为 '*' 可恢复通配（不推荐）。
_cors_env = os.environ.get('AJST_CORS_ORIGINS', '').strip()
# 本服务自身端口：与 backend/start.sh 的 PORT 同一来源（默认 27101），
# 避免改端口后这里的默认白名单变成陈旧值。
_AJST_PORT = os.environ.get('PORT', '27101')
if _cors_env == '*':
    CORS_ORIGINS = '*'
else:
    CORS_ORIGINS = [o.strip() for o in _cors_env.split(',') if o.strip()] or [
        'http://localhost:%s' % _AJST_PORT, 'http://127.0.0.1:%s' % _AJST_PORT,
        'http://localhost:8080', 'http://127.0.0.1:8080',
    ]

# 请求体大小上限（光谱上传文本 < 几 MB，32 MB 足够）
MAX_CONTENT_LENGTH = 32 * 1024 * 1024


class Config:
    SQLALCHEMY_DATABASE_URI = DB_URL
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JSON_AS_ASCII = False  # 支持中文
    CORS_ORIGINS = CORS_ORIGINS
    MAX_CONTENT_LENGTH = MAX_CONTENT_LENGTH
    SECRET_KEY = SECRET_KEY
    AUTH_PASSWORD = AUTH_PASSWORD
    AJST_INGEST_TOKEN = AJST_INGEST_TOKEN
    SESSION_COOKIE_SAMESITE = 'Lax'
