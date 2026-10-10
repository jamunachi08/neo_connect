"""Authentication, partner resolution, IP allow-list, HMAC signature and rate limiting."""

import hashlib
import hmac
import ipaddress

import frappe
from frappe import _


class GatewayError(Exception):
    """An error that is returned to the partner with an HTTP status code."""

    def __init__(self, message, http_status=400, status="Rejected"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.status = status


def get_endpoint(slug, direction):
    if not slug:
        raise GatewayError(_("Query parameter 'endpoint' is required"), 400)
    if not frappe.db.exists("API Endpoint", slug):
        raise GatewayError(_("Unknown endpoint '{0}'").format(slug), 404)
    endpoint = frappe.get_cached_doc("API Endpoint", slug)
    if not endpoint.enabled:
        raise GatewayError(_("Endpoint '{0}' is disabled").format(slug), 403)
    if endpoint.direction != direction:
        verb = "push" if endpoint.direction == "Inbound" else "pull"
        raise GatewayError(
            _("Endpoint '{0}' is {1}; call neo_connect.api.{2}").format(
                slug, endpoint.direction, verb), 405)
    return endpoint


def get_partner_for_session():
    user = frappe.session.user
    if not user or user == "Guest":
        raise GatewayError(_("Authentication required: send 'Authorization: token <api_key>:<api_secret>'"), 401)
    partner = frappe.db.get_value("API Partner", {"user": user}, "name")
    if not partner:
        raise GatewayError(_("User {0} is not linked to an API Partner").format(user), 403)
    partner = frappe.get_cached_doc("API Partner", partner)
    if not partner.enabled:
        raise GatewayError(_("API Partner {0} is disabled").format(partner.name), 403)
    return partner


def authorize(endpoint, partner):
    allowed = {row.partner for row in (endpoint.allowed_partners or [])}
    if partner.name not in allowed:
        raise GatewayError(
            _("Partner {0} is not allowed to use endpoint {1}").format(partner.name, endpoint.name), 403)


def get_client_ip():
    return getattr(frappe.local, "request_ip", None) or ""


def check_ip(partner):
    rules = [r.strip() for r in (partner.allowed_ips or "").replace(",", "\n").splitlines() if r.strip()]
    if not rules:
        return
    ip = get_client_ip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise GatewayError(_("Could not determine client IP"), 403)
    for rule in rules:
        try:
            if addr in ipaddress.ip_network(rule, strict=False):
                return
        except ValueError:
            continue
    raise GatewayError(_("IP {0} is not allowed for partner {1}").format(ip, partner.name), 403)


def verify_signature(partner, raw_body: bytes):
    """HMAC-SHA256 of the raw request body, hex encoded, in header X-Signature."""
    if not partner.require_signature:
        return
    secret = partner.get_password("signing_secret", raise_exception=False)
    if not secret:
        raise GatewayError(_("Signing is required but no signing secret is configured"), 500, "Failed")
    sent = (frappe.get_request_header("X-Signature") or "").strip()
    if sent.lower().startswith("sha256="):
        sent = sent[7:]
    expected = hmac.new(secret.encode(), raw_body or b"", hashlib.sha256).hexdigest()
    if not sent or not hmac.compare_digest(sent.lower(), expected):
        raise GatewayError(_("Invalid or missing X-Signature header"), 401)


def check_rate_limit(partner):
    limit = partner.rate_limit_per_minute or 0
    if limit <= 0:
        return
    minute = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
    key = f"neo_connect:rl:{partner.name}:{minute}"
    cache = frappe.cache()
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 70)
    if count > limit:
        raise GatewayError(_("Rate limit of {0} requests/minute exceeded").format(limit), 429)


def gate(slug, direction, raw_body=None):
    """Run every check. Returns (endpoint, partner)."""
    endpoint = get_endpoint(slug, direction)
    partner = get_partner_for_session()
    authorize(endpoint, partner)
    check_ip(partner)
    check_rate_limit(partner)
    if direction == "Inbound":
        verify_signature(partner, raw_body or b"")
    return endpoint, partner
