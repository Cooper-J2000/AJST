#!/usr/bin/env bash
# ============================================================================
#  AJST 验收测试入口
#
#  用法：
#      scripts/acceptance/run_all.sh              # 跑全部（当前为 L1）
#      scripts/acceptance/run_all.sh -k zeropoint # 透传 pytest 参数
#      scripts/acceptance/run_all.sh -v
#
#  解释器解析：AJST_PYTHON 环境变量 → systemd 用户单元 drop-in（*.service.d/override.conf）
#  → 回退 python3 —— 必须直接用 <env>/bin/python，不要依赖 conda run
#  （conda 26.3.2 插件故障会静默回退到 base python，缺依赖）。
#
#  退出码：0 = 全过；1 = 有失败；2 = 环境问题（找不到解释器/pytest）
# ============================================================================
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

PY="${AJST_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for f in "$HOME"/.config/systemd/user/*.service.d/override.conf; do
    [[ -f "$f" ]] || continue
    PY="$(grep -oP 'AJST_PYTHON=\K\S+' "$f" 2>/dev/null | tail -1 | tr -d '"'"'"'')"
    [[ -n "$PY" ]] && break
  done
fi
PY="${PY:-python3}"

if [[ ! -x "$PY" ]]; then
  echo "找不到可执行的解释器: $PY" >&2
  echo "设 AJST_PYTHON=<conda env>/bin/python 后重试" >&2
  exit 2
fi
if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
  echo "解释器 $PY 里没有 pytest：$PY -m pip install pytest" >&2
  exit 2
fi

echo "AJST 验收测试  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  仓库   $REPO"
echo "  解释器 $PY"
echo

cd "$REPO"
"$PY" -m pytest tests/acceptance -q -p no:cacheprovider "$@"
rc=$?

echo
if [[ $rc -eq 0 ]]; then
  echo "全部通过。"
else
  echo "有失败（退出码 $rc）—— 先别提交，看清失败原因再决定是改代码还是改测试。"
fi
exit $rc
