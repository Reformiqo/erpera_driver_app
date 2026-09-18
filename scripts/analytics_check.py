"""Find out why the My Analytics screen is returning zeros.

Run inside a bench console:

    bench --site <site> console
    >>> import frappe, os
    >>> exec(open(os.path.join(frappe.get_app_path("erpera_driver_app"), "..",
    ...                        "scripts", "analytics_check.py")).read())

    >>> diagnose("driver@example.com")        # the whole chain, in order
    >>> diagnose("driver@example.com", period="week")
    >>> diagnose("driver@example.com", d_from="2026-07-01", d_to="2026-07-31")

`get_screen` returns zeros for four quite different reasons and the payload
cannot tell them apart on its own:

    1. the caller has no Driver record, so no trip is ever theirs
    2. the driver has trips, but none in the selected window
    3. the trips are there, but no Delivery Note says "Delivered"
    4. deliveries are there, but nothing records WHEN they happened

This walks the same joins `analytics.get_screen` walks and stops at whichever
link is actually broken, so you are not guessing between them.
"""

import frappe
from frappe.utils import add_days, getdate, today


def _window(period, d_from, d_to):
    if d_from and d_to:
        return getdate(d_from), getdate(d_to)
    t = getdate(today())
    if period == "today":
        return t, t
    if period == "week":
        return add_days(t, -6), t
    return add_days(t, -29), t


def _exists(fieldname):
    return fieldname in {f.fieldname for f in frappe.get_meta("Delivery Note").fields}


def diagnose(user, period="month", d_from=None, d_to=None):
    d_from, d_to = _window(period, d_from, d_to)
    span = (d_to - d_from).days
    p_from, p_to = add_days(d_from, -span - 1), add_days(d_from, -1)
    print(f"\nWindow      : {d_from} .. {d_to}")
    print(f"Previous    : {p_from} .. {p_to}")

    # ---- 1. identity -------------------------------------------------------
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    print(f"\n1. IDENTITY")
    print(f"   user      : {user}")
    print(f"   employee  : {employee or 'NONE  <-- _require_driver would fail'}")
    if not employee:
        return
    driver = frappe.db.get_value("Driver", {"employee": employee}, "name")
    print(f"   driver    : {driver or 'NONE  <-- every trip section returns empty'}")
    others = frappe.get_all("Driver", fields=["name", "employee", "full_name"])
    if not driver:
        print("   Driver records on this site:")
        for d in others:
            print(f"      {d.name}  employee={d.employee}  {d.full_name}")
        return

    # ---- 2. trips ----------------------------------------------------------
    def trip_count(a, b, drv=None):
        cond = "dt.driver = %(drv)s AND" if drv else ""
        return frappe.db.sql(f"""
            SELECT COUNT(*) FROM `tabDelivery Trip` dt
             WHERE {cond} (
                   (DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
                OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
        """, {"drv": drv, "a": a, "b": b})[0][0]

    mine = trip_count(d_from, d_to, driver)
    prev = trip_count(p_from, p_to, driver)
    fleet = trip_count(d_from, d_to)
    print(f"\n2. TRIPS")
    print(f"   yours, this window     : {mine}")
    print(f"   yours, previous window : {prev}")
    print(f"   whole site, this window: {fleet}")
    if mine == 0:
        print("   -> every trip-based section is empty because the window holds no trips.")
        rng = frappe.db.sql("""SELECT MIN(DATE(COALESCE(departure_time, creation))),
                                      MAX(DATE(COALESCE(departure_time, creation))),
                                      COUNT(*)
                                 FROM `tabDelivery Trip` WHERE driver = %s""", driver)[0]
        print(f"   your trips actually run {rng[0]} .. {rng[1]}  ({rng[2]} trips)")
        if fleet:
            top = frappe.db.sql("""
                SELECT dt.driver, COUNT(*) n FROM `tabDelivery Trip` dt
                 WHERE (DATE(dt.departure_time) BETWEEN %s AND %s)
                    OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %s AND %s)
                 GROUP BY dt.driver ORDER BY n DESC LIMIT 5""",
                (d_from, d_to, d_from, d_to), as_dict=True)
            print("   drivers who DO have trips in this window:")
            for r in top:
                emp = frappe.db.get_value("Driver", r.driver, "employee")
                usr = frappe.db.get_value("Employee", emp, "user_id") if emp else None
                print(f"      {r.driver}  {r.n} trips  employee={emp}  user={usr}")
        return

    # ---- 3. the joined row set --------------------------------------------
    rows = frappe.db.sql("""
        SELECT dn.name, dn.cowberry_delivery_status AS status,
               dn.cowberry_payment_method AS pay, dn.docstatus
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE dt.driver = %(drv)s
           AND ((DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
             OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
    """, {"drv": driver, "a": d_from, "b": d_to}, as_dict=True)
    print(f"\n3. STOPS JOINED TO DELIVERY NOTES: {len(rows)}")
    if not rows:
        print("   -> trips exist but carry no stops, or the stops point at deleted notes.")
        return
    counts = {}
    for r in rows:
        counts[r.status or "(empty)"] = counts.get(r.status or "(empty)", 0) + 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"   cowberry_delivery_status = {k!r}: {v}")
    delivered = counts.get("Delivered", 0)
    if not delivered:
        print("   -> deliveries_completed = 0, success_rate = 0, COD = 0, and every")
        print("      cash/timing section is empty, because no row says 'Delivered'.")
        print("      Check which field ops actually maintain on this site:")
        for f in ("delivery_status", "custom_order_status", "custom_pod_status",
                  "delhivery_status"):
            if _exists(f):
                vals = frappe.db.sql(f"""
                    SELECT dn.`{f}`, COUNT(*) FROM `tabDelivery Trip` dt
                      JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
                      JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                     WHERE dt.driver = %(drv)s
                       AND ((DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
                         OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
                     GROUP BY 1""", {"drv": driver, "a": d_from, "b": d_to})
                print(f"      {f}: {dict(vals)}")
        return

    # ---- 4. timestamps -----------------------------------------------------
    print(f"\n4. TIMESTAMPS behind the timing sections ({delivered} delivered)")
    for f in ("custom_delivered_timestamp", "actual_arrival_time",
              "pod_timestamp", "custom_delivered_on"):
        if not _exists(f):
            print(f"   {f:32} field not on this site")
            continue
        n = frappe.db.sql(f"""
            SELECT COUNT(*) FROM `tabDelivery Trip` dt
              JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
              JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
             WHERE dt.driver = %(drv)s AND dn.`{f}` IS NOT NULL
               AND ((DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
                 OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
        """, {"drv": driver, "a": d_from, "b": d_to})[0][0]
        print(f"   {f:32} populated on {n}")
    eta = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
         WHERE dt.driver = %(drv)s AND ds.estimated_arrival IS NOT NULL
           AND ((DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
             OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
    """, {"drv": driver, "a": d_from, "b": d_to})[0][0]
    print(f"   {'Delivery Stop.estimated_arrival':32} populated on {eta}")
    print("   (0 planned arrivals is normal - Estimate Missing Arrival Times covers it)")

    # ---- 5. money ----------------------------------------------------------
    money = frappe.db.sql("""
        SELECT COUNT(*) n,
               SUM(CASE WHEN dn.rounded_total > 0 THEN 1 ELSE 0 END) with_rounded,
               SUM(CASE WHEN dn.rounding_adjustment <> 0 THEN 1 ELSE 0 END) with_adj
          FROM `tabDelivery Trip` dt
          JOIN `tabDelivery Stop` ds ON ds.parent = dt.name
          JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
         WHERE dt.driver = %(drv)s AND dn.cowberry_delivery_status = 'Delivered'
           AND ((DATE(dt.departure_time) BETWEEN %(a)s AND %(b)s)
             OR (dt.departure_time IS NULL AND DATE(dt.creation) BETWEEN %(a)s AND %(b)s))
    """, {"drv": driver, "a": d_from, "b": d_to}, as_dict=True)[0]
    print(f"\n5. CASH ROUNDING inputs")
    print(f"   delivered orders            : {money.n}")
    print(f"   with a rounded_total set    : {money.with_rounded}")
    print(f"   with a rounding_adjustment  : {money.with_adj}")
    print("   (both 0 is fine - the gap is then worked out from grand_total)")
    print("\nDone.")
