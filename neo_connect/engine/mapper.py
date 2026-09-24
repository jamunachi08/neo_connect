"""
Pure-Python mapping engine (no Frappe imports) so it can be unit-tested
outside a bench.

Path syntax (used for both the partner JSON side and the ERP side):

    customer_email                      -> top-level key
    billing_address.email               -> nested key
    items[].sku                         -> "sku" of every element of list "items"
    extension_attributes.shipping_assignments[0].shipping.address.city
                                        -> numeric index into a list

ERP side paths:

    customer                            -> parent field
    items.item_code                     -> field "item_code" of child table "items"
"""

import re
from datetime import datetime, date
from zoneinfo import ZoneInfo

_INDEX_RE = re.compile(r"^(?P<key>[^\[\]]*)\[(?P<idx>\d*)\]$")


class MappingError(Exception):
    pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_path(data, path, default=None):
    """Read a dotted path (supports [n] indexes) from nested dict/list data."""
    if path in (None, ""):
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return default
        m = _INDEX_RE.match(part)
        if m:
            key, idx = m.group("key"), m.group("idx")
            if key:
                cur = cur.get(key) if isinstance(cur, dict) else None
            if idx == "":
                raise MappingError(f"'[]' is only allowed once, at list level: {path}")
            if isinstance(cur, list) and int(idx) < len(cur):
                cur = cur[int(idx)]
            else:
                return default
        else:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return default
    return default if cur is None else cur


def set_path(data, path, value):
    """Write a dotted path into nested dicts, creating intermediate dicts."""
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def split_list_path(path):
    """'items[].sku' -> ('items', 'sku'); 'customer_email' -> (None, 'customer_email')."""
    if "[]" not in path:
        return None, path
    list_path, _, rest = path.partition("[]")
    return list_path, rest.lstrip(".")


def split_erp_field(erp_field):
    """'items.item_code' -> ('items', 'item_code'); 'customer' -> (None, 'customer')."""
    if "." in erp_field:
        table, field = erp_field.split(".", 1)
        return table, field
    return None, erp_field


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
)


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip().replace("Z", "+0000")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise MappingError(f"Unrecognised date: {value!r}")


def to_local(dt, source_tz=None, target_tz=None):
    """Convert to the ERP time zone. Naive values are assumed to be in source_tz (if given)."""
    if not target_tz:
        return dt.replace(tzinfo=None)
    if dt.tzinfo is None:
        if not source_tz:
            return dt
        dt = dt.replace(tzinfo=ZoneInfo(source_tz))
    return dt.astimezone(ZoneInfo(target_tz)).replace(tzinfo=None)


def apply_transform(value, row, ctx):
    """
    row: dict with keys transform, default_value, value_map (dict), expression
    ctx: dict with 'lookup' callable, 'eval' callable, 'record', 'row'
    """
    t = (row.get("transform") or "None").strip()

    if t == "Static":
        return row.get("default_value")

    if value in (None, "") and row.get("default_value") not in (None, ""):
        value = row.get("default_value")

    if t in ("None", ""):
        return value
    if value is None and t not in ("Expression", "Lookup"):
        return None

    if t == "Text":
        return str(value).strip()
    if t == "Upper":
        return str(value).strip().upper()
    if t == "Lower":
        return str(value).strip().lower()
    if t == "Integer":
        return int(float(value))
    if t == "Float":
        return float(value)
    if t == "Absolute":
        return abs(float(value))
    if t == "Check":
        return 1 if str(value).lower() in ("1", "true", "yes", "y") else 0
    if t in ("Date", "Datetime", "Time"):
        dt = to_local(parse_datetime(value), row.get("source_timezone"), ctx.get("timezone"))
        fmt = {"Date": "%Y-%m-%d", "Datetime": "%Y-%m-%d %H:%M:%S", "Time": "%H:%M:%S"}[t]
        return dt.strftime(fmt)
    if t == "Value Map":
        vmap = row.get("value_map") or {}
        key = str(value)
        if key in vmap:
            return vmap[key]
        if "*" in vmap:
            return vmap["*"]
        return value
    if t == "Lookup":
        if value in (None, ""):
            return None
        return ctx["lookup"](row, value)
    if t == "Expression":
        return ctx["eval"](row.get("expression"), {
            "value": value,
            "record": ctx.get("record"),
            "row": ctx.get("row"),
        })
    raise MappingError(f"Unknown transform '{t}'")


# ---------------------------------------------------------------------------
# Inbound: partner JSON -> ERP document dict
# ---------------------------------------------------------------------------

def map_inbound(record, field_maps, lookup=None, evaluator=None, row_filters=None, timezone=None):
    """
    record:      one partner JSON object
    field_maps:  list of dicts {external_path, erp_field, transform, default_value,
                 value_map, expression, required, lookup_doctype, lookup_field, ...}
    lookup:      callable(row, value) -> resolved value
    evaluator:   callable(expression, names) -> value
    row_filters: {"items[]": callable(row_dict) -> bool}  (True = keep)
    timezone:    ERP time zone; dates with an offset (or a row source_timezone) are converted to it

    Returns (doc_dict, errors)
    """
    lookup = lookup or (lambda r, v: v)
    evaluator = evaluator or (lambda e, n: n.get("value"))
    row_filters = row_filters or {}
    doc, errors = {}, []

    parent_rows, child_groups = [], {}
    for fm in field_maps:
        if not fm.get("erp_field"):
            continue
        list_path, _ = split_list_path(fm.get("external_path") or "")
        table, _ = split_erp_field(fm["erp_field"])
        if table:
            if list_path is None:
                # scalar value copied into every child row
                child_groups.setdefault((table, None), []).append(fm)
            else:
                child_groups.setdefault((table, list_path), []).append(fm)
        else:
            parent_rows.append(fm)

    # parent fields
    for fm in parent_rows:
        _map_one(fm, record, record, None, doc, errors, lookup, evaluator, timezone=timezone)

    # child tables
    tables = {}
    for (table, list_path), rows in child_groups.items():
        tables.setdefault(table, {"list_path": None, "rows": [], "scalar_rows": []})
        if list_path is None:
            tables[table]["scalar_rows"].extend(rows)
        else:
            if tables[table]["list_path"] not in (None, list_path):
                errors.append(f"Table '{table}' is mapped from two lists: "
                              f"{tables[table]['list_path']} and {list_path}")
                continue
            tables[table]["list_path"] = list_path
            tables[table]["rows"].extend(rows)

    for table, spec in tables.items():
        if spec["list_path"] is None:
            # a table with only scalar mappings -> single row
            source_rows = [record]
        else:
            source_rows = get_path(record, spec["list_path"], []) or []
            if not isinstance(source_rows, list):
                errors.append(f"'{spec['list_path']}' is not a list")
                continue
        keep = row_filters.get(f"{spec['list_path']}[]") if spec["list_path"] else None
        out_rows = []
        for idx, src in enumerate(source_rows):
            if keep and not keep(src):
                continue
            child = {}
            for fm in spec["rows"]:
                _, rel = split_list_path(fm["external_path"])
                _map_one(fm, src, record, src, child, errors, lookup, evaluator,
                         rel_path=rel, label=f"{table}[{idx}]", timezone=timezone)
            for fm in spec["scalar_rows"]:
                _map_one(fm, record, record, src, child, errors, lookup, evaluator,
                         label=f"{table}[{idx}]", timezone=timezone)
            out_rows.append(child)
        doc[table] = out_rows

    return doc, errors


def _map_one(fm, source, record, row, target, errors, lookup, evaluator,
             rel_path=None, label=None, timezone=None):
    path = rel_path if rel_path is not None else (fm.get("external_path") or "")
    _, field = split_erp_field(fm["erp_field"])
    raw = get_path(source, path) if path else None
    try:
        value = apply_transform(raw, fm, {
            "lookup": lookup, "eval": evaluator, "record": record, "row": row, "timezone": timezone,
        })
    except Exception as e:  # noqa: BLE001 - report every mapping error
        errors.append(f"{label or 'doc'}.{field} <- {path}: {e}")
        return
    if value in (None, ""):
        if fm.get("required"):
            errors.append(f"{label or 'doc'}.{field}: required value missing "
                          f"(source '{path or fm.get('transform')}')")
        return
    target[field] = value


# ---------------------------------------------------------------------------
# Outbound: ERP document dict -> partner JSON
# ---------------------------------------------------------------------------

def map_outbound(doc, field_maps, evaluator=None):
    """
    doc:        dict of an ERP document (parent fields + child tables as lists)
    field_maps: rows with erp_field (source) and external_path (target)
                e.g. erp_field 'items.item_code' -> external_path 'lines[].sku'
    """
    evaluator = evaluator or (lambda e, n: n.get("value"))
    out, lists = {}, {}
    for fm in field_maps:
        if not fm.get("erp_field") or not fm.get("external_path"):
            continue
        table, field = split_erp_field(fm["erp_field"])
        ctx = {"lookup": lambda r, v: v, "eval": evaluator, "record": doc, "row": None}
        if table:
            list_path, rel = split_list_path(fm["external_path"])
            if list_path is None:
                raise MappingError(f"Child field {fm['erp_field']} needs a list path "
                                   f"like 'lines[].{field}'")
            rows = doc.get(table) or []
            bucket = lists.setdefault(list_path, [dict() for _ in rows])
            for i, r in enumerate(rows):
                ctx["row"] = r
                set_path(bucket[i], rel, apply_transform(r.get(field), fm, ctx))
        else:
            set_path(out, fm["external_path"], apply_transform(doc.get(field), fm, ctx))
    for list_path, rows in lists.items():
        set_path(out, list_path, rows)
    return out


def extract_records(payload, records_path=None):
    """Return a list of records from a payload (single object, list, or wrapped list)."""
    data = get_path(payload, records_path) if records_path else payload
    if data is None:
        return []
    return data if isinstance(data, list) else [data]
