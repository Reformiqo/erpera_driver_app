from . import __version__ as app_version

app_name = "erpera_driver_app"
app_title = "Erpera Driver App"
app_publisher = "Reformiqo"
app_description = "Erpera Driver App ERP"
app_email = "consultant.reformiqo@gmail.com"
app_license = "MIT"

# Fixtures — export only custom fields belonging to Erpera Driver App
fixtures = [
    {"dt": "Custom Field", "filters": [["module", "=", "Erpera Driver App"]]},
    {"dt": "Property Setter", "filters": [["module", "=", "Erpera Driver App"]]}
]

# Document Events
doc_events = {
    "Delivery Note": {
        "on_submit": "erpera_driver_app.api.delivery.on_submit_delivery_note",
        "on_cancel": [
            "erpera_driver_app.api.delivery.on_cancel_delivery_note",
            # Notification event 4 — order cancelled
            "erpera_driver_app.notification_events.on_dn_cancel",
        ],
        # Notification event 3 — ops moved the delivery date
        "on_update_after_submit": "erpera_driver_app.notification_events.on_dn_update_after_submit",
    },
    "Customer": {
        "before_save": "erpera_driver_app.api.wallet.guard_direct_wallet_balance_writes",
        "validate": "erpera_driver_app.api.wallet.guard_direct_wallet_balance_writes",
    },
    "Delivery Trip": {
        "validate": "erpera_driver_app.api.delivery_trip.validate",
        # Notification events 1, 2 and 4 — assigned, re-optimised, stops pulled.
        # Both update hooks: a Delivery Trip is submittable and the manager
        # edits stops on either side of submit.
        "after_insert": "erpera_driver_app.notification_events.on_trip_insert",
        "on_update": "erpera_driver_app.notification_events.on_trip_update",
        "on_update_after_submit": "erpera_driver_app.notification_events.on_trip_update",
    },
    "Communication": {
        # Notification event 7 — someone other than the driver wrote on one of
        # their orders.
        "after_insert": "erpera_driver_app.notification_events.on_communication_insert",
    },
}

# Scheduled Tasks
scheduler_events = {
    "daily": [
        "erpera_driver_app.api.collection.daily_reset_driver_totals",
    ],
    "hourly": [
        "erpera_driver_app.api.payment.poll_pending_razorpay_orders",
    ],
}

# Permission query conditions for row-level access
permission_query_conditions = {
    "Cash Submission": "erpera_driver_app.permissions.cash_submission_query",
    "Driver Collection": "erpera_driver_app.permissions.driver_collection_query",
    "Wallet Transaction": "erpera_driver_app.permissions.wallet_transaction_query",
    "Delivery Attempt Log": "erpera_driver_app.permissions.delivery_attempt_log_query",
    "Reschedule Log": "erpera_driver_app.permissions.reschedule_log_query",
}

# After install hook
after_install = "erpera_driver_app.install.after_install"

# Website generators
# website_generators = []

# Jinja environment — add custom methods if needed
# jinja = {}
