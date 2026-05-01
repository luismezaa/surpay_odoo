#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <ruta_backup> [archivo_env=.env] [compose_file=compose.prod.yml]"
  exit 1
fi

BACKUP_PATH="$1"
ENV_FILE="${2:-.env}"
COMPOSE_FILE="${3:-compose.prod.yml}"

if [[ ! -d "$BACKUP_PATH" ]]; then
  echo "No existe el directorio de backup: $BACKUP_PATH"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existe el archivo de entorno: $ENV_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

echo "[1/4] Restaurando base de datos"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS \"$POSTGRES_DB\";"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE \"$POSTGRES_DB\" OWNER \"$POSTGRES_USER\";"

cat "$BACKUP_PATH/db.dump" | docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists

echo "[2/4] Restaurando filestore"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T odoo rm -rf /var/lib/odoo/*
cat "$BACKUP_PATH/odoo_data.tar.gz" | docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T odoo \
  tar -xzf - -C /var/lib/odoo

echo "[3/4] Ajustando permisos"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T odoo \
  sh -c "chown -R odoo:odoo /var/lib/odoo"

echo "[4/4] Restauracion completada"
