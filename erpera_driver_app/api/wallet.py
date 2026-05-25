import frappe

from erpera_driver_app.api.driver import _require_driver
from erpera_driver_app.utils.exceptions import WalletInsufficientError
from erpera_driver_app.utils.otp import PURPOSE_WALLET, dispatch_otp_v2, validate_otp_v2
from erpera_driver_app.utils.response import err, ok


@frappe.whitelist()
def load(action, **kwargs):
    """Multiplexer: routes to sub-actions."""
    actions = {
        "get_balance": _get_balance,
        "topup": _topup,
        "deduct": _deduct,
        "history": _transaction_history,
    }
    fn = actions.get(action)
    if not fn:
        return err("UNKNOWN_ACTION", f"Unknown wallet action: {action}")
    return fn(**kwargs)


def _get_balance(**kwargs):
    try:
        employee = _require_driver()
        customer = frappe.db.get_value("Customer", {"cowberry_driver": employee}, "name")
        if not customer:
            return ok(data={"balance": 0.0})
        balance = frappe.db.get_value("Customer", customer, "wallet_balance") or 0.0
        return ok(data={"balance": float(balance)})
    except Exception as e:
        return err("GET_BALANCE_FAILED", str(e))


def _topup(amount=0, reference=None, **kwargs):
    try:
        employee = _require_driver()
        customer = frappe.db.get_value("Customer", {"cowberry_driver": employee}, "name")
        if not customer:
            return err("NO_CUSTOMER", "No customer linked to this driver.")

        txn = frappe.new_doc("Cowberry Wallet Transaction")
        txn.customer = customer
        txn.transaction_type = "Credit"
        txn.amount = float(amount)
        txn.reference = reference or ""
        txn.insert(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"transaction": txn.name})
    except Exception as e:
        return err("TOPUP_FAILED", str(e))


def _deduct(amount=0, reference=None, **kwargs):
    try:
        employee = _require_driver()
        customer = frappe.db.get_value("Customer", {"cowberry_driver": employee}, "name")
        if not customer:
            return err("NO_CUSTOMER", "No customer linked to this driver.")

        balance = frappe.db.get_value("Customer", customer, "wallet_balance") or 0.0
        if float(balance) < float(amount):
            raise WalletInsufficientError()

        txn = frappe.new_doc("Cowberry Wallet Transaction")
        txn.customer = customer
        txn.transaction_type = "Debit"
        txn.amount = float(amount)
        txn.reference = reference or ""
        txn.insert(ignore_permissions=True)
        frappe.db.commit()
        return ok(data={"transaction": txn.name})
    except WalletInsufficientError as e:
        return e.to_response()
    except Exception as e:
        return err("DEDUCT_FAILED", str(e))


def _transaction_history(limit=20, offset=0, **kwargs):
    try:
        employee = _require_driver()
        customer = frappe.db.get_value("Customer", {"cowberry_driver": employee}, "name")
        if not customer:
            return ok(data={"transactions": []})
        txns = frappe.get_all(
            "Cowberry Wallet Transaction",
            filters={"customer": customer},
            fields=["name", "transaction_type", "amount", "creation", "reference"],
            order_by="creation desc",
            limit=int(limit),
            start=int(offset),
        )
        return ok(data={"transactions": txns})
    except Exception as e:
        return err("TRANSACTION_HISTORY_FAILED", str(e))


def guard_direct_wallet_balance_writes(doc, method):
    """Hook: snap wallet_balance back to sum of submitted transactions if written directly."""
    if method in ("before_save", "validate"):
        correct = frappe.db.sql(
            """SELECT COALESCE(SUM(CASE WHEN transaction_type='Credit' THEN amount ELSE -amount END), 0)
               FROM `tabCowberry Wallet Transaction`
               WHERE customer=%s AND docstatus=1""",
            doc.name,
        )[0][0] or 0.0
        doc.wallet_balance = correct
