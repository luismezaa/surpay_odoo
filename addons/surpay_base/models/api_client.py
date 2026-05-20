import ipaddress

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayApiClient(models.Model):
    _name = "surpay.api.client"
    _description = "Cliente API externo de Surpay"

    name = fields.Char(string="Nombre", required=True)
    active = fields.Boolean(string="Activo", default=True)
    client_id = fields.Char(string="ID cliente", required=True, index=True)
    client_secret = fields.Char(string="Secreto cliente", required=True)
    webhook_url = fields.Char(string="URL webhook")
    webhook_secret = fields.Char(string="Secreto webhook")
    provider_override_ids = fields.One2many(
        "surpay.api.client.provider.override",
        "client_id",
        string="Overrides por proveedor",
        help="Configuración por proveedor para este cliente. Si no existe override, se usa la activa global.",
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Contacto asociado",
        ondelete="set null",
        index=True,
        help="Contacto usado para etiquetar ventas creadas con este cliente API.",
    )
    ip_filter_mode = fields.Selection(
        string="Modo filtro IP",
        selection=[("all", "Todas las IPs"), ("list", "Lista permitida")],
        default="all",
        required=True,
        help="Elige 'Todas las IPs' para permitir cualquier origen, o 'Lista permitida' para restringir acceso.",
    )
    allowed_ips = fields.Text(
        string="IPs permitidas",
        help="IPs o CIDRs permitidos (uno por linea o separados por coma). Ejemplo: 192.168.1.10, 10.0.0.0/24",
    )

    # Return URL configuration per merchant.
    return_url = fields.Char(
        string="URL de retorno",
        help="URL a la que Surpay redirigirá al pagador al completarse el pago. Debe comenzar con http:// o https://",
    )
    return_url_behavior = fields.Selection(
        string="Comportamiento de retorno",
        selection=[
            ("webhook_only", "Solo webhook (sin redirección)"),
            ("odoo_final_screen", "Pantalla final en Odoo"),
            ("auto_redirect", "Redirección automática al comercio"),
        ],
        default="webhook_only",
        required=True,
        help="Define qué ocurre cuando el pago alcanza un estado terminal.",
    )

    # Defaults used when request payload omits these fields.
    default_local_currency = fields.Char(string="Moneda local", default="ARS", help="Codigo de moneda ISO 4217, p. ej. ARS")
    default_local_country = fields.Char(string="País local", default="AR", help="Codigo de pais ISO 3166-1 alpha-2, p. ej. AR")
    default_qr_from = fields.Char(string="País origen QR", default="AR", help="Codigo de pais de origen ISO 3166-1 alpha-2, p. ej. AR")

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

    @api.constrains("return_url")
    def _check_return_url(self):
        for rec in self:
            if rec.return_url:
                url = rec.return_url.strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    raise ValidationError(_("return_url debe comenzar con http:// o https://"))
                if len(url) > 500:
                    raise ValidationError(_("return_url no puede superar 500 caracteres."))

    def _iter_allowed_ip_rules(self):
        self.ensure_one()
        raw = self.allowed_ips or ""
        for chunk in raw.replace("\n", ",").split(","):
            item = chunk.strip()
            if item:
                yield item

    def resolve_seller_user(self, partner=None, fallback_to_admin=True):
        """Return the user that should own sales for this API client.

        Priority:
        1) Intent/sale partner user (commercial partner), preferring internal users.
        2) API client partner user, preferring internal users.
        3) Administrator (optional fallback).
        """
        self.ensure_one()

        def _pick_user(partner_rec):
            if not partner_rec:
                return self.env["res.users"]
            commercial = partner_rec.commercial_partner_id
            users = commercial.user_ids.filtered(lambda u: u.active)
            internal = users.filtered(lambda u: not u.share)
            return (internal or users)[:1]

        seller_user = _pick_user(partner)
        if not seller_user:
            seller_user = _pick_user(self.partner_id)
        if seller_user:
            return seller_user
        if fallback_to_admin:
            return self.env.ref("base.user_admin")
        return self.env["res.users"]

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

    def resolve_provider_config_for_provider(self, provider):
        self.ensure_one()
        provider = (provider or "").strip().lower()
        if not provider:
            return self.env["surpay.provider.config"]

        override = self.provider_override_ids.filtered(
            lambda rec: rec.active and rec.provider == provider and rec.provider_config_id
        )[:1]
        if override:
            return override.provider_config_id.sudo()

        return self.env["surpay.provider.config"].sudo().resolve_provider_config(provider=provider)
