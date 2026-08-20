"""Driver signup — create the records a driver login needs.

`auth._do_driver_login` refuses an account unless three things are true:

    1. the User exists and is enabled
    2. the User carries the `Driver` role
    3. an Employee is linked to it through `Employee.user_id`

and a driver who clears all three still opens an empty app without a fourth:
an ERPNext `Driver` record pointing at that Employee, because
`Delivery Trip.driver` links to `Driver` and `trip._driver_record()` resolves
through it.

So signup creates:

    User      enabled, holding the Driver role
    Employee  linked by user_id — created if none is already on file
    Driver    what Delivery Trip.driver points at

Each is created only when it is missing, so a repeat call is harmless. It is
all or nothing: the User is inserted disabled and only enabled once the
Employee and Driver exist, so a failure part-way rolls back rather than
leaving a User carrying the Driver role with nothing behind it.

The password is never stored by this app. It is set on a real User document,
so Frappe hashes and owns it from the first moment.

Worth knowing: this grants the `Driver` role from a public endpoint, and that
role is the key to `_require_driver()` — behind which
`wallet.load(action="topup"|"deduct")` moves money on a customer's wallet
with no OTP. Closing that gap is a separate change.
"""

import contextlib
import re

import frappe
from frappe.utils import today

from erpera_driver_app.utils.response import err, ok

MIN_PASSWORD_LENGTH = 8


@contextlib.contextmanager
def _as_administrator():
    """Run the record creation as Administrator.

    The endpoint is `allow_guest`, so the session user is Guest — and Guest
    cannot create the records a driver needs. `ignore_permissions` covers the
    document being inserted but not what ERPNext's own hooks do underneath:
    setting `Employee.user_id` makes it create a User Permission, and that
    insert is checked against the session user, which is how signup came back
    with "No permission for User Permission".

    Same pattern, and same reason, as `wallet.validate_topup_otp`.
    """
    original = frappe.session.user
    try:
        frappe.set_user("Administrator")
        yield
    finally:
        frappe.set_user(original)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def _clean_mobile(mobile):
    """Digits only, with an Indian country code stripped.

    Kept in the form the rest of the app compares against: Employee.cell_number
    on this site holds bare 10-digit numbers, so leaving a `+91` on would make
    the same driver look like two different people.
    """
    digits = re.sub(r"\D", "", str(mobile or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def _validate(full_name, mobile_no, email, password):
    """Return (cleaned, error). Exactly one is None."""
    full_name = (full_name or "").strip()
    email = (email or "").strip().lower()
    mobile = _clean_mobile(mobile_no)
    password = password or ""

    if len(full_name) < 2 or len(full_name) > 140:
        return None, err("VALIDATION_ERROR", "Please enter your full name.", 400)
    if not frappe.utils.validate_email_address(email):
        return None, err("VALIDATION_ERROR", "Please enter a valid email address.", 400)
    if not (10 <= len(mobile) <= 15):
        return None, err("VALIDATION_ERROR", "Please enter a valid mobile number.", 400)
    if len(password) < MIN_PASSWORD_LENGTH:
        return None, err("WEAK_PASSWORD",
                         f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", 400)

    return {"full_name": full_name, "email": email,
            "mobile": mobile, "password": password}, None


def _split_name(full_name):
    parts = full_name.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _resolve_company(settings):
    """Company for the new Employee.

    Configured value first, then Frappe's own defaults, and finally the site's
    only Company when there is exactly one — which is the usual case.
    """
    company = (settings.get("signup_default_company")
               or frappe.defaults.get_user_default("Company")
               or frappe.db.get_single_value("Global Defaults", "default_company"))
    if company:
        return company
    names = frappe.get_all("Company", pluck="name", limit=2)
    return names[0] if len(names) == 1 else None


def _create_user(data):
    first_name, last_name = _split_name(data["full_name"])
    user = frappe.new_doc("User")
    user.email = data["email"]
    user.first_name = first_name
    user.last_name = last_name
    user.mobile_no = data["mobile"]
    user.user_type = "System User"
    # Enabled at the end, once the Employee and Driver exist — see signup().
    user.enabled = 0
    user.send_welcome_email = 0
    user.new_password = data["password"]
    # Granted here rather than after the Employee exists. A role on a disabled
    # account grants nothing — `auth._do_driver_login` checks `enabled` first —
    # and inserting a System User with no roles makes Frappe raise its "user
    # has no roles enabled" warning, which surfaced in the signup response and
    # read like a failure.
    user.append_roles("Driver")
    user.flags.ignore_permissions = True
    user.flags.no_welcome_mail = True
    user.insert(ignore_permissions=True)
    return user.name


def _find_employee(email):
    """An Employee already on file for this email address, if there is one.

    Most warehouses already hold Employee records for their drivers, so a
    signup is often that person claiming an app login rather than a new
    joiner, and reusing the record avoids a duplicate.

    Matched on email only. Matching on mobile number as well looked helpful
    and was not: a phone number is weak evidence of identity, is reused and
    mistyped, and one that happened to be on an existing Employee silently
    attached a brand-new login to a real person's HR record.
    """
    for filters in ({"company_email": email}, {"personal_email": email},
                    {"user_id": email}):
        found = frappe.db.get_value("Employee", filters, "name")
        if found:
            return found
    return None


def _clear_broken_link_defaults(doc):
    """Blank Link fields whose value points at a record that does not exist.

    Other apps add Link fields to Employee carrying a default, and their
    validate hooks then look that value up. Where the referenced record was
    never created on this site the lookup throws and no Employee can be
    created at all — lantern360_integration does exactly this with a
    `Lantern360 Role` of "Fieldemployee".

    Signup never chose those values; they arrived as field defaults. Clearing
    only the ones that cannot resolve leaves every valid default alone, and
    leaves the field empty for whoever owns that app to fill in.

    Cleared to an empty string rather than None on purpose:
    `Document._set_defaults()` re-applies the DocField default to anything
    reading None, so clearing to None would put the broken value straight
    back.
    """
    cleared = []
    for df in doc.meta.fields:
        if df.fieldtype != "Link" or not df.options:
            continue
        value = doc.get(df.fieldname)
        if not value:
            continue
        try:
            resolves = frappe.db.exists(df.options, value)
        except Exception:
            resolves = False
        if not resolves:
            doc.set(df.fieldname, "")
            cleared.append(f"{df.fieldname} ({df.options} '{value}' does not exist)")
    return cleared


def _build_employee(data, user, settings):
    """The Employee document, before insert."""
    first_name, last_name = _split_name(data["full_name"])
    gender = settings.get("signup_default_gender")
    company = _resolve_company(settings)

    emp = frappe.new_doc("Employee")
    emp.first_name = first_name
    emp.last_name = last_name
    emp.employee_name = data["full_name"]
    emp.date_of_joining = today()
    emp.status = "Active"
    emp.cell_number = data["mobile"]
    emp.personal_email = data["email"]
    emp.user_id = user
    if gender:
        emp.gender = gender
    if company:
        emp.company = company

    available = {f.fieldname for f in frappe.get_meta("Employee").fields}
    warehouse = settings.get("signup_default_warehouse")
    if warehouse and "default_warehouse" in available:
        emp.default_warehouse = warehouse
    limit = settings.get("signup_default_daily_collection_limit")
    if limit and "daily_collection_limit" in available:
        emp.daily_collection_limit = limit

    dob = settings.get("signup_default_date_of_birth")
    if dob:
        emp.date_of_birth = dob

    # Read from the meta rather than a fixed list. Which fields are mandatory
    # is a per-site decision — this site also requires employee_number — and a
    # hardcoded list would report the record as complete while it was not.
    blank = [df.fieldname for df in emp.meta.fields
             if df.reqd and emp.get(df.fieldname) in (None, "")]
    # The signup form collects four things and cannot invent a birthday; a
    # fabricated one is data somebody later trusts. Anything still unknown is
    # left blank and the insert skips the mandatory check, so signup is not
    # refused over a value nobody supplied. Set the defaults in Driver
    # Settings to have the records come out complete instead.
    if blank:
        emp.flags.ignore_mandatory = True
    emp.flags.ignore_permissions = True
    return emp, blank


def _create_employee(data, user, settings):
    """Create the Employee. Returns (name, blank_fields, notes)."""
    emp, blank = _build_employee(data, user, settings)
    notes = _clear_broken_link_defaults(emp)
    emp.insert(ignore_permissions=True)
    return emp.name, blank, notes


def _ensure_driver(employee, full_name, mobile):
    """Find or create the ERPNext Driver row the trip screens resolve through.

    Without it the account logs in perfectly and then shows an empty day.
    """
    existing = frappe.db.get_value("Driver", {"employee": employee}, "name")
    if existing:
        return existing
    available = {f.fieldname for f in frappe.get_meta("Driver").fields}
    drv = frappe.new_doc("Driver")
    drv.employee = employee
    if "full_name" in available:
        drv.full_name = full_name
    if "status" in available:
        drv.status = "Active"
    if "cell_number" in available:
        drv.cell_number = mobile
    drv.flags.ignore_permissions = True
    drv.insert(ignore_permissions=True)
    return drv.name


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def signup(full_name=None, mobile_no=None, email=None, password=None,
           # The form is specified as Name / Email / Password / Phone Number,
           # so a client coded from that wording sends those names.
           name=None, phone_number=None, phone=None, mobile=None):
    """Create a driver account from a name, email, password and phone number.

    Body: `{full_name, mobile_no, email, password}`. `name` is accepted for
    `full_name`, and `phone_number` / `phone` / `mobile` for `mobile_no`.

    Returns the User, Employee and Driver it created. The account can log in
    immediately.
    """
    try:
        full_name = full_name or name
        mobile_no = mobile_no or phone_number or phone or mobile

        data, error = _validate(full_name, mobile_no, email, password)
        if error:
            return error

        if frappe.db.exists("User", data["email"]):
            return err("EMAIL_EXISTS",
                       "An account already exists for this email. Please log in "
                       "or use Forgot Password.", 409)

        # Checked before anything is written. An Employee already tied to a
        # different login cannot be reused: `_require_driver` finds the driver
        # by Employee.user_id, so a second user pointing at it would get
        # NO_EMPLOYEE at the login screen — an account created successfully
        # and then unable to log in.
        employee = _find_employee(data["email"])
        if employee:
            linked = frappe.db.get_value("Employee", employee, "user_id")
            if linked and linked != data["email"]:
                return err("EMPLOYEE_LINKED_TO_ANOTHER_USER",
                           f"Employee {employee} already carries this email and is "
                           f"linked to {linked}. Ask your administrator to sort the "
                           "records out before signing up.", 409)

        settings = frappe.get_single("Driver Settings")

        blank, notes = [], []
        with _as_administrator():
            user = _create_user(data)

            if employee:
                if not frappe.db.get_value("Employee", employee, "user_id"):
                    frappe.db.set_value("Employee", employee, "user_id", user)
            else:
                employee, blank, notes = _create_employee(data, user, settings)

            driver = _ensure_driver(employee, data["full_name"], data["mobile"])

            # Enabled last. Until the Employee and Driver exist, an enabled
            # account would log in and show an empty app; a failure before
            # this point rolls back and leaves nothing usable behind.
            frappe.db.set_value("User", user, "enabled", 1)

        frappe.db.commit()

        return ok(data={
            "user":     user,
            "employee": employee,
            "driver":   driver,
            # Employee fields ERPNext wants but signup cannot know. The account
            # works; HR can fill these in.
            "incomplete_employee_fields": blank,
            # Anything the site's own rules forced signup to work around —
            # a broken field default cleared, or validation bypassed. Empty on
            # a site whose Employee rules the signup data already satisfies.
            "notes":    notes,
            "message":  "Account created. You can log in now.",
        })
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(title="Driver signup failed",
                         message=frappe.get_traceback())
        return err("SIGNUP_FAILED", str(e), 500)
