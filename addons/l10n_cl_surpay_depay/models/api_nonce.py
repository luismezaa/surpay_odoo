from datetime import timedelta

from odoo import api, fields, models


class SurpayApiNonce(models.Model):
    _name = "surpay.api.nonce"
    _description = "Surpay API Nonce"

    client_id = fields.Many2one("surpay.api.client", required=True, ondelete="cascade", index=True)
    nonce = fields.Char(required=True)
    expires_at = fields.Datetime(required=True, index=True)

    _sql_constraints = [
        ("surpay_nonce_client_uniq", "unique(client_id, nonce)", "Nonce already used for this client."),
    ]

    @api.model
    def cleanup_expired(self):
        now = fields.Datetime.now()
        self.search([("expires_at", "<", now)]).unlink()

    @api.model
    def register_nonce(self, client, nonce, ttl_seconds=300):
        self.cleanup_expired()
        expires_at = fields.Datetime.now() + timedelta(seconds=ttl_seconds)
        return self.create(
            {
                "client_id": client.id,
                "nonce": nonce,
                "expires_at": expires_at,
            }
        )
