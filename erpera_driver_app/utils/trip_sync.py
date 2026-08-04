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
            # Reuse ERPNext's own all()/any() rule rather than re-deriving it,
            # so Draft and Cancelled trips keep their docstatus-driven status.
            trip_doc = frappe.get_doc("Delivery Trip", trip)
            trip_doc.update_status()
            status = trip_doc.status

        frappe.db.commit()
        return status
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not mark Delivery Stop visited",
            message=f"delivery_note={delivery_note}\n{frappe.get_traceback()}",
        )
        return None
