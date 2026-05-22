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
    total_base_amount = fields.Float(string="Base total", compute="_compute_totals", store=True)
    total_commission_amount = fields.Float(string="Comisión total", compute="_compute_totals", store=True)
    transferred_count = fields.Integer(string="Transferidas", compute="_compute_totals", store=True)
    pending_count = fields.Integer(string="Pendientes", compute="_compute_totals", store=True)
    is_fully_transferred = fields.Boolean(string="Todo transferido", compute="_compute_totals", store=True)

    @api.depends(
        "transaction_ids",
        "transaction_ids.amount",
        "transaction_ids.base_amount",
        "transaction_ids.commission_amount",
        "transaction_ids.transferred",
    )
    def _compute_totals(self):
        for rec in self:
            rec.transaction_count = len(rec.transaction_ids)
            rec.total_amount = sum(rec.transaction_ids.mapped("amount"))
            rec.total_base_amount = sum(rec.transaction_ids.mapped("base_amount"))
            rec.total_commission_amount = sum(rec.transaction_ids.mapped("commission_amount"))
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

    @api.depends("name", "closure_date")
    def _compute_display_name(self):
        super()._compute_display_name()
        for rec in self:
            if rec.name and rec.closure_date:
                date_label = format_date(rec.env, rec.closure_date)
                rec.display_name = f"{rec.name} - {date_label}"

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
    def _get_surpay_target_user_ids(self):
        manager_group = self.env.ref("surpay_base.group_surpay_manager", raise_if_not_found=False)
        restricted_group = self.env.ref("surpay_base.group_surpay_restricted_user", raise_if_not_found=False)
        group_ids = [group.id for group in (manager_group, restricted_group) if group]
        if not group_ids:
            return []
        users = self.env["res.users"].sudo().search(
            [
                ("active", "=", True),
                ("share", "=", False),
                ("groups_id", "in", group_ids),
            ]
        )
        return users.ids

    @api.model
    def _prepare_daily_closures(self, day_value=None, seller_user_ids=None):
        day, start, end = self._day_window(day_value)
        tx_model = self.env["surpay.payment.transaction"].sudo()
        domain = [
            ("state", "=", "paid"),
            ("transferred", "=", False),
            ("cash_closure_id", "=", False),
            ("create_date", ">=", start),
            ("create_date", "<", end),
        ]

        if seller_user_ids is None:
            seller_user_ids = self._get_surpay_target_user_ids()
        elif hasattr(seller_user_ids, "ids"):
            seller_user_ids = seller_user_ids.ids

        seller_user_ids = sorted({int(uid) for uid in (seller_user_ids or []) if uid})
        if not seller_user_ids:
            return
        domain.append(("seller_user_id", "in", seller_user_ids))

        # Group by seller in SQL to avoid loading all candidate transactions in memory.
        tx_groups = tx_model.read_group(domain, ["seller_user_id"], ["seller_user_id"], lazy=False)
        seller_keys = []
        for group in tx_groups:
            seller = group.get("seller_user_id")
            seller_keys.append((seller and seller[0]) or False)

        if not seller_keys:
            return

        seller_ids = [sid for sid in seller_keys if sid]
        closure_domain = [("closure_date", "=", day), ("state", "in", ["draft", "closed"])]
        if seller_ids and False in seller_keys:
            closure_domain += ["|", ("seller_user_id", "in", seller_ids), ("seller_user_id", "=", False)]
        elif seller_ids:
            closure_domain.append(("seller_user_id", "in", seller_ids))
        else:
            closure_domain.append(("seller_user_id", "=", False))

        existing_closures = self.sudo().search(closure_domain, order="id desc")
        draft_by_seller = {}
        closed_by_seller = {}
        for closure in existing_closures:
            key = closure.seller_user_id.id or False
            if closure.state == "draft" and key not in draft_by_seller:
                draft_by_seller[key] = closure
            elif closure.state == "closed" and key not in closed_by_seller:
                closed_by_seller[key] = closure

        closures_by_seller = {}
        to_reopen = self.browse()
        to_create = []
        for seller_user_id in seller_keys:
            closure = draft_by_seller.get(seller_user_id)
            if closure:
                closures_by_seller[seller_user_id] = closure
                continue

            closure = closed_by_seller.get(seller_user_id)
            if closure:
                to_reopen |= closure
                closures_by_seller[seller_user_id] = closure
                continue

            to_create.append(
                {
                    "user_id": seller_user_id or self.env.user.id,
                    "seller_user_id": seller_user_id,
                    "closure_date": day,
                }
            )

        if to_reopen:
            to_reopen.write({"state": "draft"})

        if to_create:
            created = self.sudo().create(to_create)
            for closure in created:
                closures_by_seller[closure.seller_user_id.id or False] = closure

        for seller_user_id in seller_keys:
            closure = closures_by_seller.get(seller_user_id)
            if not closure:
                continue
            seller_domain = [("seller_user_id", "=", seller_user_id or False)]
            tx_model.search(domain + seller_domain).write({"cash_closure_id": closure.id})

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

    def action_open_report_wizard(self):
        self.ensure_one()
        default_user_id = self.seller_user_id.id or self.user_id.id
        action = self.env.ref("surpay_base.action_surpay_cash_closure_report_wizard").sudo().read()[0]
        action["context"] = {
            **self.env.context,
            "default_report_date": self.closure_date,
            "default_user_ids": [default_user_id] if default_user_id else [],
        }
        return action
