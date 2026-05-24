import frappe


def send_push(employee, title, body, payload=None):
    """Send a push notification to an employee's device(s)."""
    # Look up device tokens linked to the employee
    tokens = frappe.get_all(
        "Cowberry Driver Settings",
        filters={},
        fields=["fcm_server_key"],
        limit=1,
    )
    if not tokens:
        return

    fcm_key = tokens[0].get("fcm_server_key") if tokens else None
    if not fcm_key:
        return

    driver_tokens = frappe.get_all(
        "Cowberry Driver Collection",
        filters={"driver": employee, "docstatus": ["!=", 2]},
        fields=["device_token"],
        limit=1,
    )

    device_token = None
    for row in driver_tokens:
        if row.get("device_token"):
            device_token = row["device_token"]
            break

    if not device_token:
        return

    import requests

    message = {
        "to": device_token,
        "notification": {"title": title, "body": body},
    }
    if payload:
        message["data"] = payload

    try:
        requests.post(
            "https://fcm.googleapis.com/fcm/send",
            json=message,
            headers={"Authorization": f"key={fcm_key}", "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass
