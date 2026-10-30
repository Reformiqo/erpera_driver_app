"""Write Driver Settings.require_pod_otp = 1 on sites that predate the field.

Driver Settings is a Single, so its row in `tabSingles` already exists on
every live site. Adding a field with `"default": "1"` to the DocType JSON
does not write anything into that existing row — a DocType default applies
when a document is created, and this one was created long ago. So after a
plain `bench migrate` the desk renders the box ticked (from the field
default) while `get_single_value` returns None.

Nothing breaks, because `utils.otp.pod_otp_required` treats None as ON for
exactly this reason. This patch closes the gap the other way: it writes the
value so the stored configuration says what the code enforces, and an admin
who later unticks the box is changing a real 1 rather than a blank.

Only fills an absent value. A site that has deliberately switched the OTP
step off stores 0, and 0 is not None, so re-running this will not switch it
back on.

Failures are logged, not raised. A patch that throws stops `bench migrate`
for the whole app, and the only thing lost here is a stored value whose
absence `pod_otp_required` already reads as ON — the secure side. Blocking
a deploy over a cosmetic write would be the worse outcome.
"""

import frappe


def execute():
	try:
		if not frappe.db.exists("DocType", "Driver Settings"):
			return

		if not frappe.get_meta("Driver Settings").get_field("require_pod_otp"):
			# post_model_sync should mean the field is already synced, so
			# getting here means the DocType JSON did not land.
			frappe.log_error(
				"Driver Settings.require_pod_otp is missing, so it could not "
				"be seeded. pod_otp_required() keeps the OTP mandatory while "
				"the value is unset, so PoD is unaffected; re-run this patch "
				"once the field exists.",
				"add_require_pod_otp_setting: field not found",
			)
			return

		# NOT get_single_value: it ends in cast(df.fieldtype, val), and
		# cast("Check", None) is 0, so an absent row comes back as 0 and
		# "is not None" is true for a field nobody has ever set. That is
		# what made the first version of this patch return here without
		# writing anything, on a site where the row did not exist.
		from erpera_driver_app.utils.otp import _raw_single_value

		if _raw_single_value("Driver Settings", "require_pod_otp") is not None:
			return

		frappe.db.set_single_value("Driver Settings", "require_pod_otp", 1)
		frappe.clear_cache(doctype="Driver Settings")
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"add_require_pod_otp_setting failed (PoD OTP stays mandatory)",
		)
