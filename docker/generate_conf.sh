#!/bin/sh
# Genera /etc/odoo/odoo.conf reemplazando placeholders con variables de entorno
set -e

TEMPLATE=/etc/odoo/odoo.conf.template
CONF=/etc/odoo/odoo.conf

if [ -f "$TEMPLATE" ]; then
    sed \
        -e "s|SURPAY_ENCRYPTION_KEY|${SURPAY_ENCRYPTION_KEY}|g" \
        -e "s|ODOO_ADMIN_PASSWD|${ODOO_ADMIN_PASSWD}|g" \
        -e "s|POSTGRES_USER|${POSTGRES_USER}|g" \
        -e "s|POSTGRES_PASSWORD|${POSTGRES_PASSWORD}|g" \
        -e "s|POSTGRES_DB|${POSTGRES_DB}|g" \
        "$TEMPLATE" > "$CONF"
fi

exec "$@"
