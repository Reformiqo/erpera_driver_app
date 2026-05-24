# Cowberry Driver App — ERP Backend

> Frappe / ERPNext custom app (`cowberry`) that powers the **Cowberry Driver** Flutter mobile app.
> Live site: `cowberry.frappe.cloud`

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Installation & Setup](#3-installation--setup)
4. [Configuration](#4-configuration)
5. [DocTypes (Database Schema)](#5-doctypes-database-schema)
6. [REST API Reference](#6-rest-api-reference)
   - [Authentication](#61-authentication)
   - [Driver Profile](#62-driver-profile)
   - [Trips](#63-trips)
   - [Orders & Delivery](#64-orders--delivery)
   - [Delivery Status](#65-delivery-status)
   - [Cash Collection](#66-cash-collection)
   - [Cash Submission](#67-cash-submission)
   - [Wallet](#68-wallet)
   - [Payments (Razorpay)](#69-payments-razorpay)
   - [Chat](#610-chat)
   - [Analytics](#611-analytics)
7. [OTP Framework](#7-otp-framework)
8. [Permissions & Row-Level Security](#8-permissions--row-level-security)
9. [Scheduler Jobs](#9-scheduler-jobs)
10. [Hooks & Doc Events](#10-hooks--doc-events)
11. [Utilities Reference](#11-utilities-reference)
12. [Error Codes](#12-error-codes)
13. [Business Rules & Hard Gates](#13-business-rules--hard-gates)
14. [File Structure](#14-file-structure)
15. [Deployment](#15-deployment)

---

## 1. Overview

The `cowberry` Frappe app is the server-side backbone for the Cowberry delivery driver platform. It:

- Exposes a **REST API** consumed exclusively by the Flutter mobile app
- Manages **delivery workflows** — trips, orders, proof of delivery, rescheduling
- Handles **cash & wallet transactions** with OTP verification and audit trails
- Integrates with **Razorpay** for COD-Online payment confirmation
- Enforces **row-level permissions** so drivers only see their own data
- Runs **scheduled background jobs** for daily resets and payment polling

**Key identifiers:**

| Item | Value |
|---|---|
| Python package name | `cowberry` |
| Frappe Module name | `Cowberry App` |
| Module folder | `cowberry/cowberry_app/` |
| API namespace | `cowberry.api.*` |
| Live host | `cowberry.frappe.cloud` |

---

## 2. Architecture

```
Flutter App
    │
    │  HTTPS  (cowberry.api.<module>.<method>)
    ▼
Frappe / ERPNext Server
    ├── cowberry/api/          ← Whitelisted REST endpoints
    ├── cowberry/utils/        ← Shared utilities (OTP, geo, response envelope)
    ├── cowberry/hooks.py      ← Doc events, scheduler, permission wiring
    ├── cowberry/permissions.py← Row-level SQL filters
    └── cowberry/cowberry_app/doctype/   ← 12 custom database tables
```

**Response envelope** — every endpoint returns the same shape:

```json
// Success
{ "success": true, "data": { ... } }

// Failure
{ "success": false, "error": { "code": "ERROR_CODE", "message": "Human readable" } }
```

---

## 3. Installation & Setup

### Prerequisites
- Frappe Bench v14+
- ERPNext v14+
- Python 3.10+

### Install the app

```bash
# From your frappe-bench directory
bench get-app https://github.com/reformiqo/cowberry_driver_app_erp
bench --site <your-site> install-app cowberry
bench --site <your-site> migrate
bench restart
```

`after_install` automatically:
- Creates the `Cowberry Driver Settings` singleton
- Creates the `Cowberry Reverse Logistics Settings` singleton
- Creates roles: `Driver`, `Delivery Manager`, `Delivery User`

### Local development

```bash
bench --site dev.localhost console          # interactive Python shell
bench --site dev.localhost migrate          # apply doctype JSON changes
bench --site dev.localhost reload-doctype "Cowberry Cash Submission"
bench --site dev.localhost export-fixtures --app cowberry  # regenerate custom_field.json
bench restart                               # after any Python change
```

### Test an endpoint

```bash
# 1. Login
curl -c /tmp/cb.cookies -X POST https://dev.localhost/api/method/login \
  -d 'usr=driver@example.com&pwd=secret'

# 2. Call any driver endpoint
curl -b /tmp/cb.cookies \
  "https://dev.localhost/api/method/cowberry.api.driver.get_profile"
```

---

## 4. Configuration

All runtime settings live in the **Cowberry Driver Settings** singleton (Frappe Single doctype). Edit via the ERPNext desk UI.

| Field | Type | Description |
|---|---|---|
| `otp_validity_pod` | Int (minutes) | OTP expiry for Proof of Delivery. Default: 10 |
| `otp_validity_cash_submission` | Int (minutes) | OTP expiry for cash handover. Default: 10 |
| `otp_validity_wallet` | Int (minutes) | OTP expiry for wallet operations. Default: 10 |
| `otp_validity_driver_login` | Int (minutes) | OTP expiry for password reset. Default: 10 |
| `otp_max_attempts` | Int | Max wrong OTP attempts before lockout. Default: 5 |
| `razorpay_key_id` | Data | Razorpay API Key ID |
| `razorpay_key_secret` | Password | Razorpay API Key Secret |
| `razorpay_webhook_secret` | Password | Razorpay webhook signing secret (HMAC) |
| `fcm_server_key` | Password | Firebase Cloud Messaging server key for push notifications |

**Reverse logistics** settings live in **Cowberry Reverse Logistics Settings**:

| Field | Type | Description |
|---|---|---|
| `enable_reverse_logistics` | Check | Master toggle |
| `return_warehouse` | Link → Warehouse | Where returned items go |
| `auto_create_return_on_failure` | Check | Auto-create return delivery on failed attempt |
| `max_return_days` | Int | Days allowed for returns. Default: 7 |

---

## 5. DocTypes (Database Schema)

### 5.1 Cowberry Driver Settings *(Single)*

Singleton settings doc. No rows — only one instance exists.

**Fields:** `otp_validity_pod`, `otp_validity_cash_submission`, `otp_validity_wallet`, `otp_validity_driver_login`, `otp_max_attempts`, `razorpay_key_id`, `razorpay_key_secret`, `razorpay_webhook_secret`, `fcm_server_key`

---

### 5.2 Cowberry Reverse Logistics Settings *(Single)*

Singleton for reverse logistics configuration.

**Fields:** `enable_reverse_logistics`, `return_warehouse`, `auto_create_return_on_failure`, `max_return_days`

---

### 5.3 Cowberry Cash Submission *(Submittable)*

Records a driver handing over collected cash to a manager, verified by OTP.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `CS-.YYYY.-.####` |
| `driver` | Link → Employee | The driver submitting cash |
| `collection` | Link → Cowberry Driver Collection | The collection being closed |
| `amount` | Currency | Amount being submitted |
| `status` | Select | `Pending OTP / Verified / Submitted / Rejected` |
| `submission_date` | Date | Date of submission |
| `notes` | Small Text | Optional notes |

**Business rule:** Cannot be submitted (`docstatus=1`) unless `status == "Verified"` (OTP confirmed).

---

### 5.4 Cowberry Driver Collection *(Submittable)*

Daily tally of all payment collected by a driver across cash, online, and wallet channels.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `DC-.YYYY.-.####` |
| `driver` | Link → Employee | The driver |
| `collection_date` | Date | Date of collection |
| `trip` | Link → Delivery Trip | Associated trip |
| `status` | Select | `Open / Closed / Submitted` |
| `total_cash` | Currency | Sum of cash collected |
| `total_online` | Currency | Sum of online payments collected |
| `total_wallet` | Currency | Sum of wallet payments collected |
| `order_breakdown` | Table → CCD Order Item | Per-order payment breakdown |
| `device_token` | Data | Driver's FCM device token (hidden) |

---

### 5.5 CCD Order Item *(Child Table)*

One row per delivery note inside a Driver Collection.

| Field | Type | Description |
|---|---|---|
| `delivery_note` | Link → Delivery Note | The delivery |
| `customer` | Link → Customer | Customer |
| `customer_name` | Data | Fetched from customer |
| `payment_method` | Select | `Cash / COD-Online / Wallet / Prepaid` |
| `cash_amount` | Currency | Cash portion |
| `online_amount` | Currency | Online portion |
| `wallet_amount` | Currency | Wallet portion |
| `total_amount` | Currency | Total for this order |
| `status` | Select | `Pending / Delivered / Failed / Rescheduled` |

---

### 5.6 Cowberry Wallet Transaction *(Submittable)*

Every credit or debit to a customer's wallet. Immutable audit trail.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `WT-.YYYY.-.####` |
| `customer` | Link → Customer | The wallet owner |
| `transaction_type` | Select | `Credit / Debit` |
| `amount` | Currency | Transaction amount |
| `reference` | Data | External reference (order ID, etc.) |
| `notes` | Small Text | Optional notes |

**Business rule:** `Customer.wallet_balance` is always the sum of submitted wallet transactions. Any direct edit to that field is snapped back by the `guard_direct_wallet_balance_writes` hook.

---

### 5.7 Cowberry OTP Log *(Append-only)*

Every OTP ever dispatched. Never updated except to mark `is_used` and increment `attempts`.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `OTP-.YYYY.-.####` |
| `purpose` | Select | `POD / CASH_SUBMISSION / WALLET / DRIVER_LOGIN` |
| `reference_doctype` | Link → DocType | What the OTP is for |
| `reference_name` | Dynamic Link | The specific document |
| `otp_hash` | Data | SHA-256 hash of the OTP (never plaintext) |
| `expires_at` | Datetime | Expiry timestamp |
| `is_used` | Check | Whether OTP has been consumed |
| `attempts` | Int | Number of wrong attempts |
| `recipient_mobile` | Data | Mobile number OTP was sent to |
| `recipient_email` | Data | Email OTP was sent to |

---

### 5.8 Cowberry Reschedule Log *(Append-only)*

Records every time a delivery is rescheduled.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `RSL-.YYYY.-.####` |
| `delivery_note` | Link → Delivery Note | The delivery being rescheduled |
| `driver` | Link → Employee | Driver who rescheduled |
| `reason` | Select | `Customer Not Available / Address Not Found / Vehicle Breakdown / Weather Conditions / Other` |
| `reschedule_date` | Date | New delivery date |
| `notes` | Small Text | Optional notes |

---

### 5.9 Cowberry Delivery Attempt Log *(Append-only)*

GPS-stamped record of every delivery attempt (status change).

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `DAL-.YYYY.-.####` |
| `delivery_note` | Link → Delivery Note | The delivery |
| `driver` | Link → Employee | The driver |
| `attempt_status` | Select | `Out for Delivery / Delivered / Failed / Rescheduled` |
| `latitude` | Float | GPS latitude at time of attempt |
| `longitude` | Float | GPS longitude at time of attempt |
| `notes` | Small Text | Driver notes |
| `failure_reason` | Small Text | Reason if failed |

---

### 5.10 Cowberry Delivery Sync Log *(Submittable)*

Idempotent queue for syncing delivery note state changes to external systems.

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `DSL-.YYYY.-.####` |
| `idempotency_key` | Data | **UNIQUE** — prevents duplicate processing |
| `delivery_note` | Link → Delivery Note | Source document |
| `status` | Select | `Pending / Processing / Completed / Failed` |
| `error_message` | Small Text | Last error if failed |
| `steps` | Table → Cowberry Delivery Sync Step | Processing steps |

---

### 5.11 Cowberry Delivery Sync Step *(Child Table)*

Individual steps within a Delivery Sync Log.

| Field | Type | Description |
|---|---|---|
| `step_name` | Data | Name of the step |
| `status` | Select | `Pending / Completed / Failed` |
| `payload` | Code (JSON) | Step-specific data |
| `error_message` | Small Text | Error if step failed |

---

### 5.12 Cowberry Order Sync Log *(Submittable)*

Retry queue for inbound e-commerce orders from the Cowberry web frontend. **50 live rows in production — never truncate.**

| Field | Type | Description |
|---|---|---|
| `naming_series` | Series | `OSL-.YYYY.-.####` |
| `order_reference` | Data | External order ID |
| `order_doctype` | Data | Source doctype (default: `Sales Order`) |
| `status` | Select | `Pending / Processing / Completed / Failed / Retrying` |
| `retry_count` | Int | How many times retried |
| `next_retry_at` | Datetime | When to retry next (exponential backoff) |
| `payload` | Code (JSON) | Full order payload |
| `error_message` | Small Text | Last failure reason |

---

## 6. REST API Reference

**Base URL:** `https://cowberry.frappe.cloud/api/method/`

**Authentication:** All driver endpoints require a valid Frappe session. Login first:

```bash
POST /api/method/login
Body: usr=<email>&pwd=<password>
```

**Response format:**
```json
{ "success": true,  "data": { ... } }
{ "success": false, "error": { "code": "...", "message": "..." } }
```

---

### 6.1 Authentication

#### `POST cowberry.api.auth.send_reset_otp`
Send a password-reset OTP to a user's email. Guest accessible.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `email` | string | yes | User's email address |

**Response:**
```json
{
  "success": true,
  "data": {
    "log_name": "OTP-2024-00001",
    "message": "OTP sent."
  }
}
```

---

#### `POST cowberry.api.auth.verify_reset_otp`
Verify an OTP without consuming it (pre-check before reset). Guest accessible.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `log_name` | string | yes | OTP log name from `send_reset_otp` |
| `otp` | string | yes | 6-digit OTP |

**Response:**
```json
{ "success": true, "data": { "verified": true } }
```

---

#### `POST cowberry.api.auth.reset_password`
Verify OTP and reset password in one step. Guest accessible.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `log_name` | string | yes | OTP log name |
| `otp` | string | yes | 6-digit OTP |
| `new_password` | string | yes | New password |

**Response:**
```json
{ "success": true, "data": { "message": "Password reset successfully." } }
```

---

### 6.2 Driver Profile

#### `GET cowberry.api.driver.get_profile`
Returns the logged-in driver's profile. Requires `Driver` role.

**Parameters:** None

**Response:**
```json
{
  "success": true,
  "data": {
    "employee_id": "EMP-0001",
    "employee_name": "John Driver",
    "email": "john@example.com",
    "mobile": "+919876543210",
    "image": "/files/john.jpg",
    "department": "Logistics",
    "designation": "Delivery Driver"
  }
}
```

---

#### `POST cowberry.api.driver.update_profile`
Update driver's mobile number or profile image.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `mobile` | string | no | New mobile number |
| `image` | string | no | File URL of new profile image |

**Response:**
```json
{ "success": true, "data": { "message": "Profile updated." } }
```

---

### 6.3 Trips

#### `GET cowberry.api.trip.get_my_trips`
Returns the driver's delivery trips.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `date` | string (YYYY-MM-DD) | no | Filter by date |

**Response:**
```json
{
  "success": true,
  "data": {
    "trips": [
      {
        "name": "DT-0001",
        "date": "2024-01-15",
        "status": "In Transit",
        "total_distance": 45.2,
        "driver": "EMP-0001"
      }
    ]
  }
}
```

---

#### `POST cowberry.api.trip.start_trip`
Mark a trip as "In Transit". Trip must be in "Draft" status.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `trip_id` | string | yes | Delivery Trip name |

**Response:**
```json
{ "success": true, "data": { "trip_id": "DT-0001", "status": "In Transit" } }
```

---

#### `POST cowberry.api.trip.complete_trip`
Mark a trip as "Completed". Trip must be "In Transit".

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `trip_id` | string | yes | Delivery Trip name |

**Response:**
```json
{ "success": true, "data": { "trip_id": "DT-0001", "status": "Completed" } }
```

---

#### `GET cowberry.api.trip.optimise_route`
Returns stops for a trip (nearest-neighbour ordered).

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `trip_id` | string | yes | Delivery Trip name |

**Response:**
```json
{
  "success": true,
  "data": {
    "trip_id": "DT-0001",
    "stops": [
      { "name": "DS-001", "delivery_note": "DN-001", "customer": "CUST-001", "address": "...", "lat": 12.9, "lng": 77.6, "idx": 1 }
    ]
  }
}
```

---

#### `GET cowberry.api.trip.get_summary`
Returns delivery outcome summary for a trip.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `trip_id` | string | yes | Delivery Trip name |

**Response:**
```json
{
  "success": true,
  "data": {
    "trip_id": "DT-0001",
    "total_stops": 12,
    "delivered": 10,
    "failed": 1,
    "status": "Completed"
  }
}
```

---

### 6.4 Orders & Delivery

#### `GET cowberry.api.order.get_order`
Returns full order details for a delivery note.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |

**Response:**
```json
{
  "success": true,
  "data": {
    "delivery_note": "DN-0001",
    "customer": "CUST-001",
    "customer_name": "Jane Smith",
    "posting_date": "2024-01-15",
    "status": "To Deliver",
    "payment_method": "COD-Online",
    "razorpay_payment_status": "Confirmed",
    "grand_total": 1250.00,
    "items": [
      { "item_code": "ITEM-001", "item_name": "Widget", "qty": 2, "rate": 500, "amount": 1000 }
    ]
  }
}
```

---

#### `POST cowberry.api.order.send_delivery_otp`
Send an OTP to the customer's mobile for proof-of-delivery confirmation.

**⚠️ Hard Gate:** If `payment_method == "COD-Online"` and `razorpay_payment_status != "Confirmed"`, returns `402 PAYMENT_NOT_CONFIRMED`. The driver cannot proceed until Razorpay confirms payment.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |
| `customer_mobile` | string | no | Override customer mobile (fallback: from Customer record) |

**Response:**
```json
{
  "success": true,
  "data": { "log_name": "OTP-2024-00042", "message": "OTP sent to customer." }
}
```

---

#### `POST cowberry.api.order.submit_proof`
Verify customer OTP and record proof of delivery.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |
| `otp_log_name` | string | yes | OTP log from `send_delivery_otp` |
| `otp` | string | yes | Customer's 6-digit OTP |
| `proof_image` | string | no | File URL of delivery photo |
| `signature` | string | no | Base64 customer signature |
| `latitude` | float | no | GPS latitude |
| `longitude` | float | no | GPS longitude |

**Response:**
```json
{ "success": true, "data": { "message": "Proof submitted.", "delivery_note": "DN-0001" } }
```

---

#### `POST cowberry.api.order.reschedule`
Reschedule a delivery to a future date.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |
| `reason` | string | yes | One of: `Customer Not Available`, `Address Not Found`, `Vehicle Breakdown`, `Weather Conditions`, `Other` |
| `reschedule_date` | string (YYYY-MM-DD) | yes | New delivery date |
| `notes` | string | no | Additional notes |

**Response:**
```json
{ "success": true, "data": { "message": "Delivery rescheduled.", "log": "RSL-2024-00001" } }
```

---

### 6.5 Delivery Status

#### `POST cowberry.api.delivery.update_status`
Update delivery status following the allowed state machine. Creates an attempt log entry.

**State machine:**
```
Pending ──► Out for Delivery ──► Delivered
                │                   (terminal)
                ├──► Failed
                │    (terminal)
                └──► Rescheduled ──► Out for Delivery
                                 └──► Failed
```

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |
| `status` | string | yes | Target status |
| `latitude` | float | no | GPS latitude |
| `longitude` | float | no | GPS longitude |
| `notes` | string | no | Driver notes |
| `failure_reason` | string | no | Reason if status is Failed |

**Response:**
```json
{ "success": true, "data": { "delivery_note": "DN-0001", "status": "Out for Delivery" } }
```

**Error — invalid transition:**
```json
{ "success": false, "error": { "code": "INVALID_STATUS_TRANSITION", "message": "Cannot transition from 'Delivered' to 'Out for Delivery'." } }
```

---

### 6.6 Cash Collection

#### `GET cowberry.api.collection.get_collection`
Returns the driver's collection records.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `date` | string (YYYY-MM-DD) | no | Filter by date |

**Response:**
```json
{
  "success": true,
  "data": {
    "collections": [
      { "name": "DC-0001", "collection_date": "2024-01-15", "total_cash": 3500, "total_online": 1200, "docstatus": 0, "status": "Open" }
    ]
  }
}
```

---

#### `POST cowberry.api.collection.submit_cash`
Update the cash total on a collection record.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `collection_id` | string | yes | Cowberry Driver Collection name |
| `amount` | float | yes | Cash amount collected |
| `notes` | string | no | Notes |

**Response:**
```json
{ "success": true, "data": { "collection_id": "DC-0001", "total_cash": 3500 } }
```

---

### 6.7 Cash Submission

The three-step OTP-verified cash handover flow:

```
initiate() ──► [OTP sent to driver's mobile]
    │
    ▼
validate_otp_endpoint() ──► [Collection closed, submission verified]
    │
    ▼
history() ──► [View past submissions]
```

#### `POST cowberry.api.cash_submission.initiate`
Create a cash submission record and dispatch OTP.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `collection_id` | string | yes | Collection being closed |
| `amount` | float | yes | Amount being submitted |

**Response:**
```json
{ "success": true, "data": { "submission_id": "CS-2024-00001", "otp_log": "OTP-2024-00050" } }
```

---

#### `POST cowberry.api.cash_submission.validate_otp_endpoint`
Verify OTP, mark submission as Verified, and close the collection.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `submission_id` | string | yes | Cash Submission name |
| `otp_log_name` | string | yes | OTP log from `initiate` |
| `otp` | string | yes | 6-digit OTP |

**Response:**
```json
{ "success": true, "data": { "submission_id": "CS-2024-00001", "status": "Verified" } }
```

---

#### `GET cowberry.api.cash_submission.history`
Returns the driver's past cash submissions.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `limit` | int | no | Records per page (default: 20) |
| `offset` | int | no | Pagination offset (default: 0) |

**Response:**
```json
{
  "success": true,
  "data": {
    "submissions": [
      { "name": "CS-2024-00001", "amount": 3500, "status": "Verified", "creation": "2024-01-15 14:30:00", "collection": "DC-0001" }
    ]
  }
}
```

---

### 6.8 Wallet

All wallet actions go through a single multiplexed endpoint using the `action` parameter.

#### `POST cowberry.api.wallet.load`

**`action=get_balance`** — Get current wallet balance.
```json
// Request
{ "action": "get_balance" }

// Response
{ "success": true, "data": { "balance": 250.00 } }
```

**`action=topup`** — Credit the wallet.
```json
// Request
{ "action": "topup", "amount": 500, "reference": "ORD-001" }

// Response
{ "success": true, "data": { "transaction": "WT-2024-00001" } }
```

**`action=deduct`** — Debit the wallet. Returns `WALLET_INSUFFICIENT` if balance is low.
```json
// Request
{ "action": "deduct", "amount": 100, "reference": "DN-001" }

// Response
{ "success": true, "data": { "transaction": "WT-2024-00002" } }
```

**`action=history`** — Paginated transaction list.
```json
// Request
{ "action": "history", "limit": 20, "offset": 0 }

// Response
{
  "success": true,
  "data": {
    "transactions": [
      { "name": "WT-2024-00001", "transaction_type": "Credit", "amount": 500, "creation": "...", "reference": "ORD-001" }
    ]
  }
}
```

---

### 6.9 Payments (Razorpay)

#### `GET cowberry.api.payment.get_status`
Check Razorpay payment status for a delivery note. Guest accessible.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `delivery_note` | string | yes | Delivery Note name |

**Response:**
```json
{
  "success": true,
  "data": {
    "delivery_note": "DN-0001",
    "razorpay_payment_status": "Confirmed",
    "razorpay_order_id": "order_xyz123"
  }
}
```

---

#### `POST cowberry.api.payment.razorpay_webhook`
Razorpay webhook receiver. Guest accessible, verified by HMAC-SHA256.

- Listens for `payment.captured` event
- Sets `razorpay_payment_status = "Confirmed"` on the matching Delivery Note
- **This is the only code that should ever set the status to "Confirmed"**

**Headers required:**
```
X-Razorpay-Signature: <hmac-sha256 of body using webhook secret>
```

**Response:**
```json
{ "success": true, "data": { "received": true } }
```

---

### 6.10 Chat

#### `GET cowberry.api.chat.get_thread`
Returns messages in a communication thread.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `thread_id` | string | yes | Communication document name |

**Response:**
```json
{
  "success": true,
  "data": {
    "thread_id": "COMM-001",
    "messages": [
      { "name": "COMM-002", "sender": "driver@example.com", "content": "On my way", "creation": "...", "sent_or_received": "Sent" }
    ]
  }
}
```

---

#### `POST cowberry.api.chat.send_message`
Send a message in a thread.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `thread_id` | string | yes | Thread identifier |
| `content` | string | yes | Message text |

**Response:**
```json
{ "success": true, "data": { "message_id": "COMM-003" } }
```

---

### 6.11 Analytics

#### `GET cowberry.api.analytics.get_my_analytics`
Returns the driver's personal delivery and earnings statistics.

**Parameters:**
| Name | Type | Required | Description |
|---|---|---|---|
| `from_date` | string (YYYY-MM-DD) | no | Start of date range |
| `to_date` | string (YYYY-MM-DD) | no | End of date range |

**Response:**
```json
{
  "success": true,
  "data": {
    "total_deliveries": 48,
    "delivered": 42,
    "failed": 3,
    "rescheduled": 3,
    "total_value": 52400.00,
    "total_cash_submitted": 18500.00
  }
}
```

---

## 7. OTP Framework

**File:** `cowberry/utils/otp.py`

### Purposes
| Constant | Value | Used in |
|---|---|---|
| `PURPOSE_POD` | `"POD"` | `order.send_delivery_otp` |
| `PURPOSE_CASH_SUBMISSION` | `"CASH_SUBMISSION"` | `cash_submission.initiate` |
| `PURPOSE_WALLET` | `"WALLET"` | wallet operations |
| `PURPOSE_DRIVER_LOGIN` | `"DRIVER_LOGIN"` | `auth.send_reset_otp` |

### How it works

1. `dispatch_otp_v2(purpose=..., reference_doctype=..., reference_name=..., recipient_mobile=...)` — generates a random 6-digit OTP, stores its SHA-256 hash in `Cowberry OTP Log`, sends via SMS/FCM, returns the log name.
2. `validate_otp_v2(log_name=..., otp_input=...)` — checks expiry, attempt count, and hash. Increments `attempts` on every call. Marks `is_used=1` on success.

### Security properties
- OTP is **never stored in plaintext** — only `hashlib.sha256(otp).hexdigest()`
- Expired OTPs raise `OTPExpiredError`
- Exceeded attempt limit raises `OTPMaxAttemptsError` (configurable in settings)
- Used OTPs cannot be reused (`OTPInvalidError`)

---

## 8. Permissions & Row-Level Security

**File:** `cowberry/permissions.py`

Drivers are restricted to seeing only their own data via SQL-level query conditions registered in `hooks.py`:

| DocType | Filter Applied |
|---|---|
| `Cowberry Cash Submission` | `driver = <current employee>` |
| `Cowberry Driver Collection` | `driver = <current employee>` |
| `Cowberry Wallet Transaction` | `customer = <driver's linked customer>` |
| `Cowberry Delivery Attempt Log` | `driver = <current employee>` |
| `Cowberry Reschedule Log` | `driver = <current employee>` |

Managers (`Delivery Manager`, `System Manager`) bypass these filters and see all records.

---

## 9. Scheduler Jobs

Configured in `hooks.py`:

| Frequency | Function | What it does |
|---|---|---|
| Daily | `cowberry.api.collection.daily_reset_driver_totals` | Zeroes out open collection totals for previous days |
| Hourly | `cowberry.api.payment.poll_pending_razorpay_orders` | Polls Razorpay API for pending/created orders and marks confirmed ones |

---

## 10. Hooks & Doc Events

### Document Events

| DocType | Event | Handler |
|---|---|---|
| `Delivery Note` | `on_submit` | `cowberry.api.delivery.on_submit_delivery_note` — creates a `Cowberry Delivery Sync Log` entry |
| `Delivery Note` | `on_cancel` | `cowberry.api.delivery.on_cancel_delivery_note` |
| `Customer` | `before_save` | `cowberry.api.wallet.guard_direct_wallet_balance_writes` — recalculates wallet balance from transactions |
| `Customer` | `validate` | `cowberry.api.wallet.guard_direct_wallet_balance_writes` |

### Fixtures
Only custom fields with `module = "Cowberry App"` are exported:
```python
fixtures = [{"dt": "Custom Field", "filters": [["module", "=", "Cowberry App"]]}]
```

Regenerate after adding new custom fields:
```bash
bench --site <name> export-fixtures --app cowberry
```

---

## 11. Utilities Reference

### `cowberry.utils.response`
```python
from cowberry.utils.response import ok, err

ok(data={"key": "value"})
# → {"success": True, "data": {"key": "value"}}

err("ERROR_CODE", "Human message", http_status=400)
# → {"success": False, "error": {"code": "ERROR_CODE", "message": "Human message"}}
# Also sets frappe.local.response["http_status_code"]
```

### `cowberry.utils.geo`
```python
from cowberry.utils.geo import haversine_m, validate_coords

haversine_m(12.9716, 77.5946, 12.9352, 77.6245)
# → 5210.3  (metres)

validate_coords(12.9716, 77.5946)
# → (12.9716, 77.5946)  or raises InvalidCoordinatesError
```

### `cowberry.utils.exceptions`
All exceptions inherit from `CowberryError` and have a `.to_response()` method:
```python
raise PaymentNotConfirmedError()
# Automatically returns {"success": false, "error": {"code": "PAYMENT_NOT_CONFIRMED", ...}}
```

---

## 12. Error Codes

| Code | HTTP Status | Meaning |
|---|---|---|
| `NOT_DRIVER` | 403 | User does not have the Driver role |
| `OTP_EXPIRED` | 400 | OTP has passed its validity window |
| `OTP_INVALID` | 400 | Wrong OTP entered |
| `OTP_MAX_ATTEMPTS` | 429 | Too many wrong attempts |
| `ORDER_NOT_FOUND` | 404 | Delivery Note does not exist |
| `DELIVERY_NOTE_NOT_FOUND` | 404 | Delivery Note does not exist |
| `PAYMENT_NOT_CONFIRMED` | 402 | COD-Online payment not confirmed by Razorpay |
| `INVALID_STATUS_TRANSITION` | 400 | Attempted illegal delivery state change |
| `COLLECTION_ALREADY_SUBMITTED` | 400 | Collection is already submitted (docstatus=1) |
| `WALLET_INSUFFICIENT` | 400 | Debit amount exceeds wallet balance |
| `INVALID_COORDINATES` | 400 | Lat/lon out of valid range |
| `RAZORPAY_WEBHOOK_ERROR` | 400 | HMAC signature mismatch |

---

## 13. Business Rules & Hard Gates

### Razorpay COD-Online Gate
**Never bypass this.** In `order.send_delivery_otp`:
```python
if payment_method == "COD-Online":
    if dn.get("razorpay_payment_status") != "Confirmed":
        raise PaymentNotConfirmedError()
```
Only `razorpay_webhook()` may set `razorpay_payment_status = "Confirmed"`. If you bypass this check, drivers can collect deliveries before money is received.

### Wallet Balance Integrity
**Never write `Customer.wallet_balance` directly.** The `guard_direct_wallet_balance_writes` hook recalculates it from submitted `Cowberry Wallet Transaction` records on every Customer save. Any manual write will be overwritten.

### OTP Plaintext
**Never log or return the raw OTP.** Only the SHA-256 hash is persisted. In developer mode only, the raw OTP is written to the Frappe error log for debugging.

### Order Sync Log
**Never truncate `tabCowberry Order Sync Log`.** It has 50 live rows on production representing the e-commerce ingestion audit trail and retry queue.

### Cash Submission Submission Guard
`Cowberry Cash Submission` cannot reach `docstatus=1` unless `status == "Verified"` (OTP confirmed). This is enforced in `cowberry_cash_submission.py:before_submit`.

---

## 14. File Structure

```
cowberry_driver_app_erp/
├── setup.py
├── MANIFEST.in
├── requirements.txt
├── README.md
└── cowberry/
    ├── __init__.py                  # version = "0.0.1"
    ├── hooks.py                     # doc_events, scheduler, fixtures, permissions
    ├── install.py                   # after_install: seed settings + roles
    ├── permissions.py               # row-level SQL filters for Driver role
    ├── modules.txt                  # "Cowberry App"
    ├── patches.txt
    ├── utils/
    │   ├── __init__.py
    │   ├── response.py              # ok() / err()
    │   ├── exceptions.py            # 9 typed error classes
    │   ├── otp.py                   # dispatch/validate v1 + v2 APIs
    │   ├── notifications.py         # send_push() via FCM
    │   └── geo.py                   # haversine_m, validate_coords
    ├── api/
    │   ├── __init__.py
    │   ├── auth.py                  # send_reset_otp, verify_reset_otp, reset_password
    │   ├── driver.py                # get_profile, update_profile, _require_driver()
    │   ├── trip.py                  # get_my_trips, start_trip, complete_trip, optimise_route, get_summary
    │   ├── order.py                 # get_order, send_delivery_otp, submit_proof, reschedule
    │   ├── delivery.py              # update_status, on_submit/on_cancel hooks
    │   ├── collection.py            # get_collection, submit_cash, daily_reset_driver_totals
    │   ├── cash_submission.py       # initiate, validate_otp_endpoint, history
    │   ├── wallet.py                # load() multiplexer, guard_direct_wallet_balance_writes
    │   ├── payment.py               # get_status, razorpay_webhook, poll_pending_razorpay_orders
    │   ├── chat.py                  # get_thread, send_message
    │   └── analytics.py             # get_my_analytics
    ├── cowberry_app/
    │   ├── __init__.py
    │   └── doctype/
    │       ├── __init__.py
    │       ├── cowberry_driver_settings/         # Single
    │       ├── cowberry_reverse_logistics_settings/ # Single
    │       ├── cowberry_cash_submission/         # Submittable
    │       ├── cowberry_driver_collection/       # Submittable
    │       ├── cowberry_wallet_transaction/      # Submittable
    │       ├── cowberry_otp_log/                 # Append-only
    │       ├── cowberry_reschedule_log/          # Append-only
    │       ├── cowberry_delivery_attempt_log/    # Append-only
    │       ├── cowberry_delivery_sync_log/       # Submittable (UNIQUE idempotency_key)
    │       ├── cowberry_delivery_sync_step/      # Child table
    │       ├── cowberry_order_sync_log/          # Submittable (retry queue)
    │       └── ccd_order_item/                   # Child table
    └── fixtures/
        └── custom_field.json        # [] — regenerate from live DB
```

---

## 15. Deployment

### Standard deployment
```bash
bench --site cowberry.frappe.cloud migrate
bench restart
```

### Migration from cowberry_app v0.0.1
If upgrading from the old `cowberry_app` package (preserves existing tables):
```bash
bench --site cowberry.frappe.cloud execute "frappe.utils.remove_from_installed_apps" --args "['cowberry_app']"
bench get-app https://github.com/reformiqo/cowberry_driver_app_erp
bench --site cowberry.frappe.cloud install-app cowberry
bench --site cowberry.frappe.cloud migrate
bench restart
```

### After adding custom fields
```bash
# On dev site — add field via Customize Form UI, then:
bench --site dev.localhost export-fixtures --app cowberry
git add cowberry/fixtures/custom_field.json
git commit -m "feat: add <field_name> custom field to <DocType>"
```

### After modifying DocType JSON
```bash
bench --site dev.localhost migrate
# or for a single doctype:
bench --site dev.localhost reload-doctype "Cowberry Cash Submission"
```
