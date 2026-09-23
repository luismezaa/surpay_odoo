import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayProviderWebhookController(http.Controller):
    def _supported_providers(self):
        return {item[0] for item in request.env["surpay.provider.config"].PROVIDERS}

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
        service_model = request.env["surpay.provider.service"].sudo()
        provider = service_model.normalize_provider(provider)
        if not provider:
            return self._error(400, "missing_provider", "Provider route parameter is required.")
        if provider not in self._supported_providers():
            return self._error(400, "unsupported_provider", "Provider is not supported.")

        provider_service = service_model.for_provider(provider)
        if provider_service is None:
            return self._error(501, "provider_service_not_available", "Provider service is not available.")

        raw_body = self._raw_body()
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._error(400, "invalid_payload", "Invalid JSON callback payload.")

        refs = provider_service.extract_event_reference(payload)
        provider_order_id = refs.get("provider_order_id") or payload.get("order_id")
        provider_client_transaction_id = refs.get("client_transaction_id") or payload.get("client_transaction_id")

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

        if provider_service.should_validate_callback_signature():
            signature_header = request.httprequest.headers.get("signature")
            signature_valid = provider_service.validate_callback_signature(
                raw_body,
                signature_header,
                provider_config=intent.provider_config_id,
            )
            if not signature_valid:
                return self._error(401, "invalid_provider_signature", "Invalid provider callback signature.")

        provider_status = provider_service.extract_status(payload)
        provider_message = provider_service.extract_status_message(payload)
        mapped_state = provider_service.map_status(
            provider_status,
            provider_message,
        )

        if intent.is_final_state():
            # Un estado final no se revierte: eventos tardíos o duplicados solo quedan registrados.
            # Una aprobación sobre una venta ya cerrada sin pago (p. ej. por timeout) significa que se
            # cobró al cliente sin venta: se registra como error para reversar o revisar.
            charged_without_sale = mapped_state == "paid" and intent.state != "paid"
            log = _logger.error if charged_without_sale else _logger.info
            log(
                "Webhook %s ignorado para %s: intent en estado final %s (evento mapeado a %s)",
                provider,
                intent.order_id,
                intent.state,
                mapped_state,
            )
            message = (
                f"APROBADO por el proveedor con la venta ya en estado {intent.state}: requiere reverso o revisión."
                if charged_without_sale
                else f"Ignorado: intent ya en estado final {intent.state} (evento mapeado a {mapped_state})."
            )
            request.env["surpay.payment.event"].sudo().create(
                {
                    "transaction_id": intent.ensure_transaction().id,
                    "source": "provider",
                    "event_type": payload.get("type") or provider_status or "PAYMENT",
                    "payload": payload,
                    "signature_valid": True,
                    "processing_status": "error" if charged_without_sale else "ok",
                    "message": message,
                }
            )
            return request.make_json_response({"status": "ignored"}, status=200)

        existing_payload = dict(intent.provider_response_payload or {})
        merged_payload = dict(existing_payload)
        merged_payload.update(payload or {})
        existing_qr = existing_payload.get("qr_data") or existing_payload.get("qr_code")
        if existing_qr and not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
            merged_payload["qr_data"] = existing_qr
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
                **provider_service.extract_payment_quote(
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

        intent.notify_status_changed(
            provider_status=provider_status,
            provider_message=payload.get("message"),
            provider_raw=payload,
        )

        return request.make_json_response({"status": "ok"}, status=200)