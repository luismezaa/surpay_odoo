from odoo import _, fields, models
from odoo.exceptions import UserError


class SurpayCashClosureTransferConfirmWizard(models.TransientModel):
    _name = "surpay.cash.closure.transfer.confirm.wizard"
    _description = "Confirmacion de cierre de caja con comprobante"

    closure_id = fields.Many2one("surpay.cash.closure", string="Cierre de caja", required=True, readonly=True)
    attachment_ids = fields.Many2many("ir.attachment", string="Comprobantes bancarios")
    note = fields.Char(string="Nota")

    def action_confirm_close(self):
        self.ensure_one()

        if not self.attachment_ids:
            raise UserError(_("Debe adjuntar al menos un comprobante para completar el cierre."))

        closure = self.closure_id.sudo()
        attachments = self.attachment_ids.sudo()
        attachments.write({"res_model": "surpay.cash.closure", "res_id": closure.id})
        closure.write({"transfer_proof_attachment_ids": [(4, attachment.id) for attachment in attachments]})
        closure._finalize_close_with_transfer_proof()

        return {"type": "ir.actions.act_window_close"}
