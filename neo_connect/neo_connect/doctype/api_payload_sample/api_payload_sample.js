const NC_SAMPLE = "neo_connect.neo_connect.doctype.api_payload_sample.api_payload_sample";

frappe.ui.form.on("API Payload Sample", {
	refresh(frm) {
		frm.set_intro(
			__("1. Add one or more sample JSONs (paste, or upload a .json / spec file).  2. Analyze.  3. Review the Fields table (tick 'Create Field' for values you want to keep).  4. Generate Endpoint."),
			"blue"
		);
		if (frm.is_new()) return;

		frm.add_custom_button(__("Analyze"), () => {
			const run = () =>
				frappe.call({ method: `${NC_SAMPLE}.analyze`, args: { name: frm.doc.name }, freeze: true,
					freeze_message: __("Reading samples...") }).then(() => frm.reload_doc());
			if ((frm.doc.fields || []).length) {
				frappe.confirm(__("Re-analyzing replaces the Fields table (your edits in it are lost). Continue?"), run);
			} else {
				run();
			}
		}).addClass("btn-primary");

		if ((frm.doc.fields || []).length) {
			frm.add_custom_button(__(frm.doc.endpoint ? "Update Endpoint" : "Generate Endpoint"), () => {
				const go = () =>
					frappe.call({ method: `${NC_SAMPLE}.generate_endpoint`, args: { name: frm.doc.name }, freeze: true,
						freeze_message: __("Creating endpoint and validating samples...") })
						.then((r) => {
							frm.reload_doc();
							frappe.msgprint({ title: __("Validation Report"), wide: true,
								message: `<pre style="white-space:pre-wrap;font-size:12px">${frappe.utils.escape_html(r.message.report)}</pre>` });
						});
				frm.is_dirty() ? frm.save().then(go) : go();
			});
		}
		if (frm.doc.endpoint) {
			frm.add_custom_button(__("Open Endpoint"), () => frappe.set_route("Form", "API Endpoint", frm.doc.endpoint));
		}
	},
});
