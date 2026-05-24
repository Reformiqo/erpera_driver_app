import frappe

from cowberry.api.driver import _require_driver
from cowberry.utils.exceptions import CollectionAlreadySubmittedError
from cowberry.utils.response import err, ok


@frappe.whitelist()
def get_collection(date=None):
    try:
        employee = _require_driver()
        filters = {"driver": employee, "docstatus": ["!=", 2]}
        if date:
            filters["collection_date"] = date
        collections = frappe.get_all(
            "Cowberry Driver Collection",
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
        col = frappe.get_doc("Cowberry Driver Collection", collection_id)
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
        UPDATE `tabCowberry Driver Collection`
        SET total_cash = 0, total_online = 0
        WHERE docstatus = 0
        AND collection_date < CURDATE()
    """)
    frappe.db.commit()
