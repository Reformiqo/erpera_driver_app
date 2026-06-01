import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import WalletInsufficientError
from erpera_driver_app.utils.response import err, ok


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
