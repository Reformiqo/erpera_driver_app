"""COD amount resolution.

Cash is handed over in whole rupees, so what a driver must collect is the
Delivery Note's `rounded_total`, not `grand_total` — an order totalling
336.78 is collected as 337.00 and ERPNext books the 0.22 difference to the
company's Round Off account.

`grand_total` remains the order's true, paise-exact value and is what revenue
reporting keeps using. Only "cash a human hands over" resolves through here.
"""

from frappe.utils import flt


def expected_cod(dn):
    """Cash the driver must collect for `dn`.

    Accepts a Delivery Note Document, a `frappe._dict` SQL row, or a plain
    dict — anything with `.get()`.

    Precedence:
      1. `cod_amount`      — explicit per-order override, when set
      2. `rounded_total`   — the whole-rupee figure cash is collected in
      3. `grand_total`     — fallback

    `rounded_total` is 0 when Global Defaults has *Disable Rounded Total*
    ticked, so the `grand_total` fallback keeps this correct on those sites.
    """
    return flt(
        dn.get("cod_amount")
        or dn.get("rounded_total")
        or dn.get("grand_total")
        or 0
    )


def collected_cod(dn):
    """Cash actually collected for `dn`, for reconciliation against a driver.

    Reads `cod_collected_amount`, which `pod.submit_proof` stamps at delivery.
    Deliveries recorded before that field was populated carry 0 — those fall
    back to `expected_cod` so historical totals don't read as zero.

    Use this, never `grand_total`, when the number represents physical cash in
    a driver's hands: after rounding the two differ by up to 0.50 per order,
    and that gap compounds across a day's deliveries until cash submission
    stops reconciling.
    """
    return flt(dn.get("cod_collected_amount") or 0) or expected_cod(dn)
