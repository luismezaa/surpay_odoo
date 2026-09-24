from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    surpay_activity = fields.Char(
        string="Giro",
        help="Giro impreso en el encabezado de los vouchers de la APK.",
    )
