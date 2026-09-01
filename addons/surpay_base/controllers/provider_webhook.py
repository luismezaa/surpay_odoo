import json
import logging
from datetime import timezone

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayProviderWebhookController(http.Controller):
    PROVIDER_ALIASES = {
        "surpay_fronterizo": "depay",
    }

    @staticmethod
    def _state_code(state):
        mapping = {
            "created": 1000,
            "pending": 1100,
            "paid": 2000,
            "failed": 5000,
            "expired": 5100,
            "cancelled": 5200,
        }
        return mapping.get(state, 1900)

    def _supported_providers(self):
        return {item[0] for item in request.env["surpay.provider.config"].PROVIDERS}

    @classmethod
    def _normalize_provider(cls, provider):
        provider = (provider or "").strip().lower()
        if not provider:
            return ""
        return cls.PROVIDER_ALIASES.get(provider, provider)

    @staticmethod
    def _provider_service_name(provider):
        mapping = {
            "depay": "surpay.depay.api",
            "kushki": "surpay.kushki.api",
        }
        return mapping.get(provider)

    def _resolve_provider_service(self, provider):
        service_name = self._provider_service_name(provider)
        if not service_name:
            return None
        if service_name not in request.env:
            return None
        return request.env[service_name].sudo()

    @staticmethod
    def _dispatch_outbound_webhook(transaction, event_payload):
        request.env["surpay.payment.event"].sudo().create_outbound_webhook_event(
            transaction,
            event_payload,
            event_type="payment.status.changed",
            contract_version="v1",
        )

    @staticmethod
    def _error(status_code, code, message):
        return request.make_json_response(
            {
                "error": {
                    "code": code,
                    "message": message,
                }
            },
            status=status_code,
        )

    @staticmethod
    def _raw_body():
        return request.httprequest.get_data(cache=False, as_text=False)

    @http.route(
        "/api/v1/webhooks/providers/<string:provider>",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def provider_webhook(self, provider):
        route_provider = (provider or "").strip().lower()
        provider = self._normalize_provider(route_provider)
        if not provider:
            return self._error(400, "missing_provider", "Provider route parameter is required.")
        if provider not in self._supported_providers():
            return self._error(400, "unsupported_provider", "Provider is not supported.")

        provider_service = self._resolve_provider_service(provider)
        if provider_service is None:
            return self._error(501, "provider_service_not_available", "Provider service is not available.")

        raw_body = self._raw_body()
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._error(400, "invalid_payload", "Invalid JSON callback payload.")

        provider_order_id = payload.get("order_id")
        provider_client_transaction_id = payload.get("client_transaction_id")
        if hasattr(provider_service, "extract_event_reference"):
            refs = provider_service.extract_event_reference(payload)
            provider_order_id = refs.get("provider_order_id") or provider_order_id
            provider_client_transaction_id = refs.get("client_transaction_id") or provider_client_transaction_id

        if not provider_order_id and not provider_client_transaction_id:
            return self._error(400, "missing_reference", "Callback payload missing provider transaction reference.")

        intent = request.env["surpay.payment.intent"].sudo().browse()
        if provider_client_transaction_id:
            intent = (
                request.env["surpay.payment.intent"]
                .sudo()
                .search([("provider_client_transaction_id", "=", provider_client_transaction_id)], limit=1)
            )
        if not intent and provider_order_id:
            intent = (
                request.env["surpay.payment.intent"]
                .sudo()
                .search([("provider_payment_id", "=", provider_order_id)], limit=1)
            )
        if not intent and provider_order_id:
            intent = (
                request.env["surpay.payment.intent"]
                .sudo()
                .search([("order_id", "=", provider_order_id)], limit=1)
            )
        if not intent and provider_client_transaction_id:
            intent = (
                request.env["surpay.payment.intent"]
                .sudo()
                .search([("provider_client_transaction_id", "=", provider_client_transaction_id)], limit=1)
            )

        if not intent:
            return self._error(404, "not_found", "Payment intent not found for provider order_id.")

        if (intent.provider or "").strip().lower() != provider:
            return self._error(
                409,
                "provider_mismatch_webhook",
                "Webhook provider does not match payment intent provider.",
            )

        validate_signature = True
        if hasattr(provider_service, "should_validate_callback_signature"):
            validate_signature = bool(provider_service.should_validate_callback_signature())

        if validate_signature:
            signature_header = request.httprequest.headers.get("signature")
            signature_valid = provider_service.validate_callback_signature(
                raw_body,
                signature_header,
                provider_config=intent.provider_config_id,
            )
            if not signature_valid:
                return self._error(401, "invalid_provider_signature", "Invalid provider callback signature.")

        provider_status = provider_service.extract_status(payload)
        provider_message = (
            provider_service.extract_status_message(payload)
            if hasattr(provider_service, "extract_status_message")
            else payload.get("message") or payload.get("detail")
        )
        mapped_state = provider_service.map_depay_status(
            provider_status,
            provider_message,
        )

        existing_payload = dict(intent.provider_response_payload or {})
        merged_payload = dict(existing_payload)
        merged_payload.update(payload or {})
        if not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
            merged_payload["qr_data"] = existing_payload.get("qr_data") or existing_payload.get("qr_code")
        intent.write(
            {
                "provider_payment_id": intent.provider_payment_id or provider_order_id,
                "provider_client_transaction_id": (
                    intent.provider_client_transaction_id
                    or provider_client_transaction_id
                    or ""
                ),
                "state": mapped_state,
                "provider_response_payload": merged_payload,
                **provider_service.extract_qr_quote(
                    merged_payload,
                    fallback_currency=intent.currency,
                    fallback_amount=intent.amount,
                ),
            }
        )
        transaction = intent.ensure_transaction()
        intent.sync_transaction()

        request.env["surpay.payment.event"].sudo().create(
            {
                "transaction_id": transaction.id,
                "source": "provider",
                "event_type": payload.get("type") or provider_service.extract_status(payload) or "PAYMENT",
                "payload": payload,
                "signature_valid": True,
                "processing_status": "ok",
            }
        )

        emitted_at = fields.Datetime.now()
        outbound_payload = {
            "contract_version": "v1",
            "event_type": "payment.status.changed",
            "emitted_at": emitted_at.replace(tzinfo=timezone.utc).isoformat(),
            "transaction": {
                "order_id": intent.order_id,
                "external_order_id": intent.external_order_id,
                "provider": provider,
                "provider_order_id": intent.provider_payment_id,
                "status": intent.state,
                "status_code": self._state_code(intent.state),
            },
            "provider": {
                "name": provider,
                "status": provider_service.extract_status(payload),
                "message": payload.get("message"),
                "raw": payload,
            },
        }
        self._dispatch_outbound_webhook(transaction, outbound_payload)

        return request.make_json_response({"status": "ok"}, status=200)