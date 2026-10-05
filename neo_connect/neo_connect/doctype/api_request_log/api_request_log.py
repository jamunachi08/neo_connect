import frappe
from frappe import _
from frappe.model.document import Document


class APIRequestLog(Document):
    pass


@frappe.whitelist()
def reprocess(log_name):
    """Re-run a failed/partial inbound request with its stored payload (duplicates are skipped)."""
    frappe.only_for("System Manager")
    from neo_connect.engine.inbound import process_log

    log = frappe.get_doc("API Request Log", log_name)
    if log.direction != "Inbound" or not log.endpoint or not log.partner:
        frappe.throw(_("Only inbound requests with a known endpoint and partner can be reprocessed"))
    log = process_log(log_name)
    return {"status": log.status, "response": log.response}


def delete_old_logs():
    """Daily: keep logs for `neo_connect_log_days` (site_config, default 90)."""
    days = frappe.conf.get("neo_connect_log_days") or 90
    frappe.db.delete("API Request Log", {"creation": ("<", frappe.utils.add_days(frappe.utils.now(), -days))})
