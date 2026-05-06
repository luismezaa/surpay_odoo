from odoo import fields, models


class SurpayPaymentTransaction(models.Model):
    _name = "surpay.payment.transaction"
    _description = "Transaccion de pago Surpay"
    _order = "id desc"

    order_id = fields.Char(string="Orden", required=True, index=True)
    external_order_id = fields.Char(string="Orden externa", index=True)
    provider = fields.Char(string="Proveedor", required=True, index=True)
    provider_config_id = fields.Many2one("surpay.provider.config", string="Config. proveedor", index=True, ondelete="set null")
    provider_payment_id = fields.Char(string="ID pago proveedor", index=True)
    state = fields.Selection(
        string="Estado",
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
    base_amount = fields.Float(string="Monto base")
    commission_percent = fields.Float(string="Comision (%)", digits=(16, 4))
    commission_amount = fields.Float(string="Monto comision")
    commission_rule_id = fields.Many2one(
        "surpay.commission.rule",
        string="Regla de comision",
        ondelete="set null",
        index=True,
    )
    amount = fields.Float(string="Monto", required=True)
    currency = fields.Char(string="Moneda", required=True)
    qr_from = fields.Char(string="Pais origen QR", help="Codigo ISO 3166-1 alpha-2 usado como origen del QR.")
    concept = fields.Char(string="Concepto", index=True)
    customer_email = fields.Char(string="Email cliente")
    partner_id = fields.Many2one("res.partner", string="Socio", ondelete="set null", index=True)
    sales_channel = fields.Selection(
        string="Canal de venta",
        selection=[("external", "Externo"), ("internal", "Interno")],
        default="external",
        required=True,
        index=True,
    )
    seller_user_id = fields.Many2one("res.users", string="Vendedor", index=True, ondelete="set null")
    transferred = fields.Boolean(string="Transferida", default=False, index=True)
    cash_closure_id = fields.Many2one("surpay.cash.closure", string="Cierre de caja", ondelete="set null", index=True)
    client_id = fields.Many2one("surpay.api.client", string="Cliente", required=True, ondelete="restrict", index=True)
    provider_raw = fields.Json(string="Respuesta proveedor")

    _sql_constraints = [
        ("surpay_payment_transaction_order_uniq", "unique(order_id)", "El order_id debe ser unico."),
    ]
