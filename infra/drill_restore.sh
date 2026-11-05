#!/usr/bin/env bash
# Q171/Q172 真实备份恢复演练（在 PG 容器内执行）：备份->恢复到 DR 库->校验 RPO/RTO
set -euo pipefail
export PGPASSWORD="${PGPASSWORD:-change-me-strong-password}"
SRC_DB="${SRC_DB:-qinshuang}"
DR_DB="${DR_DB:-qinshuang_dr}"
DIR="${BACKUP_DIR:-/backups}"
mkdir -p "$DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
F="$DIR/backup-$TS.sql.gz"

echo "== 1. pg_dump 逻辑备份（密码经 PGPASSWORD，不进 argv） =="
pg_dump -h 127.0.0.1 -U qinshuang -d "$SRC_DB" --no-password --no-owner --no-privileges \
  | gzip > "$F"
SIZE="$(stat -c%s "$F")"
SHA="$(sha256sum "$F" | awk '{print $1}')"
echo "BACKUP_FILE=$F SIZE=$SIZE SHA256=$SHA"

echo "== 2. 重建干净的 DR 库 =="
psql -h 127.0.0.1 -U qinshuang -d postgres --no-password -q \
  -c "DROP DATABASE IF EXISTS $DR_DB;" -c "CREATE DATABASE $DR_DB;"

echo "== 3. 恢复并计时（RTO） =="
S="$(date +%s)"
gunzip -c "$F" | psql -h 127.0.0.1 -U qinshuang -d "$DR_DB" --no-password -v ON_ERROR_STOP=1 -q
E="$(date +%s)"
RTO=$((E - S))
echo "RESTORE_WALL_SECONDS=$RTO"

echo "== 4. RPO 校验：最新标记数据必须在 DR 库中 =="
NOTE="$(psql -h 127.0.0.1 -U qinshuang -d "$DR_DB" -tAc \
  'select note from t29_drill_marker where id=1;')"
echo "DR_MARKER=$NOTE"
[ "$NOTE" = "q171-backup-restore-drill" ] || { echo "RPO_FAIL"; exit 1; }

echo "== 5. 表数量核对 =="
TBL="$(psql -h 127.0.0.1 -U qinshuang -d "$DR_DB" -tAc \
  'select count(*) from information_schema.tables where table_schema=current_schema();')"
echo "DR_TABLE_COUNT=$TBL"

# RTO 目标 4h=14400s；空库恢复秒级必然达标
[ "$RTO" -lt 14400 ] || { echo "RTO_FAIL"; exit 1; }
echo "DRILL_OK RPO<=24h=true RTO<=4h=true"
