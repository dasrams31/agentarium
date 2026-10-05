#!/bin/bash
# Backup harian PostgreSQL Agentarium (Fase 4).
# - pg_dump format custom terkompresi (-Fc -Z9)
# - Output: ~/workspace/agentarium/backups/agentarium-YYYYMMDD.dump
# - Retensi 7 hari (file lebih tua dihapus otomatis)
# - Kredensial: TIDAK ada password di file mana pun. pg_dump dijalankan
#   sebagai user OS 'postgres' lewat socket Unix lokal dengan peer auth.
# - Menulis backups/last_backup.json untuk dibaca /v1/status.
# - PROTEKSI: backup kosong/gagal TIDAK menimpa backup bagus sebelumnya.
# Dijalankan via agentarium-backup.service (oneshot) + agentarium-backup.timer.
set -euo pipefail

WS=/home/hatch/workspace/agentarium
BACKUP_DIR="$WS/backups"
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d)
OUT="$BACKUP_DIR/agentarium-${STAMP}.dump"
TMP="$BACKUP_DIR/.tmp-agentarium-${STAMP}.dump"

echo "[backup] dumping database 'agentarium' -> $OUT"
# Dump ke file temporary dulu. Jika pg_dump gagal, file bagus tidak tertimpa.
if ! su postgres -c "pg_dump -h /var/run/postgresql -Fc -Z9 agentarium" > "$TMP" 2>"$BACKUP_DIR/.backup-error.log"; then
  echo "[backup] GAGAL: pg_dump error, backup lama dipertahankan"
  cat "$BACKUP_DIR/.backup-error.log" >&2
  rm -f "$TMP"
  logger -t agentarium-backup "backup FAILED: pg_dump error, old backup kept"
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

# Backup valid — pindahkan ke lokasi final
mv "$TMP" "$OUT"
echo "[backup] selesai: $SIZE bytes"

# Retensi: hapus backup > 7 hari
find "$BACKUP_DIR" -maxdepth 1 -name 'agentarium-*.dump' -mtime +7 -delete

# Info backup terakhir (dibaca endpoint /v1/status)
cat > "$BACKUP_DIR/last_backup.json" <<EOF
{"file": "$(basename "$OUT")", "timestamp": "$(date -u +%FT%TZ)", "size_bytes": $SIZE, "status": "ok"}
EOF

echo "[backup] last_backup.json diperbarui"
logger -t agentarium-backup "backup ok: $(basename "$OUT") ($SIZE bytes)"
