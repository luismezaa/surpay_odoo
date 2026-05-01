import uuid
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SurpayPaymentIntent(models.Model):
    _name = "surpay.payment.intent"
    _description = "Surpay Payment Intent"
    _order = "id desc"

    state = fields.Selection(
        selection=[
            ("created", "Created"),
            ("pending", "Pending"),
            ("paid", "Paid"),
            ("failed", "Failed"),
            ("expired", "Expired"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="created",
        index=True,
    )

    provider = fields.Selection(
        selection=[("depay", "Depay")],
        default="depay",
        required=True,
        index=True,
    )

    order_id = fields.Char(required=True, index=True)
    external_order_id = fields.Char(index=True)
    provider_payment_id = fields.Char(index=True)
    amount = fields.Float(required=True)
    currency = fields.Char(required=True)
    idempotency_key = fields.Char(required=True, index=True)
    source_channel = fields.Selection(
        selection=[("external", "External"), ("internal", "Internal")],
        required=True,
        default="external",
        index=True,
    )

    client_id = fields.Many2one("surpay.api.client", required=True, ondelete="restrict", index=True)
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    notification_url = fields.Char()
    expires_at = fields.Datetime(required=True)

    provider_request_payload = fields.Json()
    provider_response_payload = fields.Json()
    payment_link_token = fields.Char(copy=False, index=True)
    transaction_id = fields.Many2one("surpay.payment.transaction", ondelete="set null", index=True)

    _sql_constraints = [
        ("surpay_payment_order_uniq", "unique(order_id)", "order_id must be unique."),
        (
            "surpay_payment_idempotency_uniq",
            "unique(client_id, idempotency_key)",
            "idempotency_key must be unique by client.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("payment_link_token"):
                vals["payment_link_token"] = uuid.uuid4().hex
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
            raise ValidationError("Invalid default expiration seconds configuration.") from exc

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
            "provider": self.provider,
            "provider_payment_id": self.provider_payment_id,
            "state": self.state,
            "amount": self.amount,
            "currency": self.currency,
            "expires_at": self.expires_at,
            "payment_url": self.payment_link_url(),
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

        tx = self.env["surpay.payment.transaction"].sudo().create(
            {
                "order_id": self.order_id,
                "external_order_id": self.external_order_id,
                "provider": self.provider,
                "provider_payment_id": self.provider_payment_id,
                "state": self.state,
                "amount": self.amount,
                "currency": self.currency,
                "client_id": self.client_id.id,
                "partner_id": (self.partner_id or self.client_id.partner_id).id,
                "sales_channel": self.source_channel or "external",
                "provider_raw": self.provider_response_payload,
            }
        )
        self.transaction_id = tx.id
        return tx

    def sync_transaction(self):
        for rec in self:
            tx = rec.ensure_transaction()
            tx.write(
                {
                    "external_order_id": rec.external_order_id,
                    "provider": rec.provider,
                    "provider_payment_id": rec.provider_payment_id,
                    "state": rec.state,
                    "amount": rec.amount,
                    "currency": rec.currency,
                    "client_id": rec.client_id.id,
                    "partner_id": (rec.partner_id or rec.client_id.partner_id).id,
                    "sales_channel": rec.source_channel or "external",
                    "provider_raw": rec.provider_response_payload,
                }
            )
