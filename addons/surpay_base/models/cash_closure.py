from datetime import datetime, time, timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date


class SurpayCashClosure(models.Model):
    _name = "surpay.cash.closure"
    _description = "Cierre de caja Surpay"
    _order = "id desc"

    name = fields.Char(string="Nombre", default="New", required=True, copy=False)
    state = fields.Selection(
        string="Estado",
        selection=[("draft", "Borrador"), ("closed", "Cerrado")],
        default="draft",
        required=True,
        index=True,
    )
    user_id = fields.Many2one("res.users", string="Usuario", required=True, default=lambda self: self.env.user, ondelete="restrict")
    seller_user_id = fields.Many2one("res.users", string="Vendedor", index=True, ondelete="set null")
    client_id = fields.Many2one("surpay.api.client", string="Cliente API", ondelete="set null", index=True)
    closure_date = fields.Date(string="Fecha de cierre", required=True, default=fields.Date.context_today, index=True)
    date_from = fields.Datetime(string="Desde", required=True, default=lambda self: fields.Datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
    date_to = fields.Datetime(string="Hasta", required=True, default=fields.Datetime.now)
    provider = fields.Char(string="Proveedor", help="Filtro opcional de proveedor, p. ej. depay")
    sales_channel = fields.Selection(
        string="Canal de venta",
        selection=[("external", "Externo"), ("internal", "Interno")],
        help="Filtro opcional de canal para el cierre.",
    )

    transaction_ids = fields.One2many("surpay.payment.transaction", "cash_closure_id", string="Transacciones")
    transaction_count = fields.Integer(string="N° Transacciones", compute="_compute_totals", store=True)
    total_amount = fields.Float(string="Total", compute="_compute_totals", store=True)
    transferred_count = fields.Integer(string="Transferidas", compute="_compute_totals", store=True)
    pending_count = fields.Integer(string="Pendientes", compute="_compute_totals", store=True)
    is_fully_transferred = fields.Boolean(string="Todo transferido", compute="_compute_totals", store=True)

    @api.depends("transaction_ids", "transaction_ids.amount", "transaction_ids.transferred")
    def _compute_totals(self):
        for rec in self:
            rec.transaction_count = len(rec.transaction_ids)
            rec.total_amount = sum(rec.transaction_ids.mapped("amount"))
            rec.transferred_count = len(rec.transaction_ids.filtered("transferred"))
            rec.pending_count = rec.transaction_count - rec.transferred_count
            rec.is_fully_transferred = bool(rec.transaction_ids) and rec.pending_count == 0

    @api.model
    def _day_window(self, day_value):
        day = fields.Date.to_date(day_value or fields.Date.context_today(self))
        start = datetime.combine(day, time.min)
        end = start + timedelta(days=1)
        return day, start, end

    @api.model
    def _apply_closure_day_bounds(self, vals):
        day_value = vals.get("closure_date")
        if not day_value:
            return
        _, start, end = self._day_window(day_value)
        vals["date_from"] = start
        vals["date_to"] = end

    @api.onchange("closure_date")
    def _onchange_closure_date(self):
        for rec in self:
            if rec.closure_date:
                _, start, end = rec._day_window(rec.closure_date)
                rec.date_from = start
                rec.date_to = end

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        for vals in vals_list:
            if not vals.get("closure_date"):
                vals["closure_date"] = fields.Date.context_today(self)
            self._apply_closure_day_bounds(vals)
            if not vals.get("name") or vals.get("name") == "New":
                vals["name"] = seq.next_by_code("surpay.cash.closure") or "CASH-CLOSURE"
        return super().create(vals_list)

    def write(self, vals):
        if "closure_date" in vals:
            self._apply_closure_day_bounds(vals)
        return super().write(vals)

    def name_get(self):
        result = []
        for rec in self:
            if rec.closure_date:
                date_label = format_date(rec.env, rec.closure_date)
                result.append((rec.id, f"{rec.name} - {date_label}"))
            else:
                result.append((rec.id, rec.name))
        return result

    def _build_tx_domain(self):
        self.ensure_one()
        _, start, end = self._day_window(self.closure_date)
        domain = [
            ("state", "=", "paid"),
            ("transferred", "=", False),
            ("cash_closure_id", "=", False),
            ("create_date", ">=", start),
            ("create_date", "<", end),
        ]
        if self.seller_user_id:
            domain.append(("seller_user_id", "=", self.seller_user_id.id))
        else:
            domain.append(("seller_user_id", "=", False))
        return domain

    @api.model
    def _prepare_daily_closures(self, day_value=None):
        day, start, end = self._day_window(day_value)
        tx_model = self.env["surpay.payment.transaction"].sudo()
        txs = tx_model.search(
            [
                ("state", "=", "paid"),
                ("transferred", "=", False),
                ("create_date", ">=", start),
                ("create_date", "<", end),
            ]
        )

        grouped = {}
        empty_set = tx_model.browse()
        for tx in txs:
            key = tx.seller_user_id.id or False
            grouped[key] = grouped.get(key, empty_set) | tx

        for seller_user_id, tx_group in grouped.items():
            closure = self.sudo().search(
                [
                    ("state", "=", "draft"),
                    ("closure_date", "=", day),
                    ("seller_user_id", "=", seller_user_id),
                ],
                limit=1,
            )
            if not closure:
                # If the box was already closed for the day and new paid sales arrived,
                # reopen the same closure so transfer tracking continues in one header.
                closed_closure = self.sudo().search(
                    [
                        ("state", "=", "closed"),
                        ("closure_date", "=", day),
                        ("seller_user_id", "=", seller_user_id),
                    ],
                    order="id desc",
                    limit=1,
                )
                if closed_closure:
                    closed_closure.state = "draft"
                    closure = closed_closure
                else:
                    closure = self.sudo().create(
                        {
                            "user_id": seller_user_id or self.env.user.id,
                            "seller_user_id": seller_user_id,
                            "closure_date": day,
                        }
                    )

            unassigned = tx_group.filtered(lambda t: not t.cash_closure_id)
            if unassigned:
                unassigned.write({"cash_closure_id": closure.id})

    @api.model
    def action_open_cash_closure(self):
        day = self.env.context.get("closure_date") or fields.Date.context_today(self)
        self._prepare_daily_closures(day)

        action = self.env.ref("surpay_base.surpay_cash_closure_action").sudo().read()[0]
        action["context"] = {
            **self.env.context,
            "search_default_draft": 1,
            "search_default_today": 1,
            "default_closure_date": day,
        }
        return action

    def action_load_transactions(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Solo los cierres en borrador pueden cargar transacciones."))
            txs = self.env["surpay.payment.transaction"].sudo().search(rec._build_tx_domain())
            txs.write({"cash_closure_id": rec.id})

    def action_close_cash(self):
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Este cierre ya esta cerrado."))
            if not rec.transaction_ids:
                rec.action_load_transactions()
            if not rec.transaction_ids:
                raise UserError(_("No hay transacciones pagadas disponibles para cerrar en el rango seleccionado."))
            pending = rec.transaction_ids.filtered(lambda t: not t.transferred)
            if pending:
                raise UserError(_("Hay transacciones pendientes sin marcar como transferidas. Marca cada detalle antes de cerrar la caja."))
            rec.state = "closed"
