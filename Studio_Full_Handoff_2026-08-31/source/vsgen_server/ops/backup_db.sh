#!/usr/bin/env bash
# Ежедневный бэкап vsgen.db (аккаунты/пароли/проекты/квоты).
# sqlite3 .backup — консистентная копия ДАЖЕ при работающем сервере (в отличие от cp при WAL).
# Handoff: production paths ниже заменены безопасными примерами.
set -euo pipefail

# --- ПРОВЕРИТЬ путь к базе перед первым запуском! ---
DB="${VSGEN_DB:-/srv/studio/vsgen/vsgen.db}"
DEST="${VSGEN_BACKUP_DIR:-/srv/studio/backups/vsgen}"
KEEP="${VSGEN_BACKUP_KEEP:-14}"   # сколько последних копий держать

if [ ! -f "$DB" ]; then
  echo "ERROR: база не найдена: $DB (задай VSGEN_DB=/правильный/путь)" >&2
  exit 1
fi

mkdir -p "$DEST"
TS="$(date +%Y%m%d_%H%M%S)"
OUT="$DEST/vsgen_${TS}.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"

# ротация: удалить всё старше последних $KEEP
ls -1t "$DEST"/vsgen_*.db.gz 2>/dev/null | tail -n +"$((KEEP+1))" | xargs -r rm -f

echo "backup ok: ${OUT}.gz ($(ls -1 "$DEST"/vsgen_*.db.gz 2>/dev/null | wc -l | tr -d ' ') копий, держим $KEEP)"

# --- Установка cron (пример) ---
#   chmod +x /srv/studio/vsgen/backup_db.sh
#   (crontab -l 2>/dev/null; echo "0 4 * * * /srv/studio/vsgen/backup_db.sh >> /var/log/vsgen_backup.log 2>&1") | crontab -
# TODO: копировать $DEST на другой хост — иначе бэкап умрёт вместе с VPS.
