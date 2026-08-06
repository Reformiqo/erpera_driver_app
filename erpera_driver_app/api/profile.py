"""Driver Profile API — spec layer (Nainsi's xlsx §Profile §§1-2).

These endpoints are the spec-named surface. The legacy `driver.get_profile`
and `driver.update_profile` remain available for back-compat.
"""
import frappe
from frappe.utils import flt

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import NotDriverError
from erpera_driver_app.utils.response import err, ok


def _coord(value):
    """Coerce a centre coordinate to a float, or None.

    center_lat/center_lng are Data fields — they hold more decimal places
    than a Float would keep — so the column accepts any string. Anything
    unparseable becomes None rather than raising: one badly typed zone should
    not take the whole profile call down with GET_PROFILE_FAILED.

    0.0 also becomes None. It's a real point in the Atlantic, and a map
    client that trusted it would drop a pin there. Same convention as
    trip._warehouse_info.
    """
    try:
        return float(value) or None
    except (TypeError, ValueError):
        return None


def _offline_map_zones(emp):
    """Flatten Employee.custom_offline_map_zone into plain dicts.

    The child doctype calls its label field `name1` — `name` is reserved on
    every Frappe doc — so expose it as `name`, which is what it means.

    Returns [] when the custom field isn't installed on this bench.
    """
    zones = []
    for z in (emp.get("custom_offline_map_zone") or []):
        zones.append({
            "name":             z.get("name1"),
            "center_lat":       _coord(z.get("center_lat")),
            "center_lng":       _coord(z.get("center_lng")),
            "radius_km":        flt(z.get("radius_km")),
            "detail_radius_km": flt(z.get("detail_radius_km")),
        })
    return zones


@frappe.whitelist(methods=["GET"])
def get():
    """Profile §1 — return the authenticated driver's flat profile block.

    Shape matches the spec exactly: employee, employee_name, cell_number,
    designation, default_warehouse, vehicle_assigned, daily_collection_limit,
    current_day_collected, app_version, offline_zone_radius_km — plus
    offline_map_zone, the driver's assigned map zones (additive; every spec
    key keeps its existing name and meaning).
    """
    try:
        emp_name = _require_driver()
        emp = frappe.get_doc("Employee", emp_name)
        return ok(data={
            "employee":               emp.name,
            "employee_name":          emp.employee_name,
            "cell_number":            emp.cell_number,
            "designation":            emp.designation,
            "default_warehouse":      emp.get("default_warehouse"),
            "vehicle_assigned":       emp.get("vehicle_assigned"),
            "daily_collection_limit": emp.get("daily_collection_limit"),
            "current_day_collected":  emp.get("current_day_collected_amount"),
            "app_version":            emp.get("app_version"),
            "offline_zone_radius_km": emp.get("offline_zone_radius_km"),
            "offline_map_zone":       _offline_map_zones(emp),
        })
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("GET_PROFILE_FAILED", str(e), 500)


@frappe.whitelist(methods=["PUT", "POST"])
def update_settings(fcm_device_token=None, offline_zone_radius_km=None,
                    app_version=None):
    """Profile §2 — update driver-configurable settings.

    Accepts PUT (spec) and POST (for HTTP-client toolchains that don't
    surface PUT cleanly). Only the three fields below are accepted; any
    other field on the body is silently ignored to keep the surface
    tight.

    Validation: offline_zone_radius_km must be a positive integer.
    Returns: {"updated": true}.
    """
    try:
        emp_name = _require_driver()
        if offline_zone_radius_km is not None:
            try:
                radius = int(offline_zone_radius_km)
            except (TypeError, ValueError):
                return err("VALIDATION_ERROR",
                           "offline_zone_radius_km must be a positive integer.", 400)
            if radius <= 0:
                return err("VALIDATION_ERROR",
                           "offline_zone_radius_km must be a positive integer.", 400)
        else:
            radius = None
        emp = frappe.get_doc("Employee", emp_name)
        if fcm_device_token is not None:
            emp.fcm_device_token = fcm_device_token
        if radius is not None:
            emp.offline_zone_radius_km = radius
        if app_version:
            emp.app_version = app_version
        emp.flags.ignore_permissions = True
        emp.save()
        frappe.db.commit()
        return ok(data={"updated": True})
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("UPDATE_PROFILE_FAILED", str(e), 500)
