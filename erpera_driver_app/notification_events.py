"""Doc-event handlers that turn ERPNext activity into driver notifications.

Covers the events that happen *to* a driver on the desk side — a trip lands in
their name, ops moves an order, a customer replies. The events a driver causes
themselves (cash collected, payment confirmed, handover accepted) are notified
from inside the API call that causes them, because only that code knows the
amounts involved.

Every handler is defensive: `notify()` never raises, and each handler swallows
its own lookup errors. A notification must not be able to block a Delivery Note
from being cancelled or a trip from being saved.
"""
import frappe

from erpera_driver_app.utils.notifications import notify


# ---------------------------------------------------------------------------
# Resolving the driver behind a document
# ---------------------------------------------------------------------------

def _employee_for_driver(driver):
    """`Delivery Trip.driver` links to Driver, which links to Employee."""
    if not driver:
        return None
    return frappe.db.get_value("Driver", driver, "employee")


def _employee_for_trip(trip_name):
    if not trip_name:
        return None
    return _employee_for_driver(
        frappe.db.get_value("Delivery Trip", trip_name, "driver"))


def _trip_for_dn(delivery_note):
    """The trip a Delivery Note is a stop on, if any."""
    if not delivery_note:
        return None
    return frappe.db.get_value("Delivery Stop", {"delivery_note": delivery_note},
                               "parent")


def _employee_for_dn(delivery_note):
    return _employee_for_trip(_trip_for_dn(delivery_note))


def _acting_user_is(employee):
    """Whether the person doing this is the driver themselves.

    Used to keep a driver from being notified about their own action — ops
    rescheduling an order is news, the driver rescheduling it is not.
    """
    if not employee:
        return False
    return frappe.db.get_value("Employee", employee, "user_id") == frappe.session.user


def _stop_notes(doc):
    """Delivery Notes on a trip, in stop order.

    Read through `get_all_children` rather than a child-table fieldname: the
    rest of this app reaches Delivery Stop by `parent` and never names the
    field, so there is nothing here that would catch it if ERPNext renamed it.
    """
    return [row.get("delivery_note")
            for row in doc.get_all_children("Delivery Stop")
            if row.get("delivery_note")]


# ---------------------------------------------------------------------------
# 1 + 2 — Delivery Trip
# ---------------------------------------------------------------------------

def on_trip_insert(doc, method=None):
    """Event 1 — a trip is created already carrying a driver."""
    try:
        employee = _employee_for_driver(doc.get("driver"))
        if not employee:
            return
        stops = len(_stop_notes(doc))
        notify(
            employee,
            "New trip assigned",
            f"Trip {doc.name} with {stops} stop{'s' if stops != 1 else ''} "
            "has been assigned to you.",
            event_key="trip_assigned",
            reference_doctype="Delivery Trip", reference_name=doc.name,
            action_type="view_trip",
            data={"trip": doc.name, "stops": stops},
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: trip insert")


def on_trip_update(doc, method=None):
    """Events 1, 2 and 4 — reassignment, resequencing, stops pulled.

    Fires on both `on_update` and `on_update_after_submit`, since a Delivery
    Trip is submittable and the manager edits stops on either side of submit.
    """
    try:
        before = doc.get_doc_before_save()
        if not before:
            return

        employee = _employee_for_driver(doc.get("driver"))
        previous = _employee_for_driver(before.get("driver"))

        # Event 1 — the trip changed hands. The new driver is told they have a
        # trip; the old one is told through event 4 that it is no longer theirs.
        if employee != previous:
            if employee:
                stops = len(_stop_notes(doc))
                notify(
                    employee, "New trip assigned",
                    f"Trip {doc.name} with {stops} stop{'s' if stops != 1 else ''} "
                    "has been assigned to you.",
                    event_key="trip_assigned",
                    reference_doctype="Delivery Trip", reference_name=doc.name,
                    action_type="view_trip", data={"trip": doc.name, "stops": stops},
                )
            if previous:
                notify(
                    previous, "Trip reassigned",
                    f"Trip {doc.name} is no longer assigned to you.",
                    event_key="order_cancelled",
                    reference_doctype="Delivery Trip", reference_name=doc.name,
                    data={"trip": doc.name},
                )
            return

        if not employee:
            return

        now_notes = _stop_notes(doc)
        was_notes = _stop_notes(before)
        added = [n for n in now_notes if n not in was_notes]
        removed = [n for n in was_notes if n not in now_notes]

        # Event 4 — stops pulled off the round. Named individually: "2 orders
        # removed" leaves the driver hunting for which parcels to hand back.
        if removed:
            notify(
                employee, "Orders removed from your trip",
                f"{', '.join(removed)} {'has' if len(removed) == 1 else 'have'} "
                f"been removed from trip {doc.name}.",
                event_key="order_cancelled",
                reference_doctype="Delivery Trip", reference_name=doc.name,
                action_type="view_trip",
                data={"trip": doc.name, "removed": ",".join(removed)},
            )

        # Event 2 — stops added, or the same stops in a different order.
        resequenced = (not added and not removed and now_notes != was_notes)
        if added or resequenced:
            if added:
                body = (f"{', '.join(added)} added to trip {doc.name}. "
                        "Check your stop list.")
            else:
                body = f"The stop order on trip {doc.name} has changed."
            notify(
                employee, "Trip updated", body,
                event_key="trip_updated",
                reference_doctype="Delivery Trip", reference_name=doc.name,
                action_type="view_trip",
                data={"trip": doc.name, "added": ",".join(added)},
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: trip update")


# ---------------------------------------------------------------------------
# 3 + 4 — Delivery Note
# ---------------------------------------------------------------------------

def on_dn_update_after_submit(doc, method=None):
    """Event 3 — ops moved the delivery date.

    The driver's own reschedule (`order.reschedule` / `delivery.reschedule`)
    writes the same field, so we compare the acting user against the driver and
    stay quiet when they are the same person.
    """
    try:
        before = doc.get_doc_before_save()
        if not before:
            return
        new_date = doc.get("cowberry_reschedule_date")
        if new_date == before.get("cowberry_reschedule_date"):
            return

        employee = _employee_for_dn(doc.name)
        if not employee or _acting_user_is(employee):
            return

        notify(
            employee, "Order rescheduled",
            f"{doc.name} has been rescheduled to {new_date} by the office.",
            event_key="order_rescheduled",
            reference_doctype="Delivery Note", reference_name=doc.name,
            action_type="view_order",
            data={"delivery_note": doc.name, "new_date": str(new_date or "")},
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: dn reschedule")


def on_dn_cancel(doc, method=None):
    """Event 4 — the order itself was cancelled."""
    try:
        employee = _employee_for_dn(doc.name)
        if not employee or _acting_user_is(employee):
            return
        notify(
            employee, "Order cancelled",
            f"{doc.name} has been cancelled. Do not attempt this delivery.",
            event_key="order_cancelled",
            reference_doctype="Delivery Note", reference_name=doc.name,
            action_type="view_order", data={"delivery_note": doc.name},
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: dn cancel")


# ---------------------------------------------------------------------------
# 7 — chat
# ---------------------------------------------------------------------------

def on_communication_insert(doc, method=None):
    """Event 7 — somebody who is not the driver wrote on one of their orders.

    `chat.send_message` writes the driver's own messages through this same
    doctype, so the sender check is what separates an incoming message from an
    echo of the driver's own.
    """
    try:
        if doc.get("reference_doctype") != "Delivery Note":
            return
        employee = _employee_for_dn(doc.get("reference_name"))
        if not employee:
            return
        if doc.get("sender") == frappe.db.get_value("Employee", employee, "user_id"):
            return

        content = frappe.utils.strip_html(doc.get("content") or "").strip()
        preview = content[:80] + ("…" if len(content) > 80 else "")
        notify(
            employee, "New message from customer",
            preview or f"You have a new message on {doc.reference_name}.",
            event_key="customer_chat_message",
            reference_doctype="Delivery Note", reference_name=doc.reference_name,
            action_type="view_chat",
            data={"delivery_note": doc.reference_name, "message_id": doc.name},
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: chat message")
