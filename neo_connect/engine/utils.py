import json

import frappe
from frappe import _


def parse_json_field(value, default=None, label="JSON"):
    """Parse a Code/JSON field; empty -> default."""
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except ValueError as e:
        frappe.throw(_("{0} is not valid JSON: {1}").format(label, e))


def dumps(obj):
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def field_map_rows(endpoint):
    """Child rows as plain dicts, with value_map parsed."""
    rows = []
    for r in endpoint.field_maps or []:
        d = r.as_dict()
        d["value_map"] = parse_json_field(r.value_map, {}, f"Value Map (row {r.idx})")
        rows.append(d)
    return rows


def _eval_globals():
    from frappe.utils import cint, cstr, flt, get_datetime, getdate, now_datetime, nowdate

    from .mapper import get_path

    def first_of(record, *paths):
        """First non-empty value among several payload paths, e.g. first_of(record, 'a.x', 'b.y')."""
        for p in paths:
            v = get_path(record, p)
            if v not in (None, ""):
                return v
        return None

    return {
        "get": get_path, "first_of": first_of,
        "flt": flt, "cint": cint, "cstr": cstr, "getdate": getdate, "get_datetime": get_datetime,
        "nowdate": nowdate, "now_datetime": now_datetime,
        "str": str, "int": int, "float": float, "bool": bool, "len": len, "abs": abs,
        "round": round, "min": min, "max": max, "sum": sum, "any": any, "all": all,
    }


def safe_eval(expression, names):
    """frappe.safe_eval (no builtins, no dunder access) with a small set of helpers."""
    return frappe.safe_eval(expression, _eval_globals(), dict(names))


def get_hooks(value):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def run_hooks(paths, ctx):
    for path in get_hooks(paths):
        frappe.get_attr(path)(ctx)


def merged_defaults(endpoint, partner):
    d = {}
    d.update(parse_json_field(partner.defaults, {}, "Partner Defaults") or {})
    if partner.company:
        d.setdefault("company", partner.company)
    d.update(parse_json_field(endpoint.defaults, {}, "Endpoint Defaults") or {})
    return d


def handler_settings(endpoint, partner):
    s = {}
    s.update(parse_json_field(partner.handler_settings, {}, "Partner Handler Settings") or {})
    s.update(parse_json_field(endpoint.handler_settings, {}, "Endpoint Handler Settings") or {})
    return frappe._dict(s)


def endpoint_url(endpoint_name, direction):
    method = "push" if direction == "Inbound" else "pull"
    return f"{frappe.utils.get_url()}/api/method/neo_connect.api.{method}?endpoint={endpoint_name}"
