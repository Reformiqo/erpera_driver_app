import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.geo import haversine_m, validate_coords
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist()
def get_my_trips(date=None):
    try:
        employee = _require_driver()
        filters = {"driver": employee}
        if date:
            filters["date"] = date
        trips = frappe.get_all(
            "Delivery Trip",
            filters=filters,
            fields=["name", "date", "status", "total_distance", "driver"],
            order_by="date desc",
            limit=50,
        )
        return ok(data={"trips": trips})
    except Exception as e:
        return err("GET_TRIPS_FAILED", str(e))


@frappe.whitelist()
def start_trip(trip_id):
    try:
        _require_driver()
        trip = frappe.get_doc("Delivery Trip", trip_id)
        if trip.status != "Draft":
            return err("INVALID_STATUS", f"Cannot start a trip with status '{trip.status}'.")
        trip.status = "In Transit"
        trip.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"trip_id": trip.name, "status": trip.status})
    except Exception as e:
        return err("START_TRIP_FAILED", str(e))


@frappe.whitelist()
def complete_trip(trip_id):
    try:
        _require_driver()
        trip = frappe.get_doc("Delivery Trip", trip_id)
        if trip.status != "In Transit":
            return err("INVALID_STATUS", f"Cannot complete a trip with status '{trip.status}'.")
        trip.status = "Completed"
        trip.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"trip_id": trip.name, "status": trip.status})
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
        stops = frappe.get_all(
            "Delivery Stop",
            filters={"parent": trip_id},
            fields=["name", "delivery_note", "customer", "status"],
        )
        delivered = sum(1 for s in stops if s.get("status") == "Delivered")
        failed = sum(1 for s in stops if s.get("status") == "Failed")
        return ok(data={
            "trip_id": trip_id,
            "total_stops": len(stops),
            "delivered": delivered,
            "failed": failed,
            "status": trip.status,
        })
    except Exception as e:
        return err("GET_SUMMARY_FAILED", str(e))
