#!/bin/bash
# AJST transient catalog — 服务启动脚本
#
# 运行前可通过环境变量配置：
#   DATABASE_URL            PostgreSQL 连接串（默认 postgresql+psycopg2:///ajst_catalog）
#   AJST_CATALOG_PASSWORD   管理员初始密码（未设置则每次启动随机生成，请显式设置）
#   AJST_SECRET_KEY         Flask 会话密钥（未设置则每次启动随机，重启即全体登出）
#   AJST_INGEST_TOKEN       数据接入 API 的 Bearer token（未配置则 ingest 接口返回 503）
#   AJST_DATA_DIR           数据目录（默认 <项目根>/catadata）
#   AJST_CORS_ORIGINS       CORS 允许来源，逗号分隔（默认本机 localhost/127.0.0.1 常见端口）
#   AJST_PYTHON             使用的 Python 解释器（默认 python3）
#   PORT                    监听端口（默认 5000）
set -e
cd "$(dirname "$0")"
exec "${AJST_PYTHON:-python3}" -c "
import sys, os; sys.path.insert(0, '.')
from app import create_app
create_app().run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
"
