import frappe
from frappe.model.document import Document
from frappe.utils import flt, today


class CashSubmission(Document):
    """Driver → Warehouse cash handover (FRD §4.2 / §7.3).

    Lifecycle:
        Pending OTP → Verified (OTP validated by Cash Submission API)
                   → Submitted (docstatus=1, PE created here)
    """

    def before_submit(self):
        if self.status != "Verified":
            frappe.throw(
                "Cash submission must be OTP-verified before submission."
            )

    def on_submit(self):
        """Create the Internal-Transfer Payment Entry that moves the
        physical cash from the driver's in-hand account to the
        warehouse's cash account.

        Per FRD §4.2 controller workflow:
            1. Verify wm_otp_status = Validated.       (before_submit)
            2. Create Payment Entry: Internal Transfer
               paid_from = Driver Cash in Hand — <driver>
               paid_to   = warehouse.warehouse_cash_account
               paid_amount = physical_amount.
            3. Submit the Payment Entry.
            4. Set self.payment_entry.
            5. Reset Employee.current_day_collected_amount.
            6. Close the linked Driver Collection.
            7. Create discrepancy ToDo if needed.

        Steps 5–6 already happen in the validate-OTP API call; this
        method completes steps 2–4 + 7. The PE creation is wrapped in
        a try/except so a missing-account configuration logs an error
        without rolling back the submission — the audit record still
        captures that the WM signed off.
        """
        try:
            self._create_internal_transfer_pe()
        except Exception as e:
            frappe.log_error(
                title=f"Cash Submission {self.name}: PE creation failed",
                message=str(e),
            )

        if self.get("discrepancy_flag") and abs(flt(self.discrepancy_amount)) > 0.01:
            self._create_discrepancy_todo()

        # Mark Driver Collection as Submitted (Closed → Submitted) so the
        # ledger row reflects that cash has physically reached the WH.
        if self.collection:
            try:
                col = frappe.get_doc("Driver Collection", self.collection)
                col.status = "Submitted"
                col.cash_submission = self.name
                col.save(ignore_permissions=True)
            except Exception:
                # The custom `cash_submission` field on Driver Collection
                # may not exist on every site; the status update alone is
                # enough for the audit log.
                pass

    def _create_internal_transfer_pe(self):
        """Resolve the from/to accounts and submit the PE.

        Lookup order for `paid_from`:
            1. `Driver Cash in Hand - <driver>` (per-employee ledger)
            2. `Driver Cash in Hand` (single shared account)
            3. The Company's default cash account (last-resort fallback)
        """
        paid_to = self._resolve_warehouse_cash_account()
        paid_from = self._resolve_driver_cash_account()

        if not paid_to or not paid_from:
            frappe.log_error(
                title=f"Cash Submission {self.name}: account lookup failed",
                message=(
                    f"paid_from={paid_from!r} paid_to={paid_to!r}. "
                    "Configure Warehouse.warehouse_cash_account and a "
                    "'Driver Cash in Hand' Account."
                ),
            )
            return

        company = frappe.db.get_value("Account", paid_to, "company") or \
            frappe.defaults.get_user_default("Company")

        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Internal Transfer"
        pe.company = company
        pe.paid_from = paid_from
        pe.paid_to = paid_to
        pe.paid_amount = flt(self.amount)
        pe.received_amount = flt(self.amount)
        pe.reference_no = self.name
        pe.reference_date = today()
        if self.get("submission_method"):
            mode = self.submission_method
            # ERPNext's Mode of Payment is Cash / Bank Draft / Wire Transfer / etc;
            # map the FRD options conservatively.
            if mode == "UPI":
                pe.mode_of_payment = "UPI" if frappe.db.exists(
                    "Mode of Payment", "UPI"
                ) else "Bank Transfer"
            elif mode == "Bank Transfer":
                pe.mode_of_payment = "Bank Transfer" if frappe.db.exists(
                    "Mode of Payment", "Bank Transfer"
                ) else "Cash"
            else:
                pe.mode_of_payment = "Cash"
        pe.insert(ignore_permissions=True)
        pe.submit()

        self.db_set("payment_entry", pe.name)

    def _resolve_warehouse_cash_account(self):
        if not self.collection:
            return None
        trip = frappe.db.get_value("Driver Collection", self.collection, "trip")
        if not trip:
            return None
        warehouse = frappe.db.get_value("Delivery Trip", trip, "source_warehouse")
        if not warehouse:
            return None
        return frappe.db.get_value(
            "Warehouse", warehouse, "warehouse_cash_account"
        )

    def _resolve_driver_cash_account(self):
        # Convention: per-driver cash-in-hand sub-account named after the
        # employee id. Falls through to a shared account, then to the
        # company default cash account.
        for query in (
            {"account_name": f"Driver Cash in Hand - {self.driver}"},
            {"account_name": "Driver Cash in Hand"},
        ):
            name = frappe.db.get_value("Account", query, "name")
            if name:
                return name

        company = frappe.defaults.get_user_default("Company")
        if company:
            return frappe.db.get_value("Company", company, "default_cash_account")
        return None

    def _create_discrepancy_todo(self):
        """Notify accounts about a cash discrepancy (FRD §7.4)."""
        try:
            frappe.get_doc({
                "doctype": "ToDo",
                "description": (
                    f"Cash Submission {self.name}: discrepancy of "
                    f"{flt(self.discrepancy_amount)}. "
                    f"Driver: {self.driver}. "
                    f"Note: {self.get('discrepancy_note') or '—'}"
                ),
                "reference_type": "Cash Submission",
                "reference_name": self.name,
                "priority": "High",
            }).insert(ignore_permissions=True)
        except Exception:
            pass
