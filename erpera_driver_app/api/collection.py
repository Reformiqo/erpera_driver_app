import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import CollectionAlreadySubmittedError
from erpera_driver_app.utils.response import err, ok

MAX_SUMMARY_DAYS = 31


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
