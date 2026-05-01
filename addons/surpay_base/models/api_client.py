import ipaddress

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SurpayApiClient(models.Model):
    _name = "surpay.api.client"
    _description = "Surpay External API Client"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    client_id = fields.Char(required=True, index=True)
    client_secret = fields.Char(required=True)
    webhook_url = fields.Char()
    webhook_secret = fields.Char()
    partner_id = fields.Many2one(
        "res.partner",
        string="Associated Partner",
        ondelete="set null",
        index=True,
        help="Partner used to tag sales created with this API client.",
    )
    ip_filter_mode = fields.Selection(
        selection=[("all", "All IPs"), ("list", "Allow List")],
        default="all",
        required=True,
        help="Choose 'All IPs' to allow any origin, or 'Allow List' to restrict access.",
    )
    allowed_ips = fields.Text(
        help="Allowed IPs or CIDRs (one per line or comma separated). Example: 192.168.1.10, 10.0.0.0/24",
    )

    # Defaults used when request payload omits these fields.
    default_local_currency = fields.Char(default="ARS", help="ISO 4217 currency code, e.g. ARS")
    default_local_country = fields.Char(default="AR", help="ISO 3166-1 alpha-2 country code, e.g. AR")
    default_qr_from = fields.Char(default="AR", help="ISO 3166-1 alpha-2 origin country code, e.g. AR")

    _sql_constraints = [
        ("surpay_api_client_id_uniq", "unique(client_id)", "client_id must be unique."),
    ]

    @api.constrains("default_local_currency", "default_local_country", "default_qr_from")
    def _check_default_codes(self):
        for rec in self:
            if rec.default_local_currency and len(rec.default_local_currency.strip()) != 3:
                raise ValidationError("default_local_currency must be a 3-letter ISO 4217 code.")
            if rec.default_local_country and len(rec.default_local_country.strip()) != 2:
                raise ValidationError("default_local_country must be a 2-letter ISO 3166-1 alpha-2 code.")
            if rec.default_qr_from and len(rec.default_qr_from.strip()) != 2:
                raise ValidationError("default_qr_from must be a 2-letter ISO 3166-1 alpha-2 code.")

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
