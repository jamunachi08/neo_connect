import re

import frappe
from frappe import _
from frappe.model.document import Document

from neo_connect.engine.utils import parse_json_field


class APIPartner(Document):
    def validate(self):
        parse_json_field(self.defaults, {}, _("Document Defaults"))
        parse_json_field(self.handler_settings, {}, _("Handler Settings"))
        if self.require_signature and not self.signing_secret:
            self.signing_secret = frappe.generate_hash(length=40)
        for role in self.get_roles():
            if not frappe.db.exists("Role", role):
                frappe.throw(_("Role {0} does not exist").format(role))
            if role in ("Administrator", "System Manager"):
                frappe.throw(_("Do not give {0} to an integration user").format(role))

    def on_update(self):
        if self.user:
            frappe.db.set_value("User", self.user, "enabled", 1 if self.enabled else 0)

    def get_roles(self):
        return [r.strip() for r in (self.roles or "").splitlines() if r.strip()]

    def ensure_user(self):
        if self.user and frappe.db.exists("User", self.user):
            user = frappe.get_doc("User", self.user)
        else:
            slug = re.sub(r"[^a-z0-9]+", ".", self.partner_name.lower()).strip(".") or "partner"
            host = (frappe.local.site or "erp.local").split(":")[0]
            if "." not in host:
                host += ".local"
            email = f"api.{slug}@{host}"
            if frappe.db.exists("User", email):
                user = frappe.get_doc("User", email)
            else:
                user = frappe.get_doc({
                    "doctype": "User",
                    "email": email,
                    "first_name": f"API {self.partner_name}"[:140],
                    "user_type": "System User",
                    "send_welcome_email": 0,
                    "enabled": 1,
                })
                user.flags.no_welcome_mail = True
                user.insert(ignore_permissions=True)
        user.set("roles", [])
        user.add_roles(*self.get_roles())
        return user


@frappe.whitelist()
def generate_credentials(partner):
    """Create/refresh the API user and a new key+secret. The secret is only returned once."""
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Partner", partner)
    user = doc.ensure_user()
    api_secret = frappe.generate_hash(length=32)
    if not user.api_key:
        user.api_key = frappe.generate_hash(length=16)
    user.api_secret = api_secret
    user.save(ignore_permissions=True)

    doc.db_set({"user": user.name, "api_key": user.api_key})
    if doc.require_signature and not doc.get_password("signing_secret", raise_exception=False):
        doc.signing_secret = frappe.generate_hash(length=40)
        doc.save()
    return {
        "user": user.name,
        "api_key": user.api_key,
        "api_secret": api_secret,
        "signing_secret": doc.get_password("signing_secret", raise_exception=False)
        if doc.require_signature else None,
        "authorization_header": f"token {user.api_key}:{api_secret}",
        "ping_url": f"{frappe.utils.get_url()}/api/method/neo_connect.api.ping",
    }


@frappe.whitelist()
def revoke_credentials(partner):
    frappe.only_for("System Manager")
    doc = frappe.get_doc("API Partner", partner)
    if doc.user:
        user = frappe.get_doc("User", doc.user)
        user.api_secret = frappe.generate_hash(length=32)  # old secret stops working
        user.save(ignore_permissions=True)
    return True
