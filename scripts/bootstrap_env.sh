#!/usr/bin/env bash
# ============================================================================
#  AJST 运行环境重建（P2-1）
#
#  用法：
#      scripts/bootstrap_env.sh --dry-run                  # 只打印计划，不动系统
#      scripts/bootstrap_env.sh <目标env路径>               # 真建（目标已存在则拒绝）
#      scripts/bootstrap_env.sh <目标env路径> --force       # 允许写进已存在的路径
#      scripts/bootstrap_env.sh <目标env路径> --venv        # 用 venv 而不是 conda
#
#  做：建 env → 按 requirements-lock.txt 装全量依赖 → 就地验证
#      （导入核心依赖 + 跑 L1 验收测试；L1 不连库、不起服务，是检验环境的正确工具）
#  不做（末尾会打印提示，需人工）：数据库建库与数据导入、SVO/pcigale 滤光片曲线、
#      dustmaps 尘图数据、两个本地可编辑包、systemd/端口/密码配置。
#
#  ⚠ 本机踩过的坑：conda 26.3.2 的 run/install 插件会崩并静默回退到 base python，
#  所以全程直接用 <env>/bin/python 与 <env>/bin/pip，绝不经过 conda run。
#  也正因为如此，本脚本不改动现有环境 —— 只往一个新路径里建。
# ============================================================================
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LOCK="$REPO/requirements-lock.txt"
DRY=0; FORCE=0; USE_VENV=0; TARGET=""; PYBASE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    --venv)    USE_VENV=1 ;;
    --python)  PYBASE="${2:-}"; shift ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    -*)        echo "未知参数: $1" >&2; exit 2 ;;
    *)         TARGET="$1" ;;
  esac
  shift
done

[[ -f "$LOCK" ]] || { echo "缺 $LOCK" >&2; exit 2; }
if [[ -z "$TARGET" ]]; then
  if [[ $DRY -eq 1 ]]; then
    TARGET="$HOME/miniconda3/envs/ajst-rebuild"      # dry-run：拿示例路径演示，不落盘
  else
    echo "必须指定目标 env 路径（本脚本不会覆盖现有环境）。" >&2
    echo "例：scripts/bootstrap_env.sh \$HOME/miniconda3/envs/ajst-rebuild" >&2
    exit 2
  fi
fi
if [[ -e "$TARGET" && $FORCE -eq 0 && $DRY -eq 0 ]]; then
  echo "目标已存在：$TARGET" >&2
  echo "拒绝写入（重建演练请换一个新路径；确实要覆盖就加 --force）。" >&2
  exit 2
fi

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "0/4 计划"
cat <<PLAN
  仓库        $REPO
  锁文件      $LOCK  （$(grep -cE '^[A-Za-z]' "$LOCK") 个包）
  目标 env    $TARGET
  方式        $( [[ $USE_VENV -eq 1 ]] && echo "venv" || echo "conda -p" )
  解释器基座  ${PYBASE:-（默认取 python3 / conda 自带）}
PLAN
if [[ $DRY -eq 1 ]]; then
  step "dry-run：只打印将要执行的命令，未改动任何东西"
  echo "  conda create -y -p '$TARGET' python=3.12        # 或 python -m venv"
  echo "  '$TARGET/bin/pip' install -U pip"
  echo "  '$TARGET/bin/pip' install -r '$LOCK'"
  echo "  '$TARGET/bin/python' -c 'import flask, sqlalchemy, numpy, astropy, pcigale, ...'"
  echo "  AJST_PYTHON='$TARGET/bin/python' '$REPO/scripts/acceptance/run_all.sh' -k l1"
  exit 0
fi

step "1/4 建 env"
if [[ $USE_VENV -eq 1 ]]; then
  "${PYBASE:-python3}" -m venv "$TARGET"
else
  command -v conda >/dev/null || { echo "没有 conda；改用 --venv，或 --python <3.12的python>" >&2; exit 2; }
  conda create -y -p "$TARGET" python=3.12
fi
PYT="$TARGET/bin/python"
[[ -x "$PYT" ]] || { echo "建 env 失败：$PYT 不存在" >&2; exit 1; }
"$PYT" -V

step "2/4 装依赖（约 $(( $(grep -cE '^[A-Za-z]' "$LOCK") )) 个包；pcigale/astropy 体积较大）"
"$TARGET/bin/pip" install -U pip
"$TARGET/bin/pip" install -r "$LOCK"

step "3/4 导入自检"
"$PYT" - <<'PY'
import importlib, sys
mods = ['flask', 'flask_cors', 'sqlalchemy', 'psycopg2', 'numpy', 'scipy', 'astropy',
        'dustmaps', 'dust_extinction', 'extinction', 'emcee', 'configobj', 'pcigale']
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as e:
        bad.append(f'{m}: {type(e).__name__}: {e}')
print('  导入失败的模块:', bad if bad else '无')
sys.exit(1 if bad else 0)
PY

step "4/4 用 L1 验收测试验证这套环境（不连库、不起服务）"
AJST_PYTHON="$PYT" "$REPO/scripts/acceptance/run_all.sh" -k l1

step "还差什么（本脚本不碰，需人工）"
cat <<'TODO'
  · 数据库：建库 + 导入 catadata/ 数据（catadata/ 不在仓库里）
  · dustmaps 尘图数据：确认是否随包安装（当前 292MB 在 site-packages 内）；需要时 python -m dustmaps.fetch
  · SVO/pcigale 滤光片曲线：库可用后跑 scripts/fetch_svo_filters.py
  · 两个本地可编辑包（ajst_phot / photometry_ajst）：源码目录已不存在，生产代码不 import，无需安装
  · 运行配置：AJST_PYTHON / DATABASE_URL / AJST_SECRET_KEY / AJST_INGEST_TOKEN / PORT
    （写进 ~/.config/systemd/user/ajst-catalog.service.d/override.conf，见 AGENTS.md）
  · 验收：scripts/preflight.sh 退出码 0 才算这台机器可以开工
TODO
