import base64
import io
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError


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
    attachment_count = fields.Integer(string="N° adjuntos", compute="_compute_attachment_metadata")
    attachment_label = fields.Char(string="Adjunto", compute="_compute_attachment_metadata")

    @api.depends("attachment_ids", "attachment_ids.name")
    def _compute_attachment_metadata(self):
        for rec in self:
            attachments = rec.attachment_ids
            rec.attachment_count = len(attachments)
            if not attachments:
                rec.attachment_label = "-"
            elif len(attachments) == 1:
                rec.attachment_label = attachments.name or _("Adjunto")
            else:
                first_name = attachments[:1].name or _("Adjunto")
                rec.attachment_label = _("%s y %s mas") % (first_name, len(attachments) - 1)

    def action_download_attachments(self):
        self.ensure_one()
        attachments = self.attachment_ids.sudo()
        if not attachments:
            raise UserError(_("No hay adjuntos disponibles en este evento de bitacora."))

        if len(attachments) == 1:
            return {
                "type": "ir.actions.act_url",
                "url": "/web/content/%s?download=true" % attachments.id,
                "target": "self",
            }

        zip_buffer = io.BytesIO()
        used_names = set()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for attachment in attachments:
                if not attachment.datas:
                    continue
                filename = self._build_unique_filename(
                    (attachment.name or f"adjunto_{attachment.id}").strip(),
                    used_names,
                )
                archive.writestr(filename, base64.b64decode(attachment.datas))

        if not zip_buffer.getvalue():
            raise UserError(_("No se pudo generar el ZIP porque los adjuntos no contienen datos."))

        zip_name = "bitacora_%s_adjuntos.zip" % self.id
        zip_attachment = self.env["ir.attachment"].sudo().create(
            {
                "name": zip_name,
                "type": "binary",
                "datas": base64.b64encode(zip_buffer.getvalue()),
                "mimetype": "application/zip",
                "res_model": self._name,
                "res_id": self.id,
            }
        )
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % zip_attachment.id,
            "target": "self",
        }

    @api.model
    def _build_unique_filename(self, filename, used_names):
        if filename not in used_names:
            used_names.add(filename)
            return filename

        if "." in filename:
            base_name, extension = filename.rsplit(".", 1)
            extension = ".%s" % extension
        else:
            base_name = filename
            extension = ""

        index = 2
        candidate = "%s_%s%s" % (base_name, index, extension)
        while candidate in used_names:
            index += 1
            candidate = "%s_%s%s" % (base_name, index, extension)

        used_names.add(candidate)
        return candidate
