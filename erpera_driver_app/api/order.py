import frappe
from frappe.utils import flt

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import (
    DeliveryNoteNotFoundError,
    OTPInvalidError,
    PaymentNotConfirmedError,
)
from erpera_driver_app.utils.otp import PURPOSE_POD, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Order §§1-2)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_order_detail(delivery_note=None):
    """Order §1 — full order detail for the Stop Detail screen.

    Returns spec shape: delivery_note, customer, customer_address,
    contact_mobile, delivery_status, payment_type, cod_amount,
    otp_status, reschedule_count, stop_sequence, expected_arrival_time,
    items[], special_instructions.

    FORBIDDEN if the DN is not assigned to the caller's active trip.
    """
    try:
        employee = _require_driver()
        if not delivery_note:
            return err("VALIDATION_ERROR", "Query param `delivery_note` is required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)
        dn = frappe.get_doc("Delivery Note", delivery_note)

        # Driver-scope check: the DN must be a stop on a trip assigned to
        # this driver. Trip.driver links to the ERPNext Driver record.
        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        if driver:
            stop_row = frappe.db.sql(
                """SELECT ds.parent AS trip_name, ds.idx, ds.estimated_arrival
                     FROM `tabDelivery Stop` ds
                     JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                    WHERE ds.delivery_note = %s AND dt.driver = %s
                    LIMIT 1""",
                (delivery_note, driver), as_dict=True,
            )
            if not stop_row:
                return err("FORBIDDEN",
                           "This delivery note is not assigned to you.", 403)
            stop_sequence = stop_row[0].idx
            expected_arrival = stop_row[0].estimated_arrival
        else:
            stop_sequence = None
            expected_arrival = None

        payment_type = dn.get("cowberry_payment_method")
        cod_amount = flt(dn.grand_total) if (payment_type or "").upper().startswith("COD") else 0
        reschedule_count = frappe.db.count("Reschedule Log", {"delivery_note": delivery_note})
        # OTP status: derived from cowberry_delivery_status + presence of
        # an outstanding OTP Log. Module 6 will replace this with a
        # proper status read from the DN's otp_attempts custom field.
        otp_status = "Validated" if dn.get("cowberry_delivery_status") == "Delivered" else "Not Requested"

        items = [
            {"item_code": i.item_code, "item_name": i.item_name,
             "qty": i.qty, "uom": i.uom}
            for i in dn.items
        ]
        # special_instructions: prefer DN.custom_order_note if set; fall
        # back to the upstream Sales Order's custom_order_note.
        special = dn.get("custom_order_note")
        if not special and dn.items:
            so_name = dn.items[0].get("against_sales_order")
            if so_name:
                special = frappe.db.get_value("Sales Order", so_name, "custom_order_note")

        return ok(data={
            "delivery_note":         dn.name,
            "customer":              dn.customer_name or dn.customer,
            "customer_address":      dn.address_display,
            "contact_mobile":        dn.contact_mobile,
            "delivery_status":       dn.get("cowberry_delivery_status") or "Pending",
            "payment_type":          payment_type,
            "cod_amount":            cod_amount,
            "otp_status":            otp_status,
            "reschedule_count":      reschedule_count,
            "stop_sequence":         stop_sequence,
            "expected_arrival_time": str(expected_arrival) if expected_arrival else None,
            "items":                 items,
            "special_instructions":  special,
        })
    except Exception as e:
        return err("GET_ORDER_DETAIL_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def get_invoice_pdf(delivery_note=None):
    """Order §2 — return a printable URL for the Sales Invoice created
    from this DN's `submit_proof` step. NOT_FOUND before delivery.
    """
    try:
        _require_driver()
        if not delivery_note:
            return err("VALIDATION_ERROR", "Query param `delivery_note` is required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)
        # SI linked via Sales Invoice Item.dn_detail → Delivery Note Item.name
        # or via against_sales_invoice on the DN itself. Try both paths.
        si = frappe.db.sql(
            """SELECT DISTINCT si.name
                 FROM `tabSales Invoice Item` sii
                 JOIN `tabSales Invoice` si ON si.name = sii.parent
                WHERE sii.dn_detail IN (
                    SELECT name FROM `tabDelivery Note Item` WHERE parent = %s
                )
                  AND si.docstatus = 1
                LIMIT 1""",
            (delivery_note,),
        )
        si_name = si[0][0] if si else None
        if not si_name:
            return err("NOT_FOUND",
                       "Sales Invoice has not been created yet. Complete the delivery first.",
                       404)
        # Use the bench's standard Cowberry print format if available, else stock
        fmt = "Cowberry Invoice" if frappe.db.exists("Print Format", "Cowberry Invoice") else "Standard"
        url = (
            "/api/method/frappe.utils.print_format.download_pdf"
            f"?doctype=Sales+Invoice&name={frappe.utils.quoted(si_name)}"
            f"&format={frappe.utils.quoted(fmt)}"
        )
        return ok(data={"invoice_pdf_url": url})
    except Exception as e:
        return err("GET_INVOICE_PDF_FAILED", str(e))


# ---------------------------------------------------------------------------
# Legacy endpoints (back-compat — same module so existing clients keep working)
# ---------------------------------------------------------------------------

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

        # Auto-create Sales Invoice from the Delivery Note (FRD §6.2.2 /
        # §6.3 row 12). Best-effort: a failure here doesn't roll back the
        # PoD — the SI can be created later via the standard ERPNext
        # "Create > Sales Invoice" button on the DN. Idempotent: skips
        # creation when one already exists for the same DN.
        sales_invoice = _ensure_sales_invoice(dn)

        frappe.db.commit()

        invoice_pdf_url = None
        if sales_invoice:
            invoice_pdf_url = (
                "/api/method/frappe.utils.print_format.download_pdf"
                f"?doctype=Sales+Invoice&name={sales_invoice}&format=Standard"
            )

        return ok(data={
            "delivery_note": dn.name,
            "delivery_status": "Delivered",
            "driver_collection": driver_collection,
            "current_day_collected": current_day_collected,
            "daily_limit": daily_limit,
            "sales_invoice": sales_invoice,
            "invoice_pdf_url": invoice_pdf_url,
            "message": "Proof submitted.",
        })
    except (DeliveryNoteNotFoundError, OTPInvalidError) as e:
        return e.to_response()
    except Exception as e:
        return err("SUBMIT_PROOF_FAILED", str(e))


def _ensure_sales_invoice(dn):
    """Create-and-submit a Sales Invoice from the DN, if missing.

    Returns the Sales Invoice name (whether pre-existing or freshly
    created) or None if creation failed. Idempotent: a DN that already
    has a billed Sales Invoice short-circuits to the existing name so
    a retried PoD doesn't double-bill the customer.
    """
    existing = frappe.db.sql(
        """
        SELECT DISTINCT si.name
        FROM `tabSales Invoice` si
        JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
        WHERE sii.delivery_note = %s AND si.docstatus < 2
        LIMIT 1
        """,
        dn.name,
    )
    if existing:
        si_name = existing[0][0]
        # Persist the link on the DN custom field so the next API
        # response can render it without rerunning this query.
        if dn.get("sales_invoice") != si_name:
            try:
                frappe.db.set_value(
                    "Delivery Note", dn.name, "sales_invoice", si_name
                )
            except Exception:
                pass
        return si_name

    try:
        from erpnext.stock.doctype.delivery_note.delivery_note import (
            make_sales_invoice,
        )
        si = make_sales_invoice(dn.name)
        si.set_missing_values()
        si.insert(ignore_permissions=True)
        si.submit()
        try:
            frappe.db.set_value(
                "Delivery Note", dn.name, "sales_invoice", si.name
            )
        except Exception:
            pass
        return si.name
    except Exception as e:
        frappe.log_error(
            title=f"submit_proof: SI auto-creation failed for {dn.name}",
            message=str(e),
        )
        return None


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
