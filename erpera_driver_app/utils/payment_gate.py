"""The COD-Online gate, generalised across payment providers.

A COD-Online order must not have its POD OTP issued until the money is in,
or a driver can hand the parcel over and walk away unpaid. Until CD2-I21
that gate was a single literal check against `razorpay_payment_status`,
written in two places (`api.order.send_delivery_otp` and
`api.otp._gate_cod_online`).

Adding Juspay UPI QR as a second provider means the gate now has to ask "did
*any* provider confirm?" rather than "did Razorpay confirm?". Both call sites
delegate here instead, so there is one answer to that question and adding a
third provider later touches one function.

The gate stays in THIS app even though Juspay lives in the separate
`juspay_integration` app, and it imports nothing from it. It reads
`juspay_payment_status` off the Delivery Note — a custom field that app
creates and writes — behind a meta guard. So the coupling runs one way and
through data: the driver app never needs Juspay installed, and Juspay never
needs to know what a POD gate is. Whether a parcel may be handed over is a
delivery decision, not a payment one.

Deliberately unchanged: a confirmed Razorpay payment still opens the gate
exactly as before. The original one-line checks are kept verbatim as `OLD`
comments at both call sites.
"""

import frappe


def payment_confirmed(dn):
    """True when `dn` has a confirmed online payment from any provider.

    Accepts a Delivery Note Document, a SQL row, or a plain dict — anything
    with `.get()`.

    Meta-guarded: `juspay_payment_status` is read only if the field exists, so
    this is safe on a bench where the CD2-I21 custom fields have not been
    created yet. There it behaves identically to the old Razorpay-only check.
    """
    if (dn.get("razorpay_payment_status") or "") == "Confirmed":
        return True
    if "juspay_payment_status" in _dn_payment_fields():
        if (dn.get("juspay_payment_status") or "") == "Confirmed":
            return True
    return False


def confirmed_provider(dn):
    """Which provider confirmed, for message and receipt purposes."""
    if (dn.get("razorpay_payment_status") or "") == "Confirmed":
        return "Razorpay"
    if "juspay_payment_status" in _dn_payment_fields():
        if (dn.get("juspay_payment_status") or "") == "Confirmed":
            return "UPI"
    return None


def is_cod_online(dn):
    return (dn.get("cowberry_payment_method") or "") == "COD-Online"


def _dn_payment_fields():
    """Delivery Note fieldnames, cached per request.

    Mirrors `api.trip._dn_field_names`: the app runs against benches whose
    custom fields differ, and reading a field that isn't there is the single
    most common way an endpoint 500s on one site and works on another.
    """
    cache = getattr(frappe.local, "_eda_dn_payment_fields", None)
    if cache is None:
        try:
            cache = {f.fieldname for f in frappe.get_meta("Delivery Note").fields}
        except Exception:
            cache = set()
        frappe.local._eda_dn_payment_fields = cache
    return cache
