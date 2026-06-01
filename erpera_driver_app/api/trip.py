import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.geo import haversine_m, validate_coords
from erpera_driver_app.utils.response import err, ok


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
