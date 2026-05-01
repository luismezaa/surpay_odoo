#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-.env}"
COMPOSE_FILE="${2:-compose.prod.yml}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existe el archivo de entorno: $ENV_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="backups/${TIMESTAMP}"
mkdir -p "$BACKUP_DIR"

echo "[1/3] Backup de base de datos"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F c > "$BACKUP_DIR/db.dump"

echo "[2/3] Backup de filestore"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T odoo \
  tar -czf - -C /var/lib/odoo . > "$BACKUP_DIR/odoo_data.tar.gz"

echo "[3/4] Backup completado en $BACKUP_DIR"

echo "[4/4] Rotacion de backups (retencion: ${RETENTION_DAYS} dias)"
find backups -mindepth 1 -maxdepth 1 -type d -mtime +"${RETENTION_DAYS}" -exec rm -rf {} +
echo "Rotacion finalizada"
