import base64
import io
import zipfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class SurpayPaymentReconciliation(models.Model):
    _name = "surpay.payment.reconciliation"
    _description = "Conciliacion de pagos Surpay"
    _order = "id desc"

    name = fields.Char(string="Nombre", required=True, default="New", copy=False, readonly=True)
    state = fields.Selection(
        selection=[
            ("draft", "Borrador"),
            ("proof_in_review", "En revision de comprobante"),
            ("proof_rejected", "Comprobante rechazado"),
            ("invoice_in_review", "Factura en revision"),
            ("invoice_rejected", "Factura rechazada"),
            ("closed", "Cerrado"),
        ],
        string="Estado",
        default="draft",
        required=True,
        index=True,
    )
    seller_user_id = fields.Many2one(
        "res.users",
        string="Vendedor",
        index=True,
        ondelete="set null",
        help="Debe seleccionar primero el vendedor para filtrar los cierres disponibles.",
    )
    user_id = fields.Many2one("res.users", string="Creado por", default=lambda self: self.env.user, required=True, readonly=True)

    closure_ids = fields.Many2many(
        "surpay.cash.closure",
        "surpay_payment_reconciliation_closure_rel",
        "reconciliation_id",
        "closure_id",
        string="Cierres de caja",
        domain="[('state', '=', 'closed'), ('reconciliation_state', '=', 'none'), ('seller_user_id', '=', seller_user_id)]",
    )

    date_from = fields.Date(string="Desde", compute="_compute_period", store=True, readonly=True)
    date_to = fields.Date(string="Hasta", compute="_compute_period", store=True, readonly=True)
    period_label = fields.Char(string="Periodo", compute="_compute_period", store=True, readonly=True)

    total_to_transfer_amount = fields.Float(string="Monto transferido", compute="_compute_amounts", store=True, readonly=True)
    reconciliation_commission_percent = fields.Float(
        string="Comision de conciliacion (%)",
        digits=(16, 4),
        compute="_compute_amounts",
        store=True,
        readonly=True,
    )
    reconciliation_commission_amount = fields.Float(string="Monto comision conciliacion", compute="_compute_amounts", store=True, readonly=True)
    total_invoice_expected = fields.Float(string="Total factura esperada", compute="_compute_amounts", store=True, readonly=True)

    closure_proof_attachment_ids = fields.Many2many(
        "ir.attachment",
        compute="_compute_closure_proofs",
        string="Comprobantes de cierres",
        readonly=True,
    )
    event_attachment_ids = fields.Many2many(
        "ir.attachment",
        "surpay_payment_reconciliation_event_rel",
        "reconciliation_id",
        "attachment_id",
        string="Adjuntos para evento",
        copy=False,
    )
    proof_attachment_ids = fields.Many2many(
        "ir.attachment",
        "surpay_payment_reconciliation_proof_rel",
        "reconciliation_id",
        "attachment_id",
        string="Comprobantes de conciliacion",
        copy=False,
    )
    invoice_attachment_ids = fields.Many2many(
        "ir.attachment",
        "surpay_payment_reconciliation_invoice_rel",
        "reconciliation_id",
        "attachment_id",
        string="Facturas",
        copy=False,
    )

    restricted_observation = fields.Text(string="Observacion cliente")
    manager_observation = fields.Text(string="Observacion Surpay")

    product_line_ids = fields.One2many(
        "surpay.payment.reconciliation.product.line",
        "reconciliation_id",
        string="Productos",
        readonly=True,
    )
    extra_info_line_ids = fields.One2many(
        "surpay.payment.reconciliation.extra.info.line",
        "reconciliation_id",
        string="Extra Info",
        readonly=True,
    )
    log_ids = fields.One2many(
        "surpay.payment.reconciliation.log",
        "reconciliation_id",
        string="Bitacora",
        readonly=True,
    )

    proof_document_name = fields.Char(string="Comprobante", compute="_compute_proof_document_name", store=False)
    closure_proof_count = fields.Integer(string="N° Comprobantes", compute="_compute_proof_document_name", store=False)

    EVENT_ACTION_SELECTION = [
        ("start_review", "Iniciar conciliacion"),
        ("restricted_approve_proof", "Aprobar comprobante"),
        ("restricted_reject_proof", "Rechazar comprobante"),
        ("manager_reopen_proof", "Reenviar comprobante"),
        ("manager_accept_invoice", "Aceptar factura"),
        ("manager_reject_invoice", "Rechazar factura"),
        ("restricted_send_invoice_review", "Enviar a revision de factura"),
    ]

    @api.onchange("seller_user_id")
    def _onchange_seller_user_id(self):
        for rec in self:
            if rec.state != "draft":
                continue
            if not rec.seller_user_id:
                rec.closure_ids = [(5, 0, 0)]
                continue

            invalid = rec.closure_ids.filtered(lambda c: c.seller_user_id != rec.seller_user_id)
            if invalid:
                rec.closure_ids = [(5, 0, 0)]
                return {
                    "warning": {
                        "title": _("Vendedor actualizado"),
                        "message": _(
                            "El vendedor cambio y se limpiaron los cierres seleccionados. Debe volver a seleccionar cierres del vendedor elegido."
                        ),
                    }
                }

    @api.onchange("closure_ids")
    def _onchange_closure_ids(self):
        for rec in self:
            if rec.state != "draft":
                continue
            rec._sync_seller_from_closures()
            rec._validate_seller_scope()
            rec._rebuild_detail_lines()
            rec._compute_period()
            rec._compute_amounts()

    @api.depends("closure_ids", "closure_ids.closure_date")
    def _compute_period(self):
        for rec in self:
            dates = rec.closure_ids.mapped("closure_date")
            if not dates:
                rec.date_from = False
                rec.date_to = False
                rec.period_label = False
                continue
            start = min(dates)
            end = max(dates)
            rec.date_from = start
            rec.date_to = end
            rec.period_label = _("Del %s al %s") % (fields.Date.to_string(start), fields.Date.to_string(end))

    @api.depends(
        "closure_ids",
        "closure_ids.total_to_transfer_amount",
        "closure_ids.transaction_ids",
        "closure_ids.transaction_ids.provider",
        "closure_ids.transaction_ids.client_id",
        "closure_ids.transaction_ids.sales_channel",
    )
    def _compute_amounts(self):
        rule_model = self.env["surpay.commission.rule"].sudo()
        for rec in self:
            total = sum(rec.closure_ids.mapped("total_to_transfer_amount"))
            rec.total_to_transfer_amount = total

            provider, client_id, sales_channel = rec._get_commission_scope_values()
            pct = rule_model.resolve_reconciliation_percent(
                provider=provider,
                client_id=client_id,
                sales_channel=sales_channel,
            )
            rec.reconciliation_commission_percent = pct
            rec.reconciliation_commission_amount = total * pct / 100.0
            rec.total_invoice_expected = rec.reconciliation_commission_amount if pct else 0.0

    @api.depends("closure_ids", "closure_ids.transfer_proof_attachment_ids")
    def _compute_closure_proofs(self):
        for rec in self:
            rec.closure_proof_attachment_ids = rec.closure_ids.mapped("transfer_proof_attachment_ids")

    @api.depends("closure_proof_attachment_ids")
    def _compute_proof_document_name(self):
        for rec in self:
            docs = rec.closure_proof_attachment_ids
            rec.closure_proof_count = len(docs)
            docs = docs[:1]
            rec.proof_document_name = docs.name if docs else "-"

    def action_open_closure_proofs(self):
        self.ensure_one()
        attachments = self.closure_proof_attachment_ids.sudo()
        if not attachments:
            raise UserError(_("No hay comprobantes disponibles para esta conciliacion."))

        if len(attachments) == 1:
            return {
                "type": "ir.actions.act_url",
                "url": "/web/content/%s?download=true" % attachments.id,
                "target": "self",
            }

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for attachment in attachments:
                if not attachment.datas:
                    continue
                filename = (attachment.name or f"adjunto_{attachment.id}").strip()
                archive.writestr(filename, base64.b64decode(attachment.datas))

        if not zip_buffer.getvalue():
            raise UserError(_("No se pudo generar el ZIP de comprobantes porque los archivos no contienen datos."))

        zip_name = f"comprobantes_{self.name or self.id}.zip"
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

    def _get_commission_scope_values(self):
        self.ensure_one()
        txs = self.closure_ids.mapped("transaction_ids")
        provider = (txs[:1].provider or "") if txs else ""
        client_id = txs[:1].client_id.id if txs and txs[:1].client_id else False
        sales_channel = (txs[:1].sales_channel or False) if txs else False
        return provider, client_id, sales_channel

    def _sync_seller_from_closures(self):
        for rec in self:
            if rec.closure_ids:
                sellers = rec.closure_ids.mapped("seller_user_id")
                if len(sellers) == 1:
                    rec.seller_user_id = sellers[0]

    def _validate_seller_scope(self):
        for rec in self:
            if not rec.closure_ids:
                continue

            if not rec.seller_user_id:
                sellers = rec.closure_ids.mapped("seller_user_id")
                if len(sellers) == 1:
                    rec.seller_user_id = sellers[0]

            if not rec.seller_user_id:
                raise ValidationError(_("Debe seleccionar un vendedor antes de agregar cierres."))

            invalid = rec.closure_ids.filtered(lambda c: c.seller_user_id != rec.seller_user_id)
            if invalid:
                raise ValidationError(_("Solo puede incluir cierres del vendedor seleccionado."))

    @api.constrains("closure_ids", "seller_user_id")
    def _check_closure_scope(self):
        for rec in self:
            if not rec.closure_ids:
                continue

            rec._validate_seller_scope()

            for closure in rec.closure_ids:
                if closure.state != "closed":
                    raise ValidationError(_("Solo se pueden incluir cierres en estado cerrado."))
                if closure.reconciliation_state == "conciliated":
                    raise ValidationError(_("No se puede incluir un cierre ya conciliado: %s") % (closure.display_name,))
                if closure.reconciliation_state == "conciliating" and rec.state == "draft":
                    raise ValidationError(_("No se puede incluir un cierre que ya esta conciliando: %s") % (closure.display_name,))

    @api.model_create_multi
    def create(self, vals_list):
        seq = self.env["ir.sequence"].sudo()
        for vals in vals_list:
            if not vals.get("name") or vals.get("name") == "New":
                seq_num = seq.next_by_code("surpay.payment.reconciliation") or "0001"
                df = vals.get("date_from")
                dt = vals.get("date_to")
                if not df or not dt:
                    today = fields.Date.context_today(self)
                    df = df or today
                    dt = dt or today
                dfrom = fields.Date.to_date(df)
                dto = fields.Date.to_date(dt)
                vals["name"] = "CON/{}/{}/{}".format(dfrom.strftime("%Y%m%d"), dto.strftime("%Y%m%d"), seq_num)
        records = super().create(vals_list)
        records._sync_seller_from_closures()
        records._rebuild_detail_lines()
        records._log_event(event_type="created", message=_("Conciliacion creada."))
        return records

    def write(self, vals):
        if "seller_user_id" in vals:
            for rec in self:
                new_seller = vals.get("seller_user_id")
                if rec.closure_ids and new_seller and rec.seller_user_id and rec.seller_user_id.id != new_seller:
                    raise ValidationError(
                        _("No puede cambiar el vendedor mientras existan cierres seleccionados. Elimine primero los cierres.")
                    )

        res = super().write(vals)
        if "closure_ids" in vals:
            self._sync_seller_from_closures()
            self._validate_seller_scope()
            self._rebuild_detail_lines()
            self._compute_period()
            self._compute_amounts()
        elif "seller_user_id" in vals:
            draft_records = self.filtered(lambda r: r.state == "draft")
            if draft_records:
                draft_records._compute_amounts()
        return res

    def _is_debug_mode(self):
        debug_value = self.env.context.get("debug_force_delete_ui", self.env.context.get("debug"))
        if isinstance(debug_value, str):
            return debug_value.strip().lower() in {"1", "true", "assets", "tests", "reload"}
        return bool(debug_value)

    def _reset_linked_closures(self):
        for rec in self:
            closures = rec.closure_ids.sudo()
            if closures:
                closures.write(
                    {
                        "reconciliation_state": "none",
                        "reconciliation_id": False,
                    }
                )

    def unlink(self):
        force_started = bool(self.env.context.get("force_delete_started_reconciliation"))

        started = self.filtered(lambda r: r.state != "draft")
        if started and not force_started:
            raise UserError(
                _(
                    "No se puede eliminar una conciliacion ya iniciada. Solo las conciliaciones en borrador se pueden eliminar."
                )
            )

        if force_started:
            self._check_manager()

        self._reset_linked_closures()
        return super().unlink()

    def action_debug_force_delete(self):
        self.ensure_one()
        self._check_manager()
        action = self.env.ref("surpay_base.surpay_payment_reconciliation_action").sudo().read()[0]
        self.sudo().with_context(force_delete_started_reconciliation=True).unlink()
        action["target"] = "current"
        return action

    def _check_manager(self):
        if not (self.env.user.has_group("surpay_base.group_surpay_manager") or self.env.user.has_group("base.group_system")):
            raise UserError(_("Esta accion requiere permisos de Surpay Manager."))

    def _check_restricted(self):
        if not self.env.user.has_group("surpay_base.group_surpay_restricted_user"):
            raise UserError(_("Esta accion requiere permisos de usuario cliente restringido."))

    def _set_state(self, new_state, note=False, attachment_ids=None, message=None):
        for rec in self:
            old = rec.state
            rec.state = new_state
            rec._log_event(
                event_type="state_changed",
                state_from=old,
                state_to=new_state,
                message=message or (_("Cambio de estado: %s -> %s") % (old, new_state)),
                note=note or False,
                attachment_ids=attachment_ids,
            )

    def _log_event(self, event_type, message, state_from=False, state_to=False, note=False, attachment_ids=None):
        log_model = self.env["surpay.payment.reconciliation.log"].sudo()
        for rec in self:
            vals = {
                "reconciliation_id": rec.id,
                "event_type": event_type,
                "state_from": state_from or rec.state,
                "state_to": state_to or rec.state,
                "user_id": self.env.user.id,
                "message": message,
                "note": note or False,
            }
            if attachment_ids:
                vals["attachment_ids"] = [(6, 0, attachment_ids)]
            log_model.create(vals)

    def _prepare_event_attachments(self, attachment_ids):
        self.ensure_one()
        attachments = self.env["ir.attachment"].sudo().browse(attachment_ids).exists()
        if attachments:
            attachments.write({"res_model": "surpay.payment.reconciliation", "res_id": self.id})
        return attachments.ids

    def _open_event_wizard(self, action_key):
        self.ensure_one()
        action_label = dict(self.EVENT_ACTION_SELECTION).get(action_key, _("Evento"))
        view = self.env.ref("surpay_base.view_surpay_payment_reconciliation_event_wizard_form")
        return {
            "type": "ir.actions.act_window",
            "name": action_label,
            "res_model": "surpay.payment.reconciliation.event.wizard",
            "view_mode": "form",
            "view_id": view.id,
            "target": "new",
            "context": {
                "default_reconciliation_id": self.id,
                "default_action_key": action_key,
                "default_action_label": action_label,
            },
        }

    def execute_event_action(self, action_key, observation, attachment_ids=None):
        self.ensure_one()
        note = (observation or "").strip()
        if action_key != "start_review" and not note:
            raise UserError(_("Debe ingresar una observacion."))

        attachments = self._prepare_event_attachments(attachment_ids or [])
        if action_key == "start_review":
            return self._execute_start_review(note, attachments)
        if action_key == "restricted_approve_proof":
            return self._execute_restricted_approve_proof(note, attachments)
        if action_key == "restricted_reject_proof":
            return self._execute_restricted_reject_proof(note, attachments)
        if action_key == "manager_reopen_proof":
            return self._execute_manager_reopen_proof(note, attachments)
        if action_key == "manager_accept_invoice":
            return self._execute_manager_accept_invoice(note, attachments)
        if action_key == "manager_reject_invoice":
            return self._execute_manager_reject_invoice(note, attachments)
        if action_key == "restricted_send_invoice_review":
            return self._execute_restricted_send_invoice_review(note, attachments)

        raise UserError(_("Accion de evento no soportada."))

    @api.model
    def _extract_tx_extra_data_fields(self, tx):
        raw = tx.provider_raw or {}
        extra_data = raw.get("extra_data") if isinstance(raw, dict) else None
        if not isinstance(extra_data, dict):
            return []

        fields_list = extra_data.get("extra_data_fields")
        if isinstance(fields_list, list):
            normalized = []
            for item in fields_list:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                value = str(item.get("value") or "").strip()
                if not title or not value:
                    continue
                normalized.append({"title": title, "value": value})
            if normalized:
                return normalized

        # Compatibilidad con payloads legacy donde no exista extra_data_fields.
        fallback = []
        for key, val in extra_data.items():
            if key == "extra_data_fields":
                continue
            if isinstance(val, dict):
                title = str(val.get("title") or key).strip()
                value = str(val.get("value") or "").strip()
            else:
                title = str(key).strip()
                value = str(val).strip()
            if not title or not value:
                continue
            fallback.append({"title": title, "value": value})
        return fallback

    @api.model
    def _format_tx_extra_info(self, tx):
        fields_list = self._extract_tx_extra_data_fields(tx)
        if not fields_list:
            return ""

        if len(fields_list) == 1:
            item = fields_list[0]
            return f"{item['title']}: {item['value']}"

        visible_items = fields_list[:2]
        parts = []
        for item in visible_items:
            value = item["value"]
            value = f"{value[:17]}..." if len(value) > 20 else value
            parts.append(f"{item['title']}: {value}")

        hidden = len(fields_list) - len(visible_items)
        if hidden > 0:
            parts.append(f"+{hidden} mas")
        return " | ".join(parts)

    def _rebuild_detail_lines(self):
        product_model = self.env["surpay.payment.reconciliation.product.line"].sudo()
        extra_model = self.env["surpay.payment.reconciliation.extra.info.line"].sudo()

        for rec in self:
            if rec.id:
                rec.product_line_ids.sudo().unlink()
                rec.extra_info_line_ids.sudo().unlink()
            else:
                rec.product_line_ids = [(5, 0, 0)]
                rec.extra_info_line_ids = [(5, 0, 0)]

            txs = rec.closure_ids.mapped("transaction_ids")
            if not txs:
                continue

            lines = []
            for tx in txs.sorted(lambda t: ((t.create_date or fields.Datetime.now()), (t._origin.id or 0))):
                concept = (tx.concept or _("Sin concepto")).strip()
                line_vals = {
                    "product_name": concept,
                    "quantity": 1,
                    "amount": tx.amount or 0.0,
                    "extra_info": rec._format_tx_extra_info(tx),
                }
                if rec.id:
                    line_vals["reconciliation_id"] = rec.id
                lines.append(line_vals)
            if lines:
                if rec.id:
                    product_model.create(lines)
                else:
                    rec.product_line_ids = [(0, 0, vals) for vals in lines]

    def _execute_start_review(self, note, attachment_ids):
        self._check_manager()
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Solo se puede iniciar una conciliacion en borrador."))
            if not rec.closure_ids:
                raise UserError(_("Debe seleccionar al menos un cierre para iniciar la conciliacion."))
            rec.closure_ids.write({"reconciliation_state": "conciliating", "reconciliation_id": rec.id})
        self._set_state(
            "proof_in_review",
            note=note,
            attachment_ids=attachment_ids,
            message=_("Inicio de conciliacion por Surpay."),
        )

    def action_start_review(self):
        self.ensure_one()
        return self._open_event_wizard("start_review")

    def _execute_restricted_approve_proof(self, note, attachment_ids):
        self._check_restricted()
        for rec in self:
            if rec.state != "proof_in_review":
                raise UserError(_("Solo se puede aprobar comprobante en estado En revision de comprobante."))
            if not attachment_ids:
                raise UserError(_("Debe adjuntar una factura PDF para aprobar el comprobante."))
            rec._log_event(
                event_type="proof_approved",
                message=_("Comprobante aprobado por cliente."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("invoice_in_review")

    def action_restricted_approve_proof(self):
        self.ensure_one()
        return self._open_event_wizard("restricted_approve_proof")

    def _execute_restricted_reject_proof(self, note, attachment_ids):
        self._check_restricted()
        for rec in self:
            if rec.state != "proof_in_review":
                raise UserError(_("Solo se puede rechazar comprobante en estado En revision de comprobante."))
            rec._log_event(
                event_type="proof_rejected",
                message=_("Comprobante rechazado por cliente."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("proof_rejected")

    def action_restricted_reject_proof(self):
        self.ensure_one()
        return self._open_event_wizard("restricted_reject_proof")

    def _execute_manager_reopen_proof(self, note, attachment_ids):
        self._check_manager()
        for rec in self:
            if rec.state != "proof_rejected":
                raise UserError(_("Solo se puede reenviar comprobante desde estado Comprobante rechazado."))
            rec._log_event(
                event_type="proof_reopened",
                message=_("Comprobante reenviado a revision por Surpay."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("proof_in_review")

    def action_manager_reopen_proof(self):
        self.ensure_one()
        return self._open_event_wizard("manager_reopen_proof")

    def _execute_manager_accept_invoice(self, note, attachment_ids):
        self._check_manager()
        for rec in self:
            if rec.state != "invoice_in_review":
                raise UserError(_("Solo se puede aceptar factura en estado Factura en revision."))
            rec.closure_ids.write({"reconciliation_state": "conciliated", "reconciliation_id": rec.id})
            rec._log_event(
                event_type="invoice_accepted",
                message=_("Factura aceptada por Surpay."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("closed")

    def action_manager_accept_invoice(self):
        self.ensure_one()
        return self._open_event_wizard("manager_accept_invoice")

    def _execute_manager_reject_invoice(self, note, attachment_ids):
        self._check_manager()
        for rec in self:
            if rec.state != "invoice_in_review":
                raise UserError(_("Solo se puede rechazar factura en estado Factura en revision."))
            rec._log_event(
                event_type="invoice_rejected",
                message=_("Factura rechazada por Surpay."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("invoice_rejected")

    def action_manager_reject_invoice(self):
        self.ensure_one()
        return self._open_event_wizard("manager_reject_invoice")

    def _execute_restricted_send_invoice_review(self, note, attachment_ids):
        self._check_restricted()
        for rec in self:
            if rec.state != "invoice_rejected":
                raise UserError(_("Solo se puede enviar factura a revision desde estado Factura rechazada."))
            if not attachment_ids:
                raise UserError(_("Debe adjuntar una factura para reenviar a revision."))
            rec._log_event(
                event_type="invoice_resubmitted",
                message=_("Factura reenviada por cliente para revision."),
                note=note,
                attachment_ids=attachment_ids,
            )
        self._set_state("invoice_in_review")

    def action_restricted_send_invoice_review(self):
        self.ensure_one()
        return self._open_event_wizard("restricted_send_invoice_review")


class SurpayPaymentReconciliationProductLine(models.Model):
    _name = "surpay.payment.reconciliation.product.line"
    _description = "Linea de productos de conciliacion"
    _order = "amount desc, id asc"

    reconciliation_id = fields.Many2one("surpay.payment.reconciliation", required=True, ondelete="cascade", index=True)
    product_name = fields.Char(string="Producto", required=True)
    quantity = fields.Integer(string="Cantidad", default=1, required=True)
    amount = fields.Float(string="Monto total")
    extra_info = fields.Text(string="Extra info")


class SurpayPaymentReconciliationExtraInfoLine(models.Model):
    _name = "surpay.payment.reconciliation.extra.info.line"
    _description = "Linea de extra info de conciliacion"
    _order = "occurrences desc, id asc"

    reconciliation_id = fields.Many2one("surpay.payment.reconciliation", required=True, ondelete="cascade", index=True)
    key = fields.Char(string="Key", required=True)
    title = fields.Char(string="Titulo", required=True)
    value = fields.Char(string="Valor", required=True)
    occurrences = fields.Integer(string="Cantidad", default=1, required=True)
