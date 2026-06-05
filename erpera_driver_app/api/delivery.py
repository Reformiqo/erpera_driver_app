"""Delivery Lifecycle API — covers Nainsi's spec §Delivery Lifecycle §§1-6.

The lifecycle spans the full pickup→delivery flow. `update_status` is the
generic transition endpoint; the other five are convenience wrappers that
call it with the right target status so the Flutter app doesn't have to
remember the transition map.
"""
import frappe
from frappe.utils import flt, getdate, now_datetime, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import (
    DeliveryNoteNotFoundError,
    InvalidStatusTransitionError,
)
from erpera_driver_app.utils.geo import validate_coords
from erpera_driver_app.utils.response import err, ok


# Lifecycle map per spec. Terminal states (Delivered, Cancelled) have
# no outgoing edges. "Pending" is the implicit starting state when
# cowberry_delivery_status is null/empty on a freshly submitted DN.
ALLOWED_TRANSITIONS = {
    "Pending":         ["Out for Pickup", "Out for Delivery"],
    "Out for Pickup":  ["Picked Up", "Failed"],
    "Picked Up":       ["Out for Delivery"],
    "Out for Delivery": ["At Location", "Delivered", "Attempted", "Rescheduled", "Failed"],
    "At Location":     ["Delivered", "Attempted", "Rescheduled", "Failed"],
    "Rescheduled":     ["Out for Pickup", "Out for Delivery"],
    "Attempted":       ["Rescheduled", "Out for Delivery", "Failed"],
    "Delivered":       [],
    "Cancelled":       [],
    "Failed":          [],
}

# Spec wording — exactly the strings the Flutter client sends and displays
TERMINAL_STATUSES = {"Delivered", "Cancelled"}

DEFAULT_MAX_RESCHEDULES = 3


def _assert_driver_owns_dn(driver, delivery_note):
    """Return (trip_name, expected_arrival) when the DN is on a trip
    assigned to this driver; raise FORBIDDEN otherwise."""
    if not driver:
        return None, None
    row = frappe.db.sql(
        """SELECT ds.parent AS trip_name, ds.estimated_arrival
             FROM `tabDelivery Stop` ds
             JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
            WHERE ds.delivery_note = %s AND dt.driver = %s
            LIMIT 1""",
        (delivery_note, driver), as_dict=True,
    )
    if not row:
        # Caller is a driver, but this DN isn't on any of their trips.
        raise frappe.PermissionError(
            "FORBIDDEN: This delivery note is not assigned to you."
        )
    return row[0].trip_name, row[0].estimated_arrival


def _do_update_status(delivery_note, target_status, gps_lat=None, gps_lng=None,
                      notes=None, failure_reason=None):
    """Shared implementation. Returns ok()/err() envelope ready to return."""
    employee = _require_driver()
    if not delivery_note:
        return err("VALIDATION_ERROR", "`delivery_note` is required.", 400)
    if not target_status:
        return err("VALIDATION_ERROR", "`target_status` is required.", 400)
    if not frappe.db.exists("Delivery Note", delivery_note):
        return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)

    # Scope: DN must be on a trip assigned to this driver.
    from erpera_driver_app.api.trip import _driver_record
    driver = _driver_record(employee)
    try:
        trip_name, expected_arrival = _assert_driver_owns_dn(driver, delivery_note)
    except frappe.PermissionError as pe:
        return err("FORBIDDEN", str(pe), 403)

    dn = frappe.get_doc("Delivery Note", delivery_note)
    current = dn.get("cowberry_delivery_status") or "Pending"
    if current in TERMINAL_STATUSES:
        return err("INVALID_TRANSITION",
                   f"Cannot transition from terminal status '{current}'.", 400)
    allowed = ALLOWED_TRANSITIONS.get(current, [])
    if target_status not in allowed:
        return err("INVALID_TRANSITION",
                   f"Cannot transition from '{current}' to '{target_status}'.", 400)
    if target_status == "At Location" and (gps_lat is None or gps_lng is None):
        return err("VALIDATION_ERROR",
                   "GPS coordinates required for 'At Location' status.", 400)

    coords = None
    if gps_lat is not None and gps_lng is not None:
        coords = validate_coords(gps_lat, gps_lng)

    # Log the transition attempt for the audit trail.
    attempt = frappe.new_doc("Delivery Attempt Log")
    attempt.delivery_note = delivery_note
    attempt.driver = employee
    attempt.attempt_status = target_status
    attempt.notes = notes or ""
    attempt.failure_reason = failure_reason or ""
    if coords:
        attempt.latitude = coords[0]
        attempt.longitude = coords[1]
    attempt.flags.ignore_permissions = True
    attempt.insert(ignore_permissions=True)

    # Compute timing fields for spec response: actual_arrival_time +
    # travel_variance_mins are meaningful when moving INTO "At Location".
    now = now_datetime()
    actual_arrival = None
    travel_variance = None
    if target_status == "At Location":
        actual_arrival = now
        if expected_arrival:
            travel_variance = int((now - expected_arrival).total_seconds() // 60)

    dn.cowberry_delivery_status = target_status
    if notes:
        dn.cowberry_delivery_notes = notes
    dn.flags.ignore_permissions = True
    dn.save(ignore_permissions=True)
    frappe.db.commit()

    return ok(data={
        "delivery_note":        dn.name,
        "delivery_status":      target_status,
        "actual_arrival_time":  str(actual_arrival) if actual_arrival else None,
        "travel_variance_mins": travel_variance,
    })


# ---------------------------------------------------------------------------
# §1 Generic update_status (PUT per spec; POST kept for back-compat)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["PUT", "POST"])
def update_status(delivery_note=None, target_status=None, gps_lat=None, gps_lng=None,
                  notes=None, failure_reason=None,
                  # legacy aliases — older clients still send these names
                  status=None, latitude=None, longitude=None):
    """Delivery §1 — generic transition endpoint.

    Spec body: {delivery_note, target_status, gps_lat?, gps_lng?}.
    Legacy aliases `status`, `latitude`, `longitude` are accepted so
    the existing convenience wrappers in our codebase keep working.
    """
    try:
        return _do_update_status(
            delivery_note=delivery_note,
            target_status=target_status or status,
            gps_lat=gps_lat if gps_lat is not None else latitude,
            gps_lng=gps_lng if gps_lng is not None else longitude,
            notes=notes,
            failure_reason=failure_reason,
        )
    except Exception as e:
        return err("UPDATE_STATUS_FAILED", str(e))


# ---------------------------------------------------------------------------
# §2-4 thin convenience wrappers
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def start_pickup(delivery_note=None):
    """Delivery §2 — Pending → Out for Pickup."""
    try:
        return _do_update_status(delivery_note, "Out for Pickup")
    except Exception as e:
        return err("START_PICKUP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def confirm_pickup(delivery_note=None):
    """Delivery §3 — Out for Pickup → Picked Up."""
    try:
        return _do_update_status(delivery_note, "Picked Up")
    except Exception as e:
        return err("CONFIRM_PICKUP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def mark_arrived(delivery_note=None, gps_lat=None, gps_lng=None):
    """Delivery §4 — → At Location. GPS mandatory."""
    try:
        return _do_update_status(delivery_note, "At Location",
                                 gps_lat=gps_lat, gps_lng=gps_lng)
    except Exception as e:
        return err("MARK_ARRIVED_FAILED", str(e))


# ---------------------------------------------------------------------------
# §5 attempt — writes a richer Attempt Log alongside the status flip
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def attempt(delivery_note=None, outcome=None, reason_note=None,
            photo_url=None, gps_lat=None, gps_lng=None):
    """Delivery §5 — record a failed delivery attempt.

    Required: outcome, photo_url. reason_note strongly recommended.
    Writes a Delivery Attempt Log with the photo + reason and flips
    cowberry_delivery_status to 'Attempted'. Returns the attempt log
    name + cumulative attempt_number for this DN.
    """
    try:
        if not outcome:
            return err("VALIDATION_ERROR", "`outcome` is required.", 400)
        if not photo_url:
            return err("VALIDATION_ERROR", "`photo_url` is required.", 400)
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)

        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        try:
            _assert_driver_owns_dn(driver, delivery_note)
        except frappe.PermissionError as pe:
            return err("FORBIDDEN", str(pe), 403)

        coords = None
        if gps_lat is not None and gps_lng is not None:
            coords = validate_coords(gps_lat, gps_lng)

        log = frappe.new_doc("Delivery Attempt Log")
        log.delivery_note = delivery_note
        log.driver = employee
        log.attempt_status = "Attempted"
        log.notes = reason_note or ""
        log.failure_reason = outcome
        if coords:
            log.latitude = coords[0]
            log.longitude = coords[1]
        # Stash the photo URL on whichever field the doctype has; older
        # versions don't have a dedicated `photo_url` column.
        if "photo_url" in {f.fieldname for f in frappe.get_meta("Delivery Attempt Log").fields}:
            log.photo_url = photo_url
        else:
            log.notes = (log.notes + f"\nphoto_url: {photo_url}").strip()
        log.flags.ignore_permissions = True
        log.insert(ignore_permissions=True)

        attempt_number = frappe.db.count(
            "Delivery Attempt Log",
            {"delivery_note": delivery_note, "attempt_status": "Attempted"},
        )
        frappe.db.set_value("Delivery Note", delivery_note,
                            "cowberry_delivery_status", "Attempted",
                            update_modified=False)
        frappe.db.commit()
        return ok(data={
            "delivery_status": "Attempted",
            "attempt_log":     log.name,
            "attempt_number":  attempt_number,
        })
    except Exception as e:
        return err("ATTEMPT_FAILED", str(e))


# ---------------------------------------------------------------------------
# §6 reschedule — also writes a Reschedule Log; enforces max_reschedules
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def reschedule(delivery_note=None, new_date=None, reason=None, reason_note=None):
    """Delivery §6 — push delivery to a later date.

    Validation:
      - new_date must be >= today
      - reschedule_count must be < max_reschedules (default 3)
      - returns RESCHEDULE_LIMIT_EXCEEDED at the cap
    """
    try:
        if not new_date:
            return err("VALIDATION_ERROR", "`new_date` is required.", 400)
        if not reason:
            return err("VALIDATION_ERROR", "`reason` is required.", 400)
        if getdate(new_date) < getdate(today()):
            return err("VALIDATION_ERROR",
                       "`new_date` must be today or later.", 400)
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)

        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        try:
            _assert_driver_owns_dn(driver, delivery_note)
        except frappe.PermissionError as pe:
            return err("FORBIDDEN", str(pe), 403)

        current_count = frappe.db.count("Reschedule Log",
                                        {"delivery_note": delivery_note})
        max_resch = frappe.db.get_single_value("Driver Settings",
                                               "max_reschedules") or DEFAULT_MAX_RESCHEDULES
        if current_count >= int(max_resch):
            return err("RESCHEDULE_LIMIT_EXCEEDED",
                       f"This delivery has already been rescheduled {current_count} times "
                       f"(max {max_resch}).", 400)

        log = frappe.new_doc("Reschedule Log")
        log.delivery_note = delivery_note
        log.driver = employee
        log.reason = reason
        log.reschedule_date = new_date
        log.notes = reason_note or ""
        log.flags.ignore_permissions = True
        log.insert(ignore_permissions=True)

        frappe.db.set_value("Delivery Note", delivery_note,
                            {"cowberry_delivery_status": "Rescheduled",
                             "cowberry_reschedule_date": new_date},
                            update_modified=False)
        frappe.db.commit()
        return ok(data={
            "delivery_status":   "Rescheduled",
            "reschedule_log":    log.name,
            "new_date":          str(new_date),
            "reschedule_count":  current_count + 1,
            # Warehouse-manager notification is async (Module 5 spec note
            # mentions it should ping the WM). We don't have an outbound
            # notify chain wired yet, so report false; replace with the
            # real flag once the WM notify channel is in place.
            "wm_notified":       False,
        })
    except Exception as e:
        return err("RESCHEDULE_FAILED", str(e))


# ---------------------------------------------------------------------------
# Document-event handlers (preserved from prior file)
# ---------------------------------------------------------------------------

def on_submit_delivery_note(doc, method):
    """Triggered when a Delivery Note is submitted."""
    _sync_delivery_note(doc)


def on_cancel_delivery_note(doc, method):
    """Triggered when a Delivery Note is cancelled."""
    pass


def _sync_delivery_note(dn):
    try:
        idempotency_key = f"dn-submit-{dn.name}-{dn.modified}"
        if frappe.db.exists("Delivery Sync Log", {"idempotency_key": idempotency_key}):
            return

        sync_log = frappe.new_doc("Delivery Sync Log")
        sync_log.idempotency_key = idempotency_key
        sync_log.delivery_note = dn.name
        sync_log.status = "Pending"
        sync_log.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        pass
