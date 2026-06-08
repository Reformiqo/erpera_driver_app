import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok

VALID_MESSAGE_TYPES = {"quick", "free_text", "system"}


def _assert_driver_owns_dn(driver, delivery_note):
    """Same scope check the delivery + order modules use."""
    if not driver:
        return
    row = frappe.db.sql(
        """SELECT 1
             FROM `tabDelivery Stop` ds
             JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
            WHERE ds.delivery_note = %s AND dt.driver = %s
            LIMIT 1""",
        (delivery_note, driver),
    )
    if not row:
        raise frappe.PermissionError(
            "FORBIDDEN: this delivery note is not on your trip."
        )


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Chat §§1-2). Driver identity is
# stripped from the customer-facing payload — the spec calls this
# "anonymous messaging".
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["POST"])
def send_message(delivery_note=None, message_type="free_text", content=None,
                 # Legacy alias for the prior thread_id-keyed contract:
                 thread_id=None):
    """§1 Send a customer-bound message keyed off the DN.

    Spec body: {delivery_note, message_type, content}. message_type
    is one of 'quick' | 'free_text' | 'system' — primarily a hint
    for the receiver UI; we store it as a tag on the Communication
    row. Driver identity is not embedded in the content delivered to
    the customer (anonymous messaging).
    """
    try:
        employee = _require_driver()
        if not content:
            return err("VALIDATION_ERROR", "`content` is required.", 400)
        if message_type not in VALID_MESSAGE_TYPES:
            return err("VALIDATION_ERROR",
                       f"`message_type` must be one of {sorted(VALID_MESSAGE_TYPES)}.",
                       400)
        ref_dt = "Delivery Note" if delivery_note else "Communication"
        ref_name = delivery_note or thread_id
        if not ref_name:
            return err("VALIDATION_ERROR",
                       "Either `delivery_note` or `thread_id` is required.", 400)
        if ref_dt == "Delivery Note":
            if not frappe.db.exists("Delivery Note", ref_name):
                return err("NOT_FOUND",
                           f"Delivery Note '{ref_name}' not found.", 404)
            from erpera_driver_app.api.trip import _driver_record
            try:
                _assert_driver_owns_dn(_driver_record(employee), ref_name)
            except frappe.PermissionError as pe:
                return err("FORBIDDEN", str(pe), 403)

        comm = frappe.new_doc("Communication")
        # Frappe v16 split the Communication doctype:
        #   communication_type: Communication | Automated Message
        #   communication_medium: Email | Chat | Phone | SMS | Event | ...
        # The spec's message_type ("quick"/"free_text"/"system") is a
        # UI hint; we don't persist it because neither Frappe column
        # accepts those values. The Flutter client uses it client-side
        # to render the right bubble style.
        comm.communication_type = "Communication"
        comm.communication_medium = "Chat"
        comm.reference_doctype = ref_dt
        comm.reference_name = ref_name
        comm.sender = frappe.session.user
        comm.content = content
        comm.sent_or_received = "Sent"
        comm.flags.ignore_permissions = True
        comm.insert(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={
            "message_id": comm.name,
            "sent_at":    str(comm.creation),
        })
    except Exception as e:
        return err("SEND_MESSAGE_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def get_messages(delivery_note=None, thread_id=None):
    """§2 Full chat history for a DN, ordered by sent_at ascending.
    Spec key the Flutter client expects: `messages[]` with
    {message_id, sender, content, sent_at}.
    """
    try:
        _require_driver()
        ref_dt = "Delivery Note" if delivery_note else "Communication"
        ref_name = delivery_note or thread_id
        if not ref_name:
            return err("VALIDATION_ERROR",
                       "Either `delivery_note` or `thread_id` is required.", 400)
        rows = frappe.db.sql(
            """SELECT name, sender, content, creation, sent_or_received
                 FROM `tabCommunication`
                WHERE reference_doctype = %s AND reference_name = %s
                ORDER BY creation ASC
                LIMIT 200""",
            (ref_dt, ref_name), as_dict=True,
        )
        messages = []
        for r in rows:
            messages.append({
                "message_id": r["name"],
                # Anonymise: 'Driver' for outbound, 'Customer' for inbound.
                "sender":     "Driver" if r["sent_or_received"] == "Sent" else "Customer",
                "content":    r["content"],
                "sent_at":    str(r["creation"]),
            })
        return ok(data={"messages": messages})
    except Exception as e:
        return err("GET_MESSAGES_FAILED", str(e))


# ---------------------------------------------------------------------------
# Legacy thread_id-keyed contract — kept for back-compat
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_thread(thread_id):
    try:
        _require_driver()
        if not frappe.db.exists("Communication", {"name": thread_id}):
            return err("NOT_FOUND", "Thread not found.", 404)
        messages = frappe.get_all(
            "Communication",
            filters={"reference_name": thread_id},
            fields=["name", "sender", "content", "creation", "sent_or_received"],
            order_by="creation asc",
            limit=100,
        )
        return ok(data={"thread_id": thread_id, "messages": messages})
    except Exception as e:
        return err("GET_THREAD_FAILED", str(e))
