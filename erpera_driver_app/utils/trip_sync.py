"""Keep Delivery Trip / Delivery Stop in sync with driver-app deliveries.

The driver app records progress on the Delivery Note's
`cowberry_delivery_status` custom field. ERPNext's own
DeliveryTrip.update_status() keys off `Delivery Stop.visited` instead, so
without this bridge a trip whose every order is Delivered stays on the
on-submit default "Scheduled".

cowberry_app already does the same thing from its own OTP flow
(cowberry_app.api.otp._complete_delivery) — this is the erpera_driver_app
side of the same bridge.
"""

import frappe


# A stop counts as "work has started" when its Delivery Note sits in any of
# these. Both status fields are listed because the two driver apps write
# different ones: cowberry_app writes `delivery_status`, erpera_driver_app
# writes `cowberry_delivery_status`. Same concept, different vocabulary —
# "In Transit" there is "Out for Delivery" here.
IN_PROGRESS_STATUSES = {
    "Out for Pickup",
    "Picked Up",
    "In Transit",        # cowberry_app
    "Out for Delivery",  # erpera_driver_app
    "At Location",
    "OTP Pending",       # cowberry_app
}

# Delivery Note status fields to consult, in no particular order — a stop is
# in progress if either one says so.
_DN_STATUS_FIELDS = ("delivery_status", "cowberry_delivery_status")


def _any_stop_in_progress(trip):
    """True when at least one live stop on the trip has started moving."""
    available = {f.fieldname for f in frappe.get_meta("Delivery Note").fields}
    fields = [f for f in _DN_STATUS_FIELDS if f in available]
    if not fields:
        return False
    cols = ", ".join(f"dn.`{f}`" for f in fields)
    rows = frappe.db.sql(
        f"""SELECT {cols}
              FROM `tabDelivery Stop` ds
              JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE ds.parent = %s AND dn.docstatus = 1""",
        (trip,),
        as_dict=True,
    )
    return any(r.get(f) in IN_PROGRESS_STATUSES for r in rows for f in fields)


def _recompute_trip_status(trip):
    """Roll the trip's status up from its stops. Returns the new status."""
    trip_doc = frappe.get_doc("Delivery Trip", trip)
    # ERPNext's own rule first: docstatus → Draft/Scheduled/Cancelled, then
    # all(visited) → Completed, any(visited) → In Transit.
    trip_doc.update_status()
    # ERPNext only leaves "Scheduled" once a stop is marked visited, which
    # happens at delivery. A driver who has picked up and is on the way is
    # plainly no longer "Scheduled" — promote those trips.
    #
    # Deliberately an upgrade only: Draft, Cancelled, In Transit and
    # Completed are all left exactly as ERPNext computed them.
    if (trip_doc.docstatus == 1 and trip_doc.status == "Scheduled"
            and _any_stop_in_progress(trip)):
        trip_doc.db_set("status", "In Transit", update_modified=False)
    return trip_doc.status


def sync_trip_status(delivery_note):
    """Recompute the status of every trip carrying this Delivery Note,
    without touching `visited`.

    Used for non-delivery transitions (pickup, on the way, arrived) so the
    trip reflects that work has started. Best-effort; never raises.
    """
    if not delivery_note:
        return None
    try:
        trips = frappe.db.sql_list(
            """SELECT DISTINCT parent FROM `tabDelivery Stop`
                WHERE delivery_note = %s""",
            (delivery_note,),
        )
        status = None
        for trip in trips:
            status = _recompute_trip_status(trip)
        if trips:
            frappe.db.commit()
        return status
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not sync Delivery Trip status",
            message=f"delivery_note={delivery_note}\n{frappe.get_traceback()}",
        )
        return None


def trip_for_delivery_note(delivery_note):
    """Return the submitted Delivery Trip carrying this Delivery Note.

    Delivery Note has a `delivery_trip` column but nothing on this bench
    populates it, so the Delivery Stop table is the only reliable link.
    When a DN sits on more than one trip (a retry after a failed attempt),
    the most recent departure wins.
    """
    if not delivery_note:
        return None
    rows = frappe.db.sql(
        """SELECT ds.parent
             FROM `tabDelivery Stop` ds
             JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
            WHERE ds.delivery_note = %s AND dt.docstatus = 1
            ORDER BY dt.departure_time DESC
            LIMIT 1""",
        (delivery_note,),
    )
    return rows[0][0] if rows else None


def mark_stop_visited(delivery_note):
    """Tick `visited` on the Delivery Stop rows pointing at this Delivery
    Note, then let ERPNext recompute the parent trip's status.

    Best-effort: never raises. A delivery must not fail because the trip
    roll-up couldn't be written — the caller has already committed the DN,
    and trip.get_trips computes its counters from the DN status directly, so
    the API response stays correct either way.

    Returns the trip's new status, or None when the DN isn't on a trip (or
    every matching stop was already visited).
    """
    if not delivery_note:
        return None
    try:
        # Only trips that still have an unvisited stop for this DN — keeps
        # a repeat call (retry, re-delivery) a no-op.
        trips = frappe.db.sql_list(
            """SELECT DISTINCT parent
                 FROM `tabDelivery Stop`
                WHERE delivery_note = %s AND IFNULL(visited, 0) = 0""",
            (delivery_note,),
        )
        if not trips:
            return None

        frappe.db.sql(
            """UPDATE `tabDelivery Stop`
                  SET visited = 1
                WHERE delivery_note = %s""",
            (delivery_note,),
        )

        status = None
        for trip in trips:
            status = _recompute_trip_status(trip)

        frappe.db.commit()
        return status
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not mark Delivery Stop visited",
            message=f"delivery_note={delivery_note}\n{frappe.get_traceback()}",
        )
        return None
