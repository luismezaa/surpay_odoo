#!/usr/bin/env bash
set -euo pipefail

MODULES="${1:-}"
BRANCH="${2:-master}"
ENV_FILE="${3:-.env}"
COMPOSE_FILE="${4:-compose.prod.yml}"
MODULES="$(echo "$MODULES" | tr -d '[:space:]')"

if [[ -z "$MODULES" ]]; then
  echo "No se recibieron modulos para actualizar."
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "No existe el archivo de entorno: $ENV_FILE"
  exit 1
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "No existe el compose file: $COMPOSE_FILE"
  exit 1
fi

set -a
source "$ENV_FILE"
set +a

echo "[1/6] Cambiando a rama $BRANCH"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "[2/6] Build y arranque base"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --build db odoo

echo "[3/6] Esperando DB"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T db \
  pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"

echo "[4/6] Backup previo"
./scripts/backup.sh "$ENV_FILE" "$COMPOSE_FILE"

echo "[5/6] Upgrade de modulos: $MODULES"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T odoo \
  odoo -c /etc/odoo/odoo.conf --stop-after-init --no-http -d "$POSTGRES_DB" -u "$MODULES"

echo "[6/6] Levantando servicios"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d odoo

echo "Upgrade completado"
