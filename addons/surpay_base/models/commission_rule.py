from decimal import Decimal, ROUND_HALF_UP

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayCommissionRule(models.Model):
    _name = "surpay.commission.rule"
    _description = "Regla de comision Surpay"
    _order = "sequence asc, id asc"

    name = fields.Char(string="Nombre", compute="_compute_name", store=True)
    active = fields.Boolean(string="Activa", default=True)
    sequence = fields.Integer(string="Prioridad", default=10, help="Menor valor = mayor prioridad.")
    provider = fields.Selection(
        selection=[("depay", "Depay"), ("klap", "Klap")],
        string="Proveedor",
        required=True,
        index=True,
    )
    client_id = fields.Many2one(
        "surpay.api.client",
        string="Cliente API",
        ondelete="set null",
        index=True,
        help="Si se deja vacio, aplica a cualquier cliente.",
    )
    sales_channel = fields.Selection(
        selection=[("external", "Externo"), ("internal", "Interno")],
        string="Canal",
        index=True,
        help="Si se deja vacio, aplica a cualquier canal.",
    )
    commission_percent = fields.Float(
        string="Comision (%)",
        digits=(16, 4),
        required=True,
        default=0.0,
        help="Porcentaje a sumar al monto base enviado al proveedor.",
    )
    note = fields.Text(string="Notas")

    @api.depends("provider", "client_id", "sales_channel", "commission_percent")
    def _compute_name(self):
        provider_labels = dict(self._fields["provider"].selection)
        channel_labels = dict(self._fields["sales_channel"].selection)
        for rec in self:
            provider_txt = provider_labels.get(rec.provider, rec.provider or "-")
            client_txt = rec.client_id.name or "Todos los clientes"
            channel_txt = channel_labels.get(rec.sales_channel, "Todos los canales")
            rec.name = f"{provider_txt} | {client_txt} | {channel_txt} | {rec.commission_percent:.4f}%"

    @api.constrains("commission_percent")
    def _check_percent(self):
        for rec in self:
            if rec.commission_percent < 0:
                raise ValidationError(_("La comision no puede ser negativa."))

    @api.constrains("provider", "client_id", "sales_channel")
    def _check_duplicate_scope(self):
        for rec in self:
            domain = [
                ("id", "!=", rec.id),
                ("provider", "=", rec.provider),
                ("client_id", "=", rec.client_id.id or False),
                ("sales_channel", "=", rec.sales_channel or False),
            ]
            if self.search_count(domain):
                raise ValidationError(
                    _("Ya existe una regla para el mismo proveedor/cliente/canal.")
                )

    @api.model
    def _amount_quant(self, currency):
        no_decimal = {"CLP", "PYG", "JPY"}
        if (currency or "").upper() in no_decimal:
            return Decimal("1")
        return Decimal("0.01")

    @api.model
    def _default_commission_percent(self):
        """Default fallback commission when no rule matches.

        Can be overridden from System Parameters with key:
        surpay_base.default_commission_percent
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "surpay_base.default_commission_percent",
            "3.0",
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 3.0
        return max(0.0, value)

    @api.model
    def resolve_rule(self, provider, client_id=False, sales_channel=False):
        provider = (provider or "").lower().strip()
        if not provider:
            return self.browse()

        search_steps = [
            [("provider", "=", provider), ("client_id", "=", client_id or False), ("sales_channel", "=", sales_channel or False)],
            [("provider", "=", provider), ("client_id", "=", client_id or False), ("sales_channel", "=", False)],
            [("provider", "=", provider), ("client_id", "=", False), ("sales_channel", "=", sales_channel or False)],
            [("provider", "=", provider), ("client_id", "=", False), ("sales_channel", "=", False)],
        ]

        for domain in search_steps:
            rule = self.search([("active", "=", True)] + domain, order="sequence asc, id asc", limit=1)
            if rule:
                return rule
        return self.browse()

    @api.model
    def compute_amounts(self, provider, base_amount, currency, client_id=False, sales_channel=False):
        rule = self.resolve_rule(provider, client_id=client_id, sales_channel=sales_channel)
        base = Decimal(str(base_amount or 0))
        percent_value = rule.commission_percent if rule else self._default_commission_percent()
        percent = Decimal(str(percent_value))
        quant = self._amount_quant(currency)

        commission_amount = (base * percent / Decimal("100")).quantize(quant, rounding=ROUND_HALF_UP)
        total_amount = (base + commission_amount).quantize(quant, rounding=ROUND_HALF_UP)

        return {
            "rule": rule,
            "base_amount": float(base),
            "commission_percent": float(percent),
            "commission_amount": float(commission_amount),
            "total_amount": float(total_amount),
        }