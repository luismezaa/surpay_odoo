#!/bin/sh
# Genera /etc/odoo/odoo.conf reemplazando placeholders con variables de entorno
set -e

TEMPLATE=/etc/odoo/odoo.conf.template
CONF=/etc/odoo/odoo.conf

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

if [ -f "$TEMPLATE" ]; then
    SURPAY_ENCRYPTION_KEY_ESCAPED=$(escape_sed_replacement "${SURPAY_ENCRYPTION_KEY}")
    ODOO_ADMIN_PASSWD_ESCAPED=$(escape_sed_replacement "${ODOO_ADMIN_PASSWD}")
    POSTGRES_USER_ESCAPED=$(escape_sed_replacement "${POSTGRES_USER}")
    POSTGRES_PASSWORD_ESCAPED=$(escape_sed_replacement "${POSTGRES_PASSWORD}")
    POSTGRES_DB_ESCAPED=$(escape_sed_replacement "${POSTGRES_DB}")

    sed \
        -e "s|SURPAY_ENCRYPTION_KEY|${SURPAY_ENCRYPTION_KEY_ESCAPED}|g" \
        -e "s|ODOO_ADMIN_PASSWD|${ODOO_ADMIN_PASSWD_ESCAPED}|g" \
        -e "s|POSTGRES_USER|${POSTGRES_USER_ESCAPED}|g" \
        -e "s|POSTGRES_PASSWORD|${POSTGRES_PASSWORD_ESCAPED}|g" \
        -e "s|POSTGRES_DB|${POSTGRES_DB_ESCAPED}|g" \
        "$TEMPLATE" > "$CONF"
fi

exec "$@"
