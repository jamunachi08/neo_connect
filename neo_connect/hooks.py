app_name = "neo_connect"
app_title = "NeoConnect"
app_publisher = "Your Company"
app_description = "NeoConnect: configurable inbound/outbound API gateway connecting e-commerce platforms and partners to ERPNext"
app_email = "it@yourcompany.com"
app_license = "MIT"

required_apps = ["erpnext"]

scheduler_events = {
    "daily": [
        "neo_connect.neo_connect.doctype.api_request_log.api_request_log.delete_old_logs",
    ],
}

# API Request Log and API External Reference are operational data - not exported as fixtures.
