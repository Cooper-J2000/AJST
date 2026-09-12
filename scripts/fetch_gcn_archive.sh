#!/bin/bash
# 从 NASA GCN 拉取全部历史 circular 存档到 catadata/gcn/archive/
# 用法: bash scripts/fetch_gcn_archive.sh
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/catadata/gcn/archive"
mkdir -p "$DEST"

# 临时目录与目标同文件系统（DEST 的父目录下），保证最后 mv 是原子替换
WORK="$(mktemp -d "$(dirname "$DEST")/.fetch_gcn.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

echo "Downloading GCN circular archive from gcn.nasa.gov ..."
curl -L --fail --retry 3 -o "$WORK/archive.json.tar.gz" \
    https://gcn.nasa.gov/circulars/archive.json.tar.gz

echo "Extracting ..."
mkdir "$WORK/extract"
tar xzf "$WORK/archive.json.tar.gz" -C "$WORK/extract"

NEW="$WORK/extract/archive.json"
if [ ! -d "$NEW" ]; then
    echo "ERROR: tarball 中未找到 archive.json/ 目录" >&2
    exit 1
fi

# 解压成功后才替换旧存档（旧目录先改名备份，成功后再删；替换失败则回滚）
BACKUP="$(dirname "$DEST")/archive.backup.$$"
if [ -d "$DEST" ]; then mv "$DEST" "$BACKUP"; fi
mv "$NEW" "$DEST" || {
    echo "ERROR: 替换存档失败，正在回滚旧存档" >&2
    [ -d "$BACKUP" ] && [ ! -d "$DEST" ] && mv "$BACKUP" "$DEST"
    exit 1
}
rm -rf "$BACKUP"

echo "Done: $(ls "$DEST" | wc -l) circulars in $DEST"
