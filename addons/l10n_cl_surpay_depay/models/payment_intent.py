import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SurpayPaymentIntent(models.Model):
    _name = "surpay.payment.intent"
    _description = "Intento de pago Surpay"
    _order = "id desc"

    state = fields.Selection(
        selection=[
            ("created", "Creado"),
            ("pending", "Pendiente"),
            ("paid", "Pagado"),
            ("failed", "Fallido"),
            ("expired", "Expirado"),
            ("cancelled", "Cancelado"),
        ],
        required=True,
        default="created",
        index=True,
    )

    provider = fields.Selection(
        selection=lambda self: self.env["surpay.provider.config"].PROVIDERS,
        default="depay",
        required=True,
        index=True,
    )
    requested_provider = fields.Char(
        string="Provider solicitado",
        help="Valor de provider recibido en el request externo. Se usa para responder respetando el contrato del integrador.",
    )

    order_id = fields.Char(required=True, index=True)
    external_order_id = fields.Char(index=True)
    provider_payment_id = fields.Char(index=True)
    base_amount = fields.Float(default=0.0)
    commission_percent = fields.Float(digits=(16, 4), default=0.0)
    commission_amount = fields.Float(default=0.0)
    commission_rule_id = fields.Many2one("surpay.commission.rule", ondelete="set null", index=True)
    amount = fields.Float(required=True)
    currency = fields.Char(required=True)
    qr_currency = fields.Char(help="Moneda final del QR devuelta por el proveedor.")
    qr_converted_amount = fields.Float(help="Monto convertido al tipo de cambio devuelto por el proveedor.")
    qr_exchange_rate = fields.Float(digits=(16, 8))
    idempotency_key = fields.Char(required=True, index=True)
    source_channel = fields.Selection(
        selection=[("external", "Externo"), ("internal", "Interno")],
        required=True,
        default="external",
        index=True,
    )
    qr_from = fields.Char(help="Codigo ISO 3166-1 alpha-2 usado como origen del QR.")

    client_id = fields.Many2one("surpay.api.client", required=True, ondelete="restrict", index=True)
    provider_config_id = fields.Many2one("surpay.provider.config", ondelete="set null", index=True)
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    notification_url = fields.Char()
    return_url = fields.Char()
    return_url_behavior = fields.Selection(
        selection=[
            ("webhook_only", "Solo webhook"),
            ("odoo_final_screen", "Pantalla final Odoo"),
            ("auto_redirect", "Redirección automática"),
        ],
        default="webhook_only",
    )
    expires_at = fields.Datetime(required=True)

    concept = fields.Char()
    provider_request_payload = fields.Json()
    provider_response_payload = fields.Json()
    payment_link_token = fields.Char(copy=False, index=True)
    transaction_id = fields.Many2one("surpay.payment.transaction", ondelete="set null", index=True)

    _sql_constraints = [
        ("surpay_payment_order_uniq", "unique(order_id)", "El order_id debe ser unico."),
        (
            "surpay_payment_idempotency_uniq",
            "unique(client_id, idempotency_key)",
            "El idempotency_key debe ser unico por cliente.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("payment_link_token"):
                vals["payment_link_token"] = uuid.uuid4().hex
            if vals.get("base_amount") is None:
                vals["base_amount"] = vals.get("amount", 0.0)
        records = super().create(vals_list)
        records.sync_transaction()
        return records

    @api.model
    def _default_expiration_seconds(self):
        value = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_cl_surpay_depay.default_expiration_seconds", "900"
        )
        try:
            seconds = int(value)
        except ValueError as exc:
            raise ValidationError(_("Configuracion invalida de segundos de expiracion por defecto.")) from exc

        return max(300, min(seconds, 3600))

    @api.model
    def generate_order_id(self):
        return f"pay_{uuid.uuid4()}"

    @api.model
    def build_expiration(self, requested_seconds=None):
        seconds = self._default_expiration_seconds()
        if requested_seconds is not None:
            seconds = max(300, min(int(requested_seconds), 3600))

        return fields.Datetime.now() + timedelta(seconds=seconds)

    def normalized_payload(self):
        self.ensure_one()
        return {
            "order_id": self.order_id,
            "external_order_id": self.external_order_id,
            "provider": self.requested_provider or self.provider,
            "provider_payment_id": self.provider_payment_id,
            "state": self.state,
            "base_amount": self.base_amount,
            "commission_percent": self.commission_percent,
            "commission_amount": self.commission_amount,
            "amount": self.amount,
            "currency": self.currency,
            "qr_currency": self.qr_currency,
            "qr_converted_amount": self.qr_converted_amount,
            "qr_exchange_rate": self.qr_exchange_rate,
            "qr_from": self.qr_from,
            "expires_at": self.expires_at,
            "payment_url": self.payment_link_url(),
            "return_url": self.return_url,
            "return_url_behavior": self.return_url_behavior,
        }

    def payment_link_url(self):
        self.ensure_one()
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        token = self.payment_link_token or ""
        if not token:
            return ""
        if base_url:
            return f"{base_url.rstrip('/')}/pay/{token}"
        return f"/pay/{token}"

    def ensure_transaction(self):
        self.ensure_one()
        if self.transaction_id:
            return self.transaction_id

        seller_user = self.client_id.resolve_seller_user(partner=self.partner_id, fallback_to_admin=True)
        tx = self.env["surpay.payment.transaction"].sudo().create(
            {
                "order_id": self.order_id,
                "external_order_id": self.external_order_id,
                "provider": self.provider,
                "provider_config_id": self.provider_config_id.id,
                "provider_payment_id": self.provider_payment_id,
                "state": self.state,
                "base_amount": self.base_amount,
                "commission_percent": self.commission_percent,
                "commission_amount": self.commission_amount,
                "commission_rule_id": self.commission_rule_id.id,
                "amount": self.amount,
                "currency": self.currency,
                "qr_currency": self.qr_currency,
                "qr_converted_amount": self.qr_converted_amount,
                "qr_exchange_rate": self.qr_exchange_rate,
                "qr_from": self.qr_from,
                "client_id": self.client_id.id,
                "partner_id": (self.partner_id or self.client_id.partner_id).id,
                "sales_channel": self.source_channel or "external",
                "provider_raw": self.provider_response_payload,
                "concept": self.concept,
                "seller_user_id": seller_user.id,
            }
        )
        self.transaction_id = tx.id
        return tx

    def sync_transaction(self):
        for rec in self:
            tx = rec.ensure_transaction()
            seller_user = rec.client_id.resolve_seller_user(partner=rec.partner_id, fallback_to_admin=True)
            incoming_provider_raw = dict(rec.provider_response_payload or {})
            existing_provider_raw = dict(tx.provider_raw or {})
            if isinstance(existing_provider_raw.get("extra_data"), dict):
                # Preserve extra metadata attached by /api/v1/payments/extra-data.
                incoming_provider_raw["extra_data"] = existing_provider_raw.get("extra_data")
            vals = {
                "external_order_id": rec.external_order_id,
                "provider": rec.provider,
                "provider_config_id": rec.provider_config_id.id,
                "provider_payment_id": rec.provider_payment_id,
                "state": rec.state,
                "base_amount": rec.base_amount,
                "commission_percent": rec.commission_percent,
                "commission_amount": rec.commission_amount,
                "commission_rule_id": rec.commission_rule_id.id,
                "amount": rec.amount,
                "currency": rec.currency,
                "qr_currency": rec.qr_currency,
                "qr_converted_amount": rec.qr_converted_amount,
                "qr_exchange_rate": rec.qr_exchange_rate,
                "qr_from": rec.qr_from,
                "client_id": rec.client_id.id,
                "partner_id": (rec.partner_id or rec.client_id.partner_id).id,
                "sales_channel": rec.source_channel or "external",
                "provider_raw": incoming_provider_raw,
                "seller_user_id": seller_user.id,
            }
            if rec.concept:
                vals["concept"] = rec.concept
            tx.write(vals)

    @api.constrains("provider", "provider_config_id")
    def _check_provider_config_consistency(self):
        for rec in self:
            if rec.provider and rec.provider_config_id and rec.provider != rec.provider_config_id.provider:
                raise ValidationError(
                    _("El proveedor del intento debe coincidir con el proveedor de la configuración asignada.")
                )
