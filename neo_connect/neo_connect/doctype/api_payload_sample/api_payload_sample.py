import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from neo_connect.engine import generator


class APIPayloadSample(Document):
    def validate(self):
        for row in self.payloads:
            if not (row.attachment or (row.payload or "").strip()):
                frappe.throw(_("Sample row {0}: paste JSON or upload a file").format(row.idx))
        self.prepare_gap_if_needed()

    def prepare_gap_if_needed(self):
        """Prepare the gap as soon as samples are added or changed (form, upload or partner API)."""
        if not cint(self.auto_analyze) or not self.payloads or self.flags.skip_auto_analyze:
            return
        if self.payload_signature == generator.payload_signature(self):
            return
        try:
            generator.analyze(self)
            if not self.flags.from_partner_api:
                frappe.msgprint(_("Gap prepared: {0} fields to create, {1} mapped automatically. "
                                  "Click Review & Confirm.").format(
                    sum(1 for f in self.fields if f.gap_status == "Create Field"),
                    sum(1 for f in self.fields if f.gap_status in ("Existing Field", "Customer Field"))),
                    alert=True, indicator="green")
        except Exception as e:  # noqa: BLE001 - never block saving a sample
            self.analysis_notes = f"Could not prepare the gap automatically: {e}"
            self.payload_signature = generator.payload_signature(self)


@frappe.whitelist()
def analyze(name):
    """Parse all samples and suggest the mapping (reviewed rows are kept)."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    generator.analyze(doc)
    doc.flags.skip_auto_analyze = True
    doc.save()
    return {"fields": len(doc.fields), "notes": doc.analysis_notes}


@frappe.whitelist()
def get_review(name):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    if not doc.fields:
        generator.analyze(doc)
        doc.flags.skip_auto_analyze = True
        doc.save()
    return generator.review_data(doc)


@frappe.whitelist()
def confirm_review(name, decisions):
    """Apply the user's decisions, create the confirmed fields, map them and build the endpoint."""
    frappe.only_for("System Manager")
    decisions = json.loads(decisions) if isinstance(decisions, str) else decisions
    doc = frappe.get_doc("API Payload Sample", name)
    out = generator.apply_review(doc, decisions)
    doc.flags.skip_auto_analyze = True
    doc.save()
    return out


@frappe.whitelist()
def undo_created_fields(name):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    removed = generator.remove_created_fields(doc)
    if doc.endpoint and frappe.db.exists("API Endpoint", doc.endpoint):
        generator.generate(doc)            # rebuild the endpoint without the removed fields
    doc.status = "Gap Ready"
    doc.flags.skip_auto_analyze = True
    doc.save()
    return removed


@frappe.whitelist()
def generate_endpoint(name):
    """Create or update the API Endpoint from the Fields table, then dry-run every sample."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    endpoint = generator.generate(doc)
    doc.flags.skip_auto_analyze = True
    doc.save()
    return {"endpoint": endpoint, "report": doc.validation_report}


@frappe.whitelist()
def get_gap_report(name):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    rep = generator.gap_report(doc)
    return {"html": generator.gap_report_html(rep), "summary": rep["summary"]}


@frappe.whitelist()
def download_gap_report(name):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    rep = generator.gap_report(doc)
    frappe.response["filename"] = f"Gap Report - {frappe.scrub(doc.title)}.xlsx"
    frappe.response["filecontent"] = generator.gap_report_xlsx(rep)
    frappe.response["type"] = "binary"


@frappe.whitelist()
def create_fields(name):
    """Create the custom fields ticked in the Fields table (without generating the endpoint)."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Payload Sample", name)
    created = generator.create_ticked_fields(doc)
    if not created:
        frappe.throw(_("Tick 'Create Field' on the rows you want to create"))
    doc.flags.skip_auto_analyze = True
    doc.save()
    return created
