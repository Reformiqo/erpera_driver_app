"""Inspect — and optionally clean up — what a driver signup left behind.

Run inside a bench console:

    bench --site <site> console
    >>> exec(open("apps/erpera_driver_app/scripts/signup_check.py").read())

    >>> report("test@example.com", "8238240381")   # what exists right now
    >>> cleanup("test@example.com")                # remove that test account
    >>> fresh()                                    # an email/phone nothing owns

`report` follows the same chain `auth._do_driver_login` walks, so it shows
where a login would stop rather than only which rows exist:

    User (enabled, Driver role) → Employee.user_id → Driver.employee
"""

import frappe


def report(email, mobile=None):
	"""Print every record tied to this email or phone, and the login verdict."""
	print(f"\n=== {email}" + (f" / {mobile}" if mobile else "") + " ===")

	user = frappe.db.get_value(
		"User", email, ["name", "enabled", "user_type"], as_dict=True)
	if user:
		roles = frappe.get_all(
			"Has Role", filters={"parent": email, "parenttype": "User"}, pluck="role")
		print(f"User      {user.name}  enabled={user.enabled}  type={user.user_type}")
		print(f"          roles: {sorted(roles) or 'NONE'}")
	else:
		print("User      (none)")

	employees = frappe.get_all(
		"Employee",
		filters={"user_id": email},
		fields=["name", "employee_name", "user_id", "cell_number", "status"])
	if mobile:
		# Anything sharing the phone number, even if it belongs to someone
		# else — this is what the old phone matching used to latch onto.
		for row in frappe.get_all(
			"Employee", filters={"cell_number": mobile},
			fields=["name", "employee_name", "user_id", "cell_number", "status"]):
			if row.name not in {e.name for e in employees}:
				row["_note"] = "same phone, different user"
				employees.append(row)
	if employees:
		for e in employees:
			note = f"   <-- {e['_note']}" if e.get("_note") else ""
			print(f"Employee  {e.name}  {e.employee_name!r}  user_id={e.user_id!r}"
			      f"  phone={e.cell_number!r}  {e.status}{note}")
	else:
		print("Employee  (none)")

	drivers = []
	for e in employees:
		drivers += frappe.get_all(
			"Driver", filters={"employee": e.name},
			fields=["name", "full_name", "employee", "status"])
	if drivers:
		for d in drivers:
			print(f"Driver    {d.name}  {d.full_name!r}  employee={d.employee}  {d.status}")
	else:
		print("Driver    (none)")

	# The four things driver_login needs, in the order it checks them.
	own = [e for e in employees if e.user_id == email]
	checks = [
		("User exists and is enabled", bool(user and user.enabled)),
		("User carries the Driver role",
		 bool(user) and "Driver" in frappe.get_roles(email)),
		("Employee linked by user_id", bool(own)),
		("Driver record for that Employee",
		 bool(own) and bool(frappe.db.get_value("Driver", {"employee": own[0].name}))),
	]
	print("\n  login chain:")
	for label, passed in checks:
		print(f"    {'PASS' if passed else 'FAIL'}  {label}")
	if all(p for _, p in checks):
		print("  => this account can log in and will see its trips")
	else:
		print("  => login would fail, or the app would open empty")
	print()


def cleanup(email, mobile=None, confirm=True):
	"""Delete the User, its Employee and that Employee's Driver.

	Only touches an Employee whose `user_id` is this email — an Employee that
	merely shares the phone number belongs to somebody else and is left alone,
	beyond clearing a user_id this signup wrote onto it.
	"""
	targets = []
	for emp in frappe.get_all("Employee", filters={"user_id": email}, pluck="name"):
		for drv in frappe.get_all("Driver", filters={"employee": emp}, pluck="name"):
			targets.append(("Driver", drv))
		targets.append(("Employee", emp))
	if frappe.db.exists("User", email):
		targets.append(("User", email))

	if not targets:
		print(f"Nothing to clean up for {email}.")
		return

	print("Will delete:")
	for doctype, name in targets:
		print(f"  {doctype:10} {name}")
	if confirm:
		print("\nRe-run with confirm=False to actually delete.")
		return

	for doctype, name in targets:
		try:
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			print(f"  deleted {doctype} {name}")
		except Exception as e:
			print(f"  could NOT delete {doctype} {name}: {e}")
	frappe.db.commit()
	print("Done.")


def diagnose_employee(company=None):
	"""Try to create an Employee and roll it back, reporting what objects.

	Signup does not validate Employee — every app installed on the site does,
	through its own hooks. When one of those refuses, signup reports the
	refusal and looks like the culprit. This makes the same attempt in
	isolation, so it is clear whether the Employee could be created at all.

	Also lists the hooks and the custom fields other apps have put on
	Employee, which is where a value like the one being rejected comes from.
	"""
	print("\n=== Who validates Employee on this site ===")
	for event in ("before_validate", "validate", "before_insert", "after_insert",
	              "before_save", "on_update"):
		handlers = frappe.get_hooks("doc_events", {}).get("Employee", {}).get(event) or []
		handlers += frappe.get_hooks("doc_events", {}).get("*", {}).get(event) or []
		for h in handlers:
			print(f"  {event:16} {h}")

	print("\n=== Custom fields other apps put on Employee (with defaults) ===")
	rows = frappe.get_all(
		"Custom Field",
		filters={"dt": "Employee"},
		fields=["fieldname", "fieldtype", "options", "default", "reqd", "module"],
		order_by="module, fieldname")
	for r in rows:
		if not (r.default or r.reqd):
			continue
		print(f"  {r.fieldname:36} {r.fieldtype:10} options={r.options!r}"
		      f" default={r.default!r} reqd={r.reqd} module={r.module!r}")

	print("\n=== Dry-run insert (rolled back) ===")
	company = (company
	           or frappe.defaults.get_user_default("Company")
	           or frappe.db.get_single_value("Global Defaults", "default_company"))
	try:
		emp = frappe.new_doc("Employee")
		emp.first_name = "Signup"
		emp.last_name = "Diagnostic"
		emp.employee_name = "Signup Diagnostic"
		emp.date_of_joining = frappe.utils.today()
		emp.status = "Active"
		if company:
			emp.company = company
		emp.flags.ignore_mandatory = True
		emp.flags.ignore_permissions = True
		emp.insert(ignore_permissions=True)
		print(f"  OK — Employee {emp.name} would have been created")
	except Exception as e:
		print(f"  FAILED — {type(e).__name__}: {e}")
		print("\n  This is not signup: the same failure happens creating an")
		print("  Employee from the desk. Fix the referenced record or clear the")
		print("  default on the custom field above, then retry the signup.")
	finally:
		frappe.db.rollback()
		print("  (rolled back — nothing was kept)")
	print()


def fresh():
	"""An email and phone number nothing on this site is using."""
	import random

	for _ in range(50):
		n = random.randint(1000, 9999)
		email = f"driver.test{n}@example.com"
		mobile = f"9{random.randint(100000000, 999999999)}"
		if frappe.db.exists("User", email):
			continue
		if frappe.db.exists("Employee", {"cell_number": mobile}):
			continue
		print(f'{{"full_name": "Test Driver {n}", "mobile_no": "{mobile}",'
		      f' "email": "{email}", "password": "Str0ng-Pass-2026"}}')
		return {"email": email, "mobile": mobile}
	print("Could not find an unused pair — try again.")
