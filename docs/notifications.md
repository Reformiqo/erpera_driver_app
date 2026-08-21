# Driver notifications

Push notifications to the driver app, over FCM HTTP v1.

Nine events are wired. Each one writes a **Driver Notification** row (the
in-app inbox) and pushes to every device the driver has registered. Every
event can be switched off individually in **Driver Notification Settings**.

---

## Setup

### 1. Firebase credentials

Firebase Console → **Project Settings → Service Accounts → Generate new private
key**. That downloads a JSON file.

Paste the entire file into **Driver Notification Settings → Firebase
Credentials → Service Account JSON**. That is the only field to fill in — the
project id is read out of the JSON itself.

`Driver Notification Settings` is a Single of its own rather than a section on
`Driver Settings`, which already carries OTP, signup, Razorpay and delivery
config. (Frappe core already owns the name `Notification Settings`, so the
`Driver` prefix is required, not decorative.) The legacy **FCM Server Key** on
`Driver Settings` is untouched and still unread — Google decommissioned that
API in June 2024.

### 2. Python dependency

```bash
./env/bin/pip install google-auth
```

Usually already present for Frappe's Google integrations. Without it, pushes
are skipped and the Error Log says exactly that; nothing else breaks.

### 3. The device registers itself — at login, for free

`auth.driver_login` already receives `fcm_token` and `device_id`, so it
registers the device itself. **The app needs no extra call to start receiving
push.**

That was previously broken rather than absent: `Employee.fcm_device_token`
stores `device_id or fcm_token` for the concurrent-login guard, so whenever the
app sent both — which it does — the FCM token was discarded and no push could
ever be addressed to that driver.

`register_token` remains for the case login does not cover: Firebase reissues
tokens on its own schedule, and that happens without a login.

```dart
FirebaseMessaging.instance.onTokenRefresh.listen((t) =>
    api.post('erpera_driver_app.api.notification.register_token',
             {'fcm_token': t, 'device_id': deviceId, 'device_type': 'Android'}));
```

Both paths go through the same `register_device`, keyed on `device_id`, so a
token refresh replaces that device's row rather than leaving a dead token we
would keep pushing at. Re-registering also reactivates a row FCM had rejected,
which is what makes a reinstall start working again.

### 4. Prove it works

```
POST /api/method/erpera_driver_app.api.notification.send_test
```

Sends the calling driver a push inline (not queued) and reports whether FCM
accepted it, with a hint about what to check if it didn't.

---

## The nine events

| # | Event | `event_key` | Fires from |
|---|---|---|---|
| 1 | New trip assigned | `trip_assigned` | Delivery Trip `after_insert` / driver changed on update |
| 2 | Trip updated / re-optimised | `trip_updated` | Stops added or resequenced |
| 3 | Order rescheduled by ops | `order_rescheduled` | DN `cowberry_reschedule_date` changed by someone other than the driver |
| 4 | Order cancelled / pulled from trip | `order_cancelled` | DN `on_cancel`, a stop removed from the trip, or the whole trip reassigned away |
| 5 | Collection limit warning (≥80%) | `collection_limit_warning` | After each COD delivery |
| 6 | Collection limit reached (100%) | `collection_limit_reached` | After each COD delivery |
| 7 | Customer chat message | `customer_chat_message` | Communication on a DN from someone other than the driver |
| 8 | COD-Online payment confirmed | `payment_confirmed` | Razorpay webhook **and** the hourly poller |
| 9 | Cash handover accepted by WM | `cash_handover_accepted` | `cash_submission.validate_otp_endpoint` |

Each has a `notify_<event_key>` checkbox in Driver Notification Settings, plus the
**Enable Notifications** master switch.

**Ticked means sent, unticked means not sent.** The saved value is
authoritative; each box carries a one-line description of what it covers.

The one wrinkle is a Single nobody has opened yet: Frappe writes no row to
`tabSingles` until the first save, so every field reads back as `None` rather
than as the default the form displays. In that case the DocField default is
used, which is why a fresh install behaves as the form shows it — everything
ticked — without that fallback ever overriding a choice someone made.

### Notes on individual events

**3 and 4** stay quiet when the driver is the one doing it — a driver
rescheduling their own stop does not need to be told they did.

**4** also covers "Trip reassigned", sent to the *previous* driver when a trip
moves to someone else. It is not a tenth event: the trip leaving their list is
the same news as an order leaving it, and there are only nine boxes.

**5 and 6** fire at most once per driver per day each. Without that, every
delivery past 80% would send another push and the one that matters — hitting
100% — would arrive as the fifth identical buzz. Crossing from warning into
reached still gets through, because it is a different event.

**8** is the highest-value one: `razorpay_payment_status == "Confirmed"` is the
hard gate in `order.send_delivery_otp`. Until it flips, the driver cannot
request the POD OTP. Both the webhook and the poller push, so whichever
confirms first is the one that tells the driver — the poller exists precisely
for when the webhook never arrived.

---

## Endpoints

All under `erpera_driver_app.api.notification.*`, all scoped to the calling
driver.

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `register_token` | Store this device's FCM token |
| POST | `unregister_token` | Deactivate on logout |
| GET | `get_notifications` | Inbox, newest first, with `unread_count` |
| POST | `mark_read` | Mark one read |
| POST | `mark_all_read` | Clear the badge |
| POST | `send_test` | Send yourself a push |

`get_notifications` takes `page`, `page_size` (max 100) and `unread_only`.

---

## Adding an event

1. Add a `notify_<key>` Check to `driver_notification_settings.json` — field and
   `field_order`, default `"1"`.
2. Call `notify()` where the thing happens:

```python
from erpera_driver_app.utils.notifications import notify

notify(employee, "Title", "Body",
       event_key="my_event",
       reference_doctype="Delivery Note", reference_name=dn,
       action_type="view_order")
```

`notify()` never raises and returns `None` when the event is switched off, so
it needs no `try`/`except` around it at the call site.

---

## Design rules

Three things this code will not do, because it runs inside doc events and
submit handlers:

1. **A notification never breaks the transaction that triggered it.** Nothing
   calls `frappe.db.rollback()` — a rollback inside `on_submit` would take the
   caller's Delivery Note down with it.
2. **No HTTP inside the request.** The FCM call is enqueued with
   `enqueue_after_commit=True`. A slow or unreachable Google costs a background
   worker, not a driver waiting on a submit.
3. **Successes do not go to the Error Log.** `frappe.log_error` is for
   failures. Delivery counts go to the site log via `frappe.logger`.

`is_pushed` on a Driver Notification means *FCM accepted it for at least one
device* — not merely that we tried. A row sitting at `is_pushed = 0` is a real
delivery failure worth looking at.

A token FCM reports as `UNREGISTERED` / `NOT_FOUND` is deactivated
automatically, so an uninstalled app stops being pushed to.
