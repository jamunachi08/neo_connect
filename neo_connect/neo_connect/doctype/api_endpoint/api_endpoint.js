const CC_METHOD = "neo_connect.neo_connect.doctype.api_endpoint.api_endpoint";

frappe.ui.form.on("API Endpoint", {
	refresh(frm) {
		if (frm.is_new()) {
			frm.add_custom_button(__("Start from Preset"), () => cc_choose_preset());
			return;
		}

		frm.set_df_property("endpoint_url", "description",
			`<a class="btn btn-xs btn-default" onclick="frappe.utils.copy_to_clipboard('${frm.doc.endpoint_url}')">${__("Copy URL")}</a>`);

		if (frm.doc.direction === "Inbound") {
			frm.add_custom_button(__("Test Mapping"), () => cc_test_mapping(frm), __("Actions"));
		} else {
			frm.add_custom_button(__("Preview Response"), () => cc_preview(frm), __("Actions"));
		}
		frm.add_custom_button(__("Health Check"), () => nc_health(frm)).addClass("btn-primary");
		frm.add_custom_button(__("Partner Guide"), () => cc_partner_guide(frm), __("Actions"));
		frm.add_custom_button(__("Export Integration Pack"), () =>
			window.open(`/api/method/${CC_METHOD}.download_pack?endpoint=${encodeURIComponent(frm.doc.name)}`, "_blank"), __("Actions"));
		frm.add_custom_button(__("Request Logs"), () =>
			frappe.set_route("List", "API Request Log", { endpoint: frm.doc.name }), __("Actions"));
		if (frm.doc.direction === "Inbound") {
			frm.add_custom_button(__("Imported Records"), () =>
				frappe.set_route("List", "API External Reference", { endpoint: frm.doc.name }), __("Actions"));
		}
	},

	endpoint_title(frm) {
		if (frm.is_new() && frm.doc.endpoint_title && !frm.doc.slug) {
			frm.set_value("slug", frm.doc.endpoint_title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""));
		}
	},
});

function cc_json(obj) {
	return `<pre style="max-height:60vh;overflow:auto;font-size:12px">${frappe.utils.escape_html(JSON.stringify(obj, null, 2))}</pre>`;
}

function cc_test_mapping(frm) {
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the endpoint first."));
		return;
	}
	frappe.call({ method: `${CC_METHOD}.test_mapping`, args: { endpoint: frm.doc.name }, freeze: true })
		.then((r) => {
			const html = (r.message || []).map((res, i) => {
				const bad = (res.errors || []).length;
				const head = bad
					? `<div class="alert alert-danger">${res.errors.map(frappe.utils.escape_html).join("<br>")}</div>`
					: `<div class="alert alert-success">${__("Record {0} ({1}) maps cleanly", [i + 1, res.external_id || ""])}</div>`;
				const warn = (res.warnings || []).length
					? `<div class="alert alert-warning">${res.warnings.map(frappe.utils.escape_html).join("<br>")}</div>` : "";
				return head + warn + cc_json(res.doc || {});
			}).join("<hr>");
			frappe.msgprint({ title: __("Mapping Result (nothing was saved)"), message: html, wide: true });
		});
}

function cc_preview(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Preview Response"),
		fields: [{ fieldname: "query", fieldtype: "Code", options: "JSON", label: __("Query params (JSON)"), default: "{\"limit\": 5}" }],
		primary_action_label: __("Run"),
		primary_action(v) {
			frappe.call({ method: `${CC_METHOD}.preview_response`, args: { endpoint: frm.doc.name, query: v.query } })
				.then((r) => frappe.msgprint({ title: __("Response"), message: cc_json(r.message), wide: true }));
		},
	});
	d.show();
}

function cc_partner_guide(frm) {
	frappe.call({ method: `${CC_METHOD}.partner_guide`, args: { endpoint: frm.doc.name } }).then((r) => {
		const md = r.message;
		const d = new frappe.ui.Dialog({
			title: __("Partner Guide"),
			size: "extra-large",
			fields: [{ fieldname: "md", fieldtype: "Code", options: "Markdown", default: md, read_only: 1 }],
			primary_action_label: __("Copy"),
			primary_action() { frappe.utils.copy_to_clipboard(md); },
			secondary_action_label: __("Download .md"),
			secondary_action() {
				const a = document.createElement("a");
				a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
				a.download = `${frm.doc.name}-api-guide.md`;
				a.click();
			},
		});
		d.show();
	});
}

function cc_choose_preset() {
	frappe.call(`${CC_METHOD}.list_presets`).then((r) => {
		const presets = r.message || [];
		const d = new frappe.ui.Dialog({
			title: __("Create Endpoint from Preset"),
			fields: [
				{ fieldname: "preset", fieldtype: "Select", label: __("Preset"), reqd: 1,
					options: presets.map((p) => ({ value: p.file, label: `${p.title} — ${p.direction} (${p.platform})` })) },
				{ fieldname: "partner", fieldtype: "Link", options: "API Partner", label: __("Allow Partner") },
				{ fieldname: "slug", fieldtype: "Data", label: __("Endpoint Code (optional)") },
			],
			primary_action_label: __("Create"),
			primary_action(v) {
				frappe.call({ method: `${CC_METHOD}.load_preset`, args: v, freeze: true }).then((res) => {
					d.hide();
					frappe.set_route("Form", "API Endpoint", res.message);
				});
			},
		});
		d.show();
	});
}

function nc_health(frm) {
	const go = () => frappe.call({ method: `${CC_METHOD}.run_health_check`, args: { endpoint: frm.doc.name }, freeze: true,
		freeze_message: __("Checking the setup and dry-running the sample order...") }).then((r) => {
		const d = new frappe.ui.Dialog({ title: __("Health Check: {0}", [frm.doc.name]), size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "body" }] });
		d.fields_dict.body.$wrapper.html(r.message.html);
		d.show();
	});
	frm.is_dirty() ? frm.save().then(go) : go();
}
