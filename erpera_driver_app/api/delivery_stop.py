"""Delivery Stop API — the stop rows themselves, not the orders behind them.

`trip.get_orders` already returns the Delivery Notes on one trip. This module
returns the `Delivery Stop` rows: sequence, coordinates, visited flag and
estimated arrival — what a map, a route sheet or a manager's overview needs,
across trips rather than inside one.

Scope
-----
These endpoints return **every** Delivery Stop on the site. The caller's role
does not narrow the result: a driver and a warehouse manager see the same
rows. Callers must still be authenticated and hold a driver or manager role,
so this is not public, but within that set there is no row-level filtering.

That is deliberate and was specified. It does mean a driver can read every
other driver's customer names, addresses, contact numbers and COD amounts, so
if this endpoint is ever pointed at the driver mobile app rather than an
internal or manager-facing surface, pass `driver=` to narrow it — or say the
word and the per-driver scoping goes back in.

Delivery Stop is a child table of Delivery Trip, so a stop's trip, driver and
warehouse all come through the parent join.

Endpoints
---------
    get_stops(...)        list, filtered and paginated
    get_stop(stop)        one stop by its row name
"""

import frappe
from frappe.utils import cint, flt, getdate

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.api.trip import (
    _PAYMENT_FIELDS,
    _dn_field_names,
    _order_stage,
    _warehouse_info,
)
from erpera_driver_app.utils.cod import expected_cod
from erpera_driver_app.utils.exceptions import NotDriverError
from erpera_driver_app.utils.response import err, ok

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# Compared case-insensitively: the site carries a lowercase `warehouse manager`
# created through the UI beside Title-Case ERPNext defaults, and an exact match
# silently locks out the people it is meant to admit.
MANAGER_ROLES = {"system manager", "delivery manager", "warehouse manager",
                 "warehouse executive"}

# Delivery Stop columns worth returning, mapped to the response key they carry.
# Selected only where the column exists — the child doctype has changed shape
# across ERPNext versions and this app runs on more than one.
_STOP_COLUMNS = {
    "delivery_note":     "delivery_note",
    "customer":          "customer",
    "address":           "address",
    "customer_address":  "customer_address",
    "contact":           "contact",
    "lat":               "latitude",
    "lng":               "longitude",
    "visited":           "visited",
    "estimated_arrival": "estimated_arrival",
    "distance":          "distance",
    "grand_total":       "stop_grand_total",
    "details":           "details",
}

# Status filter → the delivery statuses it covers. Delivery Stop itself has no
# status column; the state lives on the linked Delivery Note.
_STATUS_FILTERS = {
    "Pending":   ("NOT IN", ("Delivered", "Failed", "Returned", "Cancelled")),
    "Delivered": ("IN", ("Delivered",)),
    "Attempted": ("IN", ("Failed", "Returned", "Attempted")),
    "Cancelled": ("IN", ("Cancelled",)),
}


# Which date a date filter measures. A Delivery Stop sits between three
# meaningful dates, and "stops on 14 Aug" picks a different set for each, so
# the caller says which one they mean rather than the endpoint guessing.
_DATE_FIELDS = {
    # When the driver actually ran the trip. Falls back to the trip's creation
    # date, because a trip whose route the manager has not finalised yet has
    # no departure_time — the same rule delivery.history uses, so a stop lands
    # on the same day whichever screen shows it.
    "trip": "(CASE WHEN dt.departure_time IS NULL THEN DATE(dt.creation)"
            " ELSE DATE(dt.departure_time) END)",
    # The stop's own scheduled arrival, written by ERPNext's route pass.
    "arrival": "DATE(ds.estimated_arrival)",
    # The Delivery Note's own document date.
    "posting": "DATE(dn.posting_date)",
}
DEFAULT_DATE_FIELD = "trip"


def _stop_field_names():
    return {f.fieldname for f in frappe.get_meta("Delivery Stop").fields}


def _date_window(filters):
    """Resolve the date filter to (from, to, field). (None, None, field) when
    no date was given — an unfiltered call still returns every stop.

    `date` is the single-day shorthand for `from` = `to`. When a range is also
    supplied the range wins, matching `delivery.history`, so the two endpoints
    cannot disagree about the same query string. A range with only one end is
    that one day.
    """
    field = (filters.get("date_field") or DEFAULT_DATE_FIELD).strip().lower()
    if field not in _DATE_FIELDS:
        raise ValueError(
            "date_field must be one of: " + ", ".join(_DATE_FIELDS))

    d_from, d_to = filters.get("from"), filters.get("to")
    if not (d_from or d_to):
        d_from = d_to = filters.get("date")
    if not (d_from or d_to):
        return None, None, field

    try:
        d_from = getdate(d_from or d_to)
        d_to = getdate(d_to or d_from)
    except Exception:
        raise ValueError("Dates must be in YYYY-MM-DD format.")
    if d_from > d_to:
        # A range entered backwards is a typo, not an empty result.
        d_from, d_to = d_to, d_from
    return d_from, d_to, field


def _require_app_user():
    """Admit anyone who belongs to the app; do not narrow what they see.

    Every caller gets every stop, so the only question here is whether they
    belong at all. A manager role is enough on its own. Otherwise the caller
    must pass `_require_driver`, which checks the Driver role *and* that an
    Employee is linked — the same bar every other driver endpoint sets, so a
    stray portal or website user cannot reach fleet data.
    """
    roles = {r.strip().lower() for r in frappe.get_roles()}
    if roles & MANAGER_ROLES:
        return
    _require_driver()   # raises NotDriverError → 403


def _payment_columns():
    """Payment-method columns present on this bench, in precedence order.

    Three fields carry the method on the live site and none is populated
    consistently, so reading one mislabels COD orders as Prepaid. They are
    pulled in the list query and resolved in Python — one lookup per field
    per row would be several hundred queries on a full page of stops.
    """
    available = _dn_field_names()
    return [f for f in _PAYMENT_FIELDS if f in available]


def _payment_type(row, columns):
    for fname in columns:
        val = row.get(fname)
        if val:
            return "COD" if str(val).upper().startswith("COD") else "Prepaid"
    return "Prepaid"


def _build_conditions(filters):
    """Return (where_sql, params). Raises ValueError on a bad filter value.

    No row-level scoping: the only driver condition is the optional `driver`
    filter the caller asked for. Cancelled trips are excluded by default —
    their stops are not work anybody is doing.
    """
    conditions = ["dt.docstatus < 2"]
    params = {}

    if filters.get("driver"):
        conditions.append("dt.driver = %(driver)s")
        params["driver"] = filters["driver"]

    if filters.get("trip"):
        conditions.append("ds.parent = %(trip)s")
        params["trip"] = filters["trip"]

    if filters.get("delivery_note"):
        conditions.append("ds.delivery_note = %(delivery_note)s")
        params["delivery_note"] = filters["delivery_note"]

    if filters.get("customer"):
        conditions.append("ds.customer = %(customer)s")
        params["customer"] = filters["customer"]

    d_from, d_to, date_field = _date_window(filters)
    if d_from:
        conditions.append(
            f"{_DATE_FIELDS[date_field]} BETWEEN %(d_from)s AND %(d_to)s")
        params["d_from"] = d_from
        params["d_to"] = d_to

    visited = filters.get("visited")
    if visited not in (None, "", "All"):
        truthy = str(visited).strip().lower() in ("1", "true", "yes")
        conditions.append("IFNULL(ds.visited, 0) = %(visited)s")
        params["visited"] = 1 if truthy else 0

    status = (filters.get("status") or "All").strip().title()
    if status not in ("All",) + tuple(_STATUS_FILTERS):
        raise ValueError(
            "status must be one of: All, " + ", ".join(_STATUS_FILTERS))
    if status != "All":
        operator, values = _STATUS_FILTERS[status]
        placeholders = ", ".join(f"%(st{i})s" for i in range(len(values)))
        # A stop whose note has no status yet is Pending, and IFNULL keeps it
        # inside a NOT IN comparison that would otherwise drop it.
        conditions.append(
            f"IFNULL(dn.cowberry_delivery_status, 'Pending') {operator} ({placeholders})")
        for i, v in enumerate(values):
            params[f"st{i}"] = v

    return " AND ".join(conditions), params


@frappe.whitelist(methods=["GET"])
def get_stops(trip=None, status="All", visited=None, delivery_note=None,
              customer=None, driver=None, date=None, date_field=None,
              limit=None, offset=0, **kwargs):
    """List Delivery Stops.

    Query parameters, all optional:

        trip           one Delivery Trip's stops
        status         All | Pending | Delivered | Attempted | Cancelled
        visited        1 or 0 to filter on the stop's visited flag
        delivery_note  stops carrying one Delivery Note (it can be on several)
        customer       stops for one customer
        driver         narrow to one Driver record
        date           YYYY-MM-DD, a single day
        from / to      YYYY-MM-DD, an inclusive range. Supplying only one end
                       makes it that single day; a backwards range is swapped
                       rather than returning nothing. A range beats `date`.
        date_field     which date the filter measures — trip (default) |
                       arrival | posting. See below.
        limit / offset paging; limit defaults to 100 and is capped at 500

    `date_field` matters because a stop carries three different dates:

        trip      the day the driver ran the trip (departure_time, falling
                  back to the trip's creation). This is the day the work
                  happened and is what every other screen windows on.
        arrival   the stop's scheduled arrival. Note that
                  Delivery Stop.estimated_arrival is filled by ERPNext's
                  route pass, which needs a Maps key — where it is blank the
                  stop cannot match any arrival filter and drops out.
        posting   the Delivery Note's own document date. A stop with no
                  delivery note drops out.

    Returns every stop on the site — the caller's role does not narrow it.
    Ordered newest trip first, then by stop sequence, so an unfiltered call
    opens on today's work.
    """
    try:
        _require_app_user()

        limit = min(cint(limit) or DEFAULT_LIMIT, MAX_LIMIT)
        offset = max(cint(offset), 0)

        filters = {
            "trip": trip, "status": status, "visited": visited,
            "delivery_note": delivery_note, "customer": customer,
            "driver": driver, "date": date, "date_field": date_field,
            # `from` is a Python keyword, so it arrives in **kwargs.
            "from": kwargs.get("from") or kwargs.get("from_date"),
            "to":   kwargs.get("to") or kwargs.get("to_date"),
        }
        try:
            where, params = _build_conditions(filters)
            applied_from, applied_to, applied_field = _date_window(filters)
        except ValueError as ve:
            return err("VALIDATION_ERROR", str(ve), 400)

        total = frappe.db.sql(
            f"""SELECT COUNT(*)
                  FROM `tabDelivery Stop` ds
                  JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
                  LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                 WHERE {where}""",
            params,
        )[0][0]

        available = _stop_field_names()
        stop_cols = [c for c in _STOP_COLUMNS if c in available]
        # Aliased to the response key, not the column name. Delivery Stop and
        # Delivery Note both carry `grand_total`, and selecting both under
        # that one name leaves whichever the driver returns last — the stop's
        # own total silently replacing the order's, or the reverse.
        stop_select = "".join(f", ds.`{c}` AS `{_STOP_COLUMNS[c]}`" for c in stop_cols)
        pay_cols = _payment_columns()
        pay_select = "".join(f", dn.`{c}` AS `{c}`" for c in pay_cols)

        rows = frappe.db.sql(
            f"""
            SELECT ds.name        AS stop,
                   ds.parent      AS trip,
                   ds.idx         AS stop_sequence,
                   dt.driver      AS driver,
                   dt.status      AS trip_status,
                   dt.departure_time,
                   dt.source_warehouse,
                   dn.customer_name,
                   dn.contact_mobile,
                   dn.set_warehouse,
                   dn.grand_total,
                   dn.rounded_total,
                   dn.cod_amount,
                   dn.cod_collected_amount,
                   IFNULL(dn.cowberry_delivery_status, 'Pending') AS delivery_status
                   {stop_select}
                   {pay_select}
              FROM `tabDelivery Stop` ds
              JOIN `tabDelivery Trip` dt ON dt.name = ds.parent
              LEFT JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE {where}
             ORDER BY dt.departure_time DESC, ds.parent DESC, ds.idx ASC
             LIMIT %(limit)s OFFSET %(offset)s
            """,
            {**params, "limit": limit, "offset": offset},
            as_dict=True,
        )

        # Warehouse and driver name are per trip and per driver, not per stop.
        # Resolved once and reused, so a page of stops on one trip costs one
        # lookup rather than a hundred.
        warehouse_cache, driver_cache = {}, {}
        stops = [_serialise(r, stop_cols, pay_cols, warehouse_cache, driver_cache)
                 for r in rows]

        return ok(data={
            "stops":       stops,
            "total_count": int(total),
            "limit":       limit,
            "offset":      offset,
            "has_more":    offset + len(stops) < int(total),
            "scope":       "all",
            # Echoed back so a caller can see which window actually ran —
            # `date` resolves to a from/to pair, a backwards range comes back
            # swapped, and `date_field` decides what the dates even mean.
            "date_filter": {
                "applied":    bool(applied_from),
                "from":       str(applied_from) if applied_from else None,
                "to":         str(applied_to) if applied_to else None,
                "date_field": applied_field,
            },
        })
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("GET_STOPS_FAILED", str(e), 500)


def _serialise(r, stop_cols, pay_cols, warehouse_cache, driver_cache):
    """One stop row in the shape the app consumes."""
    payment_type = _payment_type(r, pay_cols)

    warehouse_name = r.get("set_warehouse") or r.get("source_warehouse")
    if warehouse_name not in warehouse_cache:
        warehouse_cache[warehouse_name] = _warehouse_info(warehouse_name)

    out = {
        "stop":            r.stop,
        "trip":            r.trip,
        "trip_status":     r.trip_status,
        "trip_date":       str(getdate(r.departure_time)) if r.departure_time else None,
        "stop_sequence":   r.stop_sequence,
        "customer_name":   r.get("customer_name") or r.get("customer"),
        "contact_mobile":  r.get("contact_mobile"),
        "delivery_status": r.delivery_status,
        "order_stage":     _order_stage(r.delivery_status),
        "payment_type":    payment_type,
        # Whole-rupee cash the driver collects at this stop; 0 for prepaid.
        # `grand_total` stays the order's paise-exact value — see utils.cod.
        "cod_amount":      expected_cod(r) if payment_type == "COD" else 0.0,
        "grand_total":     flt(r.get("grand_total")),
        "warehouse":       warehouse_cache[warehouse_name],
    }

    for col in stop_cols:
        key = _STOP_COLUMNS[col]
        val = r.get(key)
        if key in ("latitude", "longitude"):
            val = flt(val) or None          # 0.0 is the Atlantic, not a stop
        elif key == "visited":
            val = 1 if cint(val) else 0
        elif key == "estimated_arrival" and val is not None:
            val = str(val)
        elif key in ("distance", "stop_grand_total"):
            val = flt(val)
        out[key] = val

    # Always present: the list spans every driver, so a row without one is
    # unreadable. Cached per driver rather than looked up per stop.
    if r.driver not in driver_cache:
        driver_cache[r.driver] = frappe.db.get_value(
            "Driver", r.driver, "full_name") if r.driver else None
    out["driver"] = r.driver
    out["driver_name"] = driver_cache[r.driver]

    return out


@frappe.whitelist(methods=["GET"])
def get_stop(stop=None):
    """One Delivery Stop by its row name.

    Reachable for any stop on the site, matching the list endpoint.
    """
    try:
        if not stop:
            return err("VALIDATION_ERROR", "Query param `stop` is required.", 400)
        _require_app_user()

        trip = frappe.db.get_value("Delivery Stop", stop, "parent")
        if not trip:
            return err("NOT_FOUND", f"Delivery Stop '{stop}' not found.", 404)

        result = get_stops(trip=trip, limit=MAX_LIMIT)
        if not result.get("success"):
            return result
        for row in result["data"]["stops"]:
            if row["stop"] == stop:
                return ok(data={"stop": row})
        return err("NOT_FOUND", f"Delivery Stop '{stop}' not found.", 404)
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("GET_STOP_FAILED", str(e), 500)
