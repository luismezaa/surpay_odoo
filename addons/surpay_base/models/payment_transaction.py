import base64
import hashlib
import unicodedata

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SurpayPaymentTransaction(models.Model):
    _name = "surpay.payment.transaction"
    _description = "Transaccion de pago Surpay"
    _order = "id desc"

    order_id = fields.Char(string="Orden", required=True, index=True)
    external_order_id = fields.Char(string="Orden externa", index=True)
    provider = fields.Char(string="Proveedor", required=True, index=True)
    provider_config_id = fields.Many2one("surpay.provider.config", string="Config. proveedor", index=True, ondelete="set null")
    provider_payment_id = fields.Char(string="ID pago proveedor", index=True)
    provider_client_transaction_id = fields.Char(
        string="ID transacción proveedor",
        index=True,
        help="Identificador idempotente de la transacción en el proveedor.",
    )
    provider_terminal_serial = fields.Char(
        string="Serial terminal proveedor",
        index=True,
        help="Serial de terminal utilizado por el proveedor en la transacción.",
    )
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
    total_to_transfer = fields.Float(
        string="Total a transferir",
        compute="_compute_total_to_transfer",
        store=True,
    )
    commission_rule_id = fields.Many2one(
        "surpay.commission.rule",
        string="Regla de comision",
        ondelete="set null",
        index=True,
    )
    amount = fields.Float(string="Monto", required=True)
    currency = fields.Char(string="Moneda", required=True)
    qr_from = fields.Char(string="Pais origen QR", help="Codigo ISO 3166-1 alpha-2 usado como origen del QR.")
    qr_currency = fields.Char(string="Moneda Destino", help="Moneda final del QR devuelta por el proveedor.")
    qr_converted_amount = fields.Float(string="Monto Destino", help="Monto convertido al tipo de cambio devuelto por el proveedor.")
    qr_exchange_rate = fields.Float(string="Tipo Cambio", digits=(16, 8))
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

    @api.depends("amount", "commission_amount")
    def _compute_total_to_transfer(self):
        for rec in self:
            rec.total_to_transfer = (rec.amount or 0.0) - (rec.commission_amount or 0.0)

    def _format_amount_cl(self, amount):
        return "${}".format("{:,.0f}".format(amount or 0).replace(",", "."))

    def _voucher_operation_number(self):
        self.ensure_one()
        client_id = self.sudo().client_id.id
        raw_value = "{}|{}|{}".format(
            self.id or "",
            client_id or "",
            self.create_date or "",
        )
        digest = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
        return str(int(digest[:12], 16)).zfill(12)[-12:]

    def _to_ascii(self, value):
        text = value or ""
        normalized = unicodedata.normalize("NFKD", str(text))
        return normalized.encode("ascii", "ignore").decode("ascii")

    def _build_ascii_voucher(self):
        self.ensure_one()
        tx_sudo = self.sudo()
        operation_number = self._voucher_operation_number()
        created_at = fields.Datetime.context_timestamp(self, self.create_date) if self.create_date else None
        created_at_text = created_at.strftime("%d/%m/%Y %H:%M:%S") if created_at else ""
        client_name = self._to_ascii(tx_sudo.client_id.display_name)
        concept = self._to_ascii(self.concept)
        amount_text = self._format_amount_cl(self.amount)
        base_amount_text = self._format_amount_cl(self.base_amount)
        qr_amount_text = self._format_amount_cl(self.qr_converted_amount)
        currency = self._to_ascii(self.currency)
        qr_currency = self._to_ascii(self.qr_currency)
        order_id = self._to_ascii(self.order_id)

        line = "-" * 42
        return "\n".join(
            [
                "SURPAY",
                "COMPROBANTE DE PAGO",
                line,
                "N Operacion: {}".format(operation_number),
                "Cliente    : {}".format(client_name[:30]),
                "Fecha      : {}".format(created_at_text),
                "Motivo     : {}".format(concept[:30]),
                line,
                "Monto base : {} {}".format(base_amount_text, currency),
                "Monto total: {} {}".format(amount_text, currency),
                "Monto QR   : {} {}".format(qr_amount_text, qr_currency) if self.qr_converted_amount and qr_currency else "Monto QR   : N/D",
                "FX         : {:.8f}".format(self.qr_exchange_rate) if self.qr_exchange_rate else "FX         : N/D",
                "Orden      : {}".format(order_id[:30]),
                line,
                "Gracias por su pago",
                "",
            ]
        )

    def action_download_zebra_voucher(self):
        self.ensure_one()
        self.check_access_rights("read")
        self.check_access_rule("read")

        # Restricted users can only download vouchers from transactions owned by their user.
        if self.env.user.has_group("surpay_base.group_surpay_restricted_user") and not self.env.user.has_group(
            "surpay_base.group_surpay_manager"
        ):
            if self.seller_user_id != self.env.user:
                raise UserError(_("Solo puede descargar vouchers de sus propias transacciones."))

        if self.state != "paid":
            raise UserError(_("Solo se puede descargar voucher para transacciones pagadas."))

        txt_content = self._build_ascii_voucher().encode("ascii", "ignore")
        file_name = "voucher_{}.txt".format(self.order_id or self.id)
        attachment = self.env["ir.attachment"].sudo().create(
            {
                "name": file_name,
                "type": "binary",
                "datas": base64.b64encode(txt_content).decode("utf-8"),
                "res_model": self._name,
                "res_id": self.id,
                "mimetype": "text/plain",
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/{}?download=true".format(attachment.id),
            "target": "self",
        }
