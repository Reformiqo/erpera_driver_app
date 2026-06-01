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
def submit_proof(
    delivery_note,
    otp_log_name,
    otp,
    proof_image=None,
    signature=None,
    latitude=None,
    longitude=None,
    cod_collected_amount=None,
    payment_method=None,
):
    """Atomic delivery confirmation (FRD §6.2.2 / §9.10 example 1).

    Steps performed in one transaction:
      1. Validate the customer OTP (10-min window, single-use).
      2. Stamp the DN with proof image, signature, GPS, status=Delivered.
      3. For COD orders, increment Employee.current_day_collected_amount
         and roll the cash into today's Driver Collection.
      4. Return the linked Driver Collection, current daily total, and
         the driver's daily limit so the Flutter app can render the
         post-delivery state without an extra round-trip.

    [cod_collected_amount] + [payment_method] are required when the
    DN's `cowberry_payment_method` starts with "COD". The amount must
    equal `cod_amount` exactly (FRD §10.3 row 2 — no partial payments
    in V3); a mismatch returns COD_AMOUNT_MISMATCH.
    """
    try:
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        validate_otp_v2(log_name=otp_log_name, otp_input=otp)

        dn = frappe.get_doc("Delivery Note", delivery_note)
        payment_type = dn.get("cowberry_payment_method") or ""
        is_cod = payment_type.startswith("COD")

        if is_cod:
            expected = float(dn.get("cod_amount") or dn.grand_total or 0)
            collected = float(cod_collected_amount or 0)
            if abs(expected - collected) > 0.01:
                return err(
                    "COD_AMOUNT_MISMATCH",
                    f"Collected amount {collected} does not equal expected {expected}.",
                )
            dn.cod_collected_amount = collected
            dn.cod_collection_timestamp = frappe.utils.now_datetime()
            if payment_method:
                dn.cod_payment_method = payment_method

        if proof_image:
            dn.cowberry_proof_image = proof_image
        if signature:
            dn.cowberry_signature = signature
        if latitude and longitude:
            dn.cowberry_delivery_lat = latitude
            dn.cowberry_delivery_lng = longitude
        dn.cowberry_delivery_status = "Delivered"
        dn.save(ignore_permissions=True)

        driver_collection = None
        current_day_collected = 0.0
        daily_limit = 0.0
        if is_cod:
            driver_collection = _roll_into_collection(employee, dn)
            emp = frappe.get_doc("Employee", employee)
            current = float(emp.get("current_day_collected_amount") or 0)
            new_total = current + collected
            emp.current_day_collected_amount = new_total
            emp.current_day_collected_date = frappe.utils.today()
            emp.save(ignore_permissions=True)
            current_day_collected = new_total
            daily_limit = float(emp.get("daily_collection_limit") or 0)

        frappe.db.commit()
        return ok(data={
            "delivery_note": dn.name,
            "delivery_status": "Delivered",
            "driver_collection": driver_collection,
            "current_day_collected": current_day_collected,
            "daily_limit": daily_limit,
            "message": "Proof submitted.",
        })
    except (DeliveryNoteNotFoundError, OTPInvalidError) as e:
        return e.to_response()
    except Exception as e:
        return err("SUBMIT_PROOF_FAILED", str(e))


def _roll_into_collection(employee, dn):
    """Append the COD payment onto today's open Driver Collection.

    Creates one if none exists for the day. Updates total_cash /
    total_online based on the captured payment method. The CCD Order
    Item child row is appended so the breakdown table on the
    Collection screen stays in sync.
    """
    today = frappe.utils.today()
    name = frappe.db.get_value(
        "Driver Collection",
        {"driver": employee, "collection_date": today, "docstatus": 0},
        "name",
    )
    if name:
        col = frappe.get_doc("Driver Collection", name)
    else:
        col = frappe.new_doc("Driver Collection")
        col.driver = employee
        col.collection_date = today
        col.status = "Open"

    amount = float(dn.get("cod_collected_amount") or 0)
    method = (dn.get("cod_payment_method") or "Cash").lower()
    if method.startswith("upi") or method.startswith("online"):
        col.total_online = float(col.get("total_online") or 0) + amount
    else:
        col.total_cash = float(col.get("total_cash") or 0) + amount

    col.append("order_breakdown", {
        "delivery_note": dn.name,
        "customer": dn.customer,
        "customer_name": dn.customer_name,
        "payment_method": dn.get("cod_payment_method") or "Cash",
        "cash_amount": amount if not method.startswith(("upi", "online")) else 0,
        "online_amount": amount if method.startswith(("upi", "online")) else 0,
        "total_amount": amount,
        "status": "Delivered",
    })

    if col.name:
        col.save(ignore_permissions=True)
    else:
        col.insert(ignore_permissions=True)
    return col.name


@frappe.whitelist()
def reschedule(delivery_note, reason, reschedule_date, notes=None):
    try:
        employee = _require_driver()
        if not frappe.db.exists("Delivery Note", delivery_note):
            raise DeliveryNoteNotFoundError()

        log = frappe.new_doc("Reschedule Log")
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


@frappe.whitelist()
def get_history(from_date=None, to_date=None, payment_type=None, limit=50, offset=0):
    """Past deliveries for the logged-in driver (FRD §6 reports surface).

    Returns delivered orders only — drivers see their own completed work
    grouped by trip + payment mode. Filtered server-side via the
    `Delivery Stop`/`Delivery Trip` join because Delivery Note has no
    direct driver field; permissions on Delivery Trip ensure a driver
    cannot read another driver's rows.

    Query params:
        from_date, to_date  ISO YYYY-MM-DD — bounds on posting_date.
        payment_type        "Prepaid" | "COD-Cash" | "COD-Online" — narrow.
        limit, offset       Pagination.

    Response data shape:
        {
            "entries": [
                {
                    "order_id": "DN-...",
                    "customer_name": "...",
                    "trip_id": "DT-...",
                    "delivered_at": "2026-04-24 14:22:11",
                    "payment_type": "Prepaid" | "COD-Cash" | "COD-Online",
                    "amount": 1250.00
                },
                ...
            ],
            "total_count": <int>,
            "prepaid_count": <int>,
            "cod_count": <int>,
            "prepaid_amount": <float>,
            "cod_amount": <float>,
        }
    """
    try:
        employee = _require_driver()

        conditions = ["dn.docstatus = 1", "dn.cowberry_delivery_status = 'Delivered'"]
        params = {"employee": employee}

        if from_date:
            conditions.append("dn.posting_date >= %(from_date)s")
            params["from_date"] = from_date
        if to_date:
            conditions.append("dn.posting_date <= %(to_date)s")
            params["to_date"] = to_date
        if payment_type:
            conditions.append("dn.cowberry_payment_method = %(payment_type)s")
            params["payment_type"] = payment_type

        params["limit"] = int(limit)
        params["offset"] = int(offset)

        where = " AND ".join(conditions)
        rows = frappe.db.sql(
            f"""
            SELECT
                dn.name             AS order_id,
                dn.customer_name    AS customer_name,
                dt.name             AS trip_id,
                dn.modified AS delivered_at,
                dn.cowberry_payment_method AS payment_type,
                dn.grand_total      AS amount
            FROM `tabDelivery Note` dn
            JOIN `tabDelivery Stop` ds ON ds.delivery_note = dn.name
            JOIN `tabDelivery Trip` dt ON ds.parent = dt.name
            WHERE dt.driver = %(employee)s AND {where}
            ORDER BY delivered_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
            as_dict=True,
        )

        prepaid_count = 0
        cod_count = 0
        prepaid_amount = 0.0
        cod_amount = 0.0
        for row in rows:
            amount = float(row.get("amount") or 0)
            if (row.get("payment_type") or "").lower().startswith("prepaid"):
                prepaid_count += 1
                prepaid_amount += amount
            else:
                cod_count += 1
                cod_amount += amount
            # Normalise datetime to ISO string for transport.
            if row.get("delivered_at") is not None:
                row["delivered_at"] = str(row["delivered_at"])
            row["amount"] = amount

        return ok(data={
            "entries": rows,
            "total_count": len(rows),
            "prepaid_count": prepaid_count,
            "cod_count": cod_count,
            "prepaid_amount": prepaid_amount,
            "cod_amount": cod_amount,
        })
    except Exception as e:
        return err("GET_HISTORY_FAILED", str(e))
