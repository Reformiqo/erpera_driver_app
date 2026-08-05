import frappe
from frappe import _

def validate(self, method):
    validate_source_warehouse(self)
    set_warehouse_manager_mobile(self)

def validate_source_warehouse(self):
    if not self.source_warehouse:
        frappe.throw(_("Please select Source Warehouse before saving the Delivery Trip."))

def set_warehouse_manager_mobile(self):

    warehouse_manager = frappe.db.get_value(
        "Warehouse",
        self.source_warehouse,
        "custom_warehouse_manager",
    )

    if not warehouse_manager:
        frappe.throw(
            _("Please set Warehouse Manager in Warehouse {0}.")
            .format(self.source_warehouse)
        )

    mobile = frappe.db.get_value("User", warehouse_manager, "mobile_no")

    if not mobile:
        mobile = frappe.db.get_value("User", warehouse_manager, "phone")

    if not mobile:
        frappe.throw(
            _("Please set Mobile No or Phone for User {0}.").format(warehouse_manager)
        )


    frappe.db.set_value("Warehouse", self.source_warehouse, "warehouse_manager_mobile", mobile)
    frappe.db.commit()