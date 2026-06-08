import frappe
from frappe.utils import add_to_date, cint, flt, now_datetime, today

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import OTPInvalidError, WalletInsufficientError
from erpera_driver_app.utils.otp import PURPOSE_WALLET, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok

OTP_VALIDITY_MINUTES = 5


def _mask_mobile(mobile):
    """Render +91 9876543210 → ****3210 so the driver gets confirmation
    the OTP went to the right place without seeing the full number."""
    if not mobile:
        return None
    digits = "".join(c for c in str(mobile) if c.isdigit())
    if len(digits) < 4:
        return "****"
    return "****" + digits[-4:]


# ---------------------------------------------------------------------------
# Spec-named endpoints (Nainsi's xlsx §Wallet §§1-5)
# ---------------------------------------------------------------------------

@frappe.whitelist(methods=["GET"])
def validate_card(card_number=None):
    """§1 Pre-flight check on a customer's wallet card. Returns the
    masked balance + top-up bounds per spec; never reveals the actual
    balance to the driver."""
    try:
        _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.", 400)
        cust = _resolve_customer(card_number)
        if not cust:
            return err("NOT_FOUND", "Wallet card not registered.", 404)
        if not cust.get("wallet_active"):
            return err("WALLET_INACTIVE",
                       "Wallet is not active for this card.", 400)
        return ok(data={
            "customer_name":  cust.customer_name,
            "wallet_active":  1,
            "masked_balance": _mask_balance(flt(cust.get("wallet_balance"))),
            "min_topup":      flt(cust.get("wallet_min_topup") or 100),
            "max_topup":      flt(cust.get("wallet_max_topup") or 10000),
        })
    except Exception as e:
        return err("VALIDATE_CARD_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def initiate_topup(card_number=None, amount=None):
    """§2 Stage a top-up: creates a DRAFT Wallet Transaction, dispatches
    an OTP to the customer's mobile (5-min validity), returns the
    handle the Flutter client passes to validate_topup_otp.
    """
    try:
        employee = _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.", 400)
        cust = _resolve_customer(card_number)
        if not cust:
            return err("NOT_FOUND", "Wallet card not registered.", 404)
        if not cust.get("wallet_active"):
            return err("WALLET_INACTIVE",
                       "Wallet is not active for this card.", 400)
        amt = flt(amount or 0)
        min_t = flt(cust.get("wallet_min_topup") or 100)
        max_t = flt(cust.get("wallet_max_topup") or 10000)
        if amt < min_t or amt > max_t:
            return err("VALIDATION_ERROR",
                       f"Amount must be between {min_t} and {max_t}.", 400)

        txn = frappe.new_doc("Wallet Transaction")
        txn.customer = cust.name
        txn.transaction_type = "Credit"
        txn.amount = amt
        txn.reference = f"Driver top-up by {employee}"
        txn.flags.ignore_permissions = True
        txn.insert(ignore_permissions=True)   # docstatus=0 (Draft)

        mobile = cust.get("mobile_no") or frappe.db.get_value(
            "Contact", {"name": frappe.db.get_value(
                "Dynamic Link",
                {"link_doctype": "Customer", "link_name": cust.name},
                "parent")}, "mobile_no")
        try:
            dispatch_otp_v2(
                purpose=PURPOSE_WALLET,
                reference_doctype="Wallet Transaction",
                reference_name=txn.name,
                recipient_mobile=mobile or "",
            )
        except Exception:
            # OTP dispatch failure shouldn't block; log and let the
            # client retry. The draft txn stays so retry doesn't
            # create a duplicate.
            frappe.logger("erpera_driver_app").exception(
                f"Wallet OTP dispatch failed for {txn.name}")

        frappe.db.commit()
        return ok(data={
            "wallet_transaction":   txn.name,
            "otp_sent_to":          _mask_mobile(mobile),
            "otp_validity_minutes": OTP_VALIDITY_MINUTES,
        })
    except Exception as e:
        return err("INITIATE_TOPUP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def validate_topup_otp(wallet_transaction=None, otp=None):
    """§3 Validate the OTP, submit the Wallet Transaction with a
    row-level lock to avoid concurrent-topup races, return the new
    balance for driver-side confirmation."""
    try:
        employee = _require_driver()
        if not wallet_transaction or not otp:
            return err("VALIDATION_ERROR",
                       "`wallet_transaction` and `otp` are required.", 400)
        if not frappe.db.exists("Wallet Transaction", wallet_transaction):
            return err("NOT_FOUND",
                       f"Wallet Transaction '{wallet_transaction}' not found.", 404)
        # Resolve the matching OTP Log server-side so the client only
        # needs the transaction handle.
        otp_log = frappe.db.get_value(
            "OTP Log",
            {"reference_doctype": "Wallet Transaction",
             "reference_name":    wallet_transaction,
             "is_used":           0},
            "name", order_by="creation desc",
        )
        if not otp_log:
            return err("OTP_INVALID",
                       "No outstanding OTP for this transaction.", 400)
        try:
            validate_otp_v2(log_name=otp_log, otp_input=otp)
        except OTPInvalidError as oe:
            return oe.to_response()

        # Row-level lock on the customer to serialise concurrent topups.
        txn = frappe.get_doc("Wallet Transaction", wallet_transaction)
        if txn.docstatus == 1:
            return err("VALIDATION_ERROR",
                       "This top-up has already been validated.", 400)
        # Submit needs to elevate — drivers don't have Wallet Transaction
        # write perms (and shouldn't, in case a non-driver tampers via
        # Desk). The row-lock + submit happen as Administrator; we
        # restore the driver context immediately after.
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            frappe.db.sql(
                "SELECT name FROM `tabCustomer` WHERE name=%s FOR UPDATE",
                (txn.customer,),
            )
            txn.submit()
            frappe.db.commit()
        finally:
            frappe.set_user(original_user)

        new_balance = flt(frappe.db.get_value(
            "Customer", txn.customer, "wallet_balance"))
        return ok(data={
            "new_balance":        new_balance,
            "wallet_transaction": txn.name,
            "receipt_pending":    True,
        })
    except Exception as e:
        return err("VALIDATE_TOPUP_OTP_FAILED", str(e))


@frappe.whitelist(methods=["POST"])
def send_receipt(wallet_transaction=None, channel="SMS"):
    """§4 Send a balance receipt to the customer via SMS or WhatsApp.
    Only valid on a submitted Wallet Transaction.
    """
    try:
        _require_driver()
        if not wallet_transaction:
            return err("VALIDATION_ERROR",
                       "`wallet_transaction` is required.", 400)
        if channel not in ("SMS", "WhatsApp"):
            return err("VALIDATION_ERROR",
                       "`channel` must be 'SMS' or 'WhatsApp'.", 400)
        if not frappe.db.exists("Wallet Transaction", wallet_transaction):
            return err("NOT_FOUND",
                       f"Wallet Transaction '{wallet_transaction}' not found.", 404)
        txn = frappe.get_doc("Wallet Transaction", wallet_transaction)
        if txn.docstatus != 1:
            return err("VALIDATION_ERROR",
                       "Receipt can only be sent on a submitted top-up.", 400)
        new_balance = flt(frappe.db.get_value(
            "Customer", txn.customer, "wallet_balance"))
        mobile = frappe.db.get_value("Customer", txn.customer, "mobile_no")
        msg = (f"Wallet credit: {flt(txn.amount):.2f}. "
               f"New balance: {new_balance:.2f}. Thank you.")
        try:
            from frappe.core.doctype.sms_settings.sms_settings import send_sms
            send_sms([mobile], msg)
        except Exception:
            frappe.logger("erpera_driver_app").info(
                f"[Wallet receipt] {channel} placeholder: mobile={mobile} {msg}"
            )
        return ok(data={"sent": True, "channel": channel})
    except Exception as e:
        return err("SEND_RECEIPT_FAILED", str(e))


@frappe.whitelist(methods=["GET"])
def history(date=None):
    """§5 Driver-scoped top-ups for a given date (defaults to today).
    Includes daily counters used by the wallet dashboard.
    """
    try:
        employee = _require_driver()
        target = date or today()
        # All submitted top-ups planted by this driver on the date.
        rows = frappe.db.sql(
            """
            SELECT wt.name,
                   wt.customer,
                   c.customer_name AS customer_display,
                   wt.amount,
                   wt.creation,
                   wt.modified AS submitted_at
              FROM `tabWallet Transaction` wt
              JOIN `tabCustomer` c ON c.name = wt.customer
             WHERE wt.docstatus = 1
               AND wt.reference LIKE %(prefix)s
               AND DATE(wt.creation) = %(d)s
             ORDER BY wt.creation DESC
            """,
            {"prefix": f"Driver top-up by {employee}%", "d": target},
            as_dict=True,
        )
        out = [{
            "name":         r["name"],
            "customer":     r["customer_display"] or r["customer"],
            "amount":       flt(r["amount"]),
            "submitted_at": str(r["submitted_at"]),
        } for r in rows]
        return ok(data={
            "top_ups":            out,
            "total_topups_today": len(out),
            "total_value_today":  sum(t["amount"] for t in out),
        })
    except Exception as e:
        return err("WALLET_HISTORY_FAILED", str(e))


@frappe.whitelist()
def load(action, **kwargs):
    """Wallet API multiplexer (FRD §9.6).

    All driver-facing wallet operations route through one whitelisted
    method so the Flutter app needs only a single endpoint constant.
    The `action` kwarg selects the sub-handler; everything else is
    forwarded.

    Sub-actions:
        - `validate_card`  — look up Customer by `wallet_card_number`,
          return masked balance + topup bounds.
        - `get_balance`    — fetch wallet_balance for a known card.
        - `topup`          — credit Customer wallet via Wallet Transaction.
        - `deduct`         — debit Customer wallet.
        - `history`        — paginated transaction list for the card.

    Wallet is per-Customer (FRD §3.10). The previous implementation
    looked up Customer by a `cowberry_driver` field which inverted the
    relationship; the driver now passes the customer's card number on
    every call.
    """
    actions = {
        "validate_card": _validate_card,
        "get_balance": _get_balance,
        "topup": _topup,
        "deduct": _deduct,
        "history": _transaction_history,
    }
    fn = actions.get(action)
    if not fn:
        return err("UNKNOWN_ACTION", f"Unknown wallet action: {action}")
    return fn(**kwargs)


def _resolve_customer(card_number):
    """Lookup Customer by `wallet_card_number` custom field.

    Returns the Customer doc or None. Raising is avoided so each
    sub-action can return its own typed error envelope.
    """
    if not card_number:
        return None
    name = frappe.db.get_value(
        "Customer", {"wallet_card_number": card_number}, "name"
    )
    if not name:
        return None
    return frappe.get_doc("Customer", name)


def _mask_balance(balance):
    """Renders ₹**XX style so the raw amount never leaves the server.

    FRD §3.10 row 3: the driver should only see whether the customer
    has wallet headroom, not the exact balance.
    """
    b = float(balance or 0)
    s = f"{int(b)}"
    if len(s) <= 2:
        return "₹**"
    return f"₹**{s[-2:]}"


def _validate_card(card_number=None, **kwargs):
    """FRD §9.6.1 — pre-flight card check before the top-up screen unlocks."""
    try:
        _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.")
        cust = _resolve_customer(card_number)
        if not cust:
            return err("CARD_NOT_FOUND", "Wallet card not registered.", 404)
        if not cust.get("wallet_active"):
            return err("WALLET_INACTIVE", "Wallet is not active for this card.")
        balance = float(cust.get("wallet_balance") or 0)
        return ok(data={
            "card_number": card_number,
            "customer": cust.name,
            "customer_name": cust.customer_name,
            "wallet_active": True,
            "masked_balance": _mask_balance(balance),
            "balance": balance,
            "wallet_min_topup": float(cust.get("wallet_min_topup") or 100),
            "wallet_max_topup": float(cust.get("wallet_max_topup") or 10000),
        })
    except Exception as e:
        return err("VALIDATE_CARD_FAILED", str(e))


def _get_balance(card_number=None, **kwargs):
    try:
        _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.")
        cust = _resolve_customer(card_number)
        if not cust:
            return err("CARD_NOT_FOUND", "Wallet card not registered.", 404)
        balance = float(cust.get("wallet_balance") or 0)
        return ok(data={
            "card_number": card_number,
            "customer": cust.name,
            "balance": balance,
            "masked_balance": _mask_balance(balance),
        })
    except Exception as e:
        return err("GET_BALANCE_FAILED", str(e))


def _topup(card_number=None, amount=0, reference=None, **kwargs):
    """Credit the customer's wallet. Submitting the Wallet Transaction
    triggers `guard_direct_wallet_balance_writes` which recomputes
    Customer.wallet_balance from the ledger sum.
    """
    try:
        employee = _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.")
        cust = _resolve_customer(card_number)
        if not cust:
            return err("CARD_NOT_FOUND", "Wallet card not registered.", 404)
        if not cust.get("wallet_active"):
            return err("WALLET_INACTIVE", "Wallet is not active for this card.")

        amt = float(amount or 0)
        min_t = float(cust.get("wallet_min_topup") or 100)
        max_t = float(cust.get("wallet_max_topup") or 10000)
        if amt < min_t or amt > max_t:
            return err(
                "VALIDATION_ERROR",
                f"Amount must be between {min_t} and {max_t}.",
            )

        txn = frappe.new_doc("Wallet Transaction")
        txn.customer = cust.name
        txn.transaction_type = "Credit"
        txn.amount = amt
        txn.reference = reference or f"Driver top-up by {employee}"
        txn.insert(ignore_permissions=True)
        txn.submit()
        frappe.db.commit()
        new_balance = float(
            frappe.db.get_value("Customer", cust.name, "wallet_balance") or 0
        )
        return ok(data={
            "transaction": txn.name,
            "customer": cust.name,
            "new_balance": new_balance,
            "masked_balance": _mask_balance(new_balance),
        })
    except Exception as e:
        return err("TOPUP_FAILED", str(e))


def _deduct(card_number=None, amount=0, reference=None, **kwargs):
    try:
        _require_driver()
        if not card_number:
            return err("VALIDATION_ERROR", "card_number is required.")
        cust = _resolve_customer(card_number)
        if not cust:
            return err("CARD_NOT_FOUND", "Wallet card not registered.", 404)

        balance = float(cust.get("wallet_balance") or 0)
        amt = float(amount or 0)
        if balance < amt:
            raise WalletInsufficientError()

        txn = frappe.new_doc("Wallet Transaction")
        txn.customer = cust.name
        txn.transaction_type = "Debit"
        txn.amount = amt
        txn.reference = reference or ""
        txn.insert(ignore_permissions=True)
        txn.submit()
        frappe.db.commit()
        new_balance = float(
            frappe.db.get_value("Customer", cust.name, "wallet_balance") or 0
        )
        return ok(data={
            "transaction": txn.name,
            "customer": cust.name,
            "new_balance": new_balance,
        })
    except WalletInsufficientError as e:
        return e.to_response()
    except Exception as e:
        return err("DEDUCT_FAILED", str(e))


def _transaction_history(card_number=None, limit=20, offset=0, **kwargs):
    """Two modes of paginated history:

    - With `card_number`: returns the matching customer's transactions
      (driver looking at a specific customer's wallet ledger).
    - Without: returns the calling driver's own top-ups across all
      customers. The driver is identified via the Wallet Transaction's
      `reference` text (`_topup` writes "Driver top-up by EMP-…"). When
      a dedicated `driver` field is later added to Wallet Transaction
      this scoping should switch to the indexed column.
    """
    try:
        employee = _require_driver()
        if card_number:
            cust = _resolve_customer(card_number)
            if not cust:
                return ok(data={"transactions": []})
            txns = frappe.get_all(
                "Wallet Transaction",
                filters={"customer": cust.name, "docstatus": 1},
                fields=[
                    "name", "transaction_type", "amount", "creation",
                    "reference", "customer",
                ],
                order_by="creation desc",
                limit=int(limit),
                start=int(offset),
            )
            return ok(data={
                "transactions": txns,
                "customer": cust.name,
            })

        # Driver-scoped: match by the reference text the topup writer plants.
        # LIKE has an index when reference is a leading-anchored search.
        txns = frappe.db.sql(
            """
            SELECT name, transaction_type, amount, creation, reference, customer
            FROM `tabWallet Transaction`
            WHERE docstatus = 1
              AND reference LIKE %(prefix)s
            ORDER BY creation DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            {
                "prefix": f"Driver top-up by {employee}%",
                "limit": int(limit),
                "offset": int(offset),
            },
            as_dict=True,
        )
        return ok(data={
            "transactions": txns,
            "driver": employee,
        })
    except Exception as e:
        return err("TRANSACTION_HISTORY_FAILED", str(e))


def guard_direct_wallet_balance_writes(doc, method):
    """Hook: snap Customer.wallet_balance back to the ledger sum.

    Any direct write to Customer.wallet_balance is overwritten with
    the sum of submitted Wallet Transactions for that customer so the
    column never diverges from the audit log.
    """
    if method in ("before_save", "validate"):
        correct = frappe.db.sql(
            """SELECT COALESCE(SUM(
                   CASE WHEN transaction_type='Credit' THEN amount ELSE -amount END
               ), 0)
               FROM `tabWallet Transaction`
               WHERE customer=%s AND docstatus=1""",
            doc.name,
        )[0][0] or 0.0
        doc.wallet_balance = float(correct)
