import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist()
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


@frappe.whitelist()
def send_message(thread_id, content):
    try:
        employee = _require_driver()
        comm = frappe.new_doc("Communication")
        comm.communication_type = "Chat"
        comm.reference_doctype = "Communication"
        comm.reference_name = thread_id
        comm.sender = frappe.session.user
        comm.content = content
        comm.sent_or_received = "Sent"
        comm.insert(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message_id": comm.name})
    except Exception as e:
        return err("SEND_MESSAGE_FAILED", str(e))
