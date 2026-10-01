frappe.ui.form.on("API Request Log", {
	refresh(frm) {
		if (frm.doc.direction === "Inbound" && ["Failed", "Partial", "Received", "Queued"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Reprocess"), () => {
				frappe.confirm(__("Process this payload again? Records already imported will be skipped."), () =>
					frappe.call({
						method: "neo_connect.neo_connect.doctype.api_request_log.api_request_log.reprocess",
						args: { log_name: frm.doc.name },
						freeze: true,
					}).then(() => frm.reload_doc())
				);
			});
		}
	},
});
