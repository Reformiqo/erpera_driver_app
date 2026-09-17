"""Tunables for the analytics screen, read from the `Analytics Settings` Single.

Every number the analytics endpoints used to hard-code now lives on that Single,
so an operations lead can retune the dashboard from the desk without a code
change or a deploy.

Three rules this module keeps, because it is called from inside a whitelisted
endpoint that must not fail:

1. **It never raises.** A bench where the Single has not been migrated yet — or
   where a field was removed — falls back to `DEFAULTS`, which reproduce the
   behaviour the code had before these settings existed. The dashboard keeps
   working; it simply stops being configurable.
2. **The saved value wins, including zero.** Only `None` (no row, no column)
   falls through to the default, so an admin who deliberately sets a limit to 0
   gets 0 rather than the default silently coming back.
3. **One read per request.** The whole Single is pulled once and cached on
   `frappe.local`, so a call that touches thirteen sections costs one query
   rather than thirty.
"""
import frappe
from frappe.utils import cint, flt

SETTINGS = "Analytics Settings"

# These are the exact constants analytics.py carried before this module existed.
# Keeping them here means an un-migrated bench behaves identically to the old
# code rather than falling over.
DEFAULTS = {
    # Performance score weights, as percentages of the 0-100 gauge.
    "on_time_weight":             40.0,
    "success_weight":             30.0,
    "cod_weight":                 20.0,
    "comms_weight":               10.0,
    "cod_score_mode":             "Binary",
    # Comparison thresholds, in score points.
    "period_delta_threshold":     0.5,
    "fleet_delta_threshold":      1.0,
    # Rolling windows, in days (today inclusive).
    "week_days":                  7,
    "month_days":                 30,
    # COD history chart.
    "cod_history_buckets":        4,
    "currency_label":             "INR",
    # Delay heatmap: hour cut-offs and the count thresholds behind each word.
    "heatmap_morning_end_hour":   11,
    "heatmap_midday_end_hour":    13,
    "heatmap_afternoon_end_hour": 17,
    "heatmap_some_max":           2,
    "heatmap_often_max":          5,
    # Section behaviour.
    "eta_fallback_enabled":       1,
    "per_stop_variance_mode":     "Cash Rounding",
    "cash_variance_group_by":     "Day",
    # Arrival-time estimates.
    "default_minutes_per_stop":   30,
    "eta_trend_threshold_mins":   2,
    "timing_outlier_max_mins":    1440,
    # Longest list each section returns.
    "trip_timeline_limit":        30,
    "cash_compliance_limit":      15,
    "limit_breach_limit":         20,
    "discrepancy_limit":          20,
    "per_stop_variance_limit":    20,
    "best_worst_count":           3,
}

_CACHE_KEY = "_erpera_analytics_settings"


def _stored():
    """Every saved value on the Single, as a plain dict. `{}` when unavailable.

    `frappe.get_cached_doc` raises for a DocType that has not been synced onto
    this bench, and a Single nobody has opened has no row in `tabSingles` at
    all — both land here as an empty dict and every lookup then falls through
    to `DEFAULTS`.
    """
    cached = getattr(frappe.local, _CACHE_KEY, None)
    if cached is not None:
        return cached

    values = {}
    try:
        if frappe.db.exists("DocType", SETTINGS):
            doc = frappe.get_cached_doc(SETTINGS)
            # Only fields that actually exist on this bench's copy of the
            # DocType, so a field added later never KeyErrors an older site.
            for field in frappe.get_meta(SETTINGS).fields:
                if field.fieldname in DEFAULTS:
                    values[field.fieldname] = doc.get(field.fieldname)
    except Exception:
        # Never let a settings read break the dashboard.
        frappe.log_error(frappe.get_traceback(),
                         "Analytics Settings unreadable - using defaults")
        values = {}

    setattr(frappe.local, _CACHE_KEY, values)
    return values


def get(fieldname):
    """One setting, falling back to its pre-settings default."""
    value = _stored().get(fieldname)
    if value is None or value == "":
        return DEFAULTS.get(fieldname)
    return value


def number(fieldname):
    """A setting as a float."""
    return flt(get(fieldname))


def count(fieldname, minimum=0):
    """A setting as a non-negative int, floored at `minimum`.

    Row limits and bucket counts go through here: a negative limit would make
    `LIMIT -1` a SQL error, and a zero bucket count would divide by zero.
    """
    return max(cint(get(fieldname)), minimum)


def enabled(fieldname):
    """A checkbox setting as a bool.

    Separate from `count` because a checkbox default of 1 must survive a
    stored 0: `count` would floor both at their integer value, which is
    correct, but reading a toggle through a counter reads badly at the call
    site.
    """
    return bool(cint(get(fieldname)))


def score_weights():
    """The four score weights as fractions that sum to 1.

    Stored as readable percentages (40 / 30 / 20 / 10) and normalised here, so
    the composite stays on a 0-100 scale even if someone saves weights that add
    up to something other than 100 — the DocType's `validate` refuses that, but
    a value written straight to the database bypasses `validate`, and a gauge
    that silently reads out of 137 is worse than one that rescales.
    """
    raw = {
        "on_time_weight": number("on_time_weight"),
        "success_weight": number("success_weight"),
        "cod_weight":     number("cod_weight"),
        "comms_weight":   number("comms_weight"),
    }
    total = sum(raw.values())
    if total <= 0:
        # Every weight zeroed would make every driver score 0 with no
        # explanation. Fall back to the documented split instead.
        raw = {k: DEFAULTS[k] for k in raw}
        total = sum(raw.values())
    return {k: v / total for k, v in raw.items()}


def weight_labels():
    """`{"on_time_weight": "(40%)"}` — the caption under each component tile.

    Derived from the same stored weights the score uses, so the caption can
    never drift from the arithmetic the way a hard-coded "(40%)" string did.
    """
    return {k: f"({round(v * 100)}%)" for k, v in score_weights().items()}
