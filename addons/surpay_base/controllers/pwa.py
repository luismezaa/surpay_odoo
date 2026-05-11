import json

from odoo import http
from odoo.http import request


class SurpayPwaController(http.Controller):
    APP_NAME = "Surpay"
    THEME_COLOR = "#0b5394"
    BACKGROUND_COLOR = "#ffffff"
    PNG_ICON_PATH = "/surpay_base/static/description/src/img/surpay_512x512.png"
    SVG_ICON_PATH = "/surpay_base/static/description/src/img/surpay.svg"

    @http.route("/surpay/manifest.webmanifest", type="http", auth="public", sitemap=False)
    def manifest(self, **kwargs):
        payload = {
            "name": self.APP_NAME,
            "short_name": self.APP_NAME,
            "start_url": "/web",
            "scope": "/",
            "display": "standalone",
            "background_color": self.BACKGROUND_COLOR,
            "theme_color": self.THEME_COLOR,
            "icons": [
                {
                    "src": self.PNG_ICON_PATH,
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": self.SVG_ICON_PATH,
                    "sizes": "512x512",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
            ],
        }
        body = json.dumps(payload)
        headers = [("Content-Type", "application/manifest+json")]
        return request.make_response(body, headers=headers)

    @http.route("/surpay/pwa-icon/<int:size>", type="http", auth="public", sitemap=False)
    def pwa_icon(self, size=192, **kwargs):
        return request.redirect(self.PNG_ICON_PATH, code=302)