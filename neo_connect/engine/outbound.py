"""Outbound (partner reads from ERPNext) processing."""

import json

import frappe
from frappe import _
from frappe.utils import cint

from .company_names import all_abbrs
from .mapper import map_outbound, split_erp_field
from .security import GatewayError
from .utils import field_map_rows, handler_settings, parse_json_field, safe_eval

RESERVED_ARGS = {"endpoint", "cmd", "page", "limit", "modified_since", "filters", "data", "dry_run"}
ALLOWED_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "is", "between"}


def build_filters(endpoint, args):
    filters = parse_json_field(endpoint.base_filters, [], "Base Filters") or []
    if isinstance(filters, dict):
        filters = [[k, "=", v] for k, v in filters.items()]
    allowed = {f.strip() for f in (endpoint.allowed_filters or "").replace(",", "\n").splitlines() if f.strip()}

    # ?field=value
    for key, value in args.items():
        if key in RESERVED_ARGS or key.startswith("_"):
            continue
        if key not in allowed:
            raise GatewayError(_("Filter '{0}' is not allowed. Allowed: {1}").format(
                key, ", ".join(sorted(allowed)) or "none"), 400)
        filters.append([key, "=", value])

    # ?filters=[["field","op","value"], ...]
    if args.get("filters"):
        try:
            extra = json.loads(args["filters"]) if isinstance(args["filters"], str) else args["filters"]
        except ValueError:
            raise GatewayError(_("'filters' must be a JSON array"), 400)
        for f in extra or []:
            if not (isinstance(f, (list, tuple)) and len(f) == 3):
                raise GatewayError(_("Each filter must be [field, operator, value]"), 400)
            if f[0] not in allowed:
                raise GatewayError(_("Filter '{0}' is not allowed").format(f[0]), 400)
            if str(f[1]).lower() not in ALLOWED_OPERATORS:
                raise GatewayError(_("Operator '{0}' is not allowed").format(f[1]), 400)
            filters.append(list(f))

    if args.get("modified_since"):
        filters.append(["modified", ">", args["modified_since"]])
    return filters


def handle_pull(endpoint, partner, args):
    page = max(cint(args.get("page")), 1)
    max_len = cint(endpoint.max_page_length) or 500
    page_length = min(cint(args.get("limit")) or cint(endpoint.default_page_length) or 50, max_len)

    if endpoint.outbound_source == "Server Method":
        ctx = frappe._dict(endpoint=endpoint, partner=partner, args=frappe._dict(args),
                           settings=handler_settings(endpoint, partner), page=page, page_length=page_length)
        data = frappe.get_attr(endpoint.server_method)(ctx)
        if isinstance(data, dict) and "data" in data:
            body = data
        else:
            body = {"data": data or []}
        body.setdefault("page", page)
        body.setdefault("page_length", page_length)
        return body

    maps = field_map_rows(endpoint)
    parent_fields, needs_children = {"name", "modified"}, False
    for m in maps:
        table, field = split_erp_field(m["erp_field"])
        if table:
            needs_children = True
        else:
            parent_fields.add(field)

    rows = frappe.get_list(
        endpoint.reference_doctype,
        filters=build_filters(endpoint, args),
        fields=sorted(parent_fields),
        order_by=endpoint.order_by or "modified asc",
        limit_start=(page - 1) * page_length,
        limit_page_length=page_length,
        ignore_permissions=cint(endpoint.ignore_permissions),
    )

    abbrs = all_abbrs()
    data = []
    for row in rows:
        source = row
        if needs_children:
            doc = frappe.get_doc(endpoint.reference_doctype, row.name)
            if not cint(endpoint.ignore_permissions):
                doc.check_permission("read")
            source = doc.as_dict()
        data.append(map_outbound(source, maps, safe_eval, company_abbrs=abbrs))

    return {
        "data": data,
        "page": page,
        "page_length": page_length,
        "has_more": len(rows) == page_length,
        "next_modified_since": str(rows[-1].modified) if rows else args.get("modified_since"),
    }
