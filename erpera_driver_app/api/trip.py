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


# Cache the Delivery Note fieldnames once per request — repeated meta lookups
# would otherwise dominate the per-stop loop time.
_DN_FIELDS = None
def _dn_field_names():
    global _DN_FIELDS
    if _DN_FIELDS is None:
        _DN_FIELDS = {f.fieldname for f in frappe.get_meta("Delivery Note").fields}
    return _DN_FIELDS


def _resolve_payment_type(dn_name):
    """Return 'Prepaid' or 'COD' for a Delivery Note (CD2-I5 points 2+3).

    The site stores this under one of several custom fields depending on
    which integrations are installed:
      - cowberry_payment_method      (our spec field — set by Sales Order sync)
      - delhivery_payment_mode       (delhivery_integration's own field)
      - shopify_order_status         (paid/pending → infer Prepaid/COD)

    Tries each in priority order, falls back to 'Prepaid' (the safe default
    — online checkouts are the majority of incoming orders today).
    """
    available = _dn_field_names()
    for fname in ("cowberry_payment_method", "delhivery_payment_mode"):
        if fname in available:
            val = frappe.db.get_value("Delivery Note", dn_name, fname)
            if val:
                return "COD" if str(val).upper().startswith("COD") else "Prepaid"
    return "Prepaid"


def _warehouse_info(warehouse_name):
    """Return {name, code} for a Warehouse — the shape the Flutter cards
    consume. `name` is the human label (suffix stripped); `code` is the
    short code (Warehouse Code custom field if installed, else parsed
    from the warehouse name prefix)."""
    if not warehouse_name:
        return None
    code = None
    wh_fields = {f.fieldname for f in frappe.get_meta("Warehouse").fields}
    if "warehouse_code" in wh_fields:
        code = frappe.db.get_value("Warehouse", warehouse_name, "warehouse_code")
    if not code:
        # Pattern: "Surat Hub - CIPL" → code WH001 isn't computable; fall
        # back to the prefix before " - " as a stable identifier.
        code = warehouse_name.split(" - ")[0] if " - " in warehouse_name else warehouse_name
    label = warehouse_name.replace(" - CIPL", "").replace(" - ET", "").strip() or warehouse_name
    return {"name": label, "code": code}


# Map raw cowberry_delivery_status → UI-friendly "order stage" labels
# the Flutter cards display (CD2-I5 points 2+3).
_STAGE_MAP = {
    "Delivered": "Completed",
    "Out for Pickup":   "On the way",
    "Picked Up":        "On the way",
    "Out for Delivery": "On the way",
    "At Location":      "On the way",
    "Failed":     "Attempted",
    "Returned":   "Attempted",
    "Attempted":  "Attempted",
    "Rescheduled":"Rescheduled",
    "Cancelled":  "Cancelled",
}
def _order_stage(delivery_status):
    return _STAGE_MAP.get(delivery_status or "", "Pending")


def _resolve_expected_arrival(stop_estimated, trip_departure, stop_idx,
                              default_minutes_per_stop=30):
    """Fallback for missing Delivery Stop.estimated_arrival.

    When the warehouse manager set up the trip but didn't fill estimated
    arrival per stop, derive an ETA from the trip's departure_time + a
    fixed per-stop window. Better than handing the Flutter card a null
    that renders as '--:--' on screen.
    """
    if stop_estimated:
        return stop_estimated
    if trip_departure and stop_idx:
        from frappe.utils import add_to_date
        return add_to_date(trip_departure, minutes=stop_idx * default_minutes_per_stop)
    return None


def _aggregate_trip_stats(trip_name):
    """Roll up per-stop delivery state into the counters the Flutter
    home screen displays. Source of truth is each linked Delivery
    Note's `cowberry_delivery_status` custom field, since stock
    Delivery Stop has no status column.

    Payment type is resolved in Python via _resolve_payment_type() so
    we never reference cowberry_payment_method directly in SQL — that
    column may not exist on every bench (e.g. fresh dev installs that
    haven't run the fixture migrate). Falls back through
    cowberry_payment_method → delhivery_payment_mode → 'Prepaid'.
    """
    rows = frappe.db.sql(
        """
        SELECT ds.delivery_note,
               ds.visited,
               dn.cowberry_delivery_status   AS delivery_status,
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

    # Resolve payment type per row via the safe helper (one db.get_value
    # per row; cached fieldnames make this cheap).
    cod_expected = cod_collected = 0
    prepaid_count = cod_count = 0
    for r in rows:
        if not r.delivery_note:
            continue
        ptype = _resolve_payment_type(r.delivery_note)
        if ptype == "COD":
            cod_count += 1
            cod_expected += flt(r.grand_total)
            if r.delivery_status == "Delivered":
                cod_collected += flt(r.grand_total)
        else:
            prepaid_count += 1
    return {
        "total_stops":         total,
        "delivered":           delivered,
        "attempted":           attempted,
        "pending":             pending,
        "prepaid_count":       prepaid_count,
        "cod_count":           cod_count,
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

            # CD2-I5 Point 1: per-order status array + warehouse details +
            # cash_submitted (when Completed).
            order_rows = frappe.db.sql(
                """SELECT ds.delivery_note,
                          IFNULL(dn.cowberry_delivery_status,'Pending') AS delivery_status
                     FROM `tabDelivery Stop` ds
                     LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                    WHERE ds.parent = %s
                    ORDER BY ds.idx ASC""",
                (r.name,), as_dict=True,
            )
            orders_status = [
                {"delivery_note": o.delivery_note,
                 "delivery_status": o.delivery_status,
                 "order_stage": _order_stage(o.delivery_status)}
                for o in order_rows
            ]
            # Source warehouse — Delivery Trip custom field added by
            # erpera_driver_app fixtures.
            trip_doc_meta = frappe.db.get_value("Delivery Trip", r.name,
                ["source_warehouse"], as_dict=True) or {}
            warehouse = _warehouse_info(trip_doc_meta.get("source_warehouse"))

            # Cash already submitted for this trip (Completed trips only;
            # otherwise the Flutter card shows the in-progress total).
            cash_submitted = (
                _driver_cash_submitted_for_trip(r.name)
                if r.status in ("Completed", "Cancelled") else 0
            )

            trips.append({
                "name":                r.name,
                "date":                str(getdate(r.departure_time)) if r.departure_time else target,
                "trip_status_extended": r.status,
                "total_stops":         stats["total_stops"],
                "delivered":           stats["delivered"],
                "attempted":           stats["attempted"],
                "pending":             stats["pending"],
                "prepaid_count":       stats["prepaid_count"],
                "cod_count":           stats["cod_count"],
                "total_cod_expected":  stats["total_cod_expected"],
                "total_cod_collected": stats["total_cod_collected"],
                "cash_submitted":      cash_submitted,
                "warehouse":           warehouse,
                "orders_status":       orders_status,
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
                   dn.customer_name                         AS customer_name,
                   dn.address_display                       AS customer_address,
                   dn.contact_mobile                        AS contact_mobile,
                   IFNULL(dn.cowberry_delivery_status,'Pending') AS delivery_status,
                   dn.grand_total                           AS grand_total,
                   dn.set_warehouse                         AS warehouse_name,
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

        # Pull the trip departure once for the expected_arrival_time fallback.
        trip_departure = frappe.db.get_value("Delivery Trip", trip, "departure_time")

        orders = []
        for r in rows:
            # CD2-I5 Point 2: payment_type via safe Python-level resolver
            # (the SQL field cowberry_payment_method may be empty/missing).
            payment_type = _resolve_payment_type(r.name)
            # CD2-I5 Point 2: expected_arrival_time fallback when stop-level
            # value is unset — derive from trip departure + stop sequence.
            eta = _resolve_expected_arrival(r.expected_arrival_time,
                                            trip_departure, r.stop_sequence)
            # CD2-I5 Point 2: warehouse details (was missing entirely).
            warehouse = _warehouse_info(r.warehouse_name)

            orders.append({
                "name":                  r.name,
                "customer":              r.customer_name or r.customer,
                "customer_address":      r.customer_address,
                "contact_mobile":        r.contact_mobile,
                "delivery_status":       r.delivery_status,
                # CD2-I5 Point 2: per-order stage label (Completed / On the way / Pending)
                "order_stage":           _order_stage(r.delivery_status),
                "payment_type":          payment_type,
                "cod_amount":            (flt(r.grand_total) if payment_type == "COD" else 0),
                "stop_sequence":         r.stop_sequence,
                "expected_arrival_time": str(eta) if eta else None,
                "items_count":           r.items_count,
                "warehouse":             warehouse,
            })
        return ok(data={"orders": orders})
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
