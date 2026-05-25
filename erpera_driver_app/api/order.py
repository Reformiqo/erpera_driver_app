import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import (
    DeliveryNoteNotFoundError,
    OTPInvalidError,
    PaymentNotConfirmedError,
)
from erpera_driver_app.utils.otp import PURPOSE_POD, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist()
def get_order(delivery_note):
    try:
        _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()
        dn = frappe.get_doc("Delivery Note", delivery_note)
        items = [
            {
                "item_code": i.item_code,
                "item_name": i.item_name,
                "qty": i.qty,
                "rate": i.rate,
                "amount": i.amount,
            }
            for i in dn.items
        ]
        return ok(data={
            "delivery_note": dn.name,
            "customer": dn.customer,
            "customer_name": dn.customer_name,
            "posting_date": str(dn.posting_date),
            "status": dn.status,
            "payment_method": dn.get("cowberry_payment_method"),
            "razorpay_payment_status": dn.get("razorpay_payment_status"),
            "items": items,
            "grand_total": dn.grand_total,
        })
    except (DeliveryNoteNotFoundError,) as e:
        return e.to_response()
    except Exception as e:
        return err("GET_ORDER_FAILED", str(e))


@frappe.whitelist()
def send_delivery_otp(delivery_note, customer_mobile=None):
    try:
        _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        dn = frappe.get_doc("Delivery Note", delivery_note)

        # HARD GATE: COD-Online requires confirmed Razorpay payment
        payment_method = dn.get("cowberry_payment_method")
        if payment_method == "COD-Online":
            if dn.get("razorpay_payment_status") != "Confirmed":
                raise PaymentNotConfirmedError()

        mobile = customer_mobile
        if not mobile:
            mobile = frappe.db.get_value("Customer", dn.customer, "mobile_no")

        log_name = dispatch_otp_v2(
            purpose=PURPOSE_POD,
            reference_doctype="Delivery Note",
            reference_name=delivery_note,
            recipient_mobile=mobile or "",
        )
        return ok(data={"log_name": log_name, "message": "OTP sent to customer."})
    except (DeliveryNoteNotFoundError, PaymentNotConfirmedError) as e:
        return e.to_response()
    except Exception as e:
        return err("SEND_DELIVERY_OTP_FAILED", str(e))


@frappe.whitelist()
def submit_proof(delivery_note, otp_log_name, otp, proof_image=None, signature=None, latitude=None, longitude=None):
    try:
        _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        validate_otp_v2(log_name=otp_log_name, otp_input=otp)

        dn = frappe.get_doc("Delivery Note", delivery_note)
        if proof_image:
            dn.cowberry_proof_image = proof_image
        if signature:
            dn.cowberry_signature = signature
        if latitude and longitude:
            dn.cowberry_delivery_lat = latitude
            dn.cowberry_delivery_lng = longitude
        dn.cowberry_delivery_status = "Delivered"
        dn.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message": "Proof submitted.", "delivery_note": dn.name})
    except (DeliveryNoteNotFoundError, OTPInvalidError) as e:
        return e.to_response()
    except Exception as e:
        return err("SUBMIT_PROOF_FAILED", str(e))


@frappe.whitelist()
def reschedule(delivery_note, reason, reschedule_date, notes=None):
    try:
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        log = frappe.new_doc("Cowberry Reschedule Log")
        log.delivery_note = delivery_note
        log.driver = employee
        log.reason = reason
        log.reschedule_date = reschedule_date
        log.notes = notes or ""
        log.insert(ignore_permissions=True)

        dn = frappe.get_doc("Delivery Note", delivery_note)
        dn.cowberry_delivery_status = "Rescheduled"
        dn.cowberry_reschedule_date = reschedule_date
        dn.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message": "Delivery rescheduled.", "log": log.name})
    except (DeliveryNoteNotFoundError,) as e:
        return e.to_response()
    except Exception as e:
        return err("RESCHEDULE_FAILED", str(e))
