import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class SurpayMobileAuthController(http.Controller):
    @staticmethod
    def _normalize_uid(auth_result):
        if isinstance(auth_result, int):
            return auth_result
        if isinstance(auth_result, str):
            return int(auth_result) if auth_result.isdigit() else False
        if isinstance(auth_result, dict):
            uid_value = auth_result.get("uid")
            if isinstance(uid_value, int):
                return uid_value
            if isinstance(uid_value, str) and uid_value.isdigit():
                return int(uid_value)
            return False
        if isinstance(auth_result, (list, tuple)) and auth_result:
            first = auth_result[0]
            if isinstance(first, int):
                return first
            if isinstance(first, str) and first.isdigit():
                return int(first)
        return False

    @staticmethod
    def _json_error(status_code, code, message):
        return request.make_json_response(
            {"error": {"code": code, "message": message}},
            status=status_code,
        )

    @staticmethod
    def _raw_body():
        return request.httprequest.get_data(as_text=False)

    def _parse_payload(self):
        raw_body = self._raw_body() or b"{}"
        try:
            return json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            return None

    @staticmethod
    def _client_ip():
        xff = request.httprequest.headers.get("X-Forwarded-For")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
        return request.httprequest.remote_addr

    @staticmethod
    def _resolve_api_client_for_user(user):
        """Cliente API del comercio del usuario. Sin asociación explícita no hay cliente."""
        client_model = request.env["surpay.api.client"].sudo()
        partner = user.partner_id.commercial_partner_id
        if not partner:
            return client_model.browse()
        return client_model.search(
            [
                ("active", "=", True),
                ("partner_id", "child_of", partner.id),
            ],
            order="id asc",
            limit=1,
        )

    def _validate_terminal(self, api_client, terminal_serial):
        """Retorna el mensaje de error si el terminal no está habilitado para el cliente, o None."""
        provider_config = api_client.resolve_provider_config_for_provider("kushki")
        if not provider_config:
            return "No active Kushki configuration for this client."
        try:
            provider_config.resolve_kushki_terminal(
                partner=api_client.partner_id,
                terminal_serial=terminal_serial,
            )
        except Exception as exc:
            return str(exc)
        return None

    @staticmethod
    def _session_response(session_rec, access_token, refresh_token):
        api_client = session_rec.api_client_id.sudo()
        base_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url", "")
        return request.make_json_response(
            {
                "token_type": "bearer",
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": session_rec.expires_at,
                "refresh_expires_at": session_rec.refresh_expires_at,
                "provider": "kushki",
                "currency": "CLP",
                "terminal_serial": session_rec.terminal_serial,
                "merchant_name": api_client.partner_id.commercial_partner_id.name or api_client.name,
                "user_name": session_rec.user_id.name,
                "api_credentials": {
                    "client_id": api_client.client_id,
                    "client_secret": api_client.client_secret,
                    "base_url": base_url,
                },
            },
            status=200,
        )

    @staticmethod
    def _extract_bearer_token():
        header = request.httprequest.headers.get("Authorization") or ""
        if not header:
            return ""
        parts = header.strip().split(" ")
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return ""

    @http.route("/api/v1/mobile/auth/login", type="http", auth="public", methods=["POST"], csrf=False)
    def mobile_login(self):
        payload = self._parse_payload()
        if payload is None:
            _logger.warning("mobile_login invalid_payload ip=%s", self._client_ip())
            return self._json_error(400, "invalid_payload", "Request body must be valid JSON.")

        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        device_id = str(payload.get("device_id") or "").strip()
        terminal_serial = str(payload.get("terminal_serial") or "").strip().upper()
        _logger.info(
            "mobile_login attempt user=%s device_id=%s terminal_serial=%s ip=%s",
            username,
            device_id,
            terminal_serial,
            self._client_ip(),
        )
        if not username or not password:
            _logger.warning("mobile_login missing_credentials user=%s ip=%s", username, self._client_ip())
            return self._json_error(400, "missing_credentials", "username and password are required.")
        if not terminal_serial:
            _logger.warning("mobile_login missing_terminal_serial user=%s ip=%s", username, self._client_ip())
            return self._json_error(400, "missing_terminal_serial", "terminal_serial is required.")

        dbname = request.env.cr.dbname
        uid = False
        try:
            credentials = {
                "login": username,
                "password": password,
                "type": "password",
            }
            auth_result = request.session.authenticate(dbname, credentials)
            uid = self._normalize_uid(auth_result)
            _logger.info("mobile_login authenticate result_type=%s normalized_uid=%s", type(auth_result).__name__, uid)
        except TypeError:
            # Backward compatibility for older session signature.
            try:
                auth_result = request.session.authenticate(dbname, username, password)
                uid = self._normalize_uid(auth_result)
                _logger.info("mobile_login authenticate legacy result_type=%s normalized_uid=%s", type(auth_result).__name__, uid)
            except Exception as exc:
                _logger.exception("mobile_login authenticate failed (legacy signature) user=%s ip=%s error=%s", username, self._client_ip(), exc)
                uid = False
        except Exception as exc:
            _logger.exception("mobile_login authenticate failed user=%s ip=%s error=%s", username, self._client_ip(), exc)
            uid = False

        if not uid:
            _logger.warning("mobile_login invalid_credentials user=%s ip=%s", username, self._client_ip())
            return self._json_error(401, "invalid_credentials", "Invalid username or password.")

        user = request.env["res.users"].sudo().browse(uid)
        if not user.exists() or not user.active:
            _logger.warning("mobile_login inactive_user uid=%s user=%s ip=%s", uid, username, self._client_ip())
            return self._json_error(403, "inactive_user", "User is not active.")

        api_client = self._resolve_api_client_for_user(user)
        if not api_client:
            _logger.warning("mobile_login client_not_provisioned user=%s ip=%s", username, self._client_ip())
            return self._json_error(403, "client_not_provisioned", "No API client configured for this user.")

        terminal_error = self._validate_terminal(api_client, terminal_serial)
        if terminal_error:
            _logger.warning(
                "mobile_login terminal_not_allowed user=%s client_id=%s terminal_serial=%s ip=%s reason=%s",
                username,
                api_client.client_id,
                terminal_serial,
                self._client_ip(),
                terminal_error,
            )
            return self._json_error(403, "terminal_not_allowed", terminal_error)

        session_model = request.env["surpay.mobile.api.session"].sudo()
        session_rec, access_token, refresh_token = session_model.issue_session(
            user=user,
            api_client=api_client,
            device_id=device_id,
            terminal_serial=terminal_serial,
            source_ip=self._client_ip() or "",
            user_agent=request.httprequest.headers.get("User-Agent") or "",
        )
        _logger.info(
            "mobile_login success user=%s client_id=%s session_id=%s terminal_serial=%s ip=%s",
            username,
            api_client.client_id,
            session_rec.id,
            terminal_serial,
            self._client_ip(),
        )
        return self._session_response(session_rec, access_token, refresh_token)

    @http.route("/api/v1/mobile/auth/refresh", type="http", auth="public", methods=["POST"], csrf=False)
    def mobile_refresh(self):
        payload = self._parse_payload()
        if payload is None:
            _logger.warning("mobile_refresh invalid_payload ip=%s", self._client_ip())
            return self._json_error(400, "invalid_payload", "Request body must be valid JSON.")

        refresh_token = str(payload.get("refresh_token") or "").strip()
        device_id = str(payload.get("device_id") or "").strip()
        _logger.info("mobile_refresh attempt device_id=%s ip=%s", device_id, self._client_ip())
        if not refresh_token:
            _logger.warning("mobile_refresh missing_refresh_token ip=%s", self._client_ip())
            return self._json_error(400, "missing_refresh_token", "refresh_token is required.")

        session_model = request.env["surpay.mobile.api.session"].sudo()
        try:
            session_rec, access_token, new_refresh_token = session_model.refresh_session(
                refresh_token=refresh_token,
                device_id=device_id,
                source_ip=self._client_ip() or "",
                user_agent=request.httprequest.headers.get("User-Agent") or "",
            )
        except Exception as exc:
            _logger.warning("mobile refresh failed: %s", exc)
            return self._json_error(401, "invalid_refresh_token", "refresh token is invalid or expired.")

        # La sesión solo sigue viva si el usuario, su cliente API y el terminal siguen habilitados.
        user = session_rec.user_id
        api_client = session_rec.api_client_id
        revoke_reason = None
        if not user.active:
            revoke_reason = "inactive_user"
        elif not api_client.active or self._resolve_api_client_for_user(user) != api_client:
            revoke_reason = "client_not_provisioned"
        elif session_rec.terminal_serial and self._validate_terminal(api_client, session_rec.terminal_serial):
            revoke_reason = "terminal_not_allowed"
        if revoke_reason:
            session_rec.revoke(reason=revoke_reason)
            _logger.warning("mobile_refresh revoked session_id=%s reason=%s ip=%s", session_rec.id, revoke_reason, self._client_ip())
            return self._json_error(401, revoke_reason, "Mobile session is no longer valid.")

        _logger.info(
            "mobile_refresh success session_id=%s client_id=%s device_id=%s ip=%s",
            session_rec.id,
            api_client.client_id,
            device_id,
            self._client_ip(),
        )
        return self._session_response(session_rec, access_token, new_refresh_token)

    @http.route("/api/v1/mobile/auth/logout", type="http", auth="public", methods=["POST"], csrf=False)
    def mobile_logout(self):
        payload = self._parse_payload()
        if payload is None:
            _logger.warning("mobile_logout invalid_payload ip=%s", self._client_ip())
            return self._json_error(400, "invalid_payload", "Request body must be valid JSON.")

        access_token = self._extract_bearer_token() or str(payload.get("access_token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        _logger.info(
            "mobile_logout attempt has_access=%s has_refresh=%s ip=%s",
            bool(access_token),
            bool(refresh_token),
            self._client_ip(),
        )
        if not access_token and not refresh_token:
            _logger.warning("mobile_logout missing_token ip=%s", self._client_ip())
            return self._json_error(400, "missing_token", "access_token or refresh_token is required.")

        session_model = request.env["surpay.mobile.api.session"].sudo()
        session_rec = session_model.browse()
        if access_token:
            session_rec = session_model.resolve_access_token(access_token)
        if not session_rec and refresh_token:
            refresh_hash = session_model._token_hash(refresh_token)
            session_rec = session_model.search([("refresh_token_hash", "=", refresh_hash), ("revoked", "=", False)], limit=1)

        if session_rec:
            session_rec.revoke(reason="logout")
            _logger.info("mobile_logout success session_id=%s ip=%s", session_rec.id, self._client_ip())
        else:
            _logger.info("mobile_logout no_session_found ip=%s", self._client_ip())

        return request.make_json_response({"status": "ok"}, status=200)