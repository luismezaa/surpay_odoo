# odoo_surpay con Odoo 18 + Docker

Este proyecto queda preparado con dos perfiles:

- Desarrollo: `compose.dev.yml`
- Produccion: `compose.prod.yml`

Servicios incluidos:

- Odoo 18 (imagen custom desde `Dockerfile`)
- PostgreSQL 16
- Nginx opcional en contenedor solo para desarrollo o referencia (perfil `docker-nginx`)

Nota: Nginx no se instala en el Dockerfile de Odoo; usa su propia imagen separada cuando se habilita el perfil.

## Estructura

- `Dockerfile`: imagen de Odoo 18 con dependencias de sistema
- `docker/requirements.txt`: dependencias Python adicionales
- `compose.dev.yml`: stack de desarrollo
- `compose.prod.yml`: stack de produccion
- `config/odoo.dev.conf`: configuracion Odoo para desarrollo
- `config/odoo.prod.conf`: configuracion Odoo para produccion
- `nginx/dev.conf`: proxy HTTP para desarrollo
- `nginx/prod.conf`: proxy HTTPS para produccion
- `scripts/backup.sh`: backup de DB y filestore
- `scripts/restore.sh`: restauracion de DB y filestore
- `addons/`: modulos custom
- `backups/`: salida de backups
- `nginx/certs/`: certificados TLS para produccion

## 1) Variables de entorno

Copia `.env.example` como `.env` y ajusta valores:

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `ODOO_DB_FILTER`
- `ODOO_WORKERS`
- `ODOO_MAX_CRON_THREADS`
- `ODOO_LIMIT_TIME_CPU`
- `ODOO_LIMIT_TIME_REAL`
- `LOG_MAX_SIZE`
- `LOG_MAX_FILE`
- `BACKUP_RETENTION_DAYS`

Ademas, antes de desplegar ajusta secretos en archivos de config:

- `config/odoo.dev.conf`: `surpay_encryption_key`, `admin_passwd`, `db_password`
- `config/odoo.prod.conf`: `surpay_encryption_key`, `admin_passwd`, `db_password`

En Odoo 18 la master password se toma desde `admin_passwd` en el archivo `.conf`.

No commitees secretos reales. Este repositorio incluye placeholders para compartir codigo de forma segura.

## 2) Levantar desarrollo

```bash
docker compose --env-file .env -f compose.dev.yml up -d --build
```

Si quieres levantar tambien Nginx en contenedor:

```bash
docker compose --env-file .env -f compose.dev.yml --profile docker-nginx up -d --build
```

Acceso:

- Odoo directo: http://localhost:8069
- Odoo via Nginx (si usas profile): http://localhost

## 3) Levantar produccion

Para produccion se asume Nginx en la maquina host, no en Docker.

Levanta Odoo con:

```bash
docker compose --env-file .env -f compose.prod.yml up -d --build
```

Esto deja Odoo publicado solo en localhost (`127.0.0.1:8069` y `127.0.0.1:8072`) para que lo atienda Nginx del servidor.

Acceso:

- Con Nginx del host: configura proxy a `http://127.0.0.1:8069` y `http://127.0.0.1:8072` (longpolling)

Los archivos de `nginx/` quedan solo como referencia de configuracion.

## 4) Backups

Ejecutar backup (por defecto usa `.env` y `compose.prod.yml`):

```bash
./scripts/backup.sh
```

Backup para desarrollo:

```bash
./scripts/backup.sh .env compose.dev.yml
```

## 5) Restaurar backup

```bash
./scripts/restore.sh backups/AAAAmmdd_HHMMSS
```

Restaurar en desarrollo:

```bash
./scripts/restore.sh backups/AAAAmmdd_HHMMSS .env compose.dev.yml
```

## 6) Volumenes persistentes

Desarrollo:

- `db_data_dev`
- `odoo_data_dev`

Produccion:

- `db_data_prod`
- `odoo_data_prod`

Estos volumenes mantienen DB y filestore aunque reinicies contenedores.

## 7) Hardening de produccion aplicado

Se aplicaron medidas base de seguridad y operacion:

- Nginx con TLS 1.2/1.3 y headers de seguridad
- Redireccion HTTP -> HTTPS
- Rate limiting en rutas de acceso
- Contenedores con `no-new-privileges`
- Nginx en modo `read_only` con `tmpfs` para runtime
- Limites de CPU, memoria y procesos para servicios en produccion
- Rotacion de logs Docker (`LOG_MAX_SIZE` y `LOG_MAX_FILE`)
- Rotacion automatica de backups por antiguedad (`BACKUP_RETENTION_DAYS`)

La limpieza de backups se ejecuta al final de cada corrida de `./scripts/backup.sh`.

## 8) Deploy automatico de modulos modificados (GitHub Actions)

Se incluye el workflow `.github/workflows/odoo-modules-deploy.yml` para:

- Detectar modulos cambiados bajo `addons/`.
- Conectarse por SSH al servidor.
- Ejecutar `scripts/deploy_upgrade_modules.sh` (backup + upgrade selectivo).

### Requisitos en GitHub (Secrets)

Configura estos secrets en el repositorio:

- `DEPLOY_HOST`: IP o hostname del servidor.
- `DEPLOY_USER`: usuario SSH para deploy.
- `DEPLOY_SSH_KEY`: llave privada SSH en formato PEM.
- `DEPLOY_PORT`: puerto SSH (normalmente `22`).
- `DEPLOY_PATH`: ruta absoluta del repo en el servidor (ejemplo `/opt/odoo_surpay`).

### Requisitos en servidor

- Repo clonado en `DEPLOY_PATH`.
- `.env` local configurado (no versionado).
- Docker y Docker Compose operativos.
- Nginx instalado en el host y configurado como reverse proxy hacia `127.0.0.1:8069` y `127.0.0.1:8072`.
- Stack inicial levantado al menos una vez.

### Ejecucion

- Push a `master` con cambios en `addons/**`: ejecuta deploy automatico.
- Ejecucion manual desde Actions (`workflow_dispatch`): permite indicar lista de modulos.
