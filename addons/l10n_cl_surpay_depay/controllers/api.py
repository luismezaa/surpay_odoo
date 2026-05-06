import base64
from datetime import timezone
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

import requests

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayApiController(http.Controller):
    ALLOWED_QR_FROM = {"AR", "BR", "PE"}

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

    @staticmethod
    def _client_ip():
        xff = request.httprequest.headers.get("X-Forwarded-For")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
        return request.httprequest.remote_addr

    @classmethod
    def _normalize_country_code(cls, value):
        return (value or "").strip().upper()

    def _resolve_provider_config_for_client(self, client, provider="depay"):
        if client.provider_config_id and client.provider_config_id.provider == provider:
            return client.provider_config_id.sudo()
        return request.env["surpay.provider.config"].sudo().resolve_provider_config(provider=provider)

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

    def _dispatch_outbound_webhook(self, transaction, event_payload):
        request.env["surpay.payment.event"].sudo().create_outbound_webhook_event(
            transaction,
            event_payload,
            event_type="payment.status.changed",
            contract_version="v1",
        )

    @http.route("/api/v1/payments/intents", type="http", auth="public", methods=["POST"], csrf=False)
    def create_payment_intent(self):
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
        provider = payload.get("provider", "depay")
        amount = payload.get("amount")
        currency = payload.get("currency") or client.default_local_currency
        external_order_id = payload.get("external_order_id")
        concept = payload.get("concept")
        expires_in = payload.get("expires_in")
        local_country = self._normalize_country_code(payload.get("local_country") or client.default_local_country)
        qr_from = self._normalize_country_code(payload.get("qr_from") or client.default_qr_from)

        if provider != "depay":
            return self._error(400, "unsupported_provider", "Only depay provider is enabled in this module.")

        if amount is None or not currency:
            return self._error(400, "invalid_payload", "amount and currency are required.")

        if qr_from and qr_from not in self.ALLOWED_QR_FROM:
            return self._error(400, "invalid_qr_from", "qr_from must be one of: AR, BR, PE.")

        # Ensure amount is numeric (float)
        try:
            amount = float(amount)
            if amount <= 0:
                return self._error(400, "invalid_amount", "amount must be greater than 0.")
        except (ValueError, TypeError):
            return self._error(400, "invalid_amount", "amount must be a valid number.")

        commission_data = request.env["surpay.commission.rule"].sudo().compute_amounts(
            provider=provider,
            base_amount=amount,
            currency=currency,
            client_id=client.id,
            sales_channel="external",
        )
        amount_to_provider = commission_data["total_amount"]
        commission_rule = commission_data["rule"]

        intent_model = request.env["surpay.payment.intent"].sudo()
        existing_idempotent = intent_model.search(
            [
                ("client_id", "=", client.id),
                ("idempotency_key", "=", idempotency_key),
            ],
            limit=1,
        )
        if existing_idempotent:
            return request.make_json_response(existing_idempotent.normalized_payload(), status=200)

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

        order_id = intent_model.generate_order_id()
        provider_config = self._resolve_provider_config_for_client(client, provider="depay")
        if not provider_config:
            return self._error(400, "provider_not_configured", "No active Depay provider configuration found.")

        callback_url = request.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", ""
        ) + "/api/v1/webhooks/providers/depay"

        intent = intent_model.create(
            {
                "order_id": order_id,
                "external_order_id": external_order_id,
                "provider": "depay",
                "source_channel": "external",
                "base_amount": amount,
                "commission_percent": commission_data["commission_percent"],
                "commission_amount": commission_data["commission_amount"],
                "commission_rule_id": commission_rule.id,
                "amount": amount_to_provider,
                "currency": currency,
                "state": "created",
                "idempotency_key": idempotency_key,
                "client_id": client.id,
                "provider_config_id": provider_config.id,
                "notification_url": callback_url,
                "expires_at": expires_at,
                "concept": concept,
                "qr_from": qr_from,
            }
        )
        transaction = intent.ensure_transaction()

        cfg = request.env["ir.config_parameter"].sudo()
        external_reference = external_order_id or order_id
        depay_payload = {
            "amount": amount_to_provider,
            "local_currency": currency,
            "external_reference": external_reference,
            "notification_url": callback_url,
        }
        display_concept = concept or f"Compra de Giftcard {int(amount_to_provider)} {currency}"
        provider_request_payload = dict(depay_payload)
        provider_request_payload["display_concept"] = display_concept
        if local_country:
            depay_payload["local_country"] = local_country
        if qr_from:
            depay_payload["qr_from"] = qr_from
        pos_id = provider_config.get_credentials().get("pos_id")
        if not pos_id:
            pos_id = cfg.get_param("l10n_cl_surpay_depay.pos_id", "")
        if not pos_id:
            depay_cfg = request.env["surpay.depay.api"].sudo()._config()
            pos_id = depay_cfg.get("pos_id", "")
        if pos_id:
            depay_payload["pos_external_reference"] = pos_id

        try:
            depay_response = request.env["surpay.depay.api"].sudo().create_qr(
                depay_payload,
                provider_config=provider_config,
            )
        except Exception as exc:
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
            return self._error(502, "provider_error", "Depay QR creation failed.")

        provider_order_id = depay_response.get("order_id")
        depay_raw_status = (
            depay_response.get("order_status")
            or depay_response.get("orderStatus")
            or depay_response.get("state")
            or "PENDING"
        )
        depay_service = request.env["surpay.depay.api"].sudo()
        mapped_state = depay_service.map_depay_status(depay_raw_status)
        qr_quote = depay_service.extract_qr_quote(
            depay_response,
            fallback_currency=currency,
            fallback_amount=amount_to_provider,
        )

        intent.write(
            {
                "provider_payment_id": provider_order_id,
                "state": mapped_state,
                "provider_request_payload": provider_request_payload,
                "provider_response_payload": depay_response,
                **qr_quote,
            }
        )
        intent.sync_transaction()

        response_payload = intent.normalized_payload()
        response_payload.update(
            {
                "qr_data": depay_response.get("qr_data") or depay_response.get("qr_code"),
                "provider_order_id": provider_order_id,
                "provider_status": depay_response.get("order_status") or depay_response.get("status"),
            }
        )
        return request.make_json_response(response_payload, status=201)

    @http.route(
        "/pay/<string:payment_token>",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    def payment_link_page(self, payment_token, **kwargs):
        intent = (
            request.env["surpay.payment.intent"]
            .sudo()
            .search([("payment_link_token", "=", payment_token)], limit=1)
        )
        if not intent:
            return request.not_found()

        provider_payload = intent.provider_response_payload or {}
        request_payload = intent.provider_request_payload or {}
        qr_data = provider_payload.get("qr_data") or provider_payload.get("qr_code")
        amount_value = provider_payload.get("user_amount") or intent.amount
        if isinstance(amount_value, (int, float)):
            amount_display = f"{amount_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        else:
            amount_display = str(amount_value or "")
        values = {
            "payment_token": intent.payment_link_token,
            "merchant_name": (intent.client_id.name or "Comercio").upper(),
            "concept": request_payload.get("display_concept") or request_payload.get("external_reference") or intent.external_order_id or intent.order_id,
            "amount_display": amount_display,
            "currency": provider_payload.get("user_currency") or intent.currency,
            "state": intent.state,
            "expires_at": intent.expires_at,
            "qr_data": qr_data,
        }
        return request.render("l10n_cl_surpay_depay.payment_link_page", values)

    @http.route(
        "/pay/<string:payment_token>/status",
        type="json",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def payment_link_status(self, payment_token):
        intent = (
            request.env["surpay.payment.intent"]
            .sudo()
            .search([("payment_link_token", "=", payment_token)], limit=1)
        )
        if not intent:
            return {"error": {"code": "not_found", "message": "Payment link not found."}}

        if intent.provider_payment_id and intent.state not in ("paid", "failed", "expired", "cancelled"):
            try:
                provider_status = request.env["surpay.depay.api"].sudo().get_payment_status(
                    intent.provider_payment_id,
                    provider_config=intent.provider_config_id,
                )
                depay_service = request.env["surpay.depay.api"].sudo()
                depay_raw_status = depay_service.extract_status(provider_status)
                mapped_state = request.env["surpay.depay.api"].sudo().map_depay_status(
                    depay_raw_status,
                    provider_status.get("message") or provider_status.get("detail") or "",
                )
                existing_payload = dict(intent.provider_response_payload or {})
                merged_payload = dict(existing_payload)
                merged_payload.update(provider_status or {})
                if not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
                    merged_payload["qr_data"] = existing_payload.get("qr_data") or existing_payload.get("qr_code")
                intent.write(
                    {
                        "state": mapped_state,
                        "provider_response_payload": merged_payload,
                        **depay_service.extract_qr_quote(
                            merged_payload,
                            fallback_currency=intent.currency,
                            fallback_amount=intent.amount,
                        ),
                    }
                )
                intent.sync_transaction()
            except Exception as exc:
                _logger.info("Depay status refresh failed for payment link %s: %s", intent.order_id, exc)

        provider_payload = intent.provider_response_payload or {}
        return {
            "order_id": intent.order_id,
            "external_order_id": intent.external_order_id,
            "state": intent.state,
            "provider_status": provider_payload.get("status") or provider_payload.get("order_status"),
            "paid": intent.state == "paid",
            "failed": intent.state in ("failed", "expired", "cancelled"),
            "done": intent.state in ("paid", "failed", "expired", "cancelled"),
        }

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

        if intent.provider_payment_id:
            try:
                provider_status = request.env["surpay.depay.api"].sudo().get_payment_status(
                    intent.provider_payment_id,
                    provider_config=intent.provider_config_id,
                )
                depay_service = request.env["surpay.depay.api"].sudo()
                depay_raw_status = depay_service.extract_status(provider_status)
                mapped_state = request.env["surpay.depay.api"].sudo().map_depay_status(
                    depay_raw_status,
                    provider_status.get("message") or provider_status.get("detail") or "",
                )
                existing_payload = dict(intent.provider_response_payload or {})
                merged_payload = dict(existing_payload)
                merged_payload.update(provider_status or {})
                if not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
                    merged_payload["qr_data"] = existing_payload.get("qr_data") or existing_payload.get("qr_code")
                intent.write(
                    {
                        "state": mapped_state,
                        "provider_response_payload": merged_payload,
                        **depay_service.extract_qr_quote(
                            merged_payload,
                            fallback_currency=intent.currency,
                            fallback_amount=intent.amount,
                        ),
                    }
                )
                intent.sync_transaction()
            except Exception as exc:
                _logger.info("Depay status refresh failed for %s: %s", intent.order_id, exc)

        return request.make_json_response(intent.normalized_payload(), status=200)

    @http.route(
        "/api/v1/webhooks/providers/depay",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def depay_webhook(self):
        raw_body = self._raw_body()
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._error(400, "invalid_payload", "Invalid JSON callback payload.")

        provider_order_id = payload.get("order_id")
        if not provider_order_id:
            return self._error(400, "missing_order_id", "Callback payload missing order_id.")

        intent = (
            request.env["surpay.payment.intent"]
            .sudo()
            .search([("provider_payment_id", "=", provider_order_id)], limit=1)
        )
        if not intent:
            intent = (
                request.env["surpay.payment.intent"]
                .sudo()
                .search([("order_id", "=", provider_order_id)], limit=1)
            )

        if not intent:
            return self._error(404, "not_found", "Payment intent not found for provider order_id.")

        signature_header = request.httprequest.headers.get("signature")
        signature_valid = request.env["surpay.depay.api"].sudo().validate_callback_signature(
            raw_body,
            signature_header,
            provider_config=intent.provider_config_id,
        )
        if not signature_valid:
            return self._error(401, "invalid_provider_signature", "Invalid provider callback signature.")

        depay_service = request.env["surpay.depay.api"].sudo()
        mapped_state = depay_service.map_depay_status(
            payload.get("status") or payload.get("order_status") or payload.get("orderStatus") or payload.get("state"),
            payload.get("message") or payload.get("detail"),
        )
        existing_payload = dict(intent.provider_response_payload or {})
        merged_payload = dict(existing_payload)
        merged_payload.update(payload or {})
        if not (merged_payload.get("qr_data") or merged_payload.get("qr_code")):
            merged_payload["qr_data"] = existing_payload.get("qr_data") or existing_payload.get("qr_code")
        intent.write(
            {
                "state": mapped_state,
                "provider_response_payload": merged_payload,
                **depay_service.extract_qr_quote(
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
                "event_type": payload.get("type", "PAYMENT"),
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
                "provider": "depay",
                "provider_order_id": intent.provider_payment_id,
                "status": intent.state,
                "status_code": self._state_code(intent.state),
            },
            "provider": {
                "name": "depay",
                "status": payload.get("status"),
                "message": payload.get("message"),
                "raw": payload,
            },
        }
        self._dispatch_outbound_webhook(transaction, outbound_payload)

        return request.make_json_response({"status": "ok"}, status=200)
