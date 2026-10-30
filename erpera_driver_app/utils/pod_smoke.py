"""Find a Delivery Note worth testing the PoD OTP toggle against.

Picking one by hand is harder than it looks: the delivery endpoints refuse
a caller who is not a Driver, refuse a Delivery Note that is not on one of
THAT driver's trips, and refuse a terminal status. A Delivery Note that
fails any of those returns NOT_DRIVER, FORBIDDEN or INVALID_TRANSITION,
none of which says anything about the OTP — so a test run against the
wrong order looks like a broken feature.

This lists only orders that pass all three, and names the user to log in
as for each one.

    bench --site <site> execute erpera_driver_app.utils.pod_smoke.candidates
"""

import frappe

from erpera_driver_app.utils.otp import _raw_single_value, pod_otp_required


def _line(char="-", width=78):
	print(char * width)


def candidates(limit=15):
	_line("=")
	print("Driver Settings")
	_line("=")
	stored = _raw_single_value("Driver Settings", "require_pod_otp")
	print("  require_pod_otp stored value = %r" % (stored,))
	print("  pod_otp_required()           = %s" % pod_otp_required())
	if stored is None:
		print("  No row in tabSingles yet. That reads as ON, which is the safe")
		print("  default, but tick and untick the box once so the stored value")
		print("  matches what the form shows.")
	print()

	rows = frappe.db.sql(
		"""
		SELECT dn.name                      AS delivery_note,
		       dn.cowberry_delivery_status  AS status,
		       dn.cowberry_payment_method   AS pay_method,
		       dn.grand_total               AS grand_total,
		       dn.otp_validated             AS otp_validated,
		       dt.name                      AS trip,
		       emp.user_id                  AS login_as
		  FROM `tabDelivery Note` dn
		  JOIN `tabDelivery Stop` ds ON ds.delivery_note = dn.name
		  JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
		  JOIN `tabDriver`   drv ON drv.name = dt.driver
		  JOIN `tabEmployee` emp ON emp.name = drv.employee
		 WHERE dn.docstatus = 1
		   AND IFNULL(dn.cowberry_delivery_status, '') NOT IN
		       ('Delivered', 'Cancelled', 'Returned')
		   AND IFNULL(emp.user_id, '') != ''
		 ORDER BY FIELD(IFNULL(dn.cowberry_payment_method, ''), 'Prepaid') DESC,
		          dn.modified DESC
		 LIMIT %s
		""",
		(frappe.utils.cint(limit) or 15,),
		as_dict=True,
	)

	_line("=")
	print("Delivery Notes you can test with")
	_line("=")
	if not rows:
		print("  None. Every submitted Delivery Note is either already in a")
		print("  terminal status or sits on a trip whose driver has no user")
		print("  account, so no driver could call the endpoints for it.")
		print("  Put an open Delivery Note on a trip assigned to one of the")
		print("  drivers listed by juspay_integration.utils.smoke.diagnose.")
		_line()
		return

	print("  %-16s %-16s %-11s %9s %-5s %s"
	      % ("Delivery Note", "Status", "Payment", "Total", "OTP?", "Log in as"))
	for r in rows:
		print("  %-16s %-16s %-11s %9.2f %-5s %s" % (
			r.delivery_note,
			(r.status or "Pending")[:16],
			(r.pay_method or "-")[:11],
			frappe.utils.flt(r.grand_total),
			"yes" if frappe.utils.cint(r.otp_validated) else "no",
			r.login_as,
		))

	prepaid = [r for r in rows if (r.pay_method or "") == "Prepaid"]
	_line()
	if prepaid:
		pick = prepaid[0]
		print("  Pick a Prepaid one if you can — %s is the obvious choice."
		      % pick.delivery_note)
		print("  A COD order also needs cod_collected_amount to equal the")
		print("  expected total exactly, so a mismatch there returns")
		print("  COD_AMOUNT_MISMATCH and tells you nothing about the OTP.")
	else:
		print("  None of these is Prepaid. A COD order works, but send")
		print("  cod_collected_amount equal to the order total or you will get")
		print("  COD_AMOUNT_MISMATCH, which is not an OTP failure.")
	print()
	print("  'OTP?' is DN.otp_validated. It must read 'no' to test the gate —")
	print("  an order whose OTP is already validated passes update_status even")
	print("  with the setting on, which would look like the gate not working.")
	_line()
	return [r.delivery_note for r in rows]
