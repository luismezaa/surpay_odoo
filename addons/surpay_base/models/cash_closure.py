from odoo import api, fields, models
from odoo.exceptions import UserError


class SurpayCashClosure(models.Model):
    _name = "surpay.cash.closure"
    _description = "Surpay Cash Closure"
    _order = "id desc"

    name = fields.Char(default="New", required=True, copy=False)
    state = fields.Selection(
        selection=[("draft", "Draft"), ("closed", "Closed")],
        default="draft",
        required=True,
        index=True,
    )
    user_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user, ondelete="restrict")
    date_from = fields.Datetime(required=True, default=lambda self: fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    date_to = fields.Datetime(required=True, default=fields.Datetime.now)
    provider = fields.Char(help="Optional provider filter, e.g. depay")
    sales_channel = fields.Selection(
        selection=[("external", "External"), ("internal", "Internal")],
        help="Optional channel filter for closure.",
    )

    transaction_ids = fields.One2many("surpay.payment.transaction", "cash_closure_id", string="Transactions")
    transaction_count = fields.Integer(compute="_compute_totals", store=True)
    total_amount = fields.Float(compute="_compute_totals", store=True)

    @api.depends("transaction_ids", "transaction_ids.amount")
    def _compute_totals(self):
        for rec in self:
            rec.transaction_count = len(rec.transaction_ids)
            rec.total_amount = sum(rec.transaction_ids.mapped("amount"))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == "New":
                vals["name"] = seq.next_by_code("surpay.cash.closure") or "CASH-CLOSURE"
        return super().create(vals_list)

    def _build_tx_domain(self):
        self.ensure_one()
        domain = [
            ("state", "=", "paid"),
            ("transferred", "=", False),
            ("cash_closure_id", "=", False),
        ]
        if self.date_from:
            domain.append(("create_date", ">=", self.date_from))
        if self.date_to:
            domain.append(("create_date", "<=", self.date_to))
        if self.provider:
            domain.append(("provider", "=", self.provider))
        if self.sales_channel:
            domain.append(("sales_channel", "=", self.sales_channel))
        return domain

    def action_load_transactions(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("Only draft closures can load transactions.")
            txs = self.env["surpay.payment.transaction"].sudo().search(rec._build_tx_domain())
            txs.write({"cash_closure_id": rec.id})

    def action_close_cash(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError("This closure is already closed.")
            if not rec.transaction_ids:
                rec.action_load_transactions()
            if not rec.transaction_ids:
                raise UserError("No paid transactions available for closure in the selected range.")
            rec.transaction_ids.write({"transferred": True})
            rec.state = "closed"
