frappe.ui.form.on("API Partner", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__(frm.doc.user ? "Regenerate Secret" : "Generate Credentials"), () => {
			const go = () =>
				frappe.call({
					method: "neo_connect.neo_connect.doctype.api_partner.api_partner.generate_credentials",
					args: { partner: frm.doc.name },
					freeze: true,
				}).then((r) => {
					const c = r.message;
					frm.reload_doc();
					const rows = [
						["API User", c.user],
						["API Key", c.api_key],
						["API Secret", c.api_secret],
						["Authorization header", c.authorization_header],
						["Signing secret (HMAC)", c.signing_secret || "not required"],
						["Test URL (GET)", c.ping_url],
					]
						.map(([k, v]) => `<tr><th style="width:35%">${k}</th><td><code style="user-select:all">${frappe.utils.escape_html(v || "")}</code></td></tr>`)
						.join("");
					frappe.msgprint({
						title: __("Share these with {0} securely", [frm.doc.partner_name]),
						indicator: "orange",
						message: `<p>${__("The API secret is shown only once. Copy it now.")}</p><table class="table table-bordered">${rows}</table>`,
						wide: true,
					});
				});
			if (frm.doc.user) {
				frappe.confirm(__("The old secret will stop working immediately. Continue?"), go);
			} else {
				go();
			}
		}, __("Credentials"));

		if (frm.doc.user) {
			frm.add_custom_button(__("Revoke Access"), () => {
				frappe.confirm(__("Revoke this partner's current secret?"), () =>
					frappe.call({
						method: "neo_connect.neo_connect.doctype.api_partner.api_partner.revoke_credentials",
						args: { partner: frm.doc.name },
					}).then(() => frappe.show_alert({ message: __("Secret revoked"), indicator: "red" }))
				);
			}, __("Credentials"));
		}

		frm.add_custom_button(__("Request Logs"), () =>
			frappe.set_route("List", "API Request Log", { partner: frm.doc.name })
		);
		frm.add_custom_button(__("Endpoints"), () =>
			frappe.set_route("List", "API Endpoint", { allowed_partners: ["like", `%${frm.doc.name}%`] })
		);
	},
});
