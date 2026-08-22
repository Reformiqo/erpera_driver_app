"""Driver notification endpoints — `erpera_driver_app.api.notification.<fn>`.

The device registers its FCM token after login, then reads its inbox from
`get_notifications`. Everything is scoped to the calling driver; there is no
way to read or clear somebody else's notifications through here.
"""
import frappe
from frappe.utils import cint

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.notifications import notify, push_now, register_device
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist(methods=["POST"])
def register_token(fcm_token=None, device_type="Android", device_id=None):
    """Store the device's FCM token. Call on every cold start.

    `auth.driver_login` already registers the device from the `fcm_token` it
    receives, so the app does not have to call this at login. It is here for
    the rotation case: Firebase reissues tokens on its own schedule, and that
    happens without a login.
    """
    try:
        employee = _require_driver()
        if not fcm_token:
            return err("VALIDATION_ERROR", "`fcm_token` is required.", 400)
        if device_type not in ("Android", "iOS"):
            return err("VALIDATION_ERROR",
                       "`device_type` must be 'Android' or 'iOS'.", 400)

        name = register_device(
            user=frappe.session.user, employee=employee, fcm_token=fcm_token,
            device_type=device_type, device_id=device_id,
        )
        if not name:
            return err("REGISTER_TOKEN_FAILED",
                       "Could not store the token — check the Error Log.", 500)

        frappe.db.commit()
        return ok(data={"token_id": name, "registered": True})
    except Exception as e:
        return err("REGISTER_TOKEN_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def unregister_token(fcm_token=None, device_id=None):
    """Deactivate this device on logout. Rows are kept, not deleted, so the
    history of which device a push went to survives."""
    try:
        _require_driver()
        if not (fcm_token or device_id):
            return err("VALIDATION_ERROR",
                       "Pass `fcm_token` or `device_id`.", 400)

        filters = {"user": frappe.session.user}
        if device_id:
            filters["device_id"] = device_id
        else:
            filters["fcm_token"] = fcm_token

        names = frappe.get_all("Driver FCM Token", filters=filters, pluck="name")
        for name in names:
            frappe.db.set_value("Driver FCM Token", name, "is_active", 0)
        frappe.db.commit()
        return ok(data={"deactivated": len(names)})
    except Exception as e:
        return err("UNREGISTER_TOKEN_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def get_notifications(page=0, page_size=20, unread_only=0):
    """The driver's inbox, newest first, with the unread badge count."""
    try:
        employee = _require_driver()
        page = max(cint(page), 0)
        page_size = min(max(cint(page_size) or 20, 1), 100)

        filters = {"employee": employee}
        if cint(unread_only):
            filters["is_read"] = 0

        rows = frappe.get_all(
            "Driver Notification",
            filters=filters,
            fields=["name", "title", "message", "event_key", "action_type",
                    "reference_doctype", "reference_name", "is_read",
                    "creation"],
            order_by="creation desc",
            start=page * page_size,
            page_length=page_size,
        )
        for row in rows:
            row["creation"] = str(row["creation"])
            row["is_read"] = bool(row["is_read"])

        return ok(data={
            "notifications": rows,
            "unread_count":  frappe.db.count("Driver Notification",
                                             {"employee": employee, "is_read": 0}),
            "page":          page,
            "page_size":     page_size,
        })
    except Exception as e:
        return err("GET_NOTIFICATIONS_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def mark_read(notification_id=None):
    """Mark one notification read. Refuses a notification that is not yours."""
    try:
        employee = _require_driver()
        if not notification_id:
            return err("VALIDATION_ERROR", "`notification_id` is required.", 400)

        owner = frappe.db.get_value("Driver Notification", notification_id, "employee")
        if not owner:
            return err("NOT_FOUND",
                       f"Notification '{notification_id}' not found.", 404)
        if owner != employee:
            return err("FORBIDDEN", "That notification is not yours.", 403)

        frappe.db.set_value("Driver Notification", notification_id, "is_read", 1)
        frappe.db.commit()
        return ok(data={"notification_id": notification_id, "is_read": True})
    except Exception as e:
        return err("MARK_READ_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def mark_all_read():
    """Clear the badge."""
    try:
        employee = _require_driver()
        names = frappe.get_all("Driver Notification",
                               filters={"employee": employee, "is_read": 0},
                               pluck="name")
        for name in names:
            frappe.db.set_value("Driver Notification", name, "is_read", 1)
        frappe.db.commit()
        return ok(data={"marked": len(names)})
    except Exception as e:
        return err("MARK_ALL_READ_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def send_test(title="Test notification", body="Push from ERPNext is working."):
    """Send yourself a push, to prove the Firebase credentials and the device
    token are both good before wiring a real event to them.

    Sent inline rather than enqueued so the response reflects what actually
    happened — a background job would return success either way.
    """
    try:
        employee = _require_driver()
        # "test" is not one of the nine, so there is no notify_test checkbox to
        # switch it off — a diagnostic that a per-event box could silence would
        # be useless. It still obeys the master switch. Stamping it beats
        # leaving event_key empty: Frappe hides a read-only field with no
        # value, so a blank one makes the record look like it lost the column.
        name = notify(employee, title, body, event_key="test", push=False)
        if not name:
            return err("TEST_FAILED",
                       "Could not create the notification — check the Error Log.", 500)
        push_now(name)
        pushed = frappe.db.get_value("Driver Notification", name, "is_pushed")
        return ok(data={
            "notification_id": name,
            "pushed": bool(pushed),
            "hint": None if pushed else
                    "Created but not delivered. Check that Driver Notification Settings has "
                    "the full Service Account JSON, and that this device has "
                    "registered a token. The Error Log has the reason.",
        })
    except Exception as e:
        return err("TEST_FAILED", str(e))
