import frappe


def after_install():
    """Seed singleton settings docs if they don't already exist."""
    _seed_driver_settings()
    _seed_reverse_logistics_settings()
    _seed_roles()


def _seed_driver_settings():
    if not frappe.db.exists("Cowberry Driver Settings", "Cowberry Driver Settings"):
        doc = frappe.new_doc("Cowberry Driver Settings")
        doc.otp_validity_pod = 10
        doc.otp_validity_cash_submission = 10
        doc.otp_validity_wallet = 10
        doc.otp_validity_driver_login = 10
        doc.otp_max_attempts = 5
        doc.insert(ignore_permissions=True)
        frappe.db.commit()


def _seed_reverse_logistics_settings():
    if not frappe.db.exists(
        "Cowberry Reverse Logistics Settings", "Cowberry Reverse Logistics Settings"
    ):
        doc = frappe.new_doc("Cowberry Reverse Logistics Settings")
        doc.enable_reverse_logistics = 0
        doc.max_return_days = 7
        doc.insert(ignore_permissions=True)
        frappe.db.commit()


def _seed_roles():
    for role_name in ["Driver", "Delivery Manager", "Delivery User"]:
        if not frappe.db.exists("Role", role_name):
            role = frappe.new_doc("Role")
            role.role_name = role_name
            role.insert(ignore_permissions=True)
    frappe.db.commit()
