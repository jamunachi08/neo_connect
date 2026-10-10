const NC_SAMPLE = "neo_connect.neo_connect.doctype.api_payload_sample.api_payload_sample";
const nc_esc = (v) => frappe.utils.escape_html(v == null ? "" : String(v));

frappe.ui.form.on("API Payload Sample", {
	refresh(frm) {
		const steps = {
			Draft: __("Add the partner's sample JSON and/or spec document, then Save. The gap is prepared automatically."),
			"Gap Ready": __("Gap prepared. Click <b>Review & Confirm</b> to choose which fields to create and map."),
			Analyzed: __("Gap prepared. Click <b>Review & Confirm</b> to choose which fields to create and map."),
			Confirmed: __("Confirmed. Fields created and mapped."),
			"Endpoint Generated": __("Endpoint ready. See the Validation Report; share the Partner Guide from the endpoint."),
		};
		frm.set_intro(steps[frm.doc.status] || steps.Draft, frm.doc.status === "Endpoint Generated" ? "green" : "blue");
		if (frm.is_new()) return;

		if ((frm.doc.fields || []).length) {
			frm.add_custom_button(__("Review & Confirm"), () => nc_review(frm)).addClass("btn-primary");
		}
		frm.add_custom_button(__("Re-analyze"), () => {
			frappe.call({ method: `${NC_SAMPLE}.analyze`, args: { name: frm.doc.name }, freeze: true,
				freeze_message: __("Preparing the gap...") }).then(() => frm.reload_doc());
		}, __("More"));
		if ((frm.doc.fields || []).length) {
			frm.add_custom_button(__("Gap Report"), () => nc_gap_report(frm), __("More"));
			frm.add_custom_button(__("Download Gap Report (Excel)"), () => nc_download_gap(frm), __("More"));
			frm.add_custom_button(__(frm.doc.endpoint ? "Update Endpoint" : "Generate Endpoint"), () => nc_generate(frm), __("More"));
		}
		if (frm.doc.created_fields) {
			frm.add_custom_button(__("Undo Created Fields"), () => {
				frappe.confirm(
					__("Delete the custom fields created by this sample?<br><br>{0}<br><br>Values already saved in them are not kept.",
						[nc_esc(frm.doc.created_fields).replace(/\n/g, "<br>")]),
					() => frappe.call({ method: `${NC_SAMPLE}.undo_created_fields`, args: { name: frm.doc.name }, freeze: true })
						.then((r) => { frm.reload_doc(); frappe.show_alert({ message: __("{0} field(s) removed", [(r.message || []).length]), indicator: "orange" }); })
				);
			}, __("More"));
		}
		if (frm.doc.endpoint) {
			frm.add_custom_button(__("Open Endpoint"), () => frappe.set_route("Form", "API Endpoint", frm.doc.endpoint));
		}
	},
});

// any manual edit in the Fields table marks the row as reviewed, so re-analysis keeps it
frappe.ui.form.on("API Sample Field", {
	include: nc_mark, erp_field: nc_mark, transform: nc_mark, create_field: nc_mark, target_doctype: nc_mark,
	proposed_fieldname: nc_mark, proposed_label: nc_mark, proposed_fieldtype: nc_mark, value_map: nc_mark, role: nc_mark,
	fields_add(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, { reviewed: 1, source: "Manual", gap_status: "Create Field", role: "Unmapped" });
	},
});
function nc_mark(frm, cdt, cdn) {
	if (!locals[cdt][cdn].reviewed) frappe.model.set_value(cdt, cdn, "reviewed", 1);
}

// ---------------------------------------------------------------------------------------------
// Review & Confirm
// ---------------------------------------------------------------------------------------------
function nc_review(frm) {
	const open = () => frappe.call({ method: `${NC_SAMPLE}.get_review`, args: { name: frm.doc.name }, freeze: true })
		.then((r) => nc_review_dialog(frm, r.message));
	frm.is_dirty() ? frm.save().then(open) : open();
}

function nc_select(cls, options, value, extra = "") {
	return `<select class="form-control input-xs ${cls}" ${extra}>` +
		options.map((o) => `<option value="${nc_esc(o)}" ${o === value ? "selected" : ""}>${nc_esc(o || "—")}</option>`).join("") +
		"</select>";
}

let nc_dl = 0;
function nc_combo(cls, options, value, extra = "") {
	// pick an existing option or type a new one (added to the Select list on confirm)
	const id = `nc-dl-${++nc_dl}`;
	return `<input class="form-control input-xs ${cls}" list="${id}" value="${nc_esc(value)}" ${extra} placeholder="${__("choose or type a new option")}">` +
		`<datalist id="${id}">${options.map((o) => `<option value="${nc_esc(o)}"></option>`).join("")}</datalist>`;
}

function nc_review_dialog(frm, data) {
	const T = data.targets, FT = data.fieldtypes;
	const create = data.create.map((c) => `
		<tr data-name="${nc_esc(c.name)}">
			<td><input type="checkbox" class="nc-create" ${c.create ? "checked" : ""}></td>
			<td><code>${nc_esc(c.path)}</code><div class="text-muted small">${nc_esc(c.sample).slice(0, 40)} ${c.presence ? "· " + nc_esc(c.presence) : ""} ${c.source === "Spec" ? "· <i>spec only</i>" : ""}</div></td>
			<td>${nc_select("nc-target", T, c.target)}</td>
			<td><input class="form-control input-xs nc-label" value="${nc_esc(c.label)}"></td>
			<td><input class="form-control input-xs nc-fieldname" value="${nc_esc(c.fieldname)}"></td>
			<td>${nc_select("nc-type", FT, c.fieldtype)}</td>
			<td><input class="form-control input-xs nc-options" value="${nc_esc(c.options)}" placeholder="${__("Select/Link only")}"></td>
		</tr>`).join("");

	const mapped = data.mapped.map((m) => `
		<tr data-name="${nc_esc(m.name)}">
			<td><input type="checkbox" class="nc-use" ${m.include ? "checked" : ""}></td>
			<td><code>${nc_esc(m.path)}</code><div class="text-muted small">${nc_esc(m.sample).slice(0, 40)}</div></td>
			<td>${m.role === "Customer Field" ? `<span class="indicator-pill blue">${__("Customer")}</span>` : nc_esc(m.target)}</td>
			<td><input class="form-control input-xs nc-field" value="${nc_esc(m.field)}"></td>
			<td class="small text-muted">${nc_esc(m.transform)} ${m.role === "Customer Field" ? "· " + __("fetched into the document from the Customer") : ""}</td>
		</tr>`).join("");

	const paths = [""].concat(data.text_paths || []);
	const options = data.options.map((o) => `
		<div class="nc-opt" data-name="${nc_esc(o.name)}" style="margin-bottom:14px;padding:8px;border:1px solid var(--border-color);border-radius:6px">
			<div><code>${nc_esc(o.path)}</code> &rarr; <b>${nc_esc(o.field)}</b> <span class="text-muted small">(${nc_esc(o.target)})</span></div>
			<div class="small" style="margin:6px 0">
				<label style="margin-right:14px"><input type="radio" name="mode-${nc_esc(o.name)}" class="nc-mode" value="map" ${o.keep_as_sent ? "" : "checked"}> ${__("Translate to existing options")}</label>
				<label><input type="radio" name="mode-${nc_esc(o.name)}" class="nc-mode" value="keep" ${o.keep_as_sent ? "checked" : ""}> ${__("Keep values as sent (new values are added to the list automatically)")}</label>
			</div>
			<div class="nc-keep-box" style="${o.keep_as_sent ? "" : "display:none"}">
				<span class="small">${__("If empty, use")}</span> ${nc_select("nc-fallback", paths, o.fallback, 'style="display:inline-block;width:auto"')}
			</div>
			<div class="nc-map-box" style="${o.keep_as_sent ? "display:none" : ""}">
				<table class="table table-sm table-bordered small" style="margin-top:4px;max-width:640px"><tbody>
				${o.values.map((v) => `<tr><td style="width:45%">${v.value === "*" ? `<i>${__("any other / missing value")}</i>` : nc_esc(v.value)}</td>
					<td>${nc_combo("nc-map", o.select_options, v.mapped, `data-value="${nc_esc(v.value)}"`)}</td></tr>`).join("")}
				</tbody></table>
				<span class="small text-muted">${__("A value you type that is not in the list yet is added to the field's options.")}</span>
			</div>
		</div>`).join("");

	const html = `
	<div class="nc-review">
		<p class="text-muted">${__("Tick what to keep, edit names / types / where each field is created, translate values, add fields the samples don't show. Nothing is changed until you click <b>Confirm & Apply</b>.")}</p>

		<h5>1. ${__("New fields to create")} (${data.create.length})
			<a class="small nc-all" style="margin-left:8px">${__("tick all")}</a> · <a class="small nc-none">${__("untick all")}</a></h5>
		<div style="overflow-x:auto"><table class="table table-sm table-bordered small nc-create-t">
			<thead><tr><th></th><th>${__("Partner JSON")}</th><th>${__("Create in")}</th><th>${__("Label")}</th><th>${__("Fieldname")}</th><th>${__("Type")}</th><th>${__("Options")}</th></tr></thead>
			<tbody>${create || `<tr><td colspan="7" class="text-muted">${__("Nothing to create")}</td></tr>`}</tbody></table></div>

		<h5>2. ${__("Add fields the samples don't show")}</h5>
		<div style="overflow-x:auto"><table class="table table-sm table-bordered small nc-add-t">
			<thead><tr><th>${__("Partner JSON path")}</th><th>${__("Create in")}</th><th>${__("Label")}</th><th>${__("Fieldname")}</th><th>${__("Type")}</th><th>${__("Options")}</th><th></th></tr></thead>
			<tbody></tbody></table></div>
		<button class="btn btn-xs btn-default nc-add">+ ${__("Add field")}</button>

		<h5 style="margin-top:18px">3. ${__("Mapped automatically to existing fields")} (${data.mapped.length})</h5>
		<div style="overflow-x:auto"><table class="table table-sm table-bordered small nc-map-t">
			<thead><tr><th>${__("Use")}</th><th>${__("Partner JSON")}</th><th>${__("Document")}</th><th>${__("ERPNext field")}</th><th></th></tr></thead>
			<tbody>${mapped || `<tr><td colspan="5" class="text-muted">${__("None")}</td></tr>`}</tbody></table></div>

		<h5 style="margin-top:18px">4. ${__("Value translations for Select fields")} (${data.options.length})</h5>
		${options || `<p class="text-muted small">${__("None")}</p>`}

		<h5 style="margin-top:18px">5. ${__("Payment methods → Mode of Payment")} (${(data.payments || []).length})</h5>
		${(data.payments || []).length ? `<p class="text-muted small">${__("Used to record paid orders")} (<code>${nc_esc(data.payment_path)}</code>). ${__("Choose '+ New' to create a Mode of Payment.")}</p>
		<table class="table table-sm table-bordered small nc-mop-t"><tbody>
		${data.payments.map((p) => `<tr data-value="${nc_esc(p.value)}"><td style="width:22%">${p.value === "*" ? `<i>${__("any other method")}</i>` : nc_esc(p.value)}</td>
			<td style="width:26%">${nc_select("nc-mop", [""].concat(data.modes_of_payment).concat(["+ New"]), p.mapped)}</td>
			<td class="nc-newmop" style="display:none">
				<input class="form-control input-xs nc-mop-name" style="display:inline-block;width:150px" placeholder="${__("Name, e.g. Tabby")}" value="${p.value === "*" ? "" : nc_esc(p.value.charAt(0).toUpperCase() + p.value.slice(1))}">
				${nc_select("nc-mop-acc", [""].concat(data.accounts || []), "", 'style="display:inline-block;width:auto"')}
				<span class="small text-muted">${__("default account for")} ${nc_esc(data.company || "")}</span>
			</td></tr>`).join("")}
		</tbody></table>` : `<p class="text-muted small">${__("No payment recording for this document")}</p>`}

		<label style="margin-top:12px"><input type="checkbox" class="nc-gen" checked> ${__("Also create / update the API Endpoint and validate the samples")}</label>
	</div>`;

	const d = new frappe.ui.Dialog({
		title: __("Review & Confirm: {0}", [frm.doc.title]),
		size: "extra-large",
		fields: [{ fieldtype: "HTML", fieldname: "body" }],
		primary_action_label: __("Confirm & Apply"),
		primary_action() {
			const $w = d.fields_dict.body.$wrapper;
			const decisions = { create: [], mapped: [], options: [], add: [], generate: $w.find(".nc-gen").is(":checked") ? 1 : 0 };
			$w.find(".nc-create-t tbody tr[data-name]").each(function () {
				const $r = $(this);
				decisions.create.push({ name: $r.data("name"), create: $r.find(".nc-create").is(":checked") ? 1 : 0,
					target: $r.find(".nc-target").val(), label: $r.find(".nc-label").val(), fieldname: $r.find(".nc-fieldname").val(),
					fieldtype: $r.find(".nc-type").val(), options: $r.find(".nc-options").val() });
			});
			$w.find(".nc-map-t tbody tr[data-name]").each(function () {
				const $r = $(this);
				decisions.mapped.push({ name: $r.data("name"), include: $r.find(".nc-use").is(":checked") ? 1 : 0, field: $r.find(".nc-field").val() });
			});
			$w.find(".nc-opt").each(function () {
				const $o = $(this), map = {};
				$o.find(".nc-map").each(function () { map[$(this).data("value")] = $(this).val(); });
				decisions.options.push({ name: $o.data("name"), map,
					keep_as_sent: $o.find(".nc-mode:checked").val() === "keep" ? 1 : 0,
					fallback: $o.find(".nc-fallback").val() });
			});
			if ($w.find(".nc-mop").length) {
				decisions.mop = {}; decisions.new_mop = [];
				$w.find(".nc-mop-t tr[data-value]").each(function () {
					const $r = $(this), v = $r.data("value"), sel = $r.find(".nc-mop").val();
					if (sel === "+ New") {
						decisions.new_mop.push({ value: v, name: $r.find(".nc-mop-name").val(), account: $r.find(".nc-mop-acc").val() });
					} else {
						decisions.mop[v] = sel;
					}
				});
			}
			$w.find(".nc-add-t tbody tr").each(function () {
				const $r = $(this);
				const path = $r.find(".nc-path").val();
				if (path) decisions.add.push({ path, target: $r.find(".nc-target").val(), label: $r.find(".nc-label").val(),
					fieldname: $r.find(".nc-fieldname").val(), fieldtype: $r.find(".nc-type").val(), options: $r.find(".nc-options").val() });
			});
			const n = decisions.create.filter((c) => c.create).length + decisions.add.length;
			frappe.confirm(__("Create {0} field(s), apply the mappings and translations{1}?", [n, decisions.generate ? __(" and build the endpoint") : ""]), () => {
				frappe.call({ method: `${NC_SAMPLE}.confirm_review`, args: { name: frm.doc.name, decisions: JSON.stringify(decisions) },
					freeze: true, freeze_message: __("Creating fields and mapping...") }).then((r) => {
					d.hide();
					frm.reload_doc();
					const m = r.message || {};
					frappe.msgprint({ title: __("Done"), wide: true, message:
						`<p>${__("Fields created")}: <b>${(m.created || []).length}</b></p>` +
						((m.created || []).length ? `<p class="small">${(m.created || []).map(nc_esc).join("<br>")}</p>` : "") +
						(m.report ? `<pre style="white-space:pre-wrap;font-size:12px">${nc_esc(m.report)}</pre>` : "") });
				});
			});
		},
	});
	const $w = d.fields_dict.body.$wrapper;
	$w.html(html);
	$w.on("click", ".nc-all", () => $w.find(".nc-create").prop("checked", true));
	$w.on("click", ".nc-none", () => $w.find(".nc-create").prop("checked", false));
	$w.on("click", ".nc-del", function () { $(this).closest("tr").remove(); });
	$w.on("change", ".nc-mode", function () {
		const $o = $(this).closest(".nc-opt"), keep = $(this).val() === "keep";
		$o.find(".nc-keep-box").toggle(keep); $o.find(".nc-map-box").toggle(!keep);
	});
	$w.on("change", ".nc-mop", function () { $(this).closest("tr").find(".nc-newmop").toggle($(this).val() === "+ New"); });
	$w.on("click", ".nc-add", () => {
		$w.find(".nc-add-t tbody").append(`<tr>
			<td><input class="form-control input-xs nc-path" placeholder="e.g. vehicle.color"></td>
			<td>${nc_select("nc-target", T, T[0])}</td>
			<td><input class="form-control input-xs nc-label"></td>
			<td><input class="form-control input-xs nc-fieldname" placeholder="custom_..."></td>
			<td>${nc_select("nc-type", FT, "Data")}</td>
			<td><input class="form-control input-xs nc-options"></td>
			<td><a class="nc-del text-danger">&times;</a></td></tr>`);
	});
	$w.on("input", ".nc-add-t .nc-path", function () {
		const $r = $(this).closest("tr");
		const parts = $(this).val().replace(/\[\]/g, "").split(".").slice(-2).join("_").replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();
		if (!$r.data("touched")) {
			$r.find(".nc-fieldname").val(parts ? "custom_" + parts : "");
			$r.find(".nc-label").val(parts.split("_").filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" "));
		}
	});
	$w.on("input", ".nc-add-t .nc-label, .nc-add-t .nc-fieldname", function () { $(this).closest("tr").data("touched", 1); });
	d.show();
}

// ---------------------------------------------------------------------------------------------
function nc_generate(frm) {
	const go = () => frappe.call({ method: `${NC_SAMPLE}.generate_endpoint`, args: { name: frm.doc.name }, freeze: true,
		freeze_message: __("Creating endpoint and validating samples...") }).then((r) => {
		frm.reload_doc();
		frappe.msgprint({ title: __("Validation Report"), wide: true,
			message: `<pre style="white-space:pre-wrap;font-size:12px">${nc_esc(r.message.report)}</pre>` });
	});
	frm.is_dirty() ? frm.save().then(go) : go();
}

function nc_gap_report(frm) {
	const show = () => frappe.call({ method: `${NC_SAMPLE}.get_gap_report`, args: { name: frm.doc.name }, freeze: true })
		.then((r) => {
			const d = new frappe.ui.Dialog({ title: __("Field Gap Report"), size: "extra-large",
				fields: [{ fieldtype: "HTML", fieldname: "body" }], primary_action_label: __("Download Excel"),
				primary_action() { nc_download_gap(frm); } });
			d.fields_dict.body.$wrapper.html(r.message.html);
			d.show();
		});
	frm.is_dirty() ? frm.save().then(show) : show();
}

function nc_download_gap(frm) {
	const go = () => window.open(`/api/method/${NC_SAMPLE}.download_gap_report?name=${encodeURIComponent(frm.doc.name)}`, "_blank");
	frm.is_dirty() ? frm.save().then(go) : go();
}
