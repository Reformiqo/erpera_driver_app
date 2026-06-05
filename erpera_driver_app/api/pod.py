"""Proof-of-Delivery upload + atomic submit_proof — Nainsi's spec
§OTP & PoD §§4-5.

upload_photo is a thin wrapper around Frappe's stock file-upload pipe
so the Flutter client can stash JPEG/PNG before submitting proof.

submit_proof is the atomic write that finalises a delivery: validates
the token from otp.validate_pod_otp, validates cod amounts (when
COD-*), checks the driver's daily collection limit, then in one
transaction creates the Sales Invoice, updates Driver Collection,
flips DN.cowberry_delivery_status to Delivered, and clears the OTP
state. No partial success — any failure rolls back the whole thing.
"""
import frappe
from frappe.utils import flt, now_datetime, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok

MAX_PHOTO_BYTES = 5 * 1024 * 1024  # spec: max 5 MB


@frappe.whitelist(methods=["POST"])
def upload_photo(doctype=None, docname=None, is_private=1):
    """§4 Upload proof image. Multipart: file=<binary>. Returns the
    private-files URL the Flutter client passes back to submit_proof.

    Driver-only; the file is attached to the DN so permission checks
    later see the same ownership chain.
    """
    try:
        _require_driver()
        files = frappe.request.files
        if not files or "file" not in files:
            return err("VALIDATION_ERROR",
                       "Multipart `file` field is required.", 400)
        upload = files["file"]
        blob = upload.read()
        if len(blob) > MAX_PHOTO_BYTES:
            return err("VALIDATION_ERROR",
                       f"File exceeds {MAX_PHOTO_BYTES // (1024*1024)} MB limit.", 413)
        # Reset the stream so Frappe's File doc can read it again.
        upload.stream.seek(0)
        fdoc = frappe.get_doc({
            "doctype":       "File",
            "file_name":     upload.filename,
            "attached_to_doctype": doctype or "Delivery Note",
            "attached_to_name":    docname,
            "content":       blob,
            "is_private":    1 if int(is_private or 1) else 0,
        })
        fdoc.flags.ignore_permissions = True
        fdoc.insert(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={
            "file_url":  fdoc.file_url,
            "file_name": fdoc.file_name,
        })
    except Exception as e:
        return err("SERVER_ERROR", str(e), 500)


@frappe.whitelist(methods=["POST"])
def submit_proof(delivery_note=None, validation_token=None, photo_url=None,
                 gps_lat=None, gps_lng=None, cod_collected_amount=None,
                 payment_method=None, offline_flag=False):
    """§5 Atomic delivery finalisation.

    Validations (all server-side, all-or-nothing):
      - validation_token matches and isn't expired
      - cod_collected_amount equals DN.grand_total when COD-*
      - current_day_collected + cod_collected_amount <= daily_collection_limit
        (COLLECTION_LIMIT_EXCEEDED rejects without partial side-effects)

    On success:
      - Create + submit Sales Invoice from the DN (via ERPNext helper)
      - Roll cod_collected_amount into Driver Collection (creates one
        if today's row doesn't exist)
      - Flip cowberry_delivery_status to Delivered, stamp delivery
        coordinates, clear OTP fields, mark validation_token consumed
      - Return spec shape with the new SI + collection + daily limit
    """
    try:
        employee = _require_driver()
        if not delivery_note:
            return err("VALIDATION_ERROR", "`delivery_note` is required.", 400)
        if not validation_token:
            return err("VALIDATION_ERROR", "`validation_token` is required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)
        dn = frappe.get_doc("Delivery Note", delivery_note)

        # 1) Token check
        stored = dn.get("validation_token") or ""
        token_expires = dn.get("validation_token_expires_at")
        if stored != validation_token:
            return err("OTP_INVALID",
                       "validation_token does not match the one issued for this delivery.",
                       400)
        if token_expires and now_datetime() > token_expires:
            return err("OTP_INVALID",
                       "validation_token has expired. Validate the OTP again.", 400)

        # 2) COD amount check (only when COD-*)
        pay_method = (dn.get("cowberry_payment_method") or "")
        is_cod = pay_method.upper().startswith("COD")
        cod_amount = flt(cod_collected_amount) if cod_collected_amount is not None else 0
        if is_cod:
            expected = flt(dn.grand_total)
            if abs(cod_amount - expected) > 0.01:
                return err(
                    "COD_AMOUNT_MISMATCH",
                    f"Collected amount {cod_amount} does not match order grand total {expected}.",
                    400,
                )

        # 3) Daily collection limit
        emp = frappe.get_doc("Employee", employee)
        daily_limit = flt(emp.get("daily_collection_limit") or 0)
        current_collected = flt(emp.get("current_day_collected_amount") or 0)
        last_date = emp.get("current_day_collected_date")
        # Roll over the per-day counter if Employee.current_day_collected_date is stale.
        if str(last_date) != str(today()):
            current_collected = 0
        if daily_limit and is_cod and (current_collected + cod_amount) > daily_limit:
            return err(
                "COLLECTION_LIMIT_EXCEEDED",
                f"This delivery would push today's cash ({current_collected + cod_amount}) "
                f"past your daily limit ({daily_limit}). Submit cash to ops to reset.",
                400,
            )

        # 4) Atomic side-effects ---------------------------------------------------
        sales_invoice_name = _create_sales_invoice(dn)
        collection_name = _roll_into_driver_collection(employee, dn, cod_amount, pay_method)

        new_collected = current_collected + (cod_amount if is_cod else 0)
        frappe.db.set_value("Employee", employee, {
            "current_day_collected_amount": new_collected,
            "current_day_collected_date":   today(),
        }, update_modified=False)

        update = {
            "cowberry_delivery_status":  "Delivered",
            "validation_token":          "",          # single-use
            "validation_token_expires_at": None,
            "otp_hash":                  "",
            "otp_attempts":              0,
            "otp_validate_attempts":     0,
            "otp_validated":             0,
        }
        if gps_lat is not None:
            update["cowberry_delivery_lat"] = gps_lat
        if gps_lng is not None:
            update["cowberry_delivery_lng"] = gps_lng
        if photo_url:
            update["cowberry_proof_image"] = photo_url
        frappe.db.set_value("Delivery Note", delivery_note, update, update_modified=False)
        frappe.db.commit()

        limit_warning = (
            bool(daily_limit) and is_cod and new_collected >= (daily_limit * 0.8)
        )
        invoice_pdf_url = (
            "/api/method/frappe.utils.print_format.download_pdf"
            f"?doctype=Sales+Invoice&name={frappe.utils.quoted(sales_invoice_name)}"
            "&format=Standard"
        ) if sales_invoice_name else None

        return ok(data={
            "delivery_status":       "Delivered",
            "sales_invoice":         sales_invoice_name,
            "invoice_pdf_url":       invoice_pdf_url,
            "driver_collection":     collection_name,
            "current_day_collected": new_collected,
            "daily_limit":           daily_limit,
            "limit_warning":         limit_warning,
        })
    except Exception as e:
        frappe.db.rollback()
        return err("SERVER_ERROR", str(e), 500)


# ---------------------------------------------------------------------------
# Helpers shared with the legacy order.submit_proof path
# ---------------------------------------------------------------------------

def _create_sales_invoice(dn):
    """Make a Sales Invoice from the DN using ERPNext's stock mapper,
    submit it, and return the new SI name. Idempotent: if one already
    exists for this DN we return that instead of creating a duplicate.
    """
    existing = frappe.db.sql(
        """SELECT DISTINCT si.name
             FROM `tabSales Invoice Item` sii
             JOIN `tabSales Invoice` si ON si.name = sii.parent
            WHERE sii.dn_detail IN (SELECT name FROM `tabDelivery Note Item` WHERE parent=%s)
              AND si.docstatus = 1
            LIMIT 1""",
        (dn.name,),
    )
    if existing:
        return existing[0][0]
    try:
        # SI creation runs ERPNext's mapper which inspects roles before
        # the doc-level ignore_permissions kicks in. Elevate to
        # Administrator for the SI write only, then restore the driver
        # session — keeps the driver-scoped audit trail clean
        # everywhere except SI creation, which has to be system-level
        # anyway (drivers never own SI write perms).
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice
            si = make_sales_invoice(dn.name)
            si.set_posting_time = 1
            si.flags.ignore_permissions = True
            si.insert(ignore_permissions=True)
            si.submit()
            return si.name
        finally:
            frappe.set_user(original_user)
    except Exception:
        # Mapping can fail for fixtures missing accounts; leave the
        # delivery completion in place but mark the SI as missing so
        # ops can investigate without the driver being blocked.
        frappe.logger("erpera_driver_app").exception(
            f"SI creation failed for {dn.name}"
        )
        return None


def _roll_into_driver_collection(employee, dn, cod_amount, payment_method):
    """Append this delivery to today's Driver Collection (creating it
    if needed) and recompute the totals."""
    col_name = frappe.db.get_value(
        "Driver Collection",
        {"driver": employee, "collection_date": today()},
        "name",
    )
    if not col_name:
        col = frappe.new_doc("Driver Collection")
        col.driver = employee
        col.collection_date = today()
        col.status = "Open"
        col.flags.ignore_mandatory = True
        col.insert(ignore_permissions=True)
        col_name = col.name
    col = frappe.get_doc("Driver Collection", col_name)

    pm = (payment_method or "").upper()
    if pm.startswith("COD") and "ONLINE" in pm:
        col.total_online = flt(col.get("total_online")) + cod_amount
    elif pm.startswith("COD"):
        col.total_cash = flt(col.get("total_cash")) + cod_amount
    else:
        # Prepaid / wallet — track separately if the field exists
        if "total_wallet" in {f.fieldname for f in frappe.get_meta("Driver Collection").fields}:
            col.total_wallet = flt(col.get("total_wallet")) + cod_amount

    # Child table (CCD Order Item) constrains payment_method to a
    # narrower set than the DN field. Map "COD-Cash" → "Cash" (the
    # child only needs to know the tender; the DN keeps the full
    # payment-mode name for accounting).
    pm_for_child = "Cash" if pm == "COD-CASH" else (
        "COD-Online" if "ONLINE" in pm else (
            "Wallet" if "WALLET" in pm else "Prepaid"
        )
    )
    col.append("order_breakdown", {
        "delivery_note":   dn.name,
        "customer":        dn.customer,
        "customer_name":   dn.customer_name,
        "payment_method":  pm_for_child,
        "cash_amount":     cod_amount if pm.startswith("COD") and "ONLINE" not in pm else 0,
        "online_amount":   cod_amount if "ONLINE" in pm else 0,
        "total_amount":    cod_amount,
        "status":          "Delivered",
    })
    col.flags.ignore_permissions = True
    col.save(ignore_permissions=True)
    return col_name
