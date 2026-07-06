import json
from collections import OrderedDict

import pytz

from odoo import _, fields, models


class SurpayCashClosureReportService(models.AbstractModel):
    _name = "surpay.cash.closure.report.service"
    _description = "Servicio compartido de reportes de cierre de caja"

    @staticmethod
    def _safe_text(value):
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _truncate_text(text, max_len=24):
        value = SurpayCashClosureReportService._safe_text(text)
        if len(value) <= max_len:
            return value
        return f"{value[:max_len - 3]}..."

    def _format_amount(self, amount):
        return "${}".format("{:,.0f}".format(amount or 0).replace(",", "."))

    def _format_datetime(self, value):
        if not value:
            return ""
        localized = fields.Datetime.context_timestamp(self, value)
        return localized.strftime("%d-%m-%Y %H:%M")

    def _extract_extra_data_fields(self, provider_raw):
        payload = provider_raw
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return []

        if not isinstance(payload, dict):
            return []

        extra_data = payload.get("extra_data")
        if not isinstance(extra_data, dict):
            return []

        fields_list = extra_data.get("extra_data_fields")
        if not isinstance(fields_list, list):
            return []

        normalized = []
        for item in fields_list:
            if not isinstance(item, dict):
                continue
            title = self._safe_text(item.get("title"))
            value = self._safe_text(item.get("value"))
            if not title or not value:
                continue
            normalized.append({"title": title, "value": value})

        return normalized

    def _format_extra_data_pdf(self, provider_raw):
        fields_list = self._extract_extra_data_fields(provider_raw)
        if not fields_list:
            return ""

        if len(fields_list) == 1:
            item = fields_list[0]
            return f"{item['title']}: {item['value']}"

        visible_items = fields_list[:2]
        parts = [
            f"{item['title']}: {self._truncate_text(item['value'], 20)}"
            for item in visible_items
        ]
        hidden = len(fields_list) - len(visible_items)
        if hidden > 0:
            parts.append(f"+{hidden} mas")
        return " | ".join(parts)

    def _format_extra_data_excel(self, provider_raw):
        fields_list = self._extract_extra_data_fields(provider_raw)
        if not fields_list:
            return ""
        return " | ".join([f"{item['title']}: {item['value']}" for item in fields_list])

    def _build_cash_closure_sections(self, report_date=None, users=None, closure_ids=None, group_mode="user"):
        """
        Metodo unico para construir secciones de reportes de cierre.

        group_mode='user': una seccion por usuario (modo reporte diario original).
        group_mode='closure': una seccion por cierre (modo conciliacion).
        """
        if group_mode not in {"user", "closure"}:
            raise ValueError("group_mode debe ser 'user' o 'closure'.")

        closure_model = self.env["surpay.cash.closure"]
        tx_model = self.env["surpay.payment.transaction"]
        closure_state_labels = dict(closure_model._fields["state"].selection)
        tx_state_labels = dict(tx_model._fields["state"].selection)
        paid_label = tx_state_labels.get("paid", "Pagado")

        tz_name = self.env.user.tz or "UTC"
        user_tz = pytz.timezone(tz_name)
        utc_tz = pytz.utc

        query_params = []
        where_clause = ""
        users_recordset = self.env["res.users"]

        if closure_ids is not None:
            if hasattr(closure_ids, "ids"):
                closure_ids = closure_ids.ids
            closure_ids = sorted({int(cid) for cid in (closure_ids or []) if cid})
            if not closure_ids:
                return []
            where_clause = "sc.id = ANY(%s)"
            query_params = [closure_ids]
        else:
            if users is None:
                return []
            users_recordset = users if hasattr(users, "ids") else self.env["res.users"].browse(users)
            user_ids = users_recordset.ids
            if not user_ids:
                return []
            where_clause = "sc.closure_date = %s AND sc.seller_user_id = ANY(%s)"
            query_params = [report_date, user_ids]

        self.env.cr.execute(
            f"""
                SELECT
                    sc.id                                        AS closure_id,
                    sc.name                                      AS closure_name,
                    sc.state                                     AS closure_state,
                    sc.closure_date                              AS closure_date,
                    sc.seller_user_id,
                    st.id                                        AS tx_id,
                    st.order_id,
                    COALESCE(st.create_date, st.write_date)      AS tx_date,
                    COALESCE(st.amount, 0)                       AS amount,
                    COALESCE(st.base_amount, 0)                  AS base_amount,
                    COALESCE(st.commission_amount, 0)            AS commission_amount,
                    st.concept,
                    st.provider_raw
                FROM surpay_cash_closure sc
                LEFT JOIN surpay_payment_transaction st
                    ON st.cash_closure_id = sc.id
                    AND st.state = 'paid'
                WHERE {where_clause}
                ORDER BY sc.seller_user_id, sc.closure_date, sc.id,
                         COALESCE(st.create_date, st.write_date) NULLS LAST,
                         st.id NULLS LAST
            """,
            query_params,
        )
        rows = self.env.cr.fetchall()
        col_names = [d[0] for d in self.env.cr.description]

        closures = OrderedDict()
        user_ids_seen = set(users_recordset.ids)

        for row in rows:
            r = dict(zip(col_names, row))
            cid = r["closure_id"]
            uid = r["seller_user_id"]
            user_ids_seen.add(uid)

            if cid not in closures:
                closures[cid] = {
                    "closure_id": cid,
                    "closure_name": r["closure_name"] or "",
                    "closure_state": r["closure_state"] or "",
                    "closure_date": r["closure_date"],
                    "seller_user_id": uid,
                    "txs": [],
                }

            if r["tx_id"]:
                tx_date = r["tx_date"]
                if tx_date is not None:
                    if tx_date.tzinfo is None:
                        tx_date = utc_tz.localize(tx_date)
                    tx_date_str = tx_date.astimezone(user_tz).strftime("%d-%m-%Y %H:%M")
                else:
                    tx_date_str = ""

                closures[cid]["txs"].append(
                    {
                        "order_id": r["order_id"] or "",
                        "datetime": tx_date_str,
                        "amount": r["amount"],
                        "base_amount": r["base_amount"],
                        "commission_amount": r["commission_amount"],
                        "concept": r["concept"] or "",
                        "provider_raw": r["provider_raw"],
                    }
                )

        users_map = self.env["res.users"].browse([uid for uid in user_ids_seen if uid]).exists()
        users_by_id = {u.id: u for u in users_map}

        def _build_section(closure_rows, user_record):
            closure_names = ", ".join(c["closure_name"] for c in closure_rows)
            closure_states_set = {
                closure_state_labels.get(c["closure_state"], c["closure_state"]) for c in closure_rows
            }
            closure_states = ", ".join(sorted(closure_states_set)) or _("Sin cierres")

            all_txs = []
            for c in closure_rows:
                all_txs.extend(c["txs"])

            gross_amount = sum(t["amount"] for t in all_txs)
            net_amount = sum(t["base_amount"] for t in all_txs)
            commission_amount = sum(t["commission_amount"] for t in all_txs)
            reconciled_amount = gross_amount - commission_amount
            commission_percent = (commission_amount / net_amount * 100.0) if net_amount else 0.0

            transaction_lines = [
                {
                    "order_id": t["order_id"],
                    "datetime": t["datetime"],
                    "amount_display": self._format_amount(t["amount"]),
                    "state_label": paid_label,
                    "concept": t["concept"],
                    "extra_data_pdf": self._format_extra_data_pdf(t["provider_raw"]),
                    "extra_data_excel": self._format_extra_data_excel(t["provider_raw"]),
                }
                for t in all_txs
            ]

            closure_ids = [c["closure_id"] for c in closure_rows]
            return {
                "user": user_record,
                "closure_id": closure_ids[0] if len(closure_ids) == 1 else False,
                "closure_ids": closure_ids,
                "closure_count": len(closure_rows),
                "closure_names": closure_names,
                "closure_states": closure_states,
                "transaction_count": len(all_txs),
                "gross_amount_display": self._format_amount(gross_amount),
                "commission_percent_display": "{:.2f}%".format(commission_percent),
                "commission_amount_display": self._format_amount(commission_amount),
                "net_amount_display": self._format_amount(net_amount),
                "financial_total_display": self._format_amount(reconciled_amount),
                "transactions": transaction_lines,
                "has_data": bool(closure_rows),
            }

        if group_mode == "closure":
            sections = []
            for closure in closures.values():
                user_record = users_by_id.get(closure["seller_user_id"], self.env.user)
                sections.append(_build_section([closure], user_record))
            return sections

        # group_mode == 'user'
        if users_recordset:
            users_order = users_recordset
        else:
            users_order = self.env["res.users"].browse([uid for uid in user_ids_seen if uid])

        closures_by_user = {}
        for closure in closures.values():
            uid = closure["seller_user_id"]
            closures_by_user.setdefault(uid, []).append(closure)

        sections = []
        for user in users_order:
            sections.append(_build_section(closures_by_user.get(user.id, []), user))
        return sections
