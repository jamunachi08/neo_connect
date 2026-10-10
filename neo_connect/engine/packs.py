"""
Integration Packs: a complete, tested integration in one JSON file.

A pack contains the endpoint (field map, handlers, settings), the custom fields it needs, the Select
options and Modes of Payment it uses, and sample payloads. Install it on any site in one step;
company-specific values (tax template, warehouse, cost center, accounts) are taken from the partner's
company at install time. Export any working endpoint as a pack to reuse it for another partner or site.
"""

import json
import os

import frappe
from frappe import _
from frappe.utils import cint

from .options import ensure_select_option
from .utils import dumps, parse_json_field

PACK_VERSION = 1
PLACEHOLDERS = {
    "taxes_and_charges": "{{company.default_sales_tax_template}}",
    "items.warehouse": "{{company.default_warehouse}}",
    "cost_center": "{{company.cost_center}}",
}


# ---------------------------------------------------------------------------
# bundled packs
# ---------------------------------------------------------------------------

def pack_dir():
    return frappe.get_app_path("neo_connect", "packs")


def list_packs():
    out = []
    for fn in sorted(os.listdir(pack_dir())):
        if fn.endswith(".json"):
            with open(os.path.join(pack_dir(), fn)) as fh:
                p = json.load(fh)
            out.append({"file": fn, "pack": p.get("pack"), "platform": p.get("platform"),
                        "description": p.get("description"), "endpoint": p["endpoint"]["slug"],
                        "doctype": p["endpoint"]["reference_doctype"]})
    return out


def load_bundled(fn):
    path = os.path.join(pack_dir(), os.path.basename(fn))
    if not os.path.exists(path):
        frappe.throw(_("Pack {0} not found").format(fn))
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

_EP_KEYS = ("endpoint_title", "slug", "direction", "reference_doctype", "description", "records_path",
            "external_id_path", "on_duplicate", "max_records_per_request", "document_action", "process_async",
            "ignore_permissions", "row_filters", "pre_process_hook", "post_process_hook", "outbound_source",
            "server_method", "base_filters", "allowed_filters", "order_by", "default_page_length", "max_page_length")
_MAP_KEYS = ("external_path", "erp_field", "transform", "required", "default_value", "lookup_doctype", "lookup_field",
             "lookup_endpoint", "if_not_found", "add_missing_option", "source_timezone", "value_map", "expression", "notes")


def _cf_def(dt, fieldname):
    cf = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname},
                             ["fieldname", "label", "fieldtype", "options", "insert_after", "allow_on_submit",
                              "read_only", "fetch_from"], as_dict=True)
    return dict(cf, dt=dt) if cf else None


def export_pack(endpoint_name, pack_name=None):
    ep = frappe.get_doc("API Endpoint", endpoint_name)
    meta = frappe.get_meta(ep.reference_doctype)
    items_dt = meta.get_field("items").options if meta.has_field("items") else None

    endpoint = {k: ep.get(k) for k in _EP_KEYS}
    defaults = parse_json_field(ep.defaults, {}) or {}
    for k, ph in PLACEHOLDERS.items():
        if k in defaults:
            defaults[k] = ph
    endpoint["defaults"] = defaults
    endpoint["handler_settings"] = parse_json_field(ep.handler_settings, {}) or {}
    endpoint["field_maps"] = []
    custom_fields, select_options = [], []
    for m in ep.field_maps:
        row = {k: m.get(k) for k in _MAP_KEYS if m.get(k) not in (None, "", 0)}
        if isinstance(row.get("value_map"), str):
            row["value_map"] = parse_json_field(row["value_map"], {})
        endpoint["field_maps"].append(row)
        table, _sep, field = (m.erp_field or "").rpartition(".")
        dt = items_dt if table == "items" else ep.reference_doctype
        if field.startswith("custom_"):
            cf = _cf_def(dt, field)
            if cf:
                custom_fields.append(cf)
        if m.transform == "Value Map" and m.value_map:
            vals = sorted({str(v) for v in (parse_json_field(m.value_map, {}) or {}).values()
                           if v and not str(v).startswith("<")})
            if vals:
                select_options.append({"dt": dt, "fieldname": field, "options": vals})
    for cf_name in (endpoint["handler_settings"].get("customer_fields") or {}):
        cf = _cf_def("Customer", cf_name)
        if cf:
            custom_fields.append(cf)
    mops = sorted({v for v in (endpoint["handler_settings"].get("mode_of_payment_map") or {}).values()
                   if v and not str(v).startswith("<")})
    modes = [{"name": m, "type": frappe.db.get_value("Mode of Payment", m, "type") or "Bank"} for m in mops]
    sample = parse_json_field(ep.sample_payload, None)
    return {
        "pack": pack_name or ep.endpoint_title, "version": PACK_VERSION,
        "platform": frappe.db.get_value("API Partner", ep.allowed_partners[0].partner, "platform")
        if ep.allowed_partners else "",
        "description": ep.description or "", "exported_on": str(frappe.utils.now_datetime()),
        "custom_fields": custom_fields, "select_options": select_options, "modes_of_payment": modes,
        "endpoint": endpoint, "sample_payloads": [sample] if sample else [],
    }


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

def _company_values(company):
    vals = {}
    tmpl = frappe.db.get_value("Sales Taxes and Charges Template", {"company": company, "is_default": 1, "disabled": 0}) \
        or frappe.db.get_value("Sales Taxes and Charges Template", {"company": company, "disabled": 0})
    vals["{{company.default_sales_tax_template}}"] = tmpl
    wh = frappe.db.get_single_value("Stock Settings", "default_warehouse")
    if not wh or frappe.db.get_value("Warehouse", wh, "company") != company:
        wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0, "disabled": 0})
    vals["{{company.default_warehouse}}"] = wh
    vals["{{company.cost_center}}"] = frappe.get_cached_value("Company", company, "cost_center")
    return vals


def install_pack(pack, partner, mop_account=None, slug=None):
    """Install a pack for a partner. Returns {"log": [...], "endpoint": name}."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_field

    if isinstance(pack, str):
        pack = json.loads(pack)
    if not pack.get("endpoint"):
        frappe.throw(_("Not a NeoConnect Integration Pack (no endpoint)"))
    p = frappe.get_doc("API Partner", partner)
    if not p.company:
        frappe.throw(_("Set the Company on API Partner {0} first").format(partner))
    company = p.company
    log = []

    # 1. custom fields
    for cf in pack.get("custom_fields") or []:
        dt = cf["dt"]
        if not frappe.db.exists("DocType", dt):
            log.append(f"SKIP field {dt}.{cf['fieldname']}: DocType {dt} not installed")
            continue
        if frappe.get_meta(dt).has_field(cf["fieldname"]):
            log.append(f"exists  {dt}.{cf['fieldname']}")
            continue
        if cf.get("fieldtype") == "Link" and cf.get("options") and not frappe.db.exists("DocType", cf["options"]):
            log.append(f"SKIP field {dt}.{cf['fieldname']}: links to missing DocType {cf['options']}")
            continue
        df = {k: cf.get(k) for k in ("fieldname", "label", "fieldtype", "options", "allow_on_submit", "read_only",
                                     "fetch_from") if cf.get(k) not in (None, "")}
        after = cf.get("insert_after")
        df["insert_after"] = after if after and frappe.get_meta(dt).has_field(after) else frappe.get_meta(dt).fields[-1].fieldname
        create_custom_field(dt, df)
        log.append(f"CREATED {dt}.{cf['fieldname']} ({cf.get('fieldtype')})")

    # 2. select options
    for so in pack.get("select_options") or []:
        if not frappe.db.exists("DocType", so["dt"]) or not frappe.get_meta(so["dt"]).has_field(so["fieldname"]):
            continue
        for opt in so["options"]:
            if ensure_select_option(so["dt"], so["fieldname"], opt):
                log.append(f"ADDED option '{opt}' to {so['dt']}.{so['fieldname']}")

    # 3. modes of payment (+ default account for the company)
    account = mop_account or frappe.get_cached_value("Company", company, "default_bank_account") \
        or frappe.get_cached_value("Company", company, "default_cash_account")
    for m in pack.get("modes_of_payment") or []:
        if not frappe.db.exists("Mode of Payment", m["name"]):
            doc = frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": m["name"],
                                  "type": m.get("type") or "Bank", "enabled": 1})
            doc.insert(ignore_permissions=True)
            log.append(f"CREATED Mode of Payment {m['name']}")
        doc = frappe.get_doc("Mode of Payment", m["name"])
        if not any(a.company == company for a in doc.accounts):
            if account:
                doc.append("accounts", {"company": company, "default_account": account})
                doc.save(ignore_permissions=True)
                log.append(f"SET account {account} on Mode of Payment {m['name']} for {company}")
            else:
                log.append(f"TODO Mode of Payment {m['name']}: set a default account for {company}")

    # 4. endpoint
    e = dict(pack["endpoint"])
    e["slug"] = slug or e["slug"]
    cvals = _company_values(company)
    defaults = {}
    for k, v in (e.get("defaults") or {}).items():
        if isinstance(v, str) and v in cvals:
            v = cvals[v]
            if not v:
                log.append(f"TODO Document Default '{k}': nothing found for {company}; set it on the endpoint")
                continue
        defaults[k] = v
    if frappe.db.exists("API Endpoint", e["slug"]):
        ep = frappe.get_doc("API Endpoint", e["slug"])
        ep.add_comment("Comment", "Configuration before installing pack '{0}':<pre>{1}</pre>".format(
            frappe.utils.escape_html(pack.get("pack") or ""), frappe.utils.escape_html(dumps(ep.as_dict(no_default_fields=True)))[:60000]))
        ep.set("field_maps", [])
        log.append(f"UPDATED endpoint {ep.name} (previous configuration saved as a comment)")
    else:
        ep = frappe.new_doc("API Endpoint")
        ep.slug = e["slug"]
        log.append(f"CREATED endpoint {e['slug']}")
    for k in _EP_KEYS:
        if k in e and k != "slug" and e[k] is not None:
            ep.set(k, e[k])
    ep.enabled = 1
    ep.defaults = dumps(defaults)
    ep.handler_settings = dumps(e.get("handler_settings") or {})
    for m in e.get("field_maps") or []:
        row = dict(m)
        if isinstance(row.get("value_map"), dict):
            row["value_map"] = dumps(row["value_map"])
        ep.append("field_maps", row)
    if partner not in [r.partner for r in ep.allowed_partners]:
        ep.append("allowed_partners", {"partner": partner})
    if pack.get("sample_payloads"):
        ep.sample_payload = dumps(pack["sample_payloads"][0])
    ep.flags.ignore_links = True
    ep.save(ignore_permissions=True) if not ep.is_new() else ep.insert(ignore_permissions=True)
    return {"log": log, "endpoint": ep.name}
