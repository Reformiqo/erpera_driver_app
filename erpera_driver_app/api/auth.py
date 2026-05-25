import frappe

from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_DRIVER_LOGIN, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist(allow_guest=True)
def send_reset_otp(email):
    try:
        if not frappe.db.exists("User", {"email": email}):
            # Don't reveal if email exists
            return ok(data={"message": "If the email exists, an OTP has been sent."})

        employee = frappe.db.get_value("Employee", {"user_id": email}, "name")
        log_name = dispatch_otp_v2(
            purpose=PURPOSE_DRIVER_LOGIN,
            reference_doctype="User",
            reference_name=email,
            recipient_email=email,
        )
        return ok(data={"log_name": log_name, "message": "OTP sent."})
    except Exception as e:
        return err("SEND_OTP_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def verify_reset_otp(log_name, otp):
    try:
        validate_otp_v2(log_name=log_name, otp_input=otp)
        return ok(data={"verified": True})
    except OTPInvalidError as e:
        return e.to_response()
    except Exception as e:
        return err("VERIFY_OTP_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def reset_password(log_name, otp, new_password):
    try:
        log = frappe.get_doc("OTP Log", log_name)
        validate_otp_v2(log_name=log_name, otp_input=otp)

        user = frappe.get_doc("User", log.reference_name)
        user.new_password = new_password
        user.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message": "Password reset successfully."})
    except OTPInvalidError as e:
        return e.to_response()
    except Exception as e:
        return err("RESET_PASSWORD_FAILED", str(e))
