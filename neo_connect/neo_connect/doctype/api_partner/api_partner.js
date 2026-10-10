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

		frm.add_custom_button(__("Install Integration Pack"), () => nc_install_pack(frm)).addClass("btn-primary");
		frm.add_custom_button(__("Payload Samples"), () =>
			frappe.set_route("List", "API Payload Sample", { partner: frm.doc.name })
		);
		frm.add_custom_button(__("New Payload Sample"), () =>
			frappe.new_doc("API Payload Sample", { partner: frm.doc.name, title: `${frm.doc.name} - Sales Order` })
		);
		frm.add_custom_button(__("Request Logs"), () =>
			frappe.set_route("List", "API Request Log", { partner: frm.doc.name })
		);
		frm.add_custom_button(__("Endpoints"), () =>
			frappe.set_route("List", "API Endpoint", { allowed_partners: ["like", `%${frm.doc.name}%`] })
		);
	},
});

function nc_install_pack(frm) {
	const M = "neo_connect.neo_connect.doctype.api_partner.api_partner";
	if (!frm.doc.company) {
		frappe.msgprint(__("Set the Company on this partner first."));
		return;
	}
	frappe.call(`${M}.get_packs`).then((r) => {
		const packs = r.message || [];
		const d = new frappe.ui.Dialog({
			title: __("Install Integration Pack for {0}", [frm.doc.name]),
			fields: [
				{ fieldname: "pack_file", fieldtype: "Select", label: __("Ready-made pack"),
					options: [""].concat(packs.map((p) => ({ value: p.file, label: `${p.pack} (${p.platform || "any"} -> ${p.doctype})` }))),
					description: __("Or upload a pack exported from another site below") },
				{ fieldname: "upload", fieldtype: "Attach", label: __("Upload pack (.json)") },
				{ fieldname: "mop_account", fieldtype: "Link", options: "Account", label: __("Account for new Modes of Payment"),
					get_query: () => ({ filters: { company: frm.doc.company, is_group: 0, account_type: ["in", ["Bank", "Cash", "Receivable"]] } }),
					description: __("Empty = the company's default bank/cash account") },
				{ fieldname: "slug", fieldtype: "Data", label: __("Endpoint Code (optional)"),
					description: __("Default: the pack's code. An existing endpoint with that code is updated (old setup saved as a comment).") },
			],
			primary_action_label: __("Install"),
			primary_action(v) {
				const run = (pack_json) => frappe.call({ method: `${M}.install_pack`, freeze: true,
					freeze_message: __("Installing fields, payment methods and endpoint, then checking..."),
					args: { partner: frm.doc.name, pack_file: pack_json ? null : v.pack_file, pack_json, mop_account: v.mop_account, slug: v.slug } })
					.then((res) => {
						d.hide();
						const m = res.message;
						const log = (m.log || []).map(frappe.utils.escape_html).join("<br>");
						const r2 = new frappe.ui.Dialog({ title: __("Installed: {0}", [m.endpoint]), size: "extra-large",
							fields: [{ fieldtype: "HTML", fieldname: "body" }],
							primary_action_label: __("Open Endpoint"), primary_action() { frappe.set_route("Form", "API Endpoint", m.endpoint); } });
						r2.fields_dict.body.$wrapper.html(`<h5>${__("Health Check")}</h5>${m.html}<h5>${__("What was installed")}</h5><p class="small">${log}</p>`);
						r2.show();
					});
				if (v.upload) {
					fetch(v.upload).then((x) => x.text()).then((t) => run(t));
				} else if (v.pack_file) {
					run(null);
				} else {
					frappe.msgprint(__("Choose a ready-made pack or upload one"));
				}
			},
		});
		d.show();
	});
}
