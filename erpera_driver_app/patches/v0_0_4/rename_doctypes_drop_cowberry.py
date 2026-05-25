import frappe

# Drop the "Cowberry " prefix from this app's doctypes so they no longer
# collide with the separate `cowberry_app` ERPNext app (which keeps its own
# "Cowberry Driver Collection" etc.). Runs PRE model sync so the rename
# happens before sync_all recreates the new-named doctypes.
MODULE = "Erpera Driver App"
RENAMES = [
    ("Cowberry Cash Submission", "Cash Submission"),
    ("Cowberry Driver Collection", "Driver Collection"),
    ("Cowberry Driver Settings", "Driver Settings"),
    ("Cowberry OTP Log", "OTP Log"),
    ("Cowberry Reschedule Log", "Reschedule Log"),
    ("Cowberry Wallet Transaction", "Wallet Transaction"),
    ("Cowberry Delivery Attempt Log", "Delivery Attempt Log"),
    ("Cowberry Delivery Sync Log", "Delivery Sync Log"),
    ("Cowberry Delivery Sync Step", "Delivery Sync Step"),
    ("Cowberry Order Sync Log", "Order Sync Log"),
    ("Cowberry Reverse Logistics Settings", "Reverse Logistics Settings"),
]


def execute():
    for old, new in RENAMES:
        if frappe.db.exists("DocType", old) and not frappe.db.exists("DocType", new):
            frappe.rename_doc("DocType", old, new, force=True)
        if frappe.db.exists("DocType", new):
            frappe.db.set_value("DocType", new, "module", MODULE, update_modified=False)
    frappe.db.commit()
