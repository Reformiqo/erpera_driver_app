import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import CollectionAlreadySubmittedError
from erpera_driver_app.utils.response import err, ok

MAX_SUMMARY_DAYS = 31


# ===========================================================================
# CD2-I5 Point 7 — Unified Collection + Submit Cash screen API
# ===========================================================================

@frappe.whitelist(methods=["GET"])
def get_screen(date=None):
    """Single endpoint powering BOTH the Driver Collection screen
    (Screenshot 2/3) and the Submit Cash to Warehouse screen (Screenshot 4).

    Hardik's CD2-I5 point 7 — Flutter wants one API call to fetch
    everything the two screens render so it doesn't have to stitch
    together collection.get_today + cash_submission.history + driver
    metadata across three round-trips.

    Query:
        date — YYYY-MM-DD (optional; defaults to today)

    Response shape (all amounts in INR):
    ```
    {
        "date": "2026-06-10",
        "driver": { "name", "employee_name", "warehouse" {name, code} },
        "running_cash_total": <int>,
        "daily_limit": <int>,
        "limit_percentage": <float>,
        "limit_remaining": <int>,
        "totals": {
            "total_expected": <int>,
            "prepaid_settled": <int>,
            "cod_collected":   <int>,
            "cod_pending":     <int>
        },
        "order_breakdown": [
            { "customer", "delivery_note", "payment_type", "amount",
              "status" }   # status: "Settled" / "Pending"
        ],
        "submission": {                # populated only when ready to submit
            "available_to_submit": <int>,
            "cod_orders_completed": <int>,
            "payment_method_options": ["Physical Cash", "Online-UPI"],
            "wm_otp": {
                "required": true,
                "validity_minutes": 5,
                "sent_to_email": "<masked>",
                "status": "Not Requested" | "Sent" | "Validated"
            }
        }
    }
    ```
    """
    try:
        employee = _require_driver()
        target = date or today()
        emp = frappe.get_doc("Employee", employee)

        # ─── Driver block ──────────────────────────────────────────────
        from erpera_driver_app.api.trip import _warehouse_info
        default_wh = emp.get("default_warehouse")
        driver_block = {
            "name":          emp.name,
            "employee_name": emp.employee_name,
            "warehouse":     _warehouse_info(default_wh) if default_wh else None,
        }

        # ─── Running totals + daily limit ─────────────────────────────
        daily_limit = flt(emp.get("daily_collection_limit") or 0)
        # current_day_collected resets daily; rely on the field but rollover
        # via current_day_collected_date being == today
        last_date = emp.get("current_day_collected_date")
        running_cash = flt(emp.get("current_day_collected_amount") or 0)
        if str(last_date) != str(target):
            running_cash = 0
        limit_pct = round((running_cash / daily_limit) * 100, 1) if daily_limit else 0
        limit_remaining = max(daily_limit - running_cash, 0)

        # ─── Today's Driver Collection ────────────────────────────────
        col_name = frappe.db.get_value(
            "Driver Collection",
            {"driver": employee, "collection_date": target},
            "name",
        )

        order_breakdown = []
        prepaid_settled = cod_collected = cod_pending = 0
        cod_orders_completed = 0

        if col_name:
            col = frappe.get_doc("Driver Collection", col_name)
            for row in (col.get("order_breakdown") or []):
                amt = flt(row.get("total_amount") or 0)
                cash_amt = flt(row.get("cash_amount") or 0)
                online_amt = flt(row.get("online_amount") or 0)
                ptype = (row.get("payment_method") or "Prepaid")
                # Treat Cash/COD-* as COD, anything else as Prepaid
                is_cod = ptype.upper().startswith("CASH") or "COD" in ptype.upper()
                status = "Settled" if (cash_amt + online_amt) >= amt and amt > 0 else "Pending"
                order_breakdown.append({
                    "customer":     row.get("customer_name") or row.get("customer"),
                    "delivery_note": row.get("delivery_note"),
                    "payment_type": "COD" if is_cod else "Prepaid",
                    "amount":       amt,
                    "status":       status,
                })
                if is_cod:
                    if status == "Settled":
                        cod_collected += amt
                        cod_orders_completed += 1
                    else:
                        cod_pending += amt
                else:
                    if status == "Settled":
                        prepaid_settled += amt
        # If no Driver Collection exists yet, fall back to today's
        # delivered DNs assigned to this driver's trips so the screen
        # doesn't render empty for the in-progress case.
        else:
            from erpera_driver_app.api.trip import _driver_record, _resolve_payment_type
            drv = _driver_record(employee)
            if drv:
                rows = frappe.db.sql(
                    """SELECT dn.name AS delivery_note,
                              IFNULL(dn.customer_name, dn.customer) AS customer,
                              dn.grand_total,
                              IFNULL(dn.cowberry_delivery_status,'Pending') AS dstatus
                         FROM `tabDelivery Stop` ds
                         JOIN `tabDelivery Note` dn ON dn.name = ds.delivery_note
                         JOIN `tabDelivery Trip`  dt ON dt.name = ds.parent
                        WHERE dt.driver = %s AND DATE(dt.departure_time) = %s""",
                    (drv, target), as_dict=True,
                )
                for r in rows:
                    ptype = _resolve_payment_type(r.delivery_note)
                    amt = flt(r.grand_total)
                    settled = r.dstatus == "Delivered"
                    order_breakdown.append({
                        "customer":     r.customer,
                        "delivery_note": r.delivery_note,
                        "payment_type": ptype,
                        "amount":       amt,
                        "status":       "Settled" if settled else "Pending",
                    })
                    if ptype == "COD":
                        if settled:
                            cod_collected += amt
                            cod_orders_completed += 1
                        else:
                            cod_pending += amt
                    else:
                        if settled:
                            prepaid_settled += amt

        total_expected = prepaid_settled + cod_collected + cod_pending

        # ─── Submission block (Submit Cash screen — Screenshot 4) ──────
        # Only populated when there's actually something to submit. Real
        # WM OTP wiring is in api/cash_submission.py; here we surface a
        # state hint so the screen can render the right CTA.
        available_to_submit = cod_collected   # what's physically in hand
        existing_sub = frappe.db.get_value(
            "Cash Submission",
            {"driver": employee, "docstatus": 0,
             "status": ("in", ["Pending OTP", "Draft"])},
            ["name", "status"], as_dict=True,
        )
        otp_status = "Sent" if existing_sub and existing_sub.status == "Pending OTP" else "Not Requested"

        # Warehouse manager email/mobile — masked for display.
        wm_email = None
        if default_wh:
            wm_email = frappe.db.get_value("Warehouse", default_wh,
                                          "warehouse_manager_email")
        masked_wm = _mask_email(wm_email) if wm_email else None

        submission = {
            "available_to_submit":     available_to_submit,
            "cod_orders_completed":    cod_orders_completed,
            "payment_method_options":  ["Physical Cash", "Online-UPI"],
            "active_submission":       existing_sub.name if existing_sub else None,
            "wm_otp": {
                "required":         True,
                "validity_minutes": 5,
                "sent_to":          masked_wm,
                "status":           otp_status,
            },
        }

        return ok(data={
            "date":                target,
            "driver":              driver_block,
            "running_cash_total":  running_cash,
            "daily_limit":         daily_limit,
            "limit_percentage":    limit_pct,
            "limit_remaining":     limit_remaining,
            "totals": {
                "total_expected":  total_expected,
                "prepaid_settled": prepaid_settled,
                "cod_collected":   cod_collected,
                "cod_pending":     cod_pending,
            },
            "order_breakdown":     order_breakdown,
            "submission":          submission,
        })
    except Exception as e:
        return err("GET_COLLECTION_SCREEN_FAILED", str(e))


def _mask_email(email):
    """Render `warehouse.surat@cowberry.in` → `wa****se@cowberry.in`."""
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 4:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}****{local[-2:]}@{domain}"


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Cash Collection §§1-2)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def get_today():
    """§1 Today's collection — driver's open row plus its order breakdown
    rolled up into the spec shape. Also computes daily limit metrics
    used by the home-screen gauge.
    """
    try:
        employee = _require_driver()
        col_name = frappe.db.get_value(
            "Driver Collection",
            {"driver": employee, "collection_date": today()},
            "name",
        )
        # When the driver hasn't started today yet there's no row — we
        # still return the shape, with zeros, so the home screen can
        # render without branching on null.
        if not col_name:
            emp = frappe.get_doc("Employee", employee)
            daily_limit = flt(emp.get("daily_collection_limit") or 0)
            return ok(data={"collection": {
                "name":             None,
                "total_expected":   0,
                "total_collected":  0,
                "total_pending":    0,
                "daily_limit":      daily_limit,
                "limit_reached":    0,
                "limit_percentage": 0,
                "order_breakdown":  [],
            }})

        col = frappe.get_doc("Driver Collection", col_name)
        breakdown = []
        total_expected = 0.0
        total_collected = 0.0
        for row in col.get("order_breakdown") or []:
            cod_amt = flt(row.get("total_amount") or 0)
            collected_amt = flt(row.get("cash_amount") or 0) + flt(row.get("online_amount") or 0)
            total_expected += cod_amt
            total_collected += collected_amt
            breakdown.append({
                "delivery_note":    row.get("delivery_note"),
                "customer":         row.get("customer_name") or row.get("customer"),
                "cod_amount":       cod_amt,
                "collected_amount": collected_amt,
                "status":           row.get("status") or "Pending",
            })

        emp = frappe.get_doc("Employee", employee)
        daily_limit = flt(emp.get("daily_collection_limit") or 0)
        current_collected = flt(emp.get("current_day_collected_amount") or 0)
        limit_pct = (current_collected / daily_limit * 100) if daily_limit else 0
        return ok(data={"collection": {
            "name":             col.name,
            "total_expected":   total_expected,
            "total_collected":  total_collected,
            "total_pending":    max(total_expected - total_collected, 0),
            "daily_limit":      daily_limit,
            "limit_reached":    1 if daily_limit and current_collected >= daily_limit else 0,
            "limit_percentage": round(limit_pct, 2),
            "order_breakdown":  breakdown,
        }})
    except Exception as e:
        return err("GET_TODAY_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def get_summary(**kwargs):
    """§2 Date-range aggregate. Spec says max 31 days; rejects beyond.

    Accepts `from`/`to` (spec). `from` is a Python keyword so we read
    via frappe.form_dict to keep the param name clean on the wire.
    """
    try:
        employee = _require_driver()
        form = frappe.local.form_dict
        from_date = form.get("from") or kwargs.get("from_date")
        to_date = form.get("to") or kwargs.get("to_date") or today()
        if not from_date:
            return err("VALIDATION_ERROR", "Query param `from` is required.", 400)
        d_from = getdate(from_date)
        d_to = getdate(to_date)
        if d_to < d_from:
            return err("VALIDATION_ERROR", "`to` must be on or after `from`.", 400)
        days = (d_to - d_from).days + 1
        if days > MAX_SUMMARY_DAYS:
            return err("VALIDATION_ERROR",
                       f"Date range exceeds {MAX_SUMMARY_DAYS}-day cap "
                       f"(requested {days} days).", 400)

        # Pull per-day rollup from Driver Collection in one shot.
        rows = frappe.db.sql(
            """
            SELECT collection_date AS date,
                   IFNULL(SUM(IFNULL(total_cash,0) + IFNULL(total_online,0)), 0) AS collected,
                   (SELECT IFNULL(SUM(cs.amount), 0)
                      FROM `tabCash Submission` cs
                     WHERE cs.collection IN (
                         SELECT name FROM `tabDriver Collection` dc2
                          WHERE dc2.driver = %(emp)s
                            AND dc2.collection_date = dc.collection_date
                     )) AS submitted
              FROM `tabDriver Collection` dc
             WHERE dc.driver = %(emp)s
               AND dc.collection_date BETWEEN %(d_from)s AND %(d_to)s
             GROUP BY dc.collection_date
             ORDER BY dc.collection_date
            """,
            {"emp": employee, "d_from": d_from, "d_to": d_to},
            as_dict=True,
        )
        days_list = []
        total_collected = 0.0
        total_submitted = 0.0
        for r in rows:
            days_list.append({
                "date":      str(r["date"]),
                "collected": flt(r["collected"]),
                "submitted": flt(r["submitted"]),
            })
            total_collected += flt(r["collected"])
            total_submitted += flt(r["submitted"])
        return ok(data={"summary": {
            "total_collected":    total_collected,
            "total_submitted":    total_submitted,
            "pending_submission": max(total_collected - total_submitted, 0),
            "days":               days_list,
        }})
    except Exception as e:
        return err("GET_SUMMARY_FAILED", str(e))


# ---------------------------------------------------------------------------
# Legacy endpoints (back-compat — same module so existing clients keep working)
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_collection(date=None):
    try:
        employee = _require_driver()
        filters = {"driver": employee, "docstatus": ["!=", 2]}
        if date:
            filters["collection_date"] = date
        collections = frappe.get_all(
            "Driver Collection",
            filters=filters,
            fields=["name", "collection_date", "total_cash", "total_online", "docstatus", "status"],
            order_by="collection_date desc",
            limit=20,
        )
        return ok(data={"collections": collections})
    except Exception as e:
        return err("GET_COLLECTION_FAILED", str(e))


@frappe.whitelist()
def submit_cash(collection_id, amount, notes=None):
    try:
        employee = _require_driver()
        col = frappe.get_doc("Driver Collection", collection_id)
        if col.driver != employee:
            return err("ACCESS_DENIED", "This collection does not belong to you.", 403)
        if col.docstatus == 1:
            raise CollectionAlreadySubmittedError()

        col.total_cash = amount
        if notes:
            col.notes = notes
        col.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"collection_id": col.name, "total_cash": col.total_cash})
    except CollectionAlreadySubmittedError as e:
        return e.to_response()
    except Exception as e:
        return err("SUBMIT_CASH_FAILED", str(e))


def daily_reset_driver_totals():
    """Scheduler: reset daily collection totals at midnight."""
    frappe.db.sql("""
        UPDATE `tabDriver Collection`
        SET total_cash = 0, total_online = 0
        WHERE docstatus = 0
        AND collection_date < CURDATE()
    """)
    frappe.db.commit()
