frappe.listview_settings["API Payload Sample"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const c = { Draft: "grey", Analyzed: "orange", "Endpoint Generated": "green" };
		return [__(doc.status), c[doc.status] || "grey", `status,=,${doc.status}`];
	},
};
