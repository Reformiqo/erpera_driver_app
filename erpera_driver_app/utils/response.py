import frappe


def ok(data=None, **kwargs):
    payload = {"success": True, "data": data if data is not None else {}}
    payload.update(kwargs)
    return payload


def err(code, message, http_status=400, **kwargs):
    frappe.local.response["http_status_code"] = http_status
    payload = {"success": False, "error": {"code": code, "message": message}}
    payload.update(kwargs)
    return payload
