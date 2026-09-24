# NeoConnect for ERPNext

**NeoConnect – Commerce & Partner Integrations.** NeoConnect is a configurable API gateway for ERPNext. It is tested on v15 and should also work on v16. Each e-commerce platform, marketplace or vendor becomes an **API Partner** with its own credentials. You create **API Endpoints** in ERPNext and send the partner the generated URL.

- **Inbound (push):** the partner sends JSON, and ERPNext creates documents from it. Example: Magento sends orders, and ERPNext creates Sales Invoices.
- **Outbound (pull):** the partner reads data from ERPNext. Examples: stock levels, prices, the item catalogue and invoice status.

The partner's JSON is converted to ERPNext fields by **field mapping** that you configure, not by code. Adding Shopify, WooCommerce, Salla, Zid or a custom shop means creating another partner and endpoint.

```
 Magento / Shopify / Woo / any vendor                    ERPNext
 ─────────────────────────────────────                   ─────────────────────────────────────────────
  POST /api/method/neo_connect.api.push?endpoint=magento-sales-invoice
        Authorization: token key:secret     ──►  1. Gate: auth → partner → allowed? → IP → rate → HMAC
        { Magento order JSON }                    2. Log request (API Request Log)
                                                  3. Split into records (records_path)
                                                  4. Idempotency: external id already imported? → Duplicate
                                                  5. Field Map: partner JSON → ERPNext doc (transforms, lookups)
                                                  6. Defaults + pre-hooks (customer, taxes, shipping, discount)
                                                  7. Insert / Submit  (each record in its own savepoint)
                                                  8. Post-hooks (total check, Payment Entry)
        ◄── 200/207/422 {request_id, results[]}   9. Save External Reference + finish log

  GET  /api/method/neo_connect.api.pull?endpoint=stock-levels&modified_since=...
                                            ──►  Gate → filters (whitelisted) → query / server method
        ◄── {data[], page, has_more, next_modified_since}      → Field Map (ERPNext → partner JSON)
```

## Installation

```bash
cd ~/frappe-bench
bench get-app /path/to/neo_connect      # or a git URL once you push it
bench --site your.site install-app neo_connect
bench --site your.site migrate
bench restart
```

Background processing (the **Process in Background** option) needs the bench workers and the scheduler to be running. They already are on a production bench.

## Setup (about 10 minutes per partner)

1. **Create the partner.** Go to **API Partner → New**. Enter the name (for example *Magento KSA*), the platform and the company. Check the roles; the default is Accounts User, Sales User and Stock User. Save.
2. Click **Credentials → Generate Credentials**. This creates a dedicated API user with only those roles, and shows the **API key, API secret and signing secret once**. Send them to the vendor through a secure channel.
3. **Create the endpoint.** Go to **API Endpoint → New → Start from Preset**. Pick *Magento – Sales Invoice* and the partner. For any other shop, pick *Standard Order – Sales Invoice*.
4. **Adjust the preset for your company.** In **Document Defaults**, set the tax template (`taxes_and_charges`) and the warehouse. In **Handler Settings**, set the `shipping_item` or `shipping_account` and the `mode_of_payment_map`.
5. Click **Actions → Test Mapping**. It runs the sample payload through the mapping and hooks, then rolls everything back. Fix anything shown in red.
6. Click **Actions → Partner Guide**. Download the Markdown file and send it to the vendor, together with the **Endpoint URL** shown on the form.
7. The vendor tests with `&dry_run=1`. ERPNext fully validates the invoice and then rolls it back. When the vendor gets `Validated` results, they remove `dry_run`.

Outbound endpoints work the same way. The **Stock Levels**, **Item Prices**, **Items Catalog** and **Invoice Status** presets are included, and **Actions → Preview Response** shows what the partner will receive.

## What the vendor calls

| Purpose | Call |
|---|---|
| Check credentials | `GET /api/method/neo_connect.api.ping` |
| Push data | `POST /api/method/neo_connect.api.push?endpoint=<code>` (add `&dry_run=1` to test) |
| Read data | `GET /api/method/neo_connect.api.pull?endpoint=<code>&page=1&limit=100&modified_since=...` |

Headers: `Authorization: token <api_key>:<api_secret>` and `Content-Type: application/json`. If the partner has **Require HMAC Signature** checked, the vendor also sends `X-Signature: <hex HMAC-SHA256(raw body, signing secret)>`.

Frappe wraps every response in `{"message": {...}}`. A push response looks like this:

```json
{"message": {
  "request_id": "8f1c2a9e0b",
  "status": "Partial",
  "summary": {"total": 2, "Created": 1, "Failed": 1},
  "results": [
    {"index": 0, "external_id": "000001045", "status": "Created", "doctype": "Sales Invoice", "name": "ACC-SINV-2026-00012", "grand_total": 442.75},
    {"index": 1, "external_id": "000001046", "status": "Failed", "error": "items[0].item_code <- sku: Item not found for 'SKU-XYZ'"}
  ]
}}
```

If the body isn't valid JSON, Frappe rejects it before the app runs. The vendor gets HTTP 500 with `exc_type: JSONDecodeError`, and nothing is logged in API Request Log.

HTTP status codes:

- **200** Success or Duplicate
- **202** Queued
- **207** Partial
- **400** Bad request
- **401** or **403** Authentication or permission failure
- **413** Too many records in one request
- **422** All records failed
- **429** Rate limited

**Retrying is always safe.** Each record's external ID is stored in **API External Reference**, and the database primary key blocks a second document, even when two requests arrive at the same moment.

## Magento specifics

The *Magento – Sales Invoice* preset expects the Magento REST **order** entity, which is what `GET /V1/orders/{id}` returns. The Magento developer sends it from an observer, for example on `sales_order_invoice_pay`, or from a cron job.

- `increment_id` is the idempotency key, and it is also stored in `po_no`.
- `created_at` is in UTC and is converted to the ERPNext system time zone. An order placed at 01:30 Riyadh time therefore posts on the correct day.
- Configurable products send a parent line and a child line. The row filter `not row.get('parent_item_id')` keeps only the parent line, which carries the price.
- `sku` must equal the ERPNext **Item Code**. If your SKUs differ, add a custom field to Item (for example `magento_sku`) and set it as the mapping row's **Lookup Field**.
- The customer is looked up by e-mail and then by phone. If none is found, a Customer, Contact and Address are created.
- Shipping is added as a `SHIPPING` service item, so VAT applies to it the same way Magento calculates it. Create that non-stock item, or use `shipping_account` to add shipping as a tax row instead.
- `discount_amount` is negative in Magento. It is applied as an ERPNext discount on the **Net Total**.
- The invoice is submitted. If `payment.amount_paid` is greater than 0, a Payment Entry is created using `mode_of_payment_map`. Each Mode of Payment needs a default account for the company.
- `check_grand_total` compares the ERPNext grand total with Magento's `grand_total`. A mismatch is returned as a warning, or rejects the record if `reject_on_total_mismatch` is 1.

## The Standard Commerce Order (for every other platform)

A vendor that can't send its native format, or whose format you don't want to map, can send this JSON to the `orders-sales-invoice` or `orders-sales-order` endpoint:

```json
{
  "order_id": "WEB-10045",
  "order_date": "2026-09-24T10:12:05+03:00",
  "currency": "SAR",
  "customer": {"email": "sara@example.com", "first_name": "Sara", "last_name": "Ahmed", "phone": "+966500000000",
               "address": {"line1": "King Fahd Road 123", "city": "Riyadh", "postcode": "12211", "country_code": "SA"}},
  "lines": [{"sku": "SKU-001", "qty": 2, "unit_price": 150, "description": "Coffee Mug"}],
  "shipping_amount": 25,
  "discount_amount": 20,
  "grand_total": 442.75,
  "payment": {"method": "card", "paid_amount": 442.75, "reference": "ch_3Pabc"}
}
```

You can also map a platform's native payload directly. Copy a preset, paste a real payload into **Sample Payload**, edit the Field Map rows, and click **Test Mapping** until it maps cleanly.

## Field mapping reference

Each **Field Map** row has these columns:

| Column | Meaning |
|---|---|
| Partner JSON Path | `customer_email`, `billing_address.city`, `billing_address.street[0]`, `items[].sku` (`[]` means "each element of the list") |
| ERPNext Field | `posting_date` for a parent field, or `items.item_code` for a child-table field |
| Transform | See the list below |
| Required | Reject the record if the value is empty |
| Default / Static Value | Used when the value is empty, or always when the transform is **Static** |

For **inbound** endpoints the value flows from the partner path to the ERPNext field. For **outbound** endpoints it flows from the ERPNext field to the partner path. For example, `barcodes.barcode` maps to `barcodes[].barcode`.

The available transforms:

- **None**, **Text**, **Upper**, **Lower**
- **Integer**, **Float**, **Absolute**
- **Check**: turns `true`, `yes` or `1` into 1
- **Date**, **Datetime**, **Time**: accept ISO 8601 and common date formats. Values with a UTC offset are converted to the system time zone. Set **Source Time Zone** (for example `UTC`) for values without an offset.
- **Value Map**: translates values using a JSON object, for example `{"pending": "Draft", "*": "Other"}`, where `*` is the fallback.
- **Lookup**: finds a record by `{Lookup Field: value}` in the **Lookup DocType** and returns its name. With **Lookup via Endpoint**, it instead resolves an ID imported through another endpoint. For example, an invoice payload can find the Sales Order created from the same shop order. **If Not Found** is one of Error, Use Value or Skip.
- **Expression**: a sandboxed Python expression evaluated with `frappe.safe_eval`. It can use `value`, `record` (the whole record) and `row` (the current list element). It can also call `flt`, `cint`, `cstr`, `getdate`, `get_datetime`, `nowdate`, `str`, `int`, `float`, `round`, `abs`, `min`, `max`, `len`, `sum`, `any` and `all`. For example: `'Magento order ' + cstr(record.get('increment_id'))`.

These endpoint settings also affect mapping:

- **Document Defaults** fill in fields the mapping left empty. Use `"items.warehouse": "..."` to set a child-table field on every row. Partner defaults apply first, then endpoint defaults, and the partner's Company is added automatically.
- **Row Filters** keep only the child rows where the expression is true.
- **Records Path** handles wrapped batches such as `{"orders": [...]}`. A bare JSON array is also accepted.
- The e-commerce presets set `disable_rounded_total: 1`. Without it, ERPNext rounds 442.75 to 443, and a paid order would stay "Partly Paid" with 0.25 outstanding.

**Permissions for outbound endpoints.** Outbound endpoints read data as the partner's API user. If a call returns `403 Not permitted`, either give that user a role that can read the DocType, or tick **Ignore Permissions** on the endpoint. The response is still limited to the mapped fields and the allowed filters. The *Item Prices* preset has Ignore Permissions ticked, because the Sales User and Stock User roles can't read Item Price.

## Hooks (for custom logic, without changing the engine)

**Pre-process** and **Post-process** hooks are dotted Python paths, one per line. Each is called as `hook(ctx)`, where `ctx` has these keys:

- `endpoint`, `partner`
- `record`: the partner's JSON
- `doc`: a dict, before insert
- `document`: the saved document, after insert
- `settings`: the merged Handler Settings JSON
- `errors`: append to this to reject the record
- `warnings`: returned to the partner
- `external_id`

Hooks included in `neo_connect/handlers/commerce.py`:

| Hook | Type | Does |
|---|---|---|
| `ensure_customer` | pre | Finds the customer by e-mail or phone, or creates Customer, Contact and Address |
| `apply_charges` | pre | Adds the tax template rows, shipping (as an item or a tax row) and the order discount |
| `check_grand_total` | post | Compares the ERPNext total with the shop total |
| `create_payment` | post | Creates and submits a Payment Entry for paid orders |
| `stock_levels` | outbound server method | Returns available quantity per SKU, and optionally per warehouse |

Every handler reads its source paths from **Handler Settings**. The docstrings list every key.

To add your own hook, put a function in any installed app, for example `my_app.hooks.add_sales_team(ctx)`, and add its path to the endpoint. The same mechanism covers vendor bills, returns and other document types, such as a supplier pushing Purchase Invoices.

## Security

Each partner has its own ERPNext user with minimal roles. There is no shared or Administrator key. Other safeguards:

- An endpoint only accepts the partners listed in **Allowed Partners**.
- **Ignore Permissions** is off by default, so the API user's own roles and user permissions apply.
- Optional IP allow-list (single IPs or CIDR ranges), a per-partner rate limit, and an HMAC-SHA256 body signature.
- Outbound filters are allow-listed per endpoint and the operators are restricted. Partners can't query arbitrary fields.
- Hook and server-method paths, and expressions, can only be set by System Managers. Expressions run in `frappe.safe_eval`.
- **Regenerate Secret** or **Revoke Access** cuts a partner off immediately. Disabling the partner also disables its user.
- Every call is logged in **API Request Log** with the payload, response, IP, duration and error trace. Logs are deleted after 90 days; set `neo_connect_log_days` in `site_config.json` to change this.

## Operations

- **API Request Log** is filterable by partner, endpoint and status. **Reprocess** re-runs a failed or partial request with its stored payload; records already imported are skipped.
- **API External Reference** maps each shop ID to its ERPNext document, which is useful for support questions like "where is order 000001045?". If the ERPNext document is cancelled, the shop can send the order again and it is re-imported.
- Use **Process in Background** for large batches. The partner gets `202` and a `request_id`, and the result appears in the log.

## Tests

```bash
python -m unittest neo_connect.tests.test_mapper       # engine + presets, no bench needed
bench --site test.site run-tests --app neo_connect     # inside a bench
```

The app has been tested over real HTTP against Frappe/ERPNext **v15** (frappe 15.121, erpnext 15.121). The tests covered:

- Magento and Standard Order pushes, dry runs, duplicates, partial batches, background mode and reprocessing
- Update-draft on duplicate
- The HMAC signature, partner authorization, the IP allow-list and rate limiting
- All four outbound presets, with filters and pagination
- Customer, Address and Payment Entry creation, and the VAT and total reconciliation

v16 hasn't been tested yet. Run the tests on a staging copy before upgrading.
