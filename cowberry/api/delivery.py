import frappe

from cowberry.api.driver import _require_driver
from cowberry.utils.exceptions import DeliveryNoteNotFoundError, InvalidStatusTransitionError
from cowberry.utils.geo import validate_coords
from cowberry.utils.response import err, ok

VALID_TRANSITIONS = {
    "Pending": ["Out for Delivery", "Failed"],
    "Out for Delivery": ["Delivered", "Failed", "Rescheduled"],
    "Rescheduled": ["Out for Delivery", "Failed"],
    "Failed": [],
    "Delivered": [],
}


@frappe.whitelist()
def update_status(delivery_note, status, latitude=None, longitude=None, notes=None, failure_reason=None):
    try:
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        dn = frappe.get_doc("Delivery Note", delivery_note)
        current = dn.get("cowberry_delivery_status") or "Pending"

        allowed = VALID_TRANSITIONS.get(current, [])
        if status not in allowed:
            raise InvalidStatusTransitionError(
                f"Cannot transition from '{current}' to '{status}'."
            )

        coords = None
        if latitude and longitude:
            coords = validate_coords(latitude, longitude)

        # Log the attempt
        attempt = frappe.new_doc("Cowberry Delivery Attempt Log")
        attempt.delivery_note = delivery_note
        attempt.driver = employee
        attempt.attempt_status = status
        attempt.notes = notes or ""
        attempt.failure_reason = failure_reason or ""
        if coords:
            attempt.latitude = coords[0]
            attempt.longitude = coords[1]
        attempt.insert(ignore_permissions=True)

        dn.cowberry_delivery_status = status
        if notes:
            dn.cowberry_delivery_notes = notes
        dn.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"delivery_note": dn.name, "status": status})
    except (DeliveryNoteNotFoundError, InvalidStatusTransitionError) as e:
        return e.to_response()
    except Exception as e:
        return err("UPDATE_STATUS_FAILED", str(e))


def on_submit_delivery_note(doc, method):
    """Triggered when a Delivery Note is submitted."""
    _sync_delivery_note(doc)


def on_cancel_delivery_note(doc, method):
    """Triggered when a Delivery Note is cancelled."""
    pass


def _sync_delivery_note(dn):
    try:
        idempotency_key = f"dn-submit-{dn.name}-{dn.modified}"
        if frappe.db.exists("Cowberry Delivery Sync Log", {"idempotency_key": idempotency_key}):
            return

        sync_log = frappe.new_doc("Cowberry Delivery Sync Log")
        sync_log.idempotency_key = idempotency_key
        sync_log.delivery_note = dn.name
        sync_log.status = "Pending"
        sync_log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass
