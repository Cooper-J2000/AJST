#!/bin/bash
# 从 NASA GCN 拉取全部历史 circular 存档到 catadata/gcn/archive/
# 用法: bash scripts/fetch_gcn_archive.sh
set -euo pipefail

DEST="$(dirname "$0")/../catadata/gcn/archive"
mkdir -p "$DEST"

echo "Downloading GCN circular archive from gcn.nasa.gov ..."
curl -L --retry 3 -o /tmp/gcn_archive.tar.gz \
    https://gcn.nasa.gov/circulars/archive.json.tar.gz

echo "Extracting to $DEST ..."
tar xzf /tmp/gcn_archive.tar.gz -C "$DEST"
rm -f /tmp/gcn_archive.tar.gz

echo "Done: $(ls "$DEST" | wc -l) circulars in $DEST"
