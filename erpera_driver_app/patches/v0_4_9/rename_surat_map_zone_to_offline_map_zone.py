"""Rename the Surat Map Zone child doctype to Offline Map Zone.

The doctype was named after the first city it was set up for, but it holds
the offline-working zones for any driver anywhere. Renaming it moves the
table and the rows in it; without this patch a site that pulls the new code
would create an empty `tabOffline Map Zone` alongside the populated
`tabSurat Map Zone`, and every Employee's zones would silently disappear
from the form.

Safe to re-run: it no-ops once the new name exists.
"""

import frappe
from frappe.model.rename_doc import rename_doc

OLD = "Surat Map Zone"
NEW = "Offline Map Zone"
# The Employee field keeps its fieldname — Flutter and the profile API read
# it — but its `options` has to point at the new doctype.
EMPLOYEE_FIELD = "Employee-custom_surat_map_zone"


def execute():
	if frappe.db.exists("DocType", NEW) or not frappe.db.exists("DocType", OLD):
		return

	rename_doc("DocType", OLD, NEW, force=True, ignore_permissions=True)

	# Set explicitly rather than trusting the rename to walk Table options:
	# if this is left pointing at the old name the grid renders empty.
	if frappe.db.exists("Custom Field", EMPLOYEE_FIELD):
		frappe.db.set_value("Custom Field", EMPLOYEE_FIELD, "options", NEW)

	frappe.clear_cache()
