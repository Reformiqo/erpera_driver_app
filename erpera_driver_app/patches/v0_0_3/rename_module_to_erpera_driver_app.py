import frappe

# App renamed cowberry -> erpera_driver_app; module renamed to "Erpera Driver App".
NEW_MODULE = "Erpera Driver App"
LEGACY_MODULES = ["Cowberry App", "Cowberry Driver App"]
APP_NAME = "erpera_driver_app"

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
    """Move this app's DocTypes + Custom Fields onto the `Erpera Driver App`
    Module Def (app renamed from `cowberry` to `erpera_driver_app`). Idempotent.
    """
    if not frappe.db.exists("Module Def", NEW_MODULE):
        frappe.get_doc({
            "doctype": "Module Def",
            "module_name": NEW_MODULE,
            "app_name": APP_NAME,
        }).insert(ignore_permissions=True)
    else:
        frappe.db.set_value("Module Def", NEW_MODULE, "app_name", APP_NAME,
                            update_modified=False)

    for dt in OUR_DOCTYPES:
        if frappe.db.exists("DocType", dt):
            if frappe.db.get_value("DocType", dt, "module") in LEGACY_MODULES:
                frappe.db.set_value("DocType", dt, "module", NEW_MODULE,
                                    update_modified=False)

    for cf in frappe.get_all("Custom Field",
                             filters={"module": ["in", LEGACY_MODULES]},
                             fields=["name", "dt", "fieldname"]):
        if cf.dt in OUR_DOCTYPES or (cf.fieldname or "").startswith("cowberry_"):
            frappe.db.set_value("Custom Field", cf.name, "module", NEW_MODULE,
                                update_modified=False)

    frappe.db.commit()
