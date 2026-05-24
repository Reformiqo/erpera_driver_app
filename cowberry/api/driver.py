import frappe

from cowberry.utils.exceptions import NotDriverError
from cowberry.utils.response import err, ok


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
        })
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("GET_PROFILE_FAILED", str(e))


@frappe.whitelist()
def update_profile(mobile=None, image=None):
    try:
        employee = _require_driver()
        emp = frappe.get_doc("Employee", employee)
        if mobile:
            emp.cell_number = mobile
        if image:
            emp.image = image
        emp.save(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"message": "Profile updated."})
    except NotDriverError as e:
        return e.to_response()
    except Exception as e:
        return err("UPDATE_PROFILE_FAILED", str(e))
