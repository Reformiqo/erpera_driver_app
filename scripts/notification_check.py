"""Find out why a push did not arrive.

Run inside a bench console. `open()` resolves against whatever directory you
started the console in, so load it through the app path rather than guessing:

    bench --site <site> console
    >>> import frappe, os
    >>> exec(open(os.path.join(frappe.get_app_path("erpera_driver_app"), "..",
    ...                        "scripts", "notification_check.py")).read())

    >>> diagnose("driver@reformiqo.com")   # every step, in order, with reasons
    >>> send("driver@reformiqo.com")       # send a real push and print FCM's reply

`send_test` only reports pass/fail. This prints the raw FCM response body, which
is the thing that actually names the problem.
"""

import json

import frappe


def diagnose(user=None):
    """Walk the push path and stop at the first thing that is wrong."""
    user = user or frappe.session.user
    print(f"\n=== push diagnosis for {user} ===\n")
    ok = True

    # 1 — the library
    try:
        import google.auth.transport.requests  # noqa: F401
        from google.oauth2 import service_account  # noqa: F401
        print("PASS  google-auth is installed")
    except ImportError as e:
        print(f"FAIL  google-auth is missing ({e})")
        print("      ./env/bin/pip install google-auth && bench restart")
        return

    # 2 — the credentials
    raw = frappe.db.get_single_value("Driver Notification Settings",
                                     "firebase_service_account_json")
    if not raw:
        print("FAIL  Driver Notification Settings > Service Account JSON is empty")
        return
    try:
        sa = json.loads(raw)
    except ValueError as e:
        print(f"FAIL  Service Account JSON does not parse: {e}")
        print("      Paste the whole downloaded key, including the outer braces.")
        return
    print(f"PASS  Service Account JSON parses ({len(raw)} chars)")

    missing = [k for k in ("type", "project_id", "private_key", "client_email")
               if not sa.get(k)]
    if missing:
        print(f"FAIL  the JSON is missing: {', '.join(missing)}")
        return
    print(f"PASS  project_id  = {sa['project_id']}")
    print(f"      client_email = {sa['client_email']}")

    # A private key that came through a form or an editor often loses its
    # newlines, and then the JWT signature is silently wrong.
    pk = sa.get("private_key", "")
    if "\n" not in pk:
        print("FAIL  private_key has no newlines — it was flattened somewhere.")
        print("      Re-paste the file exactly as downloaded.")
        ok = False
    elif not pk.startswith("-----BEGIN"):
        print("FAIL  private_key does not start with -----BEGIN")
        ok = False
    else:
        print("PASS  private_key looks intact")

    # 3 — can we actually authenticate to Google?
    from erpera_driver_app.utils import notifications as N
    frappe.local._erpera_fcm_token = None          # never trust a cached one here
    token = N._access_token()
    if not token:
        print("FAIL  could not mint an OAuth2 token — see the Error Log")
        return
    print(f"PASS  OAuth2 access token minted ({token[:18]}…)")

    # 4 — is there anywhere to send it?
    employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
    print(f"\n      employee  = {employee or '(none — no Employee for this user)'}")
    rows = frappe.get_all(
        "Driver FCM Token", filters={"user": user},
        fields=["name", "device_type", "device_id", "is_active", "fcm_token"])
    if not rows:
        print("FAIL  no Driver FCM Token rows — the device never called register_token")
        return
    for r in rows:
        state = "active" if r.is_active else "INACTIVE"
        print(f"      {r.name}  {r.device_type}  {state}  "
              f"token={r.fcm_token[:24]}…({len(r.fcm_token)} chars)")
    if not [r for r in rows if r.is_active]:
        print("FAIL  every token is inactive — FCM rejected them and they were "
              "deactivated. Register a fresh one from the app.")
        return
    print("PASS  at least one active device")

    print("\n" + ("Everything checks out. Run send() to post a real message."
                  if ok else "Fix the FAIL above, then run send()."))


def send(user=None, title="Diagnostic push", body="If you can read this, it works."):
    """POST one message to FCM and print the status and body verbatim."""
    import requests

    user = user or frappe.session.user
    from erpera_driver_app.utils import notifications as N

    frappe.local._erpera_fcm_token = None
    access_token = N._access_token()
    project_id = N._firebase_project_id()
    if not (access_token and project_id):
        print("Cannot send — run diagnose() first.")
        return

    rows = frappe.get_all("Driver FCM Token",
                          filters={"user": user, "is_active": 1},
                          fields=["name", "fcm_token"])
    if not rows:
        print(f"No active device for {user}.")
        return

    for row in rows:
        response = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            json={"message": {"token": row.fcm_token,
                              "notification": {"title": title, "body": body}}},
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
        print(f"\n{row.name}  ->  HTTP {response.status_code}")
        print(response.text)
        if response.status_code == 200:
            print("  Delivered. The phone should have buzzed.")
        elif "INVALID_ARGUMENT" in response.text:
            print("  The credentials are FINE — Google authenticated you and "
                  "rejected only the token. Register a real one from "
                  "FirebaseMessaging.instance.getToken().")
        elif "UNREGISTERED" in response.text or "NOT_FOUND" in response.text:
            print("  Real token, but the app was uninstalled or it was replaced.")
        elif response.status_code in (401, 403):
            print("  The service account is not authorised. Enable the Firebase "
                  "Cloud Messaging API on this project, and check the key was "
                  "issued for the same project.")


def recent(limit=10):
    """The last notifications, and whether each one actually went out."""
    rows = frappe.get_all(
        "Driver Notification",
        fields=["name", "title", "event_key", "employee", "is_pushed", "creation"],
        order_by="creation desc", limit=limit)
    for r in rows:
        print(f"{r.name}  pushed={r.is_pushed}  {r.event_key or '-':26} "
              f"{r.employee or '-':12} {r.title}")
    if rows and not any(r.is_pushed for r in rows):
        print("\nNothing has been pushed. Run diagnose().")


def errors(limit=5):
    """The FCM entries from the Error Log, newest first."""
    rows = frappe.get_all(
        "Error Log",
        filters={"method": ["like", "%FCM%"]},
        fields=["name", "method", "error", "creation"],
        order_by="creation desc", limit=limit)
    if not rows:
        print("No FCM errors logged. If nothing is arriving either, the push was "
              "never attempted — check for an active Driver FCM Token.")
    for r in rows:
        print(f"\n--- {r.creation}  {r.method}")
        print(r.error[:600])
