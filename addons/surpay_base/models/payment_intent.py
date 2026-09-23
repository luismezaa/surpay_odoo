import logging
import uuid
from datetime import timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class SurpayPaymentIntent(models.Model):
    _name = "surpay.payment.intent"
    _description = "Intento de pago Surpay"
    _order = "id desc"

    FINAL_STATES = ("paid", "failed", "expired", "cancelled")
    STATE_CODES = {
        "created": 1000,
        "pending": 1100,
        "paid": 2000,
        "failed": 5000,
        "expired": 5100,
        "cancelled": 5200,
    }
    FAILURE_REASON_TIMEOUT = "timeout"
    FAILURE_REASON_EXPIRED = "expired"

    state = fields.Selection(
        selection=[
            ("created", "Creado"),
            ("pending", "Pendiente"),
            ("paid", "Pagado"),
            ("failed", "Fallido"),
            ("expired", "Expirado"),
            ("cancelled", "Cancelado"),
        ],
        required=True,
        default="created",
        index=True,
    )

    provider = fields.Selection(
        selection=lambda self: self.env["surpay.provider.config"].PROVIDERS,
        default="depay",
        required=True,
        index=True,
    )
    requested_provider = fields.Char(
        string="Provider solicitado",
        help="Valor de provider recibido en el request externo. Se usa para responder respetando el contrato del integrador.",
    )

    order_id = fields.Char(required=True, index=True)
    external_order_id = fields.Char(index=True)
    provider_payment_id = fields.Char(index=True)
    provider_client_transaction_id = fields.Char(
        index=True,
        help="Identificador de transacción idempotente enviado al proveedor (ej. Kushki client_transaction_id).",
    )
    provider_terminal_serial = fields.Char(
        index=True,
        help="Serial de terminal utilizado para la operación del proveedor.",
    )
    base_amount = fields.Float(default=0.0)
    commission_percent = fields.Float(digits=(16, 4), default=0.0)
    commission_amount = fields.Float(default=0.0)
    commission_rule_id = fields.Many2one("surpay.commission.rule", ondelete="set null", index=True)
    amount = fields.Float(required=True)
    currency = fields.Char(required=True)
    qr_currency = fields.Char(help="Moneda final del QR devuelta por el proveedor.")
    qr_converted_amount = fields.Float(help="Monto convertido al tipo de cambio devuelto por el proveedor.")
    qr_exchange_rate = fields.Float(digits=(16, 8))
    idempotency_key = fields.Char(required=True, index=True)
    source_channel = fields.Selection(
        selection=[("external", "Externo"), ("internal", "Interno")],
        required=True,
        default="external",
        index=True,
    )
    qr_from = fields.Char(help="Codigo ISO 3166-1 alpha-2 usado como origen del QR.")

    client_id = fields.Many2one("surpay.api.client", required=True, ondelete="restrict", index=True)
    provider_config_id = fields.Many2one("surpay.provider.config", ondelete="set null", index=True)
    partner_id = fields.Many2one("res.partner", ondelete="set null", index=True)
    notification_url = fields.Char()
    return_url = fields.Char()
    return_url_behavior = fields.Selection(
        selection=[
            ("webhook_only", "Solo webhook"),
            ("odoo_final_screen", "Pantalla final Odoo"),
            ("auto_redirect", "Redirección automática"),
        ],
        default="webhook_only",
    )
    expires_at = fields.Datetime(required=True)
    provider_dispatched_at = fields.Datetime(
        help="Momento en que se envió el cobro al proveedor. Desde aquí corre el timeout de pago pendiente.",
    )

    concept = fields.Char()
    provider_request_payload = fields.Json()
    provider_response_payload = fields.Json()
    payment_link_token = fields.Char(copy=False, index=True)
    transaction_id = fields.Many2one("surpay.payment.transaction", ondelete="set null", index=True)

    _sql_constraints = [
        ("surpay_payment_order_uniq", "unique(order_id)", "El order_id debe ser unico."),
        (
            "surpay_payment_idempotency_uniq",
            "unique(client_id, idempotency_key)",
            "El idempotency_key debe ser unico por cliente.",
        ),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("payment_link_token"):
                vals["payment_link_token"] = uuid.uuid4().hex
            if vals.get("base_amount") is None:
                vals["base_amount"] = vals.get("amount", 0.0)
        records = super().create(vals_list)
        records.sync_transaction()
        return records

    @api.model
    def _default_expiration_seconds(self):
        params = self.env["ir.config_parameter"].sudo()
        value = params.get_param("surpay_base.payment_intent_default_expiration_seconds")
        if not value:
            value = params.get_param("l10n_cl_surpay_depay.default_expiration_seconds", "900")
        try:
            seconds = int(value)
        except ValueError as exc:
            raise ValidationError(_("Configuracion invalida de segundos de expiracion por defecto.")) from exc

        return max(300, min(seconds, 3600))

    @api.model
    def generate_order_id(self):
        return f"pay_{uuid.uuid4()}"

    @api.model
    def build_expiration(self, requested_seconds=None):
        seconds = self._default_expiration_seconds()
        if requested_seconds is not None:
            seconds = max(300, min(int(requested_seconds), 3600))

        return fields.Datetime.now() + timedelta(seconds=seconds)

    def normalized_payload(self):
        self.ensure_one()
        return {
            "order_id": self.order_id,
            "external_order_id": self.external_order_id,
            "provider": self.requested_provider or self.provider,
            "provider_payment_id": self.provider_payment_id,
            "provider_client_transaction_id": self.provider_client_transaction_id,
            "provider_terminal_serial": self.provider_terminal_serial,
            "state": self.state,
            "base_amount": self.base_amount,
            "commission_percent": self.commission_percent,
            "commission_amount": self.commission_amount,
            "amount": self.amount,
            "currency": self.currency,
            "qr_currency": self.qr_currency,
            "qr_converted_amount": self.qr_converted_amount,
            "qr_exchange_rate": self.qr_exchange_rate,
            "qr_from": self.qr_from,
            "expires_at": self.expires_at,
            "payment_url": self.payment_link_url(),
            "return_url": self.return_url,
            "return_url_behavior": self.return_url_behavior,
        }

    def payment_link_url(self):
        self.ensure_one()
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        token = self.payment_link_token or ""
        if not token:
            return ""
        if base_url:
            return f"{base_url.rstrip('/')}/pay/{token}"
        return f"/pay/{token}"

    def ensure_transaction(self):
        self.ensure_one()
        if self.transaction_id:
            return self.transaction_id

        seller_user = self.client_id.resolve_seller_user(partner=self.partner_id, fallback_to_admin=True)
        tx = self.env["surpay.payment.transaction"].sudo().create(
            {
                "order_id": self.order_id,
                "external_order_id": self.external_order_id,
                "provider": self.provider,
                "provider_config_id": self.provider_config_id.id,
                "provider_payment_id": self.provider_payment_id,
                "provider_client_transaction_id": self.provider_client_transaction_id,
                "provider_terminal_serial": self.provider_terminal_serial,
                "state": self.state,
                "base_amount": self.base_amount,
                "commission_percent": self.commission_percent,
                "commission_amount": self.commission_amount,
                "commission_rule_id": self.commission_rule_id.id,
                "amount": self.amount,
                "currency": self.currency,
                "qr_currency": self.qr_currency,
                "qr_converted_amount": self.qr_converted_amount,
                "qr_exchange_rate": self.qr_exchange_rate,
                "qr_from": self.qr_from,
                "client_id": self.client_id.id,
                "partner_id": (self.partner_id or self.client_id.partner_id).id,
                "sales_channel": self.source_channel or "external",
                "provider_raw": self.provider_response_payload,
                "concept": self.concept,
                "seller_user_id": seller_user.id,
            }
        )
        self.transaction_id = tx.id
        return tx

    def sync_transaction(self):
        for rec in self:
            tx = rec.ensure_transaction()
            seller_user = rec.client_id.resolve_seller_user(partner=rec.partner_id, fallback_to_admin=True)
            incoming_provider_raw = dict(rec.provider_response_payload or {})
            existing_provider_raw = dict(tx.provider_raw or {})
            if isinstance(existing_provider_raw.get("extra_data"), dict):
                # Preserve extra metadata attached by /api/v1/payments/extra-data.
                incoming_provider_raw["extra_data"] = existing_provider_raw.get("extra_data")
            vals = {
                "external_order_id": rec.external_order_id,
                "provider": rec.provider,
                "provider_config_id": rec.provider_config_id.id,
                "provider_payment_id": rec.provider_payment_id,
                "provider_client_transaction_id": rec.provider_client_transaction_id,
                "provider_terminal_serial": rec.provider_terminal_serial,
                "state": rec.state,
                "base_amount": rec.base_amount,
                "commission_percent": rec.commission_percent,
                "commission_amount": rec.commission_amount,
                "commission_rule_id": rec.commission_rule_id.id,
                "amount": rec.amount,
                "currency": rec.currency,
                "qr_currency": rec.qr_currency,
                "qr_converted_amount": rec.qr_converted_amount,
                "qr_exchange_rate": rec.qr_exchange_rate,
                "qr_from": rec.qr_from,
                "client_id": rec.client_id.id,
                "partner_id": (rec.partner_id or rec.client_id.partner_id).id,
                "sales_channel": rec.source_channel or "external",
                "provider_raw": incoming_provider_raw,
                "seller_user_id": seller_user.id,
            }
            if rec.concept:
                vals["concept"] = rec.concept
            tx.write(vals)

    def is_final_state(self):
        self.ensure_one()
        return self.state in self.FINAL_STATES

    def refresh_provider_status(self):
        """Consulta el estado en el proveedor (si lo soporta) y lo aplica al intent y su transacción."""
        for rec in self:
            if rec.is_final_state() or not rec.provider_payment_id:
                continue
            service = self.env["surpay.provider.service"].for_provider(rec.provider)
            if service is None or not service.supports_remote_status(provider_config=rec.provider_config_id):
                continue

            provider_status = service.get_payment_status(
                rec.provider_payment_id,
                provider_config=rec.provider_config_id,
            )
            mapped_state = service.map_status(
                service.extract_status(provider_status),
                service.extract_status_message(provider_status),
            )
            existing_payload = dict(rec.provider_response_payload or {})
            merged_payload = dict(existing_payload)
            merged_payload.update(provider_status or {})
            existing_qr = existing_payload.get("qr_data") or existing_payload.get("qr_code")
            if existing_qr and not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
                merged_payload["qr_data"] = existing_qr
            rec.write(
                {
                    "state": mapped_state,
                    "provider_response_payload": merged_payload,
                    **service.extract_payment_quote(
                        merged_payload,
                        fallback_currency=rec.currency,
                        fallback_amount=rec.amount,
                    ),
                }
            )
            rec.sync_transaction()

    def notify_status_changed(self, provider_status="", provider_message="", provider_raw=None):
        """Emite el webhook saliente payment.status.changed al comercio con el estado actual."""
        self.ensure_one()
        transaction = self.ensure_transaction()
        emitted_at = fields.Datetime.now()
        self.env["surpay.payment.event"].sudo().create_outbound_webhook_event(
            transaction,
            {
                "contract_version": "v1",
                "event_type": "payment.status.changed",
                "emitted_at": emitted_at.replace(tzinfo=timezone.utc).isoformat(),
                "transaction": {
                    "order_id": self.order_id,
                    "external_order_id": self.external_order_id,
                    "provider": self.provider,
                    "provider_order_id": self.provider_payment_id,
                    "status": self.state,
                    "status_code": self.STATE_CODES.get(self.state, 1900),
                },
                "provider": {
                    "name": self.provider,
                    "status": provider_status,
                    "message": provider_message,
                    "raw": provider_raw or {},
                },
            },
            event_type="payment.status.changed",
            contract_version="v1",
        )

    def enforce_pending_timeout(self):
        """Cierra los pagos sin resultado del proveedor pasado su timeout o su expiración.

        Surpay es la fuente de verdad del estado: la app y los comercios dejan de esperar
        cuando la transacción sale de pendiente, aunque el proveedor nunca responda.
        El timeout del proveedor (p. ej. terminal sin respuesta) deja el pago fallido;
        si no aplica, al pasar expires_at (p. ej. QR sin pagar) queda expirado.
        """
        now = fields.Datetime.now()
        service_model = self.env["surpay.provider.service"]
        for rec in self:
            if rec.is_final_state():
                continue
            service = service_model.for_provider(rec.provider)
            timeout = service.pending_timeout_seconds(provider_config=rec.provider_config_id) if service is not None else 0
            started_at = rec.provider_dispatched_at or rec.create_date
            if timeout and started_at and (now - started_at).total_seconds() >= timeout:
                try:
                    service.on_pending_timeout(rec)
                except Exception as exc:
                    _logger.warning("on_pending_timeout falló para %s: %s", rec.order_id, exc)
                rec._close_unresolved(
                    state="failed",
                    reason=self.FAILURE_REASON_TIMEOUT,
                    message=f"Sin resultado del proveedor tras {timeout} segundos.",
                    event_type="payment.timeout",
                    event_payload={"timeout_seconds": timeout, "started_at": str(started_at)},
                    provider_status="SURPAY_TIMEOUT",
                )
            elif rec.expires_at and now >= rec.expires_at:
                rec._close_unresolved(
                    state="expired",
                    reason=self.FAILURE_REASON_EXPIRED,
                    message="El pago no se completó antes de su expiración.",
                    event_type="payment.expired",
                    event_payload={"expires_at": str(rec.expires_at)},
                    provider_status="SURPAY_EXPIRED",
                )

    def _close_unresolved(self, state, reason, message, event_type, event_payload, provider_status):
        """Cierra un pago pendiente por decisión de Surpay y avisa al comercio."""
        self.ensure_one()
        payload = dict(self.provider_response_payload or {})
        payload.update({"failure_reason": reason, "failure_message": message})
        self.write({"state": state, "provider_response_payload": payload})
        self.sync_transaction()
        self.env["surpay.payment.event"].sudo().create(
            {
                "transaction_id": self.transaction_id.id,
                "source": "internal",
                "event_type": event_type,
                "payload": event_payload,
                "processing_status": "ok",
                "message": message,
            }
        )
        _logger.info("Intent %s cerrado como %s (%s)", self.order_id, state, reason)
        self.notify_status_changed(provider_status=provider_status, provider_message=message)

    @api.model
    def _cron_enforce_pending_timeout(self):
        stale = self.search(
            [
                ("state", "in", ("created", "pending")),
                ("create_date", ">=", fields.Datetime.now() - timedelta(days=2)),
            ],
            order="id asc",
            limit=500,
        )
        for intent in stale:
            intent.enforce_pending_timeout()
            # Commit por intent: el abort al terminal es externo y no debe repetirse si otro falla.
            self.env.cr.commit()

    @api.constrains("provider", "provider_config_id")
    def _check_provider_config_consistency(self):
        for rec in self:
            if rec.provider and rec.provider_config_id and rec.provider != rec.provider_config_id.provider:
                raise ValidationError(
                    _("El proveedor del intento debe coincidir con el proveedor de la configuración asignada.")
                )
