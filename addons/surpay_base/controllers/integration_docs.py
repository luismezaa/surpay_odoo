from odoo import http
from odoo.http import request


class SurpayIntegrationDocsController(http.Controller):
    @http.route("/surpay/docs/external-api", type="http", auth="public", website=False, sitemap=False)
    def external_api_docs(self, **kwargs):
        providers = request.env["surpay.provider.config"].PROVIDERS
        values = {
            "providers": providers,
            "qr_from_values": ["AR", "BR", "PE"],
            "return_behaviors": [
                ("webhook_only", "Solo webhook"),
                ("odoo_final_screen", "Pantalla final en servidor"),
                ("auto_redirect", "Redireccion automatica al comercio"),
            ],
        }
        return request.render("surpay_base.integration_docs_page", values)
