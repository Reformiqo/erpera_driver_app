import frappe


def _get_driver_employee():
    """Return the Employee name for the current user if they have the Driver role."""
    if not frappe.has_role("Driver"):
        return None
    return frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")


def cash_submission_query(user):
    """Drivers see only their own cash submissions."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabCowberry Cash Submission`.driver = {frappe.db.escape(employee)}"


def driver_collection_query(user):
    """Drivers see only their own collections."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabCowberry Driver Collection`.driver = {frappe.db.escape(employee)}"


def wallet_transaction_query(user):
    """Drivers see only wallet transactions for their linked customer."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    customer = frappe.db.get_value("Customer", {"cowberry_driver": employee}, "name")
    if not customer:
        return "1=0"
    return f"`tabCowberry Wallet Transaction`.customer = {frappe.db.escape(customer)}"


def delivery_attempt_log_query(user):
    """Drivers see only their own attempt logs."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabCowberry Delivery Attempt Log`.driver = {frappe.db.escape(employee)}"


def reschedule_log_query(user):
    """Drivers see only their own reschedule logs."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabCowberry Reschedule Log`.driver = {frappe.db.escape(employee)}"
