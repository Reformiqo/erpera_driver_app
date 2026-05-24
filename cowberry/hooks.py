from . import __version__ as app_version

app_name = "cowberry"
app_title = "Cowberry"
app_publisher = "Reformiqo"
app_description = "Cowberry Driver App ERP"
app_email = "consultant.reformiqo@gmail.com"
app_license = "MIT"

# Fixtures — export only custom fields belonging to Cowberry App
fixtures = [
    {"dt": "Custom Field", "filters": [["module", "=", "Cowberry App"]]}
]

# Document Events
doc_events = {
    "Delivery Note": {
        "on_submit": "cowberry.api.delivery.on_submit_delivery_note",
        "on_cancel": "cowberry.api.delivery.on_cancel_delivery_note",
    },
    "Customer": {
        "before_save": "cowberry.api.wallet.guard_direct_wallet_balance_writes",
        "validate": "cowberry.api.wallet.guard_direct_wallet_balance_writes",
    },
}

# Scheduled Tasks
scheduler_events = {
    "daily": [
        "cowberry.api.collection.daily_reset_driver_totals",
    ],
    "hourly": [
        "cowberry.api.payment.poll_pending_razorpay_orders",
    ],
}

# Permission query conditions for row-level access
permission_query_conditions = {
    "Cowberry Cash Submission": "cowberry.permissions.cash_submission_query",
    "Cowberry Driver Collection": "cowberry.permissions.driver_collection_query",
    "Cowberry Wallet Transaction": "cowberry.permissions.wallet_transaction_query",
    "Cowberry Delivery Attempt Log": "cowberry.permissions.delivery_attempt_log_query",
    "Cowberry Reschedule Log": "cowberry.permissions.reschedule_log_query",
}

# After install hook
after_install = "cowberry.install.after_install"

# Website generators
# website_generators = []

# Jinja environment — add custom methods if needed
# jinja = {}
