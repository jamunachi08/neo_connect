"""
Sample-payload analyser (pure Python, no Frappe imports).

1. parse_payload_text()  - reads pasted/uploaded text: plain JSON, a JSON array of records, or a
                           document (e.g. a Markdown spec) containing ```json blocks. Tolerates
                           trailing commas and reports every repair it made.
2. profile()             - flattens one or many records into paths (items[].sku ...) with type,
                           sample value and how many samples contain it.
3. suggest()             - proposes how each path maps into the target ERPNext document, which
                           built-in handlers to switch on and with which settings.

The target document is passed in as a plain description (see describe_target in generator.py) so
this module can be unit-tested without a bench.
"""

import json
import re

# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def tolerant_loads(text):
    """json.loads that also accepts trailing commas. Returns (obj, notes)."""
    text = text.strip().lstrip("﻿")
    try:
        return json.loads(text), []
    except ValueError as first_error:
        fixed, count = _TRAILING_COMMA_RE.subn(r"\1", text)
        if count:
            try:
                return json.loads(fixed), [
                    f"Repaired {count} trailing comma(s). The partner must send valid JSON in production "
                    f"(original error: {first_error})."]
            except ValueError:
                pass
        raise ValueError(f"Not valid JSON: {first_error}")


def parse_payload_text(text):
    """Returns (records, notes). records = list of JSON objects found in the text."""
    text = (text or "").strip()
    if not text:
        return [], []
    chunks = _FENCE_RE.findall(text) if "```" in text else [text]
    if not chunks:
        raise ValueError("No JSON found (expected JSON, or a document with ```json blocks)")
    records, notes = [], []
    for n, chunk in enumerate(chunks, 1):
        obj, chunk_notes = tolerant_loads(chunk)
        prefix = f"Block {n}: " if len(chunks) > 1 else ""
        notes += [prefix + x for x in chunk_notes]
        if isinstance(obj, list):
            records += [o for o in obj if isinstance(o, dict)]
            notes.append(f"{prefix}JSON array: {len(obj)} record(s) used as samples")
        elif isinstance(obj, dict):
            records.append(obj)
    return records, notes


# ---------------------------------------------------------------------------
# 2. Profiling
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def value_type(v):
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        m = _DATE_RE.match(v.strip())
        if m:
            return "datetime" if m.group(1) else "date"
        return "text"
    if v is None:
        return "null"
    return "other"


def has_offset(v):
    return isinstance(v, str) and bool(re.search(r"(Z|[+-]\d{2}:?\d{2})$", v.strip()))


def _walk(obj, prefix, out, seen):
    for key, val in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            _note(out, seen, path, "object", None)
            _walk(val, path, out, seen)
        elif isinstance(val, list):
            if val and all(isinstance(x, dict) for x in val):
                _note(out, seen, path + "[]", "list", None)
                for el in val:
                    _walk(el, path + "[]", out, seen)
            else:
                _note(out, seen, path, "array", val[:3])
        else:
            _note(out, seen, path, value_type(val), val)


def _note(out, seen, path, typ, sample):
    info = out.setdefault(path, {"types": set(), "samples": [], "count": 0})
    if typ != "null" or not info["types"]:
        info["types"].add(typ)
    if sample not in (None, "") and len(info["samples"]) < 3 and sample not in info["samples"]:
        info["samples"].append(sample)
    if path not in seen:
        seen.add(path)
        info["count"] += 1


def profile(records):
    """{path: {"type", "samples", "count", "total"}} for all records (union of all samples)."""
    out = {}
    for rec in records:
        _walk(rec, "", out, set())
    result = {}
    for path, info in out.items():
        types = info["types"] - {"null"} or info["types"]
        typ = sorted(types)[0] if len(types) == 1 else "/".join(sorted(types))
        result[path] = {"type": typ, "samples": info["samples"], "count": info["count"], "total": len(records)}
    return result


# ---------------------------------------------------------------------------
# 2b. Specification tables (Markdown docs from the partner)
# ---------------------------------------------------------------------------

_SPEC_TYPE = {"string": "text", "str": "text", "text": "text", "varchar": "text", "decimal": "number",
              "number": "number", "float": "number", "double": "number", "numeric": "number", "int": "integer",
              "integer": "integer", "datetime": "datetime", "timestamp": "datetime", "date": "date",
              "boolean": "boolean", "bool": "boolean", "array": "array", "object": "object", "json": "text"}
_PATH_SYNONYMS = {"billing": ["billing_address"], "shipping": ["fulfillment", "shipping_address"],
                  "delivery": ["fulfillment"], "pickup": ["fulfillment"], "customer": ["customer"],
                  "payment": ["payment"], "installer": ["installer"]}


def parse_spec_tables(text):
    """
    Field tables in a spec document: | Field Name | Type | Description | Example |
    Returns [{"name", "type", "description", "examples", "section", "items"}].
    """
    out, section, header = [], "", None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            section, header = line.lstrip("#").strip(), None
            continue
        if not line.startswith("|"):
            header = None if not line else header
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        low = [c.lower() for c in cells]
        if any("field" in c for c in low) and header is None:
            header = {name: i for i, name in enumerate(low)}
            continue
        if header is None or set(line) <= set("|-: "):
            continue
        name_cell = cells[0] if cells else ""
        m = re.match(r"^`([^`]+)`", name_cell)
        if not m:
            continue
        def col(key):
            for h, i in header.items():
                if key in h and i < len(cells):
                    return cells[i]
            return ""
        examples = re.findall(r"`([^`]*)`", col("example"))
        out.append({"name": m.group(1).strip(), "type": _SPEC_TYPE.get(col("type").lower().strip(), "text"),
                    "description": col("description"), "examples": examples, "section": section,
                    "items": "item" in section.lower()})
    return out


def _spec_variants(name):
    """customer_email -> customer.email ; billing.street -> billing_address.street ; shipping_method -> fulfillment.method"""
    vs = [name]
    parts = name.split(".")
    if len(parts) == 1 and "_" in name:
        head, tail = name.split("_", 1)
        vs.append(f"{head}.{tail}")
        parts = [head, tail]
    if len(parts) > 1:
        for alt in _PATH_SYNONYMS.get(parts[0], []):
            vs.append(".".join([alt] + parts[1:]))
    return vs


def merge_spec(prof, spec_fields, items_path=None):
    """
    Add spec-only fields to the profile and report differences between the spec and the real JSON.
    Returns (prof, report) where report rows are {"spec_name", "json_path", "status", "note"}.
    """
    report, matched_prefix = [], {}
    items_prefix = (items_path or "items[]") + "."
    seen = set()

    def find(name, in_items):
        names = [items_prefix + v for v in _spec_variants(name)] if in_items else _spec_variants(name)
        for v in names:
            if v in prof:
                return v
        leaf = name.split(".")[-1]
        cands = [p for p in prof if p.split(".")[-1].replace("[]", "") == leaf
                 and (p.startswith(items_prefix) == in_items)]
        return cands[0] if len(cands) == 1 else None

    pending = []
    for f in spec_fields:
        key = (f["name"], f["items"])
        if key in seen:
            continue
        seen.add(key)
        path = find(f["name"], f["items"])
        if path:
            info = prof[path]
            info["source"] = "Sample + Spec"
            info["spec_name"] = f["name"]
            info["spec_description"] = f["description"]
            for ex in f["examples"]:
                if ex not in [str(x) for x in info["samples"]] and len(info["samples"]) < 6:
                    info["samples"].append(ex)
            parent = path.rsplit(".", 1)[0] if "." in path else ""
            matched_prefix.setdefault(f["section"], []).append(parent)
            if path not in (f["name"], items_prefix + f["name"]):
                report.append({"spec_name": f["name"], "json_path": path, "status": "Renamed",
                               "note": "Spec name differs from the sample JSON path; the JSON path is used"})
        else:
            pending.append(f)

    for f in pending:
        parents = [p for p in matched_prefix.get(f["section"], []) if p]
        leaf = f["name"].split(".")[-1]
        if f["items"]:
            path = items_prefix + leaf
        elif "." in f["name"]:
            head = f["name"].split(".")[0]
            alt = next((a for a in _PATH_SYNONYMS.get(head, []) if any(p.startswith(a) for p in prof)), head)
            path = ".".join([alt] + f["name"].split(".")[1:])
        elif parents and len(set(parents)) == 1:
            path = f"{parents[0]}.{leaf}"
        else:
            path = f["name"]
        if path in prof:
            continue
        prof[path] = {"type": f["type"], "samples": list(f["examples"][:6]), "count": 0,
                      "total": next(iter(prof.values()))["total"] if prof else 0, "source": "Spec",
                      "spec_name": f["name"], "spec_description": f["description"]}
        report.append({"spec_name": f["name"], "json_path": path, "status": "Spec only",
                       "note": "Not in the sample JSON; path assumed - confirm with the partner"})

    for p, info in prof.items():
        info.setdefault("source", "Sample")
        if info["source"] == "Sample" and info["type"] not in ("object", "list"):
            report.append({"spec_name": "", "json_path": p, "status": "Sample only",
                           "note": "In the sample JSON but not documented in the spec"})
    return prof, report


# ---------------------------------------------------------------------------
# 3. Suggestions

# ---------------------------------------------------------------------------

def norm_tokens(text):
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(text or ""))
    return [t for t in re.split(r"[^0-9a-z؀-ۿ]+", text.lower()) if t]


# Business concepts in English and Arabic, used to match payload keys to (custom) ERPNext fields.
CONCEPTS = {
    "payment_method": ["payment method", "method title", "payment type", "طريقة الدفع", "الدفع"],
    "sales_person": ["executive", "salesperson", "sales person", "sales rep", "مندوب", "المبيعات"],
    "fitting_location": ["installer", "fitting", "pickup store", "workshop", "موقع التركيب", "التركيب"],
    "vehicle_make": ["vehicle make", "car make", "make", "manufacturer", "car details", "ماركة", "الشركة المصنعة",
                     "صفات السيارة"],
    "mobile": ["mobile", "mobile number", "telephone", "phone", "رقم الجوال", "الجوال", "جوال"],
    "vehicle_model": ["vehicle model", "car model", "model", "موديل", "طراز"],
    "vehicle_year": ["vehicle year", "model year", "year", "سنة الصنع"],
    "plate": ["plate", "license plate", "لوحة", "رقم اللوحة"],
    "vin": ["vin", "chassis", "رقم الهيكل", "الهيكل"],
    "delivery_method": ["shipping method", "delivery method", "fulfillment method", "طريقة التوصيل", "طريقة الشحن"],
    "delivery_note_text": ["delivery comment", "delivery instructions", "comment", "ملاحظات التوصيل"],
    "order_status": ["custom order status", "order status", "حالة الطلب"],
    "store": ["store name", "store view", "المتجر"],
    "pickup_time": ["pickup time", "time slot", "وقت الاستلام", "الموعد"],
    "pickup_date": ["pickup date", "fitting date", "appointment", "تاريخ الاستلام", "تاريخ التركيب"],
    "supplier_code": ["supplier item code", "supplier code", "كود المورد"],
    "brand": ["brand", "العلامة التجارية"],
    "width": ["width", "العرض"],
    "height": ["height", "aspect ratio", "الارتفاع"],
    "rim": ["rim", "الجنط", "القطر"],
    "speed_index": ["speed index", "speed rating", "مؤشر السرعة"],
}

_GENERIC = {"custom", "name", "id", "code", "data", "value", "info", "the", "of", "no", "number"}


def concepts_of(text):
    txt = " " + " ".join(norm_tokens(text)) + " "
    found = set()
    for concept, words in CONCEPTS.items():
        for w in words:
            if " " + " ".join(norm_tokens(w)) + " " in txt:
                found.add(concept)
                break
    return found


def match_score(path, field):
    """0..1 similarity between a payload path and an ERPNext field (fieldname + label)."""
    ptxt = path.replace("[]", "").replace(".", " ")
    ftxt = f"{field['fieldname']} {field.get('label') or ''}"
    pc, fc = concepts_of(ptxt), concepts_of(ftxt)
    if pc & fc:
        return 0.9
    pt = set(norm_tokens(ptxt)) - _GENERIC
    ft = set(norm_tokens(ftxt)) - _GENERIC
    if not pt or not ft:
        return 0.0
    return len(pt & ft) / len(pt | ft)


TRANSFORM_BY_FIELDTYPE = {
    "Date": "Date", "Datetime": "Datetime", "Time": "Time", "Int": "Integer", "Float": "Float",
    "Currency": "Float", "Percent": "Float", "Check": "Check",
}
MAPPABLE_FIELDTYPES = {
    "Data", "Select", "Link", "Small Text", "Text", "Long Text", "Text Editor", "Date", "Datetime",
    "Time", "Int", "Float", "Currency", "Percent", "Check", "Phone", "Read Only",
}


def _row(path, info, role, erp_field="", transform="None", include=1, note="", confidence=100, **kw):
    sample = info["samples"][0] if info and info.get("samples") else ""
    row = {
        "include": include, "external_path": path, "data_type": info["type"] if info else "",
        "sample_value": json.dumps(sample, ensure_ascii=False) if isinstance(sample, (list, dict)) else str(sample),
        "presence": f"{info['count']}/{info['total']}" if info else "", "role": role,
        "erp_field": erp_field, "transform": transform, "note": note, "confidence": confidence,
    }
    if info:
        row.setdefault("source", info.get("source", "Sample"))
        if info.get("spec_name"):
            row.setdefault("spec_name", info["spec_name"])
    row.update(kw)
    return row


def _leaf(path):
    return path.split(".")[-1].replace("[]", "").lower()


def _first(paths, *candidates, within=None):
    """First existing path whose leaf is in candidates (optionally inside a parent key)."""
    for cand in candidates:
        for p in paths:
            if _leaf(p) == cand and (within is None or any(w in p.lower() for w in within)):
                return p
    return None


# Common payment/channel codes and the Arabic labels used for them in ERPNext Select fields
VALUE_SYNONYMS = {
    "tap": ["تاب", "تاب باي", "Tap"], "tappayment": ["تاب", "Tap"], "tamara": ["تمارا", "Tamara"],
    "tabby": ["تابي", "Tabby"], "cashondelivery": ["نقدي", "الدفع عند الاستلام", "كاش", "Cash"],
    "cod": ["نقدي", "الدفع عند الاستلام", "Cash"], "cash": ["نقدي", "كاش", "Cash"],
    "banktransfer": ["تحويل بنكي", "Bank Transfer", "Wire Transfer"], "bank": ["تحويل بنكي", "Bank Transfer"],
    "mada": ["مدى", "Mada"], "applepay": ["ابل باي", "أبل باي", "Apple Pay"],
    "creditcard": ["بطاقة ائتمان", "Credit Card"], "card": ["بطاقة ائتمان", "Credit Card"],
    "visa": ["فيزا", "بطاقة ائتمان", "Credit Card"], "stcpay": ["اس تي سي باي", "stc pay"],
}


def _vkey(v):
    k = re.sub(r"[^0-9a-z]+", "", str(v).lower())
    return k[:-7] if k.endswith("payment") and len(k) > 7 else k


def auto_value_map(values, options):
    """Translate payload values to Select options. Returns (map, values_without_option)."""
    vmap, missing = {}, []
    norm_opts = {re.sub(r"\s+", " ", o).strip(): o for o in options}
    for v in values:
        if v in options:
            vmap[v] = v
            continue
        hit = next((o for o in options if _vkey(o) and _vkey(o) == _vkey(v)), None)
        if not hit:
            for syn in VALUE_SYNONYMS.get(_vkey(v), []):
                hit = norm_opts.get(syn) or next((o for o in options if syn in o), None)
                if hit:
                    break
        if hit:
            vmap[v] = hit
        else:
            missing.append(v)
    return vmap, missing


_WRAPPERS = {"totals", "total", "data", "details", "detail", "info", "attributes", "attrs", "meta", "extra"}


def propose_field(path, info, in_items=False):
    """Proposed custom field for a payload value that has no home in ERPNext yet."""
    parts = [p.replace("[]", "") for p in path.split(".")]
    base = parts[-1:] if in_items or len(parts) == 1 else parts[-2:]
    base = [re.sub(r"^custom_", "", b) for b in base if b.lower() not in _WRAPPERS] or parts[-1:]
    if len(base) == 2 and base[1].lower().startswith(base[0].lower()):
        base = base[1:]                                      # installer.installer_type -> installer_type
    words = [w for w in "_".join(base).replace("-", "_").split("_") if w]
    label = " ".join(w.upper() if w.lower() in ("vin", "id", "sku", "url") else w.capitalize() for w in words)
    fieldname = "custom_" + re.sub(r"[^0-9a-z]+", "_", "_".join(base).lower()).strip("_")[:55]
    leaf = parts[-1].lower()
    tokens = set(re.split(r"[^a-z0-9]+", leaf))
    typ = (info or {}).get("type", "text").split("/")[0]
    options = ""
    if typ == "datetime":
        ft = "Datetime"
    elif typ == "date":
        ft = "Date"
    elif typ == "boolean":
        ft = "Check"
    elif typ == "integer":
        ft = "Int"
    elif typ == "number":
        ft = "Currency" if tokens & {"amount", "price", "total", "fee", "cost", "paid", "refunded", "subtotal"} \
            else "Float"
    elif typ in ("array", "object"):
        ft = "Small Text"
    elif tokens & {"latitude", "longitude", "lat", "lng", "lon"}:
        ft = "Float"
    elif tokens & {"comment", "comments", "note", "notes", "instruction", "instructions", "address", "description",
                   "hours", "location"}:
        ft = "Small Text"
    elif "email" in tokens:
        ft, options = "Data", "Email"
    elif tokens & {"phone", "mobile", "telephone", "tel"}:
        ft, options = "Data", "Phone"
    else:
        ft = "Data"
    return {"proposed_fieldname": fieldname, "proposed_label": label, "proposed_fieldtype": ft,
            "proposed_options": options}


def suggest(prof, target, platform=None, opts=None):
    """
    prof:   output of profile()
    target: {"doctype", "fields": [field...], "tables": {"items": {"doctype", "fields": [...]}}}
            field = {"fieldname", "label", "fieldtype", "options", "reqd"}
    Returns {"rows": [...], "external_id_path", "records_path", "settings": {...},
             "pre_hooks": [...], "post_hooks": [...], "defaults": {...}, "notes": [...]}
    """
    dt = target["doctype"]
    pfields = {f["fieldname"]: f for f in target["fields"]}
    items_table = target.get("tables", {}).get("items")
    ifields = {f["fieldname"]: f for f in (items_table or {}).get("fields", [])}
    leaves = [p for p, i in prof.items() if i["type"] not in ("object", "list")]
    used, rows, notes, settings = set(), [], [], {}
    is_magento = (platform or "").lower().startswith("magento")

    def take(path, *a, **kw):
        if path and path not in used:
            used.add(path)
            rows.append(_row(path, prof.get(path), *a, **kw))

    def date_tz(path):
        smp = (prof.get(path) or {}).get("samples") or []
        if prof.get(path, {}).get("type") == "datetime" and smp and not has_offset(smp[0]) and is_magento:
            return "UTC"
        return ""

    # --- external id -------------------------------------------------------------------------
    top = [p for p in leaves if "." not in p and "[]" not in p]
    ext = _first(top, "order_number", "increment_id", "order_id", "magento_order_id", "order_no",
                 "reference", "external_id", "number", "id")
    if ext:
        take(ext, "External ID", "po_no" if "po_no" in pfields else "", "Text",
             note="Unique order id: duplicates are detected with it", required=1)
    else:
        notes.append("No unique order id found. Ask the partner to send one (e.g. order_number).")

    # --- dates -------------------------------------------------------------------------------
    order_date = _first(top, "order_date", "created_at", "order_created_at", "date", "createdat")
    date_field = "posting_date" if "posting_date" in pfields else "transaction_date" if "transaction_date" in pfields else None
    if order_date and date_field:
        take(order_date, "Field", date_field, "Date", required=1, source_timezone=date_tz(order_date),
             note="Order date" + (" (UTC from Magento, converted to local time)" if date_tz(order_date) else ""))
        if "posting_time" in pfields and prof[order_date]["type"] == "datetime":
            rows.append(_row(order_date, prof[order_date], "Field", "posting_time", "Time",
                             source_timezone=date_tz(order_date)))
        if "po_date" in pfields:
            rows.append(_row(order_date, prof[order_date], "Field", "po_date", "Date",
                             source_timezone=date_tz(order_date)))
    if "delivery_date" in pfields:
        cands = [p for c in ("delivery_date", "requested_delivery_date", "pickup_date", "ship_date", "shipping_date")
                 for p in leaves if _leaf(p) == c and "[]" not in p]
        always = [p for p in cands if prof[p]["count"] == prof[p]["total"]]
        if always and len(cands) == 1:
            src = always[0]
            for erp in ["delivery_date"] + (["items.delivery_date"] if "delivery_date" in ifields else []):
                if src in used:
                    rows.append(_row(src, prof[src], "Field", erp, "Date", source_timezone=date_tz(src)))
                else:
                    take(src, "Field", erp, "Date", source_timezone=date_tz(src))
        else:
            # different fulfilment types send different dates -> first one present, else the order date
            chain = cands + ([order_date] if order_date else [])
            if chain:
                expr = "getdate(first_of(record, " + ", ".join(repr(p) for p in chain) + "))"
                label = " / ".join(chain)
                for erp in ["delivery_date"] + (["items.delivery_date"] if "delivery_date" in ifields else []):
                    rows.append(_row(label, {"type": "date", "samples": [], "count": 0, "total": 0}, "Field", erp,
                                     "Expression", expression=expr, external_path="",
                                     note=f"First date present: {label}"))
                for p in cands:
                    take(p, "Ignore", include=0, note="Used in the delivery_date fallback expression")
            if not cands:
                notes.append("No delivery/pickup date in the payload: the order date is used as delivery date.")

    # --- currency ----------------------------------------------------------------------------
    cur = _first(leaves, "order_currency_code", "currency", "currency_code")
    if cur and "currency" in pfields:
        take(cur, "Field", "currency", "Upper")
    for p in leaves:
        if _leaf(p) in ("base_currency_code", "base_grand_total", "base_subtotal"):
            take(p, "Ignore", include=0, note="Base-currency value; ERPNext converts itself")

    # --- customer ----------------------------------------------------------------------------
    cust_paths = [p for p in leaves if p.lower().startswith("customer")]
    email = _first(cust_paths, "email", "customer_email") or _first(top, "customer_email", "email")
    bill_obj = next((p for p, i in prof.items() if i["type"] == "object" and "billing" in p.lower()), None)
    bill_leaves = [p for p in leaves if bill_obj and p.startswith(bill_obj + ".")]
    if not email and bill_leaves:
        email = _first(bill_leaves, "email")
    if email:
        take(email, "Customer", "contact_email" if "contact_email" in pfields else "", "Lower",
             note="Customer is found by e-mail or created")
        settings["customer_email_path"] = email
    first = _first(cust_paths, "firstname", "first_name", "name", "full_name", "customer_firstname") \
        or _first(top, "customer_firstname", "customer_name")
    last = _first(cust_paths, "lastname", "last_name", "customer_lastname") or _first(top, "customer_lastname")
    if first:
        take(first, "Customer", note="Customer name")
        names = [first]
        if last:
            take(last, "Customer", note="Customer last name")
            names.append(last)
        settings["customer_name_paths"] = names
    phone = _first(cust_paths, "mobile", "phone", "telephone") or _first(bill_leaves, "telephone", "phone", "mobile")
    if phone:
        take(phone, "Customer", note="Customer phone (used only when there is no e-mail)")
        settings["customer_phone_path"] = phone
    guest = _first(cust_paths, "is_guest", "customer_is_guest") or _first(top, "customer_is_guest")
    if guest:
        take(guest, "Ignore", include=0, note="Guests are handled like customers (found/created by e-mail)")

    # --- billing address ---------------------------------------------------------------------
    if bill_obj:
        settings["billing_address_path"] = bill_obj
        amap = {}
        def rel(p):
            return p[len(bill_obj) + 1:]
        street = _first(bill_leaves, "street", "address", "address1", "line1", "street1")
        if street:
            amap["line1"] = rel(street) + ("[0]" if prof[street]["type"] == "array" else "")
            if prof[street]["type"] == "array":
                amap["line2"] = rel(street) + "[1]"
        for key, cands in (("city", ("city", "town")), ("state", ("region", "state", "province")),
                           ("pincode", ("postcode", "zip", "postal_code", "zipcode"))):
            p = _first(bill_leaves, *cands)
            if p:
                amap[key] = rel(p)
        cc = _first(bill_leaves, "country_id", "country_code")
        cn = _first(bill_leaves, "country")
        if cc:
            amap["country_code"] = rel(cc)
        elif cn:
            amap["country"] = rel(cn)
        settings["address_map"] = amap
        for p in bill_leaves:
            take(p, "Address", note="Billing address of a new customer")

    # --- items -------------------------------------------------------------------------------
    item_lists = [p for p, i in prof.items() if i["type"] == "list"
                  and any(_leaf(c) in ("sku", "item_code", "product_code", "product_sku")
                          for c in prof if c.startswith(p + "."))]
    items_path = item_lists[0] if item_lists else None
    if items_path and items_table:
        settings["items_path"] = items_path.replace("[]", "")
        il = [p for p in leaves if p.startswith(items_path + ".") and "[]" not in p[len(items_path) + 1:]]
        sku = _first(il, "sku", "item_code", "product_code", "product_sku")
        take(sku, "Field", "items.item_code", "Lookup", required=1, lookup_doctype="Item",
             lookup_field="item_code", if_not_found="Error", note="Must equal the ERPNext Item Code")
        qty = _first(il, "qty_ordered", "qty", "quantity")
        if qty:
            take(qty, "Field", "items.qty", "Float", required=1)
        rate = _first(il, "price", "unit_price", "rate", "price_excl_tax", "base_price")
        if rate:
            take(rate, "Field", "items.rate", "Float", required=1, note="Unit price excluding VAT")
        for p in il:
            leaf = _leaf(p)
            if leaf in ("price_incl_tax", "row_total", "row_total_incl_tax", "tax_amount", "qty_invoiced",
                        "qty_shipped", "qty_refunded", "qty_canceled", "base_price", "original_price"):
                take(p, "Ignore", include=0, note="ERPNext calculates this")
            elif leaf == "discount_amount":
                take(p, "Ignore", include=0, note="Line discounts are covered by the order discount_amount")
            elif leaf in ("name", "product_name", "item_name"):
                take(p, "Ignore", include=0, note="Item name comes from the Item master")
            elif leaf in ("parent_item_id",):
                take(p, "Ignore", include=0, note="Use a Row Filter to skip configurable child lines")
    elif not items_path:
        notes.append("No list of order lines (items[] with sku) found.")

    # --- totals, shipping, discount ----------------------------------------------------------
    non_item = [p for p in leaves if not (items_path and p.startswith(items_path + "."))]
    ship = _first(non_item, "shipping_amount", "shipping", "shipping_total", "delivery_fee")
    if ship:
        take(ship, "Shipping", note="Shipping charge excl. VAT")
        settings["shipping_amount_path"] = ship
    disc = _first(non_item, "discount_amount", "discount", "discount_total")
    if disc:
        take(disc, "Discount", note="Order discount (sign ignored)")
        settings["discount_amount_path"] = disc
        settings["discount_on"] = "Net Total"
    gt = _first(non_item, "grand_total", "total", "order_total")
    if gt:
        take(gt, "Grand Total", note="Compared with the ERPNext total")
        settings["grand_total_path"] = gt
    for p in non_item:
        if _leaf(p) in ("subtotal", "subtotal_incl_tax", "tax_amount", "shipping_incl_tax", "total_refunded",
                        "base_subtotal", "total_tax", "total_due", "amount_ordered"):
            take(p, "Ignore", include=0, note="ERPNext calculates this from lines + tax template")

    # --- payment -----------------------------------------------------------------------------
    pay = [p for p in non_item if "payment" in p.lower()]
    payment_mop_map = None
    method = _first(pay, "method", "payment_method", "code") or _first(top, "payment_method")
    if method:
        take(method, "Payment", note="Payment method code -> Mode of Payment map")
        settings["payment_method_path"] = method
        samples = []
        for p in [method]:
            samples += [str(s) for s in prof[p]["samples"]]
        mops = target.get("modes_of_payment") or []
        mmap, mmissing = auto_value_map(samples, mops) if mops else ({}, samples)
        for v in mmissing:
            mmap[v] = "<Mode of Payment>"
        mmap["*"] = "<Mode of Payment>"
        payment_mop_map = mmap
    paid = _first(pay, "amount_paid", "paid_amount") or _first(top, "total_paid")
    if paid:
        take(paid, "Payment", note="Amount paid: payment is recorded only when > 0")
        settings["paid_amount_path"] = paid
    ref = _first(pay, "transaction_id", "last_trans_id", "reference", "txn_id")
    if ref:
        take(ref, "Payment", note="Gateway reference on the Payment Entry")
        settings["payment_reference_path"] = ref
    mt = _first(pay, "method_title", "payment_method_title")
    if mt and method:
        take(mt, "Ignore", include=0, note=f"Display name of {method} (already used)")
    tp = _first(top, "total_paid")
    if tp and tp not in used:
        take(tp, "Ignore", include=0, note="Same as payment amount paid")

    # --- cost center -------------------------------------------------------------------------
    ccp = _first(leaves, "cost_center", "costcenter")
    if ccp:
        take(ccp, "Cost Center", note="Selected if it exists, created if not")
        settings["cost_center_path"] = ccp

    # --- status ------------------------------------------------------------------------------
    st = _first(top, "status", "state")
    if st:
        take(st, "Ignore", include=0, note="Order status: not stored (use a Row Filter/hook to skip canceled)")

    # --- everything else: match to custom fields (parent first, then item rows) ---------------
    # Fields fetched from the Customer (fetch_from "customer.x") cannot be written on the document:
    # ERPNext re-fetches them on save. Such values are written to the Customer instead.
    def candidates(fields, prefix=""):
        return [dict(f, fieldname=prefix + f["fieldname"]) for f in fields.values()
                if f["fieldname"].startswith("custom_") and f.get("fieldtype") in MAPPABLE_FIELDTYPES
                and not f.get("hidden")]

    reusable_roles = ("Payment", "Customer", "Address", "Cost Center", "External ID")
    role_of = {r["external_path"]: r["role"] for r in rows if r.get("external_path")}
    pending = [p for p in leaves if p not in used or role_of.get(p) in reusable_roles]
    taken_fields = {r["erp_field"] for r in rows if r["erp_field"]}
    pairs = []
    for p in pending:
        in_items = bool(items_path and p.startswith(items_path + "."))
        pool = candidates(ifields, "items.") if in_items else candidates(pfields)
        for f in pool:
            if (f.get("fetch_from") or "").startswith("customer.") and \
                    "mobile" in concepts_of(f"{f['fieldname']} {f.get('label') or ''}"):
                continue            # the customer's mobile is taken from the customer phone below
            sc = match_score(p, f)
            if sc >= 0.5:
                leaf = _leaf(p)
                pref = 2 if leaf in ("method", "code", "id") else 1 if leaf in ("name", "title", "method_title") else 0
                pairs.append((sc, pref, -len(p), p, f))
    pairs.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    field_used = set()
    customer_fields = {}
    for sc, _a, _b, p, f in pairs:
        if p in field_used or f["fieldname"] in taken_fields:
            continue
        if p in used and role_of.get(p) not in reusable_roles:
            continue
        add = take if p not in used else (lambda path, *a, **kw: rows.append(_row(path, prof.get(path), *a, **kw)))
        fetch = f.get("fetch_from") or ""
        if fetch.startswith("customer."):
            cust_field = fetch.split(".", 1)[1]
            customer_fields[cust_field] = p
            add(p, "Customer Field", cust_field, "None", confidence=int(sc * 100), target_doctype="Customer",
                gap_status="Customer Field",
                note=f"{f.get('label') or f['fieldname']} is fetched from the Customer: value is written to "
                     f"Customer.{cust_field}")
        else:
            transform, extra, note = "None", {}, f"Matches {f.get('label') or f['fieldname']}"
            gap = "Existing Field"
            ft = f.get("fieldtype")
            if ft in TRANSFORM_BY_FIELDTYPE:
                transform = TRANSFORM_BY_FIELDTYPE[ft]
            elif ft == "Link" and f.get("options"):
                transform = "Lookup"
                extra = dict(lookup_doctype=f["options"], lookup_field="name", if_not_found="Error")
                note += f" (must exist as {f['options']})"
            elif ft == "Select":
                options = [o for o in (f.get("options") or "").split("\n") if o.strip()]
                values = [str(x) for x in prof[p]["samples"]]
                vmap, missing = auto_value_map(values, options)
                if any(k != v for k, v in vmap.items()) or missing:
                    transform = "Value Map"
                    for v in missing:
                        vmap[v] = "<add option or choose: " + " | ".join(options[:10]) + ">"
                    vmap["*"] = vmap.get("*") or "<choose default: " + " | ".join(options[:10]) + ">"
                    extra = dict(value_map=vmap, select_options=options, missing_options=missing)
                    if missing:
                        gap = "Option Gap"
                        note += f" - no option for: {', '.join(missing)}"
                    else:
                        note += " - values translated automatically"
            add(p, "Field", f["fieldname"], transform, confidence=int(sc * 100), note=note,
                target_doctype=(items_table or {}).get("doctype") if f["fieldname"].startswith("items.") else dt,
                gap_status=gap, **extra)
        field_used.add(p)
        taken_fields.add(f["fieldname"])

    # mobile number fetched from the Customer: use the customer phone
    phone_path = settings.get("customer_phone_path")
    if phone_path:
        for f in pfields.values():
            fetch = f.get("fetch_from") or ""
            if fetch.startswith("customer.") and "mobile" in concepts_of(f"{f['fieldname']} {f.get('label') or ''}"):
                cf = fetch.split(".", 1)[1]
                if cf not in customer_fields:
                    customer_fields[cf] = phone_path
                    rows.append(_row(phone_path, prof.get(phone_path), "Customer Field", cf, target_doctype="Customer",
                                     gap_status="Customer Field",
                                     note=f"{f.get('label')} is fetched from the Customer: phone written to Customer.{cf}"))
    if customer_fields:
        settings["customer_fields"] = customer_fields
        settings["update_customer_fields"] = 1

    # --- gap: everything still unmapped gets a proposed new field -----------------------------
    cust_parents = {p.rsplit(".", 1)[0] for p in customer_fields.values() if "." in p}
    for p in leaves:
        if p in used:
            continue
        in_items = bool(items_path and p.startswith(items_path + "."))
        parent = p.rsplit(".", 1)[0] if "." in p else ""
        if in_items:
            tdt = (items_table or {}).get("doctype") or dt
        elif parent and parent in cust_parents:
            tdt = "Customer"
        else:
            tdt = dt
        prop = propose_field(p, prof[p], in_items)
        note = f"No field on {tdt}: create {prop['proposed_fieldtype']} '{prop['proposed_label']}'"
        if in_items and _leaf(p) in ("brand", "width", "height", "rim", "speed_index", "supplier_item_code"):
            note += " (also available on the Item master - store on the line only if it can differ per order)"
        if tdt == "Customer":
            note += " (sibling values are kept on the Customer)"
        take(p, "Unmapped", include=0, target_doctype=tdt, gap_status="Create Field", note=note, **prop)

    for r in rows:
        r.setdefault("target_doctype", (items_table or {}).get("doctype") if (r.get("erp_field") or "").startswith("items.")
                     else (dt if r.get("erp_field") else ""))
        if "gap_status" not in r:
            r["gap_status"] = {"Ignore": "Calculated / Not needed", "Field": "Existing Field",
                               "External ID": "Existing Field"}.get(r["role"], "Handled by NeoConnect")

    # --- hooks -------------------------------------------------------------------------------
    pre = []
    if settings.get("customer_email_path") or settings.get("customer_phone_path"):
        pre.append("ensure_customer")
    pre.append("apply_charges")
    if settings.get("cost_center_path"):
        pre.append("ensure_cost_center")
    post = []
    if settings.get("grand_total_path"):
        post.append("check_grand_total")
    posting = (opts or {}).get("payment_posting") or "Invoice Payments Table"
    if dt == "Sales Invoice" and settings.get("payment_method_path") and posting != "Do Not Record":
        settings["mode_of_payment_map"] = payment_mop_map or {"*": "<Mode of Payment>"}
        if posting == "Payment Entry":
            post.append("create_payment")
            settings["create_payment"] = 1
        else:
            pre.append("add_invoice_payments")
    elif settings.get("payment_method_path") and dt != "Sales Invoice":
        notes.append(f"Payment data is not posted for a {dt}; it is kept for the invoice/payment step.")
    if not settings.get("shipping_amount_path"):
        settings["shipping_amount_path"] = "__none__"

    order = {"External ID": 0, "Field": 1, "Customer Field": 2, "Customer": 3, "Address": 4, "Shipping": 5,
             "Discount": 6, "Grand Total": 7, "Payment": 8, "Cost Center": 9, "Unmapped": 10, "Ignore": 11}
    rows.sort(key=lambda r: order.get(r["role"], 99))
    return {
        "rows": rows, "external_id_path": ext, "records_path": "", "settings": settings,
        "pre_hooks": pre, "post_hooks": post, "notes": notes,
    }
