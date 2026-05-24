import frappe
from frappe.model.document import Document

class CowberryCashSubmission(Document):
    def before_submit(self):
        if self.status != "Verified":
            frappe.throw("Cash submission must be OTP-verified before submission.")
