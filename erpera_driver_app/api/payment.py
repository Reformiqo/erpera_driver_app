import hashlib
import hmac
import json

import frappe

from erpera_driver_app.utils.response import err, ok


@frappe.whitelist(allow_guest=True)
def get_status(delivery_note):
    try:
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", "Delivery note not found.", 404)
        dn = frappe.get_doc("Delivery Note", delivery_note)
        return ok(data={
            "delivery_note": dn.name,
            "razorpay_payment_status": dn.get("razorpay_payment_status"),
            "razorpay_order_id": dn.get("razorpay_order_id"),
        })
    except Exception as e:
        return err("GET_STATUS_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    try:
        settings = frappe.get_single("Driver Settings")
        webhook_secret = settings.razorpay_webhook_secret

        raw_body = frappe.request.get_data(as_text=True)
        signature = frappe.get_request_header("X-Razorpay-Signature", "")

        if webhook_secret:
            expected = hmac.new(
                webhook_secret.encode(),
                raw_body.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                frappe.local.response["http_status_code"] = 400
                return err("SIGNATURE_INVALID", "Webhook signature verification failed.")

        payload = json.loads(raw_body)
        event = payload.get("event")

        if event == "payment.captured":
            payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
            order_id = payment.get("order_id")
            if order_id:
                frappe.db.set_value(
                    "Delivery Note",
                    {"razorpay_order_id": order_id},
                    "razorpay_payment_status",
                    "Confirmed",
                )
                frappe.db.commit()

        return ok(data={"received": True})
    except Exception as e:
        return err("WEBHOOK_FAILED", str(e))


def poll_pending_razorpay_orders():
    """Scheduler: poll Razorpay for payment status of pending orders."""
    try:
        settings = frappe.get_single("Driver Settings")
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            return

        pending = frappe.get_all(
            "Delivery Note",
            filters={"razorpay_payment_status": ["in", ["Pending", "Created"]], "docstatus": 1},
            fields=["name", "razorpay_order_id"],
            limit=50,
        )

        import requests

        for dn in pending:
            order_id = dn.get("razorpay_order_id")
            if not order_id:
                continue
            try:
                resp = requests.get(
                    f"https://api.razorpay.com/v1/orders/{order_id}",
                    auth=(settings.razorpay_key_id, settings.get_password("razorpay_key_secret")),
                    timeout=10,
                )
                data = resp.json()
                if data.get("status") == "paid":
                    frappe.db.set_value(
                        "Delivery Note", dn["name"], "razorpay_payment_status", "Confirmed"
                    )
            except Exception:
                continue

        frappe.db.commit()
    except Exception:
        pass
