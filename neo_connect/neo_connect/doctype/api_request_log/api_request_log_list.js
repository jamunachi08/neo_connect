frappe.listview_settings["API Request Log"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { Success: "green", Duplicate: "blue", Queued: "orange", Received: "orange",
			Partial: "yellow", Failed: "red", Rejected: "red" };
		return [__(doc.status), colors[doc.status] || "grey", `status,=,${doc.status}`];
	},
};
