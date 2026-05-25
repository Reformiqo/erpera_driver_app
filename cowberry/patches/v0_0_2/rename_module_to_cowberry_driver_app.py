import frappe


OLD_MODULE = "Cowberry App"
NEW_MODULE = "Cowberry Driver App"

OUR_DOCTYPES = [
    "Cowberry Cash Submission",
    "Cowberry Driver Collection",
    "Cowberry Driver Settings",
    "Cowberry OTP Log",
    "Cowberry Reschedule Log",
    "Cowberry Wallet Transaction",
    "Cowberry Delivery Attempt Log",
    "Cowberry Delivery Sync Log",
    "Cowberry Delivery Sync Step",
    "Cowberry Order Sync Log",
    "Cowberry Reverse Logistics Settings",
    "CCD Order Item",
]


def execute():
    """Move this app's DocTypes and Custom Fields from the legacy `Cowberry App`
    Module Def onto the new `Cowberry Driver App` Module Def.

    The old name collided with another installed app (`cowberry_app`) that also
    declared `Cowberry App` in its `modules.txt`, so Frappe could resolve a
    given doctype to the wrong app's package and ImportError during migrate.
    This patch is idempotent — re-running it is a no-op.
    """
    if not frappe.db.exists("Module Def", NEW_MODULE):
        frappe.get_doc(
            {
                "doctype": "Module Def",
                "module_name": NEW_MODULE,
                "app_name": "cowberry",
            }
        ).insert(ignore_permissions=True)

    for dt in OUR_DOCTYPES:
        if not frappe.db.exists("DocType", dt):
            continue
        if frappe.db.get_value("DocType", dt, "module") == OLD_MODULE:
            frappe.db.set_value(
                "DocType", dt, "module", NEW_MODULE, update_modified=False
            )

    custom_fields = frappe.get_all(
        "Custom Field",
        filters={"module": OLD_MODULE},
        fields=["name", "dt", "fieldname"],
    )
    for cf in custom_fields:
        is_ours = cf.dt in OUR_DOCTYPES or (cf.fieldname or "").startswith("cowberry_")
        if is_ours:
            frappe.db.set_value(
                "Custom Field", cf.name, "module", NEW_MODULE, update_modified=False
            )

    frappe.db.commit()
