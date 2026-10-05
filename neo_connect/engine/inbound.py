"""Inbound (partner -> ERPNext) processing."""

import hashlib
import json
import time

import frappe
from frappe import _
from frappe.utils import cint, strip_html

from .company_names import LinkResolver, get_abbr, with_suffix
from .mapper import MappingError, extract_records, get_path, map_inbound, split_erp_field
from .security import GatewayError
from .utils import (
    dumps, field_map_rows, handler_settings, merged_defaults, parse_json_field,
    run_hooks, safe_eval,
)

MAX_LOG_CHARS = 2_000_000


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def reference_name(partner, endpoint, external_id):
    """Deterministic primary key for API External Reference (guarantees idempotency)."""
    return hashlib.sha1(f"{partner}\x1f{endpoint}\x1f{external_id}".encode()).hexdigest()


def make_lookup(partner, company=None):
    cache = {}
    abbr = get_abbr(company)

    def lookup(row, value):
        key = (row.get("lookup_doctype"), row.get("lookup_field"), row.get("lookup_endpoint"), str(value))
        if key in cache:
            return cache[key]
        if row.get("lookup_endpoint"):
            result = frappe.db.get_value(
                "API External Reference",
                reference_name(partner.name, row["lookup_endpoint"], value),
                "reference_name",
            )
            label = f"Record from endpoint {row['lookup_endpoint']}"
        elif row.get("lookup_doctype"):
            dt = row["lookup_doctype"]
            field = row.get("lookup_field") or "name"
            filters = {field: value}
            if company and field != "name" and frappe.get_meta(dt).has_field("company"):
                filters["company"] = company          # e.g. warehouse_name "Stores" in this company
            result = frappe.db.get_value(dt, filters, "name")
            if not result and field == "name" and abbr:
                # partner sent the plain name; ERPNext stores "<name> - <ABBR>"
                result = frappe.db.get_value(dt, {"name": with_suffix(str(value), abbr)}, "name")
            label = dt
        else:
            raise MappingError("Lookup needs 'Lookup DocType' or 'Lookup Endpoint'")

        if not result:
            mode = row.get("if_not_found") or "Error"
            if mode == "Use Value":
                result = value
            elif mode == "Skip":
                result = None
            else:
                raise MappingError(f"{label} not found for '{value}'")
        cache[key] = result
        return result

    return lookup


def compile_row_filters(endpoint):
    spec = parse_json_field(endpoint.row_filters, {}, "Row Filters") or {}
    compiled = {}
    for list_key, expr in spec.items():
        key = list_key if list_key.endswith("[]") else f"{list_key}[]"
        compiled[key] = (lambda e: lambda row: bool(safe_eval(e, {"row": row})))(expr)
    return compiled


def apply_defaults(doc_dict, defaults):
    for key, val in (defaults or {}).items():
        table, field = split_erp_field(key)
        if table:
            for row in doc_dict.get(table) or []:
                row.setdefault(field, val)
        else:
            doc_dict.setdefault(key, val)


def clean_error(exc):
    msg = strip_html(str(exc) or exc.__class__.__name__)
    return msg.strip()[:1000]


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------

class Processor:
    def __init__(self, endpoint, partner, log_name=None):
        self.endpoint = endpoint
        self.partner = partner
        self.log_name = log_name
        self.maps = field_map_rows(endpoint)
        self.defaults = merged_defaults(endpoint, partner)
        self.settings = handler_settings(endpoint, partner)
        self.company = self.defaults.get("company")
        self.lookup = make_lookup(partner, self.company)
        self.row_filters = compile_row_filters(endpoint)
        self.timezone = frappe.utils.get_system_timezone()

    # -- build ---------------------------------------------------------------
    def build(self, record, external_id):
        doc_dict, errors = map_inbound(record, self.maps, self.lookup, safe_eval, self.row_filters,
                                       timezone=self.timezone)
        apply_defaults(doc_dict, self.defaults)
        # partners (and defaults) may use master names without the company suffix ("Stores" -> "Stores - PT")
        resolver = LinkResolver(get_abbr(doc_dict.get("company") or self.company))
        if not errors:
            resolver.resolve_doc(self.endpoint.reference_doctype, doc_dict)
        ctx = frappe._dict(
            endpoint=self.endpoint, partner=self.partner, record=record, doc=doc_dict,
            settings=self.settings, errors=errors, warnings=[], external_id=external_id,
            document=None, log_name=self.log_name,
        )
        if not errors:
            run_hooks(self.endpoint.pre_process_hook, ctx)
        if not ctx.errors:
            resolver.resolve_doc(self.endpoint.reference_doctype, doc_dict)   # values added by hooks
        return ctx

    # -- one record ----------------------------------------------------------
    def process_record(self, record):
        ep = self.endpoint
        external_id = None
        if ep.external_id_path:
            external_id = get_path(record, ep.external_id_path)
            if external_id in (None, ""):
                return {"status": "Failed", "error": _("Missing external id at '{0}'").format(ep.external_id_path)}
            external_id = str(external_id)

        base = {"external_id": external_id}
        update_target = None
        ref_key = reference_name(self.partner.name, ep.name, external_id) if external_id else None

        if ref_key:
            ref = frappe.db.get_value("API External Reference", ref_key,
                                      ["reference_doctype", "reference_name"], as_dict=True)
            if ref and ref.reference_name and frappe.db.exists(ref.reference_doctype, ref.reference_name):
                ds = frappe.db.get_value(ref.reference_doctype, ref.reference_name, "docstatus")
                if ds != 2:  # cancelled documents can be re-imported
                    found = dict(base, doctype=ref.reference_doctype, name=ref.reference_name)
                    policy = ep.on_duplicate or "Skip"
                    if policy == "Update Draft" and ds == 0:
                        update_target = ref.reference_name
                    elif policy == "Error":
                        return dict(found, status="Failed",
                                    error=_("Already imported as {0}").format(ref.reference_name))
                    else:
                        return dict(found, status="Duplicate", message=_("Already imported"))

        ctx = self.build(record, external_id)
        if ctx.errors:
            return dict(base, status="Failed", error="; ".join(ctx.errors))

        dt = ep.reference_doctype
        if update_target:
            doc = frappe.get_doc(dt, update_target)
            for key, value in ctx.doc.items():
                if isinstance(value, list):
                    doc.set(key, [])
                    for row in value:
                        doc.append(key, row)
                else:
                    doc.set(key, value)
            doc.flags.ignore_permissions = cint(ep.ignore_permissions)
            doc.save()
            status = "Updated"
        else:
            doc = frappe.get_doc(dict(ctx.doc, doctype=dt))
            doc.flags.ignore_permissions = cint(ep.ignore_permissions)
            doc.insert()
            status = "Created"

        if ep.document_action == "Submit" and doc.docstatus == 0:
            doc.submit()

        ctx.document = doc
        if ref_key:
            self.save_reference(ref_key, external_id, doc)

        run_hooks(ep.post_process_hook, ctx)

        out = dict(base, status=status, doctype=dt, name=doc.name, docstatus=doc.docstatus)
        if doc.meta.has_field("grand_total"):
            out["grand_total"] = doc.get("grand_total")
        if ctx.warnings:
            out["warnings"] = ctx.warnings
        return out

    def save_reference(self, ref_key, external_id, doc):
        values = {"reference_doctype": doc.doctype, "reference_name": doc.name, "last_log": self.log_name}
        if frappe.db.exists("API External Reference", ref_key):
            frappe.db.set_value("API External Reference", ref_key, values)
        else:
            frappe.get_doc(dict(
                values, doctype="API External Reference", partner=self.partner.name,
                endpoint=self.endpoint.name, external_id=external_id,
            )).insert(ignore_permissions=True)

    # -- whole payload -------------------------------------------------------
    def process_payload(self, payload, dry_run=False):
        records = extract_records(payload, self.endpoint.records_path)
        if not records:
            raise GatewayError(_("No records found in payload"), 400)
        limit = cint(self.endpoint.max_records_per_request) or 100
        if len(records) > limit:
            raise GatewayError(_("Too many records: {0} (max {1})").format(len(records), limit), 413)

        results = []
        for i, record in enumerate(records):
            sp = f"cc_rec_{i}"
            frappe.db.savepoint(sp)
            try:
                if not isinstance(record, dict):
                    raise MappingError("Each record must be a JSON object")
                res = self.process_record(record)
                if res["status"] == "Failed" or dry_run:
                    frappe.db.rollback(save_point=sp)
                if dry_run and res["status"] in ("Created", "Updated"):
                    res["status"] = "Validated"
                    res["name"] = None
            except Exception as e:  # noqa: BLE001 - isolate each record
                frappe.db.rollback(save_point=sp)
                res = {"status": "Failed", "error": clean_error(e)}
                if isinstance(record, dict) and self.endpoint.external_id_path:
                    res["external_id"] = get_path(record, self.endpoint.external_id_path)
                if not isinstance(e, (MappingError, frappe.ValidationError)):
                    res["trace"] = frappe.get_traceback()
            frappe.clear_messages()
            res["index"] = i
            results.append(res)
        return summarize(results)

    def preview(self, record):
        """Map one record without touching the database (used by the 'Test Mapping' button)."""
        external_id = get_path(record, self.endpoint.external_id_path) if self.endpoint.external_id_path else None
        ctx = self.build(record, external_id)
        return {"external_id": external_id, "errors": ctx.errors, "warnings": ctx.warnings, "doc": ctx.doc}


def summarize(results):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    ok = counts.get("Created", 0) + counts.get("Updated", 0) + counts.get("Validated", 0)
    failed = counts.get("Failed", 0)
    dup = counts.get("Duplicate", 0)
    if failed and (ok or dup):
        status, http = "Partial", 207
    elif failed:
        status, http = "Failed", 422
    elif dup and not ok:
        status, http = "Duplicate", 200
    else:
        status, http = "Success", 200
    public = [{k: v for k, v in r.items() if k != "trace"} for r in results]
    return {
        "status": status,
        "http_status": http,
        "summary": {"total": len(results), **counts},
        "results": public,
        "_traces": [r["trace"] for r in results if r.get("trace")],
    }


# ---------------------------------------------------------------------------
# logging + entry points
# ---------------------------------------------------------------------------

def new_log(direction, endpoint=None, partner=None, payload_text=None, query=None):
    log = frappe.get_doc({
        "doctype": "API Request Log",
        "direction": direction,
        "endpoint": endpoint,
        "partner": partner,
        "status": "Received",
        "request_ip": getattr(frappe.local, "request_ip", None),
        "http_method": frappe.request.method if getattr(frappe, "request", None) else None,
        "query_string": query,
        "user": frappe.session.user,
        "payload": (payload_text or "")[:MAX_LOG_CHARS],
    })
    log.insert(ignore_permissions=True)
    return log


def finish_log(log, result, started, error=None):
    log.status = result.get("status")
    log.http_status = result.get("http_status")
    summary = result.get("summary") or {}
    log.record_count = summary.get("total", 0)
    log.success_count = sum(summary.get(k, 0) for k in ("Created", "Updated", "Validated", "Returned"))
    log.failed_count = summary.get("Failed", 0)
    log.duplicate_count = summary.get("Duplicate", 0)
    log.duration_ms = round((time.time() - started) * 1000, 1)
    log.response = dumps({k: v for k, v in result.items() if not k.startswith("_")})[:MAX_LOG_CHARS]
    traces = result.get("_traces") or []
    if error or traces:
        log.error = (error or "") + "\n\n".join(traces)
    first = next((r for r in result.get("results", []) if r.get("name")), None)
    if first:
        log.reference_doctype = first.get("doctype")
        log.reference_name = first.get("name")
    log.save(ignore_permissions=True)


def process_log(log_name, as_user=None):
    """Background job / reprocess entry point."""
    log = frappe.get_doc("API Request Log", log_name)
    started = time.time()
    endpoint = frappe.get_doc("API Endpoint", log.endpoint)
    partner = frappe.get_doc("API Partner", log.partner)
    if as_user:
        frappe.set_user(as_user)
    try:
        payload = json.loads(log.payload or "null")
        result = Processor(endpoint, partner, log.name).process_payload(payload, dry_run=cint(log.dry_run))
        finish_log(log, result, started)
    except GatewayError as e:
        finish_log(log, {"status": e.status, "http_status": e.http_status, "error": e.message}, started, e.message)
    except Exception:  # noqa: BLE001
        frappe.db.rollback()
        log.reload()
        finish_log(log, {"status": "Failed", "http_status": 500}, started, frappe.get_traceback())
    log.db_set("attempts", cint(log.attempts) + 1)
    frappe.db.commit()
    return log
