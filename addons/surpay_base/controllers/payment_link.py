import hashlib
import hmac
import logging
from urllib.parse import urlencode

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayPaymentLinkController(http.Controller):
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
        return request.render("surpay_base.payment_link_page", values)

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

        try:
            intent.refresh_provider_status()
        except Exception as exc:
            _logger.info("Provider status refresh failed for payment link %s: %s", intent.order_id, exc)
        intent.enforce_pending_timeout()

        provider_payload = intent.provider_response_payload or {}
        redirect_url = None
        if intent.is_final_state() and intent.return_url_behavior == "auto_redirect" and intent.return_url:
            redirect_url = self._build_callback_url(intent)
        return {
            "order_id": intent.order_id,
            "external_order_id": intent.external_order_id,
            "state": intent.state,
            "provider_status": provider_payload.get("status") or provider_payload.get("order_status"),
            "paid": intent.state == "paid",
            "failed": intent.state in ("failed", "expired", "cancelled"),
            "done": intent.is_final_state(),
            "redirect_url": redirect_url,
        }
