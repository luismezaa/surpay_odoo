import hashlib
import secrets
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SurpayMobileApiSession(models.Model):
    _name = "surpay.mobile.api.session"
    _description = "Sesion movil para acceso API Surpay"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade")
    api_client_id = fields.Many2one("surpay.api.client", required=True, index=True, ondelete="restrict")
    device_id = fields.Char(index=True)
    terminal_serial = fields.Char(index=True)
    source_ip = fields.Char()
    user_agent = fields.Char()

    access_token_hash = fields.Char(required=True, index=True)
    refresh_token_hash = fields.Char(required=True, index=True)
    expires_at = fields.Datetime(required=True, index=True)
    refresh_expires_at = fields.Datetime(required=True, index=True)

    revoked = fields.Boolean(default=False, index=True)
    revoked_at = fields.Datetime()
    revoked_reason = fields.Char()
    last_seen_at = fields.Datetime()

    _sql_constraints = [
        ("surpay_mobile_access_token_hash_uniq", "unique(access_token_hash)", "access_token_hash must be unique."),
        ("surpay_mobile_refresh_token_hash_uniq", "unique(refresh_token_hash)", "refresh_token_hash must be unique."),
    ]

    @api.model
    def _token_hash(self, token):
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    @api.model
    def _new_token(self):
        return secrets.token_urlsafe(48)

    @api.model
    def _access_ttl_seconds(self):
        return int(self.env["ir.config_parameter"].sudo().get_param("surpay_mobile.access_ttl_seconds", "3600"))

    @api.model
    def _refresh_ttl_seconds(self):
        return int(self.env["ir.config_parameter"].sudo().get_param("surpay_mobile.refresh_ttl_seconds", "604800"))

    @api.model
    def issue_session(self, user, api_client, device_id="", terminal_serial="", source_ip="", user_agent=""):
        now = fields.Datetime.now()
        access_token = self._new_token()
        refresh_token = self._new_token()

        rec = self.create(
            {
                "user_id": user.id,
                "api_client_id": api_client.id,
                "device_id": (device_id or "").strip(),
                "terminal_serial": (terminal_serial or "").strip().upper(),
                "source_ip": source_ip or "",
                "user_agent": (user_agent or "")[:512],
                "access_token_hash": self._token_hash(access_token),
                "refresh_token_hash": self._token_hash(refresh_token),
                "expires_at": now + timedelta(seconds=self._access_ttl_seconds()),
                "refresh_expires_at": now + timedelta(seconds=self._refresh_ttl_seconds()),
                "last_seen_at": now,
            }
        )
        return rec, access_token, refresh_token

    @api.model
    def resolve_access_token(self, token):
        token_hash = self._token_hash(token)
        now = fields.Datetime.now()
        rec = self.search(
            [
                ("access_token_hash", "=", token_hash),
                ("revoked", "=", False),
                ("expires_at", ">", now),
            ],
            limit=1,
        )
        return rec

    def revoke(self, reason="logout"):
        now = fields.Datetime.now()
        self.write(
            {
                "revoked": True,
                "revoked_at": now,
                "revoked_reason": reason,
            }
        )

    @api.model
    def refresh_session(self, refresh_token, device_id="", source_ip="", user_agent=""):
        token_hash = self._token_hash(refresh_token)
        now = fields.Datetime.now()
        rec = self.search(
            [
                ("refresh_token_hash", "=", token_hash),
                ("revoked", "=", False),
                ("refresh_expires_at", ">", now),
            ],
            limit=1,
        )
        if not rec:
            raise ValidationError("refresh_token_invalid")

        input_device = (device_id or "").strip()
        if rec.device_id and input_device and rec.device_id != input_device:
            raise ValidationError("device_mismatch")

        new_access_token = self._new_token()
        new_refresh_token = self._new_token()
        rec.write(
            {
                "access_token_hash": self._token_hash(new_access_token),
                "refresh_token_hash": self._token_hash(new_refresh_token),
                "expires_at": now + timedelta(seconds=self._access_ttl_seconds()),
                "refresh_expires_at": now + timedelta(seconds=self._refresh_ttl_seconds()),
                "source_ip": source_ip or rec.source_ip,
                "user_agent": (user_agent or rec.user_agent or "")[:512],
                "last_seen_at": now,
            }
        )
        return rec, new_access_token, new_refresh_token