import json
import os
import re

import frappe
from frappe import _
from frappe.model.document import Document

from neo_connect.engine.mapper import split_erp_field, split_list_path
from neo_connect.engine.utils import (
    dumps, endpoint_url, get_hooks, parse_json_field,
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class APIEndpoint(Document):
    @property
    def endpoint_url(self):
        if not self.slug:
            return ""
        return endpoint_url(self.slug, self.direction)

    def validate(self):
        self.slug = (self.slug or "").strip().lower()
        if not SLUG_RE.match(self.slug):
            frappe.throw(_("Endpoint Code must be lowercase letters, digits and hyphens, e.g. magento-sales-invoice"))

        for label, value in (("Defaults", self.defaults), ("Row Filters", self.row_filters),
                             ("Handler Settings", self.handler_settings),
                             ("Base Filters", self.base_filters), ("Sample Payload", self.sample_payload)):
            parse_json_field(value, None, _(label))

        for path in get_hooks(self.pre_process_hook) + get_hooks(self.post_process_hook) + (
                [self.server_method] if self.direction == "Outbound" and self.outbound_source == "Server Method" else []):
            try:
                frappe.get_attr(path)
            except Exception:
                frappe.throw(_("Cannot import hook/method {0}").format(path))

        self.validate_field_maps()

    def validate_field_maps(self):
        meta = frappe.get_meta(self.reference_doctype) if self.reference_doctype else None
        for row in self.field_maps:
            parse_json_field(row.value_map, None, _("Value Map in row {0}").format(row.idx))
            if row.transform not in ("Static",) and not row.external_path and self.direction == "Inbound" \
                    and row.transform != "Expression":
                frappe.throw(_("Row {0}: Partner JSON Path is required for transform {1}").format(
                    row.idx, row.transform))
            if row.transform == "Lookup" and not (row.lookup_doctype or row.lookup_endpoint):
                frappe.throw(_("Row {0}: Lookup needs a Lookup DocType or Lookup Endpoint").format(row.idx))
            if row.transform == "Expression" and not row.expression:
                frappe.throw(_("Row {0}: Expression is empty").format(row.idx))
            if self.direction == "Outbound" and not row.external_path:
                frappe.throw(_("Row {0}: Partner JSON Path (output key) is required").format(row.idx))
            if meta:
                table, field = split_erp_field(row.erp_field)
                target = meta
                if table:
                    df = meta.get_field(table)
                    if not df or df.fieldtype not in frappe.model.table_fields:
                        frappe.throw(_("Row {0}: {1} is not a child table of {2}").format(
                            row.idx, table, self.reference_doctype))
                    target = frappe.get_meta(df.options)
                    if self.direction == "Outbound" and split_list_path(row.external_path or "")[0] is None:
                        frappe.throw(_("Row {0}: child fields need an output path like lines[].{1}").format(
                            row.idx, field))
                if field not in ("name", "modified", "creation", "docstatus", "owner", "idx") \
                        and not target.has_field(field):
                    frappe.throw(_("Row {0}: field {1} not found in {2}").format(row.idx, field, target.name))
        if self.direction == "Outbound" and self.outbound_source == "DocType Query" and not self.field_maps:
            frappe.throw(_("Add at least one Field Map row to define the response"))


# ---------------------------------------------------------------------------
# Form actions
# ---------------------------------------------------------------------------

@frappe.whitelist()
def test_mapping(endpoint, payload=None):
    """Dry-map the sample payload (no DB writes). Returns the document ERPNext would receive."""
    frappe.only_for("System Manager")
    from neo_connect.engine.inbound import Processor
    from neo_connect.engine.mapper import extract_records

    ep = frappe.get_doc("API Endpoint", endpoint)
    if ep.direction != "Inbound":
        frappe.throw(_("Test Mapping is for inbound endpoints. For outbound, use Preview Response."))
    data = parse_json_field(payload or ep.sample_payload, None, _("Sample Payload"))
    if data is None:
        frappe.throw(_("Paste a Sample Payload first"))
    partner_name = ep.allowed_partners[0].partner if ep.allowed_partners else None
    partner = frappe.get_doc("API Partner", partner_name) if partner_name else frappe._dict(
        name="(none)", defaults=None, handler_settings=None, company=None)

    out = []
    savepoint = "cc_test_mapping"
    frappe.db.savepoint(savepoint)
    try:
        proc = Processor(ep, partner)
        for rec in extract_records(data, ep.records_path)[:5]:
            try:
                out.append(proc.preview(rec))
            except Exception as e:  # noqa: BLE001
                out.append({"errors": [str(e)]})
    finally:
        # pre-process hooks may create customers etc. - undo everything
        frappe.db.rollback(save_point=savepoint)
    return out


@frappe.whitelist()
def preview_response(endpoint, query=None):
    frappe.only_for("System Manager")
    from neo_connect.engine.outbound import handle_pull

    ep = frappe.get_doc("API Endpoint", endpoint)
    if ep.direction != "Outbound":
        frappe.throw(_("Preview Response is for outbound endpoints"))
    args = frappe._dict(parse_json_field(query, {}, "Query") or {})
    args.setdefault("limit", 5)
    partner = frappe.get_doc("API Partner", ep.allowed_partners[0].partner) if ep.allowed_partners \
        else frappe._dict(name="(none)", handler_settings=None)
    return handle_pull(ep, partner, args)


@frappe.whitelist()
def partner_guide(endpoint):
    """Markdown integration guide for this endpoint, ready to send to the vendor."""
    frappe.only_for("System Manager")
    ep = frappe.get_doc("API Endpoint", endpoint)
    base = frappe.utils.get_url()
    url = ep.endpoint_url
    sample = parse_json_field(ep.sample_payload, None) if ep.sample_payload else None
    lines = [
        f"# {ep.endpoint_title}",
        "",
        ep.description or "",
        "",
        "## Endpoint",
        "",
        f"`{'POST' if ep.direction == 'Inbound' else 'GET'} {url}`",
        "",
        "## Authentication",
        "",
        "Send this header with every request (key and secret are shared separately):",
        "",
        "```",
        "Authorization: token <api_key>:<api_secret>",
        "Content-Type: application/json" if ep.direction == "Inbound" else "Accept: application/json",
        "```",
        "",
        f"Connectivity check: `GET {base}/api/method/neo_connect.api.ping`",
        "",
    ]
    partners = [p.partner for p in ep.allowed_partners]
    if any(frappe.db.get_value("API Partner", p, "require_signature") for p in partners):
        lines += [
            "### Request signing",
            "",
            "Also send `X-Signature: <hex HMAC-SHA256 of the exact raw request body, using the signing secret>`.",
            "",
        ]

    if ep.direction == "Inbound":
        lines += [
            "## Request body",
            "",
            (f"Records are read from `{ep.records_path}`. " if ep.records_path else
             "The body can be a single record or a JSON array of records. ")
            + f"Up to {ep.max_records_per_request or 100} records per call.",
            "",
            f"Each record must contain a unique id at `{ep.external_id_path}`. Re-sending the same id is safe: "
            f"it is reported as `Duplicate` and not created twice.",
            "",
            "| Field in your JSON | Required | Notes |",
            "|---|---|---|",
        ]
        for r in ep.field_maps:
            if r.transform == "Static" or not r.external_path:
                continue
            note = r.notes or ""
            if r.transform in ("Date", "Datetime"):
                note = (note + " Date, e.g. 2026-09-24 or 2026-09-24 10:15:00").strip()
            if r.transform == "Value Map":
                vals = [k for k in parse_json_field(r.value_map, {}) if k != "*"]
                note = (note + " One of: " + ", ".join(vals)).strip()
            lines.append(f"| `{r.external_path}` | {'Yes' if r.required else 'No'} | {note} |")
        lines += [
            "",
            "## Test without creating anything",
            "",
            f"Add `&dry_run=1` to the URL. ERPNext validates the record fully and rolls it back.",
            "",
            "## Response",
            "",
            "```json",
            dumps({"message": {
                "request_id": "a1b2c3d4e5",
                "status": "Success",
                "summary": {"total": 1, "Created": 1},
                "results": [{"index": 0, "external_id": "000001045", "status": "Created",
                             "doctype": ep.reference_doctype, "name": "ACC-SINV-2026-00012"}],
            }}),
            "```",
            "",
            "| HTTP | status | Meaning |",
            "|---|---|---|",
            "| 200 | Success / Duplicate | All records created, or already imported |",
            "| 202 | Queued | Accepted; processed in background |",
            "| 207 | Partial | Some records failed; see `results[].error`. Fix and resend only those |",
            "| 400 | Rejected | Invalid JSON / missing endpoint |",
            "| 401/403 | Rejected | Bad credentials, signature, IP or permission |",
            "| 422 | Failed | No record could be created; see `results[].error` |",
            "| 429 | Rejected | Rate limit exceeded; retry after a minute |",
            "| 5xx | Failed | Server error; retry with the same payload (safe) |",
            "",
            "Keep the `request_id` in your logs; we can look up every call with it.",
        ]
        if sample:
            lines += ["", "## Example request", "", "```bash",
                      f"curl -X POST '{url}' \\",
                      "  -H 'Authorization: token API_KEY:API_SECRET' \\",
                      "  -H 'Content-Type: application/json' \\",
                      "  -d @payload.json", "```", "", "`payload.json`:", "", "```json",
                      dumps(sample), "```"]
    else:
        allowed = [a.strip() for a in (ep.allowed_filters or "").splitlines() if a.strip()]
        lines += [
            "## Query parameters",
            "",
            "| Parameter | Description |",
            "|---|---|",
            "| `page` | Page number, starting at 1 |",
            f"| `limit` | Page size (default {ep.default_page_length or 50}, max {ep.max_page_length or 500}) |",
            "| `modified_since` | Only records changed after this time, e.g. `2026-09-24 00:00:00`. "
            "Store `next_modified_since` from each response and send it next time |",
        ]
        for a in allowed:
            lines.append(f"| `{a}` | Exact match filter |")
        if allowed:
            lines.append('| `filters` | JSON array, e.g. `[["' + allowed[0] + '","in",["A","B"]]]` |')
        lines += ["", "## Response fields", "", "| Field | Source |", "|---|---|"]
        for r in ep.field_maps:
            lines.append(f"| `{r.external_path}` | {r.notes or r.erp_field} |")
        lines += ["", "Response shape:", "", "```json",
                  dumps({"message": {"request_id": "...", "data": ["..."], "page": 1, "page_length": 50,
                                     "has_more": False, "next_modified_since": "2026-09-24 10:15:00"}}),
                  "```", "", "## Example", "", "```bash",
                  f"curl '{url}&page=1&limit=100' -H 'Authorization: token API_KEY:API_SECRET'", "```"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

@frappe.whitelist()
def list_presets():
    out = []
    preset_dir = frappe.get_app_path("neo_connect", "presets")
    for fn in sorted(os.listdir(preset_dir)):
        if fn.endswith(".json"):
            with open(os.path.join(preset_dir, fn)) as fh:
                d = json.load(fh)
            out.append({"file": fn, "title": d.get("endpoint_title"), "direction": d.get("direction"),
                        "slug": d.get("slug"), "platform": d.get("_platform", "Any")})
    return out


@frappe.whitelist()
def load_preset(preset, partner=None, slug=None):
    """Create an API Endpoint from presets/<preset>. Returns the new endpoint name."""
    frappe.only_for("System Manager")
    fn = os.path.basename(preset)
    path = os.path.join(frappe.get_app_path("neo_connect", "presets"), fn)
    if not os.path.exists(path):
        frappe.throw(_("Preset {0} not found").format(fn))
    with open(path) as fh:
        data = json.load(fh)
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    for key in ("defaults", "row_filters", "handler_settings", "base_filters", "sample_payload"):
        if isinstance(data.get(key), (dict, list)):
            data[key] = dumps(data[key])
    for row in data.get("field_maps", []):
        if isinstance(row.get("value_map"), dict):
            row["value_map"] = dumps(row["value_map"])
    if slug:
        data["slug"] = slug
    if frappe.db.exists("API Endpoint", data["slug"]):
        frappe.throw(_("Endpoint {0} already exists. Give a different code.").format(data["slug"]))
    if partner:
        data["allowed_partners"] = [{"partner": partner}]
    doc = frappe.get_doc(dict(data, doctype="API Endpoint"))
    doc.flags.ignore_links = True
    doc.insert()
    return doc.name
