# CLAUDE.md

Project context for [Claude Code](https://docs.claude.com/claude-code). Read this
before making any change — the constraints below come from a live production
site, not training data.

## What this is

ERPNext / Frappe custom app that backs the **Cowberry Driver** Flutter mobile
app (FRD v1.0). Lives at `cowberry.frappe.cloud`.

- **App name** (Python package): `cowberry`  ← matches Flutter's hardcoded
  `cowberry.api.*` URL namespace. Do NOT rename.
- **Module name** (Frappe Module Def): `Cowberry Driver App`  ← renamed from
  `Cowberry App` in v0.0.2. Another installed app (`cowberry_app`) also
  declared `Cowberry App` in its `modules.txt`, so Frappe could resolve a
  doctype to the wrong app's Python package and `bench migrate` failed with
  `No module named 'cowberry.cowberry_app.doctype.<x>'`. The
  `cowberry.patches.v0_0_2.rename_module_to_cowberry_driver_app` patch
  moves existing live data onto the new Module Def.
- **Module folder**: `cowberry/cowberry_app/`  ← folder name kept as-is to
  preserve import paths (`cowberry.cowberry_app.doctype.<x>`); only the
  Frappe Module Def label changed.

## Hard constraints (don't violate these)

1. **Flutter URL contract is frozen.** Every endpoint Flutter calls
   (`lib/app/data/services/api_endpoints.dart`) must resolve at
   `cowberry.api.<module>.<method>`. Renaming a Python module under `api/`
   breaks the mobile app. New endpoints go in the same namespace.

2. **Response envelope.** Every whitelisted method returns
   `{"success": True, "data": ...}` on success and `{"success": False,
   "error": {"code": "...", "message": "..."}}` on failure. Use
   `cowberry.utils.response.ok()` and `err()` — don't hand-roll dicts.

3. **DocType names from the live DB are authoritative.** The site already
   has tables for `Cowberry Cash Submission`, `Cowberry Driver Collection`,
   `Cowberry Driver Settings`, `Cowberry OTP Log`, `Cowberry Reschedule Log`,
   `Cowberry Wallet Transaction`, `Cowberry Delivery Attempt Log`,
   `Cowberry Delivery Sync Log` (+ `Step` child), `Cowberry Order Sync Log`,
   `Cowberry Reverse Logistics Settings`, and `CCD Order Item`. Renaming
   any of these orphans existing data. The word "Driver" in
   `Cowberry Driver Collection` / `Cowberry Driver Settings` is part of the
   doctype name — do not "fix" it to "Cowberry Driver App".

4. **135 custom fields are committed.** `cowberry/fixtures/custom_field.json`
   was generated from the live `tabCustom Field`. The fieldnames are used by
   the Python API code AND by Flutter's deserializers — never rename a
   fieldname. Add new fields, don't refactor old ones.

5. **Roles use existing site casing.** The site has lowercase
   `warehouse manager` and `warehouse executive` (created via UI), and
   Title-Case `Driver`, `Delivery Manager`, `Delivery User` (ERPNext
   defaults). Match the site's casing exactly when checking roles —
   `frappe.has_role("warehouse manager")` works, `"Warehouse Manager"` does
   not unless the site rename is done first.

6. **Singles vs submittable.** `Cowberry Driver Settings` and
   `Cowberry Reverse Logistics Settings` are Singles. `Cowberry Cash
   Submission`, `Cowberry Driver Collection`, `Cowberry Wallet Transaction`,
   `Cowberry Delivery Sync Log`, and `Cowberry Order Sync Log` are
   submittable. Don't change these flags without a data-migration plan.

## Layout

```
cowberry/
├── hooks.py                    # doc_events, scheduler, fixtures filter, permissions
├── install.py                  # after_install seeds Cowberry Driver Settings
├── permissions.py              # row-level filters for Driver role
├── modules.txt                 # contains exactly: "Cowberry Driver App"
├── patches.txt
├── utils/
│   ├── otp.py                  # OTP framework — has v1 (purpose+ref) and v2 (kwarg-style) APIs
│   ├── notifications.py        # send_push(employee, title, body, payload=None)
│   ├── geo.py                  # haversine_m, validate_coords
│   ├── response.py             # ok() / err() — use these, not raw dicts
│   └── exceptions.py           # typed app errors mapped to FRD §9.11 error codes
├── api/                        # whitelisted endpoints → cowberry.api.<module>.<fn>
│   ├── auth.py                 # send_reset_otp, verify_reset_otp, reset_password
│   ├── driver.py               # get_profile, update_profile, _require_driver() helper
│   ├── trip.py                 # get_my_trips, start_trip, complete_trip, optimise_route, get_summary
│   ├── order.py                # get_order, send_delivery_otp, submit_proof, reschedule  ← Razorpay HARD GATE
│   ├── delivery.py             # update_status (state machine), DN doc_event hooks
│   ├── collection.py           # get_collection, submit_cash, daily_reset_driver_totals
│   ├── cash_submission.py      # initiate (closes collection), validate_otp_endpoint, history
│   ├── wallet.py               # load() multiplexer + guard_direct_wallet_balance_writes hook
│   ├── payment.py              # get_status, razorpay_webhook (HMAC verify), poll_pending_razorpay_orders
│   ├── chat.py                 # get_thread, send_message
│   └── analytics.py            # get_my_analytics
├── cowberry_app/doctype/       # 13 doctypes (folder names are snake_case of doctype name)
│   ├── cowberry_driver_settings/         (Single)
│   ├── cowberry_reverse_logistics_settings/ (Single)
│   ├── cowberry_cash_submission/         (submittable)
│   ├── cowberry_driver_collection/       (submittable)
│   ├── cowberry_wallet_transaction/      (submittable)
│   ├── cowberry_otp_log/                 (append-only)
│   ├── cowberry_reschedule_log/          (append-only)
│   ├── cowberry_delivery_attempt_log/    (append-only)
│   ├── cowberry_delivery_sync_log/       (submittable, idempotency_key UNIQUE)
│   ├── cowberry_delivery_sync_step/      (child table)
│   ├── cowberry_order_sync_log/          (submittable, retry/backoff)
│   └── ccd_order_item/                   (child of Driver Collection.order_breakdown)
└── fixtures/
    └── custom_field.json       # 135 entries — DO NOT hand-edit; regenerate from live DB
```

## Common tasks

### Add a new API endpoint
1. Find the right module under `cowberry/api/` (by domain — order/trip/wallet/etc).
2. Add a `@frappe.whitelist()` function. Call `_require_driver()` first if it's
   driver-scoped.
3. Return via `ok(data=...)`. Raise typed exceptions from
   `cowberry.utils.exceptions` for known failure modes (they auto-set the
   response envelope + HTTP status).
4. Update Flutter's `api_endpoints.dart` with the matching `cowberry.api.<...>`
   URL.

### Add a custom field to an existing doctype
1. Add it in the Frappe UI (Customize Form) on the dev site.
2. Export: `bench --site <name> export-fixtures --app cowberry` — this
   re-generates `cowberry/fixtures/custom_field.json` based on the
   `module = "Cowberry Driver App"` filter in `hooks.py`.
3. Make sure the new field has `module = "Cowberry Driver App"` set in
   Custom Field doctype, otherwise the export will skip it.

### Add a new doctype
1. Create it in the UI under module **Cowberry Driver App**.
2. Set autoname, naming series, permissions (System Manager + Delivery Manager
   read at minimum; Driver only if drivers will access it through the API).
3. Export the JSON to `cowberry/cowberry_app/doctype/<snake_name>/<snake_name>.json`.
4. Add `__init__.py` and `<snake_name>.py` with a `class <CamelName>(Document): pass`.
5. If row-level permissions are needed, add an entry in `permissions.py` and
   wire it in `hooks.py` under `permission_query_conditions`.

### Modify OTP flow
- `cowberry/utils/otp.py` exposes both the original `dispatch_otp(purpose, ...)`
  / `validate_otp(log_name, ...)` API and the v2 kwarg adapters
  `dispatch_otp_v2` / `validate_otp_v2` (the API modules use v2).
- OTP purposes are constants: `PURPOSE_POD`, `PURPOSE_CASH_SUBMISSION`,
  `PURPOSE_WALLET`, `PURPOSE_DRIVER_LOGIN`.
- Validity windows + max attempts live in `Cowberry Driver Settings`, not in code.

### Test locally
```bash
# Bench commands run from ~/frappe-bench
bench --site dev.localhost console      # interactive shell with the app loaded
bench --site dev.localhost migrate      # apply doctype JSON changes
bench --site dev.localhost reload-doctype "Cowberry Cash Submission"   # reload a single doctype
bench --site dev.localhost export-fixtures --app cowberry              # regenerate custom_field.json
bench restart                            # after Python changes
```

### Try an endpoint
```bash
# Login (gets a session cookie)
curl -c /tmp/cb.cookies -X POST https://dev.localhost/api/method/login \
  -d 'usr=driver@example.com&pwd=...'

# Call a driver endpoint
curl -b /tmp/cb.cookies https://dev.localhost/api/method/cowberry.api.driver.get_profile
```

## Things that will burn you

- **`pe.cowberry_driver = ...`** in `cash_submission.py` is a Payment Entry
  custom **field** named `cowberry_driver`. Don't grep-replace `cowberry_driver`
  globally — you'll break custom field references.
- **`Cowberry Driver Collection`** vs the module name **`Cowberry Driver App`**:
  both contain "Driver" — don't confuse "the doctype" with "the module".
- **Module-name collision history.** This app's Frappe module used to be
  `Cowberry App`. Another installed app (`cowberry_app`) also claimed that
  name in its `modules.txt`, causing `bench migrate` to fail with
  `No module named 'cowberry.cowberry_app.doctype.<x>'`. v0.0.2 renamed
  this app's module to `Cowberry Driver App` (modules.txt, all doctype JSONs,
  fixtures filter); the
  `cowberry.patches.v0_0_2.rename_module_to_cowberry_driver_app` patch
  rewrites `tabDocType.module` and `tabCustom Field.module` rows on existing
  sites. Always set new doctypes/CFs to **Cowberry Driver App**, never the
  old name.
- **`razorpay_payment_status == "Confirmed"`** is a hard gate in
  `order.send_delivery_otp` for COD-Online — if you bypass it, drivers can
  deliver before money is received. The webhook in `payment.py` is the only
  thing that should ever set it to `"Confirmed"`.
- **`Cowberry Wallet Transaction`** balance updates take a row lock
  (`SELECT ... FOR UPDATE` on the Customer). Don't update
  `Customer.wallet_balance` directly — the `guard_direct_wallet_balance_writes`
  hook will snap it back to the sum of submitted wallet transactions.
- **`Cowberry Order Sync Log`** has 50 real rows on the production DB
  (e-commerce ingestion from the Cowberry web frontend). Don't truncate this
  table — it's the audit trail / retry queue.
- **All driver-app doctypes (`Cash Submission`, `Driver Collection`, etc.)
  are currently empty** on the production DB, so it's safe to amend their
  JSONs without a data migration. This won't be true for long once drivers
  start using the Flutter app.

## Deployment

See `README.md` — the **Migration from `cowberry_app` v0.0.1** section has the
exact `bench remove-from-installed-apps cowberry_app` → `install-app cowberry`
flow that preserves the existing tables.
