from odoo import models


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    def load_web_menus(self, debug):
        menus = super().load_web_menus(debug)

        user = self.env.user
        if not user.has_group("surpay_base.group_surpay_restricted_user"):
            return menus
        if user.has_group("base.group_system"):
            return menus

        surpay_root = self.env.ref("surpay_base.surpay_menu_root", raise_if_not_found=False)
        if not surpay_root:
            return menus

        # Collect surpay_root and all its descendants
        allowed_ids = set()

        def collect(menu_id):
            allowed_ids.add(menu_id)
            item = menus.get(menu_id)
            if item:
                for child_id in item.get("children", []):
                    collect(child_id)

        collect(surpay_root.id)

        # Filter menus dict keeping only surpay branch + "root"
        filtered = {k: v for k, v in menus.items() if k in allowed_ids or k == "root"}

        # Patch root's children list
        if "root" in filtered:
            filtered["root"] = dict(filtered["root"])
            filtered["root"]["children"] = [
                c for c in filtered["root"].get("children", []) if c in allowed_ids
            ]

        return filtered
