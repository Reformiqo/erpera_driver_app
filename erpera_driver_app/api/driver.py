import frappe

from erpera_driver_app.utils.exceptions import NotDriverError
from erpera_driver_app.utils.response import err, ok


def _require_driver():
    if not frappe.has_role("Driver"):
        raise NotDriverError()
    employee = frappe.db.get_value("Employee", {"user_id": frappe.session.user}, "name")
    if not employee:
        raise NotDriverError("No employee record linked to this user.")
    return employee


@frappe.whitelist()
def get_profile():
    try:
        employee = _require_driver()
        emp = frappe.get_doc("Employee", employee)
        user = frappe.get_doc("User", frappe.session.user)
        return ok(data={
            "employee_id": emp.name,
            "employee_name": emp.employee_name,
            "email": user.email,
            "mobile": emp.cell_number or user.mobile_no,
            "image": emp.image or user.user_image,
            "department": emp.department,
            "designation": emp.designation,
            # Driver-app extensions (custom fields on Employee, FRD §3.7).
            "default_warehouse": emp.get("default_warehouse"),
            "vehicle_assigned": emp.get("vehicle_assigned"),
            "daily_collection_limit": emp.get("daily_collection_limit"),
            "current_day_collected": emp.get("current_day_collected_amount"),
            "offline_zone_radius_km": emp.get("offline_zone_radius_km"),
            "fcm_device_token": emp.get("fcm_device_token"),
            "app_version": emp.get("app_version"),
        })
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("GET_PROFILE_FAILED", str(e))


@frappe.whitelist()
def update_profile(
    mobile=None,
    image=None,
    fcm_device_token=None,
    offline_zone_radius_km=None,
    app_version=None,
):
    """Update driver-configurable Employee fields.

    `mobile` + `image` are standard Employee fields. The remaining
    three live on Employee as custom fields (see FRD §3.7 / fixtures):

    - `fcm_device_token` — refreshed on every cold start so push
      notifications reach the live device; also used by the
      concurrent-login guard (BRD §5.3).
    - `offline_zone_radius_km` — per-driver override for the
      offline-tile download radius. Defaults to the warehouse
      setting if null.
    - `app_version` — last reported client build string; powers
      force-update banners on too-old installs.
    """
    try:
        employee = _require_driver()
        emp = frappe.get_doc("Employee", employee)
        if mobile:
            emp.cell_number = mobile
        if image:
            emp.image = image
        if fcm_device_token is not None:
            emp.fcm_device_token = fcm_device_token
        if offline_zone_radius_km is not None:
            emp.offline_zone_radius_km = int(offline_zone_radius_km)
        if app_version:
            emp.app_version = app_version
        emp.last_login_at = frappe.utils.now_datetime()
        emp.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message": "Profile updated."})
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("UPDATE_PROFILE_FAILED", str(e))
