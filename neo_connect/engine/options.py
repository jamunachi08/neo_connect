"""Select fields that grow with the partner's values ("keep values as sent")."""

import frappe


def current_options(doctype, fieldname):
    df = frappe.get_meta(doctype).get_field(fieldname)
    if not df or df.fieldtype != "Select":
        return None
    return (df.options or "").split("\n")


def ensure_select_option(doctype, fieldname, value):
    """
    Add `value` to the options of a Select field if it is missing. Pure data updates (no DDL), so it is
    rolled back with the record in a dry run or on failure. Returns True when an option was added.
    """
    if value in (None, ""):
        return False
    opts = current_options(doctype, fieldname)
    if opts is None or str(value) in opts:
        return False
    lead = [""] if opts and opts[0] == "" else []          # keep the leading blank (no selection) if present
    clean = lead + [o for o in opts if o != ""]
    new = "\n".join(clean + [str(value)])
    ps = frappe.db.get_value("Property Setter", {"doc_type": doctype, "field_name": fieldname, "property": "options"})
    cf = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
    if ps:
        frappe.db.set_value("Property Setter", ps, "value", new)
    elif cf:
        frappe.db.set_value("Custom Field", cf, "options", new)
    else:
        from frappe.custom.doctype.property_setter.property_setter import make_property_setter
        make_property_setter(doctype, fieldname, "options", new, "Text", validate_fields_for_doctype=False)
    frappe.clear_cache(doctype=doctype)
    return True
