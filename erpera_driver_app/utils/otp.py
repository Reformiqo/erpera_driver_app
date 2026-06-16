import hashlib
import random
import string

import frappe
from frappe.utils import add_to_date, now_datetime

PURPOSE_POD = "POD"
PURPOSE_CASH_SUBMISSION = "CASH_SUBMISSION"
PURPOSE_WALLET = "WALLET"
PURPOSE_DRIVER_LOGIN = "DRIVER_LOGIN"

_VALID_PURPOSES = {PURPOSE_POD, PURPOSE_CASH_SUBMISSION, PURPOSE_WALLET, PURPOSE_DRIVER_LOGIN}

# Human-readable subject/header per purpose for the SMS body + email subject.
_PURPOSE_LABELS = {
    PURPOSE_POD:             "Delivery confirmation",
    PURPOSE_CASH_SUBMISSION: "Cash handover confirmation",
    PURPOSE_WALLET:          "Wallet top-up",
    PURPOSE_DRIVER_LOGIN:    "Driver app login / password reset",
}


def _get_settings():
    return frappe.get_single("Driver Settings")


def _deliver_via_email(recipient, subject, body):
    """Send the OTP email via Frappe's stock mail queue.

    Raises whatever frappe.sendmail raises (smtplib / network errors)
    so the caller can mark the OTP Log as failed-to-deliver.
    """
    frappe.sendmail(
        recipients=[recipient],
        subject=subject,
        message=body,
        now=True,            # bypass the deferred queue — drivers need it instantly
        delayed=False,
    )


def _deliver_via_sms(recipient, body):
    """Send the OTP via Frappe's stock SMS gateway.

    Raises whatever send_sms raises (no SMS Settings, provider HTTP
    error, etc.) so the caller can record the failure visibly.
    """
    from frappe.core.doctype.sms_settings.sms_settings import send_sms
    send_sms([recipient], body)


def _record_failure(log_name, channel, exc, reference_doctype, reference_name):
    """When dispatch fails, leave a visible audit trail so admins notice.

    1. ERROR-level log line for `bench logs --error` grep.
    2. Comment on the referenced doc so it shows in the document timeline.
    """
    err_msg = f"{channel} dispatch failed for OTP Log {log_name}: {type(exc).__name__}: {exc}"
    frappe.logger("erpera_driver_app").error(err_msg)
    if reference_doctype and reference_name:
        try:
            frappe.get_doc({
                "doctype":           "Comment",
                "comment_type":      "Comment",
                "reference_doctype": reference_doctype,
                "reference_name":    reference_name,
                "content":           f"⚠ {err_msg}",
            }).insert(ignore_permissions=True)
        except Exception:
            pass


def _generate_otp(length=6):
    return "".join(random.choices(string.digits, k=length))


def _hash_otp(otp):
    return hashlib.sha256(otp.encode()).hexdigest()


def _validity_minutes(purpose, settings):
    mapping = {
        PURPOSE_POD: settings.otp_validity_pod or 10,
        PURPOSE_CASH_SUBMISSION: settings.otp_validity_cash_submission or 10,
        PURPOSE_WALLET: settings.otp_validity_wallet or 10,
        PURPOSE_DRIVER_LOGIN: settings.otp_validity_driver_login or 10,
    }
    return mapping.get(purpose, 10)


def _max_attempts(settings):
    return settings.otp_max_attempts or 5


# v1 API
def dispatch_otp(purpose, reference_doctype, reference_name,
                 recipient_mobile=None, recipient_email=None):
    """Generate + store + deliver an OTP. Returns the OTP Log name.

    Delivery is best-effort: when an email recipient is supplied we send
    via Frappe's mail queue; when a mobile recipient is supplied we send
    via Frappe's stock SMS gateway. Both channels can be supplied
    independently (e.g. cash-submission OTPs go to the warehouse
    manager's email + mobile so they receive whichever they check
    first).

    On dispatch failure: the OTP Log is created (so the operator can
    still read out the OTP manually if needed), a structured ERROR is
    logged, and a Comment is dropped on the referenced doc so the
    failure is visible in the document timeline rather than silently
    swallowed.

    Returns the OTP Log name regardless of dispatch outcome — the
    caller can inspect `delivered_to_email` / `delivered_to_mobile`
    flags on the returned Log if it needs to branch on delivery state.
    """
    if purpose not in _VALID_PURPOSES:
        frappe.throw(f"Invalid OTP purpose: {purpose}")

    settings = _get_settings()
    otp = _generate_otp()
    validity = _validity_minutes(purpose, settings)
    # Use Frappe's site-aware now_datetime() — NOT datetime.now() which
    # returns the SERVER's local clock. FC servers run UTC; sites
    # typically use Asia/Kolkata (UTC+5:30). Mixing the two means
    # expires_at is stored 5h30m in the past, so every validate_otp
    # call returns "OTP has expired" immediately — even when the
    # caller submits the OTP one second after receiving it.
    expiry = add_to_date(now_datetime(), minutes=validity)

    log = frappe.new_doc("OTP Log")
    log.purpose = purpose
    log.reference_doctype = reference_doctype
    log.reference_name = reference_name
    log.otp_hash = _hash_otp(otp)
    log.expires_at = expiry
    log.is_used = 0
    log.attempts = 0
    log.recipient_mobile = recipient_mobile or ""
    log.recipient_email = recipient_email or ""
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    label = _PURPOSE_LABELS.get(purpose, purpose)
    sms_body = (
        f"Your OTP for {label} is {otp}. "
        f"Valid for {validity} minutes. Do not share this code."
    )

    # Email dispatch — used by auth.send_reset_otp (Forgot Password) and
    # cash_submission.initiate (warehouse manager confirmation).
    if recipient_email:
        try:
            email_body = (
                f"<p>Your OTP for <b>{label}</b> is:</p>"
                f"<h2 style='letter-spacing:4px'>{otp}</h2>"
                f"<p>Valid for {validity} minutes. If you didn't request this, ignore this email.</p>"
            )
            _deliver_via_email(recipient_email,
                               subject=f"OTP for {label}",
                               body=email_body)
        except Exception as e:
            _record_failure(log.name, "Email", e, reference_doctype, reference_name)

    # SMS dispatch — used by otp.request_pod_otp (customer PoD),
    # cash_submission (WM mobile fallback), wallet.initiate_topup (customer).
    if recipient_mobile:
        try:
            _deliver_via_sms(recipient_mobile, sms_body)
        except Exception as e:
            _record_failure(log.name, "SMS", e, reference_doctype, reference_name)

    # Dev convenience: when running with developer_mode=1 (local benches),
    # also log the plaintext OTP so engineers can copy it without needing
    # a working SMS / mail provider. NEVER fires in production
    # (developer_mode=0 on FC).
    if frappe.conf.get("developer_mode"):
        frappe.logger("erpera_driver_app").info(
            f"[OTP DEBUG] {purpose}/{reference_name}: {otp}"
        )

    return log.name


def validate_otp(log_name, otp_input, consume=True):
    """Validate an OTP against an OTP Log row.

    When ``consume`` is True (default) the OTP is marked ``is_used=1`` on
    success, so it can't be replayed. When False, this acts as a *peek*:
    the hash, expiry and attempt cap are still checked (and attempts is
    still incremented, so brute-force is still bounded), but the OTP
    stays valid for a follow-up call that does consume it.

    The forgot-password flow uses both: ``verify_reset_otp`` peeks so the
    Flutter app can branch to the new-password screen; ``reset_password``
    consumes when it actually changes the password. Other flows (PoD,
    Cash Submission, Wallet) keep the consume-on-validate default.
    """
    from erpera_driver_app.utils.exceptions import OTPExpiredError, OTPInvalidError, OTPMaxAttemptsError

    log = frappe.get_doc("OTP Log", log_name)
    settings = _get_settings()

    if log.is_used:
        raise OTPInvalidError("OTP has already been used.")

    if log.attempts >= _max_attempts(settings):
        raise OTPMaxAttemptsError()

    if frappe.utils.now_datetime() > log.expires_at:
        raise OTPExpiredError()

    log.attempts += 1

    if log.otp_hash != _hash_otp(otp_input):
        log.save(ignore_permissions=True)
        frappe.db.commit()
        raise OTPInvalidError()

    if consume:
        log.is_used = 1
    log.save(ignore_permissions=True)
    frappe.db.commit()
    return True


# v2 API (kwarg-style, used by api modules)
def dispatch_otp_v2(*, purpose, reference_doctype, reference_name, recipient_mobile=None, recipient_email=None):
    return dispatch_otp(purpose, reference_doctype, reference_name, recipient_mobile, recipient_email)


def validate_otp_v2(*, log_name, otp_input, consume=True):
    return validate_otp(log_name, otp_input, consume=consume)
