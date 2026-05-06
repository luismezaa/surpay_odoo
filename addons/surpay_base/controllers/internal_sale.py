import uuid
import json
import logging

from odoo import http
from odoo.http import request


_logger = logging.getLogger(__name__)


class SurpayInternalSaleController(http.Controller):
    FINAL_STATES = {"paid", "failed", "cancelled", "expired"}
    QR_FROM_OPTIONS = (
        ("AR", "Argentina", "🇦🇷"),
        ("BR", "Brasil", "🇧🇷"),
        ("PE", "Peru", "🇵🇪"),
    )

    def _odoo_return_url(self):
        """Build a stable backend URL to avoid returning to the last act_url state."""
        menu = request.env.ref("surpay_base.surpay_payment_transaction_menu", raise_if_not_found=False)
        action = request.env.ref("surpay_base.surpay_payment_transaction_action", raise_if_not_found=False)
        if menu and action:
            return f"/web#menu_id={menu.id}&action={action.id}"
        return "/web"

    def _active_provider_configs(self):
        return request.env["surpay.provider.config"].sudo().search([
            ("state", "=", "active"),
        ])

    def _default_api_client(self):
        client_model = request.env["surpay.api.client"].sudo()
        client = client_model.search([
            ("client_id", "=", "surpay_internal"),
        ], limit=1)
        if client:
            return client

        return client_model.create(
            {
                "name": "Surpay Internal",
                "client_id": "surpay_internal",
                "client_secret": uuid.uuid4().hex,
                "webhook_secret": uuid.uuid4().hex,
                "ip_filter_mode": "all",
                "default_local_currency": "CLP",
                "default_local_country": "CL",
                "default_qr_from": "CL",
            }
        )

    @classmethod
    def _normalize_qr_from(cls, value):
        code = (value or "").strip().upper()
        allowed = {item[0] for item in cls.QR_FROM_OPTIONS}
        return code if code in allowed else ""

    def _create_depay_intent(self, provider_config, amount_clp, concept, email, qr_from):
        if "surpay.payment.intent" not in request.env or "surpay.depay.api" not in request.env:
            return {
                "error": "El módulo de Depay no está disponible."
            }

        client = self._default_api_client()
        commission_data = request.env["surpay.commission.rule"].sudo().compute_amounts(
            provider=provider_config.provider,
            base_amount=amount_clp,
            currency="CLP",
            client_id=client.id,
            sales_channel="internal",
        )
        amount_to_provider = commission_data["total_amount"]
        commission_rule = commission_data["rule"]

        intent_model = request.env["surpay.payment.intent"].sudo()
        order_id = intent_model.generate_order_id()
        expires_at = intent_model.build_expiration(900)

        callback_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        callback_url = f"{callback_url.rstrip('/')}/api/v1/webhooks/providers/depay"

        intent = intent_model.create(
            {
                "order_id": order_id,
                "external_order_id": order_id,
                "provider": "depay",
                "source_channel": "internal",
                "base_amount": amount_clp,
                "commission_percent": commission_data["commission_percent"],
                "commission_amount": commission_data["commission_amount"],
                "commission_rule_id": commission_rule.id,
                "amount": amount_to_provider,
                "currency": "CLP",
                "idempotency_key": uuid.uuid4().hex,
                "client_id": client.id,
                "partner_id": request.env.user.partner_id.id,
                "notification_url": callback_url,
                "expires_at": expires_at,
                "concept": concept,
                "qr_from": qr_from,
                "provider_config_id": provider_config.id,
            }
        )

        depay_payload = {
            "amount": amount_to_provider,
            "local_currency": "CLP",
            "local_country": "CL",
            "qr_from": qr_from,
            "external_reference": order_id,
            "notification_url": callback_url,
        }
        creds = provider_config.get_credentials()
        if creds.get("pos_id"):
            depay_payload["pos_external_reference"] = creds.get("pos_id")
        display_concept = concept or f"Compra {int(amount_to_provider)} CLP"
        provider_request_payload = dict(depay_payload)
        provider_request_payload["display_concept"] = display_concept

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
            tx = intent.ensure_transaction()
            tx.write(
                {
                    "concept": concept,
                    "customer_email": email,
                    "seller_user_id": request.env.user.id,
                    "provider_config_id": provider_config.id,
                }
            )
            return {"error": str(exc)}

        provider_order_id = depay_response.get("order_id")
        depay_raw_status = (
            depay_response.get("order_status")
            or depay_response.get("orderStatus")
            or depay_response.get("state")
            or "PENDING"
        )
        mapped_state = request.env["surpay.depay.api"].sudo().map_depay_status(depay_raw_status)

        intent.write(
            {
                "provider_payment_id": provider_order_id,
                "state": mapped_state,
                "provider_request_payload": provider_request_payload,
                "provider_response_payload": depay_response,
            }
        )
        intent.sync_transaction()

        tx = intent.ensure_transaction()
        tx.write(
            {
                "concept": concept,
                "customer_email": email,
                "seller_user_id": request.env.user.id,
                "provider_config_id": provider_config.id,
            }
        )

        return {
            "order_id": intent.order_id,
            "payment_url": intent.payment_link_url(),
            "state": intent.state,
        }

    @http.route("/surpay/new-sale", type="http", auth="user", methods=["GET"], csrf=False)
    def new_sale_page(self, **kwargs):
        configs = self._active_provider_configs()
        values = {
            "provider_configs": configs,
            "default_provider_config_id": configs[:1].id if configs else False,
            "qr_from_options": self.QR_FROM_OPTIONS,
            "default_qr_from": "AR",
            "odoo_return_url": self._odoo_return_url(),
        }
        return request.render("surpay_base.new_sale_page", values)

    @http.route("/surpay/new-sale/start", type="json", auth="user", methods=["POST"], csrf=False)
    def new_sale_start(self):
        payload = {}
        try:
            raw = request.httprequest.get_data(cache=False, as_text=True) or ""
            if raw:
                payload = json.loads(raw)
        except Exception:
            payload = {}
        amount_clp = payload.get("amount_clp")
        concept = (payload.get("concept") or "").strip()
        email = (payload.get("email") or "").strip()
        provider_config_id = payload.get("provider_config_id")
        qr_from = self._normalize_qr_from(payload.get("qr_from"))

        try:
            amount_clp = float(amount_clp)
        except (TypeError, ValueError):
            return {"error": {"message": "Monto inválido."}}

        if amount_clp <= 0:
            return {"error": {"message": "El monto debe ser mayor a 0."}}
        if not concept:
            return {"error": {"message": "El código de referencia (concepto) es obligatorio."}}
        if not provider_config_id:
            return {"error": {"message": "Selecciona una plataforma de pago."}}
        if not qr_from:
            return {"error": {"message": "Selecciona un pais para origen QR."}}

        provider_config = request.env["surpay.provider.config"].sudo().browse(int(provider_config_id))
        if not provider_config.exists() or provider_config.state != "active":
            return {"error": {"message": "La plataforma seleccionada no está activa."}}

        if provider_config.provider != "depay":
            return {
                "error": {
                    "message": f"La plataforma {provider_config.provider.upper()} aún no está implementada en nueva venta.",
                }
            }

        result = self._create_depay_intent(provider_config, amount_clp, concept, email, qr_from)
        if result.get("error"):
            return {"error": {"message": result["error"]}}

        return {
            "ok": True,
            "order_id": result["order_id"],
            "payment_url": result["payment_url"],
            "processing_url": f"/surpay/new-sale/processing/{result['order_id']}",
        }

    @http.route("/surpay/new-sale/processing/<string:order_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def new_sale_processing(self, order_id, **kwargs):
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        if not tx:
            return request.not_found()

        values = {
            "order_id": order_id,
            "state": tx.state,
            "concept": tx.concept,
            "amount": tx.amount,
            "currency": tx.currency,
            "provider": tx.provider,
        }
        return request.render("surpay_base.new_sale_processing_page", values)

    @http.route("/surpay/new-sale/status/<string:order_id>", type="json", auth="user", methods=["POST"], csrf=False)
    def new_sale_status(self, order_id):
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        if not tx:
            return {"error": {"message": "Transacción no encontrada."}}

        if tx.provider == "depay" and tx.provider_payment_id and tx.state not in self.FINAL_STATES:
            try:
                provider_status = request.env["surpay.depay.api"].sudo().get_payment_status(
                    tx.provider_payment_id,
                    provider_config=tx.provider_config_id,
                )
                depay_service = request.env["surpay.depay.api"].sudo()
                depay_raw_status = depay_service.extract_status(provider_status)
                mapped_state = request.env["surpay.depay.api"].sudo().map_depay_status(
                    depay_raw_status,
                    provider_status.get("message") or provider_status.get("detail") or "",
                )
                tx.write(
                    {
                        "state": mapped_state,
                        "provider_raw": provider_status,
                    }
                )
                intent = request.env["surpay.payment.intent"].sudo().search([
                    ("order_id", "=", tx.order_id)
                ], limit=1)
                if intent:
                    merged = dict(intent.provider_response_payload or {})
                    merged.update(provider_status or {})
                    intent.write({"state": mapped_state, "provider_response_payload": merged})
                    intent.sync_transaction()
            except Exception as exc:
                _logger.warning(
                    "No se pudo refrescar estado Depay para order_id=%s provider_payment_id=%s: %s",
                    tx.order_id,
                    tx.provider_payment_id,
                    exc,
                )

        done = tx.state in self.FINAL_STATES
        return {
            "ok": True,
            "order_id": tx.order_id,
            "state": tx.state,
            "done": done,
            "paid": tx.state == "paid",
            "failed": tx.state in {"failed", "cancelled", "expired"},
        }

    @http.route("/surpay/new-sale/success/<string:order_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def new_sale_success(self, order_id, **kwargs):
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        return request.render(
            "surpay_base.new_sale_success_page",
            {
                "order_id": order_id,
                "concept": tx.concept if tx else "",
                "odoo_return_url": self._odoo_return_url(),
            },
        )

    @http.route("/surpay/new-sale/failed/<string:order_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def new_sale_failed(self, order_id, **kwargs):
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        return request.render(
            "surpay_base.new_sale_failed_page",
            {
                "order_id": order_id,
                "state": tx.state if tx else "failed",
                "odoo_return_url": self._odoo_return_url(),
            },
        )

