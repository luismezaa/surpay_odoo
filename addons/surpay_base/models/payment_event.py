import base64
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class SurpayPaymentEvent(models.Model):
    _name = "surpay.payment.event"
    _description = "Surpay Payment Event"
    _order = "id desc"

    transaction_id = fields.Many2one("surpay.payment.transaction", ondelete="cascade", index=True)
    source = fields.Selection(
        selection=[
            ("provider", "Provider"),
            ("internal", "Internal"),
            ("outbound_webhook", "Outbound Webhook"),
        ],
        required=True,
    )
    event_type = fields.Char(required=True)
    payload = fields.Json()
    signature_valid = fields.Boolean(default=False)
    processing_status = fields.Selection(
        selection=[("ok", "OK"), ("error", "Error")],
        default="ok",
        required=True,
    )
    message = fields.Char()

    retry_count = fields.Integer(default=0)
    last_attempt_at = fields.Datetime()
    next_retry_at = fields.Datetime(index=True)
    retry_deadline_at = fields.Datetime(index=True)
    last_response_code = fields.Integer()
    is_final = fields.Boolean(default=False)
    webhook_url = fields.Char()
    contract_version = fields.Char(default="v1")

    @api.model
    def _retry_plan_seconds(self):
        # Progressive backoff schedule, with final slot reused until the 24h deadline.
        return [30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400]

    @api.model
    def _next_retry_delay(self, retry_count):
        plan = self._retry_plan_seconds()
        idx = max(0, min(retry_count - 1, len(plan) - 1))
        return plan[idx]

    def _webhook_headers(self, body, secret):
        timestamp = str(int(fields.Datetime.now().timestamp()))
        signature_raw = hmac.new(
            (secret or "").encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(signature_raw).decode("utf-8")
        return {
            "Content-Type": "application/json",
            "X-Surpay-Timestamp": timestamp,
            "X-Surpay-Signature": signature,
        }

    def _schedule_or_finalize(self, error_message):
        self.ensure_one()
        now = fields.Datetime.now()
        next_delay = self._next_retry_delay(self.retry_count)
        next_retry = now + timedelta(seconds=next_delay)

        if self.retry_deadline_at and next_retry > self.retry_deadline_at:
            self.write(
                {
                    "is_final": True,
                    "processing_status": "error",
                    "message": f"Final delivery failure after retries: {error_message}",
                    "next_retry_at": False,
                }
            )
            _logger.error(
                "Outbound webhook permanently failed transaction=%s event=%s message=%s",
                self.transaction_id.order_id,
                self.id,
                error_message,
            )
            return

        self.write(
            {
                "processing_status": "error",
                "message": error_message,
                "next_retry_at": next_retry,
            }
        )

    def attempt_outbound_delivery(self):
        for event in self:
            if event.source != "outbound_webhook" or event.is_final:
                continue

            event.last_attempt_at = fields.Datetime.now()
            event.retry_count += 1

            transaction = event.transaction_id
            if not transaction:
                event.write(
                    {
                        "is_final": True,
                        "processing_status": "error",
                        "message": "Missing transaction reference for outbound webhook delivery.",
                        "next_retry_at": False,
                    }
                )
                _logger.error("Outbound webhook missing transaction reference event=%s", event.id)
                continue
            client = transaction.client_id
            webhook_url = event.webhook_url or client.webhook_url
            webhook_secret = client.webhook_secret

            if not webhook_url or not webhook_secret:
                event.write(
                    {
                        "is_final": True,
                        "processing_status": "error",
                        "message": "Missing webhook_url or webhook_secret for outbound webhook delivery.",
                        "next_retry_at": False,
                    }
                )
                _logger.error(
                    "Outbound webhook cannot be delivered due to missing config transaction=%s event=%s",
                    transaction.order_id,
                    event.id,
                )
                continue

            body = json.dumps(event.payload or {}, separators=(",", ":"), ensure_ascii=False)
            headers = event._webhook_headers(body, webhook_secret)

            try:
                response = requests.post(webhook_url, data=body.encode("utf-8"), headers=headers, timeout=10)
                event.last_response_code = response.status_code
                if response.status_code < 300:
                    event.write(
                        {
                            "processing_status": "ok",
                            "is_final": True,
                            "message": f"Delivered successfully status={response.status_code}",
                            "next_retry_at": False,
                        }
                    )
                    continue

                event._schedule_or_finalize(f"Webhook response status={response.status_code}")
            except Exception as exc:
                event._schedule_or_finalize(str(exc))

    @api.model
    def create_outbound_webhook_event(self, transaction, payload, event_type="state_change", contract_version="v1"):
        event = self.sudo().create(
            {
                "transaction_id": transaction.id,
                "source": "outbound_webhook",
                "event_type": event_type,
                "payload": payload,
                "signature_valid": True,
                "processing_status": "error",
                "message": "Delivery scheduled.",
                "retry_count": 0,
                "retry_deadline_at": fields.Datetime.now() + timedelta(days=1),
                "next_retry_at": fields.Datetime.now(),
                "webhook_url": transaction.client_id.webhook_url,
                "contract_version": contract_version,
            }
        )
        event.attempt_outbound_delivery()
        return event

    @api.model
    def run_outbound_webhook_retries(self):
        # One-time safeguard: disable legacy provider-level cron after migration to base.
        legacy_xmlid = self.env["ir.model.data"].sudo().search(
            [
                ("module", "=", "l10n_cl_surpay_depay"),
                ("name", "=", "ir_cron_outbound_webhook_retry"),
                ("model", "=", "ir.cron"),
            ],
            limit=1,
        )
        if legacy_xmlid:
            legacy_cron = self.env["ir.cron"].sudo().browse(legacy_xmlid.res_id)
            if legacy_cron.exists() and legacy_cron.active:
                legacy_cron.active = False

        now = fields.Datetime.now()
        pending = self.sudo().search(
            [
                ("source", "=", "outbound_webhook"),
                ("processing_status", "=", "error"),
                ("is_final", "=", False),
                ("next_retry_at", "!=", False),
                ("next_retry_at", "<=", now),
            ],
            order="next_retry_at asc",
            limit=200,
        )
        if pending:
            pending.attempt_outbound_delivery()
