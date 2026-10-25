"""Push delivery progress back to the Cowberry customer app.

cowberry_app owns the outbound call: `update_sales_order_status` stamps
`custom_order_status` on every Sales Order behind a Delivery Note and
enqueues `push_order_status_to_cowberry`, which POSTs the mapped status to
the Cowberry web API (and logs/retries through Cowberry Order Sync Log).

On the desk that pipeline runs off the Delivery Note doc events. The driver
app writes delivery progress with `frappe.db.set_value`, so no doc event
fires and the push has to be made by hand.
"""

import frappe


def push_order_status(delivery_note, status):
    """Stamp `custom_order_status` on the DN and push it to Cowberry.

    `status` is an ERPNext order status — a key of
    `cowberry_app.doc_events.utils.STATUS_MAP` ("Delivered", "Out for
    Delivery", ...).

    Best-effort: never raises. A delivery must not fail because the customer
    app couldn't be told about it, and cowberry_app retries failed pushes
    from its own sync log.
    """
    if not delivery_note:
        return
    try:
        from cowberry_app.doc_events.utils import update_sales_order_status

        frappe.db.set_value(
            "Delivery Note", delivery_note, "custom_order_status", status,
            update_modified=False,
        )
        update_sales_order_status(delivery_note, status)
        frappe.db.commit()
    except ImportError:
        # Bench without cowberry_app — there's nothing to push to.
        pass
    except Exception:
        frappe.log_error(
            title="erpera_driver_app: could not push order status to Cowberry",
            message=(f"delivery_note={delivery_note} status={status}\n"
                     f"{frappe.get_traceback()}"),
        )
