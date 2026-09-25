import base64
from datetime import timezone
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from odoo import fields, http
from odoo.http import request

from odoo.addons.surpay_base.models.provider_service import SurpayApiRequestError

_logger = logging.getLogger(__name__)


class SurpayExternalApiController(http.Controller):
    EXTRA_FIELDS_MAX_ITEMS = 6
    EXTRA_FIELD_TITLE_MAX_LEN = 24
    EXTRA_FIELD_VALUE_MAX_LEN = 60
    EXTRA_FIELD_KEY_MAX_LEN = 64

    @staticmethod
    def _client_ip():
        xff = request.httprequest.headers.get("X-Forwarded-For")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
        return request.httprequest.remote_addr

    @staticmethod
    def _service_model():
        return request.env["surpay.provider.service"].sudo()

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
        return request.httprequest.get_data(as_text=False)

    @staticmethod
    def _body_sha256(raw_body):
        return hashlib.sha256(raw_body or b"").hexdigest()

    @staticmethod
    def _normalized_query():
        query = request.httprequest.query_string.decode("utf-8") if request.httprequest.query_string else ""
        if not query:
            return ""
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs.sort(key=lambda item: (item[0], item[1]))
        return "&".join([f"{k}={v}" for k, v in pairs])

    def _resolve_client(self):
        client_id = request.httprequest.headers.get("X-Client-Id")
        if not client_id:
            return None

        return (
            request.env["surpay.api.client"]
            .sudo()
            .search([("client_id", "=", client_id), ("active", "=", True)], limit=1)
        )

    def _verify_hmac(self):
        client = self._resolve_client()
        if not client:
            return None, self._error(401, "invalid_client", "Invalid client credentials.")

        source_ip = self._client_ip()
        if not client.is_ip_allowed(source_ip):
            return None, self._error(403, "forbidden_ip", "Source IP is not allowed for this client.")

        timestamp = request.httprequest.headers.get("X-Timestamp")
        nonce = request.httprequest.headers.get("X-Nonce")
        signature = request.httprequest.headers.get("X-Signature")
        idempotency_key = request.httprequest.headers.get("Idempotency-Key")

        if not timestamp or not nonce or not signature:
            return None, self._error(401, "missing_auth_headers", "Missing HMAC headers.")

        try:
            timestamp_int = int(timestamp)
        except ValueError:
            return None, self._error(401, "invalid_timestamp", "Invalid timestamp format.")

        if abs(int(time.time()) - timestamp_int) > 300:
            return None, self._error(401, "expired_timestamp", "Timestamp outside allowed window.")

        try:
            request.env["surpay.api.nonce"].sudo().register_nonce(client, nonce, 300)
        except Exception:
            return None, self._error(401, "replayed_nonce", "Nonce was already used.")

        raw_body = self._raw_body()
        canonical = "\n".join(
            [
                request.httprequest.method.upper(),
                request.httprequest.path,
                self._normalized_query(),
                self._body_sha256(raw_body),
                timestamp,
                nonce,
            ]
        )

        digest = hmac.new(
            client.client_secret.encode("utf-8"),
            canonical.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected_signature = base64.b64encode(digest).decode("utf-8")

        if not hmac.compare_digest(expected_signature, signature):
            return None, self._error(401, "invalid_signature", "Invalid HMAC signature.")

        return {
            "client": client,
            "idempotency_key": idempotency_key,
            "raw_body": raw_body,
            "source_ip": source_ip,
        }, None

    def _normalize_extra_data_fields(self, extra_data_fields):
        if not isinstance(extra_data_fields, list):
            raise ValueError("extra_data_fields must be an array.")
        if not extra_data_fields:
            raise ValueError("extra_data_fields must contain at least 1 item.")
        if len(extra_data_fields) > self.EXTRA_FIELDS_MAX_ITEMS:
            raise ValueError(f"extra_data_fields supports up to {self.EXTRA_FIELDS_MAX_ITEMS} items.")

        normalized = []
        seen_keys = set()
        for idx, item in enumerate(extra_data_fields, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"extra_data_fields[{idx}] must be an object.")

            key = str(item.get("key") or "").strip()
            title = str(item.get("title") or "").strip()
            value = str(item.get("value") or "").strip()

            if not key or not title or not value:
                raise ValueError(f"extra_data_fields[{idx}] requires non-empty key, title and value.")
            if len(key) > self.EXTRA_FIELD_KEY_MAX_LEN:
                raise ValueError(f"extra_data_fields[{idx}].key max length is {self.EXTRA_FIELD_KEY_MAX_LEN}.")
            if len(title) > self.EXTRA_FIELD_TITLE_MAX_LEN:
                raise ValueError(f"extra_data_fields[{idx}].title max length is {self.EXTRA_FIELD_TITLE_MAX_LEN}.")
            if len(value) > self.EXTRA_FIELD_VALUE_MAX_LEN:
                raise ValueError(f"extra_data_fields[{idx}].value max length is {self.EXTRA_FIELD_VALUE_MAX_LEN}.")
            if key in seen_keys:
                raise ValueError(f"extra_data_fields has duplicate key '{key}'.")

            seen_keys.add(key)
            normalized.append({"key": key, "title": title, "value": value})

        return normalized

    @http.route("/api/v1/payments/intents", type="http", auth="public", methods=["POST"], csrf=False)
    def create_payment_intent(self):
        auth_data, auth_error = self._verify_hmac()
        if auth_error:
            _logger.warning("[INTENT] Error de autenticación HMAC")
            return auth_error

        client = auth_data["client"]
        idempotency_key = auth_data["idempotency_key"]
        if not idempotency_key:
            return self._error(400, "missing_idempotency_key", "Idempotency-Key header is required.")

        raw_body = auth_data.get("raw_body") or b"{}"
        _logger.info("[INTENT] create_payment_intent client=%s payload=%s", client.client_id, raw_body)
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return self._error(400, "invalid_payload", "Request body must be valid JSON.")
        if not isinstance(payload, dict):
            return self._error(400, "invalid_payload", "Request body must be a JSON object.")

        service_model = self._service_model()
        requested_provider = str(payload.get("provider") or "").strip().lower()
        provider = service_model.normalize_provider(requested_provider)
        if not provider:
            return self._error(400, "missing_provider", "provider is required.")
        if provider not in self._supported_providers():
            return self._error(400, "unsupported_provider", "Provider is not supported.")

        provider_service = service_model.for_provider(provider)
        if provider_service is None:
            _logger.error("[INTENT] Provider service no disponible para: %s", provider)
            return self._error(501, "provider_service_not_available", "Provider service is not available.")

        amount = payload.get("amount")
        currency = str(payload.get("currency") or provider_service.api_default_currency(client) or "").strip().upper()
        external_order_id = payload.get("external_order_id")
        concept = payload.get("concept")
        expires_in = payload.get("expires_in")

        if amount is None or not currency:
            return self._error(400, "invalid_payload", "amount and currency are required.")

        try:
            amount = float(amount)
        except (ValueError, TypeError):
            return self._error(400, "invalid_amount", "amount must be a valid number.")
        if amount <= 0:
            return self._error(400, "invalid_amount", "amount must be greater than 0.")

        intent_model = request.env["surpay.payment.intent"].sudo()
        existing_idempotent = intent_model.search(
            [
                ("client_id", "=", client.id),
                ("idempotency_key", "=", idempotency_key),
            ],
            limit=1,
        )
        if existing_idempotent:
            _logger.info("[INTENT] Request idempotente, intent existente: %s", existing_idempotent.order_id)
            return request.make_json_response(existing_idempotent.normalized_payload(), status=200)

        provider_config = client.resolve_provider_config_for_provider(provider)
        if not provider_config:
            return self._error(400, "provider_not_configured", "No provider configuration found for the selected provider.")

        try:
            provider_service.api_validate_intent_request(payload, client, provider_config, currency)
        except SurpayApiRequestError as exc:
            _logger.warning("[INTENT] Request inválido para %s: %s", provider, exc.message)
            return self._error(exc.status, exc.code, exc.message)

        if external_order_id:
            existing_external = intent_model.search(
                [
                    ("client_id", "=", client.id),
                    ("external_order_id", "=", external_order_id),
                    ("state", "not in", ["failed", "expired"]),
                ],
                limit=1,
            )
            if existing_external:
                return self._error(
                    409,
                    "external_order_conflict",
                    "external_order_id already exists with a non-final recoverable state.",
                )

        try:
            expires_at = intent_model.build_expiration(expires_in)
        except Exception:
            return self._error(400, "invalid_expiration", "expires_in must be numeric within allowed range.")

        commission_data = request.env["surpay.commission.rule"].sudo().compute_amounts(
            provider=provider,
            base_amount=amount,
            currency=currency,
            client_id=client.id,
            sales_channel="external",
        )
        amount_to_provider = commission_data["total_amount"]

        callback_url = self._build_provider_callback_url(provider)
        intent_vals = {
            "order_id": intent_model.generate_order_id(),
            "external_order_id": external_order_id,
            "provider": provider,
            "requested_provider": requested_provider or provider,
            "source_channel": "external",
            "base_amount": amount,
            "commission_percent": commission_data["commission_percent"],
            "commission_amount": commission_data["commission_amount"],
            "commission_rule_id": commission_data["rule"].id,
            "amount": amount_to_provider,
            "currency": currency,
            "state": "created",
            "idempotency_key": idempotency_key,
            "client_id": client.id,
            "provider_config_id": provider_config.id,
            "notification_url": callback_url,
            "expires_at": expires_at,
            "concept": concept,
            "return_url": client.return_url or "",
            "return_url_behavior": client.return_url_behavior or "webhook_only",
        }
        intent_vals.update(provider_service.api_prepare_intent_vals(payload, client, provider_config))
        intent = intent_model.create(intent_vals)
        transaction = intent.ensure_transaction()

        intent.provider_dispatched_at = fields.Datetime.now()
        if provider_service.api_commit_before_provider_call():
            request.env.cr.commit()

        provider_payload = {
            "amount": amount_to_provider,
            "local_currency": currency,
            "external_reference": external_order_id or intent.order_id,
            "notification_url": callback_url,
        }
        provider_payload = provider_service.api_build_provider_payload(
            intent, payload, client, provider_config, provider_payload
        )
        provider_request_payload = dict(provider_payload)
        provider_request_payload["display_concept"] = concept or f"Compra {int(amount_to_provider)} {currency}"

        try:
            provider_response = provider_service.create_payment(provider_payload, provider_config=provider_config)
            _logger.info("[INTENT] Respuesta de %s.create_payment: %s", provider, provider_response)
        except Exception as exc:
            _logger.error("[INTENT] Falló create_payment en %s: %s", provider, exc)
            intent.write(
                {
                    "state": "failed",
                    "provider_request_payload": provider_request_payload,
                    "provider_response_payload": {"error": str(exc)},
                }
            )
            intent.sync_transaction()
            request.env["surpay.payment.event"].sudo().create(
                {
                    "transaction_id": transaction.id,
                    "source": "internal",
                    "event_type": "provider_create_failed",
                    "payload": {"error": str(exc)},
                    "processing_status": "error",
                    "message": str(exc),
                }
            )
            return self._error(502, "provider_error", "Provider payment creation failed.")

        provider_client_transaction_id = (
            provider_response.get("client_transaction_id") or intent.provider_client_transaction_id
        )
        provider_order_id = provider_response.get("order_id") or provider_client_transaction_id
        raw_status = provider_service.extract_status(provider_response) or "PENDING"
        mapped_state = provider_service.map_status(
            raw_status,
            provider_service.extract_status_message(provider_response),
        )

        vals = {
            "provider_payment_id": provider_order_id,
            "provider_client_transaction_id": provider_client_transaction_id,
            "provider_terminal_serial": provider_response.get("terminal_serial") or intent.provider_terminal_serial,
            "provider_request_payload": provider_request_payload,
            "provider_response_payload": provider_response,
            **provider_service.extract_payment_quote(
                provider_response,
                fallback_currency=currency,
                fallback_amount=amount_to_provider,
            ),
        }
        # Un webhook pudo cerrar el pago mientras esperábamos la respuesta del proveedor.
        if not intent.is_final_state():
            vals["state"] = mapped_state
        intent.write(vals)
        intent.sync_transaction()

        response_payload = intent.normalized_payload()
        response_payload.update(
            {
                "provider_order_id": provider_order_id,
                "provider_status": raw_status,
                **provider_service.api_response_extras(intent, provider_response),
            }
        )
        return request.make_json_response(response_payload, status=201)

    @staticmethod
    def _build_provider_callback_url(provider):
        base_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return f"{base_url.rstrip('/')}/api/v1/webhooks/providers/{provider}"

    @http.route(
        "/api/v1/payments/intents/<string:order_id>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def get_payment_intent(self, order_id):
        auth_data, auth_error = self._verify_hmac()
        if auth_error:
            return auth_error

        client = auth_data["client"]

        intent = (
            request.env["surpay.payment.intent"]
            .sudo()
            .search([("order_id", "=", order_id), ("client_id", "=", client.id)], limit=1)
        )
        if not intent:
            return self._error(404, "not_found", "Payment intent not found.")

        try:
            intent.refresh_provider_status()
        except Exception as exc:
            _logger.info("Provider status refresh failed for %s: %s", intent.order_id, exc)
        intent.enforce_pending_timeout()

        return request.make_json_response(intent.normalized_payload(), status=200)

    @http.route(
        "/api/v1/payments/transactions/<string:order_id>/state",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def get_payment_transaction_state(self, order_id):
        auth_data, auth_error = self._verify_hmac()
        if auth_error:
            return auth_error

        client = auth_data["client"]

        tx = (
            request.env["surpay.payment.transaction"]
            .sudo()
            .search([("order_id", "=", order_id), ("client_id", "=", client.id)], limit=1)
        )
        if not tx:
            return self._error(404, "not_found", "Payment transaction not found.")

        # No hay consulta de estado en el proveedor: Surpay decide, cerrando por timeout lo que no tuvo respuesta.
        request.env["surpay.payment.intent"].sudo().search([("transaction_id", "=", tx.id)], limit=1).enforce_pending_timeout()

        return request.make_json_response(tx.normalized_status_payload(), status=200)

    @http.route("/api/v1/payments/extra-data", type="http", auth="public", methods=["POST"], csrf=False)
    def update_payment_extra_data(self):
        auth_data, auth_error = self._verify_hmac()
        if auth_error:
            return auth_error

        client = auth_data["client"]
        idempotency_key = auth_data["idempotency_key"]
        if not idempotency_key:
            return self._error(400, "missing_idempotency_key", "Idempotency-Key header is required.")

        raw_body = auth_data.get("raw_body") or b"{}"
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return self._error(400, "invalid_payload", "Request body must be valid JSON.")

        order_id = str(payload.get("order_id") or "").strip()
        if not order_id:
            return self._error(400, "missing_order_id", "order_id is required.")

        try:
            normalized_fields = self._normalize_extra_data_fields(payload.get("extra_data_fields"))
        except ValueError as exc:
            return self._error(400, "invalid_extra_data_fields", str(exc))

        tx = (
            request.env["surpay.payment.transaction"]
            .sudo()
            .search([("order_id", "=", order_id), ("client_id", "=", client.id)], limit=1)
        )
        if not tx:
            return self._error(404, "not_found", "Payment transaction not found.")

        provider_raw = dict(tx.provider_raw or {})
        extra_data = provider_raw.get("extra_data") if isinstance(provider_raw.get("extra_data"), dict) else {}
        if extra_data.get("last_idempotency_key") == idempotency_key:
            return request.make_json_response(
                {
                    "status": "ok",
                    "idempotent": True,
                    "updated": False,
                    "order_id": tx.order_id,
                },
                status=200,
            )

        extra_data = {
            "client_code": str(payload.get("client_code") or client.client_id or "").strip(),
            "provider": str(payload.get("provider") or tx.provider or "").strip(),
            "source_process": str(payload.get("source_process") or "").strip(),
            "transaction_id": str(payload.get("transaction_id") or tx.external_order_id or "").strip(),
            "sent_at": payload.get("sent_at"),
            "updated_at": fields.Datetime.now().replace(tzinfo=timezone.utc).isoformat(),
            "last_idempotency_key": idempotency_key,
            "extra_data_fields": normalized_fields,
        }
        provider_raw["extra_data"] = extra_data

        tx.write({"provider_raw": provider_raw})
        request.env["surpay.payment.event"].sudo().create(
            {
                "transaction_id": tx.id,
                "source": "internal",
                "event_type": "metadata.extra.updated",
                "payload": {
                    "order_id": tx.order_id,
                    "idempotency_key": idempotency_key,
                    "extra_data_fields_count": len(normalized_fields),
                },
                "signature_valid": True,
                "processing_status": "ok",
                "message": "Metadata extra actualizada por API.",
            }
        )

        return request.make_json_response(
            {
                "status": "ok",
                "idempotent": False,
                "updated": True,
                "order_id": tx.order_id,
                "extra_data_fields_count": len(normalized_fields),
            },
            status=200,
        )
