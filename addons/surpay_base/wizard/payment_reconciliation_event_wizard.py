from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SurpayPaymentReconciliationEventWizard(models.TransientModel):
    _name = "surpay.payment.reconciliation.event.wizard"
    _description = "Wizard de evento de conciliacion"

    reconciliation_id = fields.Many2one(
        "surpay.payment.reconciliation",
        string="Conciliacion",
        required=True,
        ondelete="cascade",
    )
    action_key = fields.Selection(
        selection=lambda self: self.env["surpay.payment.reconciliation"].EVENT_ACTION_SELECTION,
        string="Accion",
        required=True,
    )
    action_label = fields.Char(string="Evento", readonly=True)
    observation_required = fields.Boolean(string="Observacion obligatoria", compute="_compute_observation_required")
    observation = fields.Text(string="Observacion")
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "surpay_payment_reconciliation_event_wizard_attachment_rel",
        "wizard_id",
        "attachment_id",
        string="Adjuntos para evento",
        copy=False,
    )

    @api.depends("action_key")
    def _compute_observation_required(self):
        for rec in self:
            rec.observation_required = rec.action_key != "start_review"

    def action_confirm(self):
        self.ensure_one()
        rec = self.reconciliation_id.sudo()
        if not rec:
            raise UserError(_("No se encontro la conciliacion asociada."))
        if self.observation_required and not (self.observation or "").strip():
            raise UserError(_("Debe ingresar una observacion."))

        rec.execute_event_action(
            action_key=self.action_key,
            observation=self.observation,
            attachment_ids=self.attachment_ids.ids,
        )
        return {"type": "ir.actions.act_window_close"}
