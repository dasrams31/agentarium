#!/bin/bash
# Backup harian SQLite Agentarium (pengganti backup_postgres.sh pasca migrasi
# darurat 2026-10-05: PostgreSQL hilang saat VM reset, backend kini pakai SQLite).
# - Snapshot konsisten via `sqlite3 .backup`
# - Output: ~/workspace/agentarium/backups/agentarium-YYYYMMDD.db
# - Retensi 7 hari (file lebih tua dihapus otomatis)
# - Menulis backups/last_backup.json untuk dibaca /v1/status.
# - PROTEKSI: backup kosong/gagal TIDAK menimpa backup bagus sebelumnya.
# Dijalankan via agentarium-backup.service (oneshot) + agentarium-backup.timer.
set -euo pipefail

WS=/home/hatch/workspace/agentarium
DB="$WS/data/agentarium.db"
BACKUP_DIR="$WS/backups"
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d)
OUT="$BACKUP_DIR/agentarium-${STAMP}.db"
TMP="$BACKUP_DIR/.tmp-agentarium-${STAMP}.db"

if [ ! -f "$DB" ]; then
  echo "[backup] GAGAL: DB tidak ditemukan: $DB"
  logger -t agentarium-backup "backup FAILED: db file missing"
  exit 1
fi

echo "[backup] snapshot sqlite '$DB' -> $OUT"
if ! sqlite3 "$DB" ".backup '$TMP'" 2>"$BACKUP_DIR/.backup-error.log"; then
  echo "[backup] GAGAL: sqlite backup error, backup lama dipertahankan"
  cat "$BACKUP_DIR/.backup-error.log" >&2
  rm -f "$TMP"
  logger -t agentarium-backup "backup FAILED: sqlite error, old backup kept"
  exit 1
fi

SIZE=$(stat -c%s "$TMP")

# Validasi: backup harus lebih dari 1KB (tidak kosong)
if [ "$SIZE" -lt 1024 ]; then
  echo "[backup] GAGAL: backup hanya $SIZE bytes (terlalu kecil), backup lama dipertahankan"
  rm -f "$TMP"
  logger -t agentarium-backup "backup FAILED: too small ($SIZE bytes), old backup kept"
  exit 1
fi

# Validasi: file hasil adalah SQLite valid
if ! sqlite3 "$TMP" "PRAGMA integrity_check;" 2>/dev/null | grep -q "^ok$"; then
  echo "[backup] GAGAL: integrity_check gagal, backup lama dipertahankan"
  rm -f "$TMP"
  logger -t agentarium-backup "backup FAILED: integrity_check failed, old backup kept"
  exit 1
fi

# Backup valid — pindahkan ke lokasi final
mv "$TMP" "$OUT"
echo "[backup] selesai: $SIZE bytes"

# Retensi: hapus backup > 7 hari (format .db baru; .dump lama dibiarkan apa adanya)
find "$BACKUP_DIR" -maxdepth 1 -name 'agentarium-*.db' -mtime +7 -delete

# Info backup terakhir (dibaca endpoint /v1/status)
cat > "$BACKUP_DIR/last_backup.json" <<EOF
{"file": "$(basename "$OUT")", "timestamp": "$(date -u +%FT%TZ)", "size_bytes": $SIZE, "status": "ok"}
EOF

echo "[backup] last_backup.json diperbarui"
logger -t agentarium-backup "backup ok: $(basename "$OUT") ($SIZE bytes)"
