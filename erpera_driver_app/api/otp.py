"""Proof-of-Delivery OTP API — Nainsi's spec §OTP & PoD §§1-3.

The PoD OTP is a 6-digit code sent to the customer's mobile when the
driver presses "Send OTP" on the Stop Detail screen. The customer
reads it out; the driver types it into validate_pod_otp; on success
they get a single-use validation_token they pass to pod.submit_proof.

State is held on the Delivery Note itself (8 custom fields added in
the fixture): otp_hash, otp_requested_at, otp_expires_at, otp_attempts,
otp_validate_attempts, otp_validated, validation_token,
validation_token_expires_at. The plaintext OTP never touches the DB —
only its SHA-256 hash.
"""
import hashlib

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok


# Spec-mandated limits and validity windows
OTP_VALID_MINUTES = 10
MAX_SEND_ATTEMPTS = 3            # initial + 2 resends
MAX_VALIDATE_ATTEMPTS = 5        # before lockout
VALIDATION_TOKEN_MINUTES = 5      # window the driver has to call submit_proof


def _hash_otp(otp):
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _generate_otp():
    """6-digit numeric code. Uses Frappe's secure RNG (random_string is
    crypto-grade per their docs)."""
    import secrets
    return f"{secrets.randbelow(900000) + 100000:06d}"


def _send_sms(mobile, otp):
    """Dispatch OTP via SMS. We don't have an SMS provider wired on
    this bench yet; for now log a structured entry the operator can
    forward manually. When the SMS settings are configured Frappe's
    stock send_sms picks them up automatically and this call goes
    over-the-wire.
    """
    try:
        from frappe.core.doctype.sms_settings.sms_settings import send_sms
        message = f"Your delivery OTP is {otp}. Valid for {OTP_VALID_MINUTES} minutes."
        send_sms([mobile], message)
    except Exception:
        # No SMS provider — log so the dev/operator can see the OTP in
        # the bench logs without it leaking into the API response.
        frappe.logger("erpera_driver_app").info(
            f"[PoD OTP] dispatch placeholder: mobile={mobile} otp={otp}"
        )


def _gate_cod_online(dn):
    """COD-Online hard gate: razorpay_payment_status must be Confirmed."""
    if (dn.get("cowberry_payment_method") or "") == "COD-Online":
        if dn.get("razorpay_payment_status") != "Confirmed":
            return err(
                "PAYMENT_NOT_CONFIRMED",
                "The customer's Razorpay payment is not yet confirmed. "
                "Poll payment.get_status and try again once it's Confirmed.",
                400,
                polling_url="/api/method/erpera_driver_app.api.payment.get_status",
            )
    return None


@frappe.whitelist(methods=["POST"])
def request_pod_otp(delivery_note=None):
    """§1 Request a fresh OTP for the customer. First call sets
    otp_attempts=1; subsequent calls bump the counter and rotate the
    OTP (which also invalidates the prior SMS). Hard-capped at 3.
    """
    try:
        _require_driver()
        if not delivery_note:
            return err("VALIDATION_ERROR", "`delivery_note` is required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)
        dn = frappe.get_doc("Delivery Note", delivery_note)

        gate = _gate_cod_online(dn)
        if gate:
            return gate

        attempts = cint(dn.get("otp_attempts")) + 1
        if attempts > MAX_SEND_ATTEMPTS:
            return err("OTP_MAX_ATTEMPTS",
                       f"Maximum OTP send attempts ({MAX_SEND_ATTEMPTS}) reached for this delivery.",
                       429)

        otp = _generate_otp()
        now = now_datetime()
        expires = add_to_date(now, minutes=OTP_VALID_MINUTES)

        frappe.db.set_value(
            "Delivery Note", delivery_note,
            {
                "otp_hash":              _hash_otp(otp),
                "otp_requested_at":      now,
                "otp_expires_at":        expires,
                "otp_attempts":          attempts,
                "otp_validate_attempts": 0,        # fresh OTP resets validate-attempt budget
                "otp_validated":         0,
                "validation_token":      "",
                "validation_token_expires_at": None,
            },
            update_modified=False,
        )
        frappe.db.commit()

        mobile = dn.contact_mobile or frappe.db.get_value("Customer", dn.customer, "mobile_no")
        if mobile:
            _send_sms(mobile, otp)

        return ok(data={
            "otp_requested_at":  str(now),
            "otp_valid_minutes": OTP_VALID_MINUTES,
            "channel":           "SMS",
            "otp_attempts":      attempts,
        })
    except Exception as e:
        return err("REQUEST_OTP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def resend_pod_otp(delivery_note=None):
    """§2 Re-send an OTP. Same logic as request — just exists for the
    Flutter UX of a distinct "Resend" button. New OTP invalidates the
    previous one; customer must use the latest SMS.
    """
    return request_pod_otp(delivery_note=delivery_note)


@frappe.whitelist(methods=["POST"])
def validate_pod_otp(delivery_note=None, otp=None):
    """§3 Compare submitted OTP against the stored SHA-256 hash. On
    success issue a single-use validation_token (5-min validity) that
    the Flutter client passes straight to pod.submit_proof.

    Hardened: max 5 validate attempts before lockout (caller must
    request a fresh OTP via §1/§2 to reset the counter).
    """
    try:
        _require_driver()
        if not delivery_note or not otp:
            return err("VALIDATION_ERROR",
                       "Both `delivery_note` and `otp` are required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)
        dn = frappe.get_doc("Delivery Note", delivery_note)

        if cint(dn.get("otp_validated")):
            return err("OTP_ALREADY_USED",
                       "This OTP has already been validated. Use the existing token or request a new OTP.",
                       400)

        stored_hash = dn.get("otp_hash")
        if not stored_hash:
            return err("OTP_INVALID",
                       "No OTP has been requested for this delivery. Send one first.", 400)

        attempts = cint(dn.get("otp_validate_attempts")) + 1
        if attempts > MAX_VALIDATE_ATTEMPTS:
            return err("OTP_MAX_ATTEMPTS",
                       f"Maximum OTP validation attempts ({MAX_VALIDATE_ATTEMPTS}) reached. "
                       "Request a fresh OTP to continue.", 429)

        expires = dn.get("otp_expires_at")
        if expires and now_datetime() > expires:
            frappe.db.set_value("Delivery Note", delivery_note,
                                "otp_validate_attempts", attempts,
                                update_modified=False)
            frappe.db.commit()
            return err("OTP_EXPIRED",
                       "The OTP has expired. Request a fresh one.", 400)

        if _hash_otp(str(otp).strip()) != stored_hash:
            frappe.db.set_value("Delivery Note", delivery_note,
                                "otp_validate_attempts", attempts,
                                update_modified=False)
            frappe.db.commit()
            return err("OTP_INVALID",
                       f"Invalid OTP ({attempts} of {MAX_VALIDATE_ATTEMPTS} attempts used).", 400)

        token = "tkn_" + frappe.generate_hash(length=16)
        token_expires = add_to_date(now_datetime(), minutes=VALIDATION_TOKEN_MINUTES)
        frappe.db.set_value(
            "Delivery Note", delivery_note,
            {
                "otp_validated":               1,
                "otp_validate_attempts":       attempts,
                "validation_token":            token,
                "validation_token_expires_at": token_expires,
            },
            update_modified=False,
        )
        frappe.db.commit()
        return ok(data={
            "valid":            True,
            "validation_token": token,
        })
    except Exception as e:
        return err("VALIDATE_OTP_FAILED", str(e))
