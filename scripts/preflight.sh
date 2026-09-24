#!/usr/bin/env bash
# ============================================================================
#  AJST 预检 / preflight
#
#  目的：任何 agent（Hermes / kimi-code / OpenCode / dsh）对 AJST 动手之前，
#        把"已经踩过的坑"和"不可逆事故"拦在门口。只读、无副作用、不联网
#        （除本机服务健康检查）。
#
#  每条检查都对应一次真实事故或硬约定（依据见 AGENTS.md 与 git log）：
#    1. PGOPTIONS 残留 search_path → 命令静默写进 review 副本而不是生产库（实测踩过）
#    2. .gitignore 本身不进 git → 换机器/新 worktree 必然缺失，
#       之后 git add -A 会把 EP 数据、GRB251025B、备份、GCN 存档全部提交（不可逆）
#    3. conda 26.3.2 插件故障 → 必须直接用 <env>/bin/python，不能依赖 conda run
#    4. 服务只允许绑 127.0.0.1（硬约定，不要改回 0.0.0.0）
#    5. 无任何定时备份 → 备份新鲜度只能靠人盯
#    6. 部署顺序：先重启服务跑 init_db 迁移，再跑 etl/backfill（反了报 UndefinedColumn）
#
#  用法：
#      scripts/preflight.sh                    # 在仓库里跑
#      scripts/preflight.sh --repo /path/to/AJST_Transient_lc_Cata
#      scripts/preflight.sh --quiet            # 只打印 WARN/FAIL 与总结
#
#  退出码：0 = 无 FAIL（WARN 自行判断）；1 = 有 FAIL，先别开工；2 = 用法错误
# ============================================================================
set -uo pipefail

REPO=""
QUIET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO="${2:-}"; shift 2 ;;
    --repo=*)  REPO="${1#*=}"; shift ;;
    --quiet|-q) QUIET=1; shift ;;
    --help|-h) sed -n '3,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（--help 看用法）" >&2; exit 2 ;;
  esac
done

if [[ -z "$REPO" ]]; then
  REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi
REPO="$(cd "$REPO" 2>/dev/null && pwd -P)" || { echo "仓库路径不存在" >&2; exit 2; }

PORT="${PORT:-27101}"
DBNAME="${AJST_DB:-ajst_catalog}"
DATA_DIR="${AJST_DATA_DIR:-$REPO/catadata}"

# ---------- 输出 ----------
if [[ -t 1 ]]; then C_R=$'\033[31m'; C_G=$'\033[32m'; C_Y=$'\033[33m'; C_B=$'\033[1m'; C_0=$'\033[0m'
else C_R=""; C_G=""; C_Y=""; C_B=""; C_0=""; fi

FAILS=0; WARNS=0; SEC=""
pfx() { [[ $QUIET -eq 1 && -n "$SEC" ]] && printf '[%s] ' "$SEC"; }
section() { SEC="${1%%.*}"; [[ $QUIET -eq 1 ]] || printf '\n%s%s%s\n' "$C_B" "$1" "$C_0"; }
pass() { [[ $QUIET -eq 1 ]] || printf '  %s✓%s %-26s %s\n' "$C_G" "$C_0" "$1" "$2"; }
info() { [[ $QUIET -eq 1 ]] || printf '    %-26s %s\n' "$1" "$2"; }
warn() { WARNS=$((WARNS+1)); printf '  %s!%s %s%-26s %s\n' "$C_Y" "$C_0" "$(pfx)" "$1" "$2"; }
fail() { FAILS=$((FAILS+1)); printf '  %s✗%s %s%-26s %s\n' "$C_R" "$C_0" "$(pfx)" "$1" "$2"; }
note()  { printf '      %s→ %s%s\n' "$C_Y" "$1" "$C_0"; }
pnote() { [[ $QUIET -eq 1 ]] || note "$1"; }

printf '%sAJST 预检%s  %s\n' "$C_B" "$C_0" "$(date '+%Y-%m-%d %H:%M:%S %Z')"
printf '  仓库 %s\n' "$REPO"

# ============================================================================
section "1. 环境变量污染（写错库的头号原因）"
# ============================================================================
if [[ -n "${PGOPTIONS:-}" ]]; then
  fail "PGOPTIONS" "非空: $PGOPTIONS"
  note "唯一已知用途是 -c search_path=review,public 的临时测试；残留会让后续命令"
  note "静默写进副本而不是生产库（实测踩过）。修复：unset PGOPTIONS"
else
  pass "PGOPTIONS" "未设置"
fi

if [[ -n "${DATABASE_URL:-}" ]] && [[ "$DATABASE_URL" != *"$DBNAME"* ]]; then
  fail "DATABASE_URL" "指向非 $DBNAME: ${DATABASE_URL%%\?*}"
  note "etl.py / 服务会连到别的库。确认后 unset DATABASE_URL（或用 .env 里的值）"
else
  pass "DATABASE_URL" "${DATABASE_URL:+指向 $DBNAME}${DATABASE_URL:-未设置（走默认）}"
fi

if [[ -n "${AJST_DATA_DIR:-}" ]] && [[ "$(cd "$AJST_DATA_DIR" 2>/dev/null && pwd -P)" != "$(cd "$DATA_DIR" 2>/dev/null && pwd -P)" ]]; then
  warn "AJST_DATA_DIR" "=$AJST_DATA_DIR（与仓库内 catadata 不一致）"
  note "确认这是你想要的数据目录，否则 etl.py --dump 会写到别处"
else
  pass "AJST_DATA_DIR" "未覆盖（用仓库内 catadata/）"
fi

# ============================================================================
section "2. .gitignore 护栏（不可逆事故）"
# ============================================================================
ROOT_GI="$REPO/.gitignore"
DATA_GI="$DATA_DIR/.gitignore"
if [[ ! -f "$ROOT_GI" ]]; then
  fail "仓库 .gitignore" "不存在！"
  note "它本身不进 git，所以 clone / 新 worktree 里必然没有。"
  note "后果：git add -A 会把 EP、GRB251025B、backups、GCN 存档全部提交到公开仓库。"
  note "修复：按 AGENTS.md §6 重建（内容可直接抄那一节）"
else
  miss=()
  for p in 'catadata/' 'backend/fitting_store/' '*.log' 'AGENTS.md' '技术文档.md' '.gitignore'; do
    grep -qxF -- "$p" "$ROOT_GI" || miss+=("$p")
  done
  if [[ ${#miss[@]} -gt 0 ]]; then
    fail "仓库 .gitignore 缺项" "${miss[*]}"
  else
    pass "仓库 .gitignore" "$(wc -l < "$ROOT_GI") 行，必需项齐全"
  fi
fi

if [[ ! -f "$DATA_GI" ]]; then
  fail "catadata/.gitignore" "不存在！"
  note "后果：EP 数据、GRB251025B、backups、gcn/archive 会被提交（其中 EP/GRB251025B 不可再生）"
else
  miss=()
  for p in 'gcn/archive/' 'backups/' 'info/EP*.json' 'lc/EP*.csv' 'spectra/EP*/' \
           'info/GRB251025B.json' 'lc/GRB251025B.csv' 'spectra/GRB251025B*/' '.gitignore'; do
    grep -qxF -- "$p" "$DATA_GI" || miss+=("$p")
  done
  if [[ ${#miss[@]} -gt 0 ]]; then
    fail "catadata/.gitignore 缺项" "${miss[*]}"
  else
    pass "catadata/.gitignore" "$(wc -l < "$DATA_GI") 行，必需项齐全"
  fi
fi

# 行为验证：光有文件不算数，要看 git 是否真的忽略（-q 静默，非 0 = 未忽略）
if [[ -d "$REPO/.git" ]]; then
  notignored=()
  for p in 'AGENTS.md' '技术文档.md' 'catadata/x' 'backend/fitting_store/x' '.gitignore'; do
    git -C "$REPO" check-ignore -q -- "$p" 2>/dev/null || notignored+=("$p")
  done
  if [[ ${#notignored[@]} -gt 0 ]]; then
    fail "忽略规则未生效" "${notignored[*]}"
    note "文件里有对应行但 git 不认（可能被 -f 加过或有反向规则）。"
    note "检查：git -C \"$REPO\" check-ignore -v -- <path>"
  else
    pass "忽略规则生效" "5 个关键路径经 git check-ignore 验证"
  fi
fi

# ============================================================================
section "3. git 状态"
# ============================================================================
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  fail "git 仓库" "$REPO 不是 git 仓库"
else
  BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [[ "$BRANCH" == "HEAD" ]]; then
    warn "分支" "detached HEAD（改动容易丢）"
  else
    pass "分支" "$BRANCH"
  fi
  info "HEAD" "$(git -C "$REPO" log -1 --format='%h %ad %s' --date=short)"

  if git -C "$REPO" rev-parse --verify -q origin/main >/dev/null; then
    read -r ah bh < <(git -C "$REPO" rev-list --left-right --count HEAD...origin/main 2>/dev/null || echo "? ?")
    if [[ "$ah" == "?" ]]; then
      info "与 origin/main" "无法比较"
    elif [[ "$ah" == "0" && "$bh" == "0" ]]; then
      pass "与 origin/main" "同步（0 ahead / 0 behind，基于上次 fetch）"
    elif [[ "$bh" != "0" ]]; then
      warn "与 origin/main" "落后 $bh 个提交（先 git pull --ff-only）"
    else
      info "与 origin/main" "领先 $ah 个提交（未推送）"
    fi
  fi

  UNTRACKED="$(git -C "$REPO" status --porcelain --untracked-files=all | wc -l)"
  STAGED="$(git -C "$REPO" diff --cached --name-only | wc -l)"
  if [[ "$UNTRACKED" -gt 0 ]]; then
    warn "未提交/未跟踪" "$UNTRACKED 个文件"
    git -C "$REPO" status --porcelain --untracked-files=all | head -5 | sed 's/^/        /'
    [[ "$UNTRACKED" -gt 5 ]] && echo "        …另 $((UNTRACKED-5)) 个"
  else
    pass "未提交/未跟踪" "工作区干净"
  fi
  [[ "$STAGED" -gt 0 ]] && info "已 stage" "$STAGED 个文件"
fi

# ============================================================================
section "4. 已 stage 的危险内容"
# ============================================================================
if git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  SENSITIVE="$(git -C "$REPO" diff --cached --name-only | grep -E '(^|/)(EP[0-9]{6}|GRB251025B)' || true)"
  if [[ -n "$SENSITIVE" ]]; then
    fail "EP / GRB251025B 被 stage" ""
    echo "$SENSITIVE" | sed 's/^/        /'
    note "这些不进公开仓库（AGENTS.md §4）。修复：git restore --staged <文件>"
  else
    pass "EP / GRB251025B" "未出现在暂存区"
  fi

  CATA="$(git -C "$REPO" diff --cached --name-only | grep -E '^catadata/' || true)"
  if [[ -n "$CATA" ]]; then
    fail "catadata/ 被 stage" ""
    echo "$CATA" | head -5 | sed 's/^/        /'
    note "catadata 是独立的第二个仓库，不该出现在代码仓库的暂存区"
  else
    pass "catadata/" "未出现在暂存区"
  fi

  BIG="$(git -C "$REPO" diff --cached --numstat 2>/dev/null | awk '$1=="-" {print $3}' || true)"
  if [[ -n "$BIG" ]]; then
    warn "暂存区含二进制文件" "$(echo "$BIG" | tr '\n' ' ')"
    note "确认不是 fitting_store / 备份 / 图像误入"
  else
    pass "暂存区文件类型" "无二进制大文件"
  fi
fi

# ============================================================================
section "5. Python 环境（绕开 conda 插件故障）"
# ============================================================================
PY="${AJST_PYTHON:-}"
PY_SRC="环境变量 AJST_PYTHON"
if [[ -z "$PY" ]]; then
  DROPIN="$HOME/.config/systemd/user/ajst-catalog.service.d/override.conf"
  if [[ -f "$DROPIN" ]]; then
    PY="$(grep -oP 'AJST_PYTHON=\K\S+' "$DROPIN" 2>/dev/null | tail -1)"
    [[ -n "$PY" ]] && PY_SRC="systemd drop-in 的 AJST_PYTHON"
  fi
fi
if [[ -z "$PY" ]] && [[ -f "$HOME/.config/ajst.env" ]]; then
  PY="$(grep -oP '^[[:space:]]*(export[[:space:]]+)?AJST_PYTHON=\K\S+' "$HOME/.config/ajst.env" 2>/dev/null | tail -1 | tr -d '"'"'"'')"
  [[ -n "$PY" ]] && PY_SRC="~/.config/ajst.env 的 AJST_PYTHON"
fi

if [[ -n "$PY" ]]; then
  if [[ -x "$PY" ]]; then
    pass "解释器" "$PY"
    info "来源" "$PY_SRC"
    if DEPS="$("$PY" -c 'import flask, sqlalchemy, psycopg2, astropy, numpy; print("ok")' 2>&1)"; then
      pass "核心依赖" "flask / sqlalchemy / psycopg2 / astropy / numpy 均可导入"
    else
      fail "核心依赖" "导入失败"
      echo "$DEPS" | tail -3 | sed 's/^/        /'
      note "注意：conda run 在这个环境上会崩并静默回退到 base python —— 必须直接用它"
    fi
  else
    fail "解释器" "$PY 不存在或不可执行（来源：$PY_SRC）"
    note "AGENTS.md §7：环境是 burst_advocate；start.sh 默认的 python3 是系统 Python，没有依赖"
  fi
else
  warn "解释器" "未找到 AJST_PYTHON（未设环境变量、无 systemd drop-in、无 ajst.env）"
  note "手动 CLI 请显式指定：<conda env>/bin/python …（不要依赖 conda run）"
fi

# ============================================================================
section "6. 服务与数据库"
# ============================================================================
ACTIVE="$(systemctl --user is-active ajst-catalog 2>/dev/null || true)"
if [[ "$ACTIVE" == "active" ]]; then
  pass "systemd 服务" "ajst-catalog active"
else
  warn "systemd 服务" "ajst-catalog ${ACTIVE:-unknown}"
  note "如果这是有意的，忽略；否则 systemctl --user restart ajst-catalog"
fi

BINDS="$(ss -ltn 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $4}' | sort -u)"
if [[ -z "$BINDS" ]]; then
  info "端口 $PORT" "未监听"
elif echo "$BINDS" | grep -qE '^(0\.0\.0\.0|\*|\[::\])'; then
  fail "端口 $PORT 绑定" "$(echo "$BINDS" | tr '\n' ' ')"
  note "硬约定：只允许 127.0.0.1（AGENTS.md §7，不要移除该护栏、不要改回 0.0.0.0）"
  note "需要外部可达请走 ssh -L $PORT:127.0.0.1:$PORT"
else
  pass "端口 $PORT 绑定" "$(echo "$BINDS" | tr '\n' ' ')（仅 loopback）"
fi

if [[ "$ACTIVE" == "active" ]]; then
  CODE="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:$PORT/api/transients?limit=1" 2>/dev/null)"
  if [[ "$CODE" == "200" ]]; then
    pass "HTTP 健康" "GET /api/transients?limit=1 → 200"
  else
    fail "HTTP 健康" "返回 $CODE（期望 200）"
    note "看日志：journalctl --user -u ajst-catalog -n 50 --no-pager"
  fi
fi

if ! command -v psql >/dev/null 2>&1; then
  info "psql" "未安装，跳过数据库直连检查"
else
  ROWSTATS="$(psql -d "$DBNAME" -tAc \
    "SELECT (SELECT count(*) FROM transients)||' / '||(SELECT count(*) FROM lightcurves)" 2>&1)"
  if [[ "$ROWSTATS" =~ ^[0-9]+\ /\ [0-9]+$ ]]; then
    T="${ROWSTATS%% *}"; L="${ROWSTATS##* }"
    if [[ "$T" -gt 0 && "$L" -gt 0 ]]; then
      pass "数据库 $DBNAME" "transients=$T  lightcurves=$L"
      pnote "⚠ 涉及 etl / 回填 / 迁移前，先 pg_dump 到 catadata/backups/"
    else
      fail "数据库 $DBNAME" "行数为空！transients=$T lightcurves=$L"
      note "疑似被清库。先停下，检查 latest backup：ls -lt $DATA_DIR/backups/*.sql | head"
    fi
  else
    fail "数据库 $DBNAME" "连接/查询失败"
    echo "$ROWSTATS" | tail -2 | sed 's/^/        /'
  fi
fi

# ============================================================================
section "7. 备份与磁盘"
# ============================================================================
NEWEST="$(find "$DATA_DIR/backups" -maxdepth 1 -name '*.sql*' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1)"
NEWEST_TS="${NEWEST%% *}"
if [[ -z "$NEWEST" ]]; then
  warn "数据库备份" "catadata/backups 里没有任何 .sql"
  note "无定时备份（crontab 空、无 systemd timer）。etl/迁移前手动 pg_dump"
else
  AGE_D=$(( ( $(date +%s) - ${NEWEST_TS%%.*} ) / 86400 ))
  BNAME="$(basename "${NEWEST#* }")"
  if [[ "$AGE_D" -gt 14 ]]; then
    warn "最近备份" "$BNAME（${AGE_D} 天前）"
    note "备份全在同一块盘、无异地副本；不可再生的 EP/GRB251025B 数据也在里面"
  else
    pass "最近备份" "$BNAME（${AGE_D} 天前）"
  fi
  info "备份目录" "$(du -sh "$DATA_DIR/backups" 2>/dev/null | cut -f1) / $(ls -1 "$DATA_DIR/backups"/*.sql 2>/dev/null | wc -l) 份"
fi

USE_PCT="$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')"
if [[ -n "$USE_PCT" && "$USE_PCT" -gt 85 ]]; then
  warn "根分区占用" "${USE_PCT}%"
else
  pass "根分区占用" "${USE_PCT}%"
fi

# ============================================================================
section "8. 提交信息 scope 分布（信息项，为统一提交约定提供依据）"
# ============================================================================
if [[ $QUIET -eq 0 ]] && git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$REPO" log -50 --format='%s' \
    | sed -E 's/^([a-z0-9+_-]+):.*/\1/' \
    | grep -E '^[a-z0-9+_-]+$' | sort | uniq -c | sort -rn | head -10 \
    | awk '{printf "        %-14s %s\n", $2, $1}'
fi

# ============================================================================
printf '\n%s──── 总结 ────%s\n' "$C_B" "$C_0"
if [[ "$FAILS" -eq 0 && "$WARNS" -eq 0 ]]; then
  printf '  %s全部通过%s\n\n' "$C_G" "$C_0"
elif [[ "$FAILS" -eq 0 ]]; then
  printf '  %s%d 项警告%s，无阻塞项 —— 可以开工，但先看上面标 ! 的行\n\n' "$C_Y" "$WARNS" "$C_0"
else
  printf '  %s%d 项失败%s / %d 项警告 —— %s先别开工%s，修完再跑一次\n\n' \
    "$C_R" "$FAILS" "$C_0" "$WARNS" "$C_R" "$C_0"
fi
exit $(( FAILS > 0 ? 1 : 0 ))
