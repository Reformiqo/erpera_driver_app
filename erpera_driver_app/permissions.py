import frappe


def _get_driver_employee():
    """Return the Employee name for the current user if they have the Driver role."""
    if "Driver" not in frappe.get_roles():
        return None
    return frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")


def cash_submission_query(user):
    """Drivers see only their own cash submissions."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabCash Submission`.driver = {frappe.db.escape(employee)}"


def driver_collection_query(user):
    """Drivers see only their own collections."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabDriver Collection`.driver = {frappe.db.escape(employee)}"


def wallet_transaction_query(user):
    """Drivers don't browse Wallet Transaction in Desk — the mobile
    app uses wallet.load `history` with a card_number, which gates by
    card ownership. Deny the list-view path for drivers; leave other
    roles unrestricted."""
    if "Driver" in frappe.get_roles():
        return "1=0"
    return ""


def delivery_attempt_log_query(user):
    """Drivers see only their own attempt logs."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabDelivery Attempt Log`.driver = {frappe.db.escape(employee)}"


def reschedule_log_query(user):
    """Drivers see only their own reschedule logs."""
    employee = _get_driver_employee()
    if not employee:
        return ""
    return f"`tabReschedule Log`.driver = {frappe.db.escape(employee)}"
