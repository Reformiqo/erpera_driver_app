import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_CASH_SUBMISSION, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist()
def initiate(collection_id, amount):
    try:
        employee = _require_driver()
        col = frappe.get_doc("Driver Collection", collection_id)
        if col.driver != employee:
            return err("ACCESS_DENIED", "This collection does not belong to you.", 403)

        sub = frappe.new_doc("Cash Submission")
        sub.driver = employee
        sub.collection = collection_id
        sub.amount = amount
        sub.status = "Pending OTP"
        sub.insert(ignore_permissions=True)

        mobile = frappe.db.get_value("Employee", employee, "cell_number")
        log_name = dispatch_otp_v2(
            purpose=PURPOSE_CASH_SUBMISSION,
            reference_doctype="Cash Submission",
            reference_name=sub.name,
            recipient_mobile=mobile or "",
        )
        frappe.db.commit()
        return ok(data={"submission_id": sub.name, "otp_log": log_name})
    except Exception as e:
        return err("INITIATE_SUBMISSION_FAILED", str(e))


@frappe.whitelist()
def validate_otp_endpoint(submission_id, otp_log_name, otp):
    try:
        employee = _require_driver()
        sub = frappe.get_doc("Cash Submission", submission_id)
        if sub.driver != employee:
            return err("ACCESS_DENIED", "This submission does not belong to you.", 403)

        validate_otp_v2(log_name=otp_log_name, otp_input=otp)

        sub.status = "Verified"
        sub.save(ignore_permissions=True)

        # Close the collection
        col = frappe.get_doc("Driver Collection", sub.collection)
        col.status = "Closed"
        col.save(ignore_permissions=True)

        frappe.db.commit()
        return ok(data={"submission_id": sub.name, "status": "Verified"})
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
            fields=["name", "amount", "status", "creation", "collection"],
            order_by="creation desc",
            limit=int(limit),
            start=int(offset),
        )
        return ok(data={"submissions": submissions})
    except Exception as e:
        return err("HISTORY_FAILED", str(e))
