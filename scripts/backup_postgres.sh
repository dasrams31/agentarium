#!/bin/bash
# Backup harian PostgreSQL Agentarium (Fase 4).
# - pg_dump format custom terkompresi (-Fc -Z9)
# - Output: ~/workspace/agentarium/backups/agentarium-YYYYMMDD.dump
# - Retensi 7 hari (file lebih tua dihapus otomatis)
# - Kredensial: TIDAK ada password di file mana pun. pg_dump dijalankan
#   sebagai user OS 'postgres' lewat socket Unix lokal dengan peer auth.
# - Menulis backups/last_backup.json untuk dibaca /v1/status.
# Dijalankan via agentarium-backup.service (oneshot) + agentarium-backup.timer.
set -euo pipefail

WS=/home/hatch/workspace/agentarium
BACKUP_DIR="$WS/backups"
mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y%m%d)
OUT="$BACKUP_DIR/agentarium-${STAMP}.dump"

echo "[backup] dumping database 'agentarium' -> $OUT"
# pg_dump jalan sebagai postgres (peer auth via socket), output dipipe ke file
# yang ditulis sebagai root agar tidak perlu ubah permission direktori.
su postgres -c "pg_dump -h /var/run/postgresql -Fc -Z9 agentarium" > "$OUT"

SIZE=$(stat -c%s "$OUT")
echo "[backup] selesai: $SIZE bytes"

# Retensi: hapus backup > 7 hari
find "$BACKUP_DIR" -maxdepth 1 -name 'agentarium-*.dump' -mtime +7 -delete

# Info backup terakhir (dibaca endpoint /v1/status)
cat > "$BACKUP_DIR/last_backup.json" <<EOF
{"file": "$(basename "$OUT")", "timestamp": "$(date -u +%FT%TZ)", "size_bytes": $SIZE, "status": "ok"}
EOF

echo "[backup] last_backup.json diperbarui"
logger -t agentarium-backup "backup ok: $(basename "$OUT") ($SIZE bytes)"
