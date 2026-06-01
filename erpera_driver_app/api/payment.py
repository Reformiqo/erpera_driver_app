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
            "razorpay_confirmed_at": (
                str(dn.get("razorpay_confirmed_at"))
                if dn.get("razorpay_confirmed_at")
                else None
            ),
            "razorpay_payment_entry": dn.get("razorpay_payment_entry"),
            "polling_interval_seconds": 5,
        })
    except Exception as e:
        return err("GET_STATUS_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def razorpay_webhook():
    """Razorpay → ERPNext webhook receiver (FRD §6.4).

    Verifies the HMAC-SHA256 signature against the shared
    `razorpay_webhook_secret`, then on `payment.captured` events:
      1. Flips `razorpay_payment_status` to "Confirmed" on the matching DN.
      2. Stamps `razorpay_confirmed_at` so the polling driver app can
         display the receipt timestamp.
      3. Attempts to create a Payment Entry against the DN's linked
         Sales Order (or Sales Invoice if one already exists),
         recording the PE name in `razorpay_payment_entry`.

    Step 3 is best-effort — without a configured Razorpay payments
    account on the company the PE creation logs but doesn't fail the
    webhook, since the status flip alone unblocks the driver-app gate
    (FRD §6.4 step 7).
    """
    try:
        settings = frappe.get_single("Driver Settings")
        webhook_secret = settings.get_password("razorpay_webhook_secret") \
            if settings.get("razorpay_webhook_secret") else None

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
                return err(
                    "SIGNATURE_INVALID",
                    "Webhook signature verification failed.",
                )

        payload = json.loads(raw_body)
        event = payload.get("event")

        if event == "payment.captured":
            payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
            order_id = payment.get("order_id")
            paid_amount = float(payment.get("amount", 0)) / 100.0  # paise → rupees
            payment_id = payment.get("id")

            dn_name = None
            if order_id:
                dn_name = frappe.db.get_value(
                    "Delivery Note", {"razorpay_order_id": order_id}, "name"
                )

            if dn_name:
                frappe.db.set_value(
                    "Delivery Note",
                    dn_name,
                    {
                        "razorpay_payment_status": "Confirmed",
                        "razorpay_confirmed_at": frappe.utils.now_datetime(),
                    },
                )
                pe_name = _create_razorpay_payment_entry(
                    dn_name, paid_amount, payment_id
                )
                if pe_name:
                    frappe.db.set_value(
                        "Delivery Note",
                        dn_name,
                        "razorpay_payment_entry",
                        pe_name,
                    )
                frappe.db.commit()
            else:
                frappe.log_error(
                    title="Razorpay webhook: no matching Delivery Note",
                    message=(
                        f"order_id={order_id} payment_id={payment_id} "
                        f"amount={paid_amount}"
                    ),
                )

        return ok(data={"received": True})
    except Exception as e:
        return err("WEBHOOK_FAILED", str(e))


def _create_razorpay_payment_entry(dn_name, amount, razorpay_payment_id):
    """Best-effort PE creation against the DN's source Sales Order / SI.

    Walks Delivery Note → first DN Item.against_sales_order or .si_detail
    to find the receivable doc, then uses ERPNext's standard
    `get_payment_entry` helper to scaffold a Customer-type PE and
    submits it. Returns the PE name on success, None otherwise — the
    webhook continues either way.
    """
    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import (
            get_payment_entry,
        )
    except Exception:
        return None

    try:
        dn = frappe.get_doc("Delivery Note", dn_name)
        ref_doctype = None
        ref_name = None
        for item in dn.items:
            if item.get("against_sales_invoice"):
                ref_doctype = "Sales Invoice"
                ref_name = item.against_sales_invoice
                break
            if item.get("against_sales_order"):
                ref_doctype = "Sales Order"
                ref_name = item.against_sales_order
                break
        if not ref_name:
            return None

        pe = get_payment_entry(ref_doctype, ref_name, party_amount=amount)
        pe.reference_no = razorpay_payment_id or pe.reference_no or dn_name
        pe.reference_date = frappe.utils.today()
        if frappe.db.exists("Mode of Payment", "Razorpay"):
            pe.mode_of_payment = "Razorpay"
        pe.insert(ignore_permissions=True)
        pe.submit()
        return pe.name
    except Exception as e:
        frappe.log_error(
            title=f"Razorpay webhook: PE creation failed for {dn_name}",
            message=str(e),
        )
        return None


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
