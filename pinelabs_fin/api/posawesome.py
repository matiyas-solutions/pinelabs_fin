"""POS Awesome integration.

POS Awesome submits its invoice only after payment, so terminal payments are
initiated against the POS Profile (see public/js/pinelabs_posawesome.js) with
create_payment_entry=0 — the payment is recorded on the invoice's own
payments table. Each payment carries POS Awesome's posa_client_request_id,
which the invoice also stores, so:

- before charging, the till asks which payments this sale already has
  (no double charge after a failed submit, refresh or new tab);
- on invoice submit, the payments are re-pointed at the invoice;
- payments still pointing at a POS Profile (money taken, no invoice) are
  shown to the cashier and logged for reconciliation.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

POS_INVOICE_DOCTYPES = ("Sales Invoice", "POS Invoice")
OPEN_OR_PAID = ("INITIATED", "PENDING", "SUCCESS")
UNLINKED_ALERT_AFTER_MINUTES = 15


@frappe.whitelist()
def get_request_payments(client_request_id: str) -> list:
	"""Terminal payments already started or taken for this POS sale."""
	if not client_request_id:
		return []
	return frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"client_request_id": client_request_id,
			"flow_type": "Terminal",
			"status": ["in", OPEN_OR_PAID],
		},
		fields=["name", "status", "mode_of_payment", "payment_method", "amount", "reference_doctype"],
		order_by="creation asc",
	)


def link_on_invoice_submit(doc, method=None):
	"""doc_events hook: attach this sale's Pine Labs payments to the invoice."""
	request_id = doc.get("posa_client_request_id")
	if not request_id:
		return
	names = frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"client_request_id": request_id,
			"status": "SUCCESS",
			"reference_doctype": "POS Profile",
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			"Pinelabs Transaction",
			name,
			{"reference_doctype": doc.doctype, "reference_name": doc.name},
		)


@frappe.whitelist()
def get_unlinked_pos_payments() -> list:
	"""SUCCESS POS payments taken by the current user that have no invoice yet."""
	return frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"status": "SUCCESS",
			"reference_doctype": "POS Profile",
			"owner": frappe.session.user,
		},
		fields=["name", "reference_name", "mode_of_payment", "payment_method", "amount", "payment_id", "completed_at"],
		order_by="completed_at asc",
	)


@frappe.whitelist()
def link_pos_invoice(transactions, invoice_doctype: str, invoice_name: str) -> dict:
	"""Manually point SUCCESS POS-Profile terminal transactions at an invoice."""
	if isinstance(transactions, str):
		transactions = json.loads(transactions)
	if invoice_doctype not in POS_INVOICE_DOCTYPES:
		frappe.throw(_("Unsupported invoice type {0}.").format(invoice_doctype))
	if not frappe.db.exists(invoice_doctype, invoice_name):
		frappe.throw(_("{0} {1} does not exist.").format(invoice_doctype, invoice_name))
	frappe.has_permission(invoice_doctype, "write", invoice_name, throw=True)

	invoice_modes = set(
		frappe.get_all(
			"Sales Invoice Payment",  # child table for both Sales Invoice and POS Invoice
			filters={"parent": invoice_name, "parenttype": invoice_doctype},
			pluck="mode_of_payment",
		)
	)

	linked, skipped = [], []
	for name in transactions or []:
		txn = frappe.db.get_value(
			"Pinelabs Transaction",
			name,
			["name", "status", "reference_doctype", "mode_of_payment"],
			as_dict=True,
		)
		if (
			not txn
			or txn.status != "SUCCESS"
			or txn.reference_doctype != "POS Profile"
			or (txn.mode_of_payment and txn.mode_of_payment not in invoice_modes)
		):
			skipped.append(name)
			continue
		frappe.db.set_value(
			"Pinelabs Transaction",
			name,
			{"reference_doctype": invoice_doctype, "reference_name": invoice_name},
		)
		linked.append(name)

	if skipped:
		frappe.throw(
			_("Could not link {0} to {1} {2}. The payment must be SUCCESS, not yet linked, and the invoice must have a {3} payment row.").format(
				", ".join(skipped), invoice_doctype, invoice_name, _("Pine Labs")
			)
		)
	return {"linked": linked}


def alert_unlinked_pos_payments():
	"""Scheduler (every 15 min): log POS payments with no invoice after 15 min.

	Only payments completed in the previous 15-minute window are reported,
	so each one is logged once.
	"""
	now = now_datetime()
	rows = frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"status": "SUCCESS",
			"reference_doctype": "POS Profile",
			"completed_at": [
				"between",
				[
					add_to_date(now, minutes=-2 * UNLINKED_ALERT_AFTER_MINUTES),
					add_to_date(now, minutes=-UNLINKED_ALERT_AFTER_MINUTES),
				],
			],
		},
		fields=["name", "reference_name", "mode_of_payment", "amount", "payment_id", "owner", "completed_at"],
	)
	if not rows:
		return
	lines = [
		f"{r.name}: {r.amount} via {r.mode_of_payment} (Plutus ref {r.payment_id}), "
		f"POS Profile {r.reference_name}, cashier {r.owner}, paid at {r.completed_at}"
		for r in rows
	]
	frappe.log_error(
		title="Pinelabs POS payment without invoice",
		message=(
			"These Pine Labs payments succeeded but no POS invoice was created for them. "
			"Create the invoice and link it (POS page notice, or Pinelabs Transaction), "
			"or refund the customer through Pine Labs.\n\n" + "\n".join(lines)
		),
	)
