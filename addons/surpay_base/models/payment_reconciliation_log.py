from odoo import fields, models


class SurpayPaymentReconciliationLog(models.Model):
    _name = "surpay.payment.reconciliation.log"
    _description = "Bitacora de conciliacion de pagos"
    _order = "create_date desc, id desc"

    reconciliation_id = fields.Many2one(
        "surpay.payment.reconciliation",
        string="Conciliacion",
        required=True,
        ondelete="cascade",
        index=True,
    )
    event_type = fields.Selection(
        selection=[
            ("created", "Creada"),
            ("state_changed", "Cambio de estado"),
            ("proof_approved", "Comprobante aprobado"),
            ("proof_rejected", "Comprobante rechazado"),
            ("proof_reopened", "Comprobante reenviado"),
            ("invoice_accepted", "Factura aceptada"),
            ("invoice_rejected", "Factura rechazada"),
            ("invoice_resubmitted", "Factura reenviada"),
        ],
        string="Tipo evento",
        required=True,
        default="state_changed",
    )
    state_from = fields.Selection(
        selection=[
            ("draft", "Borrador"),
            ("proof_in_review", "En revision de comprobante"),
            ("proof_rejected", "Comprobante rechazado"),
            ("invoice_in_review", "Factura en revision"),
            ("invoice_rejected", "Factura rechazada"),
            ("closed", "Cerrado"),
        ],
        string="Estado origen",
    )
    state_to = fields.Selection(
        selection=[
            ("draft", "Borrador"),
            ("proof_in_review", "En revision de comprobante"),
            ("proof_rejected", "Comprobante rechazado"),
            ("invoice_in_review", "Factura en revision"),
            ("invoice_rejected", "Factura rechazada"),
            ("closed", "Cerrado"),
        ],
        string="Estado destino",
    )
    user_id = fields.Many2one("res.users", string="Usuario", required=True, default=lambda self: self.env.user, index=True)
    message = fields.Char(string="Mensaje", required=True)
    note = fields.Text(string="Observacion")
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "surpay_payment_reconciliation_log_attachment_rel",
        "log_id",
        "attachment_id",
        string="Adjuntos",
        copy=False,
    )
