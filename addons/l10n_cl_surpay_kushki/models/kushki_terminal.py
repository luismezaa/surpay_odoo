from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayProviderConfig(models.Model):
    _inherit = "surpay.provider.config"

    kushki_terminal_ids = fields.One2many(
        "surpay.kushki.terminal",
        "provider_config_id",
        string="Terminales Kushki",
        help="Listado de terminales POS por cliente para la configuración Kushki.",
    )

    def resolve_kushki_terminal(self, partner=None, terminal_serial=None):
        self.ensure_one()

        if self.provider != "kushki":
            raise ValidationError(_("La configuración seleccionada no corresponde al proveedor Kushki."))

        terminal_model = self.env["surpay.kushki.terminal"].sudo()
        serial = (terminal_serial or "").strip().upper()
        commercial_partner = partner.commercial_partner_id if partner else self.env["res.partner"]

        if serial and commercial_partner:
            terminal = terminal_model.search(
                [
                    ("provider_config_id", "=", self.id),
                    ("partner_id", "=", commercial_partner.id),
                    ("terminal_serial", "=", serial),
                    ("active", "=", True),
                ],
                limit=1,
            )
            if terminal:
                return terminal

        if serial and not commercial_partner:
            terminals = terminal_model.search(
                [
                    ("provider_config_id", "=", self.id),
                    ("terminal_serial", "=", serial),
                    ("active", "=", True),
                ],
                limit=2,
            )
            if len(terminals) > 1:
                raise ValidationError(
                    _("El serial de terminal '%s' está asociado a más de un cliente en esta configuración.") % serial
                )
            if terminals:
                return terminals[0]

        if commercial_partner:
            fallback_terminal = terminal_model.search(
                [
                    ("provider_config_id", "=", self.id),
                    ("partner_id", "=", commercial_partner.id),
                    ("active", "=", True),
                ],
                order="id asc",
                limit=1,
            )
            if fallback_terminal:
                return fallback_terminal

        if serial:
            raise ValidationError(
                _("No existe una terminal Kushki activa para el serial '%s' con el cliente seleccionado.") % serial
            )

        raise ValidationError(_("No se encontró una terminal Kushki activa para el cliente solicitado."))


class SurpayKushkiTerminal(models.Model):
    _name = "surpay.kushki.terminal"
    _description = "Terminal Kushki por cliente"
    _order = "partner_id, terminal_alias, terminal_serial"

    provider_config_id = fields.Many2one(
        "surpay.provider.config",
        string="Configuración de proveedor",
        required=True,
        ondelete="cascade",
        index=True,
        domain=[("provider", "=", "kushki")],
    )
    partner_id = fields.Many2one(
        "res.partner",
        string="Cliente",
        required=True,
        ondelete="restrict",
        index=True,
        help="Cliente (partner comercial) dueño de la terminal.",
    )
    terminal_serial = fields.Char(
        string="Serial terminal",
        required=True,
        index=True,
        help="Serial único de la terminal POS de Kushki para este cliente.",
    )
    terminal_alias = fields.Char(
        string="Alias terminal",
        help="Nombre amigable para identificar la terminal en la selección de venta interna.",
    )
    active = fields.Boolean(string="Activo", default=True)
    note = fields.Char(string="Nota")

    _sql_constraints = [
        (
            "surpay_kushki_terminal_unique",
            "unique(provider_config_id, partner_id, terminal_serial)",
            "Ya existe ese serial de terminal para el cliente dentro de esta configuración de Kushki.",
        ),
    ]

    @api.constrains("provider_config_id")
    def _check_provider_is_kushki(self):
        for rec in self:
            if rec.provider_config_id and rec.provider_config_id.provider != "kushki":
                raise ValidationError(_("Solo se pueden asociar terminales al proveedor Kushki."))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            alias = vals.get("terminal_alias")
            if alias:
                vals["terminal_alias"] = alias.strip()
            serial = vals.get("terminal_serial")
            if serial:
                vals["terminal_serial"] = serial.strip().upper()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("terminal_alias"):
            vals["terminal_alias"] = vals["terminal_alias"].strip()
        if vals.get("terminal_serial"):
            vals["terminal_serial"] = vals["terminal_serial"].strip().upper()
        return super().write(vals)

    def name_get(self):
        result = []
        for record in self:
            alias = (record.terminal_alias or "").strip()
            serial = (record.terminal_serial or "").strip()
            partner_name = record.partner_id.commercial_partner_id.name if record.partner_id else ""
            label = alias or serial or str(record.id)
            if serial and alias and serial != alias:
                label = f"{alias} ({serial})"
            elif serial and not alias:
                label = serial
            if partner_name:
                label = f"{label} - {partner_name}"
            result.append((record.id, label))
        return result
