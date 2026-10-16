# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Server-side gates for the payment entry points.

Why this module exists
----------------------
Every switch in ``Pinelabs Settings`` used to be enforced **only in the
browser** — ``public/js/pinelabs_buttons.js`` reads the Settings single and
decides which buttons to draw. The whitelisted API methods themselves ran no
such check, so turning a switch off hid the button but left the endpoint
callable by any logged-in user:

  - ``enabled`` (master switch)      — gated the Terminal flow via
    ``pinelabs_client``, but the Plural flow never read it at all.
  - ``enable_payment_links``         — gated the button and
    ``config.test_plural_connection``, never ``initiate_payment_link``.
  - ``payable_doctypes`` rows        — read only by the JS; the API accepted
    any doctype, listed or not.
  - No permission check of any kind  — a zero-privilege user could start a
    real card charge against any document, and read/cancel any transaction.

These helpers close that gap. They are called at the top of both
``initiate_*`` entry points (before anything touches Pine Labs / Plural, so a
refused call never leaves a customer charged) and on the transaction-scoped
endpoints that take a bare ``transaction_name``.

Deliberately NOT enforced here
------------------------------
``show_on_docstatus`` stays a presentation-only hint. It says *show the
button on drafts / submitted docs*; the real docstatus rules live in each
flow's ``_load_eligible_source`` (Terminal accepts drafts and auto-submits
them at finalize time, Plural requires a submitted doc). Enforcing the
display hint here would break the documented draft flow.

The **permissive path** is also preserved: a doctype with *no* Payable
Documents row is still allowed through, matching the contract documented in
``transaction.validate_finalization_ready`` case (3) that consumer apps
(e.g. ``fin_pos``) and custom buttons rely on. What is now refused is a row
that *exists* and is explicitly switched off — previously a no-op.
"""

import frappe
from frappe import _
from frappe.utils import cint

# flow → (Payable Documents per-flow toggle, human label)
_FLOW_TOGGLE = {
	"Terminal": ("enable_pay_on_machine", "Pay on Machine"),
	"Plural": ("enable_send_payment_link", "Send Payment Link"),
}


def assert_integration_enabled(flow):
	"""Refuse when the integration (or this flow) is switched off in Settings.

	``enabled`` is the master switch and covers both flows —
	``pinelabs_client`` already enforced it for Terminal, but nothing enforced
	it for Plural, so a payment link could still be created and delivered to a
	customer with the integration fully "off".
	"""
	settings = frappe.get_single("Pinelabs Settings")

	if not cint(settings.enabled):
		frappe.throw(
			_("Pine Labs integration is not enabled. Tick 'Enabled' in Pinelabs Settings."),
			title=_("Integration Disabled"),
		)

	if flow == "Plural" and not cint(getattr(settings, "enable_payment_links", 0)):
		frappe.throw(
			_("Payment Links are not enabled. Tick 'Enable Payment Links' in Pinelabs Settings."),
			title=_("Payment Links Disabled"),
		)


def assert_doctype_payable(reference_doctype, flow):
	"""Refuse when the doctype's Payable Documents row is switched off.

	No row at all → allowed (the permissive path — see module docstring).
	Row present   → both ``is_enabled`` and the per-flow toggle must be on.
	"""
	from pinelabs_fin.api import transaction as txn_service

	config = txn_service._get_payable_config(reference_doctype)
	if not config:
		# Permissive path — caller opted out of configuration entirely.
		return

	if not cint(config.get("is_enabled")):
		frappe.throw(
			_("Pine Labs payments are disabled for {0}. Tick 'Enabled' on its row in Pinelabs Settings → Payable Documents.").format(
				reference_doctype
			),
			title=_("Document Type Disabled"),
		)

	toggle_field, label = _FLOW_TOGGLE.get(flow, (None, None))
	if toggle_field and not cint(config.get(toggle_field, 1)):
		frappe.throw(
			_("'{0}' is disabled for {1}. Tick it on the {1} row in Pinelabs Settings → Payable Documents.").format(
				label, reference_doctype
			),
			title=_("Payment Method Disabled"),
		)


def assert_can_pay(reference_doctype, reference_name, source_doc=None):
	"""Refuse when the session user may not write to the source document.

	Taking a payment against a document mutates its financial state (a
	Payment Entry is posted against it, or a status field on it is written),
	so write permission on the source doc is the right bar. Without this,
	every ``@frappe.whitelist()`` payment method was callable by any logged-in
	user against any document — the endpoint URL is identical in every install
	of this app.

	Administrator and the cron/webhook contexts (which run as Administrator)
	bypass this the same way they bypass every other Frappe permission check.
	"""
	if reference_doctype == "POS Profile":
		assert_pos_profile_cashier(reference_name)
		return
	frappe.has_permission(
		doctype=reference_doctype,
		ptype="write",
		doc=source_doc or reference_name,
		throw=True,
	)


def assert_pos_profile_cashier(pos_profile):
	"""POS Awesome payments reference the POS Profile — the invoice doesn't
	exist until the payment succeeds (see api/posawesome.py). Cashiers rarely
	have write on POS Profile, so the bar there is: assigned to this profile
	(POS Profile → Applicable for Users) and allowed to create an invoice.
	Users with write on the profile (Accounts Manager etc.) also pass.
	"""
	if frappe.session.user == "Administrator" or frappe.has_permission(
		"POS Profile", ptype="write", doc=pos_profile
	):
		return
	assigned = frappe.db.exists(
		"POS Profile User",
		{"parent": pos_profile, "parenttype": "POS Profile", "user": frappe.session.user},
	)
	can_invoice = frappe.has_permission("Sales Invoice", ptype="create") or frappe.has_permission(
		"POS Invoice", ptype="create"
	)
	if not (assigned and can_invoice):
		frappe.throw(
			_("You are not a cashier on POS Profile {0}.").format(pos_profile),
			frappe.PermissionError,
		)


def assert_initiate_allowed(reference_doctype, reference_name, *, flow, source_doc=None):
	"""Single gate for both ``initiate_*`` entry points.

	Runs the Settings switches, then the per-doctype row, then the permission
	check — cheapest and most likely-to-refuse first. Must be called BEFORE
	the gateway is contacted so a refusal can never leave a customer charged.
	"""
	assert_integration_enabled(flow)
	assert_doctype_payable(reference_doctype, flow)
	assert_can_pay(reference_doctype, reference_name, source_doc=source_doc)


def assert_txn_access(txn):
	"""Refuse when the session user may not act on this transaction.

	The transaction-scoped endpoints (``poll_terminal_status``,
	``check_terminal_status``, ``cancel_terminal_payment``,
	``retry_failed_terminal_payment``, ``refresh_payment_link_status``) take a
	bare ``transaction_name``. Without this check any logged-in user could
	read another user's payment records, or cancel a live payment they have
	nothing to do with, just by guessing a ``PLT-…`` name.

	Access is derived from the *source document*, not the transaction row, so
	it stays consistent with :func:`assert_can_pay` — whoever may take the
	payment may also watch and cancel it.

	A transaction whose source doc has since been deleted falls back to a read
	check on Pinelabs Transaction itself, so the row stays reachable for
	reconciliation instead of becoming permanently inaccessible.
	"""
	reference_doctype = getattr(txn, "reference_doctype", None)
	reference_name = getattr(txn, "reference_name", None)

	if reference_doctype and reference_name and frappe.db.exists(reference_doctype, reference_name):
		assert_can_pay(reference_doctype, reference_name)
		return

	frappe.has_permission(doctype="Pinelabs Transaction", ptype="read", throw=True)
