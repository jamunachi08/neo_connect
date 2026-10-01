"""Unit tests for the pure mapping engine. Run: python -m pytest neo_connect/tests/test_mapper.py"""

import json
import os
import unittest

from neo_connect.engine.mapper import (
    MappingError, apply_transform, extract_records, get_path, map_inbound, map_outbound,
)

PRESETS = os.path.join(os.path.dirname(__file__), "..", "presets")


def load(name):
    with open(os.path.join(PRESETS, name)) as fh:
        return json.load(fh)


def simple_eval(expr, names):
    return eval(expr, {"str": str, "abs": abs, "cstr": lambda v: "" if v is None else str(v)}, dict(names))  # noqa: S307 - test only


def row_filters(preset):
    return {k: (lambda e: lambda row: bool(simple_eval(e, {"row": row})))(v)
            for k, v in (preset.get("row_filters") or {}).items()}


class TestPaths(unittest.TestCase):
    def test_get_path(self):
        d = {"a": {"b": [{"c": 1}, {"c": 2}]}, "s": ["x", "y"]}
        self.assertEqual(get_path(d, "a.b[1].c"), 2)
        self.assertEqual(get_path(d, "s[0]"), "x")
        self.assertIsNone(get_path(d, "a.z.q"))
        self.assertIsNone(get_path(d, "s[5]"))

    def test_extract_records(self):
        self.assertEqual(extract_records({"a": 1}), [{"a": 1}])
        self.assertEqual(extract_records([{"a": 1}, {"a": 2}]), [{"a": 1}, {"a": 2}])
        self.assertEqual(extract_records({"orders": [{"a": 1}]}, "orders"), [{"a": 1}])
        self.assertEqual(extract_records({"x": 1}, "orders"), [])


class TestTransforms(unittest.TestCase):
    ctx = {"lookup": lambda r, v: f"L:{v}", "eval": simple_eval, "record": {}, "row": None}

    def t(self, value, transform, **kw):
        return apply_transform(value, dict(kw, transform=transform), self.ctx)

    def test_basic(self):
        self.assertEqual(self.t(" sar ", "Upper"), "SAR")
        self.assertEqual(self.t("2", "Integer"), 2)
        self.assertEqual(self.t("-20.5", "Absolute"), 20.5)
        self.assertEqual(self.t("true", "Check"), 1)
        self.assertEqual(self.t(None, "Static", default_value="X"), "X")
        self.assertEqual(self.t(None, "None", default_value="D"), "D")

    def test_dates(self):
        self.assertEqual(self.t("2026-09-24 07:12:05", "Date"), "2026-09-24")
        self.assertEqual(self.t("2026-09-24T10:12:05+03:00", "Datetime"), "2026-09-24 10:12:05")
        self.assertEqual(self.t("2026-09-24T10:12:05Z", "Time"), "10:12:05")
        with self.assertRaises(MappingError):
            self.t("yesterday", "Date")

    def test_timezone(self):
        ctx = dict(self.ctx, timezone="Asia/Riyadh")
        row = {"transform": "Datetime", "source_timezone": "UTC"}
        self.assertEqual(apply_transform("2026-09-24 22:30:00", row, ctx), "2026-09-25 01:30:00")
        # values with an explicit offset are converted even without source_timezone
        self.assertEqual(apply_transform("2026-09-24T19:30:00Z", {"transform": "Date"}, ctx), "2026-09-24")
        self.assertEqual(apply_transform("2026-09-24T21:30:00Z", {"transform": "Date"}, ctx), "2026-09-25")
        # naive value, no source tz -> unchanged
        self.assertEqual(apply_transform("2026-09-24 22:30:00", {"transform": "Time"}, ctx), "22:30:00")

    def test_value_map_lookup_expression(self):
        vm = {"pending": "Draft", "*": "Other"}
        self.assertEqual(self.t("pending", "Value Map", value_map=vm), "Draft")
        self.assertEqual(self.t("weird", "Value Map", value_map=vm), "Other")
        self.assertEqual(self.t("SKU-1", "Lookup"), "L:SKU-1")
        self.assertEqual(self.t(5, "Expression", expression="value * 2"), 10)


class TestMagentoPreset(unittest.TestCase):
    def setUp(self):
        self.p = load("magento_sales_invoice.json")

    def test_maps_sample(self):
        doc, errors = map_inbound(self.p["sample_payload"], self.p["field_maps"],
                                  lookup=lambda r, v: v, evaluator=simple_eval,
                                  row_filters=row_filters(self.p), timezone="Asia/Riyadh")
        self.assertEqual(errors, [])
        self.assertEqual(doc["po_no"], "000001045")
        self.assertEqual(doc["posting_date"], "2026-09-24")
        self.assertEqual(doc["posting_time"], "10:12:05")  # 07:12 UTC -> Riyadh
        self.assertEqual(doc["currency"], "SAR")
        self.assertEqual(doc["remarks"], "Magento order 000001045 / sara.ahmed@example.com")
        # configurable child line (parent_item_id=12) is filtered out
        self.assertEqual(doc["items"], [
            {"item_code": "SKU-001", "qty": 2.0, "rate": 150.0},
            {"item_code": "TSHIRT-RED-M", "qty": 1.0, "rate": 80.0},
        ])

    def test_required_missing(self):
        rec = dict(self.p["sample_payload"])
        rec["items"] = [{"qty_ordered": 1, "price": 10}]
        _, errors = map_inbound(rec, self.p["field_maps"], evaluator=simple_eval)
        self.assertTrue(any("item_code" in e for e in errors), errors)

    def test_lookup_error_is_reported(self):
        def lookup(row, value):
            raise MappingError(f"Item not found for '{value}'")
        _, errors = map_inbound(self.p["sample_payload"], self.p["field_maps"], lookup=lookup,
                                evaluator=simple_eval, row_filters=row_filters(self.p))
        self.assertEqual(len(errors), 2)
        self.assertIn("Item not found for 'SKU-001'", errors[0])


class TestStandardPreset(unittest.TestCase):
    def test_sales_invoice(self):
        p = load("standard_order_sales_invoice.json")
        doc, errors = map_inbound(p["sample_payload"], p["field_maps"], evaluator=simple_eval,
                                  timezone="Asia/Riyadh")
        self.assertEqual(errors, [])
        self.assertEqual(doc["posting_date"], "2026-09-24")
        self.assertEqual(doc["posting_time"], "10:12:05")
        self.assertEqual(doc["contact_email"], "sara.ahmed@example.com")
        self.assertEqual(len(doc["items"]), 2)
        self.assertEqual(doc["items"][0]["description"], "Coffee Mug")
        self.assertNotIn("description", doc["items"][1])

    def test_sales_order_scalar_into_child(self):
        p = load("standard_order_sales_order.json")
        doc, errors = map_inbound(p["sample_payload"], p["field_maps"], evaluator=simple_eval)
        self.assertEqual(errors, [])
        self.assertEqual(doc["transaction_date"], "2026-09-24")
        self.assertTrue(all(r["delivery_date"] == "2026-09-24" for r in doc["items"]))


class TestMagentoCustomPreset(unittest.TestCase):
    def test_custom_json(self):
        p = load("magento_custom_sales_invoice.json")
        doc, errors = map_inbound(p["sample_payload"], p["field_maps"], evaluator=simple_eval,
                                  timezone="Asia/Riyadh")
        self.assertEqual(errors, [])
        self.assertEqual(doc["po_no"], "100012345")
        self.assertEqual(doc["posting_date"], "2026-09-24")
        self.assertEqual(doc["contact_email"], "john@example.com")
        self.assertEqual(doc["remarks"], "Magento order 100012345 / Cash on Delivery")
        self.assertEqual(doc["items"], [
            {"item_code": "TYRE-001", "qty": 2.0, "rate": 450.0},
            {"item_code": "TYRE-002", "qty": 1.0, "rate": 650.0},
        ])


class TestOutbound(unittest.TestCase):
    def test_items_catalog(self):
        p = load("items_catalog.json")
        erp_doc = {
            "item_code": "SKU-001", "item_name": "Coffee Mug", "description": "Mug", "item_group": "Kitchen",
            "brand": None, "stock_uom": "Nos", "is_stock_item": 1, "weight_per_unit": 0.4,
            "modified": "2026-09-24 10:00:00",
            "barcodes": [{"barcode": "6281000000011", "barcode_type": "EAN"}],
        }
        out = map_outbound(erp_doc, p["field_maps"])
        self.assertEqual(out["sku"], "SKU-001")
        self.assertEqual(out["category"], "Kitchen")
        self.assertIsNone(out["brand"])
        self.assertEqual(out["barcodes"], [{"barcode": "6281000000011", "type": "EAN"}])
        self.assertEqual(out["updated_at"], "2026-09-24 10:00:00")

    def test_nested_output(self):
        maps = [{"erp_field": "grand_total", "external_path": "totals.grand", "transform": "Float"},
                {"erp_field": "items.item_code", "external_path": "lines[].product.sku"}]
        out = map_outbound({"grand_total": "10", "items": [{"item_code": "A"}]}, maps)
        self.assertEqual(out, {"totals": {"grand": 10.0}, "lines": [{"product": {"sku": "A"}}]})


if __name__ == "__main__":
    unittest.main()
