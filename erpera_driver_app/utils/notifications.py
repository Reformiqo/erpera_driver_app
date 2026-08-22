"""Driver push notifications — FCM HTTP v1.

One entry point, `notify()`. Everything else here supports it.

    notify(employee, "New trip assigned",
           "Trip MAT-DT-2026-00040 with 6 stops.",
           event_key="trip_assigned",
           reference_doctype="Delivery Trip", reference_name=trip)

It writes a Driver Notification row (the in-app inbox the app reads through
`api.notification.get_notifications`) and queues a push to every device the
driver has registered.

Three rules this module keeps, because it is called from inside doc events and
submit handlers:

1. **A notification never breaks the transaction that triggered it.** Every
   public function swallows its own exceptions and logs. Nothing here calls
   `frappe.db.rollback()` — a rollback inside an `on_submit` would take the
   caller's Delivery Note down with it.
2. **No HTTP inside the request.** The FCM call is enqueued. A slow or
   unreachable Google costs a background worker, not the driver's app waiting
   on a submit.
3. **The checkbox is authoritative.** Ticked sends, unticked does not. The one
   exception is a Single nobody has saved yet, which reads back as `None`
   rather than as its defaults — see `_toggle`.
"""
import json

import frappe
from frappe.utils import cint, now_datetime

# Notification config lives in its own Single rather than on Driver Settings,
# which already carries OTP, signup, Razorpay and delivery config.
SETTINGS = "Driver Notification Settings"

# Scope for the OAuth2 token FCM v1 requires. The legacy `Authorization: key=`
# server key was decommissioned by Google in June 2024.
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _log(message, title="Driver Notification"):
    """Informational logging that does not pollute the Error Log.

    Successes go to the site log; only genuine failures get `log_error`.
    """
    frappe.logger("erpera_driver_app").info(f"{title}: {message}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _toggle(fieldname):
    """Read one checkbox from the settings Single.

    The saved value is authoritative: unchecked means off, full stop. The only
    special case is a Single nobody has opened yet — Frappe writes no row to
    `tabSingles` until the first save, so the read comes back `None` rather
    than the checkbox's default. Falling back to the DocField default there
    makes a fresh install behave as the form shows it (everything ticked),
    without ever overriding a choice someone actually made.
    """
    value = frappe.db.get_single_value(SETTINGS, fieldname)
    if value is not None:
        return bool(cint(value))
    field = frappe.get_meta(SETTINGS).get_field(fieldname)
    return bool(cint(field.default)) if field and field.default is not None else True


def is_event_enabled(event_key):
    """Whether `event_key` should produce a notification right now.

    The master switch wins; then the per-event `notify_<event_key>` box.
    """
    if not _toggle("enable_notifications"):
        return False
    if not event_key:
        return True
    return _toggle(f"notify_{event_key}")


def _firebase_project_id():
    """The Firebase project to send through, read out of the service account.

    There is no separate setting for this. The service-account JSON already
    carries `project_id`, and a second field holding the same string is just a
    second place to typo it — or to leave stale after switching projects.
    """
    raw = frappe.db.get_single_value(SETTINGS, "firebase_service_account_json")
    if not raw:
        return None
    try:
        return json.loads(raw).get("project_id")
    except ValueError:
        frappe.log_error("Service Account JSON is not valid JSON.", "FCM Config Error")
        return None


def _access_token():
    """Mint an OAuth2 bearer token from the service account in the settings.

    Cached on `frappe.local` for the life of the request/job: FCM tokens are
    valid for about an hour, and minting one is a round trip to Google. Without
    the cache a broadcast to N devices would make N of them.
    """
    cached = getattr(frappe.local, "_erpera_fcm_token", None)
    if cached:
        return cached

    raw = frappe.db.get_single_value(SETTINGS, "firebase_service_account_json")
    if not raw:
        frappe.log_error(
            "Service Account JSON is empty in Driver Notification Settings — "
            "no push can be sent.",
            "FCM Config Error")
        return None

    try:
        import google.auth.transport.requests
        from google.oauth2 import service_account
    except ImportError:
        frappe.log_error(
            "google-auth is not installed. Run: ./env/bin/pip install google-auth",
            "FCM Config Error")
        return None

    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=[FCM_SCOPE])
        creds.refresh(google.auth.transport.requests.Request())
    except Exception as e:
        frappe.log_error(f"Could not mint an FCM access token: {e}", "FCM Config Error")
        return None

    frappe.local._erpera_fcm_token = creds.token
    return creds.token


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------

def _user_for_employee(employee):
    return frappe.db.get_value("Employee", employee, "user_id")


def active_tokens(user):
    """Every live device registered to this user."""
    if not user:
        return []
    return frappe.get_all(
        "Driver FCM Token",
        filters={"user": user, "is_active": 1},
        fields=["name", "fcm_token", "device_type"],
    )


def register_device(user, employee=None, fcm_token=None, device_type=None,
                    device_id=None):
    """Record this device's FCM token. Returns the row name, or None.

    Both `auth.driver_login` and `api.notification.register_token` come through
    here, so a device registered at login and one registered explicitly land in
    the same row rather than two.

    Keyed on `device_id` when the app sends one — Firebase rotates tokens on its
    own schedule, and without a stable key each rotation would leave the old
    token behind as a row we keep pushing at forever. Re-registering also
    reactivates a row FCM previously rejected, which is what makes a reinstall
    start working again.

    Never raises: it is called from the login path, and a notification detail
    must not be able to stop a driver signing in.
    """
    try:
        if not (user and fcm_token):
            return None
        if device_type not in ("Android", "iOS"):
            device_type = None

        match = {"user": user, "device_id": device_id} if device_id \
            else {"user": user, "fcm_token": fcm_token}
        existing = frappe.db.get_value("Driver FCM Token", match, "name")

        values = {
            "fcm_token": fcm_token,
            "employee":  employee,
            "is_active": 1,
            "last_used": now_datetime(),
        }
        if device_type:
            values["device_type"] = device_type

        if existing:
            frappe.db.set_value("Driver FCM Token", existing, values)
            return existing

        # Merged positionally, not as **values: `values` may already carry
        # device_type, and passing it both ways raises TypeError — which the
        # except below would swallow, leaving login-time registration silently
        # doing nothing at all.
        values.setdefault("device_type", "Android")
        doc = frappe.get_doc(dict(
            values, doctype="Driver FCM Token", user=user, device_id=device_id))
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         f"Could not register device for {user}")
        return None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _send_one(fcm_token, title, body, data, access_token, project_id):
    """POST a single message. Returns True when FCM accepted it.

    Raises nothing — a dead token deactivates itself and everything else is
    logged, because this runs in a background job with no one to catch it.
    """
    import requests

    message = {
        "message": {
            "token": fcm_token,
            "notification": {"title": title, "body": body},
            # FCM rejects non-string data values outright.
            "data": {k: str(v) for k, v in (data or {}).items()},
            "android": {
                "priority": "high",
                "notification": {"sound": "default",
                                 "channel_id": "high_importance_channel"},
            },
            "apns": {"payload": {"aps": {"sound": "default", "badge": 1}}},
        }
    }
    try:
        response = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            json=message,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        frappe.log_error(f"FCM request failed: {e}", "FCM Push Error")
        return False

    if response.status_code == 200:
        return True

    text = response.text or ""
    frappe.log_error(f"FCM {response.status_code}: {text}", "FCM Push Error")
    # The app was uninstalled or the token was replaced. Stop trying it.
    if "UNREGISTERED" in text or "NOT_FOUND" in text or "INVALID_ARGUMENT" in text:
        frappe.db.set_value(
            "Driver FCM Token", {"fcm_token": fcm_token}, "is_active", 0,
            update_modified=False)
    return False


def push_now(notification_name):
    """Deliver the push for one Driver Notification. Runs in a worker.

    Enqueued by `notify()`; safe to call by hand from a console to retry.
    """
    try:
        notif = frappe.db.get_value(
            "Driver Notification", notification_name,
            ["name", "title", "message", "user", "payload"], as_dict=True)
        if not notif:
            return

        tokens = active_tokens(notif.user)
        if not tokens:
            _log(f"{notif.name}: {notif.user or 'no user'} has no registered device.")
            return

        access_token = _access_token()
        project_id = _firebase_project_id()
        if not access_token or not project_id:
            # _access_token already logged; name the missing project id too.
            if not project_id:
                frappe.log_error(
                    "No `project_id` in the Service Account JSON in Driver "
                    "Notification Settings — paste the whole downloaded key, "
                    "not a fragment.",
                    "FCM Config Error")
            return

        try:
            data = json.loads(notif.payload) if notif.payload else {}
        except ValueError:
            data = {}

        delivered = 0
        for row in tokens:
            if _send_one(row.fcm_token, notif.title, notif.message, data,
                         access_token, project_id):
                delivered += 1
                frappe.db.set_value("Driver FCM Token", row.name, "last_used",
                                    now_datetime(), update_modified=False)

        # is_pushed means "FCM took it for at least one device" — not merely
        # "we tried". A row left at 0 is a real delivery failure worth seeing.
        if delivered:
            frappe.db.set_value("Driver Notification", notif.name, {
                "is_pushed": 1,
                "push_sent_at": now_datetime(),
            }, update_modified=False)
        _log(f"{notif.name}: delivered to {delivered}/{len(tokens)} device(s).")
    except Exception:
        frappe.log_error(frappe.get_traceback(),
                         f"Push failed for {notification_name}")


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------

def notify(employee, title, body, event_key=None, reference_doctype=None,
           reference_name=None, action_type="none", data=None, push=True):
    """Record and send one driver notification.

    Returns the Driver Notification name, or None when the event was disabled
    or something went wrong. Callers are not expected to check: this never
    raises, so it can sit inside a doc event or a submit handler untouched by
    a try/except of its own.
    """
    try:
        if not employee:
            return None
        if not is_event_enabled(event_key):
            _log(f"'{title}' skipped — event '{event_key}' is off in the settings.")
            return None

        user = _user_for_employee(employee)
        payload = dict(data or {})
        payload.setdefault("event", event_key or "")
        if reference_doctype:
            payload.setdefault("reference_doctype", reference_doctype)
        if reference_name:
            payload.setdefault("reference_name", reference_name)

        notif = frappe.get_doc({
            "doctype":           "Driver Notification",
            "title":             title,
            "message":           body,
            "event_key":         event_key,
            "employee":          employee,
            "user":              user,
            "reference_doctype": reference_doctype,
            "reference_name":    reference_name,
            "action_type":       action_type or "none",
            "payload":           json.dumps(payload),
            "is_read":           0,
            "is_pushed":         0,
        })
        notif.insert(ignore_permissions=True)

        if push and user:
            # Enqueued rather than sent inline: the caller is usually mid-submit
            # and must not wait on Google. `enqueue_after_commit` keeps the
            # worker from reading a row the caller has not committed yet.
            frappe.enqueue(
                "erpera_driver_app.utils.notifications.push_now",
                queue="short",
                notification_name=notif.name,
                enqueue_after_commit=True,
            )
        return notif.name
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"Notification failed: {title} (event={event_key})")
        return None


WARNING_THRESHOLD = 0.8


def notify_collection_limit(employee, collected, limit):
    """Events 5 and 6 — the driver's daily cash ceiling.

    Called after each COD delivery has been added to the running total. Sends
    at most one warning and one limit-reached notification per driver per day:
    without that, every delivery past 80% would fire another push, and the one
    that matters — hitting 100% — would arrive as the fifth identical buzz.
    """
    try:
        limit = float(limit or 0)
        collected = float(collected or 0)
        if limit <= 0:
            return          # no ceiling configured for this driver
        ratio = collected / limit

        if ratio >= 1:
            event_key, title = "collection_limit_reached", "Cash limit reached"
            body = (f"You have collected {collected:.0f} of your {limit:.0f} "
                    "daily limit. You cannot accept further COD until the cash "
                    "is handed over.")
        elif ratio >= WARNING_THRESHOLD:
            event_key, title = "collection_limit_warning", "Approaching cash limit"
            body = (f"You have collected {collected:.0f} of your {limit:.0f} "
                    f"daily limit ({ratio * 100:.0f}%). Plan a handover soon.")
        else:
            return

        if _already_sent_today(employee, event_key):
            return
        notify(employee, title, body, event_key=event_key,
               action_type="view_collection",
               data={"collected": round(collected, 2), "limit": round(limit, 2),
                     "percentage": round(ratio * 100)})
    except Exception:
        frappe.log_error(frappe.get_traceback(), "notify: collection limit")


def _already_sent_today(employee, event_key):
    return bool(frappe.db.exists("Driver Notification", {
        "employee":  employee,
        "event_key": event_key,
        "creation":  [">=", frappe.utils.today()],
    }))
