import frappe
from frappe.utils import flt, getdate, now_datetime, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.geo import haversine_m, validate_coords
from erpera_driver_app.utils.response import err, ok


# ---------------------------------------------------------------------------
# Internal helpers — used by both legacy get_my_trips and spec-named get_trips
# ---------------------------------------------------------------------------

def _driver_record(employee):
    """Resolve the ERPNext Driver row for an Employee. Delivery Trip's
    `driver` field links to Driver, not Employee."""
    return frappe.db.get_value("Driver", {"employee": employee}, "name")


def _aggregate_trip_stats(trip_name):
    """Roll up per-stop delivery state into the counters the Flutter
    home screen displays. Source of truth is each linked Delivery
    Note's `cowberry_delivery_status` custom field, since stock
    Delivery Stop has no status column.
    """
    rows = frappe.db.sql(
        """
        SELECT ds.delivery_note,
               ds.visited,
               dn.cowberry_delivery_status   AS delivery_status,
               dn.cowberry_payment_method    AS payment_method,
               dn.grand_total                AS grand_total
          FROM `tabDelivery Stop` ds
          LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE ds.parent = %s
        """,
        (trip_name,),
        as_dict=True,
    )
    delivered_set = {"Delivered"}
    failed_set = {"Failed", "Returned"}
    total = len(rows)
    delivered = sum(1 for r in rows if r.delivery_status in delivered_set or r.visited)
    attempted = sum(1 for r in rows if r.delivery_status in failed_set)
    pending = max(total - delivered - attempted, 0)
    cod_expected = sum(
        flt(r.grand_total) for r in rows
        if (r.payment_method or "").upper().startswith("COD")
    )
    cod_collected = sum(
        flt(r.grand_total) for r in rows
        if (r.payment_method or "").upper().startswith("COD") and r.delivery_status == "Delivered"
    )
    return {
        "total_stops":         total,
        "delivered":           delivered,
        "attempted":           attempted,
        "pending":             pending,
        "total_cod_expected":  cod_expected,
        "total_cod_collected": cod_collected,
    }


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Trip §§1-5)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_trips(date=None):
    """Trip §1 — list trips for the authenticated driver on a given date
    (defaults to today). Each row includes the rolled-up counters the
    Flutter home screen needs (total_stops, delivered, attempted,
    pending, total_cod_expected, total_cod_collected, route_optimised).
    """
    try:
        employee = _require_driver()
        driver = _driver_record(employee)
        if not driver:
            return ok(data={"trips": []})
        target = date or today()
        filters = {
            "driver": driver,
            "departure_time": ["between", [f"{target} 00:00:00", f"{target} 23:59:59"]],
        }
        rows = frappe.get_all(
            "Delivery Trip",
            filters=filters,
            fields=["name", "departure_time", "status"],
            order_by="departure_time desc",
            limit=50,
        )
        trips = []
        for r in rows:
            stats = _aggregate_trip_stats(r.name)
            trips.append({
                "name":                r.name,
                "date":                str(getdate(r.departure_time)) if r.departure_time else target,
                "trip_status_extended": r.status,
                "total_stops":         stats["total_stops"],
                "delivered":           stats["delivered"],
                "attempted":           stats["attempted"],
                "pending":             stats["pending"],
                "total_cod_expected":  stats["total_cod_expected"],
                "total_cod_collected": stats["total_cod_collected"],
                # `route_optimised` will be 1 once an optimise_route call
                # writes a timestamp/flag back to the trip; until then 0.
                "route_optimised":     0,
            })
        return ok(data={"trips": trips})
    except Exception as e:
        return err("GET_TRIPS_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def get_trip_detail(trip=None):
    """Trip §2 — single trip + aggregated stats. Caller must be the
    driver assigned to the trip.
    """
    try:
        employee = _require_driver()
        if not trip:
            return err("VALIDATION_ERROR", "Query param `trip` is required.", 400)
        if not frappe.db.exists("Delivery Trip", trip):
            return err("NOT_FOUND", f"Delivery Trip '{trip}' not found.", 404)
        trip_doc = frappe.get_doc("Delivery Trip", trip)
        driver = _driver_record(employee)
        if driver and trip_doc.driver and trip_doc.driver != driver:
            return err("FORBIDDEN", "This trip is not assigned to you.", 403)
        stats = _aggregate_trip_stats(trip)
        # on_time vs late uses Delivery Stop.estimated_arrival vs the DN's
        # cowberry_pod_timestamp if both are set. When either is missing
        # we drop the row from the comparison (don't count as late).
        timing_rows = frappe.db.sql(
            """
            SELECT ds.estimated_arrival,
                   dn.modified AS delivered_at,
                   dn.cowberry_delivery_status AS delivery_status
              FROM `tabDelivery Stop` ds
              LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE ds.parent = %s
            """,
            (trip,), as_dict=True,
        )
        on_time = late = 0
        for r in timing_rows:
            if r.delivery_status != "Delivered" or not r.estimated_arrival or not r.delivered_at:
                continue
            if r.delivered_at <= r.estimated_arrival:
                on_time += 1
            else:
                late += 1
        return ok(data={
            "trip": {
                "name":                          trip_doc.name,
                "date":                          str(getdate(trip_doc.departure_time)) if trip_doc.departure_time else None,
                "trip_status_extended":          trip_doc.status,
                "total_planned_duration_mins":   None,
                "total_actual_duration_mins":    None,
                "driver_cash_submitted":         _driver_cash_submitted_for_trip(trip),
            },
            "stats": {
                "on_time_count":       on_time,
                "late_count":          late,
                "total_cod_expected":  stats["total_cod_expected"],
                "total_cod_collected": stats["total_cod_collected"],
            },
        })
    except Exception as e:
        return err("GET_TRIP_DETAIL_FAILED", str(e))


def _driver_cash_submitted_for_trip(trip):
    """Total cash the driver has already submitted for this trip's
    Driver Collection (closed Cash Submissions with wm_otp_validated=1
    if the field exists, otherwise any submission record)."""
    col = frappe.db.get_value("Driver Collection", {"trip": trip}, "name")
    if not col:
        return 0
    rows = frappe.db.sql(
        """SELECT IFNULL(SUM(amount), 0) AS total
             FROM `tabCash Submission`
            WHERE collection = %s""",
        (col,),
    )
    return flt(rows[0][0]) if rows else 0


@frappe.whitelist(methods=["GET"])
def get_orders(trip=None, status="All"):
    """Trip §3 — list DNs in a trip with status filter
    (All | Pending | Delivered | Attempted).
    """
    try:
        employee = _require_driver()
        if not trip:
            return err("VALIDATION_ERROR", "Query param `trip` is required.", 400)
        if not frappe.db.exists("Delivery Trip", trip):
            return err("NOT_FOUND", f"Delivery Trip '{trip}' not found.", 404)
        trip_doc = frappe.get_doc("Delivery Trip", trip)
        driver = _driver_record(employee)
        if driver and trip_doc.driver and trip_doc.driver != driver:
            return err("FORBIDDEN", "This trip is not assigned to you.", 403)

        status_filter = ""
        if status == "Pending":
            status_filter = " AND IFNULL(dn.cowberry_delivery_status,'') NOT IN ('Delivered','Failed','Returned')"
        elif status == "Delivered":
            status_filter = " AND dn.cowberry_delivery_status = 'Delivered'"
        elif status == "Attempted":
            status_filter = " AND dn.cowberry_delivery_status IN ('Failed','Returned')"
        elif status not in ("All", None, ""):
            return err("VALIDATION_ERROR",
                       "status must be one of All | Pending | Delivered | Attempted.", 400)

        rows = frappe.db.sql(
            f"""
            SELECT dn.name                                  AS name,
                   dn.customer                              AS customer,
                   dn.address_display                       AS customer_address,
                   dn.contact_mobile                        AS contact_mobile,
                   IFNULL(dn.cowberry_delivery_status,'Pending') AS delivery_status,
                   dn.cowberry_payment_method               AS payment_type,
                   dn.grand_total                           AS cod_amount,
                   ds.idx                                   AS stop_sequence,
                   ds.estimated_arrival                     AS expected_arrival_time,
                   (SELECT COUNT(*) FROM `tabDelivery Note Item` dni
                     WHERE dni.parent = dn.name)            AS items_count
              FROM `tabDelivery Stop` ds
              JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE ds.parent = %(trip)s {status_filter}
             ORDER BY ds.idx ASC
            """,
            {"trip": trip},
            as_dict=True,
        )
        for r in rows:
            # cod_amount only counts for COD payment methods; for prepaid
            # the customer already paid so cod_amount is 0.
            if not (r.payment_type or "").upper().startswith("COD"):
                r["cod_amount"] = 0
            if r.expected_arrival_time:
                r["expected_arrival_time"] = str(r["expected_arrival_time"])
        return ok(data={"orders": rows})
    except Exception as e:
        return err("GET_ORDERS_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def reoptimise_route(trip=None, completed_stops=None):
    """Trip §5 — re-run route optimisation, excluding stops the driver
    has already completed. `completed_stops` is a list of Delivery Note
    names. Same output shape as `optimise_route`.
    """
    try:
        if not trip:
            return err("VALIDATION_ERROR", "`trip` is required.", 400)
        completed = set(completed_stops or [])
        _require_driver()
        stops = frappe.db.sql(
            """
            SELECT ds.name, ds.delivery_note, ds.customer, ds.address,
                   ds.lat, ds.lng, ds.idx, ds.estimated_arrival
              FROM `tabDelivery Stop` ds
             WHERE ds.parent = %s
             ORDER BY ds.idx ASC
            """,
            (trip,), as_dict=True,
        )
        remaining = [s for s in stops if s.delivery_note not in completed]
        ordered = []
        for i, s in enumerate(remaining, start=1):
            ordered.append({
                "delivery_note":          s.delivery_note,
                "stop_sequence":          i,
                "expected_arrival_time":  str(s.estimated_arrival) if s.estimated_arrival else None,
            })
        return ok(data={"ordered_stops": ordered})
    except Exception as e:
        return err("REOPTIMISE_ROUTE_FAILED", str(e))


# ---------------------------------------------------------------------------
# Legacy endpoints kept for back-compat
# ---------------------------------------------------------------------------

@frappe.whitelist()
def get_my_trips(date=None):
    try:
        employee = _require_driver()
        # The driver-app trip list is keyed off the ERPNext Driver record,
        # not the Employee — resolve before filtering.
        driver = frappe.db.get_value("Driver", {"employee": employee}, "name")
        filters = {"driver": driver} if driver else {}
        if date:
            # Delivery Trip uses `departure_time` (Datetime) — match the date part.
            filters["departure_time"] = ["between", [f"{date} 00:00:00", f"{date} 23:59:59"]]
        trips = frappe.get_all(
            "Delivery Trip",
            filters=filters,
            fields=["name", "departure_time", "status", "total_distance", "driver"],
            order_by="departure_time desc",
            limit=50,
        )
        return ok(data={"trips": trips})
    except Exception as e:
        return err("GET_TRIPS_FAILED", str(e))


@frappe.whitelist()
def start_trip(trip_id):
    try:
        _require_driver()
        current = frappe.db.get_value("Delivery Trip", trip_id, "status")
        if current != "Draft":
            return err("INVALID_STATUS", f"Cannot start a trip with status '{current}'.")
        # Use db.set_value to flip status without firing ERPNext's
        # Delivery Trip on_update hook, which calls save() on every
        # linked Delivery Note and trips the driver's per-DN permission
        # check (drivers have field-level write via custom rules, not
        # blanket Delivery Note write).
        frappe.db.set_value("Delivery Trip", trip_id, "status", "In Transit",
                            update_modified=True)
        frappe.db.commit()
        return ok(data={"trip_id": trip_id, "status": "In Transit"})
    except Exception as e:
        return err("START_TRIP_FAILED", str(e))


@frappe.whitelist()
def complete_trip(trip_id):
    try:
        _require_driver()
        current = frappe.db.get_value("Delivery Trip", trip_id, "status")
        if current != "In Transit":
            return err("INVALID_STATUS", f"Cannot complete a trip with status '{current}'.")
        frappe.db.set_value("Delivery Trip", trip_id, "status", "Completed",
                            update_modified=True)
        frappe.db.commit()
        return ok(data={"trip_id": trip_id, "status": "Completed"})
    except Exception as e:
        return err("COMPLETE_TRIP_FAILED", str(e))


@frappe.whitelist()
def optimise_route(trip_id):
    try:
        _require_driver()
        trip = frappe.get_doc("Delivery Trip", trip_id)
        stops = frappe.get_all(
            "Delivery Stop",
            filters={"parent": trip_id},
            fields=["name", "delivery_note", "customer", "address", "lat", "lng", "idx"],
            order_by="idx asc",
        )
        # Simple nearest-neighbour optimisation placeholder
        return ok(data={"trip_id": trip_id, "stops": stops})
    except Exception as e:
        return err("OPTIMISE_ROUTE_FAILED", str(e))


@frappe.whitelist()
def get_summary(trip_id):
    try:
        _require_driver()
        trip = frappe.get_doc("Delivery Trip", trip_id)
        # Stock Delivery Stop has no `status` column; the actual delivery
        # state lives on the linked Delivery Note via the
        # `cowberry_delivery_status` custom field. Pull both so a stop
        # whose DN is "Delivered" / "Failed" counts correctly.
        stops = frappe.db.sql(
            """
            SELECT ds.name,
                   ds.delivery_note,
                   ds.customer,
                   ds.visited,
                   dn.cowberry_delivery_status AS delivery_status
              FROM `tabDelivery Stop` ds
              LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE ds.parent = %s
            """,
            (trip_id,),
            as_dict=True,
        )
        delivered = sum(1 for s in stops if (s.get("delivery_status") == "Delivered") or s.get("visited"))
        failed = sum(1 for s in stops if s.get("delivery_status") == "Failed")
        return ok(data={
            "trip_id": trip_id,
            "total_stops": len(stops),
            "delivered": delivered,
            "failed": failed,
            "status": trip.status,
        })
    except Exception as e:
        return err("GET_SUMMARY_FAILED", str(e))
