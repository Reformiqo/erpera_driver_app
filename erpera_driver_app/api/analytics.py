import frappe
from frappe.utils import add_days, flt, get_datetime, getdate, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.api.trip import (
    delivered_at_sql,
    payment_columns,
    payment_type_from_row,
)
from erpera_driver_app.utils.cod import collected_cod
from erpera_driver_app.utils.response import err, ok


# Composite-score weights per FRD §11 (the spec's score_breakdown surfaces
# them so the Flutter dashboard can render a stacked-weight gauge).
SCORE_WEIGHTS = {
    "on_time_weight": 0.4,
    "success_weight": 0.3,
    "cod_weight":     0.2,
    "comms_weight":   0.1,
}

# A stop counts towards success/reschedule rates once the driver has actually
# worked it. "Pending" is work not yet done, not work done badly — leaving it
# in the denominator made every in-progress trip look like a failing one.
ATTEMPTED_STATUSES = {"Delivered", "Failed", "Returned", "Rescheduled"}
FAILED_STATUSES = {"Failed", "Returned"}


def _num(value, default=0):
    """Coerce a not-measurable metric to a number for the wire.

    The section builders carry `None` for "couldn't be measured" so the
    composite can drop that component instead of scoring it zero. The JSON
    keys stay numeric, because released Flutter builds format them directly;
    the matching `*_measurable` flag is what tells the client to render a dash.
    """
    return default if value is None else value


# ---------------------------------------------------------------------------
# Shared driver-scoped query helpers — used by every section builder.
# ---------------------------------------------------------------------------

def _dedupe_by_delivery_note(rows):
    """One row per Delivery Note, keeping the most recent trip's stop.

    A Delivery Note can sit on more than one Delivery Stop — re-added to the
    same trip, or moved to a later trip after a failed attempt — and the
    three-table join returns it once per stop. Counting those rows straight
    made a single delivery register as two: deliveries, COD collected and the
    success denominator all inflated together, which is how a 30-day window
    reported more completed deliveries than the site had Delivered notes in
    total.

    Rows arrive ordered by departure_time ASC, so overwriting on each hit
    leaves the latest trip's stop — the one whose ETA the delivery was
    actually judged against.
    """
    by_dn = {}
    for r in rows:
        by_dn[r.delivery_note] = r
    return list(by_dn.values())


def _driver_dn_rows(driver, d_from, d_to):
    """Every Delivery Note reachable through the driver's trips in the
    window, one row per note. Returns per-row: delivery_note,
    delivery_status, payment_type, grand_total, delivered_at,
    expected_arrival, trip, departure_time, stop_idx, customer_name.

    Filter uses trip departure/creation (same rule as delivery.history)
    so ranking + windowing stay consistent across screens."""
    if not driver:
        return []
    pay_cols = payment_columns()
    pay_select = "".join(f", dn.`{c}` AS `{c}`" for c in pay_cols)
    rows = frappe.db.sql(
        f"""
        SELECT dn.name                        AS delivery_note,
               dn.cowberry_delivery_status    AS delivery_status,
               dn.grand_total                 AS grand_total,
               dn.rounded_total               AS rounded_total,
               dn.cod_amount                  AS cod_amount,
               dn.cod_collected_amount        AS cod_collected_amount,
               {delivered_at_sql()}           AS delivered_at,
               dn.customer_name               AS customer_name,
               ds.estimated_arrival           AS expected_arrival,
               ds.idx                         AS stop_idx,
               dt.name                        AS trip,
               dt.departure_time              AS departure_time
               {pay_select}
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE dt.driver = %(driver)s
           AND dn.docstatus = 1
           AND (
                 (DATE(dt.departure_time) BETWEEN %(d_from)s AND %(d_to)s)
              OR (dt.departure_time IS NULL
                  AND DATE(dt.creation) BETWEEN %(d_from)s AND %(d_to)s)
           )
         ORDER BY dt.departure_time ASC, ds.idx ASC
        """,
        {"driver": driver, "d_from": d_from, "d_to": d_to},
        as_dict=True,
    )
    for r in rows:
        # Resolved once here so every section agrees, and so a payment method
        # parked in `payment_type` or `delhivery_payment_mode` still reads as
        # COD — reading only `cowberry_payment_method` dropped those orders
        # out of the COD total entirely. Deliberately not called `payment_type`:
        # that is itself one of the source columns selected above.
        r["pay_type"] = payment_type_from_row(r, pay_cols)
    return _dedupe_by_delivery_note(rows)


def _summarise(rows):
    """Common counters used by performance_score + key_metrics + vs_fleet.
    Kept as a single pass so we don't re-scan the row set per section.

    Rates are `None` when the underlying data cannot support them — no
    attempted stops, or no stop with both an ETA and a delivery time. Callers
    put a number on the wire via `_num()` and ship a `*_measurable` flag
    beside it; the composite drops the component entirely rather than
    scoring it zero, because "we could not measure this" and "the driver
    scored zero on this" are not the same statement.
    """
    total = len(rows)
    attempted = delivered = failed = rescheduled = on_time = 0
    cod_orders = 0
    cod_collected = 0.0
    delays = []     # positive minute deltas for delivered rows only
    comparable = 0  # delivered rows where punctuality could actually be judged
    for r in rows:
        status = (r.delivery_status or "").strip()
        is_cod = (r.get("pay_type") or "") == "COD"
        if is_cod:
            cod_orders += 1
        if status in ATTEMPTED_STATUSES:
            attempted += 1
        if status == "Delivered":
            delivered += 1
            if is_cod:
                # Cash actually taken, not order value — see utils.cod.
                cod_collected += collected_cod(r)
            if r.expected_arrival and r.delivered_at:
                comparable += 1
                delta_mins = (r.delivered_at - r.expected_arrival).total_seconds() / 60
                if delta_mins <= 0:
                    on_time += 1
                else:
                    delays.append(delta_mins)
        elif status in FAILED_STATUSES:
            failed += 1
        elif status == "Rescheduled":
            rescheduled += 1

    success_rate = round((delivered / attempted) * 100, 1) if attempted else None
    # Denominator is the stops that could be judged, not every delivered stop.
    # Delivery Stop.estimated_arrival is filled by ERPNext's route pass, which
    # needs a Maps key; where it is blank the old denominator counted the stop
    # as late and pinned the rate at 0%.
    on_time_rate = round((on_time / comparable) * 100, 1) if comparable else None
    reschedule_rate = round((rescheduled / attempted) * 100, 1) if attempted else None
    avg_delay = round(sum(delays) / len(delays), 1) if delays else None
    # COD stays the spec's binary "did any cash come in" signal, but a driver
    # who was never given a COD order is not scored on it.
    cod_component = None if not cod_orders else (100.0 if cod_collected > 0 else 0.0)

    return {
        "total":           total,
        "attempted":       attempted,
        "delivered":       delivered,
        "failed":          failed,
        "rescheduled":     rescheduled,
        "on_time":         on_time,
        "comparable":      comparable,
        "cod_orders":      cod_orders,
        "cod_collected":   cod_collected,
        "delays":          delays,
        "success_rate":    success_rate,
        "on_time_rate":    on_time_rate,
        "reschedule_rate": reschedule_rate,
        "avg_delay":       avg_delay,
        "cod_component":   cod_component,
        "composite":       _composite(success_rate, on_time_rate, cod_component),
        "coverage":        _coverage(success_rate, on_time_rate, cod_component),
    }


def _score_parts(success_rate, on_time_rate, cod_component):
    """(value, weight) for each scorecard component that has a real signal.

    `comms` is absent by design: nothing on this site records driver-to-
    customer contact yet. It used to be filled with a second copy of
    success_rate, which quietly gave success 40% of the score instead of 30%.
    """
    return [
        (on_time_rate,   SCORE_WEIGHTS["on_time_weight"]),
        (success_rate,   SCORE_WEIGHTS["success_weight"]),
        (cod_component,  SCORE_WEIGHTS["cod_weight"]),
        (None,           SCORE_WEIGHTS["comms_weight"]),   # comms — no source
    ]


def _composite(success_rate, on_time_rate, cod_component):
    """Weighted score over the components that could be measured.

    Weights are re-normalised across those components, so an unmeasurable one
    neither drags the score down nor silently counts as full marks.
    """
    live = [(v, w) for v, w in _score_parts(success_rate, on_time_rate, cod_component)
            if v is not None]
    live_weight = sum(w for _, w in live)
    if not live_weight:
        return 0
    return round(sum(v * w for v, w in live) / live_weight)


def _coverage(success_rate, on_time_rate, cod_component):
    """How much of the designed scorecard the composite actually rests on.

    100 means every component had data. The Flutter gauge should caption the
    score with this — a 96 built on half the scorecard is not a 96.
    """
    parts = _score_parts(success_rate, on_time_rate, cod_component)
    live = sum(w for v, w in parts if v is not None)
    return round(live / sum(w for _, w in parts) * 100, 1)


def _fleet_summary(d_from, d_to):
    """Fleet-wide summary across ALL drivers in the window.

    Also reports how many drivers the average is built from: when that is 1
    the "fleet" is the driver themselves, and the comparison on the dashboard
    means nothing — the client needs to be able to hide it.
    """
    pay_cols = payment_columns()
    pay_select = "".join(f", dn.`{c}` AS `{c}`" for c in pay_cols)
    rows = frappe.db.sql(
        f"""
        SELECT dn.name                        AS delivery_note,
               dn.cowberry_delivery_status    AS delivery_status,
               dn.grand_total                 AS grand_total,
               dn.rounded_total               AS rounded_total,
               dn.cod_amount                  AS cod_amount,
               dn.cod_collected_amount        AS cod_collected_amount,
               {delivered_at_sql()}           AS delivered_at,
               ds.estimated_arrival           AS expected_arrival,
               dt.departure_time              AS departure_time,
               dt.driver                      AS driver
               {pay_select}
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE dn.docstatus = 1
           AND (
                 (DATE(dt.departure_time) BETWEEN %(d_from)s AND %(d_to)s)
              OR (dt.departure_time IS NULL
                  AND DATE(dt.creation) BETWEEN %(d_from)s AND %(d_to)s)
           )
         ORDER BY dt.departure_time ASC
        """,
        {"d_from": d_from, "d_to": d_to},
        as_dict=True,
    )
    for r in rows:
        r["pay_type"] = payment_type_from_row(r, pay_cols)
    drivers = {r.driver for r in rows if r.driver}
    summary = _summarise(_dedupe_by_delivery_note(rows))
    summary["driver_count"] = len(drivers)
    return summary


def _wallet_topups(employee, d_from, d_to):
    """Wallet Transactions for driver top-ups in the window. The
    driver-app writes reference='Driver top-up by <EMP-...>' so we key
    off the employee prefix."""
    row = frappe.db.sql(
        """SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS v
             FROM `tabWallet Transaction`
            WHERE docstatus = 1
              AND reference LIKE %(prefix)s
              AND DATE(creation) BETWEEN %(d_from)s AND %(d_to)s""",
        {"prefix": f"Driver top-up by {employee}%",
         "d_from": d_from, "d_to": d_to},
        as_dict=True,
    )[0]
    return int(flt(row.n)), flt(row.v)


# ===========================================================================
# CD2-I5 Point 8 — Unified My Analytics screen API (13 sections)
# ===========================================================================

@frappe.whitelist(methods=["GET"])
def get_screen(period="month", **kwargs):
    """Single endpoint powering the entire My Analytics screen.

    Returns 13 sections in one payload so the Flutter dashboard renders
    in one pass. Every section is real data — no more stubs. When a
    section has no source rows in the period, we return empty arrays or
    zeros rather than dummies so the UI can distinguish "no data" from
    "not yet computed".

    Query:
        period — today | week | month | custom (custom needs from+to)
    """
    try:
        employee = _require_driver()
        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)

        d_from, d_to = _period_window(
            period,
            kwargs.get("from") or kwargs.get("from_date"),
            kwargs.get("to")   or kwargs.get("to_date"),
        )
        if d_from is None:
            return err("VALIDATION_ERROR",
                       "period=custom requires both `from` and `to`.", 400)

        # Pull the driver row set ONCE and hand it to every section that
        # needs it. Section builders that reach into other tables
        # (cash_submission_compliance, discrepancy_history, wallet)
        # query on their own.
        dn_rows = _driver_dn_rows(driver, d_from, d_to)
        summary = _summarise(dn_rows)
        prev_summary = _summarise(_driver_dn_rows(
            driver,
            add_days(d_from, -(d_to - d_from).days - 1),
            add_days(d_from, -1),
        ))
        fleet = _fleet_summary(d_from, d_to)

        return ok(data={
            "period":                     period,
            "from":                       str(d_from),
            "to":                         str(d_to),
            "performance_score":          _performance_score(summary, prev_summary, fleet),
            "key_metrics":                _key_metrics(employee, summary, d_from, d_to),
            "timing_compliance":          _timing_compliance(employee, driver, dn_rows, d_from, d_to),
            "vs_fleet":                   _vs_fleet(summary, fleet),
            "daily_cod_history":          _daily_cod_history(dn_rows, d_from, d_to),
            "cash_submission_compliance": _cash_submission_compliance(employee, d_from, d_to),
            "collection_limit_breaches":  _collection_limit_breaches(employee, d_from, d_to),
            "discrepancy_history":        _discrepancy_history(employee, d_from, d_to),
            "trip_timeline":              _trip_timeline_section(driver, d_from, d_to),
            "delay_heatmap":              _delay_heatmap(dn_rows),
            "worst_vs_best_trips":        _worst_vs_best_trips(driver, d_from, d_to),
            "per_stop_variance":          _per_stop_variance(dn_rows),
            "eta_accuracy_trend":         _eta_accuracy_trend(dn_rows),
        })
    except Exception as e:
        return err("GET_ANALYTICS_SCREEN_FAILED", str(e))


# ---------------------------------------------------------------------------
# Section builders — every one reads from real tables. When a section has
# no rows in the period we return empty arrays so the Flutter UI can render
# a "no data" state rather than showing fake values.
# ---------------------------------------------------------------------------

def _performance_score(summary, prev, fleet):
    """Section 1 — Performance Score gauge + components.

    Each component reports whether it was measurable. `weight_pct` keeps its
    existing numeric meaning (the component's rate, despite the name) so
    released clients keep rendering; `measurable` is what tells a newer client
    to show a dash instead of a zero, and `unavailable_reason` says why.
    """
    # A first period with nothing in it is not an improvement. Saying "Up 35"
    # against an empty window reads as progress the driver never made.
    if not prev["attempted"] and not prev["total"]:
        delta_label = "No data for last period"
    else:
        delta = summary["composite"] - prev["composite"]
        if delta > 0.5:
            delta_label = f"Up {round(delta, 1)} vs last period"
        elif delta < -0.5:
            delta_label = f"Down {abs(round(delta, 1))} vs last period"
        else:
            delta_label = "Flat vs last period"

    fleet_comparable = fleet.get("driver_count", 0) > 1
    if not fleet_comparable:
        fleet_label = "No other drivers active this period"
    else:
        fleet_delta = summary["composite"] - fleet["composite"]
        if fleet_delta > 1:
            fleet_label = "You are above average"
        elif fleet_delta < -1:
            fleet_label = "You are below average"
        else:
            fleet_label = "You are at fleet average"

    def _component(name, value, weight_key, label, reason):
        return {
            "name":               name,
            "weight_pct":         _num(value),
            "weight_label":       label,
            "value":              value,
            "measurable":         value is not None,
            "design_weight_pct":  round(SCORE_WEIGHTS[weight_key] * 100),
            "unavailable_reason": None if value is not None else reason,
        }

    return {
        "score":                summary["composite"],
        # What share of the designed scorecard the score is actually built on.
        "score_coverage_pct":   summary["coverage"],
        "vs_last_month_delta":  delta_label,
        "fleet_avg_score":      fleet["composite"],
        "fleet_avg_comparison": fleet_label,
        "fleet_driver_count":   fleet.get("driver_count", 0),
        "fleet_comparable":     fleet_comparable,
        "components": [
            _component("On-Time", summary["on_time_rate"], "on_time_weight", "(40%)",
                       "No stop in this period had both an estimated arrival "
                       "and a delivery time."),
            _component("Success", summary["success_rate"], "success_weight", "(30%)",
                       "No delivery was attempted in this period."),
            _component("COD", summary["cod_component"], "cod_weight", "(20%)",
                       "No COD order was assigned in this period."),
            _component("Comms", None, "comms_weight", "(10%)",
                       "Driver-to-customer contact is not recorded yet."),
        ],
    }


def _key_metrics(employee, summary, d_from, d_to):
    """Section 2 — Key Metrics tiles.

    The `*_pct` keys stay numeric for released clients. The `*_measurable`
    flags and the sample counts beside them are what let a tile render "—"
    rather than a 0% the data never supported.
    """
    topup_count, topup_amount = _wallet_topups(employee, d_from, d_to)
    return {
        "deliveries_completed": summary["delivered"],
        "success_rate_pct":     _num(summary["success_rate"]),
        "success_measurable":   summary["success_rate"] is not None,
        # Stops that were actually worked — the success denominator. Pending
        # stops are excluded: an in-progress trip is not a failed one.
        "attempted_count":      summary["attempted"],
        "on_time_rate_pct":     _num(summary["on_time_rate"]),
        "on_time_measurable":   summary["on_time_rate"] is not None,
        # Deliveries that carried both an ETA and a delivery time, i.e. the
        # ones punctuality could be judged on.
        "on_time_sample":       summary["comparable"],
        "avg_delay_mins":       _num(summary["avg_delay"]),
        "avg_delay_measurable": summary["avg_delay"] is not None,
        "cod_collected":        summary["cod_collected"],
        "cod_orders":           summary["cod_orders"],
        "wallet_topups": {
            "count":  topup_count,
            "amount": topup_amount,
        },
        "reschedule_rate_pct":  _num(summary["reschedule_rate"]),
        "reschedule_measurable": summary["reschedule_rate"] is not None,
    }


# A delivery trip is a day's work. A span longer than this is a data problem —
# a stale timestamp, or a trip left open across days — not a long shift, and
# averaging it in swamps every honest trip beside it.
MAX_TRIP_SPAN_MINUTES = 16 * 60


def _timing_compliance(employee, driver, dn_rows, d_from, d_to):
    """Section 3 — Timing & Compliance.

    avg_trip_start_time uses Delivery Trip.departure_time.
    avg_trip_end_time uses the latest delivery timestamp on the trip.
    avg_time_per_stop_mins spreads the trip's working span across the stops
    that were actually delivered on it.
    cash_discrepancies counts Cash Submission rows in the window with
    discrepancy_flag=1 for this driver (docstatus=1).
    """
    trip_start_secs = []
    trip_end_secs = []
    per_stop_mins = []
    skipped_trips = 0

    # Group DN rows by trip so per-trip end can be computed.
    trips = {}
    for r in dn_rows:
        trips.setdefault(r.trip, {"departure": r.departure_time, "delivered_at": [], "stops": 0})
        trips[r.trip]["stops"] += 1
        if r.delivery_status == "Delivered" and r.delivered_at:
            trips[r.trip]["delivered_at"].append(r.delivered_at)

    for trip_data in trips.values():
        dep = trip_data["departure"]
        if dep:
            start_secs = dep.hour * 3600 + dep.minute * 60 + dep.second
            trip_start_secs.append(start_secs)

        if not trip_data["delivered_at"]:
            continue
        end = max(trip_data["delivered_at"])
        end_secs = end.hour * 3600 + end.minute * 60 + end.second
        trip_end_secs.append(end_secs)
        if not dep:
            continue
        span_mins = (end - dep).total_seconds() / 60
        # A negative span, or one that runs past a plausible shift, means the
        # delivery timestamp is not trustworthy for this trip — most often an
        # order delivered before the field existed, whose fallback is
        # `modified` and therefore moves every time anyone edits it.
        if span_mins <= 0 or span_mins > MAX_TRIP_SPAN_MINUTES:
            skipped_trips += 1
            continue
        # Divide by the stops actually delivered on this trip, not every stop
        # assigned to it. Dividing the working span by stops the driver never
        # reached describes nothing.
        per_stop_mins.append(span_mins / len(trip_data["delivered_at"]))

    avg_start = _secs_to_ampm(sum(trip_start_secs) / len(trip_start_secs)) if trip_start_secs else None
    avg_end = _secs_to_ampm(sum(trip_end_secs) / len(trip_end_secs)) if trip_end_secs else None
    avg_per_stop = round(sum(per_stop_mins) / len(per_stop_mins)) if per_stop_mins else None

    discrepancies = frappe.db.count("Cash Submission", {
        "driver": employee,
        "discrepancy_flag": 1,
        "docstatus": 1,
        "submission_date": ["between", [d_from, d_to]],
    })

    return {
        "avg_trip_start_time":            avg_start,
        "avg_trip_end_time":              avg_end,
        "avg_time_per_stop_mins":         _num(avg_per_stop),
        "avg_time_per_stop_measurable":   avg_per_stop is not None,
        # How many trips carried a usable start and end.
        "timing_sample_trips":            len(per_stop_mins),
        # Trips dropped for an implausible span — a visible count beats a
        # quietly skewed average.
        "timing_skipped_trips":           skipped_trips,
        # Window-scoped, whatever `period` was asked for. The key below keeps
        # its old name for released clients even when the window is a week.
        "cash_discrepancies":             discrepancies,
        "cash_discrepancies_this_month":  discrepancies,
    }


def _vs_fleet(summary, fleet):
    """Section 4 — vs Fleet Average rates.

    A delta only exists when both sides could be measured; subtracting a
    fleet rate that was never measurable from a driver rate that was is a
    comparison of two different things.
    """
    def _pair(key):
        driver_v, fleet_v = summary[key], fleet[key]
        comparable = driver_v is not None and fleet_v is not None
        return {
            "driver":     _num(driver_v),
            "fleet":      _num(fleet_v),
            "delta":      round(driver_v - fleet_v, 1) if comparable else 0,
            "comparable": comparable and fleet.get("driver_count", 0) > 1,
        }

    return {
        "on_time_rate":       _pair("on_time_rate"),
        "success_rate":       _pair("success_rate"),
        "fleet_driver_count": fleet.get("driver_count", 0),
    }


def _daily_cod_history(dn_rows, d_from, d_to):
    """Section 5 — Weekly COD collection buckets across the period."""
    # 4 buckets across the window (roughly weekly for a month view).
    span_days = max((d_to - d_from).days + 1, 1)
    bucket_days = max(span_days // 4, 1)
    buckets = []
    for i in range(4):
        start = add_days(d_from, i * bucket_days)
        end = add_days(d_from, (i + 1) * bucket_days - 1) if i < 3 else d_to
        if start > d_to:
            break
        buckets.append({"start": start, "end": end, "amount": 0.0})

    total = 0.0
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        if (r.get("pay_type") or "") != "COD":
            continue
        if not r.delivered_at:
            continue
        d = getdate(r.delivered_at)
        for b in buckets:
            if b["start"] <= d <= b["end"]:
                # COD cash trend — actual cash, consistent with _summarise.
                collected = collected_cod(r)
                b["amount"] += collected
                total += collected
                break

    return {
        "total_period_cod": total,
        "unit":             "INR",
        "buckets": [
            {"label": f"W{i+1}", "amount": b["amount"],
             "start": str(b["start"]), "end": str(b["end"]),
             # The window rarely divides into four equal weeks — a 30-day
             # month leaves the last bucket 9 days long. Bars drawn without
             # this read as four like-for-like weeks when they are not.
             "days": (b["end"] - b["start"]).days + 1}
            for i, b in enumerate(buckets)
        ],
    }


def _cash_submission_compliance(employee, d_from, d_to):
    """Section 6 — recent Cash Submissions with on-time / late tag.

    On-time means the cash reached the warehouse on the day it was collected.
    The comparison is therefore Cash Submission.submission_date against the
    linked Driver Collection's collection_date — the day the money actually
    came into the driver's hands.

    It used to compare submission_date against the submission's own
    `creation`, which are two timestamps of the same handover:
    `cash_submission.initiate` creates the row and
    `cash_submission.validate_otp_endpoint` stamps submission_date with
    today(). Even a driver who sat on the cash for a week initiated and
    validated on the same later day, so `created_date <= submitted_on` held
    and the section could not return "Late" for anybody.
    """
    rows = frappe.db.sql(
        """SELECT cs.name, cs.submission_date, cs.creation, cs.status,
                  dc.collection_date
             FROM `tabCash Submission` cs
             LEFT JOIN `tabDriver Collection` dc ON dc.name = cs.collection
            WHERE cs.driver = %s
              AND cs.docstatus = 1
              AND cs.submission_date BETWEEN %s AND %s
            ORDER BY cs.submission_date DESC, cs.creation DESC
            LIMIT 15""",
        (employee, d_from, d_to),
        as_dict=True,
    )
    entries = []
    for r in rows:
        submitted_on = getdate(r.submission_date)
        entry = {
            "date":            _short_date(r.submission_date),
            "submission_date": str(submitted_on),
        }
        if not r.collection_date:
            # Submissions raised without a Driver Collection (or created
            # before `collection` was always stamped) have nothing to be
            # judged against. Say so rather than defaulting to a pass.
            entry.update({
                "status":          "Not assessed",
                "detail":          "No collection linked to this submission.",
                "collection_date": None,
                "days_late":       None,
            })
        else:
            collected_on = getdate(r.collection_date)
            late_days = (submitted_on - collected_on).days
            entry["collection_date"] = str(collected_on)
            entry["days_late"] = max(late_days, 0)
            if late_days <= 0:
                entry["status"] = "On time"
            else:
                entry["status"] = "Late"
                entry["detail"] = f"{late_days} day(s) after collection"
        entries.append(entry)
    return {"entries": entries}


def _collection_limit_breaches(employee, d_from, d_to):
    """Section 7 — days the driver had to hand cash over before the day ended.

    A mid-day submission is the observable consequence of hitting the ceiling:
    `pod.submit_proof` refuses a delivery once
    Employee.current_day_collected_amount plus the new cash would pass
    daily_collection_limit, and the only way to carry on is to hand the cash
    in and have `cash_submission.validate_otp_endpoint` reset the counter. So
    a second (or third) submission on one day is a breach that actually
    happened. Nothing stores the ceiling event itself, and the counter is
    overwritten on the next delivery, so this is the record that survives.

    The section used to count submissions whose amount was at or above the
    whole daily limit. That is not what a breach looks like: a driver at a
    fifty-thousand ceiling who hands over eight thousand twice in a day has
    breached it and would still be counted as zero, which is why this panel
    read "no breaches" for every driver on the site.
    """
    limit = flt(frappe.db.get_value("Employee", employee, "daily_collection_limit") or 0)

    rows = frappe.db.sql(
        """SELECT name, submission_date, creation, amount
             FROM `tabCash Submission`
            WHERE driver = %s
              AND docstatus = 1
              AND submission_date BETWEEN %s AND %s
            ORDER BY submission_date ASC, creation ASC""",
        (employee, d_from, d_to),
        as_dict=True,
    )

    by_day = {}
    for r in rows:
        by_day.setdefault(getdate(r.submission_date), []).append(r)

    events = []
    at_or_above_limit = 0
    for day in sorted(by_day, reverse=True):
        day_rows = by_day[day]
        if limit and any(flt(r.amount) >= limit for r in day_rows):
            at_or_above_limit += 1
        # Rows are ordered oldest-first, so the last handover of the day is the
        # ordinary end-of-shift one. Every earlier one happened because the
        # driver had hit the ceiling and could not keep delivering — those are
        # the mid-day events worth showing, at the time they actually occurred.
        for r in day_rows[:-1]:
            created = get_datetime(r.creation)
            events.append({
                "date":       _short_date(r.submission_date),
                "time":       created.strftime("%-I:%M %p"),
                "amount":     flt(r.amount),
                "day_total":  flt(sum(flt(x.amount) for x in day_rows)),
                "submission": r.name,
            })

    return {
        "midday_submissions_count": len(events),
        "daily_limit":              limit,
        "limit_configured":         bool(limit),
        # Days on which one single handover met or exceeded the whole ceiling.
        # Kept separate: it is a different, rarer event from a mid-day break.
        "single_submission_at_limit_days": at_or_above_limit,
        "days_with_submissions":    len(by_day),
        "events":                   events[:20],
        "note": None if limit else "Driver has no daily_collection_limit configured.",
    }


def _discrepancy_history(employee, d_from, d_to):
    """Section 8 — Cash Submissions with a non-zero discrepancy."""
    rows = frappe.db.sql(
        """SELECT name, submission_date, discrepancy_amount,
                  discrepancy_note, status
             FROM `tabCash Submission`
            WHERE driver = %s
              AND docstatus = 1
              AND discrepancy_flag = 1
              AND submission_date BETWEEN %s AND %s
            ORDER BY submission_date DESC
            LIMIT 20""",
        (employee, d_from, d_to),
        as_dict=True,
    )
    entries = []
    for r in rows:
        entries.append({
            "id":       r.name,
            "date":     _short_date(r.submission_date),
            "variance": flt(r.discrepancy_amount),
            "reason":   r.discrepancy_note or "",
            "status":   r.status or "",
        })
    return {"entries": entries}


def _trip_timeline_section(driver, d_from, d_to):
    """Section 9 — planned vs actual per trip in the window."""
    if not driver:
        return {"trips": []}
    trip_rows = frappe.db.sql(
        """SELECT name, departure_time, status
             FROM `tabDelivery Trip`
            WHERE driver = %s
              AND (
                    (DATE(departure_time) BETWEEN %s AND %s)
                 OR (departure_time IS NULL
                     AND DATE(creation) BETWEEN %s AND %s)
              )
            ORDER BY departure_time DESC
            LIMIT 30""",
        (driver, d_from, d_to, d_from, d_to),
        as_dict=True,
    )
    trips = []
    for t in trip_rows:
        stop_rows = frappe.db.sql(
            f"""SELECT ds.estimated_arrival,
                      {delivered_at_sql()}     AS delivered_at,
                      dn.cowberry_delivery_status AS delivery_status
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                WHERE ds.parent = %s AND dn.docstatus = 1
                ORDER BY ds.idx ASC""",
            (t.name,), as_dict=True,
        )
        if not stop_rows:
            continue

        delivered = [r for r in stop_rows if r.delivery_status == "Delivered" and r.delivered_at]
        on_time = sum(1 for r in delivered
                      if r.estimated_arrival and r.delivered_at <= r.estimated_arrival)

        planned_start = t.departure_time
        actual_start = min((r.delivered_at for r in delivered), default=None) if delivered else None
        planned_end = max((r.estimated_arrival for r in stop_rows if r.estimated_arrival), default=None)
        actual_end = max((r.delivered_at for r in delivered), default=None)

        # Both spans are measured from the same point — the trip's departure.
        # Planned ran departure → last ETA while actual ran first delivery →
        # last delivery, so the two durations shown side by side were counted
        # from different starts and the comparison meant nothing.
        planned_span = (planned_end - planned_start).total_seconds() // 60 if planned_start and planned_end else None
        actual_span = (actual_end - planned_start).total_seconds() // 60 if planned_start and actual_end else None
        if actual_span is not None and not (0 < actual_span <= MAX_TRIP_SPAN_MINUTES):
            # Same guard as _timing_compliance: a span past a plausible shift
            # is a stale timestamp, not a long day.
            actual_span = None

        badge = ""
        if planned_end and actual_end:
            var = int((actual_end - planned_end).total_seconds() // 60)
            if var > 0:
                badge = f"+{var} min late"
            elif var < 0:
                badge = f"{abs(var)} min early"
            else:
                badge = "on time"

        trips.append({
            "name":             t.name,
            "date":             _short_date(t.departure_time) if t.departure_time else "",
            "planned_start":    _time_ampm(planned_start),
            "actual_start":     _time_ampm(actual_start),
            "planned_duration": _mins_to_span(planned_span),
            "actual_duration":  _mins_to_span(actual_span),
            "on_time_stops":    f"{on_time} / {len(stop_rows)}",
            "badge":            badge,
        })
    return {"trips": trips}


def _delay_heatmap(dn_rows):
    """Section 10 — day-of-week × time-slot delay intensity.

    Cell = "rare" | "some" | "often" | "usually", scaled off the count
    of late deliveries in that cell. AM=<11am, Mid=11-13, PM=13-17,
    Eve=>=17.
    """
    def slot(hour):
        if hour < 11:  return 0
        if hour < 13:  return 1
        if hour < 17:  return 2
        return 3

    # Mon=0, ..., Sat=5, Sun=6
    grid = [[0] * 7 for _ in range(4)]
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        if not (r.expected_arrival and r.delivered_at):
            continue
        delta_min = (r.delivered_at - r.expected_arrival).total_seconds() / 60
        if delta_min <= 0:
            continue  # on time / early doesn't fill the delay heatmap
        d = r.delivered_at
        grid[slot(d.hour)][d.weekday()] += 1

    def label(n):
        if n == 0:  return "rare"
        if n <= 2:  return "some"
        if n <= 5:  return "often"
        return "usually"

    return {
        "x_labels": ["M", "T", "W", "T", "F", "S", "S"],
        "y_labels": ["AM", "Mid", "PM", "Eve"],
        "cells": [[label(n) for n in row] for row in grid],
    }


def _worst_vs_best_trips(driver, d_from, d_to):
    """Section 11 — Worst/Best trips ranked by on-time score."""
    if not driver:
        return {"best": [], "worst": []}
    trip_names = [r["name"] for r in frappe.db.sql(
        """SELECT name FROM `tabDelivery Trip`
            WHERE driver = %s
              AND (
                    (DATE(departure_time) BETWEEN %s AND %s)
                 OR (departure_time IS NULL
                     AND DATE(creation) BETWEEN %s AND %s)
              )""",
        (driver, d_from, d_to, d_from, d_to), as_dict=True,
    )]
    scored = []
    for name in trip_names:
        rows = frappe.db.sql(
            f"""SELECT ds.estimated_arrival,
                      {delivered_at_sql()} AS delivered_at,
                      dn.cowberry_delivery_status AS status,
                      dt.departure_time
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                 JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                WHERE ds.parent = %s AND dn.docstatus = 1""",
            (name,), as_dict=True,
        )
        # Only stops that carry both an ETA and a delivery time can be ranked
        # on punctuality. Scoring against every stop meant a trip with no ETAs
        # scored 0, so "worst trips" listed whichever trips ERPNext had not
        # routed rather than whichever trips ran badly.
        judged = [r for r in rows
                  if r.status == "Delivered" and r.delivered_at and r.estimated_arrival]
        if not judged:
            continue
        on_time = sum(1 for r in judged if r.delivered_at <= r.estimated_arrival)
        score = round((on_time / len(judged)) * 100)
        trip_date = rows[0].departure_time
        scored.append({
            "name":         name,
            "date":         _short_date(trip_date) if trip_date else "",
            "score_pct":    score,
            "judged_stops": len(judged),
            "total_stops":  len(rows),
        })

    scored_by_score = sorted(scored, key=lambda x: x["score_pct"], reverse=True)
    return {
        "best":  scored_by_score[:3],
        "worst": sorted(scored, key=lambda x: x["score_pct"])[:3],
        # Zero here means no trip in the window had a routed ETA, not that the
        # driver ran no trips.
        "ranked_trips": len(scored),
    }


def _per_stop_variance(dn_rows):
    """Section 12 — Per-stop variance list (Delivered stops only)."""
    stops = []
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        if not (r.expected_arrival and r.delivered_at):
            continue
        variance_mins = int((r.delivered_at - r.expected_arrival).total_seconds() // 60)
        if variance_mins > 0:
            label = f"+{variance_mins} min late"
        elif variance_mins < 0:
            label = f"{abs(variance_mins)} min early"
        else:
            label = "On time"
        stops.append({
            "customer":      r.customer_name or "",
            "variance_mins": variance_mins,
            "label":         label,
        })
    # cap for the flutter list
    return {"stops": stops[:20]}


def _eta_accuracy_trend(dn_rows):
    """Section 13 — daily average absolute variance across the period."""
    by_day = {}
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        if not (r.expected_arrival and r.delivered_at):
            continue
        d = getdate(r.delivered_at)
        variance = abs((r.delivered_at - r.expected_arrival).total_seconds() / 60)
        by_day.setdefault(d, []).append(variance)

    points = []
    for d in sorted(by_day.keys()):
        avg = round(sum(by_day[d]) / len(by_day[d]))
        points.append({"date": _short_date(d), "variance_mins": avg})

    trend_note = ""
    if len(points) >= 2:
        first_half = points[:len(points) // 2]
        second_half = points[len(points) // 2:]
        first_avg = sum(p["variance_mins"] for p in first_half) / len(first_half)
        second_avg = sum(p["variance_mins"] for p in second_half) / len(second_half)
        if second_avg < first_avg - 2:
            trend_note = "Flattening towards zero - Improving accuracy"
        elif second_avg > first_avg + 2:
            trend_note = "Variance widening - Slipping"
        else:
            trend_note = "Steady variance"
    return {
        "unit":       "min",
        "trend_note": trend_note,
        "points":     points,
    }


# ---------------------------------------------------------------------------
# Small formatting helpers.
# ---------------------------------------------------------------------------

def _period_window(period, from_date, to_date):
    t = getdate(today())
    if period == "today":
        return t, t
    if period == "week":
        return add_days(t, -6), t
    if period == "month":
        return add_days(t, -29), t
    if not from_date or not to_date:
        return None, None
    return getdate(from_date), getdate(to_date)


def _secs_to_ampm(secs):
    hours = int(secs) // 3600
    mins = (int(secs) % 3600) // 60
    period = "AM" if hours < 12 else "PM"
    disp_hour = hours % 12 or 12
    return f"{disp_hour}:{mins:02d} {period}"


def _time_ampm(dt):
    if not dt:
        return None
    return get_datetime(dt).strftime("%-I:%M %p")


def _short_date(d):
    if not d:
        return ""
    try:
        return get_datetime(d).strftime("%d %b")
    except Exception:
        return str(d)


def _mins_to_span(mins):
    if not mins:
        return "0h 00m"
    mins = int(mins)
    h, m = divmod(mins, 60)
    return f"{h}h {m:02d}m"


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Analytics §§1-2) — unchanged
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def driver_dashboard(period="today", **kwargs):
    """§1 KPI bundle for the driver's home dashboard.

    Spec keys: kpis{...}, trend, fleet_avg_score, score_breakdown.
    Period: today | week | month | custom (custom requires from + to).
    """
    try:
        employee = _require_driver()
        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        form = frappe.local.form_dict
        d_from, d_to = _period_window(
            period,
            form.get("from") or kwargs.get("from_date"),
            form.get("to") or kwargs.get("to_date"),
        )
        if d_from is None:
            return err("VALIDATION_ERROR",
                       "period=custom requires both `from` and `to`.", 400)

        rows = _driver_dn_rows(driver, d_from, d_to)
        s = _summarise(rows)
        topup_count, topup_amount = _wallet_topups(employee, d_from, d_to)
        fleet = _fleet_summary(d_from, d_to)

        # Trend vs the prior window of the same size
        prev_from = add_days(d_from, -(d_to - d_from).days - 1)
        prev_to = add_days(d_from, -1)
        prev_s = _summarise(_driver_dn_rows(driver, prev_from, prev_to))
        if s["delivered"] > prev_s["delivered"]:
            trend = "up"
        elif s["delivered"] < prev_s["delivered"]:
            trend = "down"
        else:
            trend = "flat"

        return ok(data={
            "kpis": {
                "total_delivered":       s["delivered"],
                "delivery_success_rate": _num(s["success_rate"]),
                "on_time_rate":          _num(s["on_time_rate"]),
                "avg_delay_mins":        _num(s["avg_delay"]),
                "total_cod_collected":   s["cod_collected"],
                "wallet_topups":         topup_count,
                "wallet_topup_value":    topup_amount,
                "reschedule_rate":       _num(s["reschedule_rate"]),
                "composite_score":       s["composite"],
            },
            # Which KPIs above rest on real data — a false flag means the 0
            # beside it is "not measurable", not a measured zero.
            "measurable": {
                "delivery_success_rate": s["success_rate"] is not None,
                "on_time_rate":          s["on_time_rate"] is not None,
                "avg_delay_mins":        s["avg_delay"] is not None,
                "reschedule_rate":       s["reschedule_rate"] is not None,
            },
            "trend":              trend,
            "fleet_avg_score":    fleet["composite"],
            "fleet_driver_count": fleet.get("driver_count", 0),
            "score_coverage_pct": s["coverage"],
            "score_breakdown":    SCORE_WEIGHTS,
        })
    except Exception as e:
        return err("DRIVER_DASHBOARD_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def trip_timeline(trip=None):
    """§2 Per-stop timing for a trip's vertical timeline.

    Spec keys per stop: delivery_note, stop_sequence, customer,
    expected_arrival_time, actual_arrival_time, travel_variance_mins,
    on_time, delivery_status.
    """
    try:
        employee = _require_driver()
        if not trip:
            return err("VALIDATION_ERROR", "Query param `trip` is required.", 400)
        if not frappe.db.exists("Delivery Trip", trip):
            return err("NOT_FOUND", f"Delivery Trip '{trip}' not found.", 404)
        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        trip_driver = frappe.db.get_value("Delivery Trip", trip, "driver")
        if driver and trip_driver and trip_driver != driver:
            return err("FORBIDDEN", "This trip is not assigned to you.", 403)

        rows = frappe.db.sql(
            f"""SELECT ds.delivery_note,
                      ds.idx                   AS stop_sequence,
                      dn.customer_name         AS customer,
                      ds.estimated_arrival     AS expected_arrival,
                      {delivered_at_sql()}     AS actual_arrival,
                      dn.cowberry_delivery_status AS delivery_status
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                WHERE ds.parent = %s
                ORDER BY ds.idx ASC""",
            (trip,), as_dict=True,
        )
        stops = []
        for r in rows:
            variance = None
            on_time = None
            if r.expected_arrival and r.actual_arrival and r.delivery_status == "Delivered":
                variance = int((r.actual_arrival - r.expected_arrival).total_seconds() // 60)
                on_time = variance <= 0
            stops.append({
                "delivery_note":          r.delivery_note,
                "stop_sequence":          r.stop_sequence,
                "customer":               r.customer,
                "expected_arrival_time":  str(r.expected_arrival) if r.expected_arrival else None,
                "actual_arrival_time":    str(r.actual_arrival) if r.actual_arrival and r.delivery_status == "Delivered" else None,
                "travel_variance_mins":   variance,
                "on_time":                on_time,
                "delivery_status":        r.delivery_status or "Pending",
            })
        return ok(data={"stops": stops})
    except Exception as e:
        return err("TRIP_TIMELINE_FAILED", str(e))


# ---------------------------------------------------------------------------
# Legacy endpoint kept for back-compat
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_my_analytics(from_date=None, to_date=None):
    try:
        employee = _require_driver()
        # Delivery Trip.driver links to the Driver doctype, not Employee.
        # Filtering by the employee id matched nothing, so this endpoint
        # returned all-zero stats regardless of how much the driver had done.
        from erpera_driver_app.api.trip import _driver_record
        driver = _driver_record(employee)
        if not driver:
            return ok(data={
                "total_deliveries": 0, "delivered": 0, "failed": 0,
                "rescheduled": 0, "total_value": 0, "total_cash_submitted": 0,
            })

        date_filter = ""
        params = [driver]
        if from_date:
            date_filter += " AND posting_date >= %s"
            params.append(from_date)
        if to_date:
            date_filter += " AND posting_date <= %s"
            params.append(to_date)

        delivery_stats = frappe.db.sql(
            f"""
            SELECT
                COUNT(*) as total_deliveries,
                SUM(CASE WHEN cowberry_delivery_status='Delivered' THEN 1 ELSE 0 END) as delivered,
                SUM(CASE WHEN cowberry_delivery_status='Failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN cowberry_delivery_status='Rescheduled' THEN 1 ELSE 0 END) as rescheduled,
                COALESCE(SUM(grand_total), 0) as total_value
            FROM `tabDelivery Note`
            WHERE docstatus=1
            AND EXISTS (
                SELECT 1 FROM `tabDelivery Stop` ds
                JOIN `tabDelivery Trip` dt ON ds.parent=dt.name
                WHERE ds.delivery_note=`tabDelivery Note`.name AND dt.driver=%s
            )
            {date_filter}
            """,
            params,
            as_dict=True,
        )

        cash_stats = frappe.db.sql(
            """
            SELECT COALESCE(SUM(amount), 0) as total_cash_submitted
            FROM `tabCash Submission`
            WHERE driver=%s AND docstatus=1
            """,
            [employee],
            as_dict=True,
        )

        stats = delivery_stats[0] if delivery_stats else {}
        stats.update(cash_stats[0] if cash_stats else {})

        return ok(data=stats)
    except Exception as e:
        return err("GET_ANALYTICS_FAILED", str(e))
