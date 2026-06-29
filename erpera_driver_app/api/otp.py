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


def _send_sms(mobile, otp, reference_dn=None):
    """Dispatch OTP via SMS, surfacing failures clearly (CD2-I5 point 5).

    Calls Frappe's stock SMS gateway. Previously this swallowed all
    exceptions silently, which caused the bug Hardik reported: API
    returns success, customer never receives OTP, no log trail.

    Now the failure path:
      1. Logs a SEVERE error to the erpera_driver_app logger with the
         provider's actual exception (for admin grep'ing).
      2. Writes a Comment on the linked Delivery Note so the driver
         operations team can see "OTP requested but SMS dispatch
         failed: <reason>" right on the DN.
      3. Returns a failure indicator so the caller can surface a more
         honest API response if desired (e.g. PAYMENT_PROVIDER_DOWN).

    Returns: True on success, False on failure.
    """
    message = f"Your delivery OTP is {otp}. Valid for {OTP_VALID_MINUTES} minutes."
    try:
        from frappe.core.doctype.sms_settings.sms_settings import send_sms
        send_sms([mobile], message)
        return True
    except Exception as e:
        err_msg = f"SMS dispatch failed: {type(e).__name__}: {e}"
        frappe.logger("erpera_driver_app").error(
            f"[PoD OTP] {err_msg} mobile={mobile} dn={reference_dn}"
        )
        # Drop a Comment on the DN so ops can see this in the document
        # timeline without trawling the bench logs.
        if reference_dn:
            try:
                frappe.get_doc({
                    "doctype":         "Comment",
                    "comment_type":    "Comment",
                    "reference_doctype": "Delivery Note",
                    "reference_name":  reference_dn,
                    "content":         f"⚠ OTP dispatched but SMS gateway failed: {err_msg}",
                }).insert(ignore_permissions=True)
            except Exception:
                pass
        return False


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


def _trip_for_dn(delivery_note):
    """Return the most recent Delivery Trip linked to this DN, or None.

    The new MSG91 send endpoint needs a delivery_trip to authorise the
    caller against. We pick the latest stop's parent because a DN may be
    rescheduled across trips.
    """
    row = frappe.db.sql(
        """SELECT ds.parent
             FROM `tabDelivery Stop` ds
             JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
            WHERE ds.delivery_note = %s
            ORDER BY dt.modified DESC LIMIT 1""",
        delivery_note,
    )
    return row[0][0] if row else None


@frappe.whitelist(methods=["POST"])
def request_pod_otp(delivery_note=None):
    """Send a delivery PoD OTP.

    DEPRECATED endpoint kept for backwards compatibility with existing
    Flutter builds. Internally delegates to
    ``cowberry_app.api.otp.send_delivery_otp`` which uses MSG91 as the
    transport and the Cowberry OTP Log as the source of truth. The
    response shape below is preserved unchanged for the legacy app.
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

        trip = _trip_for_dn(delivery_note)
        if not trip:
            return err(
                "TRIP_NOT_FOUND",
                "This Delivery Note isn't on any Delivery Trip; cannot authorise OTP.",
                400,
            )

        from cowberry_app.api.otp import send_delivery_otp

        try:
            new_res = send_delivery_otp(delivery_trip=trip, delivery_note=delivery_note)
        except frappe.ValidationError as ve:
            # Surface MSG91 errors / cap violations under the legacy code.
            return err("REQUEST_OTP_FAILED", str(ve), 400)

        attempts = MAX_SEND_ATTEMPTS - int(new_res.get("resends_remaining") or 0)
        return ok(data={
            "otp_requested_at":  str(now_datetime()),
            "otp_valid_minutes": int((new_res.get("expires_in") or 600) / 60),
            "channel":           "SMS",
            "otp_attempts":      attempts,
            "sms_dispatched":    True,
            "otp_log":           new_res.get("otp_log"),
            "masked_mobile":     new_res.get("masked_mobile"),
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
    """Validate a PoD OTP and issue a single-use validation_token.

    DEPRECATED endpoint kept for backwards compatibility with existing
    Flutter builds. Delegates verification to
    ``cowberry_app.api.otp.verify_delivery_otp`` (MSG91 + HMAC-SHA256 +
    Cowberry OTP Log as source of truth), but passes ``complete=False``
    so the legacy ``pod.submit_proof`` continues to own the actual
    delivery completion (Sales Invoice, Driver Collection,
    daily-collection-limit check, etc.).
    """
    try:
        _require_driver()
        if not delivery_note or not otp:
            return err("VALIDATION_ERROR",
                       "Both `delivery_note` and `otp` are required.", 400)
        if not frappe.db.exists("Delivery Note", delivery_note):
            return err("NOT_FOUND", f"Delivery Note '{delivery_note}' not found.", 404)

        from cowberry_app.api.otp import verify_delivery_otp

        try:
            verify_delivery_otp(
                otp=otp,
                delivery_note=delivery_note,
                delivery_trip=_trip_for_dn(delivery_note),
                complete=False,
            )
        except frappe.ValidationError as ve:
            return err("OTP_INVALID", str(ve), 400)

        # Issue the legacy validation_token so the Flutter client can
        # call pod.submit_proof next. The new MSG91 source-of-truth is
        # already marked Verified in the OTP Log; this token only
        # bridges the financial-side flow until clients migrate.
        token = "tkn_" + frappe.generate_hash(length=16)
        token_expires = add_to_date(now_datetime(), minutes=VALIDATION_TOKEN_MINUTES)
        frappe.db.set_value(
            "Delivery Note", delivery_note,
            {
                "otp_validated":               1,
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
