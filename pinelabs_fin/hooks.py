app_name = "pinelabs_fin"
app_title = "Pinelabs Fin"
app_publisher = "Finstein"
app_description = "Pine Labs payments for ERPNext: terminal taps and hosted payment links, with auto Payment Entry on success."
app_email = "bharathiraja.b@finstein.ai"
app_license = "mit"

# Apps
# ------------------

required_apps = ["erpnext"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "pinelabs_fin",
# 		"logo": "/assets/pinelabs_fin/logo.png",
# 		"title": "Pinelabs Fin",
# 		"route": "/pinelabs_fin",
# 		"has_permission": "pinelabs_fin.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/pinelabs_fin/css/pinelabs_fin.css"
app_include_js = [
	# Plain /assets files are cached by browsers for 12h — bump ?v= whenever
	# these files change so cashiers' browsers fetch the new version.
	"/assets/pinelabs_fin/js/pinelabs_buttons.js?v=20261021",
	"/assets/pinelabs_fin/js/pinelabs_posawesome.js?v=20261021",
]

# include js, css files in header of web template
# web_include_css = "/assets/pinelabs_fin/css/pinelabs_fin.css"
# web_include_js = "/assets/pinelabs_fin/js/pinelabs_fin.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "pinelabs_fin/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "pinelabs_fin/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "pinelabs_fin.utils.jinja_methods",
# 	"filters": "pinelabs_fin.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "pinelabs_fin.install.before_install"
after_install = "pinelabs_fin.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "pinelabs_fin.uninstall.before_uninstall"
# after_uninstall = "pinelabs_fin.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "pinelabs_fin.utils.before_app_install"
# after_app_install = "pinelabs_fin.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "pinelabs_fin.utils.before_app_uninstall"
# after_app_uninstall = "pinelabs_fin.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "pinelabs_fin.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Refuse a manual Payment Entry when a Pine Labs payment is in flight
# for the same source doc. Pairs with the finalize-time guard in
# api/transaction.py so the two payment flows can never silently
# double-record the same invoice. Our own PEs bypass this via
# flags.pinelabs_internal_pe.
doc_events = {
	"Payment Entry": {
		"validate": "pinelabs_fin.api.conflict_guard.refuse_if_pinelabs_active",
	},
	# POS Awesome: attach the sale's Pine Labs payments to the invoice.
	"Sales Invoice": {
		"on_submit": "pinelabs_fin.api.posawesome.link_on_invoice_submit",
	},
	"POS Invoice": {
		"on_submit": "pinelabs_fin.api.posawesome.link_on_invoice_submit",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"cron": {
		# Reconcile any PENDING transactions with Pine Labs every minute.
		# - Terminal: catches users who closed the dialog before the foreground
		#   poll saw a terminal state.
		# - Plural: catches missed webhooks (no public URL, dropped
		#   delivery, secret mismatch, …) so doc finalizes without manual click.
		"* * * * *": [
			"pinelabs_fin.api.terminal.reconcile_pending_terminal_payments",
			"pinelabs_fin.api.plural.reconcile_pending_plural_payments",
		],
		# POS Awesome: flag payments that never got an invoice.
		"*/15 * * * *": [
			"pinelabs_fin.api.posawesome.alert_unlinked_pos_payments",
		],
	},
}

# Testing
# -------

# before_tests = "pinelabs_fin.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "pinelabs_fin.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "pinelabs_fin.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["pinelabs_fin.utils.before_request"]
# after_request = ["pinelabs_fin.utils.after_request"]

# Job Events
# ----------
# before_job = ["pinelabs_fin.utils.before_job"]
# after_job = ["pinelabs_fin.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"pinelabs_fin.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
