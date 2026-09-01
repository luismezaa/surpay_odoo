import base64
from datetime import timezone
import hashlib
import hmac
import json
import logging
import time
import uuid
from urllib.parse import parse_qsl, urlencode

import requests

from odoo import fields, http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayApiController(http.Controller):
    ALLOWED_QR_FROM = {"AR", "BR", "PE"}
    PROVIDER_ALIASES = {
        "surpay_fronterizo": "depay",
    }
    EXTRA_FIELDS_MAX_ITEMS = 6
    EXTRA_FIELD_TITLE_MAX_LEN = 24
    EXTRA_FIELD_VALUE_MAX_LEN = 60
    EXTRA_FIELD_KEY_MAX_LEN = 64

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
        _logger.info(f"[PROVIDER DEBUG] Provider recibido: {provider} | Service name: {service_name}")
        if not service_name:
            _logger.warning(f"[PROVIDER DEBUG] No se encontró mapping para provider: {provider}")
            return None
        if service_name not in request.env:
            _logger.error(f"[PROVIDER DEBUG] Service {service_name} no está en request.env. Keys disponibles: {list(request.env.keys())}")
            return None
        _logger.info(f"[PROVIDER DEBUG] Service {service_name} encontrado en request.env")
        return request.env[service_name].sudo()

    @staticmethod
    def _build_provider_callback_url(provider):
        base_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return f"{base_url.rstrip('/')}/api/v1/webhooks/providers/{provider}"

    def _refresh_intent_provider_status(self, intent):
        service = self._resolve_provider_service(intent.provider)
        if service is None or not intent.provider_payment_id:
            return

        provider_status = service.get_payment_status(
            intent.provider_payment_id,
            provider_config=intent.provider_config_id,
        )
        raw_status = service.extract_status(provider_status)
        mapped_state = service.map_depay_status(
            raw_status,
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
                **service.extract_qr_quote(
                    merged_payload,
                    fallback_currency=intent.currency,
                    fallback_amount=intent.amount,
                ),
            }
        )
        intent.sync_transaction()

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
            _logger.warning(f"[INTENT DEBUG] Error de autenticación HMAC: {auth_error}")
            return auth_error

        client = auth_data["client"]
        idempotency_key = auth_data["idempotency_key"]
        if not idempotency_key:
            _logger.warning("[INTENT DEBUG] Falta Idempotency-Key header")
            return self._error(400, "missing_idempotency_key", "Idempotency-Key header is required.")

        raw_body = auth_data.get("raw_body") or b"{}"
        _logger.info(f"[INTENT DEBUG] Iniciando create_payment_intent. Payload recibido: {raw_body}")
        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            _logger.error(f"[INTENT DEBUG] Payload inválido: {raw_body}")
            return self._error(400, "invalid_payload", "Request body must be valid JSON.")
        requested_provider = str(payload.get("provider") or "").strip().lower()
        provider = self._normalize_provider(requested_provider)
        amount = payload.get("amount")
        currency = payload.get("currency") or client.default_local_currency
        external_order_id = payload.get("external_order_id")
        concept = payload.get("concept")
        expires_in = payload.get("expires_in")
        local_country = self._normalize_country_code(payload.get("local_country") or client.default_local_country)
        qr_from = self._normalize_country_code(payload.get("qr_from") or client.default_qr_from)
        terminal_serial = str(payload.get("terminal_serial") or "").strip()
        provider_mode = str(payload.get("provider_mode") or "").strip().lower()

        _logger.info(
            f"[INTENT DEBUG] provider={provider}, requested_provider={requested_provider}, amount={amount}, "
            f"currency={currency}, external_order_id={external_order_id}, concept={concept}, expires_in={expires_in}, "
            f"local_country={local_country}, qr_from={qr_from}, terminal_serial={terminal_serial}, provider_mode={provider_mode}"
        )

        if not provider:
            _logger.warning("[INTENT DEBUG] Falta provider en el payload")
            return self._error(400, "missing_provider", "provider is required.")

        if provider not in self._supported_providers():
            _logger.warning(f"[INTENT DEBUG] Provider no soportado: {provider}")
            return self._error(400, "unsupported_provider", "Provider is not supported.")

        provider_service = self._resolve_provider_service(provider)
        if provider_service is None:
            _logger.error(f"[INTENT DEBUG] Provider service no disponible para: {provider}")
            return self._error(501, "provider_service_not_available", "Provider service is not available.")

        if amount is None or not currency:
            _logger.warning("[INTENT DEBUG] amount o currency faltantes")
            return self._error(400, "invalid_payload", "amount and currency are required.")

        if qr_from and qr_from not in self.ALLOWED_QR_FROM:
            _logger.warning(f"[INTENT DEBUG] qr_from inválido: {qr_from}")
            return self._error(400, "invalid_qr_from", "qr_from must be one of: AR, BR, PE.")

        if provider == "kushki" and not terminal_serial:
            _logger.warning("[INTENT DEBUG] terminal_serial es obligatorio para Kushki")
            return self._error(400, "missing_terminal_serial", "terminal_serial is required for kushki provider.")

        try:
            amount = float(amount)
            if amount <= 0:
                _logger.warning(f"[INTENT DEBUG] amount inválido: {amount}")
                return self._error(400, "invalid_amount", "amount must be greater than 0.")
        except (ValueError, TypeError):
            _logger.warning(f"[INTENT DEBUG] amount no numérico: {amount}")
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
            _logger.info(f"[INTENT DEBUG] Intento idempotente ya existe: {existing_idempotent.id}")
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
                _logger.warning(f"[INTENT DEBUG] external_order_id en conflicto: {external_order_id}")
                return self._error(
                    409,
                    "external_order_conflict",
                    "external_order_id already exists with a non-final recoverable state.",
                )

        try:
            expires_at = intent_model.build_expiration(expires_in)
        except Exception:
            _logger.warning(f"[INTENT DEBUG] expires_in inválido: {expires_in}")
            return self._error(400, "invalid_expiration", "expires_in must be numeric within allowed range.")

        order_id = intent_model.generate_order_id()
        provider_config = client.resolve_provider_config_for_provider(provider)
        if not provider_config:
            _logger.warning(f"[INTENT DEBUG] No se encontró configuración para provider: {provider}")
            return self._error(400, "provider_not_configured", "No provider configuration found for the selected provider.")

        callback_url = self._build_provider_callback_url(provider)
        provider_client_transaction_id = ""
        provider_terminal_serial = ""
        if provider == "kushki":
            provider_client_transaction_id = str(uuid.uuid4())
            provider_terminal_serial = terminal_serial.upper()

        _logger.info(f"[INTENT DEBUG] Creando intent: order_id={order_id}, provider_config_id={provider_config.id}, callback_url={callback_url}")
        intent = intent_model.create(
            {
                "order_id": order_id,
                "external_order_id": external_order_id,
                "provider": provider,
                "requested_provider": requested_provider or provider,
                "provider_client_transaction_id": provider_client_transaction_id,
                "provider_terminal_serial": provider_terminal_serial,
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
                "return_url": client.return_url or "",
                "return_url_behavior": client.return_url_behavior or "webhook_only",
            }
        )
        transaction = intent.ensure_transaction()

        if provider == "kushki":
            # Kushki async callbacks can arrive before this request commits.
            # Persist intent and transaction early to avoid webhook not_found races.
            request.env.cr.commit()

        external_reference = external_order_id or order_id
        provider_payload = {
            "amount": amount_to_provider,
            "local_currency": currency,
            "external_reference": external_reference,
            "notification_url": callback_url,
        }
        display_concept = concept or f"Compra de Giftcard {int(amount_to_provider)} {currency}"
        if local_country:
            provider_payload["local_country"] = local_country
        if qr_from:
            provider_payload["qr_from"] = qr_from

        if provider == "depay":
            pos_id = provider_config.get_credentials().get("pos_id")
            if not pos_id:
                depay_cfg = provider_service._config()
                pos_id = depay_cfg.get("pos_id", "")
            if pos_id:
                provider_payload["pos_external_reference"] = pos_id
        elif provider == "kushki":
            provider_payload["terminal_serial"] = terminal_serial
            provider_payload["partner_id"] = client.partner_id.id if client.partner_id else False
            provider_payload["client_transaction_id"] = provider_client_transaction_id
            if provider_mode in {"sync", "async"}:
                provider_payload["provider_mode"] = provider_mode

        provider_request_payload = dict(provider_payload)
        provider_request_payload["display_concept"] = display_concept

        try:
            _logger.info(f"[INTENT DEBUG] Llamando a provider_service.create_qr con provider_payload: {provider_payload}")
            depay_response = provider_service.create_qr(
                provider_payload,
                provider_config=provider_config,
            )
            _logger.info(f"[INTENT DEBUG] Respuesta de provider_service.create_qr: {depay_response}")
        except Exception as exc:
            _logger.error(f"[INTENT DEBUG] Excepción en provider_service.create_qr: {exc}")
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
            return self._error(502, "provider_error", "Provider QR creation failed.")

        provider_order_id = depay_response.get("order_id")
        provider_client_transaction_id = (
            depay_response.get("client_transaction_id")
            or provider_client_transaction_id
        )
        provider_terminal_serial = (
            depay_response.get("terminal_serial")
            or provider_terminal_serial
        )
        if not provider_order_id and provider == "kushki":
            provider_order_id = provider_client_transaction_id
        depay_raw_status = provider_service.extract_status(depay_response) or "PENDING"
        depay_message = (
            provider_service.extract_status_message(depay_response)
            if hasattr(provider_service, "extract_status_message")
            else depay_response.get("message") or depay_response.get("detail") or ""
        )
        mapped_state = provider_service.map_depay_status(depay_raw_status, depay_message)
        qr_quote = provider_service.extract_qr_quote(
            depay_response,
            fallback_currency=currency,
            fallback_amount=amount_to_provider,
        )

        intent.write(
            {
                "provider_payment_id": provider_order_id,
                "provider_client_transaction_id": provider_client_transaction_id,
                "provider_terminal_serial": provider_terminal_serial,
                "state": mapped_state,
                "provider_request_payload": provider_request_payload,
                "provider_response_payload": depay_response,
                **qr_quote,
            }
        )
        intent.sync_transaction()

        qr_data = None
        if provider == "depay":
            qr_data = depay_response.get("qr_data") or depay_response.get("qr_code")

        response_payload = intent.normalized_payload()
        response_payload.update(
            {
                "qr_data": qr_data,
                "provider_order_id": provider_order_id,
                "provider_status": depay_response.get("order_status") or depay_response.get("orderStatus") or depay_response.get("status"),
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
            "provider": intent.provider,
            "provider_terminal_serial": intent.provider_terminal_serial or provider_payload.get("terminal_serial") or "",
            "provider_status": provider_payload.get("order_status") or provider_payload.get("status") or "",
            "state": intent.state,
            "expires_at": intent.expires_at,
            "qr_data": qr_data,
            "return_url_behavior": intent.return_url_behavior or "webhook_only",
        }
        return request.render("l10n_cl_surpay_depay.payment_link_page", values)

    def _build_callback_url(self, intent):
        """Construye la URL de retorno al comercio con query params y firma HMAC opcional."""
        status_map = {
            "paid": "success",
            "failed": "rejected",
            "expired": "expired",
            "cancelled": "rejected",
        }
        status = status_map.get(intent.state, intent.state)
        params = {
            "status": status,
            "order_id": intent.order_id or "",
            "transaction_id": intent.external_order_id or "",
        }
        secret = intent.client_id.webhook_secret or ""
        if secret:
            msg = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            params["sig"] = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        base = intent.return_url.rstrip("?&")
        sep = "&" if "?" in base else "?"
        return base + sep + urlencode(params)

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
                self._refresh_intent_provider_status(intent)
            except Exception as exc:
                _logger.info("Provider status refresh failed for payment link %s: %s", intent.order_id, exc)

        provider_payload = intent.provider_response_payload or {}
        terminal_states = ("paid", "failed", "expired", "cancelled")
        redirect_url = None
        if intent.state in terminal_states and intent.return_url_behavior == "auto_redirect" and intent.return_url:
            redirect_url = self._build_callback_url(intent)
        return {
            "order_id": intent.order_id,
            "external_order_id": intent.external_order_id,
            "state": intent.state,
            "provider_status": provider_payload.get("status") or provider_payload.get("order_status"),
            "paid": intent.state == "paid",
            "failed": intent.state in ("failed", "expired", "cancelled"),
            "done": intent.state in terminal_states,
            "redirect_url": redirect_url,
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
                self._refresh_intent_provider_status(intent)
            except Exception as exc:
                _logger.info("Provider status refresh failed for %s: %s", intent.order_id, exc)

        return request.make_json_response(intent.normalized_payload(), status=200)

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
