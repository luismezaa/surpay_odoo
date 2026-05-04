from odoo import fields, models


class SurpayPaymentTransaction(models.Model):
    _name = "surpay.payment.transaction"
    _description = "Transaccion de pago Surpay"
    _order = "id desc"

    order_id = fields.Char(required=True, index=True)
    external_order_id = fields.Char(index=True)
    provider = fields.Char(required=True, index=True)
    provider_config_id = fields.Many2one("surpay.provider.config", index=True, ondelete="set null")
    provider_payment_id = fields.Char(index=True)
    state = fields.Selection(
        selection=[
            ("created", "Creada"),
            ("pending", "Pendiente"),
            ("paid", "Pagada"),
            ("failed", "Fallida"),
            ("expired", "Expirada"),
            ("cancelled", "Cancelada"),
        ],
        required=True,
        default="created",
        index=True,
    )
    amount = fields.Float(required=True)
    currency = fields.Char(required=True)
    concept = fields.Char(index=True)
    customer_email = fields.Char()
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    sales_channel = fields.Selection(
        selection=[("external", "Externo"), ("internal", "Interno")],
        default="external",
        required=True,
        index=True,
    )
    seller_user_id = fields.Many2one("res.users", index=True, ondelete="set null")
    transferred = fields.Boolean(default=False, index=True)
    cash_closure_id = fields.Many2one("surpay.cash.closure", ondelete="set null", index=True)
    client_id = fields.Many2one("surpay.api.client", required=True, ondelete="restrict", index=True)
    provider_raw = fields.Json()

    _sql_constraints = [
        ("surpay_payment_transaction_order_uniq", "unique(order_id)", "El order_id debe ser unico."),
    ]
