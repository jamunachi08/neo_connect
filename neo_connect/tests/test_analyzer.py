"""Unit tests for the sample analyser. Run: python -m unittest neo_connect.tests.test_analyzer"""

import unittest

from neo_connect.engine.analyzer import parse_payload_text, profile, suggest

SPEC = """# Spec
Some text
```json
{"order_number": "PT-1", "order_date": "2026-09-15T10:30:00Z", "erp_executive_code": "EXC-001",
 "customer": {"email": "a@x.com", "firstname": "Ahmed"},
 "billing_address": {"street": "King Fahad Rd", "city": "Riyadh", "country_id": "SA", "telephone": "+966"},
 "fulfillment": {"method": "Store Pickup", "pickup_date": "2026-09-20T00:00:00Z"},
 "installer": {"name": "Riyadh North"},
 "items": [{"sku": "T-1", "qty_ordered": 4, "price": 450.0, "price_incl_tax": 517.5, "brand": "Michelin",},],
 "totals": {"grand_total": 2070.0, "shipping_amount": 0.0,},
 "payment": {"method": "tap", "method_title": "TAP Payment", "amount_paid": 2070.0},
}
```
"""
OTHER = ('{"order_number": "PT-2", "order_date": "2026-10-04T18:45:00Z", "customer": {"email": "s@x.com"},'
         ' "fulfillment": {"method": "Home Delivery", "delivery_date": "2026-10-07T00:00:00Z"},'
         ' "items": [{"sku": "T-1", "qty_ordered": 2, "price": 450.0}], "totals": {"grand_total": 1035.0},'
         ' "payment": {"method": "cashondelivery", "method_title": "Cash on Delivery", "amount_paid": 0}}')


def F(n, label, t="Data", o=None, r=0):
    return {"fieldname": n, "label": label, "fieldtype": t, "options": o, "reqd": r}


TARGET = {"doctype": "Sales Order", "fields": [
    F("po_no", "PO"), F("po_date", "PO Date", "Date"), F("transaction_date", "Date", "Date", r=1),
    F("delivery_date", "Delivery Date", "Date"), F("currency", "Currency", "Link", "Currency"),
    F("contact_email", "Contact Email"),
    F("custom_طريقة_الدفع", "طريقة الدفع", "Select", "\nنقدي\nبطاقة ائتمان", 1),
    F("custom_fitting_location__موقع_التركيب", "Fitting Location / موقع التركيب", "Link", "Branch", 1),
    F("custom_اسم_مندوب_المبيعات", "اسم مندوب المبيعات", "Link", "Sales Person", 1)],
    "tables": {"items": {"doctype": "Sales Order Item", "fields": [
        F("item_code", "Item", "Link", "Item", 1), F("qty", "Qty", "Float", r=1), F("rate", "Rate", "Currency"),
        F("delivery_date", "Delivery Date", "Date", r=1), F("custom_brand", "Brand")]}}}


class TestSpecAndGaps(unittest.TestCase):
    def test_spec_tables_and_reconcile(self):
        from neo_connect.engine.analyzer import merge_spec, parse_spec_tables
        spec = """## 3. Customer Data
| Field Name | Type | Description | Example |
|---|---|---|---|
| `customer_email` | string | Email | `a@x.com` |
| `store_name` | string | Store | `PT SA` |
## 9. Payment
| Field Name | Type | Description | Example |
|---|---|---|---|
| `payment.method` | string | Code | `tap`, `tabby` |
"""
        fields = parse_spec_tables(spec)
        self.assertEqual([f["name"] for f in fields], ["customer_email", "store_name", "payment.method"])
        prof = profile(parse_payload_text('{"customer": {"email": "a@x.com"}, "payment": {"method": "tap"}}')[0])
        prof, rep = merge_spec(prof, fields)
        st = {(r["spec_name"], r["status"]) for r in rep}
        self.assertIn(("customer_email", "Renamed"), st)
        self.assertIn(("store_name", "Spec only"), st)
        self.assertIn("tabby", prof["payment.method"]["samples"])           # spec examples feed value maps

    def test_customer_fetched_fields_and_proposals(self):
        recs = parse_payload_text('{"order_number": "1", "customer": {"email": "a@x.com"}, '
                                  '"billing_address": {"telephone": "+966"}, '
                                  '"vehicle": {"plate": "ABC", "vin_number": "V1"}, "items": [{"sku": "S", "qty": 1, "price": 1}]}')[0]
        tgt = {"doctype": "Sales Invoice", "fields": [
            F("po_no", "PO"), dict(F("custom_plate_number", "Plate Number"), fetch_from="customer.custom_plate_number"),
            dict(F("custom_mobile", "Mobile Number"), fetch_from="customer.custom_mobile_no")],
            "tables": {"items": {"doctype": "Sales Invoice Item", "fields": [F("item_code", "Item", "Link", "Item")]}}}
        res = suggest(profile(recs), tgt)
        cf = res["settings"]["customer_fields"]
        self.assertEqual(cf["custom_plate_number"], "vehicle.plate")
        self.assertEqual(cf["custom_mobile_no"], "billing_address.telephone")
        vin = [r for r in res["rows"] if r["external_path"] == "vehicle.vin_number"][0]
        self.assertEqual(vin["gap_status"], "Create Field")
        self.assertEqual(vin["target_doctype"], "Customer")                  # next to the other vehicle values
        self.assertEqual(vin["proposed_fieldname"], "custom_vehicle_vin_number")


class TestAnalyzer(unittest.TestCase):
    def test_parse_markdown_with_trailing_commas(self):
        recs, notes = parse_payload_text(SPEC)
        self.assertEqual(len(recs), 1)
        self.assertTrue(any("trailing comma" in n for n in notes))

    def test_parse_array_and_invalid(self):
        recs, _ = parse_payload_text('[{"a": 1}, {"a": 2}]')
        self.assertEqual(len(recs), 2)
        with self.assertRaises(ValueError):
            parse_payload_text("{not json")

    def test_profile_presence(self):
        recs = parse_payload_text(SPEC)[0] + parse_payload_text(OTHER)[0]
        p = profile(recs)
        self.assertEqual(p["items[].sku"]["count"], 2)
        self.assertEqual(p["installer.name"]["count"], 1)
        self.assertEqual(p["order_date"]["type"], "datetime")

    def test_suggest(self):
        recs = parse_payload_text(SPEC)[0] + parse_payload_text(OTHER)[0]
        res = suggest(profile(recs), TARGET, platform="Magento")
        m = {(r["external_path"], r["erp_field"]): r for r in res["rows"] if r["include"] and r["erp_field"]}
        self.assertEqual(res["external_id_path"], "order_number")
        self.assertIn(("order_number", "po_no"), m)
        self.assertIn(("order_date", "transaction_date"), m)
        self.assertIn(("items[].sku", "items.item_code"), m)
        self.assertIn(("items[].price", "items.rate"), m)
        self.assertIn(("installer.name", "custom_fitting_location__موقع_التركيب"), m)
        self.assertIn(("payment.method", "custom_طريقة_الدفع"), m)          # stable code preferred over title
        self.assertIn(("erp_executive_code", "custom_اسم_مندوب_المبيعات"), m)
        self.assertIn(("items[].brand", "items.custom_brand"), m)
        pay = m[("payment.method", "custom_طريقة_الدفع")]
        self.assertEqual(pay["transform"], "Value Map")
        self.assertEqual(pay["value_map"]["cashondelivery"], "نقدي")          # translated automatically
        self.assertEqual(pay["gap_status"], "Option Gap")                     # 'tap' has no option here
        dd = [r for r in res["rows"] if r["erp_field"] == "delivery_date"][0]
        self.assertEqual(dd["transform"], "Expression")
        self.assertIn("fulfillment.delivery_date", dd["expression"])
        s = res["settings"]
        self.assertEqual(s["customer_email_path"], "customer.email")
        self.assertEqual(s["billing_address_path"], "billing_address")
        self.assertEqual(s["address_map"]["country_code"], "country_id")
        self.assertEqual(s["grand_total_path"], "totals.grand_total")
        self.assertNotIn("mode_of_payment_map", s)       # Sales Order: no payment posting
        self.assertIn("ensure_customer", res["pre_hooks"])


if __name__ == "__main__":
    unittest.main()
