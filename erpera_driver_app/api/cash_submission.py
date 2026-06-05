import frappe
from frappe.utils import flt, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_CASH_SUBMISSION, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok

# Spec wording for the next_call hint surfaced in initiate's response
SPEC_NEXT_CALL = "cash_submission.validate_otp"
MAX_DISCREPANCY_NOTE_CHARS = 500


# ---------------------------------------------------------------------------
# Spec-shaped entry points (Nainsi's xlsx §Cash Submission §§1-4)
# ---------------------------------------------------------------------------

def _collection_for_trip(trip, driver_employee):
    """Look up the Driver Collection backing a Delivery Trip for this
    driver. Used by initiate when called with the spec body that keys
    off `delivery_trip`."""
    return frappe.db.get_value(
        "Driver Collection",
        {"trip": trip, "driver": driver_employee},
        "name",
    )


def _existing_pending(driver_employee):
    """Check the spec invariant 'one active pending Cash Submission per
    driver at a time'. Returns the offending row name, or None."""
    return frappe.db.get_value(
        "Cash Submission",
        {"driver": driver_employee, "docstatus": 0,
         "status": ("in", ["Pending OTP", "Draft"])},
        "name",
    )


@frappe.whitelist(methods=["POST"])
def validate_otp(cash_submission=None, otp=None, submission_id=None,
                 otp_log_name=None):
    """§2 Spec-shaped wrapper around validate_otp_endpoint.

    Spec body: {cash_submission, otp}. The original validate_otp_endpoint
    also needs otp_log_name — when the spec-shaped call comes in we
    look up the active OTP Log for this submission ourselves so the
    Flutter client doesn't have to track it.
    """
    try:
        sub_name = cash_submission or submission_id
        if not sub_name or not otp:
            return err("VALIDATION_ERROR",
                       "`cash_submission` and `otp` are required.", 400)
        # Resolve the OTP Log for this submission if the caller didn't
        # send one. We pick the most recent unvalidated log targeted at
        # this Cash Submission.
        if not otp_log_name:
            otp_log_name = frappe.db.get_value(
                "OTP Log",
                {
                    "reference_doctype": "Cash Submission",
                    "reference_name":    sub_name,
                    "validated":         0,
                },
                "name",
                order_by="creation desc",
            )
        if not otp_log_name:
            return err("OTP_INVALID",
                       "No outstanding OTP for this submission. Call initiate first.",
                       400)
        legacy = validate_otp_endpoint(submission_id=sub_name,
                                       otp_log_name=otp_log_name, otp=otp)
        if not legacy.get("success"):
            return legacy
        # Reshape legacy keys → spec keys
        d = legacy["data"]
        sub_doc = frappe.get_doc("Cash Submission", sub_name)
        return ok(data={
            "cash_submission":      sub_name,
            "payment_entry":        d.get("payment_entry"),
            "submitted":            True,
            "daily_limit_reset":    bool(d.get("daily_limit_reset")),
            "new_collected_amount": 0,
        })
    except Exception as e:
        return err("VALIDATE_OTP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def flag_discrepancy(cash_submission=None, note=None):
    """§3 Append a discrepancy note. Read-only after wm_otp validated.
    Note max 500 chars per spec."""
    try:
        employee = _require_driver()
        if not cash_submission or not note:
            return err("VALIDATION_ERROR",
                       "`cash_submission` and `note` are required.", 400)
        if not frappe.db.exists("Cash Submission", cash_submission):
            return err("NOT_FOUND",
                       f"Cash Submission '{cash_submission}' not found.", 404)
        sub = frappe.get_doc("Cash Submission", cash_submission)
        if sub.driver != employee:
            return err("FORBIDDEN", "This submission does not belong to you.", 403)
        if sub.docstatus == 1 or sub.get("status") == "Verified":
            return err("VALIDATION_ERROR",
                       "Cannot flag a discrepancy after the WM OTP has validated.",
                       400)
        if len(note) > MAX_DISCREPANCY_NOTE_CHARS:
            return err("VALIDATION_ERROR",
                       f"Note exceeds {MAX_DISCREPANCY_NOTE_CHARS}-char limit.", 400)
        sub.discrepancy_note = note
        sub.discrepancy_flag = 1
        sub.flags.ignore_permissions = True
        sub.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"discrepancy_note": note})
    except Exception as e:
        return err("FLAG_DISCREPANCY_FAILED", str(e))


def _resolve_warehouse_recipient(collection):
    """Find the warehouse-manager email/mobile that should receive the OTP.

    Per FRD §3.7 + §7.3 the cash-handover OTP goes to the warehouse
    manager (not the driver), so the trail of trust looks like:

        Driver Collection.trip  → Delivery Trip
                                  → Source Warehouse
                                     → warehouse_manager_email
                                     → warehouse_manager_mobile

    Returns `(email, mobile)`; either may be None if the warehouse
    isn't configured.
    """
    trip = collection.get("trip")
    if not trip:
        return None, None

    source_warehouse = frappe.db.get_value(
        "Delivery Trip", trip, "source_warehouse"
    )
    if not source_warehouse:
        return None, None

    email, mobile = frappe.db.get_value(
        "Warehouse",
        source_warehouse,
        ["warehouse_manager_email", "warehouse_manager_mobile"],
    ) or (None, None)
    return email, mobile


@frappe.whitelist(methods=["POST"])
def initiate(
    collection_id=None,
    amount=None,
    submission_method=None,
    screenshot_url=None,
    discrepancy_note=None,
    # Spec-shaped params (Nainsi's xlsx §Cash Submission §1):
    delivery_trip=None,
    physical_amount=None,
):
    """Start the OTP-gated cash handover.

    Creates a draft Cash Submission, dispatches the warehouse-manager
    OTP, and returns the handle the Flutter app passes back to
    `validate_otp_endpoint`. Fields per FRD §9.5:

        collection_id        — Driver Collection being closed (required).
        amount               — Physical amount the driver counted (required).
        submission_method    — "Physical Cash" | "UPI" | "Bank Transfer".
        screenshot_url       — File URL for UPI / bank-transfer evidence.
                               Required if submission_method != Physical Cash.
        discrepancy_note     — Driver's free-text note when physical
                               and system amounts diverge.

    The OTP is sent to the warehouse manager's email (with mobile
    fallback when configured) — the driver self-validates would
    defeat the whole point of the handover gate.
    """
    try:
        employee = _require_driver()
        # Spec body: lookup the collection from `delivery_trip` when the
        # client uses Nainsi's path. Legacy body keeps using `collection_id`.
        if not collection_id and delivery_trip:
            collection_id = _collection_for_trip(delivery_trip, employee)
            if not collection_id:
                return err("NOT_FOUND",
                           f"No Driver Collection found for trip '{delivery_trip}'.",
                           404)
        if not collection_id:
            return err("VALIDATION_ERROR",
                       "Either `collection_id` or `delivery_trip` is required.", 400)

        # Spec invariant — only one pending Cash Submission per driver.
        offender = _existing_pending(employee)
        if offender:
            return err("VALIDATION_ERROR",
                       f"A pending Cash Submission ({offender}) already exists. "
                       "Validate or cancel it first.", 400)

        amount = amount if amount is not None else physical_amount
        col = frappe.get_doc("Driver Collection", collection_id)
        if col.driver != employee:
            return err(
                "ACCESS_DENIED",
                "This collection does not belong to you.",
                403,
            )

        # UPI / Bank Transfer evidence is mandatory when the driver
        # claims a non-cash handover (FRD §10.3 row 5).
        if submission_method and submission_method != "Physical Cash":
            if not screenshot_url:
                return err(
                    "VALIDATION_ERROR",
                    "Transfer screenshot is required for non-cash submissions.",
                )

        wm_email, wm_mobile = _resolve_warehouse_recipient(col)
        if not (wm_email or wm_mobile):
            return err(
                "WAREHOUSE_NOT_CONFIGURED",
                "Warehouse manager contact is not set on the source warehouse.",
            )

        # System amount snapshot — used by Flutter to render the
        # discrepancy banner (per Flutter note in FRD §9.10 example 3).
        system_amount = float(col.get("total_cash") or 0)
        physical_amount = float(amount or 0)
        discrepancy = system_amount - physical_amount

        sub = frappe.new_doc("Cash Submission")
        sub.driver = employee
        sub.collection = collection_id
        sub.amount = physical_amount
        sub.status = "Pending OTP"
        if submission_method:
            sub.submission_method = submission_method
        if screenshot_url:
            sub.transfer_screenshot = screenshot_url
        if abs(discrepancy) > 0.01:
            sub.discrepancy_amount = discrepancy
            sub.discrepancy_flag = 1
            if discrepancy_note:
                sub.discrepancy_note = discrepancy_note
        sub.insert(ignore_permissions=True)

        log_name = dispatch_otp_v2(
            purpose=PURPOSE_CASH_SUBMISSION,
            reference_doctype="Cash Submission",
            reference_name=sub.name,
            recipient_email=wm_email or "",
            recipient_mobile=wm_mobile or "",
        )

        frappe.db.commit()
        return ok(data={
            # Spec keys (Nainsi's §1 response):
            "cash_submission":       sub.name,
            "system_amount":         system_amount,
            "physical_amount":       physical_amount,
            "discrepancy":           discrepancy,
            "discrepancy_flag":      abs(discrepancy) > 0.01,
            "wm_otp_sent_to":        wm_email or wm_mobile,
            "otp_validity_minutes":  5,
            "next_call":             SPEC_NEXT_CALL,
            # Legacy keys retained so older clients keep working:
            "submission_id":         sub.name,
            "otp_log":               log_name,
        })
    except Exception as e:
        return err("INITIATE_SUBMISSION_FAILED", str(e))


@frappe.whitelist()
def validate_otp_endpoint(submission_id, otp_log_name, otp):
    """Validate the WM OTP, submit the Cash Submission, close the collection.

    Per FRD §4.2: the Cash Submission has no Draft state once the OTP
    is validated. This call:
      1. Validates the OTP.
      2. Sets status=Verified.
      3. Submits the Cash Submission (docstatus=1) — this triggers
         the DocType's `on_submit` which creates the Internal Transfer
         Payment Entry.
      4. Marks the Driver Collection as Closed.
      5. Zeroes the driver's daily-collected counter so the limit
         gate releases for the next trip.
    """
    try:
        employee = _require_driver()
        sub = frappe.get_doc("Cash Submission", submission_id)
        if sub.driver != employee:
            return err(
                "ACCESS_DENIED",
                "This submission does not belong to you.",
                403,
            )

        validate_otp_v2(log_name=otp_log_name, otp_input=otp)

        sub.status = "Verified"
        sub.submission_date = frappe.utils.today()
        sub.save(ignore_permissions=True)
        # `.submit()` fires `before_submit` (Verified guard) and
        # `on_submit` (PE creation, discrepancy ToDo, Collection close).
        sub.submit()

        col = frappe.get_doc("Driver Collection", sub.collection)
        if col.status != "Submitted":
            col.status = "Closed"
            col.save(ignore_permissions=True)

        # Reset the driver's daily running total — the limit blocks
        # further COD PoDs until a successful handover (FRD §7.2).
        emp = frappe.get_doc("Employee", employee)
        if emp.get("current_day_collected_amount"):
            emp.current_day_collected_amount = 0
            emp.save(ignore_permissions=True)

        frappe.db.commit()

        # `on_submit` may have created the Payment Entry; re-read to
        # surface it in the response so Flutter renders the receipt.
        sub.reload()

        return ok(data={
            "submission_id": sub.name,
            "status": "Verified",
            "collection_id": col.name,
            "collection_status": col.status,
            "payment_entry": sub.get("payment_entry"),
            "discrepancy_flag": bool(sub.get("discrepancy_flag")),
            "daily_limit_reset": True,
        })
    except OTPInvalidError as e:
        return e.to_response()
    except Exception as e:
        return err("VALIDATE_OTP_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def history(date=None, limit=20, offset=0):
    """§4 Submission history. Spec accepts `?date=YYYY-MM-DD` (defaults
    to today). Returned row keys match the spec verbatim: name,
    submission_date, system_amount, physical_amount, discrepancy,
    payment_entry, submission_method.
    """
    try:
        employee = _require_driver()
        filters = {"driver": employee}
        if date:
            filters["submission_date"] = date
        rows = frappe.db.sql(
            """
            SELECT cs.name,
                   cs.submission_date,
                   IFNULL(dc.total_cash, 0) + IFNULL(dc.total_online, 0) AS system_amount,
                   cs.amount                 AS physical_amount,
                   cs.discrepancy_amount     AS discrepancy,
                   cs.submission_method,
                   cs.payment_entry,
                   cs.discrepancy_flag,
                   cs.status,
                   cs.creation
              FROM `tabCash Submission` cs
              LEFT JOIN `tabDriver Collection` dc ON dc.name = cs.collection
             WHERE cs.driver = %(emp)s
               {date_clause}
             ORDER BY cs.creation DESC
             LIMIT %(limit)s OFFSET %(offset)s
            """.format(
                date_clause="AND cs.submission_date = %(date)s" if date else ""
            ),
            {"emp": employee, "date": date, "limit": int(limit), "offset": int(offset)},
            as_dict=True,
        )
        for r in rows:
            r["system_amount"] = flt(r["system_amount"])
            r["physical_amount"] = flt(r["physical_amount"])
            r["discrepancy"] = flt(r["discrepancy"])
            if r.get("submission_date"):
                r["submission_date"] = str(r["submission_date"])
        return ok(data={"submissions": rows})
    except Exception as e:
        return err("HISTORY_FAILED", str(e))
