import frappe
from frappe.utils import add_days, add_to_date, flt, get_datetime, getdate, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils import analytics_settings as settings
from erpera_driver_app.utils.cod import collected_cod, expected_cod
from erpera_driver_app.utils.response import err, ok


# OLD (pre-Analytics Settings) — kept for reference:
# SCORE_WEIGHTS = {
#     "on_time_weight": 0.4,
#     "success_weight": 0.3,
#     "cod_weight":     0.2,
#     "comms_weight":   0.1,
# }
#
# Composite-score weights per FRD §11 now live on the `Analytics Settings`
# Single, so ops can retune the gauge from the desk without a deploy. The
# spec's score_breakdown still surfaces them, via `settings.weight_labels()`,
# so the caption under each tile is derived from the same numbers the score
# uses and cannot drift from them. The pre-settings split (40/30/20/10) is the
# default, so a bench that has not migrated yet scores exactly as before.


# ---------------------------------------------------------------------------
# Shared driver-scoped query helpers — used by every section builder.
# ---------------------------------------------------------------------------

# Delivery Note columns that hold a REAL delivery moment, best first. None of
# them is guaranteed to exist: `custom_delivered_timestamp` is this app's own
# (written by utils.status_timestamps), the other three belong to cowberry_app
# and the Delhivery integration. Whichever the bench has is used; where none is
# populated the code falls back to `modified`, which is a last-edited stamp and
# drifts — see `_resolve_row_times`.
_REAL_DELIVERED_FIELDS = (
    "custom_delivered_timestamp",   # erpera_driver_app, written at the POD
    "actual_arrival_time",          # cowberry_app
    "pod_timestamp",                # cowberry_app
    "custom_delivered_on",          # cowberry_app
)


def _delivered_at_expr():
    """SQL for the best real delivery timestamp this bench can offer.

    Meta-guarded the same way `trip._raw_payment_method` is: naming a column
    that does not exist on this site turns the whole dashboard into a 500,
    and these four columns exist on different benches.
    """
    from erpera_driver_app.api.trip import _dn_field_names

    available = _dn_field_names()
    cols = [f"dn.`{f}`" for f in _REAL_DELIVERED_FIELDS if f in available]
    return f"COALESCE({', '.join(cols)})" if cols else "NULL"


# Delivery Note columns that record the driver actually setting off. The app
# stamps these from its own status transitions (utils.status_timestamps), so
# where the driver worked through the app they are the real start of the round.
_REAL_START_FIELDS = (
    "custom_out_for_delivery_timestamp",
    "custom_picked_up_timestamp",
    "custom_out_for_pickup_timestamp",
)


def _started_at_expr():
    """SQL for the earliest real "driver set off" stamp this bench can offer."""
    from erpera_driver_app.api.trip import _dn_field_names

    available = _dn_field_names()
    cols = [f"dn.`{f}`" for f in _REAL_START_FIELDS if f in available]
    return f"COALESCE({', '.join(cols)})" if cols else "NULL"


def _resolve_row_times(rows):
    """Give every row a planned arrival and a delivery time, and say where
    each came from.

    Two fields decide almost every timing number on this screen, and on most
    benches neither is populated:

    * **Planned arrival** — `Delivery Stop.estimated_arrival` is only ever
      written by ERPNext's Calculate Arrival Time / Optimise Route buttons,
      which need a Google Directions key. Unpressed, it stays NULL forever and
      every on-time figure collapses to zero — which reads on screen as "never
      late" rather than "never measured". When `eta_fallback_enabled` is on we
      derive one from the trip's departure plus N minutes per stop, the same
      estimate `trip._resolve_expected_arrival` already shows on the order
      card, so the dashboard stops contradicting the rest of the app.
    * **Delivery time** — the code used `modified`, Frappe's last-edited
      stamp. It moves whenever anyone touches the order, which is how a stop
      came to average 41 hours. Any real timestamp the bench has is preferred.

    `eta_source` and `delivered_source` ride along on each row so a section can
    tell the client whether its numbers were measured or estimated.
    """
    use_fallback = settings.enabled("eta_fallback_enabled")
    per_stop = settings.count("default_minutes_per_stop", minimum=1)
    outlier_max = settings.count("timing_outlier_max_mins")
    for r in rows:
        if r.get("delivered_real"):
            r.delivered_at = r.delivered_real
            r.delivered_source = "recorded"
        else:
            r.delivered_source = "modified"

        if r.get("expected_arrival"):
            r.eta_source = "planned"
        elif use_fallback and r.get("departure_time") and r.get("stop_idx"):
            r.expected_arrival = add_to_date(
                r.departure_time, minutes=int(r.stop_idx) * per_stop)
            r.eta_source = "derived"
        else:
            r.eta_source = "none"

        # Whether this row's two timestamps may be compared at all. A stop
        # cannot really be a day late against its own trip; a gap that wide
        # means `modified` was a later edit, not the delivery. Comparing it
        # anyway is how a stop came to average 41 hours, so those rows are
        # dropped from every timing figure and counted in `_quality` instead.
        gap = None
        if r.get("expected_arrival") and r.get("delivered_at"):
            gap = abs((r.delivered_at - r.expected_arrival).total_seconds() / 60)
        r.timing_gap_mins = gap
        r.timing_usable = bool(
            gap is not None and (not outlier_max or gap <= outlier_max))
    return rows


def _quality(rows):
    """How far the timing numbers over `rows` can be trusted.

    Returned beside any section built on arrival times so the app can caption
    or grey it out instead of rendering an estimate as a measurement.
    """
    delivered = [r for r in rows if (r.delivery_status or "").strip() == "Delivered"]
    return {
        "eta_planned":       sum(1 for r in delivered if r.get("eta_source") == "planned"),
        "eta_derived":       sum(1 for r in delivered if r.get("eta_source") == "derived"),
        "eta_missing":       sum(1 for r in delivered if r.get("eta_source") == "none"),
        "time_recorded":     sum(1 for r in delivered if r.get("delivered_source") == "recorded"),
        "time_from_modified": sum(1 for r in delivered if r.get("delivered_source") == "modified"),
        # Delivered stops whose two timestamps were too far apart to compare.
        "timing_outliers":   sum(1 for r in delivered
                                 if r.get("timing_gap_mins") is not None
                                 and not r.get("timing_usable")),
        "timing_usable":     sum(1 for r in delivered if r.get("timing_usable")),
    }


def _driver_dn_rows(driver, d_from, d_to):
    """Every Delivery Note reachable through the driver's trips in the
    window. Returns per-row: delivery_note, delivery_status,
    payment_method, grand_total, delivered_at, expected_arrival,
    trip, departure_time, stop_idx, customer_name.

    Filter uses trip departure/creation (same rule as delivery.history)
    so ranking + windowing stay consistent across screens."""
    if not driver:
        return []
    # OLD: this query selected `dn.modified AS delivered_at` and nothing else
    # for the delivery moment, and had no rounding_adjustment column.
    return _resolve_row_times(frappe.db.sql(
        f"""
        SELECT dn.name                        AS delivery_note,
               dn.cowberry_delivery_status    AS delivery_status,
               dn.cowberry_payment_method     AS payment_method,
               dn.grand_total                 AS grand_total,
               dn.rounded_total               AS rounded_total,
               dn.rounding_adjustment         AS rounding_adjustment,
               dn.cod_amount                  AS cod_amount,
               dn.cod_collected_amount        AS cod_collected_amount,
               dn.modified                    AS delivered_at,
               {_delivered_at_expr()}         AS delivered_real,
               dn.customer_name               AS customer_name,
               ds.estimated_arrival           AS expected_arrival,
               ds.idx                         AS stop_idx,
               dt.name                        AS trip,
               dt.departure_time              AS departure_time
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE dt.driver = %(driver)s
           AND (
                 (DATE(dt.departure_time) BETWEEN %(d_from)s AND %(d_to)s)
              OR (dt.departure_time IS NULL
                  AND DATE(dt.creation) BETWEEN %(d_from)s AND %(d_to)s)
           )
         ORDER BY dt.departure_time ASC, ds.idx ASC
        """,
        {"driver": driver, "d_from": d_from, "d_to": d_to},
        as_dict=True,
    ))


def _summarise(rows):
    """Common counters used by performance_score + key_metrics + vs_fleet.
    Kept as a single pass so we don't re-scan the row set per section."""
    total = len(rows)
    delivered = failed = rescheduled = on_time = 0
    cod_collected = 0.0
    cod_expected = 0.0
    delays = []  # positive minute deltas for delivered rows only
    for r in rows:
        status = (r.delivery_status or "").strip()
        if status == "Delivered":
            delivered += 1
            if (r.payment_method or "").upper().startswith("COD"):
                # Cash actually taken, not order value — see utils.cod.
                cod_collected += collected_cod(r)
                cod_expected += expected_cod(r)
            # OLD: if r.expected_arrival and r.delivered_at:
            # Now also requires the pair to be comparable — see
            # `_resolve_row_times`. Rows resolved before this helper existed
            # have no flag, so fall back to the old condition for them.
            if r.get("timing_usable") if "timing_usable" in r else (
                    r.expected_arrival and r.delivered_at):
                delta_mins = (r.delivered_at - r.expected_arrival).total_seconds() / 60
                if delta_mins <= 0:
                    on_time += 1
                else:
                    delays.append(delta_mins)
        elif status in ("Failed", "Returned"):
            failed += 1
        elif status == "Rescheduled":
            rescheduled += 1

    success_rate = round((delivered / total) * 100, 1) if total else 0.0
    on_time_rate = round((on_time / delivered) * 100, 1) if delivered else 0.0
    reschedule_rate = round((rescheduled / total) * 100, 1) if total else 0.0
    avg_delay = round(sum(delays) / len(delays), 1) if delays else 0.0
    cod_score = _cod_score(cod_collected, cod_expected)
    weights = settings.score_weights()
    # OLD: composite = round(
    #     on_time_rate * SCORE_WEIGHTS["on_time_weight"]
    #     + success_rate * SCORE_WEIGHTS["success_weight"]
    #     + (100 if cod_collected > 0 else 0) * SCORE_WEIGHTS["cod_weight"]
    #     + success_rate * SCORE_WEIGHTS["comms_weight"]
    # )
    composite = round(
        on_time_rate * weights["on_time_weight"]
        + success_rate * weights["success_weight"]
        + cod_score * weights["cod_weight"]
        + success_rate * weights["comms_weight"]
    )
    return {
        "total":           total,
        "delivered":       delivered,
        "failed":          failed,
        "rescheduled":     rescheduled,
        "on_time":         on_time,
        "cod_collected":   cod_collected,
        "cod_expected":    cod_expected,
        "cod_score":       cod_score,
        "delays":          delays,
        "success_rate":    success_rate,
        "on_time_rate":    on_time_rate,
        "reschedule_rate": reschedule_rate,
        "avg_delay":       avg_delay,
        "composite":       composite,
    }


def _cod_score(collected, expected):
    """The COD component's 0-100 score, per `Analytics Settings`.

    "Binary" is the original rule: full marks for collecting any cash at all
    and zero for none, so ₹1 and ₹1,00,000 score identically. "Collection Rate"
    scores cash collected against cash expected, which is what the percentage
    sign on the tile already implies to whoever reads it. Binary stays the
    default so switching is a deliberate act, not a surprise after an upgrade.
    """
    if (settings.get("cod_score_mode") or "Binary") == "Collection Rate":
        if expected <= 0:
            return 0.0
        return round(min(collected / expected, 1.0) * 100, 1)
    return 100.0 if collected > 0 else 0.0


def _fleet_summary(d_from, d_to):
    """Fleet-wide summary across ALL drivers in the window."""
    # OLD: selected only `dn.modified AS delivered_at` and the raw
    # `ds.estimated_arrival`, with no stop index or departure time, so the
    # fleet baseline could not resolve times the way the driver's rows do.
    rows = _resolve_row_times(frappe.db.sql(
        f"""
        SELECT dn.cowberry_delivery_status    AS delivery_status,
               dn.cowberry_payment_method     AS payment_method,
               dn.grand_total                 AS grand_total,
               dn.rounded_total               AS rounded_total,
               dn.rounding_adjustment         AS rounding_adjustment,
               dn.cod_amount                  AS cod_amount,
               dn.cod_collected_amount        AS cod_collected_amount,
               dn.modified                    AS delivered_at,
               {_delivered_at_expr()}         AS delivered_real,
               ds.estimated_arrival           AS expected_arrival,
               ds.idx                         AS stop_idx,
               dt.departure_time              AS departure_time
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE (
                 (DATE(dt.departure_time) BETWEEN %(d_from)s AND %(d_to)s)
              OR (dt.departure_time IS NULL
                  AND DATE(dt.creation) BETWEEN %(d_from)s AND %(d_to)s)
           )
        """,
        {"d_from": d_from, "d_to": d_to},
        as_dict=True,
    ))
    return _summarise(rows)


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
    """Section 1 — Performance Score gauge + components."""
    # OLD: the two thresholds below were the literals 0.5 and 1.
    period_threshold = settings.number("period_delta_threshold")
    fleet_threshold = settings.number("fleet_delta_threshold")

    delta = summary["composite"] - prev["composite"]
    if delta > period_threshold:
        delta_label = f"Up {round(delta, 1)} vs last period"
    elif delta < -period_threshold:
        delta_label = f"Down {abs(round(delta, 1))} vs last period"
    else:
        delta_label = "Flat vs last period"

    fleet_delta = summary["composite"] - fleet["composite"]
    if fleet_delta > fleet_threshold:
        fleet_label = "You are above average"
    elif fleet_delta < -fleet_threshold:
        fleet_label = "You are below average"
    else:
        fleet_label = "You are at fleet average"

    # comms_weight has no source signal yet — fall back to success_rate so
    # the composite isn't artificially depressed pre-chat-rollout.
    # OLD component list — captions were string literals that could drift
    # from the weights actually used:
    #     {"name": "On-Time", "weight_pct": summary["on_time_rate"], "weight_label": "(40%)"},
    #     {"name": "Success", "weight_pct": summary["success_rate"], "weight_label": "(30%)"},
    #     {"name": "COD",     "weight_pct": 100 if summary["cod_collected"] > 0 else 0,
    #      "weight_label": "(20%)"},
    #     {"name": "Comms",   "weight_pct": summary["success_rate"], "weight_label": "(10%)"},
    labels = settings.weight_labels()
    return {
        "score":                summary["composite"],
        "vs_last_month_delta":  delta_label,
        "fleet_avg_score":      fleet["composite"],
        "fleet_avg_comparison": fleet_label,
        "components": [
            {"name": "On-Time", "weight_pct": summary["on_time_rate"],
             "weight_label": labels["on_time_weight"]},
            {"name": "Success", "weight_pct": summary["success_rate"],
             "weight_label": labels["success_weight"]},
            {"name": "COD",     "weight_pct": summary["cod_score"],
             "weight_label": labels["cod_weight"]},
            {"name": "Comms",   "weight_pct": summary["success_rate"],
             "weight_label": labels["comms_weight"]},
        ],
    }


def _key_metrics(employee, summary, d_from, d_to):
    """Section 2 — Key Metrics tiles."""
    topup_count, topup_amount = _wallet_topups(employee, d_from, d_to)
    return {
        "deliveries_completed": summary["delivered"],
        "success_rate_pct":     summary["success_rate"],
        "on_time_rate_pct":     summary["on_time_rate"],
        "avg_delay_mins":       summary["avg_delay"],
        "cod_collected":        summary["cod_collected"],
        "wallet_topups": {
            "count":  topup_count,
            "amount": topup_amount,
        },
        "reschedule_rate_pct":  summary["reschedule_rate"],
    }


def _timing_compliance(employee, driver, dn_rows, d_from, d_to):
    """Section 3 — Timing & Compliance.

    avg_trip_start_time uses Delivery Trip.departure_time.
    avg_trip_end_time uses the max DN.modified of Delivered stops per trip.
    avg_time_per_stop_mins = (end - start) / stops for the trip, averaged.
    cash_discrepancies_this_month = COUNT of Cash Submission rows in the
    window with discrepancy_flag=1 for this driver (docstatus=1).
    """
    trip_start_secs = []
    trip_end_secs = []
    per_stop_mins = []

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

        if trip_data["delivered_at"]:
            end = max(trip_data["delivered_at"])
            end_secs = end.hour * 3600 + end.minute * 60 + end.second
            trip_end_secs.append(end_secs)
            if dep and trip_data["stops"]:
                span_mins = max((end - dep).total_seconds() / 60, 0)
                per_stop_mins.append(span_mins / trip_data["stops"])

    avg_start = _secs_to_ampm(sum(trip_start_secs) / len(trip_start_secs)) if trip_start_secs else None
    avg_end = _secs_to_ampm(sum(trip_end_secs) / len(trip_end_secs)) if trip_end_secs else None
    avg_per_stop = round(sum(per_stop_mins) / len(per_stop_mins)) if per_stop_mins else 0

    discrepancies = frappe.db.count("Cash Submission", {
        "driver": employee,
        "discrepancy_flag": 1,
        "docstatus": 1,
        "submission_date": ["between", [d_from, d_to]],
    })

    return {
        "avg_trip_start_time":            avg_start,
        "avg_trip_end_time":              avg_end,
        "avg_time_per_stop_mins":         avg_per_stop,
        "cash_discrepancies_this_month":  discrepancies,
    }


def _vs_fleet(summary, fleet):
    """Section 4 — vs Fleet Average rates."""
    on_time_delta = round(summary["on_time_rate"] - fleet["on_time_rate"], 1)
    success_delta = round(summary["success_rate"] - fleet["success_rate"], 1)
    return {
        "on_time_rate": {
            "driver": summary["on_time_rate"],
            "fleet":  fleet["on_time_rate"],
            "delta":  on_time_delta,
        },
        "success_rate": {
            "driver": summary["success_rate"],
            "fleet":  fleet["success_rate"],
            "delta":  success_delta,
        },
    }


def _daily_cod_history(dn_rows, d_from, d_to):
    """Section 5 — Weekly COD collection buckets across the period."""
    # 4 buckets across the window (roughly weekly for a month view).
    # OLD: bucket_days = max(span_days // 4, 1)
    #      for i in range(4):
    #          end = add_days(d_from, (i + 1) * bucket_days - 1) if i < 3 else d_to
    bucket_count = settings.count("cod_history_buckets", minimum=1)
    span_days = max((d_to - d_from).days + 1, 1)
    bucket_days = max(span_days // bucket_count, 1)
    buckets = []
    for i in range(bucket_count):
        start = add_days(d_from, i * bucket_days)
        end = (add_days(d_from, (i + 1) * bucket_days - 1)
               if i < bucket_count - 1 else d_to)
        if start > d_to:
            break
        buckets.append({"start": start, "end": end, "amount": 0.0})

    total = 0.0
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        if not (r.payment_method or "").upper().startswith("COD"):
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
        "unit":             settings.get("currency_label"),  # OLD: "INR"
        "buckets": [
            {"label": f"W{i+1}", "amount": b["amount"],
             "start": str(b["start"]), "end": str(b["end"])}
            for i, b in enumerate(buckets)
        ],
    }


def _cash_submission_compliance(employee, d_from, d_to):
    """Section 6 — recent Cash Submissions with on-time / late tag.

    On-time = submission created on the same day as the submission_date
    (driver didn't carry the day's cash overnight). Late = created after
    submission_date.
    """
    rows = frappe.db.sql(
        """SELECT name, submission_date, creation, status
             FROM `tabCash Submission`
            WHERE driver = %s
              AND docstatus = 1
              AND submission_date BETWEEN %s AND %s
            ORDER BY submission_date DESC, creation DESC
            LIMIT %s""",
        # OLD: LIMIT 15
        (employee, d_from, d_to,
         settings.count("cash_compliance_limit", minimum=1)),
        as_dict=True,
    )
    entries = []
    for r in rows:
        created = get_datetime(r.creation)
        submitted_on = getdate(r.submission_date)
        created_date = getdate(created)
        if created_date <= submitted_on:
            entries.append({
                "date":   _short_date(r.submission_date),
                "status": "On time",
            })
        else:
            late_days = (created_date - submitted_on).days
            entries.append({
                "date":   _short_date(r.submission_date),
                "status": "Late",
                "detail": f"{late_days} day(s) late",
            })
    return {"entries": entries}


def _collection_limit_breaches(employee, d_from, d_to):
    """Section 7 — mid-day submissions vs the driver's
    daily_collection_limit. A "breach" is a Cash Submission whose amount
    is at or above the driver's daily limit (early submission because
    the driver hit their ceiling before the end of the day).
    """
    limit = frappe.db.get_value("Employee", employee, "daily_collection_limit") or 0
    if not limit:
        return {"midday_submissions_count": 0, "events": [],
                "note": "Driver has no daily_collection_limit configured."}

    rows = frappe.db.sql(
        """SELECT name, submission_date, creation, amount
             FROM `tabCash Submission`
            WHERE driver = %s
              AND docstatus = 1
              AND submission_date BETWEEN %s AND %s
              AND amount >= %s
            ORDER BY submission_date DESC, creation DESC
            LIMIT %s""",
        # OLD: LIMIT 20
        (employee, d_from, d_to, limit,
         settings.count("limit_breach_limit", minimum=1)),
        as_dict=True,
    )
    events = []
    for r in rows:
        created = get_datetime(r.creation)
        events.append({
            "date":   _short_date(r.submission_date),
            "time":   created.strftime("%-I:%M %p"),
            "amount": flt(r.amount),
        })
    return {
        "midday_submissions_count": len(events),
        "daily_limit":              flt(limit),
        "events":                   events,
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
            LIMIT %s""",
        # OLD: LIMIT 20
        (employee, d_from, d_to,
         settings.count("discrepancy_limit", minimum=1)),
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
            LIMIT %s""",
        # OLD: LIMIT 30
        (driver, d_from, d_to, d_from, d_to,
         settings.count("trip_timeline_limit", minimum=1)),
        as_dict=True,
    )
    trips = []
    for t in trip_rows:
        # OLD: selected only ds.estimated_arrival, dn.modified AS delivered_at
        # and the status, with no stop index and no real timestamps.
        stop_rows = frappe.db.sql(
            f"""SELECT ds.estimated_arrival,
                      ds.idx                   AS stop_idx,
                      dn.modified              AS delivered_at,
                      {_delivered_at_expr()}   AS delivered_real,
                      {_started_at_expr()}     AS started_real,
                      dn.cowberry_delivery_status AS delivery_status
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                WHERE ds.parent = %s
                ORDER BY ds.idx ASC""",
            (t.name,), as_dict=True,
        )
        if not stop_rows:
            continue
        for r in stop_rows:
            r.departure_time = t.departure_time
        stop_rows = _resolve_row_times(stop_rows)

        delivered = [r for r in stop_rows if r.delivery_status == "Delivered" and r.delivered_at]
        on_time = sum(1 for r in delivered
                      if r.estimated_arrival and r.delivered_at <= r.estimated_arrival)

        planned_start = t.departure_time
        # OLD: actual_start = min(r.delivered_at for r in delivered) — the
        # earliest time an order on the trip was last edited, which is not a
        # start time at all. It produced badges like "1h 31m late start" from
        # nothing more than when somebody saved a form, and ranked a trip that
        # began four hours EARLY as the worst of the month.
        #
        # A trip started when the driver set off. Where the app stamped that,
        # use it; where it did not, say so rather than inventing one.
        actual_start = min((r.started_real for r in stop_rows if r.get("started_real")),
                           default=None)
        planned_end = max((r.estimated_arrival for r in stop_rows if r.estimated_arrival), default=None)
        actual_end = max((r.delivered_at for r in delivered), default=None)

        planned_span = (planned_end - planned_start).total_seconds() // 60 if planned_start and planned_end else None
        actual_span = (actual_end - actual_start).total_seconds() // 60 if actual_start and actual_end else None

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
            # Delivered stops as well, because "0 / 2" against every stop on the
            # trip reads as a failure when one of the two was never attempted.
            "on_time_of_delivered": f"{on_time} / {len(delivered)}",
            "stops_total":      len(stop_rows),
            "stops_delivered":  len(delivered),
            "badge":            badge,
            "start_recorded":   bool(actual_start),
            "eta_source":       ("planned" if any(r.get("eta_source") == "planned"
                                                  for r in stop_rows)
                                 else "derived" if any(r.get("eta_source") == "derived"
                                                       for r in stop_rows)
                                 else "none"),
        })
    return {"trips": trips}


def _delay_heatmap(dn_rows):
    """Section 10 — day-of-week × time-slot delay intensity.

    Cell = "rare" | "some" | "often" | "usually", scaled off the count
    of late deliveries in that cell. AM=<11am, Mid=11-13, PM=13-17,
    Eve=>=17.
    """
    # OLD: def slot(hour):
    #          if hour < 11:  return 0
    #          if hour < 13:  return 1
    #          if hour < 17:  return 2
    #          return 3
    morning_end = settings.count("heatmap_morning_end_hour", minimum=1)
    midday_end = settings.count("heatmap_midday_end_hour", minimum=1)
    afternoon_end = settings.count("heatmap_afternoon_end_hour", minimum=1)

    def slot(hour):
        if hour < morning_end:   return 0
        if hour < midday_end:    return 1
        if hour < afternoon_end: return 2
        return 3

    # Mon=0, ..., Sat=5, Sun=6
    grid = [[0] * 7 for _ in range(4)]
    for r in dn_rows:
        if r.delivery_status != "Delivered":
            continue
        # OLD: if not (r.expected_arrival and r.delivered_at): continue
        if not r.get("timing_usable"):
            continue
        delta_min = (r.delivered_at - r.expected_arrival).total_seconds() / 60
        if delta_min <= 0:
            continue  # on time / early doesn't fill the delay heatmap
        d = r.delivered_at
        grid[slot(d.hour)][d.weekday()] += 1

    # OLD: def label(n):
    #          if n == 0:  return "rare"
    #          if n <= 2:  return "some"
    #          if n <= 5:  return "often"
    #          return "usually"
    some_max = settings.count("heatmap_some_max", minimum=1)
    often_max = settings.count("heatmap_often_max", minimum=1)

    def label(n):
        if n == 0:          return "rare"
        if n <= some_max:   return "some"
        if n <= often_max:  return "often"
        return "usually"

    quality = _quality(dn_rows)
    return {
        "x_labels": ["M", "T", "W", "T", "F", "S", "S"],
        "y_labels": ["AM", "Mid", "PM", "Eve"],
        "cells": [[label(n) for n in row] for row in grid],
        "data_quality": quality,
        # False when not a single delivery could be measured for lateness. An
        # all-"rare" grid then means "never measured", not "never late", and
        # the client should say so rather than render it as good news.
        "measurable": quality["timing_usable"] > 0,
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
        # OLD: selected dn.modified AS delivered_at and the raw
        # ds.estimated_arrival, then scored on_time / len(rows).
        rows = frappe.db.sql(
            f"""SELECT ds.estimated_arrival,
                      ds.idx      AS stop_idx,
                      dn.modified AS delivered_at,
                      {_delivered_at_expr()} AS delivered_real,
                      dn.cowberry_delivery_status AS status,
                      dt.departure_time
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                 JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                WHERE ds.parent = %s""",
            (name,), as_dict=True,
        )
        # `_resolve_row_times` reads `delivery_status`; this query calls it
        # `status`, so mirror it across before resolving.
        for r in rows:
            r.delivery_status = r.status
        rows = _resolve_row_times(rows)

        delivered = [r for r in rows if r.status == "Delivered" and r.delivered_at]
        if not delivered:
            continue
        on_time = sum(1 for r in delivered
                      if r.estimated_arrival and r.delivered_at <= r.estimated_arrival)
        # OLD: score = round((on_time / len(rows)) * 100)
        # Divided by every stop on the trip, so a trip with an unattempted stop
        # could not reach 100% however punctual the driver was — and it
        # disagreed with on_time_rate_pct, which divides by delivered stops.
        score = round((on_time / len(delivered)) * 100)
        trip_date = rows[0].departure_time
        scored.append({
            "name":      name,
            "date":      _short_date(trip_date) if trip_date else "",
            "score_pct": score,
            "stops_delivered": len(delivered),
            "stops_total":     len(rows),
            "on_time_stops":   on_time,
        })

    # OLD: "best":  scored_by_score[:3]
    #       "worst": sorted(scored, key=lambda x: x["score_pct"])[:3]
    top_n = settings.count("best_worst_count", minimum=1)
    scored_by_score = sorted(scored, key=lambda x: x["score_pct"], reverse=True)
    distinct = len({s["score_pct"] for s in scored})
    return {
        "best":  scored_by_score[:top_n],
        "worst": sorted(scored, key=lambda x: x["score_pct"])[:top_n],
        "ranked_trips":   len(scored),
        # False when every trip ties, which is what made `best` and `worst`
        # come back as the same three trips. The client used to detect that and
        # re-rank on-device off planned-vs-actual start; with real timestamps
        # resolved server-side there is a genuine spread to rank on, and that
        # fallback should be switched off.
        "ranking_usable": distinct > 1,
    }


def _rounding_variance(r):
    """(variance, cash_settled) for one order, in rupees.

    Cash is handed over in whole rupees, so an order worth 297.36 is settled
    at 297.00 and 0.36 goes to the company's Round Off account. Across a day
    of deliveries those fractions add up, and they are the usual reason a
    driver's cash count disagrees with the system by a rupee or two.

    ERPNext's own `rounding_adjustment` is used where it is set. Where it is
    not — the field is 0 whenever Disable Rounded Total is ticked, which is
    the case on many of these orders — the same figure is worked out from
    `rounded_total`, and failing that from the order value rounded to the
    nearest rupee.

    Sign follows ERPNext: positive means the customer paid slightly more than
    the order value, negative means slightly less.
    """
    order_value = flt(r.get("grand_total"))
    if not order_value:
        return 0.0, 0.0
    stored = flt(r.get("rounding_adjustment"))
    if stored:
        return round(stored, 2), round(order_value + stored, 2)
    cash = flt(r.get("rounded_total")) or float(round(order_value))
    return round(cash - order_value, 2), round(cash, 2)


def _variance_label(amount):
    if amount > 0:
        return f"\u20b9{amount:.2f} over"
    if amount < 0:
        return f"\u20b9{abs(amount):.2f} short"
    return "Exact"


def _cash_rounding_rows(dn_rows):
    """Section 12 (Cash Rounding) — the whole-rupee gap, grouped for reading.

    Grouped by day by default. Cash is handed over per day — Driver Collection
    and Cash Submission are both one per driver per day — so the daily total is
    the figure that either reconciles against a handover or does not. Set
    `cash_variance_group_by` to Trip for a driver running several rounds a day.
    """
    group_by = (settings.get("cash_variance_group_by") or "Day").strip()
    buckets = {}
    for r in dn_rows:
        if (r.delivery_status or "").strip() != "Delivered":
            continue
        variance, cash = _rounding_variance(r)
        day = getdate(r.delivered_at) if r.delivered_at else None
        key = (r.trip if group_by == "Trip" else day)
        if key is None:
            continue
        b = buckets.setdefault(key, {
            "day": day, "trips": set(), "orders": 0, "cod_orders": 0,
            "order_value": 0.0, "cash_settled": 0.0,
            "variance": 0.0, "cod_variance": 0.0,
        })
        # A trip spanning midnight keeps the earliest day it touched, so the
        # row still lines up with the handover it belongs to.
        if day and (b["day"] is None or day < b["day"]):
            b["day"] = day
        if r.trip:
            b["trips"].add(r.trip)
        b["orders"] += 1
        b["order_value"] += flt(r.get("grand_total"))
        b["cash_settled"] += cash
        b["variance"] += variance
        if (r.payment_method or "").upper().startswith("COD"):
            b["cod_orders"] += 1
            b["cod_variance"] += variance

    rows = []
    for key, b in buckets.items():
        variance = round(b["variance"], 2)
        trips = sorted(b["trips"])
        label_date = _short_date(b["day"]) if b["day"] else ""
        rows.append({
            "date":         label_date,
            "trip":         key if group_by == "Trip" else None,
            "trips":        trips,
            "orders":       b["orders"],
            "cod_orders":   b["cod_orders"],
            "order_value":  round(b["order_value"], 2),
            "cash_settled": round(b["cash_settled"], 2),
            "variance":     variance,
            "cod_variance": round(b["cod_variance"], 2),
            "label":        _variance_label(variance),
            # Compatibility keys for the existing Flutter row widget, which was
            # built for the minutes-based list. `variance_mins` is meaningless
            # in this mode and is always 0 — read `variance` instead.
            "customer":     (key if group_by == "Trip"
                             else f"{label_date} \u00b7 {b['orders']} order(s)"),
            "variance_mins": 0,
        })
    rows.sort(key=lambda x: (x["date"] or "", x["customer"]))
    return rows


def _time_variance_rows(dn_rows):
    """Section 12 (Time Variance) — the original minutes-early/late list.

    Unchanged in meaning. It used to come back empty on every site because it
    needs a planned arrival time; with `eta_fallback_enabled` on it now has one
    for every delivered stop.
    """
    stops = []
    for r in dn_rows:
        if (r.delivery_status or "").strip() != "Delivered":
            continue
        # OLD: if not (r.expected_arrival and r.delivered_at): continue
        if not r.get("timing_usable"):
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
            "eta_source":    r.get("eta_source"),
            "time_source":   r.get("delivered_source"),
        })
    return stops


def _per_stop_variance(dn_rows):
    """Section 12 — Per-Stop Variance History.

    OLD implementation (kept for reference; it is now `_time_variance_rows`
    and still reachable by setting Per-Stop Variance Shows = Time Variance):

        def _per_stop_variance(dn_rows):
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
            return {"stops": stops[:20]}

    Both modes are always computed and both are returned; `mode` says which
    one `stops` carries, so switching the setting cannot leave a client with
    nothing to read.
    """
    mode = (settings.get("per_stop_variance_mode") or "Cash Rounding").strip()
    limit = settings.count("per_stop_variance_limit", minimum=1)

    cash_rows = _cash_rounding_rows(dn_rows)
    time_rows = _time_variance_rows(dn_rows)
    active = cash_rows if mode == "Cash Rounding" else time_rows

    totals = {
        "variance":     round(sum(r["variance"] for r in cash_rows), 2),
        "cod_variance": round(sum(r["cod_variance"] for r in cash_rows), 2),
        "orders":       sum(r["orders"] for r in cash_rows),
        "order_value":  round(sum(r["order_value"] for r in cash_rows), 2),
        "cash_settled": round(sum(r["cash_settled"] for r in cash_rows), 2),
    }
    totals["label"] = _variance_label(totals["variance"])

    return {
        "mode":          mode,
        "group_by":      settings.get("cash_variance_group_by"),
        "unit":          settings.get("currency_label"),
        "stops":         active[:limit],
        "truncated":     len(active) > limit,
        "totals":        totals,
        "cash_rounding": cash_rows[:limit],
        "time_variance": time_rows[:limit],
        "data_quality":  _quality(dn_rows),
    }


def _eta_accuracy_trend(dn_rows):
    """Section 13 — how far deliveries land from their planned arrival, by day.

    One point per day: the average gap in minutes between when a stop was
    planned to be reached and when it was actually delivered, ignoring
    direction — 10 minutes early and 10 minutes late are both 10 minutes of
    inaccuracy.

    This returned an empty list on every site until now, because it needs a
    planned arrival time and nothing writes one. It now uses whatever
    `_resolve_row_times` could resolve, and reports in `data_quality` how many
    of the points rest on an estimate rather than a real planned time.
    """
    by_day = {}
    for r in dn_rows:
        if (r.delivery_status or "").strip() != "Delivered":
            continue
        # OLD: if not (r.expected_arrival and r.delivered_at): continue
        if not r.get("timing_usable"):
            continue
        d = getdate(r.delivered_at)
        variance = abs((r.delivered_at - r.expected_arrival).total_seconds() / 60)
        by_day.setdefault(d, []).append(variance)

    points = []
    for d in sorted(by_day.keys()):
        avg = round(sum(by_day[d]) / len(by_day[d]))
        points.append({
            "date":          _short_date(d),
            "variance_mins": avg,
            "samples":       len(by_day[d]),
        })

    trend_note = ""
    if len(points) >= 2:
        # OLD: the comparisons below used the literal 2:
        #       if second_avg < first_avg - 2: ... elif second_avg > first_avg + 2:
        threshold = settings.number("eta_trend_threshold_mins")
        first_half = points[:len(points) // 2]
        second_half = points[len(points) // 2:]
        first_avg = sum(p["variance_mins"] for p in first_half) / len(first_half)
        second_avg = sum(p["variance_mins"] for p in second_half) / len(second_half)
        if second_avg < first_avg - threshold:
            trend_note = "Flattening towards zero - Improving accuracy"
        elif second_avg > first_avg + threshold:
            trend_note = "Variance widening - Slipping"
        else:
            trend_note = "Steady variance"
    elif len(points) == 1:
        # One day of data is not a trend, but it is not "no data" either -
        # saying so beats an empty string the client renders as a blank card.
        trend_note = "Only one day in this window - no trend yet"

    quality = _quality(dn_rows)
    return {
        "unit":         "min",
        "trend_note":   trend_note,
        "points":       points,
        "data_quality": quality,
        # True when every point rests on an estimated planned time, so the app
        # can caption the chart rather than present it as measured accuracy.
        "estimated":    quality["eta_planned"] == 0 and quality["eta_derived"] > 0,
    }


# ---------------------------------------------------------------------------
# Small formatting helpers.
# ---------------------------------------------------------------------------

def _period_window(period, from_date, to_date):
    """Resolve the period selector to an inclusive (from, to) date pair.

    `week` and `month` are rolling windows ending today, and their length comes
    from `Analytics Settings`. A 30-day month means "the last 30 days", not the
    calendar month: on 17 September it starts on 19 August, not 1 September.
    """
    # OLD: if period == "week":  return add_days(t, -6), t
    #       if period == "month": return add_days(t, -29), t
    t = getdate(today())
    if period == "today":
        return t, t
    if period == "week":
        return add_days(t, -(settings.count("week_days", minimum=1) - 1)), t
    if period == "month":
        return add_days(t, -(settings.count("month_days", minimum=1) - 1)), t
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
                "delivery_success_rate": s["success_rate"],
                "on_time_rate":          s["on_time_rate"],
                "avg_delay_mins":        s["avg_delay"],
                "total_cod_collected":   s["cod_collected"],
                "wallet_topups":         topup_count,
                "wallet_topup_value":    topup_amount,
                "reschedule_rate":       s["reschedule_rate"],
                "composite_score":       s["composite"],
            },
            "trend":           trend,
            "fleet_avg_score": fleet["composite"],
            "score_breakdown": settings.score_weights(),  # OLD: SCORE_WEIGHTS
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
            """SELECT ds.delivery_note,
                      ds.idx                   AS stop_sequence,
                      dn.customer_name         AS customer,
                      ds.estimated_arrival     AS expected_arrival,
                      dn.modified              AS actual_arrival,
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

        date_filter = ""
        params = [employee]
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
