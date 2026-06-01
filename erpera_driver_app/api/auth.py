import frappe
import frappe.auth  # noqa: F401  -- LoginManager lives here

from erpera_driver_app import __version__
from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_DRIVER_LOGIN, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


def _issue_api_credentials(user_id):
    """Return (api_key, api_secret) for the user. api_key is created once
    and persists across logins; api_secret is freshly rotated on every login
    so a fresh device login invalidates the previous token. The Flutter
    client sends both together as `Authorization: token <key>:<secret>` on
    every subsequent request.
    """
    user_doc = frappe.get_doc("User", user_id)
    if not user_doc.api_key:
        user_doc.api_key = frappe.generate_hash(length=15)
    api_secret = frappe.generate_hash(length=15)
    user_doc.api_secret = api_secret
    user_doc.flags.ignore_permissions = True
    user_doc.save()
    frappe.db.commit()
    return user_doc.api_key, api_secret


# ---------------------------------------------------------------------------
# Driver login — authenticate, enforce Driver role, return sid + profile.
# Pattern adapted from orange_fsm.api.auth.login so the Flutter client gets
# session + user payload in one call instead of two.
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(user=None, password=None):
    """Driver-app login. Returns the Frappe session id (`sid`) plus the
    authenticated user's profile + linked Employee. Caller is rejected with
    `NOT_DRIVER` if the user lacks the Driver role.
    """
    try:
        if not user or not isinstance(user, str):
            return err("MISSING_USER", "Please provide your user (email).", 400)
        if not password or not isinstance(password, str):
            return err("MISSING_PASSWORD", "Please provide your password.", 400)

        email = user.strip().lower()
        user_row = frappe.db.get_value(
            "User", {"name": email}, ["name", "enabled"], as_dict=True
        )
        # Generic credential error — don't leak whether the email exists.
        if not user_row:
            return err("INVALID_CREDENTIALS", "Invalid email or password.", 401)
        if not user_row.enabled:
            return err(
                "ACCOUNT_DISABLED",
                "Your account has been disabled. Please contact your administrator.",
                403,
            )

        try:
            lm = frappe.auth.LoginManager()
            lm.authenticate(user=email, pwd=password)
            lm.post_login()
        except frappe.AuthenticationError:
            frappe.clear_messages()
            return err("INVALID_CREDENTIALS", "Invalid email or password.", 401)

        roles = frappe.get_roles(email)
        if "Driver" not in roles:
            # Hand back a clean error so the app can branch.
            frappe.local.login_manager.logout()
            frappe.clear_messages()
            return err(
                "NOT_DRIVER",
                "This account does not have driver access. Please contact your administrator.",
                403,
            )

        employee = frappe.db.get_value(
            "Employee",
            {"user_id": email},
            [
                "name", "employee_name", "cell_number", "image",
                "department", "designation", "branch",
            ],
            as_dict=True,
        )
        if not employee:
            frappe.local.login_manager.logout()
            return err(
                "NO_EMPLOYEE",
                "No Employee record is linked to this user. Please contact your administrator.",
                403,
            )

        user_doc = frappe.get_doc("User", email)
        api_key, api_secret = _issue_api_credentials(email)
        return ok(data={
            "user":       email,
            "full_name":  user_doc.full_name,
            "email":      user_doc.email,
            "mobile_no":  user_doc.mobile_no or employee.cell_number,
            "user_image": user_doc.user_image or employee.image,
            "employee":   employee,
            "roles":      roles,
            # Token credentials — Flutter client sends them as
            #   Authorization: token <api_key>:<api_secret>
            # on every subsequent call. api_secret rotates per login.
            "api_key":    api_key,
            "api_secret": api_secret,
        })
    except Exception as e:
        return err("LOGIN_FAILED", str(e), 500)


@frappe.whitelist(methods=["GET"])
def get_user_info():
    """Returns the currently-authenticated driver's profile in the same
    shape as `login`'s `data` block, minus the `sid` (the client already has
    it). Useful on app cold-start to check that the cached session is alive.
    """
    try:
        email = frappe.session.user
        if email == "Guest":
            return err("NOT_AUTHENTICATED", "Session has expired. Please log in again.", 401)

        roles = frappe.get_roles(email)
        if "Driver" not in roles:
            return err("NOT_DRIVER", "This account does not have driver access.", 403)

        employee = frappe.db.get_value(
            "Employee", {"user_id": email},
            ["name", "employee_name", "cell_number", "image",
             "department", "designation", "branch"],
            as_dict=True,
        )
        user_doc = frappe.get_doc("User", email)
        return ok(data={
            "user":       email,
            "full_name":  user_doc.full_name,
            "email":      user_doc.email,
            "mobile_no":  user_doc.mobile_no or (employee.cell_number if employee else None),
            "user_image": user_doc.user_image or (employee.image if employee else None),
            "employee":   employee,
            "roles":      roles,
        })
    except Exception as e:
        return err("GET_USER_INFO_FAILED", str(e), 500)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def app_version():
    """Returns the installed app version so the Flutter client can show /
    enforce a minimum supported backend version."""
    return ok(data={"app": "erpera_driver_app", "version": __version__})


@frappe.whitelist(methods=["POST"])
def logout():
    """Invalidates the Frappe session. Mobile client should drop its cached
    sid + user payload after a 200 response."""
    try:
        frappe.local.login_manager.logout()
        return ok(data={"message": "Logged out."})
    except Exception as e:
        return err("LOGOUT_FAILED", str(e), 500)


# ---------------------------------------------------------------------------
# Password reset (existing OTP-by-email flow, kept as-is).
# ---------------------------------------------------------------------------

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
