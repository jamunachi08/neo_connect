frappe.listview_settings["API Endpoint"] = {
	add_fields: ["direction", "enabled"],
	get_indicator(doc) {
		if (!doc.enabled) return [__("Disabled"), "grey", "enabled,=,0"];
		return doc.direction === "Inbound"
			? [__("Inbound"), "blue", "direction,=,Inbound"]
			: [__("Outbound"), "purple", "direction,=,Outbound"];
	},
	onload(listview) {
		listview.page.add_inner_button(__("New from Preset"), () => {
			frappe.new_doc("API Endpoint");
		});
	},
};
