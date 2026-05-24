import hashlib
import random
import string
from datetime import datetime, timedelta

import frappe

PURPOSE_POD = "POD"
PURPOSE_CASH_SUBMISSION = "CASH_SUBMISSION"
PURPOSE_WALLET = "WALLET"
PURPOSE_DRIVER_LOGIN = "DRIVER_LOGIN"

_VALID_PURPOSES = {PURPOSE_POD, PURPOSE_CASH_SUBMISSION, PURPOSE_WALLET, PURPOSE_DRIVER_LOGIN}


def _get_settings():
    return frappe.get_single("Cowberry Driver Settings")


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
def dispatch_otp(purpose, reference_doctype, reference_name, recipient_mobile=None, recipient_email=None):
    if purpose not in _VALID_PURPOSES:
        frappe.throw(f"Invalid OTP purpose: {purpose}")

    settings = _get_settings()
    otp = _generate_otp()
    validity = _validity_minutes(purpose, settings)
    expiry = datetime.now() + timedelta(minutes=validity)

    log = frappe.new_doc("Cowberry OTP Log")
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

    # In production, send via SMS/email gateway here
    if frappe.conf.get("developer_mode"):
        frappe.log_error(f"[OTP DEBUG] {purpose}/{reference_name}: {otp}", "OTP Dispatch")

    return log.name


def validate_otp(log_name, otp_input):
    from cowberry.utils.exceptions import OTPExpiredError, OTPInvalidError, OTPMaxAttemptsError

    log = frappe.get_doc("Cowberry OTP Log", log_name)
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

    log.is_used = 1
    log.save(ignore_permissions=True)
    frappe.db.commit()
    return True


# v2 API (kwarg-style, used by api modules)
def dispatch_otp_v2(*, purpose, reference_doctype, reference_name, recipient_mobile=None, recipient_email=None):
    return dispatch_otp(purpose, reference_doctype, reference_name, recipient_mobile, recipient_email)


def validate_otp_v2(*, log_name, otp_input):
    return validate_otp(log_name, otp_input)
