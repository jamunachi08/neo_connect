"""
Ready-made hooks for e-commerce flows. They are platform-neutral: every
source path they read comes from the endpoint/partner "Handler Settings" JSON,
so the same hooks work for Magento, Shopify, WooCommerce, Salla, Zid, custom shops...

Pre-process hooks receive ctx before the document is inserted
(ctx.doc is a plain dict you can change). Post-process hooks receive ctx after
insert/submit (ctx.document is the saved document).

ctx keys: endpoint, partner, record, doc, settings, errors, warnings,
          external_id, document, log_name
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from neo_connect.engine.mapper import get_path


def _cfg(ctx, key, default=None):
    value = ctx.settings.get(key)
    return default if value in (None, "") else value


def _joined(record, paths):
    if isinstance(paths, str):
        paths = [paths]
    parts = [str(get_path(record, p) or "").strip() for p in paths or []]
    return " ".join(p for p in parts if p).strip()


# ---------------------------------------------------------------------------
# PRE: customer
# ---------------------------------------------------------------------------

def ensure_customer(ctx):
    """
    Find the customer by e-mail (by phone only when the order has no e-mail);
    create Customer, Contact and Address if missing.

    Handler Settings (all optional, defaults shown for Magento):
      "customer_email_path":  "customer_email"
      "customer_name_paths":  ["customer_firstname", "customer_lastname"]
      "customer_phone_path":  "billing_address.telephone"
      "billing_address_path": "billing_address"      (object with street/city/postcode/country_id)
      "address_map": {"line1": "street[0]", "line2": "street[1]", "city": "city",
                      "state": "region", "pincode": "postcode", "country_code": "country_id"}
                      (use "country" instead of "country_code" when the payload has a country name, e.g. "Saudi Arabia")
      "customer_group": "Individual", "territory": "All Territories",
      "customer_type": "Individual",
      "guest_customer": "Web Guest"      (used when no e-mail and no phone)
      "match_phone_when_email_present": 0  (1 = also match by phone when the e-mail is unknown)
    """
    if ctx.doc.get("customer"):
        # already resolved by a mapping row (e.g. a Lookup)
        if frappe.db.exists("Customer", ctx.doc["customer"]):
            return

    record = ctx.record
    email = (get_path(record, _cfg(ctx, "customer_email_path", "customer_email")) or "").strip().lower()
    phone = str(get_path(record, _cfg(ctx, "customer_phone_path", "billing_address.telephone")) or "").strip()
    full_name = _joined(record, _cfg(ctx, "customer_name_paths", ["customer_firstname", "customer_lastname"]))

    customer = None
    if email:
        customer = frappe.db.get_value("Customer", {"email_id": email}, "name") or _customer_from_contact(
            "Contact Email", "email_id", email)
    # Phone is only used when there is no e-mail (or when explicitly enabled): different people
    # can share a phone number, and an e-mail is a stronger identity.
    if not customer and phone and (not email or int(_cfg(ctx, "match_phone_when_email_present", 0))):
        customer = frappe.db.get_value("Customer", {"mobile_no": phone}, "name") or _customer_from_contact(
            "Contact Phone", "phone", phone)

    if not customer and not email and not phone:
        guest = _cfg(ctx, "guest_customer")
        if not guest:
            ctx.errors.append(_("No customer e-mail/phone in payload and no 'guest_customer' configured"))
            return
        ctx.doc["customer"] = guest
        return

    if not customer:
        customer = _create_customer(ctx, full_name or email or phone, email, phone)

    ctx.doc["customer"] = customer


def _customer_from_contact(child_doctype, field, value):
    rows = frappe.db.sql(
        f"""
        select dl.link_name
        from `tab{child_doctype}` c
        join `tabDynamic Link` dl on dl.parent = c.parent and dl.parenttype = 'Contact'
        where c.{field} = %s and dl.link_doctype = 'Customer'
        limit 1
        """,
        value,
    )
    return rows[0][0] if rows else None


def _create_customer(ctx, name, email, phone):
    ignore = True  # the integration user may not have Contact/Address rights
    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": name,
        "customer_type": _cfg(ctx, "customer_type", "Individual"),
        "customer_group": _cfg(ctx, "customer_group")
        or frappe.db.get_single_value("Selling Settings", "customer_group") or "All Customer Groups",
        "territory": _cfg(ctx, "territory")
        or frappe.db.get_single_value("Selling Settings", "territory") or "All Territories",
    })
    customer.flags.ignore_permissions = ignore
    customer.insert()

    if email or phone:
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": name,
            "links": [{"link_doctype": "Customer", "link_name": customer.name}],
        })
        if email:
            contact.append("email_ids", {"email_id": email, "is_primary": 1})
        if phone:
            contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
        contact.flags.ignore_permissions = ignore
        contact.insert()
        customer.db_set({"customer_primary_contact": contact.name, "email_id": email or None,
                         "mobile_no": phone or None})

    address = _build_address(ctx, customer.name, name)
    if address:
        address.flags.ignore_permissions = ignore
        address.insert()
        customer.db_set("customer_primary_address", address.name)

    ctx.warnings.append(_("Created customer {0}").format(customer.name))
    return customer.name


def _build_address(ctx, customer, title):
    src = get_path(ctx.record, _cfg(ctx, "billing_address_path", "billing_address"))
    if not isinstance(src, dict):
        return None
    amap = _cfg(ctx, "address_map", {
        "line1": "street[0]", "line2": "street[1]", "city": "city", "state": "region",
        "pincode": "postcode", "country_code": "country_id",
    })

    def val(*keys):
        """Text value for the first mapped key; None when the key is not mapped or the value is empty."""
        for key in keys:
            path = amap.get(key)
            if path:
                v = get_path(src, path)
                if v not in (None, "") and not isinstance(v, (dict, list)):
                    return str(v).strip()
        return None

    line1, city = val("line1"), val("city")
    if not (line1 and city):
        return None
    # country may be an ISO code ("SA") or a name ("Saudi Arabia")
    raw_country = val("country_code", "country")
    country = None
    if raw_country:
        country = (frappe.db.exists("Country", raw_country)
                   or frappe.db.get_value("Country", {"code": raw_country.lower()}, "name"))
    country = country or frappe.db.get_default("country")
    return frappe.get_doc({
        "doctype": "Address",
        "address_title": title,
        "address_type": "Billing",
        "address_line1": line1[:140],
        "address_line2": (val("line2") or "")[:140] or None,
        "city": city,
        "state": val("state"),
        "pincode": val("pincode"),
        "country": country,
        "is_primary_address": 1,
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })


# ---------------------------------------------------------------------------
# PRE: taxes, shipping, discount
# ---------------------------------------------------------------------------

def apply_charges(ctx):
    """
    Adds taxes from the Sales Taxes and Charges Template, a shipping line and an order discount.

    Handler Settings:
      "shipping_amount_path": "shipping_amount",
      "shipping_item": "SHIPPING",        (recommended: non-stock service item, gets VAT like other lines)
      "shipping_account": "Freight and Forwarding Charges - YC",   (alternative: adds an 'Actual' tax row)
      "shipping_description": "Shipping",
      "discount_amount_path": "discount_amount",  (sign is ignored; Magento sends negatives)
      "discount_on": "Grand Total"                ("Net Total" if discount is before tax)
    """
    doc = ctx.doc
    template = doc.get("taxes_and_charges")
    if template and not doc.get("taxes"):
        from erpnext.controllers.accounts_controller import get_taxes_and_charges

        doc["taxes"] = [dict(r) for r in get_taxes_and_charges(
            "Sales Taxes and Charges Template", template) or []]

    shipping = flt(get_path(ctx.record, _cfg(ctx, "shipping_amount_path", "shipping_amount")))
    if shipping and _cfg(ctx, "shipping_item"):
        # shipping as a service item line -> VAT "On Net Total" also applies to it
        doc.setdefault("items", []).append({
            "item_code": _cfg(ctx, "shipping_item"), "qty": 1, "rate": shipping,
        })
    elif shipping:
        account = _cfg(ctx, "shipping_account")
        if not account:
            ctx.errors.append(_("Order has shipping {0} but neither 'shipping_item' nor "
                                "'shipping_account' is configured").format(shipping))
        else:
            doc.setdefault("taxes", []).append({
                "charge_type": "Actual",
                "account_head": account,
                "description": _cfg(ctx, "shipping_description", "Shipping"),
                "tax_amount": shipping,
            })

    discount = abs(flt(get_path(ctx.record, _cfg(ctx, "discount_amount_path", "discount_amount"))))
    if discount:
        doc["apply_discount_on"] = _cfg(ctx, "discount_on", "Grand Total")
        doc["discount_amount"] = discount


# ---------------------------------------------------------------------------
# POST: reconciliation + payment
# ---------------------------------------------------------------------------

def check_grand_total(ctx):
    """
    Warn when the ERPNext total differs from the shop total.
    Handler Settings: "grand_total_path": "grand_total", "total_tolerance": 0.05,
                      "reject_on_total_mismatch": 0
    """
    expected = get_path(ctx.record, _cfg(ctx, "grand_total_path", "grand_total"))
    if expected in (None, "") or not ctx.document.meta.has_field("grand_total"):
        return
    diff = abs(flt(ctx.document.grand_total) - flt(expected))
    if diff > flt(_cfg(ctx, "total_tolerance", 0.05)):
        msg = _("Grand total mismatch: ERPNext {0} vs shop {1}").format(ctx.document.grand_total, expected)
        if int(_cfg(ctx, "reject_on_total_mismatch", 0)):
            frappe.throw(msg)
        ctx.warnings.append(msg)


def create_payment(ctx):
    """
    Create and submit a Payment Entry against the submitted Sales Invoice when the order is paid.

    Handler Settings:
      "create_payment": 1,
      "paid_amount_path": "payment.amount_paid",  (if set and 0/missing -> order unpaid, no payment;
                                                   if not set -> pay full outstanding)
      "payment_method_path": "payment.method",
      "mode_of_payment_map": {"checkmo": "Cash", "stripe_payments": "Credit Card", "*": "Wire Transfer"},
      "paid_when_path": "status", "paid_when_values": ["processing", "complete"],
      "payment_reference_path": "increment_id"
    """
    doc = ctx.document
    if not int(_cfg(ctx, "create_payment", 0)) or doc.doctype != "Sales Invoice" or doc.docstatus != 1:
        return
    if doc.get("is_pos") or flt(doc.outstanding_amount) <= 0:
        return

    when_path = _cfg(ctx, "paid_when_path")
    if when_path:
        allowed = [str(v).lower() for v in _cfg(ctx, "paid_when_values", [])]
        if str(get_path(ctx.record, when_path) or "").lower() not in allowed:
            return

    method = str(get_path(ctx.record, _cfg(ctx, "payment_method_path", "payment.method")) or "")
    mop_map = _cfg(ctx, "mode_of_payment_map", {}) or {}
    mode_of_payment = mop_map.get(method) or mop_map.get("*")
    if not mode_of_payment:
        ctx.warnings.append(_("No Mode of Payment mapped for '{0}'; payment not created").format(method))
        return

    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
    from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

    paid_path = _cfg(ctx, "paid_amount_path")
    if paid_path:
        paid = flt(get_path(ctx.record, paid_path))
        if paid <= 0:
            return  # not paid yet (e.g. cash on delivery)
        amount = min(paid, flt(doc.outstanding_amount))
    else:
        amount = flt(doc.outstanding_amount)

    pe = get_payment_entry("Sales Invoice", doc.name, party_amount=amount)
    pe.mode_of_payment = mode_of_payment
    account = (get_bank_cash_account(mode_of_payment, doc.company) or {}).get("account")
    if account:
        pe.paid_to = account
        pe.paid_to_account_currency = frappe.db.get_value("Account", account, "account_currency")
    pe.reference_no = str(get_path(ctx.record, _cfg(ctx, "payment_reference_path", "increment_id"))
                          or ctx.external_id or doc.name)
    pe.reference_date = getdate(doc.posting_date)
    pe.flags.ignore_permissions = True
    pe.insert()
    pe.submit()
    ctx.warnings.append(_("Payment Entry {0} created").format(pe.name))


# ---------------------------------------------------------------------------
# OUTBOUND server methods
# ---------------------------------------------------------------------------

def stock_levels(ctx):
    """
    Available stock per item (optionally per warehouse).

    Query params: skus=SKU-1,SKU-2   modified_since=2026-09-01 00:00:00   page, limit
    Handler Settings:
      "warehouses": ["Stores - YC"],      (empty = all warehouses)
      "subtract_reserved": 1,
      "per_warehouse": 0,
      "sku_key": "sku", "qty_key": "qty"
    """
    s = ctx.settings
    conditions, values = ["1=1"], {}
    if s.get("warehouses"):
        conditions.append("b.warehouse in %(warehouses)s")
        values["warehouses"] = tuple(s["warehouses"])
    if ctx.args.get("skus"):
        conditions.append("b.item_code in %(skus)s")
        values["skus"] = tuple(x.strip() for x in ctx.args["skus"].split(",") if x.strip())
    if ctx.args.get("modified_since"):
        conditions.append("b.modified > %(since)s")
        values["since"] = ctx.args["modified_since"]

    qty_expr = "b.actual_qty - b.reserved_qty" if int(s.get("subtract_reserved", 1)) else "b.actual_qty"
    group = "b.item_code, b.warehouse" if int(s.get("per_warehouse", 0)) else "b.item_code"
    values.update(limit=ctx.page_length, offset=(ctx.page - 1) * ctx.page_length)

    rows = frappe.db.sql(
        f"""
        select b.item_code, {'b.warehouse,' if int(s.get('per_warehouse', 0)) else ''}
               sum({qty_expr}) as qty, max(b.modified) as modified
        from `tabBin` b
        where {' and '.join(conditions)}
        group by {group}
        order by b.item_code
        limit %(limit)s offset %(offset)s
        """,
        values,
        as_dict=True,
    )
    sku_key, qty_key = s.get("sku_key", "sku"), s.get("qty_key", "qty")
    data = []
    for r in rows:
        item = {sku_key: r.item_code, qty_key: max(flt(r.qty), 0), "is_in_stock": flt(r.qty) > 0,
                "modified": str(r.modified)}
        if r.get("warehouse"):
            item["source_code"] = r.warehouse
        data.append(item)
    return {"data": data, "has_more": len(rows) == ctx.page_length}
