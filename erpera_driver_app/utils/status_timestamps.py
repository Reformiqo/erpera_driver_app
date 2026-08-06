"""Record when a Delivery Note entered each of its delivery states.

Seven Datetime fields on Delivery Note hold the moment the order reached a
given state. Six of them track `cowberry_delivery_status`; the last tracks
ERPNext's own `status` reaching "Completed".

All writes go through frappe.db.set_value. The fields are `allow_on_submit:
0` and Delivery Notes are submitted documents, so a doc-level save would be
rejected with "Not allowed to change after submission" — and the doc events
that would carry it don't fire for the API paths anyway, which already write
with set_value.
"""

import frappe
from frappe.utils import now_datetime


# cowberry_delivery_status → the field that records entering that state.
# Statuses absent from this map (Pending, Out for Delivery, Rescheduled,
# Failed, Cancelled) have no column of their own and are simply not stamped.
DELIVERY_STATUS_TIMESTAMP = {
    "Out for Pickup":   "custom_out_for_pickup_timestamp",
    "Picked Up":        "custom_picked_up_timestamp",
    "Out for Delivery": "custom_out_for_delivery_timestamp",
    "At Location":      "custom_at_location_timestamp",
    "Delivered":        "custom_delivered_timestamp",
    "Attempted":        "custom_attempted_timestamp",
}

# Driven by Delivery Note.status — ERPNext's billing field — reaching
# "Completed" (per_billed == 100). Deliberately NOT cowberry_delivery_status:
# that Select has no "Completed" option, and pointing it at "Delivered" would
# make this column a duplicate of custom_delivered_timestamp.
COMPLETED_TIMESTAMP_FIELD = "custom_completed_timestamp"
COMPLETED_STATUS = "Completed"


def timestamp_for(delivery_status):
    """Return {field: now} for a cowberry_delivery_status transition.

    Empty dict when the status has no column, so callers can merge the result
    into an update dict unconditionally.
    """
    field = DELIVERY_STATUS_TIMESTAMP.get(delivery_status)
    return {field: now_datetime()} if field else {}


def stamp_completed(delivery_note):
    """Stamp custom_completed_timestamp if the DN has reached Completed.

    Called after anything that can push per_billed to 100 (creating the Sales
    Invoice). ERPNext writes `status` with db_set, which fires no doc events,
    so there is nothing to hook — the moment has to be checked for.

    Best-effort: never raises, and never overwrites an existing stamp, since
    a Delivery Note only completes once.
    """
    if not delivery_note:
        return
    try:
        row = frappe.db.get_value(
            "Delivery Note", delivery_note,
            ["status", COMPLETED_TIMESTAMP_FIELD], as_dict=True) or {}
        if row.get("status") != COMPLETED_STATUS or row.get(COMPLETED_TIMESTAMP_FIELD):
            return
        frappe.db.set_value("Delivery Note", delivery_note,
                            COMPLETED_TIMESTAMP_FIELD, now_datetime(),
                            update_modified=False)
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not stamp completed timestamp",
            message=f"delivery_note={delivery_note}\n{frappe.get_traceback()}",
        )


def stamp_status(delivery_note, delivery_status):
    """Write the timestamp for a status the caller has already committed.

    For paths that update the Delivery Note through a doc save rather than an
    update dict. Best-effort; never raises.
    """
    updates = timestamp_for(delivery_status)
    if not (delivery_note and updates):
        return
    try:
        frappe.db.set_value("Delivery Note", delivery_note, updates,
                            update_modified=False)
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not stamp status timestamp",
            message=f"delivery_note={delivery_note} status={delivery_status}\n"
                    f"{frappe.get_traceback()}",
        )
