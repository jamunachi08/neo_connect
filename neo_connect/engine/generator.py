"""
Frappe side of the sample analyser: reads stored samples, fills the suggested mapping, creates or
updates the API Endpoint (and optional custom fields) and validates it with a full dry run.
"""

import json
import re

import frappe
from frappe import _
from frappe import scrub
from frappe.utils import cint, now_datetime

from . import analyzer
from .utils import dumps, parse_json_field

HANDLERS = "neo_connect.handlers.commerce."
MANAGED_SETTING_KEYS = (
    "customer_email_path", "customer_name_paths", "customer_phone_path", "billing_address_path", "address_map",
    "items_path", "shipping_amount_path", "discount_amount_path", "discount_on", "grand_total_path",
    "payment_method_path", "paid_amount_path", "payment_reference_path", "cost_center_path",
)


# ---------------------------------------------------------------------------
# reading samples
# ---------------------------------------------------------------------------

def read_row_text(row):
    if row.attachment:
        file_doc = frappe.get_doc("File", {"file_url": row.attachment})
        content = file_doc.get_content()
        return content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    return row.payload or ""


def load_records(sample):
    records, notes = [], []
    for row in sample.payloads:
        try:
            recs, row_notes = analyzer.parse_payload_text(read_row_text(row))
            row.record_count = len(recs)
            row.parse_note = "\n".join(row_notes) or "OK"
            if row.attachment and row.source == "Pasted":
                row.source = "Uploaded"
            records += recs
            notes += [f"Sample {row.idx}: {n}" for n in row_notes]
        except Exception as e:  # noqa: BLE001
            row.record_count = 0
            row.parse_note = f"ERROR: {e}"
            notes.append(f"Sample {row.idx}: {e}")
    return records, notes


# ---------------------------------------------------------------------------
# target description
# ---------------------------------------------------------------------------

def _fdesc(df):
    return {"fieldname": df.fieldname, "label": df.label, "fieldtype": df.fieldtype, "options": df.options,
            "reqd": cint(df.reqd)}


def describe_target(doctype):
    meta = frappe.get_meta(doctype)
    desc = {"doctype": doctype, "fields": [_fdesc(df) for df in meta.fields if df.fieldname], "tables": {}}
    items = meta.get_field("items")
    if items and items.fieldtype in frappe.model.table_fields:
        cmeta = frappe.get_meta(items.options)
        desc["tables"]["items"] = {"doctype": items.options,
                                   "fields": [_fdesc(df) for df in cmeta.fields if df.fieldname]}
    return desc


# ---------------------------------------------------------------------------
# analyse
# ---------------------------------------------------------------------------

def analyze(sample):
    records, notes = load_records(sample)
    if not records:
        frappe.throw(_("No usable JSON in the samples. Paste or upload at least one payload."))
    platform = frappe.db.get_value("API Partner", sample.partner, "platform")
    res = analyzer.suggest(analyzer.profile(records), describe_target(sample.reference_doctype), platform)
    sample.set("fields", [])
    for r in res["rows"]:
        r = dict(r)
        if isinstance(r.get("value_map"), dict):
            r["value_map"] = dumps(r["value_map"])
        sample.append("fields", r)
    res.pop("rows")
    res["record_count"] = len(records)
    sample.analysis_json = dumps(res)
    mapped = sum(1 for f in sample.fields if f.include)
    unmapped = sum(1 for f in sample.fields if f.role == "Unmapped")
    sample.analysis_notes = "\n".join(
        [f"{len(records)} sample record(s), {len(sample.fields)} fields: {mapped} used, {unmapped} unmapped."]
        + notes + res["notes"])
    sample.status = "Analyzed"
    return res


# ---------------------------------------------------------------------------
# custom fields
# ---------------------------------------------------------------------------

_FIELDTYPE_BY_DATA = {"date": "Date", "datetime": "Datetime", "number": "Float", "integer": "Int",
                      "boolean": "Check"}


def create_custom_field(sample, row):
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field as _ccf

    path = row.external_path
    in_items = path.split(".")[0].endswith("[]") or ".items[]" in path or path.startswith("items[]")
    parts = [p.replace("[]", "") for p in path.split(".")]
    base = parts[-2:] if not in_items else parts[-1:]
    fieldname = "custom_" + scrub("_".join(base))[:50]
    label = " ".join(x.replace("_", " ").title() for x in base)
    fieldtype = _FIELDTYPE_BY_DATA.get((row.data_type or "").split("/")[0], "Data")
    meta = frappe.get_meta(sample.reference_doctype)
    if in_items:
        target_dt = meta.get_field("items").options
        insert_after = "item_name"
    else:
        target_dt = sample.reference_doctype
        section = "custom_neo_connect_section"
        if not frappe.get_meta(target_dt).has_field(section):
            _ccf(target_dt, {"fieldname": section, "fieldtype": "Section Break",
                             "label": f"{sample.partner} Data", "collapsible": 1,
                             "insert_after": meta.fields[-1].fieldname})
        insert_after = section
    if not frappe.get_meta(target_dt).has_field(fieldname):
        _ccf(target_dt, {"fieldname": fieldname, "fieldtype": fieldtype, "label": label,
                         "insert_after": insert_after, "read_only": 0, "allow_on_submit": 1})
    row.erp_field = ("items." if in_items else "") + fieldname
    row.transform = {"Date": "Date", "Datetime": "Datetime", "Float": "Float", "Int": "Integer",
                     "Check": "Check"}.get(fieldtype, "Text")
    row.role = "Field"
    row.include = 1
    row.create_field = 0
    return row.erp_field


# ---------------------------------------------------------------------------
# defaults that can be derived from the company
# ---------------------------------------------------------------------------

def derive_defaults(sample, company, plan):
    meta = frappe.get_meta(sample.reference_doctype)
    d = {}
    if meta.has_field("taxes_and_charges") and company:
        tmpl_dt = meta.get_field("taxes_and_charges").options
        tmpl = frappe.db.get_value(tmpl_dt, {"company": company, "is_default": 1, "disabled": 0}) \
            or frappe.db.get_value(tmpl_dt, {"company": company, "disabled": 0})
        if tmpl:
            d["taxes_and_charges"] = tmpl
    if meta.has_field("set_posting_time"):
        d["set_posting_time"] = 1
    if meta.has_field("disable_rounded_total"):
        d["disable_rounded_total"] = 1
    if meta.has_field("update_stock"):
        d["update_stock"] = 0
    items_dt = meta.get_field("items").options if meta.has_field("items") else None
    if items_dt and frappe.get_meta(items_dt).has_field("warehouse") and company:
        wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
        if not wh or frappe.db.get_value("Warehouse", wh, "company") != company:
            wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "disabled": 0})
        if wh:
            d["items.warehouse"] = wh
    if not plan["settings"].get("cost_center_path") and company:
        cc_field = meta.get_field("cost_center")
        if cc_field and cint(cc_field.reqd):
            cc = frappe.get_cached_value("Company", company, "cost_center")
            if cc:
                d["cost_center"] = cc
    return d


# ---------------------------------------------------------------------------
# generate / update endpoint
# ---------------------------------------------------------------------------

def generate(sample):
    if not sample.analysis_json:
        analyze(sample)
    plan = json.loads(sample.analysis_json)
    partner = frappe.get_doc("API Partner", sample.partner)
    company = partner.company

    created = []
    for row in sample.fields:
        if cint(row.create_field):
            created.append(create_custom_field(sample, row))

    maps = []
    ext_path = None
    for row in sample.fields:
        if row.role == "External ID" and row.include:
            ext_path = row.external_path
        if not (row.include and row.erp_field):
            continue
        maps.append({
            "external_path": row.external_path, "erp_field": row.erp_field, "transform": row.transform or "None",
            "required": cint(row.required), "lookup_doctype": row.lookup_doctype, "lookup_field": row.lookup_field,
            "if_not_found": row.if_not_found or "Error", "source_timezone": row.source_timezone,
            "value_map": row.value_map, "expression": row.expression, "notes": (row.note or "")[:140],
        })
    ext_path = ext_path or plan.get("external_id_path")
    if not ext_path:
        frappe.throw(_("No External ID: set the Role 'External ID' on the field that holds the unique order number."))

    settings = dict(plan["settings"])
    pre = [HANDLERS + h for h in plan["pre_hooks"]]
    post = [HANDLERS + h for h in plan["post_hooks"]]
    defaults = derive_defaults(sample, company, plan)

    slug = (sample.endpoint_slug or f"{scrub(sample.partner)}-{scrub(sample.reference_doctype)}")
    slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
    records_total = plan.get("record_count") or 0
    first_record = _first_record(sample)

    if sample.endpoint and frappe.db.exists("API Endpoint", sample.endpoint):
        ep = frappe.get_doc("API Endpoint", sample.endpoint)
        old_defaults = parse_json_field(ep.defaults, {}) or {}
        defaults.update(old_defaults)                      # the user's own defaults win
        old_settings = parse_json_field(ep.handler_settings, {}) or {}
        for k, v in old_settings.items():
            if k in MANAGED_SETTING_KEYS:
                continue
            if k not in settings and "<" in json.dumps(v, ensure_ascii=False):
                continue                                   # unfilled placeholder that is no longer needed
            settings[k] = v                                # keep the user's business settings
        pre = _merge_lines(pre, ep.pre_process_hook)
        post = _merge_lines(post, ep.post_process_hook)
        ep.set("field_maps", [])
    else:
        if frappe.db.exists("API Endpoint", slug):
            frappe.throw(_("Endpoint {0} already exists. Set a different Endpoint Code.").format(slug))
        ep = frappe.new_doc("API Endpoint")
        ep.slug = slug
        ep.endpoint_title = sample.title
        ep.direction = "Inbound"
        ep.enabled = 1
        ep.on_duplicate = "Skip"
        ep.append("allowed_partners", {"partner": sample.partner})

    ep.reference_doctype = sample.reference_doctype
    ep.external_id_path = ext_path
    ep.document_action = sample.document_action or "Save as Draft"
    ep.description = ep.description or f"Generated from Payload Sample {sample.name} ({records_total} sample record(s))."
    ep.pre_process_hook = "\n".join(pre)
    ep.post_process_hook = "\n".join(post)
    ep.defaults = dumps(defaults)
    ep.handler_settings = dumps(settings)
    ep.sample_payload = dumps(first_record) if first_record else ep.sample_payload
    for m in maps:
        ep.append("field_maps", m)
    ep.save() if not ep.is_new() else ep.insert()

    sample.endpoint = ep.name
    sample.status = "Endpoint Generated"
    sample.validation_report = validate(sample, ep, created, settings)
    return ep.name


def _merge_lines(generated, existing):
    out = list(generated)
    for line in (existing or "").splitlines():
        if line.strip() and line.strip() not in out:
            out.append(line.strip())
    return out


def _first_record(sample):
    for row in sample.payloads:
        try:
            recs, _n = analyzer.parse_payload_text(read_row_text(row))
            if recs:
                return recs[0]
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# validation report
# ---------------------------------------------------------------------------

_MANDATORY_RE = re.compile(r"\[([^,\]]+),\s*[^\]]*\]:\s*(.+)")


def validate(sample, ep, created, settings):
    from .inbound import Processor

    lines = [f"Endpoint: {ep.name}", f"URL: {ep.endpoint_url}", ""]
    if created:
        lines.append("Custom fields created: " + ", ".join(created))
    todo = []
    for key, val in settings.items():
        if "<" in json.dumps(val, ensure_ascii=False):
            todo.append(f"Handler Settings '{key}': replace the placeholder values (in angle brackets)")
    for row in sample.fields:
        if row.include and row.value_map and "<" in row.value_map:
            todo.append(f"Field {row.erp_field}: fill the Value Map with the allowed options")
    defaults = parse_json_field(ep.defaults, {}) or {}
    if frappe.get_meta(ep.reference_doctype).has_field("taxes_and_charges") and not defaults.get("taxes_and_charges"):
        todo.append("Document Defaults: no Sales Taxes template found for the company; set 'taxes_and_charges'")

    partner = frappe.get_doc("API Partner", sample.partner)
    results = []
    records = []
    for row in sample.payloads:
        try:
            records += analyzer.parse_payload_text(read_row_text(row))[0]
        except Exception:  # noqa: BLE001
            pass
    for i, rec in enumerate(records[:5]):
        sp = f"cc_validate_{i}"
        frappe.db.savepoint(sp)
        try:
            out = Processor(ep, partner).process_payload(rec, dry_run=True)
            results.append(out["results"][0])
        except Exception as e:  # noqa: BLE001
            results.append({"status": "Failed", "error": str(e)})
        finally:
            frappe.db.rollback(save_point=sp)
            frappe.clear_messages()

    lines.append("Dry run with the sample payload(s):")
    missing = {}
    for i, r in enumerate(results, 1):
        lines.append(f"  Sample {i}: {r.get('status')}" + (f" - {r.get('error')}" if r.get("error") else ""))
        if " not found for " in (r.get("error") or ""):
            lines.append("     fix: create that record in ERPNext, translate it with a Value Map, "
                         "or set the row's 'If Not Found' to 'Use Value'/'Skip'")
        for w in r.get("warnings") or []:
            lines.append(f"     warning: {w}")
        m = _MANDATORY_RE.search(r.get("error") or "")
        if m:
            for fn in [x.strip() for x in m.group(2).split(",") if x.strip()]:
                missing.setdefault(fn, m.group(1).strip())

    if missing:
        lines += ["", "Mandatory fields still without a value (from ERPNext's own validation):"]
        meta = frappe.get_meta(ep.reference_doctype)
        items_dt = meta.get_field("items").options if meta.has_field("items") else None
        for fn in missing:
            df = meta.get_field(fn)
            where = ""
            if not df and items_dt:
                df = frappe.get_meta(items_dt).get_field(fn)
                where = "items."
            label = (df.label if df else fn) or fn
            hint = ""
            if df and df.fieldtype == "Select":
                hint = "options: " + " | ".join(o for o in (df.options or "").split("\n") if o)
            elif df and df.fieldtype == "Link":
                hint = f"a {df.options} name"
            lines.append(f"  - {where}{fn} ({label}): {hint}")
            mapped = next((r for r in sample.fields if r.include and r.erp_field == where + fn), None)
            if mapped and mapped.transform == "Value Map":
                lines.append(f"      fix: fill the Value Map of {where}{fn} (Fields table) so every payload value "
                             f"becomes one of the options, then Update Endpoint")
            elif mapped:
                lines.append(f"      mapped from '{mapped.external_path}' (present in {mapped.presence} samples): "
                             f"add \"{where}{fn}\": \"VALUE\" to Document Defaults for orders without it")
            else:
                cand = _best_candidate(sample, df) if df else None
                lines.append(f"      fix: add \"{where}{fn}\": \"VALUE\" to Document Defaults"
                             + (f", or map it from '{cand}' in the Fields table" if cand else ""))
    if todo:
        lines += ["", "To do before go-live:"] + [f"  - {t}" for t in todo]
    if results and all(r.get("status") == "Validated" for r in results) and not todo:
        lines += ["", "READY: all samples validate. Share the Partner Guide from the endpoint."]
    return "\n".join(lines)


def _best_candidate(sample, df):
    best, score = None, 0
    for row in sample.fields:
        if row.external_path and row.role in ("Unmapped", "Ignore"):
            s = analyzer.match_score(row.external_path, {"fieldname": df.fieldname, "label": df.label})
            if s > score:
                best, score = row.external_path, s
    return best if score >= 0.3 else None


# ---------------------------------------------------------------------------
# samples pushed by partners through the API
# ---------------------------------------------------------------------------

def store_partner_sample(partner, message_type, raw_text, label=None):
    analyzer.parse_payload_text(raw_text)   # raises if not JSON
    name = frappe.db.get_value("API Payload Sample", {"partner": partner, "message_type": message_type})
    if name:
        sample = frappe.get_doc("API Payload Sample", name)
    else:
        ref_dt = message_type if frappe.db.exists("DocType", message_type) else "Sales Order"
        sample = frappe.get_doc({"doctype": "API Payload Sample", "title": f"{partner} - {message_type}",
                                 "partner": partner, "message_type": message_type, "reference_doctype": ref_dt})
    sample.append("payloads", {"label": label or f"API {now_datetime():%Y-%m-%d %H:%M}", "payload": raw_text,
                               "source": "Partner API", "received_on": now_datetime()})
    sample.flags.ignore_permissions = True
    sample.save() if not sample.is_new() else sample.insert()
    return sample
