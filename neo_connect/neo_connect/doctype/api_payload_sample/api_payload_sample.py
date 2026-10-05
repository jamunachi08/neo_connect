import frappe
from frappe import _
from frappe.model.document import Document

from neo_connect.engine import generator


class APIPayloadSample(Document):
    def validate(self):
        if not self.payloads:
            return
        for row in self.payloads:
            if not (row.attachment or (row.payload or "").strip()):
                frappe.throw(_("Sample row {0}: paste JSON or upload a file").format(row.idx))


@frappe.whitelist()
def analyze(name):
    """Parse all samples and suggest the mapping (overwrites the Fields table)."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    generator.analyze(doc)
    doc.save()
    return {"fields": len(doc.fields), "notes": doc.analysis_notes}


@frappe.whitelist()
def generate_endpoint(name):
    """Create or update the API Endpoint from the Fields table, then dry-run every sample."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    endpoint = generator.generate(doc)
    doc.save()
    return {"endpoint": endpoint, "report": doc.validation_report}
