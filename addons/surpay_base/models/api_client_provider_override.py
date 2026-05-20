from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayApiClientProviderOverride(models.Model):
    _name = "surpay.api.client.provider.override"
    _description = "Override de proveedor por cliente API"
    _order = "client_id, provider"

    client_id = fields.Many2one(
        "surpay.api.client",
        required=True,
        ondelete="cascade",
        index=True,
        string="Cliente API",
    )
    provider = fields.Selection(
        selection=lambda self: self.env["surpay.provider.config"].PROVIDERS,
        required=True,
        index=True,
        string="Proveedor",
    )
    provider_config_id = fields.Many2one(
        "surpay.provider.config",
        required=True,
        ondelete="restrict",
        string="Configuración del proveedor",
    )
    active = fields.Boolean(default=True, string="Activo")
    note = fields.Char(string="Nota")

    _sql_constraints = [
        (
            "surpay_api_client_provider_override_unique",
            "unique(client_id, provider)",
            "Solo se permite un override por proveedor para cada cliente API.",
        ),
    ]

    @api.constrains("provider", "provider_config_id")
    def _check_provider_matches_config(self):
        for rec in self:
            if rec.provider and rec.provider_config_id and rec.provider != rec.provider_config_id.provider:
                raise ValidationError(
                    _(
                        "La configuración seleccionada pertenece al proveedor '%s', "
                        "pero el override está definido para '%s'."
                    )
                    % (rec.provider_config_id.provider, rec.provider)
                )