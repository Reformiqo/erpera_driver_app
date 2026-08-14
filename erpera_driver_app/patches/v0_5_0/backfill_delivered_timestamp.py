"""Backfill Delivery Note.custom_delivered_timestamp on historical orders.

The field was added part-way through the project, so every order delivered
before that carries NULL. Analytics falls back to `Delivery Note.modified` for
those, which is not a delivery time at all — it moves on every later edit, so
punctuality measured against it drifts further from the truth the longer an
order sits in the system.

Three earlier records already hold the real moment. In descending order of
how directly they witness the delivery:

  1. Delivery Attempt Log, attempt_status = "Delivered"
     `delivery._do_update_status` inserts a row on every transition, before
     touching the Delivery Note. Its `creation` is the delivery itself.

  2. Delivery Note.cod_collection_timestamp
     Stamped by `order.submit_proof` when the driver records the cash. COD
     orders only, and only on the legacy proof-of-delivery path.

  3. The linked Sales Invoice's `creation`
     Both proof-of-delivery paths create and submit the invoice inside the
     same transaction as the status flip, so it is within seconds of it.

Only NULL values are written, so this never overwrites a real stamp and is
safe to re-run. Orders that match none of the three keep their NULL and go on
falling back to `modified` — there is nothing better to give them, and a
fabricated timestamp would be worse than an honest gap.
"""

import frappe


def execute():
	if not frappe.db.has_column("Delivery Note", "custom_delivered_timestamp"):
		return

	# 1. Delivery Attempt Log — the transition record itself.
	frappe.db.sql("""
		UPDATE `tabDelivery Note` dn
		  JOIN (
		        SELECT delivery_note, MIN(creation) AS delivered_at
		          FROM `tabDelivery Attempt Log`
		         WHERE attempt_status = 'Delivered'
		           AND delivery_note IS NOT NULL
		         GROUP BY delivery_note
		       ) al ON al.delivery_note = dn.name
		   SET dn.custom_delivered_timestamp = al.delivered_at
		 WHERE dn.cowberry_delivery_status = 'Delivered'
		   AND dn.custom_delivered_timestamp IS NULL
	""")

	# 2. COD collection stamp — legacy proof-of-delivery path.
	if frappe.db.has_column("Delivery Note", "cod_collection_timestamp"):
		frappe.db.sql("""
			UPDATE `tabDelivery Note`
			   SET custom_delivered_timestamp = cod_collection_timestamp
			 WHERE cowberry_delivery_status = 'Delivered'
			   AND custom_delivered_timestamp IS NULL
			   AND cod_collection_timestamp IS NOT NULL
		""")

	# 3. Sales Invoice creation — within seconds of the status flip.
	frappe.db.sql("""
		UPDATE `tabDelivery Note` dn
		  JOIN (
		        SELECT sii.delivery_note, MIN(si.creation) AS invoiced_at
		          FROM `tabSales Invoice Item` sii
		          JOIN `tabSales Invoice` si ON si.name = sii.parent
		         WHERE si.docstatus = 1
		           AND sii.delivery_note IS NOT NULL
		           AND sii.delivery_note != ''
		         GROUP BY sii.delivery_note
		       ) inv ON inv.delivery_note = dn.name
		   SET dn.custom_delivered_timestamp = inv.invoiced_at
		 WHERE dn.cowberry_delivery_status = 'Delivered'
		   AND dn.custom_delivered_timestamp IS NULL
	""")

	remaining = frappe.db.count("Delivery Note", {
		"cowberry_delivery_status": "Delivered",
		"custom_delivered_timestamp": ("is", "not set"),
	})
	frappe.logger("erpera_driver_app").info(
		f"backfill_delivered_timestamp: {remaining} delivered notes still "
		"have no timestamp and will fall back to `modified`."
	)
