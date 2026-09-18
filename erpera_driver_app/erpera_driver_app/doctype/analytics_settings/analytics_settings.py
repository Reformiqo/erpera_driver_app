import frappe
from frappe.model.document import Document
from frappe.utils import flt


class AnalyticsSettings(Document):
    def validate(self):
        self._validate_score_weights()
        self._validate_heatmap_hours()

    def _validate_score_weights(self):
        """The four weights are shares of one score, so they have to add to 100.

        Letting them add to anything else silently rescales the whole gauge:
        weights summing to 50 cap every driver at 50/100, and weights summing
        to 200 let a driver score 200. Neither is visible on the screen — it
        just looks like performance changed.
        """
        total = sum(flt(self.get(f)) for f in (
            "on_time_weight", "success_weight", "cod_weight", "comms_weight"))
        if abs(total - 100) > 0.01:
            frappe.throw(
                f"Performance score weights must add up to 100%, not {total:g}%. "
                f"On-Time {flt(self.on_time_weight):g} + Success {flt(self.success_weight):g} "
                f"+ COD {flt(self.cod_weight):g} + Comms {flt(self.comms_weight):g}.",
                title="Score weights do not balance",
            )

    def _validate_heatmap_hours(self):
        """The three cut-offs carve the day into AM / Mid / PM / Eve in order.

        Out-of-order values don't error anywhere downstream — they just make a
        slot unreachable, so a whole row of the heatmap stays empty forever.
        """
        morning = int(self.heatmap_morning_end_hour or 0)
        midday = int(self.heatmap_midday_end_hour or 0)
        afternoon = int(self.heatmap_afternoon_end_hour or 0)
        if not (0 < morning < midday < afternoon <= 24):
            frappe.throw(
                "Heatmap hours must increase through the day and sit between 1 and 24 "
                f"— got morning {morning}, midday {midday}, afternoon {afternoon}.",
                title="Heatmap hours out of order",
            )
