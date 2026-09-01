import datetime
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from odoo import _, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class KushkiApiService(models.AbstractModel):
    _name = "surpay.kushki.api"
    _description = "Servicio API de Kushki"

    ZERO_DECIMAL_CURRENCIES = {"CLP", "JPY", "PYG"}

    @staticmethod
    def _object_payload(payload):
        data = payload if isinstance(payload, dict) else {}
        return data.get("object") if isinstance(data.get("object"), dict) else {}

    @classmethod
    def is_terminal_error_payload(cls, payload):
        data = payload if isinstance(payload, dict) else {}
        nested_object = cls._object_payload(data)
        message = str(data.get("message") or "").lower()
        payload_type = str(data.get("type") or "").upper()

        if payload_type in ("MANUFACTURER","ACQUIRER", "NOT_FOUND", "AUTHENTICATION", "TERMINAL", "PARAMETER", "CONFIGURATION", "TERMINAL-PRINTER") and nested_object:
            return True
        if nested_object and "interrump" in message:
            return True
        return False

    @classmethod
    def is_approved_payload(cls, payload):
        data = payload if isinstance(payload, dict) else {}
        operation = data.get("operation") if isinstance(data.get("operation"), dict) else {}
        nested_object = cls._object_payload(data)

        if data.get("approved") is True or operation.get("approved") is True or nested_object.get("approved") is True:
            return True

        response_code = (
            str(data.get("responseCode") or data.get("response_code") or data.get("code") or "").strip()
            or str(operation.get("responseCode") or operation.get("response_code") or operation.get("code") or "").strip()
            or str(nested_object.get("responseCode") or nested_object.get("response_code") or nested_object.get("code") or "").strip()
        )
        return response_code in {"00", "000"}

    def _assert_config(self, provider_config=None):
        if provider_config is None:
            provider_config = self.env["surpay.provider.config"].sudo().resolve_provider_config(provider="kushki")

        if not provider_config:
            raise ValidationError(_("No existe configuración activa para Kushki."))

        creds = provider_config.get_credentials()
        environment = (provider_config.environment or "sandbox").strip().lower()
        default_host = (
            "https://cloudt.kushkipagos.com"
            if environment == "production"
            else "https://uat-cloudt.kushkipagos.com"
        )
        cfg = {
            "base_url": (creds.get("base_url") or default_host).strip(),
            "business_code": (creds.get("business_code") or "").strip(),
            "timeout": int(
                self.env["ir.config_parameter"].sudo().get_param(
                    "l10n_cl_surpay_kushki.timeout_seconds", "90"
                )
            ),
            "status_path": self.env["ir.config_parameter"].sudo().get_param(
                "l10n_cl_surpay_kushki.payment_status_path", ""
            ),
            "default_mode": (self.env["ir.config_parameter"].sudo().get_param(
                "l10n_cl_surpay_kushki.charge_mode", "async"
            ) or "async").strip().lower(),
            "provider_config": provider_config,
        }

        if not cfg["business_code"]:
            raise ValidationError(_("Falta configurar el Business Code de Kushki."))
        if not cfg["base_url"]:
            raise ValidationError(_("Falta configurar la URL base de Kushki."))

        return cfg

    @staticmethod
    def _normalize_mode(mode, default_mode="async"):
        candidate = (mode or default_mode or "async").strip().lower()
        return candidate if candidate in {"sync", "async"} else "async"

    @staticmethod
    def _timestamp_seconds():
        return int(time.time())

    @staticmethod
    def _format_timestamp(timestamp):
        dt = datetime.datetime.fromtimestamp(int(timestamp), tz=datetime.timezone.utc)
        return dt.strftime("%Y:%m:%d:%H:%M")

    @classmethod
    def _generate_token_password(cls, token, timestamp):
        formatted_date = cls._format_timestamp(timestamp)
        key = (f"{token}{formatted_date}").ljust(32, "0")
        return hashlib.md5(key.encode("utf-8")).hexdigest()

    @classmethod
    def _build_authentication_hash(cls, request_data, timestamp, terminal_serial, business_code):
        token = f"{business_code}{terminal_serial}"
        password = cls._generate_token_password(token, timestamp)
        encoded_key_timestamp = base64.b64encode(f"{password}{timestamp}".encode("utf-8")).decode("utf-8")

        auth_data = dict(request_data or {})
        auth_data["key"] = encoded_key_timestamp
        data_json = base64.b64encode(
            json.dumps(auth_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("utf-8")
        return hashlib.sha512(data_json.encode("utf-8")).hexdigest()

    @classmethod
    def _encrypt_data(cls, text, timestamp, terminal_serial, business_code):
        token = f"{business_code}{terminal_serial}"
        password = cls._generate_token_password(token, timestamp)
        key = f"{timestamp}___{password}"[:32]

        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded_text = padder.update(text.encode("utf-8")) + padder.finalize()

        encryptor = Cipher(algorithms.AES(key.encode("utf-8")), modes.CBC(iv)).encryptor()
        encrypted = encryptor.update(padded_text) + encryptor.finalize()
        return f"{iv.hex()}:{encrypted.hex()}"

    @classmethod
    def _charge_headers(cls, cfg, request_data, timestamp, terminal_serial):
        signature = cls._build_authentication_hash(
            request_data,
            timestamp,
            terminal_serial,
            cfg["business_code"],
        )
        return {
            "Authorization": f"Basic {signature}",
            "timestamp": str(timestamp),
            "Content-Type": "application/json",
        }

    @classmethod
    def _to_minor_units(cls, amount, currency):
        value = Decimal(str(amount or 0))
        currency = (currency or "").upper()
        if currency in cls.ZERO_DECIMAL_CURRENCIES:
            return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _build_charge_endpoint(base_url, terminal_serial, mode):
        serial = (terminal_serial or "").strip()
        if not serial:
            raise ValidationError(_("Kushki requiere un serial de terminal para ejecutar charge."))
        normalized_mode = (mode or "sync").strip().lower()
        return f"{base_url.rstrip('/')}/terminal/v1/{serial}/{normalized_mode}/charge"

    def _build_charge_payload(self, payload, client_transaction_id, terminal_label, mode):
        amount = payload.get("amount")
        currency = payload.get("local_currency")
        amount_minor = self._to_minor_units(amount, currency)
        tip_minor = self._to_minor_units(payload.get("tip") or 0, currency)
        cashback_minor = self._to_minor_units(payload.get("cashback_amount") or 0, currency)

        metadata = {
            "reference": (payload.get("reference") or payload.get("external_reference") or "").strip(),
            "customer_email": (payload.get("customer_email") or payload.get("email") or "").strip(),
            "device": (terminal_label or payload.get("device") or payload.get("terminal_serial") or "").strip(),
        }

        request_payload = {
            "client_transaction_id": client_transaction_id,
            "amount": {
                "subtotal_iva0": amount_minor,
                "subtotal_iva": 0,
                "iva": 0,
                "tip": tip_minor,
                "extra_taxes": payload.get("extra_taxes") or {
                    "airport_tax": 0,
                    "iac": 0,
                    "ice": 0,
                    "travel_agency": 0,
                },
            },
            "metadata": metadata,
        }

        

        if cashback_minor:
            request_payload["cashback_amount"] = cashback_minor

        if payload.get("query_deferred") is not None:
            request_payload["query_deferred"] = bool(payload.get("query_deferred"))

        if (mode or "sync").strip().lower() == "async":
            events_webhook_url = (payload.get("events_webhook_url") or payload.get("notification_url") or "").strip()
            if not events_webhook_url:
                raise ValidationError(_("Kushki async charge requiere events_webhook_url o notification_url."))
            request_payload["events_webhook_url"] = events_webhook_url

        return request_payload

    def _send_charge(self, cfg, terminal_serial, request_payload, mode):
        timestamp = self._timestamp_seconds()
        body_json = json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False)
        encrypted_data = self._encrypt_data(body_json, timestamp, terminal_serial, cfg["business_code"])
        headers = self._charge_headers(cfg, request_payload, timestamp, terminal_serial)
        body = json.dumps({"data": encrypted_data}, separators=(",", ":"), ensure_ascii=False)
        url = self._build_charge_endpoint(cfg["base_url"], terminal_serial, mode)

        _logger.info("[KUSHKI][charge] POST %s | terminal=%s | body=%s", url, terminal_serial, body_json)
        response = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=cfg["timeout"])
        _logger.info("[KUSHKI][charge] response status=%s body=%s", response.status_code, response.text[:500])
        response.raise_for_status()

        data = response.json() if response.text else {}
        if not isinstance(data, dict):
            raise ValidationError(_("La respuesta de Kushki no tiene formato JSON objeto."))
        return data

    @classmethod
    def _derive_charge_status(cls, data, mode):
        if cls.is_terminal_error_payload(data):
            return "TERMINAL_REJECTED"
        if cls.is_approved_payload(data):
            return "APPROVED"

        status = cls.extract_status(data)
        if status:
            return status

        message = cls.extract_status_message(data)
        code = str(data.get("code") or "").strip()
        if code == "-1" and "cancel" in message.lower():
            return "TERMINAL_CANCELED"

        return "TERMINAL_ACKNOWLEDGED" if mode == "async" else "PENDING"

    def create_qr(self, payload, provider_config=None):
        # Mantiene la firma común multi-provider, pero en Kushki esta operación
        # no genera QR: envía un comando charge al terminal POS.
        cfg = self._assert_config(provider_config=provider_config)
        payload = dict(payload or {})

        partner = self.env["res.partner"].sudo().browse(int(payload.get("partner_id") or 0))
        terminal_serial = payload.get("terminal_serial")
        terminal = cfg["provider_config"].resolve_kushki_terminal(
            partner=partner if partner.exists() else None,
            terminal_serial=terminal_serial,
        )

        mode = self._normalize_mode(payload.get("provider_mode"), cfg.get("default_mode"))

        client_transaction_id = (
            str(payload.get("client_transaction_id") or "").strip() or str(uuid.uuid4())
        )
        terminal_label = (terminal.terminal_alias or "").strip() or (terminal.terminal_serial or "").strip()
        request_payload = self._build_charge_payload(payload, client_transaction_id, terminal_label, mode)
        data = self._send_charge(cfg, terminal.terminal_serial, request_payload, mode)

        terminal_data = data.get("terminal") if isinstance(data.get("terminal"), dict) else {}

        if not data.get("order_id"):
            data["order_id"] = (
                data.get("transaction_id")
                or data.get("transactionReference")
                or client_transaction_id
            )

        if not data.get("order_status"):
            data["order_status"] = self._derive_charge_status(data, mode)

        data.setdefault("client_transaction_id", client_transaction_id)
        data.setdefault("terminal_serial", terminal_data.get("serialNumber") or terminal.terminal_serial)
        data.setdefault("charge_mode", mode)
        data.setdefault("business_code", cfg["business_code"])
        return data

    def get_payment_status(self, provider_order_id, provider_config=None):
        cfg = self._assert_config(provider_config=provider_config)
        if not cfg.get("status_path"):
            return {
                "order_id": provider_order_id,
                "status": "PENDING",
                "detail": "Kushki Cloud no tiene status endpoint configurado; usar webhook para estado final.",
            }
        status_path = cfg["status_path"].format(provider_order_id=provider_order_id)
        url = f"{cfg['base_url'].rstrip('/')}{status_path}"
        _logger.info("[KUSHKI][get_payment_status] GET %s", url)

        body = ""
        headers = self._headers(cfg, body)
        response = requests.get(url, headers=headers, timeout=cfg["timeout"])
        _logger.info("[KUSHKI][get_payment_status] response status=%s body=%s", response.status_code, response.text[:400])
        response.raise_for_status()
        return response.json() if response.text else {}

    @staticmethod
    def extract_status(payload):
        data = payload or {}
        if not isinstance(data, dict):
            return ""
        operation = data.get("operation") if isinstance(data.get("operation"), dict) else {}
        nested_object = data.get("object") if isinstance(data.get("object"), dict) else {}

        if KushkiApiService.is_terminal_error_payload(data):
            return "TERMINAL_REJECTED"
        if KushkiApiService.is_approved_payload(data):
            return "APPROVED"

        return (
            data.get("order_status")
            or data.get("orderStatus")
            or data.get("payment_status")
            or data.get("event_status")
            or data.get("state")
            or data.get("status")
            or operation.get("order_status")
            or operation.get("orderStatus")
            or operation.get("payment_status")
            or operation.get("state")
            or operation.get("status")
            or nested_object.get("order_status")
            or nested_object.get("orderStatus")
            or nested_object.get("payment_status")
            or nested_object.get("state")
            or nested_object.get("status")
            or ""
        )

    @staticmethod
    def extract_status_message(payload):
        data = payload if isinstance(payload, dict) else {}
        operation = data.get("operation") if isinstance(data.get("operation"), dict) else {}
        nested_object = data.get("object") if isinstance(data.get("object"), dict) else {}
        return (
            data.get("message")
            or data.get("detail")
            or data.get("description")
            or operation.get("message")
            or operation.get("detail")
            or operation.get("description")
            or nested_object.get("message")
            or nested_object.get("detail")
            or nested_object.get("description")
            or ""
        )

    @staticmethod
    def extract_event_reference(payload):
        data = payload if isinstance(payload, dict) else {}
        operation = data.get("operation") if isinstance(data.get("operation"), dict) else {}
        nested_object = data.get("object") if isinstance(data.get("object"), dict) else {}

        provider_order_id = (
            data.get("order_id")
            or data.get("orderId")
            or operation.get("transactionReference")
            or operation.get("transaction_reference")
            or operation.get("id")
            or nested_object.get("order_id")
            or nested_object.get("orderId")
            or nested_object.get("transaction_reference")
            or nested_object.get("transactionReference")
            or nested_object.get("transaction_id")
            or nested_object.get("transactionId")
            or nested_object.get("id")
            or ""
        )
        client_transaction_id = (
            data.get("client_transaction_id")
            or data.get("clientTransactionId")
            or operation.get("client_transaction_id")
            or operation.get("clientTransactionId")
            or nested_object.get("client_transaction_id")
            or nested_object.get("clientTransactionId")
            or ""
        )
        return {
            "provider_order_id": provider_order_id,
            "client_transaction_id": client_transaction_id,
            "event_id": data.get("event_id") or data.get("eventId") or "",
            "previous_status": data.get("previous_status") or data.get("previousStatus") or "",
        }

    @staticmethod
    def extract_callback_signature(headers):
        return (
            headers.get("Authorization")
            or headers.get("authorization")
            or headers.get("signature")
            or ""
        )

    @staticmethod
    def _as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def extract_qr_quote(self, payload, fallback_currency="", fallback_amount=0.0):
        data = payload if isinstance(payload, dict) else {}

        has_qr_quote = any(
            key in data
            for key in ("currency", "qr_currency", "amount", "qr_amount", "converted_amount", "exchange_rate", "fx_rate")
        )
        if not has_qr_quote:
            # Kushki terminal-only normalmente no entrega datos de cotización QR.
            return {
                "qr_currency": "",
                "qr_converted_amount": 0.0,
                "qr_exchange_rate": 0.0,
            }

        qr_currency = (data.get("currency") or data.get("qr_currency") or fallback_currency or "").upper()
        qr_amount = self._as_float(
            data.get("amount")
            or data.get("qr_amount")
            or data.get("converted_amount")
            or fallback_amount
        )
        qr_rate = self._as_float(data.get("exchange_rate") or data.get("fx_rate"))

        if qr_amount is None:
            qr_amount = fallback_amount or 0.0
        if qr_rate is None:
            qr_rate = (qr_amount / fallback_amount) if fallback_amount else 0.0

        return {
            "qr_currency": qr_currency,
            "qr_converted_amount": qr_amount,
            "qr_exchange_rate": qr_rate or 0.0,
        }

    def map_depay_status(self, status, message=""):
        status = (status or "").upper()
        message = (message or "").lower()

        if status in {"TERMINAL_REJECTED", "MANUFACTURER"}:
            return "failed"

        if status in {"APPROVAL", "APPROVED", "PAID", "COMPLETED", "SUCCESS", "SUCCEEDED"}:
            return "paid"
        if status in {"DECLINED", "TERMINAL_REJECTED", "REJECTED", "FAILED", "ERROR", "DENIED"}:
            return "failed"
        if status in {"TERMINAL_ACKNOWLEDGED", "CARD_PRESENTED", "APPROVAL_REQUESTED", "PROCESSING", "PENDING", "CREATED"}:
            return "pending"
        if status in {"EXPIRED", "TIMEOUT"}:
            return "expired"
        if status in {"TERMINAL_CANCELED", "TERMINAL_CANCELLED", "CANCELLED", "CANCELED", "USER_CANCELED", "USER_CANCELLED", "CANCELED_BY_USER", "CANCELLED_BY_USER"}:
            return "cancelled"
        if "cancel" in message:
            return "cancelled"
        if "expire" in message:
            return "expired"
        if "fail" in message or "reject" in message or "error" in message:
            return "failed"
        return "pending"

    def validate_callback_signature(self, raw_body, signature_header, provider_config=None):
        cfg = self._assert_config(provider_config=provider_config)
        if not signature_header:
            return False

        signature_value = signature_header.strip()
        if " " in signature_value:
            # Accept values like: "HMAC <base64>".
            signature_value = signature_value.split(" ")[-1].strip()

        body = raw_body.decode("utf-8") if isinstance(raw_body, (bytes, bytearray)) else str(raw_body or "")
        expected_signature = base64.b64encode(
            hmac.new(
                cfg["business_code"].encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        return hmac.compare_digest(expected_signature, signature_value)

    @staticmethod
    def should_validate_callback_signature():
        # Current Kushki flow does not provide a stable callback signature
        # contract in this integration, so validation is skipped.
        return False
