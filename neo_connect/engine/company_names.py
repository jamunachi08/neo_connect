"""
Company-abbreviation handling for masters.

ERPNext names many company-specific masters as "<name> - <ABBR>" (Cost Center "Main - PT",
Warehouse "Stores - PT", Account "Sales - PT", tax templates, ...). Partners should not have to
know that suffix: they send "Main" / "Stores", and we resolve to the real ERPNext name. On the way
out the suffix can be removed again.
"""

import frappe


def get_abbr(company):
    if not company:
        return None
    return frappe.get_cached_value("Company", company, "abbr")


def all_abbrs():
    return [a for a in frappe.get_all("Company", pluck="abbr") if a]


def with_suffix(value, abbr):
    """'Stores' -> 'Stores - PT' (unchanged if it already ends with the suffix)."""
    if not (value and abbr) or not isinstance(value, str):
        return value
    suffix = f" - {abbr}"
    return value if value.endswith(suffix) else f"{value}{suffix}"


def strip_suffix(value, abbrs):
    """'Stores - PT' -> 'Stores' for any known company abbreviation."""
    if not isinstance(value, str):
        return value
    for abbr in abbrs or []:
        suffix = f" - {abbr}"
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


class LinkResolver:
    """Fills in the company suffix on Link fields whose value does not exist as sent."""

    def __init__(self, abbr):
        self.abbr = abbr
        self._exists = {}

    def exists(self, doctype, name):
        key = (doctype, name)
        if key not in self._exists:
            self._exists[key] = bool(frappe.db.exists(doctype, name))
        return self._exists[key]

    def resolve_value(self, doctype, value):
        if not self.abbr or not isinstance(value, str) or not value or self.exists(doctype, value):
            return value
        candidate = with_suffix(value, self.abbr)
        return candidate if candidate != value and self.exists(doctype, candidate) else value

    def resolve_doc(self, doctype, doc_dict):
        """Resolve all Link fields of a document dict and its child tables (in place)."""
        meta = frappe.get_meta(doctype)
        for df in meta.get_link_fields():
            if df.fieldname in doc_dict and df.options:
                doc_dict[df.fieldname] = self.resolve_value(df.options, doc_dict[df.fieldname])
        for df in meta.get_table_fields():
            for row in doc_dict.get(df.fieldname) or []:
                if isinstance(row, dict):
                    self.resolve_doc(df.options, row)
        return doc_dict
