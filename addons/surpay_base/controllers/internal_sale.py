import uuid
import json
import logging
import base64

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request


_logger = logging.getLogger(__name__)


class SurpayInternalSaleController(http.Controller):
    FINAL_STATES = {"paid", "failed", "cancelled", "expired"}
    QR_FROM_OPTIONS = (
        ("AR", "Argentina", "🇦🇷"),
        ("BR", "Brasil", "🇧🇷"),
        ("PE", "Peru", "🇵🇪"),
    )

    @staticmethod
    def _detect_image_content_type(image_bytes):
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return "image/gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        return "application/octet-stream"

    def _odoo_return_url(self):
        """Build a stable backend URL to avoid returning to the last act_url state."""
        menu = request.env.ref("surpay_base.surpay_payment_transaction_menu", raise_if_not_found=False)
        action = request.env.ref("surpay_base.surpay_payment_transaction_action", raise_if_not_found=False)
        if menu and action:
            return f"/web#menu_id={menu.id}&action={action.id}"
        return "/web"

    def _ensure_new_sale_access(self):
        user = request.env.user
        if user.has_group("surpay_base.group_surpay_provider_user") and not user.has_group("surpay_base.group_surpay_manager"):
            raise AccessError("No tiene acceso a Nueva Venta.")

    def _active_provider_configs(self):
        return request.env["surpay.provider.config"].sudo().search([
            ("state", "=", "active"),
        ])

    @staticmethod
    def _kushki_terminal_label(terminal):
        alias = (terminal.terminal_alias or "").strip()
        serial = (terminal.terminal_serial or "").strip()
        partner_name = terminal.partner_id.commercial_partner_id.name if terminal.partner_id else ""
        label = alias or serial or str(terminal.id)
        if alias and serial and alias != serial:
            label = f"{alias} ({serial})"
        elif serial and not alias:
            label = serial
        if partner_name:
            label = f"{label} - {partner_name}"
        return label

    def _kushki_terminals_by_config(self, provider_configs):
        terminals_by_config = {}
        terminal_model = request.env["surpay.kushki.terminal"].sudo()
        for cfg in provider_configs:
            if cfg.provider != "kushki":
                continue
            terminals = terminal_model.search([
                ("provider_config_id", "=", cfg.id),
                ("active", "=", True),
            ], order="partner_id, terminal_alias, terminal_serial")
            terminals_by_config[str(cfg.id)] = [
                {
                    "id": terminal.id,
                    "label": self._kushki_terminal_label(terminal),
                    "terminal_alias": terminal.terminal_alias or "",
                    "terminal_serial": terminal.terminal_serial,
                    "partner_name": terminal.partner_id.commercial_partner_id.name if terminal.partner_id else "",
                }
                for terminal in terminals
            ]
        return terminals_by_config

    def _resolve_kushki_terminal_by_id(self, provider_config, terminal_id):
        try:
            terminal_record_id = int(terminal_id or 0)
        except (TypeError, ValueError):
            return None
        terminal = request.env["surpay.kushki.terminal"].sudo().browse(terminal_record_id)
        if not terminal.exists():
            return None
        if terminal.provider_config_id.id != provider_config.id:
            return None
        if not terminal.active:
            return None
        return terminal

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
        callback_url = f"{callback_url.rstrip('/')}/api/v1/webhooks/providers/{provider_config.provider}"

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
        depay_service = request.env["surpay.depay.api"].sudo()
        mapped_state = depay_service.map_depay_status(depay_raw_status)
        qr_quote = depay_service.extract_qr_quote(
            depay_response,
            fallback_currency="CLP",
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

    def _create_kushki_intent(self, provider_config, amount_clp, concept, email, terminal_id):
        if "surpay.payment.intent" not in request.env or "surpay.kushki.api" not in request.env:
            return {"error": "El módulo de Kushki no está disponible."}

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

        terminal = self._resolve_kushki_terminal_by_id(provider_config, terminal_id)
        if not terminal:
            return {"error": "Selecciona una terminal Kushki válida para este cliente."}

        intent_model = request.env["surpay.payment.intent"].sudo()
        order_id = intent_model.generate_order_id()
        expires_at = intent_model.build_expiration(900)

        callback_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        callback_url = f"{callback_url.rstrip('/')}/api/v1/webhooks/providers/{provider_config.provider}"

        intent = intent_model.create(
            {
                "order_id": order_id,
                "external_order_id": order_id,
                "provider": "kushki",
                "source_channel": "internal",
                "base_amount": amount_clp,
                "commission_percent": commission_data["commission_percent"],
                "commission_amount": commission_data["commission_amount"],
                "commission_rule_id": commission_rule.id,
                "amount": amount_to_provider,
                "currency": "CLP",
                "idempotency_key": uuid.uuid4().hex,
                "client_id": client.id,
                "partner_id": terminal.partner_id.id,
                "notification_url": callback_url,
                "expires_at": expires_at,
                "concept": concept,
                "provider_config_id": provider_config.id,
                "provider_terminal_serial": terminal.terminal_serial,
                "provider_client_transaction_id": uuid.uuid4().hex,
            }
        )

        provider_payload = {
            "amount": amount_to_provider,
            "local_currency": "CLP",
            "external_reference": order_id,
            "reference": concept,
            "customer_email": email,
            "device": terminal.terminal_alias or terminal.terminal_serial,
            "notification_url": callback_url,
            "events_webhook_url": callback_url,
            "terminal_serial": terminal.terminal_serial,
            "partner_id": terminal.partner_id.id,
            "client_transaction_id": intent.provider_client_transaction_id,
        }
        display_concept = concept or f"Compra {int(amount_to_provider)} CLP"
        provider_request_payload = dict(provider_payload)
        provider_request_payload["display_concept"] = display_concept

        intent.write(
            {
                "state": "pending",
                "provider_request_payload": provider_request_payload,
                "provider_response_payload": {
                    "dispatch_state": "queued",
                    "message": "Despacho a terminal en cola.",
                },
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

    def _dispatch_kushki_charge_if_needed(self, tx):
        if not tx or tx.provider != "kushki" or tx.state in self.FINAL_STATES:
            return

        intent = request.env["surpay.payment.intent"].sudo().search([("order_id", "=", tx.order_id)], limit=1)
        if not intent:
            return

        provider_response_payload = dict(intent.provider_response_payload or {})
        dispatch_state = str(provider_response_payload.get("dispatch_state") or "queued").lower()
        if intent.provider_payment_id or dispatch_state in {"running", "done"}:
            return

        provider_payload = dict(intent.provider_request_payload or {})
        provider_payload.pop("display_concept", None)
        if not provider_payload:
            provider_response_payload["dispatch_state"] = "failed"
            provider_response_payload["dispatch_error"] = "Missing provider request payload for Kushki dispatch."
            intent.write(
                {
                    "state": "failed",
                    "provider_response_payload": provider_response_payload,
                }
            )
            intent.sync_transaction()
            return

        provider_response_payload["dispatch_state"] = "running"
        provider_response_payload.pop("dispatch_error", None)
        intent.write({"provider_response_payload": provider_response_payload})

        # Persist running state to avoid duplicate dispatch while the terminal call is in flight.
        request.env.cr.commit()

        provider_service = request.env["surpay.kushki.api"].sudo()
        try:
            provider_response = provider_service.create_qr(
                provider_payload,
                provider_config=intent.provider_config_id,
            )
        except Exception as exc:
            failed_payload = dict(intent.provider_response_payload or {})
            failed_payload["dispatch_state"] = "failed"
            failed_payload["dispatch_error"] = str(exc)
            intent.write(
                {
                    "state": "failed",
                    "provider_response_payload": failed_payload,
                }
            )
            intent.sync_transaction()
            return

        provider_order_id = provider_response.get("order_id")
        provider_client_transaction_id = provider_response.get("client_transaction_id") or intent.provider_client_transaction_id
        provider_terminal_serial = provider_response.get("terminal_serial") or intent.provider_terminal_serial
        provider_status = provider_service.extract_status(provider_response)
        provider_message = provider_service.extract_status_message(provider_response)
        mapped_state = provider_service.map_depay_status(provider_status, provider_message)

        merged_payload = dict(intent.provider_response_payload or {})
        merged_payload.update(provider_response or {})
        merged_payload["dispatch_state"] = "done"
        merged_payload.pop("dispatch_error", None)

        intent.write(
            {
                "provider_payment_id": provider_order_id,
                "provider_client_transaction_id": provider_client_transaction_id,
                "provider_terminal_serial": provider_terminal_serial,
                "state": mapped_state,
                "provider_response_payload": merged_payload,
                **provider_service.extract_qr_quote(
                    merged_payload,
                    fallback_currency=intent.currency,
                    fallback_amount=intent.amount,
                ),
            }
        )
        intent.sync_transaction()

    @http.route("/surpay/new-sale", type="http", auth="user", methods=["GET"], csrf=False)
    def new_sale_page(self, **kwargs):
        self._ensure_new_sale_access()
        configs = self._active_provider_configs()
        values = {
            "provider_configs": configs,
            "default_provider_config_id": configs[:1].id if configs else False,
            "qr_from_options": self.QR_FROM_OPTIONS,
            "default_qr_from": "AR",
            "odoo_return_url": self._odoo_return_url(),
            "kushki_terminals_by_config_json": json.dumps(self._kushki_terminals_by_config(configs), ensure_ascii=False),
        }
        return request.render("surpay_base.new_sale_page", values)

    @http.route("/surpay/provider-logo/<int:config_id>", type="http", auth="user", methods=["GET"], csrf=False)
    def provider_logo(self, config_id, **kwargs):
        config = request.env["surpay.provider.config"].sudo().search([
            ("id", "=", config_id),
            ("state", "=", "active"),
        ], limit=1)
        if not config or not config.logo:
            return request.not_found()

        try:
            image_bytes = base64.b64decode(config.logo)
        except Exception:
            _logger.warning("No se pudo decodificar logo para provider config id=%s", config_id)
            return request.not_found()

        headers = [
            ("Content-Type", self._detect_image_content_type(image_bytes)),
            ("Cache-Control", "public, max-age=3600"),
        ]
        return request.make_response(image_bytes, headers=headers)

    @http.route("/surpay/new-sale/start", type="json", auth="user", methods=["POST"], csrf=False)
    def new_sale_start(self):
        self._ensure_new_sale_access()
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
        terminal_id = payload.get("terminal_id")

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

        provider_config = request.env["surpay.provider.config"].sudo().browse(int(provider_config_id))
        if not provider_config.exists() or provider_config.state != "active":
            return {"error": {"message": "La plataforma seleccionada no está activa."}}

        if provider_config.provider == "depay":
            if not qr_from:
                return {"error": {"message": "Selecciona un pais para origen QR."}}
            result = self._create_depay_intent(provider_config, amount_clp, concept, email, qr_from)
        elif provider_config.provider == "kushki":
            if not terminal_id:
                return {"error": {"message": "Selecciona una terminal Kushki."}}
            result = self._create_kushki_intent(provider_config, amount_clp, concept, email, terminal_id)
        else:
            return {
                "error": {
                    "message": f"La plataforma {provider_config.provider.upper()} aún no está implementada en nueva venta.",
                }
            }

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
        self._ensure_new_sale_access()
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
        self._ensure_new_sale_access()
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        if not tx:
            return {"error": {"message": "Transacción no encontrada."}}

        if tx.provider == "kushki" and tx.state not in self.FINAL_STATES:
            self._dispatch_kushki_charge_if_needed(tx)
            tx = request.env["surpay.payment.transaction"].sudo().browse(tx.id)

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
        self._ensure_new_sale_access()
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
        self._ensure_new_sale_access()
        tx = request.env["surpay.payment.transaction"].sudo().search([("order_id", "=", order_id)], limit=1)
        return request.render(
            "surpay_base.new_sale_failed_page",
            {
                "order_id": order_id,
                "state": tx.state if tx else "failed",
                "odoo_return_url": self._odoo_return_url(),
            },
        )

