import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_CASH_SUBMISSION, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


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


@frappe.whitelist()
def initiate(
    collection_id,
    amount,
    submission_method=None,
    screenshot_url=None,
    discrepancy_note=None,
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
            "submission_id": sub.name,
            "otp_log": log_name,
            "system_amount": system_amount,
            "physical_amount": physical_amount,
            "discrepancy": discrepancy,
            "discrepancy_flag": abs(discrepancy) > 0.01,
            "wm_otp_sent_to": wm_email or wm_mobile,
            "otp_validity_minutes": 5,
        })
    except Exception as e:
        return err("INITIATE_SUBMISSION_FAILED", str(e))


@frappe.whitelist()
def validate_otp_endpoint(submission_id, otp_log_name, otp):
    """Validate the warehouse-manager OTP and close the collection.

    On success, the Cash Submission is marked Verified (the WM has
    signed off), the linked Driver Collection moves to Closed, and
    the driver's running daily total can be reset by a downstream
    Employee.current_day_collected_amount adjustment (handled by the
    DocType's `on_submit` controller, not here).
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
        sub.save(ignore_permissions=True)

        col = frappe.get_doc("Driver Collection", sub.collection)
        col.status = "Closed"
        col.save(ignore_permissions=True)

        # Reset the driver's daily running total — the limit blocks
        # further COD PoDs until a successful handover (FRD §7.2).
        emp = frappe.get_doc("Employee", employee)
        if emp.get("current_day_collected_amount"):
            emp.current_day_collected_amount = 0
            emp.save(ignore_permissions=True)

        frappe.db.commit()
        return ok(data={
            "submission_id": sub.name,
            "status": "Verified",
            "collection_id": col.name,
            "collection_status": col.status,
            "daily_limit_reset": True,
        })
    except OTPInvalidError as e:
        return e.to_response()
    except Exception as e:
        return err("VALIDATE_OTP_FAILED", str(e))


@frappe.whitelist()
def history(limit=20, offset=0):
    try:
        employee = _require_driver()
        submissions = frappe.get_all(
            "Cash Submission",
            filters={"driver": employee},
            fields=[
                "name",
                "amount",
                "status",
                "creation",
                "collection",
                "submission_method",
                "discrepancy_amount",
                "discrepancy_flag",
            ],
            order_by="creation desc",
            limit=int(limit),
            start=int(offset),
        )
        return ok(data={"submissions": submissions})
    except Exception as e:
        return err("HISTORY_FAILED", str(e))
