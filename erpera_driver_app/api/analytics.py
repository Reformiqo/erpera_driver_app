import frappe
from frappe.utils import add_days, flt, get_datetime, getdate, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok


# Composite-score weights per FRD §11 (the spec's score_breakdown surfaces
# them so the Flutter dashboard can render a stacked-weight gauge).
SCORE_WEIGHTS = {
    "on_time_weight": 0.4,
    "success_weight": 0.3,
    "cod_weight":     0.2,
    "comms_weight":   0.1,
}


# ---------------------------------------------------------------------------
# Shared driver-scoped query helpers — used by every section builder.
# ---------------------------------------------------------------------------

def _driver_dn_rows(driver, d_from, d_to):
    """Every Delivery Note reachable through the driver's trips in the
    window. Returns per-row: delivery_note, delivery_status,
    payment_method, grand_total, delivered_at, expected_arrival,
    trip, departure_time, stop_idx, customer_name.

    Filter uses trip departure/creation (same rule as delivery.history)
    so ranking + windowing stay consistent across screens."""
    if not driver:
        return []
    return frappe.db.sql(
        """
        SELECT dn.name                        AS delivery_note,
               dn.cowberry_delivery_status    AS delivery_status,
               dn.cowberry_payment_method     AS payment_method,
               dn.grand_total                 AS grand_total,
               dn.modified                    AS delivered_at,
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
    )


def _summarise(rows):
    """Common counters used by performance_score + key_metrics + vs_fleet.
    Kept as a single pass so we don't re-scan the row set per section."""
    total = len(rows)
    delivered = failed = rescheduled = on_time = 0
    cod_collected = 0.0
    delays = []  # positive minute deltas for delivered rows only
    for r in rows:
        status = (r.delivery_status or "").strip()
        if status == "Delivered":
            delivered += 1
            if (r.payment_method or "").upper().startswith("COD"):
                cod_collected += flt(r.grand_total)
            if r.expected_arrival and r.delivered_at:
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
    composite = round(
        on_time_rate * SCORE_WEIGHTS["on_time_weight"]
        + success_rate * SCORE_WEIGHTS["success_weight"]
        + (100 if cod_collected > 0 else 0) * SCORE_WEIGHTS["cod_weight"]
        + success_rate * SCORE_WEIGHTS["comms_weight"]
    )
    return {
        "total":           total,
        "delivered":       delivered,
        "failed":          failed,
        "rescheduled":     rescheduled,
        "on_time":         on_time,
        "cod_collected":   cod_collected,
        "delays":          delays,
        "success_rate":    success_rate,
        "on_time_rate":    on_time_rate,
        "reschedule_rate": reschedule_rate,
        "avg_delay":       avg_delay,
        "composite":       composite,
    }


def _fleet_summary(d_from, d_to):
    """Fleet-wide summary across ALL drivers in the window."""
    rows = frappe.db.sql(
        """
        SELECT dn.cowberry_delivery_status    AS delivery_status,
               dn.cowberry_payment_method     AS payment_method,
               dn.grand_total                 AS grand_total,
               dn.modified                    AS delivered_at,
               ds.estimated_arrival           AS expected_arrival
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
    )
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
    delta = summary["composite"] - prev["composite"]
    if delta > 0.5:
        delta_label = f"Up {round(delta, 1)} vs last period"
    elif delta < -0.5:
        delta_label = f"Down {abs(round(delta, 1))} vs last period"
    else:
        delta_label = "Flat vs last period"

    fleet_delta = summary["composite"] - fleet["composite"]
    if fleet_delta > 1:
        fleet_label = "You are above average"
    elif fleet_delta < -1:
        fleet_label = "You are below average"
    else:
        fleet_label = "You are at fleet average"

    # comms_weight has no source signal yet — fall back to success_rate so
    # the composite isn't artificially depressed pre-chat-rollout.
    return {
        "score":                summary["composite"],
        "vs_last_month_delta":  delta_label,
        "fleet_avg_score":      fleet["composite"],
        "fleet_avg_comparison": fleet_label,
        "components": [
            {"name": "On-Time", "weight_pct": summary["on_time_rate"], "weight_label": "(40%)"},
            {"name": "Success", "weight_pct": summary["success_rate"], "weight_label": "(30%)"},
            {"name": "COD",     "weight_pct": 100 if summary["cod_collected"] > 0 else 0,
             "weight_label": "(20%)"},
            {"name": "Comms",   "weight_pct": summary["success_rate"], "weight_label": "(10%)"},
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
        if not (r.payment_method or "").upper().startswith("COD"):
            continue
        if not r.delivered_at:
            continue
        d = getdate(r.delivered_at)
        for b in buckets:
            if b["start"] <= d <= b["end"]:
                b["amount"] += flt(r.grand_total)
                total += flt(r.grand_total)
                break

    return {
        "total_period_cod": total,
        "unit":             "INR",
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
            LIMIT 15""",
        (employee, d_from, d_to),
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
            LIMIT 20""",
        (employee, d_from, d_to, limit),
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
            """SELECT ds.estimated_arrival,
                      dn.modified              AS delivered_at,
                      dn.cowberry_delivery_status AS delivery_status
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                WHERE ds.parent = %s
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
            """SELECT ds.estimated_arrival,
                      dn.modified AS delivered_at,
                      dn.cowberry_delivery_status AS status,
                      dt.departure_time
                 FROM `tabDelivery Stop` ds
                 JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                 JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                WHERE ds.parent = %s""",
            (name,), as_dict=True,
        )
        delivered = [r for r in rows if r.status == "Delivered" and r.delivered_at]
        if not delivered:
            continue
        on_time = sum(1 for r in delivered
                      if r.estimated_arrival and r.delivered_at <= r.estimated_arrival)
        score = round((on_time / len(rows)) * 100) if rows else 0
        trip_date = rows[0].departure_time
        scored.append({
            "name":      name,
            "date":      _short_date(trip_date) if trip_date else "",
            "score_pct": score,
        })

    scored_by_score = sorted(scored, key=lambda x: x["score_pct"], reverse=True)
    return {
        "best":  scored_by_score[:3],
        "worst": sorted(scored, key=lambda x: x["score_pct"])[:3],
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
            "score_breakdown": SCORE_WEIGHTS,
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
