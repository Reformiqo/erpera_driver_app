import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.response import err, ok


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
            FROM `tabCowberry Cash Submission`
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
