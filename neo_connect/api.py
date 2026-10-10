"""
Public API of NeoConnect.

    POST /api/method/neo_connect.api.push?endpoint=<slug>[&dry_run=1]
    GET  /api/method/neo_connect.api.pull?endpoint=<slug>&page=1&limit=100&modified_since=...
    GET  /api/method/neo_connect.api.ping
    POST /api/method/neo_connect.api.sample?type=Sales%20Order   (partner shares a sample payload)

Authentication: header  Authorization: token <api_key>:<api_secret>
Optional:       header  X-Signature: <hex HMAC-SHA256 of raw body>   (if the partner requires it)
"""

import json
import time

import frappe
from frappe import _
from frappe.utils import cint

from neo_connect.engine import inbound, outbound
from neo_connect.engine.security import GatewayError, gate, get_partner_for_session
from neo_connect.engine.utils import dumps


def _respond(body, http_status):
    frappe.local.response.http_status_code = http_status
    return body


def _query_args():
    """Query-string args (Frappe drops them from form_dict when the body is JSON)."""
    return frappe._dict(frappe.request.args or {})


@frappe.whitelist(methods=["POST"])
def push(**kwargs):
    started = time.time()
    args = _query_args()
    slug = args.get("endpoint")
    dry_run = cint(args.get("dry_run"))
    raw = frappe.request.get_data() or b""
    raw_text = raw.decode("utf-8", errors="replace")

    log = inbound.new_log("Inbound", payload_text=raw_text, query=frappe.request.query_string.decode())
    log.dry_run = dry_run
    request_id = log.name
    try:
        endpoint, partner = gate(slug, "Inbound", raw)
        log.endpoint, log.partner = endpoint.name, partner.name
        try:
            payload = json.loads(raw_text) if raw_text.strip() else None
        except ValueError as e:
            raise GatewayError(_("Body is not valid JSON: {0}").format(e), 400)
        if payload is None:
            raise GatewayError(_("Empty request body"), 400)

        if cint(endpoint.process_async) and not dry_run:
            log.status = "Queued"
            log.http_status = 202
            log.save(ignore_permissions=True)
            frappe.enqueue(
                "neo_connect.engine.inbound.process_log",
                queue="short", log_name=log.name, enqueue_after_commit=True,
                job_id=f"neo_connect::{log.name}",
            )
            frappe.db.commit()
            return _respond({"request_id": request_id, "status": "Queued",
                             "message": _("Accepted for background processing")}, 202)

        result = inbound.Processor(endpoint, partner, log.name).process_payload(payload, dry_run=dry_run)
        inbound.finish_log(log, result, started)
        frappe.db.commit()
        body = {"request_id": request_id, "dry_run": bool(dry_run),
                **{k: v for k, v in result.items() if not k.startswith("_") and k != "http_status"}}
        return _respond(body, result["http_status"])

    except GatewayError as e:
        frappe.db.rollback()
        return _fail(log, started, e.status, e.http_status, e.message)
    except Exception:  # noqa: BLE001
        frappe.db.rollback()
        return _fail(log, started, "Failed", 500, _("Internal error"), frappe.get_traceback())


@frappe.whitelist(methods=["GET", "POST"])
def pull(**kwargs):
    started = time.time()
    args = frappe._dict(frappe.local.form_dict)
    args.update(_query_args())
    args.pop("cmd", None)
    log = inbound.new_log("Outbound", query=frappe.request.query_string.decode())
    try:
        endpoint, partner = gate(args.get("endpoint"), "Outbound")
        log.endpoint, log.partner = endpoint.name, partner.name
        body = outbound.handle_pull(endpoint, partner, args)
        count = len(body.get("data") or [])
        inbound.finish_log(log, {"status": "Success", "http_status": 200,
                                 "summary": {"total": count, "Returned": count}}, started)
        frappe.db.commit()
        return _respond({"request_id": log.name, **body}, 200)
    except GatewayError as e:
        frappe.db.rollback()
        return _fail(log, started, e.status, e.http_status, e.message)
    except (frappe.PermissionError,) as e:
        frappe.db.rollback()
        return _fail(log, started, "Rejected", 403, str(e) or _("Not permitted"))
    except Exception:  # noqa: BLE001
        frappe.db.rollback()
        return _fail(log, started, "Failed", 500, _("Internal error"), frappe.get_traceback())


@frappe.whitelist(methods=["GET"])
def ping():
    """Connectivity + credential check for partners."""
    try:
        partner = get_partner_for_session()
    except GatewayError as e:
        return _respond({"ok": False, "error": e.message}, e.http_status)
    endpoints = frappe.get_all(
        "API Endpoint Partner", filters={"partner": partner.name, "parenttype": "API Endpoint"},
        pluck="parent",
    )
    return {"ok": True, "partner": partner.name, "server_time": str(frappe.utils.now_datetime()),
            "endpoints": sorted(set(endpoints))}


@frappe.whitelist(methods=["POST"])
def sample(**kwargs):
    """
    A partner sends an example payload (any shape). It is stored in API Payload Sample for
    <partner> + <type> so the ERP team can analyse it and generate the endpoint. Nothing is created
    in ERPNext from it.
    """
    from neo_connect.engine.generator import store_partner_sample
    from neo_connect.engine.security import check_ip, check_rate_limit

    started = time.time()
    args = _query_args()
    raw_text = (frappe.request.get_data() or b"").decode("utf-8", errors="replace")
    log = inbound.new_log("Inbound", payload_text=raw_text, query=frappe.request.query_string.decode())
    try:
        partner = get_partner_for_session()
        log.partner = partner.name
        check_ip(partner)
        check_rate_limit(partner)
        message_type = (args.get("type") or "Sales Order").strip()[:140]
        try:
            doc = store_partner_sample(partner.name, message_type, raw_text, args.get("label"))
        except ValueError as e:
            raise GatewayError(str(e), 400)
        body = {"request_id": log.name, "status": "Stored", "sample": doc.name, "message_type": message_type,
                "samples_stored": len(doc.payloads),
                "message": _("Thank you. The ERP team will review the sample and share the endpoint URL.")}
        inbound.finish_log(log, {"status": "Success", "http_status": 200, "summary": {"total": 1, "Returned": 1}},
                           started)
        frappe.db.commit()
        return _respond(body, 200)
    except GatewayError as e:
        frappe.db.rollback()
        return _fail(log, started, e.status, e.http_status, e.message)
    except Exception:  # noqa: BLE001
        frappe.db.rollback()
        return _fail(log, started, "Failed", 500, _("Internal error"), frappe.get_traceback())


def _fail(log, started, status, http_status, message, trace=None):
    """Persist the failure log in its own transaction and answer the partner."""
    try:
        log_doc = frappe.get_doc({
            "doctype": "API Request Log",
            "direction": log.direction,
            "endpoint": log.endpoint if log.endpoint and frappe.db.exists("API Endpoint", log.endpoint) else None,
            "partner": log.partner if log.partner and frappe.db.exists("API Partner", log.partner) else None,
            "status": status,
            "http_status": http_status,
            "request_ip": log.request_ip,
            "http_method": log.http_method,
            "query_string": log.query_string,
            "user": log.user,
            "payload": log.payload,
            "dry_run": log.get("dry_run"),
            "duration_ms": round((time.time() - started) * 1000, 1),
            "response": dumps({"status": status, "error": message}),
            "error": (message or "") + ("\n\n" + trace if trace else ""),
        })
        log_doc.insert(ignore_permissions=True)
        frappe.db.commit()
        request_id = log_doc.name
    except Exception:  # noqa: BLE001 - never let logging hide the real error
        frappe.db.rollback()
        request_id = None
    return _respond({"request_id": request_id, "status": status, "error": message}, http_status)
