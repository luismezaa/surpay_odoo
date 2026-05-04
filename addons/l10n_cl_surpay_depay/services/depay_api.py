import hashlib
import hmac
import json
import logging

import requests

from odoo import models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class DepayApiService(models.AbstractModel):
    _name = "surpay.depay.api"
    _description = "Depay API Service"

    def _config(self):
        params = self.env["ir.config_parameter"].sudo()
        cfg = {
            "base_url": params.get_param("l10n_cl_surpay_depay.base_url", "https://stage.api.payments.depay.us"),
            "api_key": params.get_param("l10n_cl_surpay_depay.api_key", ""),
            "api_key_header": params.get_param("l10n_cl_surpay_depay.api_key_header", "x-api-key"),
            "customer_uuid": params.get_param("l10n_cl_surpay_depay.customer_uuid", ""),
            "pos_id": params.get_param("l10n_cl_surpay_depay.pos_id", ""),
            "timeout": int(params.get_param("l10n_cl_surpay_depay.timeout_seconds", "20")),
        }

        # Fallback: read credentials from provider configurator when params are missing.
        if not cfg["api_key"] or not cfg["customer_uuid"] or not cfg["pos_id"]:
            provider_model = self.env.get("surpay.provider.config")
            if provider_model is not None:
                provider_cfg = provider_model.sudo().search(
                    [("provider", "=", "depay"), ("state", "=", "active"), ("environment", "=", "sandbox")],
                    limit=1,
                )
                if not provider_cfg:
                    provider_cfg = provider_model.sudo().search(
                        [("provider", "=", "depay"), ("state", "=", "active")],
                        limit=1,
                    )
                if provider_cfg:
                    creds = provider_cfg.get_credentials()
                    cfg["base_url"] = creds.get("base_url") or cfg["base_url"]
                    cfg["api_key"] = creds.get("api_key") or cfg["api_key"]
                    cfg["customer_uuid"] = creds.get("customer_uuid") or cfg["customer_uuid"]
                    cfg["pos_id"] = creds.get("pos_id") or cfg["pos_id"]

        return cfg

    def _assert_config(self, provider_config=None):
        cfg = self._config()
        _logger.info(
            "[DEPAY][_assert_config] from ir.config_parameter: api_key=%s... customer_uuid=%s base_url=%s pos_id=%s",
            (cfg.get("api_key") or "")[:12],
            (cfg.get("customer_uuid") or "")[:8],
            cfg.get("base_url"),
            (cfg.get("pos_id") or "")[:8],
        )
        if provider_config:
            creds = provider_config.get_credentials()
            _logger.info(
                "[DEPAY][_assert_config] provider_config id=%s env=%s: api_key=%s... customer_uuid=%s base_url=%s pos_id=%s",
                provider_config.id,
                provider_config.environment,
                (creds.get("api_key") or "")[:12],
                (creds.get("customer_uuid") or "")[:8],
                creds.get("base_url"),
                (creds.get("pos_id") or "")[:8],
            )
            cfg["base_url"] = creds.get("base_url") or cfg.get("base_url")
            cfg["api_key"] = creds.get("api_key") or cfg.get("api_key")
            cfg["customer_uuid"] = creds.get("customer_uuid") or cfg.get("customer_uuid")
            cfg["pos_id"] = creds.get("pos_id") or cfg.get("pos_id")

            # Production traffic should not use staging hosts unless explicitly configured.
            if provider_config.environment == "production" and "stage.api.payments.depay.us" in (cfg.get("base_url") or ""):
                cfg["base_url"] = cfg["base_url"].replace("stage.api.payments.depay.us", "api.payments.depay.us")

        _logger.info(
            "[DEPAY][_assert_config] FINAL: api_key=%s... customer_uuid=%s base_url=%s pos_id=%s",
            (cfg.get("api_key") or "")[:12],
            (cfg.get("customer_uuid") or "")[:8],
            cfg.get("base_url"),
            (cfg.get("pos_id") or "")[:8],
        )
        if not cfg["api_key"]:
            raise ValidationError("Missing Depay API key configuration.")

        return cfg

    def _get_token(self, cfg):
        url = f"{cfg['base_url'].rstrip('/')}/auth/token"
        headers = {cfg["api_key_header"]: cfg["api_key"]}
        # Some tenants require customer UUID in auth requests as tenant context.
        if cfg.get("customer_uuid"):
            headers["x-customer-uuid"] = cfg["customer_uuid"]

        _logger.info("[DEPAY][_get_token] GET %s | api_key=%s...", url, (cfg["api_key"] or "")[:12])
        response = requests.get(url, headers=headers, timeout=cfg["timeout"])
        _logger.info("[DEPAY][_get_token] response status=%s body=%s", response.status_code, response.text[:200])
        response.raise_for_status()

        data = response.json()
        token = data.get("accessToken")
        if not token:
            raise ValidationError("Depay token response does not include accessToken.")

        _logger.info("[DEPAY][_get_token] token obtained ok (first 20): %s...", token[:20])
        return token

    def create_qr(self, payload, provider_config=None):
        cfg = self._assert_config(provider_config=provider_config)
        payload = dict(payload or {})
        pos_external_reference = payload.get("pos_external_reference")
        if not pos_external_reference:
            pos_external_reference = cfg.get("pos_id")

        # Last fallback: read directly from provider credentials when available.
        if not pos_external_reference and provider_config:
            creds = provider_config.get_credentials()
            pos_external_reference = creds.get("pos_id")

        if pos_external_reference:
            payload["pos_external_reference"] = pos_external_reference
        else:
            _logger.warning(
                "[DEPAY][create_qr] Missing pos_external_reference (cfg.pos_id empty). Provider id=%s env=%s",
                provider_config.id if provider_config else None,
                provider_config.environment if provider_config else None,
            )

        token = self._get_token(cfg)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            cfg["api_key_header"]: cfg["api_key"],
        }
        if cfg.get("customer_uuid"):
            headers["x-customer-uuid"] = cfg["customer_uuid"]

        base_url = cfg["base_url"].rstrip("/")
        base_urls = [base_url]
        if "stage.api.payments.depay.us" in base_url:
            base_urls.append(base_url.replace("stage.api.payments.depay.us", "api.payments.depay.us"))
        endpoints = ["/v2/qr", "/v2/payins/qr"]
        last_error = None

        for current_base_url in base_urls:
            for endpoint in endpoints:
                url = f"{current_base_url}{endpoint}"
                _logger.info("[DEPAY][create_qr] POST %s | payload=%s", url, payload)
                response = requests.post(url, headers=headers, json=payload, timeout=cfg["timeout"])
                _logger.info("[DEPAY][create_qr] response status=%s body=%s", response.status_code, response.text[:400])
                if response.status_code == 404:
                    _logger.warning("Depay endpoint not found (%s), trying fallback if available.", url)
                    last_error = requests.HTTPError(f"404 Client Error: Not Found for url: {url}", response=response)
                    continue
                if response.status_code >= 400:
                    _logger.error(
                        "Depay QR request failed [%s] url=%s body=%s",
                        response.status_code,
                        url,
                        response.text[:500],
                    )

                response.raise_for_status()
                return response.json()

        if last_error:
            raise last_error

        raise ValidationError("Depay QR endpoint resolution failed.")

    def get_payment_status(self, provider_order_id, provider_config=None):
        cfg = self._assert_config(provider_config=provider_config)
        token = self._get_token(cfg)
        url = f"{cfg['base_url'].rstrip('/')}/v2/payins/orders/{provider_order_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            cfg["api_key_header"]: cfg["api_key"],
        }
        if cfg.get("customer_uuid"):
            headers["x-customer-uuid"] = cfg["customer_uuid"]

        response = requests.get(url, headers=headers, timeout=cfg["timeout"])
        response.raise_for_status()
        return response.json()

    def map_depay_status(self, status, message=""):
        status = (status or "").upper()
        message = (message or "").lower()

        if status == "COMPLETED":
            return "paid"
        if status == "FAILED":
            return "failed"
        if status == "CANCELED":
            if "expired" in message:
                return "expired"
            return "cancelled"
        if status in {"CREATED", "WAITING_AMOUNT"}:
            return "pending"

        _logger.warning("Unknown Depay status: %s", status)
        return "pending"

    def validate_callback_signature(self, raw_body, signature_header):
        cfg = self._config()
        customer_uuid = cfg["customer_uuid"]
        api_key = cfg["api_key"]
        if not signature_header or not customer_uuid or not api_key:
            return False

        payload_string = raw_body.decode("utf-8")
        signed_data = f"{payload_string}+{customer_uuid}".encode("utf-8")
        digest = hmac.new(api_key.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()
        return hmac.compare_digest(digest, signature_header)

    @staticmethod
    def dump_json(payload):
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
