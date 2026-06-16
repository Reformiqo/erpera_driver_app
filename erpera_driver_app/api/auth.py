import frappe
import frappe.auth  # noqa: F401  -- LoginManager lives here
from frappe.utils import add_days, now_datetime

from erpera_driver_app import __version__
from erpera_driver_app.utils.exceptions import OTPInvalidError
from erpera_driver_app.utils.otp import PURPOSE_DRIVER_LOGIN, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok

# Session validity window the spec advertises (refresh_session returns
# expires_at = now + this). Frappe doesn't expire api_key:api_secret
# tokens; this value is informational for the Flutter client's
# foreground-refresh schedule.
SESSION_VALIDITY_DAYS = 30


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
# Driver login — authenticate, enforce Driver role, return token + profile.
# `driver_login` is the spec-named endpoint (Nainsi's xlsx §Authentication.1).
# `login` is the original alias and stays for backward-compat.
# ---------------------------------------------------------------------------

def _do_driver_login(email, password, device_id=None, fcm_token=None, app_version=None):
    """Shared implementation behind both `driver_login` and `login`.

    Returns the same ok()/err() envelope the wrappers would. Side effects:
    on success, updates Employee.fcm_device_token, Employee.app_version,
    Employee.last_login_at; rotates api_secret to invalidate prior tokens
    (concurrent-login guard).
    """
    if not email or not isinstance(email, str):
        return err("MISSING_USER", "Please provide your user (email).", 400)
    if not password or not isinstance(password, str):
        return err("MISSING_PASSWORD", "Please provide your password.", 400)

    email = email.strip().lower()
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
        frappe.local.login_manager.logout()
        frappe.clear_messages()
        return err(
            "NOT_DRIVER",
            "This account does not have driver access. Please contact your administrator.",
            403,
        )

    emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if not emp_name:
        frappe.local.login_manager.logout()
        return err(
            "NO_EMPLOYEE",
            "No Employee record is linked to this user. Please contact your administrator.",
            403,
        )
    emp = frappe.get_doc("Employee", emp_name)

    # Concurrent-login guard: when the incoming device_id differs from the
    # stored fcm_device_token, this is a new device install. Rotating
    # api_secret in _issue_api_credentials() below invalidates the prior
    # device's bearer token on its very next request.
    if device_id and emp.get("fcm_device_token") and emp.fcm_device_token != device_id:
        # The prior device gets a 401 on the next call; the Flutter client
        # should treat that as "logged out on another device" and route
        # back to the login screen.
        pass  # rotation below is sufficient — no separate kill step needed

    # Persist device + version metadata if the client sent them. device_id
    # is the stable per-install UUID; fcm_token is FCM push registration
    # (may rotate independently). We share one custom field for now; if
    # push and concurrent-login start needing separate tracking we'll add
    # a second custom field.
    new_device = device_id or fcm_token
    if new_device:
        emp.fcm_device_token = new_device
    if app_version:
        emp.app_version = app_version
    emp.last_login_at = now_datetime()
    emp.flags.ignore_permissions = True
    emp.save()
    frappe.db.commit()

    api_key, api_secret = _issue_api_credentials(email)
    # Flat one-round-trip response per the FRD spec: Flutter calls
    # login on cold start and has everything the home screen needs
    # (token + driver profile) without a follow-up profile.get call.
    return ok(data={
        "api_key":                api_key,
        "api_secret":             api_secret,
        "employee":               emp.name,
        "employee_name":          emp.employee_name,
        "default_warehouse":      emp.get("default_warehouse"),
        "daily_collection_limit": emp.get("daily_collection_limit"),
        "vehicle_assigned":       emp.get("vehicle_assigned"),
        "offline_zone_radius_km": emp.get("offline_zone_radius_km"),
        "roles":                  roles,
    })


@frappe.whitelist(allow_guest=True, methods=["POST"])
def driver_login(usr=None, pwd=None, device_id=None, fcm_token=None, app_version=None,
                 user=None, password=None):
    """Spec-named driver login (Authentication §1).

    Accepts the spec-shaped body `{usr, pwd, device_id, fcm_token, app_version}`
    AND the legacy `{user, password}` aliases so an in-flight Flutter build
    on either shape works.
    """
    try:
        return _do_driver_login(
            email=usr or user,
            password=pwd or password,
            device_id=device_id,
            fcm_token=fcm_token,
            app_version=app_version,
        )
    except Exception as e:
        return err("LOGIN_FAILED", str(e), 500)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def login(user=None, password=None, usr=None, pwd=None,
          device_id=None, fcm_token=None, app_version=None):
    """Backward-compat alias of `driver_login`. Same contract."""
    try:
        return _do_driver_login(
            email=user or usr,
            password=password or pwd,
            device_id=device_id,
            fcm_token=fcm_token,
            app_version=app_version,
        )
    except Exception as e:
        return err("LOGIN_FAILED", str(e), 500)


@frappe.whitelist(methods=["POST"])
def refresh_session():
    """Bump Employee.last_login_at and return a fresh `expires_at` so the
    Flutter client knows how long the cached token is good for
    (Authentication §2). Call on `AppLifecycleState.resumed`.

    Note: Frappe doesn't expire api_key/api_secret tokens server-side, so
    `expires_at` is informational — it's now + SESSION_VALIDITY_DAYS. The
    client uses it to schedule the next refresh, not as a hard cutoff.
    """
    try:
        email = frappe.session.user
        if email == "Guest":
            return err("NOT_AUTHENTICATED", "Session has expired. Please log in again.", 401)
        emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if emp_name:
            frappe.db.set_value("Employee", emp_name, "last_login_at",
                                now_datetime(), update_modified=False)
            frappe.db.commit()
        expires_at = add_days(now_datetime(), SESSION_VALIDITY_DAYS)
        return ok(data={"expires_at": str(expires_at)})
    except Exception as e:
        return err("REFRESH_SESSION_FAILED", str(e), 500)


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

        emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if not emp_name:
            return err("NO_EMPLOYEE", "No Employee record is linked to this user.", 403)
        emp = frappe.get_doc("Employee", emp_name)
        # Same flat shape as login, minus the token (client already has it).
        return ok(data={
            "employee":               emp.name,
            "employee_name":          emp.employee_name,
            "default_warehouse":      emp.get("default_warehouse"),
            "daily_collection_limit": emp.get("daily_collection_limit"),
            "vehicle_assigned":       emp.get("vehicle_assigned"),
            "offline_zone_radius_km": emp.get("offline_zone_radius_km"),
            "roles":                  roles,
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
    """Explicit logout (Authentication §3). Clears Employee.fcm_device_token
    and rotates api_secret (so the token in the just-logged-out app is dead
    on its next call), then destroys the Frappe session. Mobile client
    should clear its secure-storage cache on a 200 response."""
    try:
        email = frappe.session.user
        if email and email != "Guest":
            emp_name = frappe.db.get_value("Employee", {"user_id": email}, "name")
            if emp_name:
                frappe.db.set_value("Employee", emp_name, "fcm_device_token", "",
                                    update_modified=False)
            # Rotate api_secret (Password field — must go through Document
            # save so Frappe's password-encryption hook fires; a direct
            # db.set_value would write the plaintext and break Frappe's
            # token verification). api_key stays so the user keeps the
            # same identifier across sessions; only the secret changes.
            user_doc = frappe.get_doc("User", email)
            user_doc.api_secret = frappe.generate_hash(length=15)
            user_doc.flags.ignore_permissions = True
            user_doc.save()
            frappe.db.commit()
        frappe.local.login_manager.logout()
        return ok(data={"message": "Logged out"})
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
    """Peek-validate the OTP without consuming it.

    The Flutter forgot-password flow needs this step to gate the
    new-password screen, BUT it then re-submits the same OTP to
    `reset_password` along with the new password. If we consumed the
    OTP here, the follow-up reset_password call would fail with
    OTP_INVALID / 'already used'. So we validate with consume=False
    and let reset_password be the single consumption point.
    """
    try:
        validate_otp_v2(log_name=log_name, otp_input=otp, consume=False)
        return ok(data={"verified": True})
    except OTPInvalidError as e:
        return e.to_response()
    except Exception as e:
        return err("VERIFY_OTP_FAILED", str(e))


@frappe.whitelist(allow_guest=True)
def reset_password(log_name, otp, new_password):
    try:
        log = frappe.get_doc("OTP Log", log_name)
        # consume=True (default) — this is the actual one-shot point.
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
