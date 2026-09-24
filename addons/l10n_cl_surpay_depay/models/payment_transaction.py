import pytz

from odoo import models

MONTHS = [
    "ENERO", "FEBRERO", "MARZO", "ABRIL", "MAYO", "JUNIO",
    "JULIO", "AGOSTO", "SEPTIEMBRE", "OCTUBRE", "NOVIEMBRE", "DICIEMBRE",
]
QR_COUNTRIES = {"AR": "ARGENTINA", "BR": "BRASIL", "PE": "PERÚ"}
COPY_LABELS = {"client": "* ORIGINAL: CLIENTE *", "merchant": "* COPIA: COMERCIO *"}


def _text(text, align="left", bold=False, size="normal"):
    return {"type": "text", "text": text, "align": align, "bold": bold, "size": size}


def _pair(left, right, bold=False, size="normal"):
    return {"type": "pair", "left": left, "right": right, "bold": bold, "size": size}


SEPARATOR = {"type": "separator"}
FEED = {"type": "feed"}


class SurpayPaymentTransaction(models.Model):
    _inherit = "surpay.payment.transaction"

    def _voucher_payload(self):
        if self.provider != "depay":
            return super()._voucher_payload()
        self.ensure_one()
        return {copy: self._depay_voucher_lines(copy) for copy in COPY_LABELS}

    @staticmethod
    def _format_money(amount, decimals=0):
        text = "{:,.{}f}".format(amount or 0, decimals)
        return "$" + text.replace(",", "_").replace(".", ",").replace("_", ".")

    def _depay_voucher_lines(self, copy):
        """Voucher transfronterizo: la comisión es el servicio que cobra Surpay; el monto base es del comercio."""
        self.ensure_one()
        company = self.env.company.sudo()
        partner = company.partner_id
        tz = pytz.timezone(partner.tz or "America/Santiago")
        created_at = pytz.utc.localize(self.create_date).astimezone(tz)
        currency = self.currency or "CLP"
        qr_currency = self.qr_currency or ""
        commission = round(self.commission_amount or 0.0)
        # Por ahora la comisión va exenta de IVA; queda pendiente definir su tratamiento tributario.
        net_amount, vat_amount, taxable_amount, exempt_amount = commission, 0, 0, commission

        lines = [_text((company.name or "").upper(), align="center", bold=True, size="large")]
        if partner.vat:
            lines.append(_text("R.U.T.: {}".format(partner.vat), align="center"))
        if company.surpay_activity:
            lines.append(_text("GIRO: {}".format(company.surpay_activity.upper()), align="center"))
        for address_line in (partner.street, " - ".join(filter(None, [partner.street2, partner.city]))):
            if address_line:
                lines.append(_text(address_line.upper(), align="center"))
        lines.append(SEPARATOR)
        if self.provider_terminal_serial:
            lines.append(_pair("TERMINAL:", self.provider_terminal_serial, bold=True))
        lines += [
            _pair("Nº OPERACIÓN:", self._voucher_operation_number(), bold=True),
            _pair("CÓD. AUTORIZ.:", (self.provider_payment_id or "")[-8:].upper(), bold=True),
            _pair("FECHA:", "{:02d}/{}/{}".format(created_at.day, MONTHS[created_at.month - 1], created_at.year), bold=True),
            _pair("HORA:", created_at.strftime("%H:%M:%S"), bold=True),
            SEPARATOR,
            _pair("MONTO NETO:", self._format_money(net_amount), bold=True),
            _pair("I.V.A. (19%):", self._format_money(vat_amount), bold=True),
            _pair("MONTO AFECTO:", self._format_money(taxable_amount), bold=True),
            _pair("MONTO EXENTO:", self._format_money(exempt_amount), bold=True),
            SEPARATOR,
            _pair("TOTAL COMIS.:", "{} {}".format(self._format_money(commission), currency), bold=True, size="large"),
            SEPARATOR,
            _text("RESUMEN CONSOLIDADO TRANSACCIÓN", align="center", bold=True),
            _pair("TICKET 1 (BASE):", "{} {}".format(self._format_money(self.base_amount), currency)),
            _pair("TICKET 2 (COMISIÓN):", "{} {}".format(self._format_money(commission), currency)),
            SEPARATOR,
            _pair("TOTAL COBRADO {}:".format(currency), self._format_money(self.amount), bold=True),
        ]
        if qr_currency and self.qr_converted_amount:
            country = QR_COUNTRIES.get((self.qr_from or "").upper(), (self.qr_from or "").upper())
            lines += [
                SEPARATOR,
                _text("MONEDA ORIGEN ({})".format(country) if country else "MONEDA ORIGEN", align="center", bold=True),
                _pair("M. ORIGEN TOTAL:", "{} {}".format(self._format_money(self.qr_converted_amount, 2), qr_currency), bold=True),
            ]
            if self.amount:
                rate = self.qr_converted_amount / self.amount
                lines.append(
                    _text(
                        "Ref: 1 {} = {} {} ({})".format(
                            currency,
                            self._format_money(rate, 4).lstrip("$"),
                            qr_currency,
                            created_at.strftime("%d/%m/%Y"),
                        ),
                        align="center",
                        size="small",
                    )
                )
        lines += [
            FEED,
            _text("MULTI-CURRENCY ACQUIRING", align="center", bold=True),
            _text(COPY_LABELS[copy], align="center"),
        ]
        return lines
