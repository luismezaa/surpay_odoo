#!/usr/bin/env python3
"""Importa clientes desde CSV y crea/actualiza usuarios de Odoo.

Uso recomendado (docker dev):
1) Copiar CSV al contenedor:
   docker cp /home/lmeza/Descargas/clientes.csv odoo_surpay_app_dev:/tmp/clientes.csv
2) Ejecutar en Odoo shell:
   docker compose -f compose.dev.yml exec odoo \
     bash -lc 'SURPAY_CLIENTS_CSV=/tmp/clientes.csv SURPAY_CLIENTS_PASSWORD=surpay123 odoo shell -c /etc/odoo/odoo.conf -d surpay_dev < /mnt/extra-addons/../scripts/import_surpay_clients.py'

Variables opcionales:
- SURPAY_CLIENTS_CSV: ruta al CSV (default: /tmp/clientes.csv)
- SURPAY_CLIENTS_PASSWORD: password comun (default: surpay123)
- SURPAY_CLIENTS_GROUP_XMLID: grupo a asignar (default: surpay_base.group_surpay_restricted_user)
"""

import csv
import os
from collections import Counter

CSV_PATH = os.environ.get("SURPAY_CLIENTS_CSV", "/tmp/clientes.csv")
DEFAULT_PASSWORD = os.environ.get("SURPAY_CLIENTS_PASSWORD", "surpay123")
GROUP_XMLID = os.environ.get("SURPAY_CLIENTS_GROUP_XMLID", "surpay_base.group_surpay_restricted_user")

Users = env["res.users"].sudo().with_context(active_test=False)
Partners = env["res.partner"].sudo().with_context(active_test=False)


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_name(value, email_value):
    name = (value or "").strip()
    if name:
        return name
    if email_value and "@" in email_value:
        return email_value.split("@", 1)[0]
    return "Cliente Surpay"


if not os.path.isfile(CSV_PATH):
    raise FileNotFoundError(f"No existe el CSV: {CSV_PATH}")

with open(CSV_PATH, newline="", encoding="utf-8") as csv_file:
    reader = csv.DictReader(csv_file)
    rows = list(reader)

emails_in_file = [normalize_email(row.get("email")) for row in rows if normalize_email(row.get("email"))]
duplicate_emails = sorted([email for email, qty in Counter(emails_in_file).items() if qty > 1])

print("=" * 72)
print("[IMPORT] Inicio importacion de clientes")
print(f"[IMPORT] CSV: {CSV_PATH}")
print(f"[IMPORT] Filas leidas: {len(rows)}")
print(f"[IMPORT] Emails validos: {len(emails_in_file)}")
if duplicate_emails:
    print("[IMPORT][WARNING] Emails duplicados detectados en el CSV:")
    for email in duplicate_emails:
        print(f"  - {email}")
else:
    print("[IMPORT] Sin emails duplicados en CSV")

base_group = env.ref("base.group_user")
surpay_group = env.ref(GROUP_XMLID)

created_users = 0
updated_users = 0
created_partners = 0
skipped_rows = 0

processed_emails = set()
for idx, row in enumerate(rows, start=2):
    raw_email = row.get("email")
    email = normalize_email(raw_email)
    name = normalize_name(row.get("nombre"), email)

    if not email:
        print(f"[IMPORT][SKIP] linea={idx} email vacio")
        skipped_rows += 1
        continue

    if email in processed_emails:
        print(f"[IMPORT][SKIP] linea={idx} email repetido en CSV: {email}")
        skipped_rows += 1
        continue

    processed_emails.add(email)

    partner = Partners.search([("email", "=", email)], limit=1)
    if not partner:
        partner = Partners.create({
            "name": name,
            "email": email,
            "company_type": "person",
            "active": True,
        })
        created_partners += 1

    user = Users.search([("login", "=", email)], limit=1)
    if user:
        update_vals = {
            "name": name,
            "email": email,
            "partner_id": partner.id,
            "active": True,
            "groups_id": [(4, base_group.id), (4, surpay_group.id)],
            "password": DEFAULT_PASSWORD,
        }
        user.write(update_vals)
        updated_users += 1
        print(f"[IMPORT][UPDATE] {email} -> user_id={user.id}")
        continue

    new_user = Users.with_context(no_reset_password=True).create(
        {
            "name": name,
            "login": email,
            "email": email,
            "partner_id": partner.id,
            "active": True,
            "groups_id": [(6, 0, [base_group.id, surpay_group.id])],
            "password": DEFAULT_PASSWORD,
        }
    )
    created_users += 1
    print(f"[IMPORT][CREATE] {email} -> user_id={new_user.id}")

print("-" * 72)
print("[IMPORT] Resumen")
print(f"[IMPORT] Usuarios creados: {created_users}")
print(f"[IMPORT] Usuarios actualizados: {updated_users}")
print(f"[IMPORT] Partners creados: {created_partners}")
print(f"[IMPORT] Filas omitidas: {skipped_rows}")
print("=" * 72)
