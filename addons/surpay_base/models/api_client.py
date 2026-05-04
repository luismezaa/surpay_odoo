import ipaddress

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayApiClient(models.Model):
    _name = "surpay.api.client"
    _description = "Cliente API externo de Surpay"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    client_id = fields.Char(required=True, index=True)
    client_secret = fields.Char(required=True)
    webhook_url = fields.Char()
    webhook_secret = fields.Char()
    partner_id = fields.Many2one(
        "res.partner",
        string="Contacto asociado",
        ondelete="set null",
        index=True,
        help="Contacto usado para etiquetar ventas creadas con este cliente API.",
    )
    ip_filter_mode = fields.Selection(
        selection=[("all", "Todas las IPs"), ("list", "Lista permitida")],
        default="all",
        required=True,
        help="Elige 'Todas las IPs' para permitir cualquier origen, o 'Lista permitida' para restringir acceso.",
    )
    allowed_ips = fields.Text(
        help="IPs o CIDRs permitidos (uno por linea o separados por coma). Ejemplo: 192.168.1.10, 10.0.0.0/24",
    )

    # Defaults used when request payload omits these fields.
    default_local_currency = fields.Char(default="ARS", help="Codigo de moneda ISO 4217, p. ej. ARS")
    default_local_country = fields.Char(default="AR", help="Codigo de pais ISO 3166-1 alpha-2, p. ej. AR")
    default_qr_from = fields.Char(default="AR", help="Codigo de pais de origen ISO 3166-1 alpha-2, p. ej. AR")

    _sql_constraints = [
        ("surpay_api_client_id_uniq", "unique(client_id)", "El client_id debe ser unico."),
    ]

    @api.constrains("default_local_currency", "default_local_country", "default_qr_from")
    def _check_default_codes(self):
        for rec in self:
            if rec.default_local_currency and len(rec.default_local_currency.strip()) != 3:
                raise ValidationError(_("default_local_currency debe tener 3 letras (ISO 4217)."))
            if rec.default_local_country and len(rec.default_local_country.strip()) != 2:
                raise ValidationError(_("default_local_country debe tener 2 letras (ISO 3166-1 alpha-2)."))
            if rec.default_qr_from and len(rec.default_qr_from.strip()) != 2:
                raise ValidationError(_("default_qr_from debe tener 2 letras (ISO 3166-1 alpha-2)."))

    def _iter_allowed_ip_rules(self):
        self.ensure_one()
        raw = self.allowed_ips or ""
        for chunk in raw.replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                yield item

    def is_ip_allowed(self, ip_value):
        self.ensure_one()
        if self.ip_filter_mode == "all":
            return True

        if not ip_value:
            return False

        try:
            ip_obj = ipaddress.ip_address(ip_value.strip())
        except ValueError:
            return False

        for rule in self._iter_allowed_ip_rules():
            try:
                if "/" in rule:
                    network = ipaddress.ip_network(rule, strict=False)
                    if ip_obj in network:
                        return True
                else:
                    if ip_obj == ipaddress.ip_address(rule):
                        return True
            except ValueError:
                # Ignore invalid entries and continue evaluating valid rules.
                continue

        return False
