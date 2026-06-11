import frappe
from frappe.utils import add_days, flt, getdate, today

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


# ===========================================================================
# CD2-I5 Point 8 — Unified My Analytics screen API (13 sections)
# ===========================================================================

@frappe.whitelist(methods=["GET"])
def get_screen(period="month"):
    """Single endpoint powering the entire My Analytics screen
    (Screenshots 5-11). Returns 13 sections in one payload so the
    Flutter dashboard renders in one render-pass.

    Per Hardik's CD2-I5 point 8 — "Use dummy data where real data is
    not yet available so the frontend can integrate immediately."
    Real driver-specific aggregations slot in over time; the response
    shape is locked TODAY.

    Query:
        period — today | week | month (default: month)

    Sections returned:
        performance_score, key_metrics, timing_compliance, vs_fleet,
        daily_cod_history, cash_submission_compliance,
        collection_limit_breaches, discrepancy_history, trip_timeline,
        delay_heatmap, worst_vs_best_trips, per_stop_variance,
        eta_accuracy_trend
    """
    try:
        employee = _require_driver()
        d_from, d_to = _period_window(period, None, None)
        if d_from is None:
            d_from, d_to = _period_window("month", None, None)

        return ok(data={
            "period":                     period,
            "performance_score":          _performance_score(employee, d_from, d_to),
            "key_metrics":                _key_metrics(employee, d_from, d_to),
            "timing_compliance":          _timing_compliance(employee, d_from, d_to),
            "vs_fleet":                   _vs_fleet(employee, d_from, d_to),
            "daily_cod_history":          _daily_cod_history(employee, d_from, d_to),
            "cash_submission_compliance": _cash_submission_compliance(employee, d_from, d_to),
            "collection_limit_breaches":  _collection_limit_breaches(employee, d_from, d_to),
            "discrepancy_history":        _discrepancy_history(employee, d_from, d_to),
            "trip_timeline":              _trip_timeline(employee, d_from, d_to),
            "delay_heatmap":              _delay_heatmap(employee, d_from, d_to),
            "worst_vs_best_trips":        _worst_vs_best_trips(employee, d_from, d_to),
            "per_stop_variance":          _per_stop_variance(employee, d_from, d_to),
            "eta_accuracy_trend":         _eta_accuracy_trend(employee, d_from, d_to),
        })
    except Exception as e:
        return err("GET_ANALYTICS_SCREEN_FAILED", str(e))


# ---------------------------------------------------------------------------
# Section builders. Each returns the shape Flutter expects. Real data is
# pulled when cheap to compute from existing tables; otherwise dummy data
# matches the Screenshots so the Flutter team can wire visuals today.
# Mark each section with TODO when it returns dummies — easy to grep for
# when iterating real data in.
# ---------------------------------------------------------------------------

def _performance_score(employee, d_from, d_to):
    """Section 1 — Performance Score gauge + components."""
    # Real computation: rates from the existing driver_dashboard logic
    # would feed in here; for v1 we surface a sane composite + dummy
    # comparison values matching Screenshot 5's gauge.
    return {
        "score":                 0,
        "vs_last_month_delta":   "Up to last month",  # TODO real delta
        "fleet_avg_score":       0,
        "fleet_avg_comparison":  "You are above average",  # TODO real comparison
        "components": [
            {"name": "On-Time", "weight_pct": 85, "weight_label": "(40%)"},
            {"name": "Success", "weight_pct": 96, "weight_label": "(30%)"},
            {"name": "COD",     "weight_pct": 100, "weight_label": "(20%)"},
            {"name": "Comms",   "weight_pct": 72, "weight_label": "(10%)"},
        ],
        "_dummy": True,  # TODO: replace with real driver_dashboard math
    }


def _key_metrics(employee, d_from, d_to):
    """Section 2 — Key Metrics tiles (Screenshot 5)."""
    # Quick real numbers where cheap: deliveries completed from
    # cowberry_delivery_status=Delivered.
    delivered = frappe.db.sql(
        """SELECT COUNT(*) FROM `tabDelivery Note` dn
             WHERE dn.docstatus=1
               AND dn.cowberry_delivery_status='Delivered'
               AND DATE(dn.modified) BETWEEN %s AND %s
               AND EXISTS (SELECT 1 FROM `tabDelivery Stop` ds
                            JOIN `tabDelivery Trip` dt ON dt.name=ds.parent
                            JOIN `tabDriver` d ON d.name=dt.driver
                           WHERE ds.delivery_note=dn.name AND d.employee=%s)""",
        (d_from, d_to, employee))[0][0] or 0
    return {
        "deliveries_completed": delivered,
        "success_rate_pct":     0.0,    # TODO real
        "on_time_rate_pct":     0.0,    # TODO real
        "avg_delay_mins":       0.0,    # TODO real (avg of late variance)
        "cod_collected":        0,      # TODO real
        "wallet_topups": {
            "count":  0,
            "amount": 0,
        },
        "reschedule_rate_pct":  0.0,    # TODO real
    }


def _timing_compliance(employee, d_from, d_to):
    """Section 3 — Timing & Compliance (Screenshot 6)."""
    return {
        "avg_trip_start_time":     "9:04 AM",   # TODO real
        "avg_trip_end_time":       "4:38 PM",   # TODO real
        "avg_time_per_stop_mins":  11,           # TODO real
        "cash_discrepancies_this_month": 0,
        "_dummy": True,
    }


def _vs_fleet(employee, d_from, d_to):
    """Section 4 — vs Fleet Average rates (Screenshot 6)."""
    return {
        "on_time_rate": {"driver": 85, "fleet": 78, "delta": +7},
        "success_rate": {"driver": 96, "fleet": 91, "delta": +5},
        "_dummy": True,
    }


def _daily_cod_history(employee, d_from, d_to):
    """Section 5 — Daily COD Collection History (Screenshot 7 bar chart)."""
    return {
        "total_period_cod": 84600,        # TODO real
        "unit":             "INR",
        "buckets": [
            {"label": "W1", "amount": 19500},
            {"label": "W2", "amount": 21000},
            {"label": "W3", "amount": 21300},
            {"label": "W4", "amount": 22800},
        ],
        "_dummy": True,
    }


def _cash_submission_compliance(employee, d_from, d_to):
    """Section 6 — Cash Submission Compliance (Screenshot 7 timeline)."""
    return {
        "entries": [
            {"date": "Mar 15", "status": "On time"},
            {"date": "Mar 14", "status": "Late",  "detail": "14 min late"},
            {"date": "Mar 13", "status": "On time"},
            {"date": "Mar 12", "status": "Late",  "detail": "8 min late"},
            {"date": "Mar 11", "status": "On time"},
            {"date": "Mar 08", "status": "On time"},
            {"date": "Mar 06", "status": "Late",  "detail": "8 min late"},
            {"date": "Mar 03", "status": "On time"},
        ],
        "_dummy": True,
    }


def _collection_limit_breaches(employee, d_from, d_to):
    """Section 7 — Mid-day breaches counter + recent events (Screenshot 8)."""
    return {
        "midday_submissions_count": 3,
        "events": [
            {"date": "Mar 15", "time": "2:45 PM", "amount": 5050},
            {"date": "Mar 09", "time": "4:12 PM", "amount": 5120},
            {"date": "Mar 03", "time": "3:30 PM", "amount": 5085},
        ],
        "_dummy": True,
    }


def _discrepancy_history(employee, d_from, d_to):
    """Section 8 — Cash Submission discrepancies (Screenshot 8)."""
    return {
        "entries": [
            {"id": "DSC-2026-0007", "date": "Mar 14",
             "variance": -30, "reason": "Customer disputed change of ₹30",
             "status": "Under review"},
            {"id": "DSC-2026-0005", "date": "Mar 09",
             "variance": 10,  "reason": "WM verified - matched",
             "status": "Resolved"},
            {"id": "DSC-2026-0003", "date": "Mar 03",
             "variance": -20, "reason": "Pending supervisor review",
             "status": "Open"},
        ],
        "_dummy": True,
    }


def _trip_timeline(employee, d_from, d_to):
    """Section 9 — Per-trip planned vs actual (Screenshot 8)."""
    return {
        "trips": [
            {"name": "DT-2026-0041", "date": "15 Mar",
             "planned_start": "9:00 AM", "actual_start": "9:08 AM",
             "planned_duration": "4h 05m", "actual_duration": "4h 52m",
             "on_time_stops": "8 / 8",
             "badge": "+47 min late"},
            {"name": "DT-2026-0039", "date": "14 Mar",
             "planned_start": "9:00 AM", "actual_start": "8:55 AM",
             "planned_duration": "3h 40m", "actual_duration": "3h 32m",
             "on_time_stops": "7 / 7",
             "badge": "8 min early"},
            {"name": "DT-2026-0037", "date": "13 Mar",
             "planned_start": "9:00 AM", "actual_start": "9:14 AM",
             "planned_duration": "5h 00m", "actual_duration": "5h 45m",
             "on_time_stops": "5 / 9",
             "badge": "+45 min late"},
        ],
        "_dummy": True,
    }


def _delay_heatmap(employee, d_from, d_to):
    """Section 10 — day-of-week × time-slot delay intensity (Screenshot 9).

    Values per cell: "rare" | "some" | "often" | "usually".
    Rows = AM/Mid/PM/Eve; Cols = Mon-Sat.
    """
    return {
        "x_labels": ["M", "T", "W", "T", "F", "S", "S"],
        "y_labels": ["AM", "Mid", "PM", "Eve"],
        "cells": [
            ["rare","some","rare","some","often","rare","rare"],
            ["rare","some","rare","often","often","rare","rare"],
            ["rare","often","some","often","usually","some","rare"],
            ["rare","some","some","often","often","rare","rare"],
        ],
        "_dummy": True,
    }


def _worst_vs_best_trips(employee, d_from, d_to):
    """Section 11 — Worst/Best trips list (Screenshot 9-10)."""
    return {
        "best": [
            {"name": "DT-2026-0039", "date": "14 Mar", "score_pct": 100},
            {"name": "DT-2026-0034", "date": "11 Mar", "score_pct": 95},
        ],
        "worst": [
            {"name": "DT-2026-0037", "date": "13 Mar", "score_pct": 56},
            {"name": "DT-2026-0041", "date": "15 Mar", "score_pct": 75},
        ],
        "_dummy": True,
    }


def _per_stop_variance(employee, d_from, d_to):
    """Section 12 — Per-stop variance customer list (Screenshot 10-11)."""
    return {
        "stops": [
            {"customer": "Priya Sharma", "variance_mins": -2,  "label": "2 min early"},
            {"customer": "Amit Patel",   "variance_mins": 0,   "label": "On time"},
            {"customer": "Meera Joshi",  "variance_mins": 18,  "label": "+18 min late"},
            {"customer": "Rahul Singh",  "variance_mins": 4,   "label": "+4 min late"},
            {"customer": "Dev Mehta",    "variance_mins": -1,  "label": "1 min early"},
            {"customer": "Suresh Mehta", "variance_mins": 12,  "label": "+12 min late"},
            {"customer": "Anika Desai",  "variance_mins": 6,   "label": "+6 min late"},
            {"customer": "Neha Verma",   "variance_mins": 22,  "label": "+22 min late"},
        ],
        "_dummy": True,
    }


def _eta_accuracy_trend(employee, d_from, d_to):
    """Section 13 — ETA Accuracy trend line (Screenshot 11)."""
    # Roughly the shape from Screenshot 11: ETA variance decreasing toward
    # zero over the period (i.e. driver is improving accuracy).
    return {
        "unit":     "min",
        "trend_note": "Flattening towards zero - Improving accuracy",
        "points": [
            {"date": "Mar 01", "variance_mins": 28},
            {"date": "Mar 03", "variance_mins": 24},
            {"date": "Mar 06", "variance_mins": 18},
            {"date": "Mar 09", "variance_mins": 14},
            {"date": "Mar 12", "variance_mins": 11},
            {"date": "Mar 15", "variance_mins": 8},
            {"date": "Mar 18", "variance_mins": 6},
            {"date": "Mar 22", "variance_mins": 4},
        ],
        "_dummy": True,
    }


def _period_window(period, from_date, to_date):
    """Resolve {today, week, month, custom} → (from, to) date pair."""
    t = getdate(today())
    if period == "today":
        return t, t
    if period == "week":
        return add_days(t, -6), t
    if period == "month":
        return add_days(t, -29), t
    # custom — both ends mandatory
    if not from_date or not to_date:
        return None, None
    return getdate(from_date), getdate(to_date)


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Analytics §§1-2)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def driver_dashboard(period="today", **kwargs):
    """§1 KPI bundle for the driver's home dashboard.

    Spec keys: kpis{...}, trend, fleet_avg_score, score_breakdown.
    Period: today | week | month | custom (custom requires from + to).
    """
    try:
        employee = _require_driver()
        form = frappe.local.form_dict
        d_from, d_to = _period_window(
            period,
            form.get("from") or kwargs.get("from_date"),
            form.get("to") or kwargs.get("to_date"),
        )
        if d_from is None:
            return err("VALIDATION_ERROR",
                       "period=custom requires both `from` and `to`.", 400)

        # Per-DN delivery state, scoped to this driver's trips.
        dn_rows = frappe.db.sql(
            """
            SELECT dn.cowberry_delivery_status AS status,
                   dn.cowberry_payment_method  AS pay,
                   dn.grand_total              AS amount,
                   dn.modified                 AS delivered_at,
                   ds.estimated_arrival        AS expected_arrival
              FROM `tabDelivery Note` dn
              JOIN `tabDelivery Stop` ds ON ds.delivery_note = dn.name
              JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
              JOIN `tabDriver` d           ON d.name = dt.driver
             WHERE d.employee = %(emp)s
               AND DATE(dn.modified) BETWEEN %(d_from)s AND %(d_to)s
            """,
            {"emp": employee, "d_from": d_from, "d_to": d_to},
            as_dict=True,
        )
        total = len(dn_rows)
        delivered = sum(1 for r in dn_rows if r.status == "Delivered")
        failed = sum(1 for r in dn_rows if r.status in ("Failed", "Returned"))
        rescheduled = sum(1 for r in dn_rows if r.status == "Rescheduled")
        cod_collected = sum(
            flt(r.amount) for r in dn_rows
            if (r.pay or "").upper().startswith("COD") and r.status == "Delivered"
        )

        # On-time = delivered AND modified <= expected_arrival
        on_time = 0
        delays = []
        for r in dn_rows:
            if r.status == "Delivered" and r.expected_arrival and r.delivered_at:
                delta_mins = (r.delivered_at - r.expected_arrival).total_seconds() / 60
                if delta_mins <= 0:
                    on_time += 1
                else:
                    delays.append(delta_mins)
        avg_delay = round(sum(delays) / len(delays), 2) if delays else 0

        # Wallet stats for the same window, scoped by driver via the
        # "Driver top-up by EMP-…" reference prefix.
        wallet = frappe.db.sql(
            """SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v
                 FROM `tabWallet Transaction`
                WHERE docstatus=1
                  AND reference LIKE %(prefix)s
                  AND DATE(creation) BETWEEN %(d_from)s AND %(d_to)s""",
            {"prefix": f"Driver top-up by {employee}%",
             "d_from": d_from, "d_to": d_to},
            as_dict=True,
        )[0]

        success_rate = round((delivered / total) * 100, 1) if total else 0
        on_time_rate = round((on_time / delivered) * 100, 1) if delivered else 0
        reschedule_rate = round((rescheduled / total) * 100, 1) if total else 0
        # Composite — straight weighted blend of percentages (scaled 0-100).
        # comms_weight has no signal yet, defaults to success_rate so the
        # composite isn't artificially depressed pre-chat-rollout.
        composite = round(
            on_time_rate * SCORE_WEIGHTS["on_time_weight"]
            + success_rate * SCORE_WEIGHTS["success_weight"]
            + (100 if cod_collected > 0 else 0) * SCORE_WEIGHTS["cod_weight"]
            + success_rate * SCORE_WEIGHTS["comms_weight"]
        )

        # Fleet average — same window, all active drivers
        fleet = frappe.db.sql(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN cowberry_delivery_status='Delivered' THEN 1 ELSE 0 END) AS delivered
                 FROM `tabDelivery Note`
                WHERE docstatus=1
                  AND DATE(modified) BETWEEN %s AND %s""",
            (d_from, d_to), as_dict=True,
        )[0]
        fleet_success = (flt(fleet.delivered) / flt(fleet.total) * 100) if flt(fleet.total) else 0
        fleet_avg_score = round(fleet_success)  # rough proxy until per-driver scores aggregate

        # Trend = compare this window to the prior window of the same size
        prev_from = add_days(d_from, -(d_to - d_from).days - 1)
        prev_to = add_days(d_from, -1)
        prev_delivered = frappe.db.sql(
            """SELECT COUNT(*) AS n FROM `tabDelivery Note` dn
                 JOIN `tabDelivery Stop` ds ON ds.delivery_note = dn.name
                 JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                 JOIN `tabDriver` d ON d.name = dt.driver
                WHERE d.employee=%s AND dn.cowberry_delivery_status='Delivered'
                  AND DATE(dn.modified) BETWEEN %s AND %s""",
            (employee, prev_from, prev_to),
        )[0][0] or 0
        trend = "flat"
        if delivered > prev_delivered:
            trend = "up"
        elif delivered < prev_delivered:
            trend = "down"

        return ok(data={
            "kpis": {
                "total_delivered":       delivered,
                "delivery_success_rate": success_rate,
                "on_time_rate":          on_time_rate,
                "avg_delay_mins":        avg_delay,
                "total_cod_collected":   cod_collected,
                "wallet_topups":         int(flt(wallet.n)),
                "wallet_topup_value":    flt(wallet.v),
                "reschedule_rate":       reschedule_rate,
                "composite_score":       composite,
            },
            "trend":            trend,
            "fleet_avg_score":  fleet_avg_score,
            "score_breakdown":  SCORE_WEIGHTS,
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
        # Scope: only the assigned driver may read.
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
