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
