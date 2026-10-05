from frappe.model.document import Document

from neo_connect.engine.inbound import reference_name


class APIExternalReference(Document):
    def autoname(self):
        # deterministic key => the database primary key enforces one ERP document per external id
        self.name = reference_name(self.partner, self.endpoint, self.external_id)
