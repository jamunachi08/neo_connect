"""
Health Check: tests every part of an inbound endpoint on THIS site and explains how to fix what fails.
"""

import json

import frappe
from frappe.utils import cint

from .company_names import LinkResolver, get_abbr
from .mapper import extract_records, get_path, split_erp_field
from .utils import endpoint_url, get_hooks, parse_json_field

OK, WARN, FAIL = "ok", "warn", "fail"


def health_check(endpoint_name, payload=None):
    ep = frappe.get_doc("API Endpoint", endpoint_name)
    checks = []

    def add(area, status, detail, fix=""):
        checks.append({"area": area, "status": status, "detail": detail, "fix": fix})

    # --- endpoint & partner --------------------------------------------------------------------
    add("Endpoint", OK if ep.enabled else FAIL, f"{ep.name} is {'enabled' if ep.enabled else 'disabled'}",
        "" if ep.enabled else "Tick Enabled on the API Endpoint")
    partners = [r.partner for r in ep.allowed_partners]
    if not partners:
        add("Partner", FAIL, "No partner is allowed to use this endpoint", "Add the partner under Allowed Partners")
    company = None
    for pn in partners:
        p = frappe.get_doc("API Partner", pn)
        company = company or p.company
        add("Partner", OK if p.enabled else FAIL, f"{pn}: {'enabled' if p.enabled else 'disabled'}",
            "" if p.enabled else "Tick Enabled on the API Partner")
        if not p.company:
            add("Partner", FAIL, f"{pn}: no Company", "Set Company on the API Partner")
        if not (p.user and frappe.db.get_value("User", p.user, "api_key")):
            add("Credentials", FAIL, f"{pn}: no API key yet", "API Partner > Credentials > Generate Credentials")
        else:
            add("Credentials", OK, f"{pn}: API user {p.user}, key {p.api_key}")
        if p.require_signature:
            add("Credentials", WARN, f"{pn}: HMAC signature required",
                "The vendor must send X-Signature; untick 'Require HMAC Signature' while testing with Postman")

    if ep.direction != "Inbound":
        add("Endpoint", WARN, "Outbound endpoint: only the basic checks apply")
        return _summary(ep, checks, None)

    meta = frappe.get_meta(ep.reference_doctype)
    items_dt = meta.get_field("items").options if meta.has_field("items") else None
    settings = parse_json_field(ep.handler_settings, {}) or {}
    defaults = parse_json_field(ep.defaults, {}) or {}
    resolver = LinkResolver(get_abbr(company))

    # --- hooks ---------------------------------------------------------------------------------
    for h in get_hooks(ep.pre_process_hook) + get_hooks(ep.post_process_hook):
        try:
            frappe.get_attr(h)
        except Exception:
            add("Hooks", FAIL, f"Cannot load {h}", "Remove the line or install the app that provides it")

    # --- mapped fields exist ---------------------------------------------------------------------
    missing = []
    for m in ep.field_maps:
        table, field = split_erp_field(m.erp_field or "")
        dt = (meta.get_field(table).options if table and meta.get_field(table) else ep.reference_doctype)
        if field and not frappe.get_meta(dt).has_field(field) and field not in ("name",):
            missing.append(f"{dt}.{field}")
    add("Fields", FAIL if missing else OK,
        "Missing fields: " + ", ".join(missing) if missing else f"All {len(ep.field_maps)} mapped fields exist",
        "Install the pack again, or create the fields from the Payload Sample (Review & Confirm)" if missing else "")
    cmeta = frappe.get_meta("Customer")
    cmiss = [f for f in (settings.get("customer_fields") or {}) if not cmeta.has_field(f)]
    if settings.get("customer_fields"):
        add("Fields", FAIL if cmiss else OK,
            "Missing Customer fields: " + ", ".join(cmiss) if cmiss else
            f"{len(settings['customer_fields'])} Customer fields exist (vehicle / mobile)",
            "Create them on the Customer (install the pack again)" if cmiss else "")

    # --- defaults --------------------------------------------------------------------------------
    for key, val in defaults.items():
        table, field = split_erp_field(key)
        dt = items_dt if table == "items" else ep.reference_doctype
        df = frappe.get_meta(dt).get_field(field) if dt else None
        if df and df.fieldtype == "Link" and isinstance(val, str):
            real = resolver.resolve_value(df.options, val)
            ok = frappe.db.exists(df.options, real)
            add("Defaults", OK if ok else FAIL, f"{key} = {val}" + ("" if ok else f" ({df.options} not found)"),
                "" if ok else f"Set an existing {df.options} in the endpoint's Document Defaults")
    if meta.has_field("taxes_and_charges") and not defaults.get("taxes_and_charges"):
        add("Defaults", WARN, "No tax template in Document Defaults: invoices will have no VAT",
            "Add \"taxes_and_charges\": \"<your VAT template>\" to Document Defaults")

    # --- new customers need a non-group Customer Group and Territory --------------------------------
    if "ensure_customer" in (ep.pre_process_hook or ""):
        from neo_connect.handlers.commerce import _leaf_tree_value
        for dt_, key, sel in (("Customer Group", "customer_group", "customer_group"), ("Territory", "territory", "territory")):
            chosen = _leaf_tree_value(dt_, settings.get(key), frappe.db.get_single_value("Selling Settings", sel))
            add("Customers", OK if chosen else FAIL, f"New customers get {dt_}: {chosen}" if chosen else f"No usable {dt_}",
                "" if chosen else f"Create a non-group {dt_}, or set it in Selling Settings")

    # --- modes of payment -------------------------------------------------------------------------
    for code, mop in (settings.get("mode_of_payment_map") or {}).items():
        if not mop or str(mop).startswith("<"):
            add("Payments", FAIL, f"Payment method '{code}' has no Mode of Payment",
                "Payload Sample > Review & Confirm > section 5, or edit Handler Settings mode_of_payment_map")
            continue
        if not frappe.db.exists("Mode of Payment", mop):
            add("Payments", FAIL, f"'{code}' -> Mode of Payment {mop} does not exist", f"Create Mode of Payment {mop}")
            continue
        acc = frappe.db.get_value("Mode of Payment Account", {"parent": mop, "company": company}, "default_account")
        add("Payments", OK if acc else FAIL, f"'{code}' -> {mop}" + (f" (account {acc})" if acc else " has no account"),
            "" if acc else f"Open Mode of Payment {mop} and add a default account for {company}")

    # --- select translations ------------------------------------------------------------------------
    for m in ep.field_maps:
        if m.transform != "Value Map" or not m.value_map:
            continue
        table, field = split_erp_field(m.erp_field)
        dt = items_dt if table == "items" else ep.reference_doctype
        df = frappe.get_meta(dt).get_field(field)
        if not df or df.fieldtype != "Select":
            continue
        opts = (df.options or "").split("\n")
        bad = [f"{k} -> {v}" for k, v in (parse_json_field(m.value_map, {}) or {}).items() if v not in opts]
        add("Translations", FAIL if bad else OK, f"{field}: " + ("not valid options: " + ", ".join(bad) if bad
                                                                else "all values translate to valid options"),
            "Fix the Value Map, or add the options in Customize Form" if bad else "")

    # --- sample payload ------------------------------------------------------------------------------
    sample = payload if payload is not None else parse_json_field(ep.sample_payload, None)
    result = None
    if sample is None:
        add("Sample", WARN, "No sample payload on the endpoint: the dry run is skipped",
            "Paste a real order JSON into the endpoint's Sample Payload")
    else:
        records = extract_records(sample, ep.records_path)
        rec = records[0] if records else {}
        ext = get_path(rec, ep.external_id_path) if ep.external_id_path else None
        add("Sample", OK if ext else FAIL, f"Order id at '{ep.external_id_path}': {ext}" if ext else
            f"No value at '{ep.external_id_path}' in the sample", "" if ext else "Fix External ID Path")
        # lookups: do the sample's values exist? (items are the most common failure)
        missing_items = []
        for m in ep.field_maps:
            if m.transform != "Lookup" or not m.lookup_doctype:
                continue
            list_path, _s, rel = (m.external_path or "").partition("[]")
            vals = [get_path(r, rel.lstrip(".")) for r in (get_path(rec, list_path) or [])] if "[]" in (m.external_path or "") \
                else [get_path(rec, m.external_path)]
            for v in [x for x in vals if x not in (None, "")]:
                field = m.lookup_field or "name"
                hit = frappe.db.get_value(m.lookup_doctype, {field: v}) or (
                    field == "name" and frappe.db.exists(m.lookup_doctype, resolver.resolve_value(m.lookup_doctype, str(v))))
                if not hit and m.lookup_doctype == "Item" and "[]" in (m.external_path or ""):
                    missing_items.append((m, v))
                    continue
                add("Masters", OK if hit else FAIL, f"{m.lookup_doctype} '{v}' (from {m.external_path})" +
                    ("" if hit else " not found"),
                    "" if hit else f"Create {m.lookup_doctype} '{v}', or point the mapping's Lookup Field to the "
                                   f"{m.lookup_doctype} field that holds Magento's value (e.g. a custom 'magento_sku')")
        # the sample's SKUs are often examples: dry-run with a real item instead and say so
        if missing_items:
            m0 = missing_items[0][0]
            field = m0.lookup_field or "name"
            sub = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1, "has_variants": 0}, field,
                                      order_by="is_stock_item desc, modified desc")
            vals = ", ".join(sorted({str(v) for _m, v in missing_items}))
            if sub:
                sample = json.loads(json.dumps(sample))
                list_path, _s, rel = m0.external_path.partition("[]")
                for r in extract_records(sample, ep.records_path):
                    for line in get_path(r, list_path) or []:
                        if isinstance(line, dict):
                            line[rel.lstrip(".")] = sub
                add("Masters", WARN, f"Sample SKU(s) {vals} are not Items in ERPNext; the dry run below uses your item '{sub}'",
                    f"Fine if the sample is only an example. Real orders must send SKUs that equal the Item "
                    f"'{field}' (or change the mapping's Lookup Field, e.g. to a custom 'magento_sku' on Item)")
            else:
                add("Masters", FAIL, f"Sample SKU(s) {vals} not found and no sellable Item exists",
                    "Create the Items Magento sells (Item Code = Magento SKU)")
        # full dry run through ERPNext
        from .inbound import Processor
        p = frappe.get_doc("API Partner", partners[0]) if partners else None
        if p:
            sp = "nc_health"
            frappe.db.savepoint(sp)
            try:
                out = Processor(ep, p).process_payload(sample, dry_run=True)
                result = out["results"][0]
            except Exception as e:  # noqa: BLE001
                result = {"status": "Failed", "error": str(e)}
            finally:
                frappe.db.rollback(save_point=sp)
                frappe.clear_messages()
            ok = result.get("status") == "Validated"
            add("Dry run", OK if ok else FAIL,
                "ERPNext accepted the sample order (nothing saved)" if ok else (result.get("error") or str(result)),
                "" if ok else _hint(result.get("error") or ""))
            for w in result.get("warnings") or []:
                add("Dry run", WARN if "mismatch" in w.lower() else OK, w)
    return _summary(ep, checks, result)


def _hint(err):
    e = err.lower()
    if "not found for" in e:
        return "A master record from the order is missing (see 'Masters' above)"
    if "]:" in err:
        return "Mandatory fields without a value: map them or add Document Defaults (see 'Fields' / 'Defaults')"
    if "mode of payment" in e:
        return "See 'Payments' above"
    if "group type" in e or "customer group" in e or "territory" in e:
        return ("Set a non-group Default Customer Group and Territory in Selling Settings, or 'customer_group' / "
                "'territory' in the endpoint's Handler Settings")
    if "template" in e:
        return "Set an existing tax template in Document Defaults"
    return "Open API Request Log for the full message"


def _summary(ep, checks, result):
    fails = [c for c in checks if c["status"] == FAIL]
    warns = [c for c in checks if c["status"] == WARN]
    status = "READY" if not fails else "NOT READY"
    return {
        "endpoint": ep.name, "status": status, "fails": len(fails), "warnings": len(warns), "checks": checks,
        "url": endpoint_url(ep.name, ep.direction),
        "test": f"POST {endpoint_url(ep.name, ep.direction)}&dry_run=1  with header  Authorization: token <api_key>:<api_secret>",
    }


def health_html(rep):
    from frappe.utils import escape_html as esc

    icon = {OK: "<span style='color:#2e7d32'>&#10004;</span>", WARN: "<span style='color:#ef6c00'>&#9888;</span>",
            FAIL: "<span style='color:#c62828'>&#10008;</span>"}
    color = "green" if rep["status"] == "READY" else "red"
    parts = [f"<p><span class='indicator-pill {color}'>{esc(rep['status'])}</span> &nbsp; "
             f"{rep['fails']} problem(s), {rep['warnings']} warning(s) &nbsp; <code>{esc(rep['url'])}</code></p>",
             "<table class='table table-bordered table-sm small'><thead><tr><th></th><th>Area</th><th>Check</th>"
             "<th>How to fix</th></tr></thead><tbody>"]
    order = {FAIL: 0, WARN: 1, OK: 2}
    for c in sorted(rep["checks"], key=lambda c: order[c["status"]]):
        parts.append(f"<tr><td>{icon[c['status']]}</td><td>{esc(c['area'])}</td><td>{esc(c['detail'])}</td>"
                     f"<td>{esc(c['fix'])}</td></tr>")
    parts.append("</tbody></table>")
    if rep["status"] == "READY":
        parts.append(f"<p class='small'>Test it: <code>{esc(rep['test'])}</code></p>")
    return "".join(parts)
