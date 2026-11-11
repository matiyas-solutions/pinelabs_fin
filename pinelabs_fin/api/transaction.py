# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Core state machine for Pinelabs Transaction.

This module is the *only* place that writes Pinelabs Transaction rows
or creates Payment Entries. terminal.py / plural.py / webhook.py all
delegate to the helpers here so the state-transition rules and
Payment-Entry guard rails stay in one place.

State machine (enforced by both this module and the doctype controller):

    INITIATED ──▶ PENDING ──▶ SUCCESS
        │            │
        │            ├──▶ FAILED
        │            └──▶ CANCELLED
        │
        ├──▶ SUCCESS    (Terminal: response → success without PENDING)
        ├──▶ FAILED
        └──▶ CANCELLED

Payment Entry creation rules (from PHASE_1_PLAN §8):
  1. Created only from mark_success() in this module.
  2. Skipped if a PE with the same reference_no already exists.
  3. Skipped if the transaction already has a payment_entry linked.
  4. Built via erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry.
  5. mode_of_payment / reference_no / reference_date / remarks set explicitly.
  6. PE name is written back to the transaction; status flipped to SUCCESS.
  7. Wrapped in one DB transaction; rollback on any error.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

VALID_FLOW_TYPES = ("Terminal", "Plural")
ACTIVE_STATUSES = ("INITIATED", "PENDING")

# ERPNext's get_payment_entry helper natively supports these three. For any
# other doctype, mark_success will throw at PE creation time — see
# _create_payment_entry_idempotent. The validator at the bottom of this
# module checks this BEFORE the customer is asked to pay.
PE_NATIVE_DOCTYPES = ("Sales Invoice", "POS Invoice", "Payment Request")

# ──────────────────────────────────────────────────────────────────────────
# Canonical Pine Labs Mode of Payment names. The app installs and operates
# against exactly these three rows; no custom fields on Mode of Payment are
# required to recognise them. Renaming any of these rows will break the
# integration — they are part of the install contract.
# ──────────────────────────────────────────────────────────────────────────

# Pine Labs Cloud allowed_payment_mode integers.
# UPI has two codes: 10 = UPI Sale (UPI QR on terminal), 11 = UPI Bharat QR.
# Which one is sent is configurable via Pinelabs Settings → UPI Payment Mode Code.
CARD_TERMINAL_CODE = 1
UPI_TERMINAL_CODES = (10, 11)
DEFAULT_UPI_TERMINAL_CODE = 10

# Terminal modes — name → Pine Labs Cloud allowed_payment_mode integer.
# The UPI value here is only the default; use get_terminal_code() so the
# Settings override is honoured.
PINELABS_TERMINAL_MODES = {
	"PineLabs - Card": CARD_TERMINAL_CODE,
	"PineLabs - UPI": DEFAULT_UPI_TERMINAL_CODE,
}


def get_upi_terminal_code():
	"""AllowedPaymentMode to send for UPI, from Pinelabs Settings (default 10)."""
	code = cint(frappe.db.get_single_value("Pinelabs Settings", "upi_payment_mode_code"))
	return code if code in UPI_TERMINAL_CODES else DEFAULT_UPI_TERMINAL_CODE


def get_terminal_code(mode_of_payment):
	"""Pine Labs AllowedPaymentMode for a terminal Mode of Payment, or None."""
	if mode_of_payment not in PINELABS_TERMINAL_MODES:
		return None
	if mode_of_payment == "PineLabs - UPI":
		return get_upi_terminal_code()
	return PINELABS_TERMINAL_MODES[mode_of_payment]

# Terminal method string (CARD / UPI on the txn) → mode name.
PINELABS_METHOD_TO_MODE = {
	"CARD": "PineLabs - Card",
	"UPI": "PineLabs - UPI",
}

# Terminal mode name → method string (inverse of the map above).
PINELABS_MODE_TO_METHOD = {v: k for k, v in PINELABS_METHOD_TO_MODE.items()}

# Plural / Pay-by-Link uses a single mode covering every method the customer
# can pick on the hosted page.
PINELABS_PLURAL_MODE = "PineLabs - Payment Link"

PINELABS_MODES = (*PINELABS_TERMINAL_MODES, PINELABS_PLURAL_MODE)


def validate_finalization_ready(reference_doctype):
	"""Refuse to start a payment when the app cannot finish it CLEANLY.

	Three valid configurations:
	1. reference_doctype is one of PE_NATIVE_DOCTYPES → ERPNext's
	   ``get_payment_entry`` will build the PE on success. No setup needed.
	2. A Payable Documents row exists with finalize_via_payment_entry = 0 →
	   the payment is recorded on the Pinelabs Transaction but no Payment
	   Entry is posted and the source doc is left untouched (apart from the
	   optional Transaction Reference Field — see
	   ``_write_transaction_reference``).
	3. No Payable Documents row at all → permissive: the payment will
	   proceed and the Pinelabs Transaction will record SUCCESS, but the
	   source doc is **not** auto-updated. The caller (custom button,
	   external system) is responsible for reacting to the
	   ``pinelabs_txn_update`` realtime event or polling the Pinelabs
	   Transaction status if they want the source doc to reflect payment.

	The only case that still throws is (4) — a non-PE-native doctype whose
	Payable Documents row has 'Create Payment Entry' ticked. ERPNext's
	builder can't handle it, so finalization would fail *after* the customer
	paid. That's an admin-side error worth surfacing loudly beforehand.
	"""
	if reference_doctype in PE_NATIVE_DOCTYPES:
		return

	config = _get_payable_config(reference_doctype)
	if not config:
		# Permissive — see docstring case (3). Payment proceeds; source
		# doc is not auto-finalized.
		return

	if cint(config.get("finalize_via_payment_entry", 1)):
		frappe.throw(
			_("Cannot accept Pine Labs payments on {0}: it is not Sales Invoice / POS Invoice / Payment Request, but its Payable Documents row has 'Create Payment Entry' ticked. ERPNext's Payment Entry builder does not support {0}, so finalization would fail after the customer paid. Fix, pick one: (a) open the row in Pinelabs Settings → Payable Documents and untick 'Create Payment Entry' — the payment is still recorded, no Payment Entry is posted; (b) delete the row entirely to use the permissive path; or (c) call the API with create_payment_entry=1, which builds a standalone Payment Entry for this doctype (it needs a Customer and a Company to resolve).").format(reference_doctype),
		)


# ──────────────────────────────────────────────────────────────────────────
# Creation
# ──────────────────────────────────────────────────────────────────────────


def create_transaction(
	reference_doctype,
	reference_name,
	flow_type,
	amount,
	*,
	payment_method=None,
	machine=None,
	order_id=None,
	request_payload=None,
	create_payment_entry=None,
	client_request_id=None,
):
	"""Insert a new Pinelabs Transaction in INITIATED state.

	Caller must have already validated eligibility (docstatus, outstanding,
	etc.) — this function only enforces structural invariants.

	``create_payment_entry`` is the API caller's optional per-transaction
	override. ``1`` / ``True`` / ``"Yes"`` → finalize will always create a PE;
	``0`` / ``False`` / ``"No"`` → finalize will always skip PE creation
	(the payment is recorded, but nothing is posted to the ledger). ``None``
	→ use the Payable Documents row's setting.
	"""
	if flow_type not in VALID_FLOW_TYPES:
		frappe.throw(
			_("Invalid flow_type {0}. Expected one of {1}.").format(flow_type, ", ".join(VALID_FLOW_TYPES)),
		)
	if not (reference_doctype and reference_name):
		frappe.throw(_("reference_doctype and reference_name are required."))
	if flt(amount) <= 0:
		frappe.throw(_("Transaction amount must be greater than zero."))

	doc = frappe.new_doc("Pinelabs Transaction")
	doc.reference_doctype = reference_doctype
	doc.reference_name = reference_name
	doc.flow_type = flow_type
	doc.payment_method = payment_method
	doc.amount = flt(amount)
	doc.machine = machine
	doc.order_id = order_id
	doc.created_at = now_datetime()
	doc.status = "INITIATED"
	override = normalize_create_payment_entry(create_payment_entry)
	if override:
		doc.create_payment_entry_override = override
	if client_request_id:
		doc.client_request_id = client_request_id
	if request_payload is not None:
		doc.request_payload = _to_json_str(request_payload)

	doc.insert(ignore_permissions=True)
	return doc


def lock_source_for_initiate(reference_doctype, reference_name):
	"""Row-lock the source document so concurrent initiate calls serialize.

	``get_active_for_doc`` + ``create_transaction`` is a check-then-act with no
	lock: two requests can both see "no active transaction" and both create one,
	producing two charges for the same doc. Taking ``SELECT … FOR UPDATE`` on the
	source doc row here (before the active-transaction check) forces the second
	caller to block until the first commits; it then sees the first's active
	transaction and is refused. Same ``for_update`` mechanism as
	``_lock_and_reload``. No-op when the reference is incomplete.
	"""
	if reference_doctype and reference_name:
		frappe.db.get_value(reference_doctype, reference_name, "name", for_update=True)


def get_active_for_doc(reference_doctype, reference_name, for_update=False):
	"""Return the open (INITIATED/PENDING) transaction for a source doc, or None.

	Used by the JS button-handlers to detect "already in flight" before
	starting a new attempt — prevents duplicate spends from rapid clicks.

	``for_update=True`` reads the LATEST committed rows (bypassing this request's
	REPEATABLE-READ snapshot). The initiate path uses it *after*
	``lock_source_for_initiate`` so the second, just-unblocked caller sees the
	first caller's freshly-committed transaction and is refused — a plain read
	would still see the pre-lock snapshot and miss it.
	"""
	if not (reference_doctype and reference_name):
		return None
	# Fetch name + flow_type in the one (optionally FOR UPDATE) read. Loading the
	# full doc with a second get_doc would read this request's REPEATABLE-READ
	# snapshot and miss a row the FOR UPDATE query just saw committed.
	return frappe.db.get_value(
		"Pinelabs Transaction",
		{
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": ["in", ACTIVE_STATUSES],
		},
		["name", "flow_type"],
		order_by="creation desc",
		for_update=for_update,
		as_dict=True,
	)


def get_active_for_machine(machine_name, machine_row=None):
	"""Return the open (INITIATED/PENDING) Terminal transaction on a machine, or None.

	Pine Labs terminals take one transaction at a time: in production the
	previous one must be completed or cancelled before a new upload. This is
	checked across ALL source docs (any invoice / POS sale on that machine).

	``machine_row`` (the Pinelabs Machine Config child row name) is row-locked
	first so two concurrent initiates on the same machine serialize — same
	lock-then-fresh-read pattern as lock_source_for_initiate + get_active_for_doc.
	"""
	if not machine_name:
		return None
	if machine_row:
		frappe.db.get_value("Pinelabs Machine Config", machine_row, "name", for_update=True)
	return frappe.db.get_value(
		"Pinelabs Transaction",
		{
			"flow_type": "Terminal",
			"machine": machine_name,
			"status": ["in", ACTIVE_STATUSES],
		},
		["name", "amount", "reference_doctype", "reference_name", "creation"],
		order_by="creation desc",
		for_update=True,
		as_dict=True,
	)


# ──────────────────────────────────────────────────────────────────────────
# Status transitions
# ──────────────────────────────────────────────────────────────────────────


def mark_pending(transaction, *, order_id=None, payment_id=None, response_payload=None, request_payload=None):
	"""Move a transaction from INITIATED to PENDING.

	Called right after the gateway accepts the request but before the
	customer has actually paid. For the Terminal flow, callers must pass
	`payment_id` here (the Plutus reference) — `_reload` would otherwise
	drop any in-memory change set on the doc before this call.
	"""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "PENDING":
		# Another worker raced ahead and already moved us here.
		return transaction
	if transaction.status != "INITIATED":
		frappe.throw(
			_("Cannot mark PENDING from status {0}.").format(transaction.status),
		)

	updates = {"status": "PENDING"}
	if order_id:
		updates["order_id"] = order_id
	if payment_id:
		updates["payment_id"] = payment_id
	if response_payload is not None:
		updates["response_payload"] = _to_json_str(response_payload)
	if request_payload is not None:
		updates["request_payload"] = _to_json_str(request_payload)

	transaction.flags.pinelabs_internal_transition = True
	transaction.update(updates)
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def mark_success(transaction, *, payment_id, payment_method=None, response_payload=None, request_payload=None):
	"""Mark transaction SUCCESS and create the Payment Entry idempotently.

	This is the ONLY path that creates Payment Entries. Wraps the PE creation
	in a try/except so that a failure leaves the transaction in its pre-call
	state (PENDING or INITIATED) for retry.
	"""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "SUCCESS":
		# Idempotent — caller is replaying a webhook or polling result.
		return transaction
	if transaction.status in ("FAILED", "CANCELLED"):
		frappe.throw(
			_("Cannot mark SUCCESS from terminal status {0}.").format(transaction.status),
		)
	if not payment_id:
		frappe.throw(_("payment_id is required to mark SUCCESS."))

	if payment_method:
		transaction.payment_method = payment_method
	transaction.payment_id = payment_id
	if response_payload is not None:
		transaction.response_payload = _to_json_str(response_payload)
	if request_payload is not None:
		transaction.request_payload = _to_json_str(request_payload)
	transaction.completed_at = now_datetime()

	mode_of_payment = _resolve_mode_of_payment(transaction)
	transaction.mode_of_payment = mode_of_payment

	# Wrap PE creation + status flip in a savepoint so partial failures
	# don't leave the transaction "successful but no PE".
	savepoint = "pinelabs_mark_success"
	frappe.db.savepoint(savepoint)
	try:
		pe_name = _create_payment_entry_idempotent(transaction)
		transaction.payment_entry = pe_name
		# Optional write-back onto the source doc, independent of whether a
		# Payment Entry was posted. Single call site so every finalization
		# path (PE-native, custom-doctype PE, and no-PE) behaves the same —
		# previously this only happened on the now-removed status-mapping
		# path, so a configured reference field was never filled when a
		# Payment Entry was created.
		_write_transaction_reference(transaction)
		transaction.status = "SUCCESS"
		transaction.flags.pinelabs_internal_transition = True
		transaction.save(ignore_permissions=True)
		# Durability boundary: the customer has ALREADY been charged by Pine
		# Labs at this point. The PE + status flip must survive even if a
		# later step in the same request (or the caller — webhook/cron) errors
		# out; otherwise the money is taken but the transaction looks unpaid.
		# The savepoint above scopes a clean rollback if PE creation itself
		# fails, so this commit only persists a fully consistent SUCCESS state.
		frappe.db.commit()  # nosemgrep: frappe-manual-commit
	except Exception:
		# MariaDB auto-invalidates the savepoint when an inner statement
		# fails (e.g. an Error Log insert tripping a length validation).
		# In that case ROLLBACK TO SAVEPOINT itself raises
		# "SAVEPOINT ... does not exist", masking the real error. Swallow
		# that secondary failure — the outer try/except in the request
		# handler will still see the original exception we're about to
		# re-raise.
		try:
			frappe.db.rollback(save_point=savepoint)
		except Exception:
			pass
		frappe.log_error(
			title=f"Pinelabs mark_success failed for {transaction.name}",
			message=frappe.get_traceback(),
		)
		raise

	_publish_status_update(transaction)
	return transaction


def mark_failed(transaction, *, error_message=None, response_payload=None, request_payload=None):
	"""Move a transaction to FAILED. Idempotent."""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "FAILED":
		return transaction
	if transaction.status in ("SUCCESS", "CANCELLED"):
		frappe.throw(
			_("Cannot mark FAILED from terminal status {0}.").format(transaction.status),
		)

	transaction.status = "FAILED"
	transaction.completed_at = now_datetime()
	if error_message:
		transaction.error_message = _truncate_with_ellipsis(error_message, 1000)
	if response_payload is not None:
		transaction.response_payload = _to_json_str(response_payload)
	if request_payload is not None:
		transaction.request_payload = _to_json_str(request_payload)
	transaction.flags.pinelabs_internal_transition = True
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def mark_cancelled(transaction, *, reason=None):
	"""Move a transaction to CANCELLED. Idempotent."""
	transaction = _lock_and_reload(transaction)
	if transaction.status == "CANCELLED":
		return transaction
	if transaction.status in ("SUCCESS", "FAILED"):
		frappe.throw(
			_("Cannot mark CANCELLED from terminal status {0}.").format(transaction.status),
		)

	transaction.status = "CANCELLED"
	transaction.completed_at = now_datetime()
	if reason:
		transaction.error_message = _truncate_with_ellipsis(reason, 1000)
	transaction.flags.pinelabs_internal_transition = True
	transaction.save(ignore_permissions=True)
	_publish_status_update(transaction)
	return transaction


def _publish_status_update(transaction):
	"""Push a status change to the browser via Frappe's realtime channel.

	The Pay-on-Machine modal subscribes to `pinelabs_txn_update` events on
	the user's socket and reacts when the transaction it's watching reaches
	a terminal state. Server-side callers (the cron reconcile task, webhook,
	and the foreground request) all flow through this function so any state
	transition lands on every open dialog watching that transaction.

	`after_commit=True` means the event fires only after the DB commit, so
	the browser never sees a status the database hasn't durably persisted.
	"""
	try:
		payload = {
			"transaction_name": transaction.name,
			"reference_doctype": transaction.reference_doctype,
			"reference_name": transaction.reference_name,
			"flow_type": transaction.flow_type,
			"status": transaction.status,
			"payment_id": transaction.payment_id,
			"payment_entry": transaction.payment_entry,
			"error_message": transaction.error_message,
		}
		frappe.logger("pinelabs").info(
			f"[Pinelabs DEBUG] publish_realtime pinelabs_txn_update "
			f"user={transaction.owner} session_user={frappe.session.user} "
			f"payload={payload}"
		)
		frappe.publish_realtime(
			event="pinelabs_txn_update",
			message=payload,
			user=transaction.owner,
			after_commit=True,
		)
	except Exception:
		# Realtime is best-effort. Browser polling fallback still reconciles.
		frappe.log_error(
			frappe.get_traceback(),
			f"Pinelabs realtime publish failed for {transaction.name}",
		)


# ──────────────────────────────────────────────────────────────────────────
# Mode of Payment lookup
# ──────────────────────────────────────────────────────────────────────────


def _resolve_mode_of_payment(transaction):
	"""Return the canonical Pine Labs Mode of Payment for this transaction.

	Pine Labs modes are identified by **name**, not by custom fields. The
	three canonical names (Card, UPI, Payment Link) are seeded by
	``install_pinelabs_modes.py``. Renaming them in the desk breaks the
	integration — they are part of the install contract.

	- For Terminal: payment_method "CARD" → "PineLabs - Card", "UPI" →
	  "PineLabs - UPI".
	- For Plural: a single ``PineLabs - Payment Link`` mode covers the whole
	  flow regardless of what the customer ended up paying with on the
	  hosted page. The customer's real method is still captured on
	  ``Pinelabs Transaction.payment_method`` and on the resulting
	  Payment Entry's ``reference_no`` (= Plural ``payment_id``).
	"""
	flow = transaction.flow_type
	method = (transaction.payment_method or "").upper()

	if flow == "Terminal":
		name = PINELABS_METHOD_TO_MODE.get(method)
		if not name:
			frappe.throw(_("Unknown terminal payment_method {0}.").format(method or "?"))
		if not frappe.db.exists("Mode of Payment", name):
			frappe.throw(
				_(
					"Mode of Payment '{0}' is missing. Run "
					"`bench --site <site> migrate` to seed the canonical Pine Labs modes."
				).format(name),
			)
		return name

	# Plural — single mode covers all hosted-link payments.
	return get_plural_mode_of_payment()


def get_plural_mode_of_payment():
	"""Return the canonical Mode of Payment used for every Plural payment.

	Centralised so the initiate-time account-mapping pre-flight and the
	finalize-time PE creation can't drift apart.
	"""
	name = PINELABS_PLURAL_MODE
	if not frappe.db.exists("Mode of Payment", name):
		frappe.throw(
			_(
				"Mode of Payment '{0}' is missing. Run "
				"`bench --site <site> migrate` to seed it."
			).format(name),
		)
	return name


def normalize_create_payment_entry(value):
	"""Coerce an API ``create_payment_entry`` argument into ``"Yes"`` / ``"No"`` / ``None``.

	The wire form accepts any of: ``None`` (or unset), ``0``, ``1``, ``"0"``,
	``"1"``, ``True``, ``False``, ``"Yes"``, ``"No"``. Anything else falls
	through to ``None`` (= "inherit from Payable Documents config").
	"""
	if value is None or value == "":
		return None
	if value in (1, "1", True, "Yes", "yes", "y", "Y", "true", "True"):
		return "Yes"
	if value in (0, "0", False, "No", "no", "n", "N", "false", "False"):
		return "No"
	return None


def will_create_payment_entry(reference_doctype, override=None):
	"""Return True iff the configured finalization path will produce a Payment Entry.

	``override`` is the API caller's explicit per-call decision (already
	normalized to ``"Yes"`` / ``"No"`` / ``None`` via
	``normalize_create_payment_entry``). When set, it wins over any
	Payable Documents row. When ``None``, the decision falls back to:

	1. The reference_doctype isn't PE-native AND has no Payable Documents
	   row (permissive — custom-button flow, source doc is not auto-finalized).
	2. The Payable Documents row has ``finalize_via_payment_entry = 0``
	   (the payment is recorded; no Payment Entry is posted).
	3. (Implicitly) misconfigured rows — ``validate_finalization_ready``
	   throws on those before we ever get here.

	The account-mapping pre-flight only matters when a PE is going to be
	posted, so it should be gated on this.
	"""
	if override == "Yes":
		return True
	if override == "No":
		return False

	config = _get_payable_config(reference_doctype)
	if config and not cint(config.get("finalize_via_payment_entry", 1)):
		return False  # row says: record the payment, post no Payment Entry
	if reference_doctype in PE_NATIVE_DOCTYPES:
		return True
	if not config:
		return False  # permissive — custom button + no PE
	# Non-PE-native doctype with finalize_via_pe=1 — validate_finalization_ready
	# refuses this combination, so we never actually finalize via PE here.
	return True


def assert_mode_has_account_mapped(mode_of_payment, company, *, reference_doctype=None, reference_name=None, create_payment_entry=None):
	"""Throw if ``Mode of Payment → Accounts`` has no row for this company.

	ERPNext's ``get_payment_entry`` won't be able to post the credit leg of
	the PE without the account mapping, but the failure surfaces deep inside
	``mark_success`` — long after the customer has paid. Call this BEFORE
	the request hits Pine Labs / Plural so the operator gets a clean,
	actionable error and the customer is never charged for a payment we
	can't finalize.

	The check is skipped when the configured finalization path will NOT
	create a Payment Entry (Create Payment Entry unticked, or the permissive
	custom-button path on a non-PE-native doctype). In those cases the
	Mode of Payment is informational only — no account is posted to.

	When the caller explicitly opted in to PE creation (``create_payment_entry
	= 1``) on a custom doctype that has no ``company`` field, the company is
	resolved via ERPNext's defaults — mirroring the resolution chain in
	``_build_custom_doctype_pe`` so this pre-flight catches missing account
	mappings the finalize-time builder would also reject.
	"""
	override = normalize_create_payment_entry(create_payment_entry)

	if reference_doctype is not None and not will_create_payment_entry(
		reference_doctype, override=override
	):
		# Mapped-field or permissive path — no PE will be posted, so the
		# Mode of Payment's account doesn't need to be configured.
		return

	if not company and override == "Yes":
		# Custom doctype with no company field — match the finalize-time
		# builder's fallback chain so we catch the same misconfig now.
		company = (
			frappe.defaults.get_user_default("company")
			or frappe.defaults.get_global_default("company")
		)

	if not (mode_of_payment and company):
		# Caller didn't provide enough context to check — better to skip
		# than to throw a misleading error. The finalize-time path will
		# still surface any real misconfig.
		return

	account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": mode_of_payment, "company": company},
		"default_account",
	)
	if not account:
		frappe.throw(
			_(
				"Mode of Payment <b>{0}</b> has no default account configured for "
				"<b>{1}</b>. Open Mode of Payment → {0} → Accounts and add a row "
				"with Company = {1} and a Default Account before taking this payment."
			).format(mode_of_payment, company),
		)

	# When override=Yes on a non-PE-native doctype, also verify a Customer
	# party can be resolved — ERPNext's Payment Entry rejects "Receive" PEs
	# without one. Catching this here prevents the customer from getting
	# stuck at "Waiting for terminal" only for the PE creation to fail with
	# "Party Type is mandatory" after they've paid.
	if (
		override == "Yes"
		and reference_doctype
		and reference_doctype not in PE_NATIVE_DOCTYPES
	):
		source_doc = None
		if reference_name and frappe.db.exists(reference_doctype, reference_name):
			source_doc = frappe.get_doc(reference_doctype, reference_name)
		party = _resolve_pe_party_for_custom_doctype(source_doc)
		if not party:
			frappe.throw(
				_(
					"Cannot create a Payment Entry for {0}: no Customer can be "
					"resolved. Add a 'customer' Link field to {0}, or set the "
					"Default Customer in Global Defaults, or call the API with "
					"create_payment_entry=0."
				).format(reference_doctype),
			)


# ──────────────────────────────────────────────────────────────────────────
# Payment Entry creation
# ──────────────────────────────────────────────────────────────────────────


def _create_payment_entry_idempotent(transaction):
	"""Finalize the transaction. Returns the PE name when one is created, or
	None when the configured doctype is set to post no Payment Entry.

	Idempotent against:
	- A pre-existing PE with the same reference_no (Plan §8 rule 2).
	- The transaction already having a linked PE (Plan §8 rule 3).
	"""
	# Rule 3: transaction already linked → return that linked PE.
	if transaction.payment_entry:
		return transaction.payment_entry

	# Per-doctype config decides whether a PE is posted — unless the
	# Pinelabs Transaction has an explicit override stored from initiate time
	# (set by the API caller passing create_payment_entry=0/1).
	config = _get_payable_config(transaction.reference_doctype)
	override = (getattr(transaction, "create_payment_entry_override", None) or "").strip()
	if override == "Yes":
		finalize_via_pe = 1
	elif override == "No":
		finalize_via_pe = 0
	else:
		finalize_via_pe = cint((config or {}).get("finalize_via_payment_entry", 1))

	if not finalize_via_pe:
		# Either the Payable Documents row or the API caller said "no PE".
		# The payment is still recorded on the Pinelabs Transaction; the
		# source doc is left untouched apart from the optional reference
		# field, which mark_success writes for every path.
		return None

	# Rule 2: PE with same reference_no exists → reuse it.
	# Restrict to non-cancelled PEs (docstatus 0 or 1) so a previously
	# cancelled PE pointing at the same Pine Labs payment doesn't block
	# a legitimate retry.
	existing = frappe.db.get_value(
		"Payment Entry",
		{
			"reference_no": transaction.payment_id,
			"docstatus": ["<", 2],
		},
		"name",
	)
	if existing:
		return existing

	# Rule 4: standard ERPNext PE builder.
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if transaction.reference_doctype not in PE_NATIVE_DOCTYPES:
		# Three sub-cases:
		# 1. Explicit ``create_payment_entry = 1`` from the API caller →
		#    build a standalone PE since ERPNext's get_payment_entry only
		#    supports SI/POSI/PR. Lenient on company / party resolution so
		#    arbitrary custom doctypes can work.
		# 2. No Payable Documents row and no override → permissive path.
		#    The caller chose to skip configuration; mark the Pinelabs
		#    Transaction as SUCCESS without touching the source doc. They
		#    can react to the pinelabs_txn_update realtime event if they
		#    want their source doc updated.
		# 3. Row exists with finalize_via_payment_entry = 1 and no override
		#    (misconfig). validate_finalization_ready should have caught
		#    this at click time. Reaching here means a code path bypassed
		#    it. Throw with the actionable message.
		if override == "Yes":
			return _build_custom_doctype_pe(transaction)
		if not config:
			return None
		frappe.throw(
			_("PE creation supports Sales Invoice / POS Invoice / Payment Request only. Got {0}. Either add support or set Create Payment Entry to 0 on this doctype's Payable Doctypes row.").format(
				transaction.reference_doctype
			),
		)

	source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)

	# Auto-submit drafts. The customer has paid — the corresponding
	# Sales / POS Invoice now needs to be a real, submitted ledger row so a
	# Payment Entry can attach to it. Drafts also report outstanding_amount=0
	# which would falsely trip the conflict guard below. We submit inside
	# mark_success's savepoint, so if anything downstream rolls back, the
	# submission rolls back with it (no orphaned ledger rows).
	if cint(getattr(source, "docstatus", 0)) == 0:
		source.flags.ignore_permissions = True
		source.submit()
		# Refetch so outstanding_amount and friends reflect the post-submit
		# state ERPNext just computed.
		source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)

	# Conflict guard — source doc already fully paid by something else?
	#
	# If a cashier (or any other path) recorded a Payment Entry manually
	# while our Pine Labs payment was in flight, the source doc's
	# Outstanding will already be 0 by the time we get here. ERPNext's
	# get_payment_entry would throw in that case ("nothing to pay"),
	# the savepoint in mark_success would roll back, and the Pinelabs
	# Transaction would stay PENDING — with the money already debited
	# from the customer on the Pine Labs side.
	#
	# Instead, we detect the conflict, write a clear note onto the
	# transaction, and return None. mark_success will then complete the
	# transaction as SUCCESS (because the customer DID pay) but leave
	# payment_entry empty and error_message populated. The operator
	# sees the conflict on the Pinelabs Transaction record and can do
	# manual reconciliation.
	outstanding = flt(getattr(source, "outstanding_amount", 0) or 0)
	if outstanding <= 0:
		conflict_msg = (
			f"Customer paid {flt(transaction.amount):.2f} via Pine Labs "
			f"(payment_id {transaction.payment_id}), but {transaction.reference_doctype} "
			f"{transaction.reference_name} was already fully paid by another "
			f"Payment Entry. Manual reconciliation required — either refund the "
			f"Pine Labs charge or cancel the conflicting Payment Entry and re-run "
			f"Refresh Status on this transaction."
		)
		transaction.error_message = _truncate_with_ellipsis(conflict_msg, 1000)
		# Frappe's log_error(title, message) auto-swaps args only when the
		# title contains a newline (treats it as a traceback). Our
		# conflict message is single-line and >140 chars, so without
		# explicit kwargs it lands in the Title field and trips Error Log's
		# length validation — which would otherwise propagate up and break
		# mark_success's savepoint rollback (MariaDB invalidates the
		# savepoint when an inner statement fails). Be explicit.
		frappe.log_error(title="Pinelabs PE conflict", message=conflict_msg)
		return None

	pe = get_payment_entry(transaction.reference_doctype, transaction.reference_name)

	# Rule 5: explicit fields.
	pe.mode_of_payment = transaction.mode_of_payment
	pe.paid_amount = flt(transaction.amount)
	pe.received_amount = flt(transaction.amount)
	pe.reference_no = transaction.payment_id
	pe.reference_date = (transaction.completed_at or now_datetime())
	pe.remarks = (
		f"Pine Labs {transaction.flow_type} payment "
		f"({transaction.payment_method or 'unknown method'}) "
		f"for {transaction.reference_doctype} {transaction.reference_name}. "
		f"Pinelabs Transaction: {transaction.name}."
	)

	# Bypass flag for conflict_guard.refuse_if_pinelabs_active — without
	# this our own PE insertion would trip the validate hook because
	# THIS Pinelabs Transaction is still in PENDING at this exact moment.
	pe.flags.pinelabs_internal_pe = True
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _resolve_pe_party_for_custom_doctype(source_doc):
	"""Resolve a Customer party for a non-PE-native source doctype.

	Used by both the initiate-time pre-flight (so the misconfig is caught
	before the gateway is contacted) and the finalize-time builder (so a
	PE creation that does reach finalize doesn't crash on "Party Type is
	mandatory"). Keeping a single resolution chain prevents the two paths
	from disagreeing.

	Resolution order:
	  - ``source_doc.customer`` (most custom doctypes use this name)
	  - ``source_doc.party`` (generic)
	  - user's default Customer
	  - global default Customer

	Returns the Customer name, or None when nothing resolves.
	"""
	if source_doc is not None:
		party = getattr(source_doc, "customer", None) or getattr(source_doc, "party", None)
		if party:
			return party
	return (
		frappe.defaults.get_user_default("customer")
		or frappe.defaults.get_global_default("customer")
	)


def _build_custom_doctype_pe(transaction):
	"""Build a Payment Entry for a non-PE-native source doctype.

	Used only when the API caller explicitly opted in via
	``create_payment_entry = 1``. ERPNext's ``get_payment_entry`` helper
	only knows how to build PEs against Sales Invoice / POS Invoice /
	Payment Request; for any other doctype this function constructs the
	PE manually with sensible fallbacks for the fields the helper would
	have filled in.

	Resolution order:

	- ``company``: source doc field → user default → global default.
	- ``party``: source_doc.customer / source_doc.party → no party.
	- ``paid_to`` (cash/bank): Mode of Payment Account row for this company
	  (already enforced by the initiate-time pre-flight when override = Yes).
	- ``paid_from``: company's default receivable (when party set) →
	  any Receivable account → Temporary account → any Income account.

	The PE produced has no entry in ``references`` (the source doctype
	may not be allowed there); identification flows through
	``reference_no = transaction.payment_id`` and the back-link on the
	Pinelabs Transaction record.
	"""
	source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)

	company = (
		getattr(source, "company", None)
		or frappe.defaults.get_user_default("company")
		or frappe.defaults.get_global_default("company")
	)
	if not company:
		frappe.throw(
			_(
				"Cannot create a Payment Entry for {0} {1}: no Company resolved. "
				"Either add a 'company' field to {0}, set the Default Company in "
				"Global Defaults, or call the API with create_payment_entry=0."
			).format(transaction.reference_doctype, transaction.reference_name),
		)

	mode_account = frappe.db.get_value(
		"Mode of Payment Account",
		{"parent": transaction.mode_of_payment, "company": company},
		"default_account",
	)
	if not mode_account:
		frappe.throw(
			_(
				"Mode of Payment {0} has no default account configured for "
				"Company {1}. Open Mode of Payment → {0} → Accounts and add a "
				"row before retrying."
			).format(transaction.mode_of_payment, company),
		)

	party = _resolve_pe_party_for_custom_doctype(source)
	if not party:
		frappe.throw(
			_(
				"Cannot create a Payment Entry for {0} {1}: no Customer resolved. "
				"ERPNext's Payment Entry requires a Customer / party_type for "
				"'Receive' payments. Add a 'customer' Link field to {0}, or set "
				"the Default Customer in Global Defaults, or call the API with "
				"create_payment_entry=0."
			).format(transaction.reference_doctype, transaction.reference_name),
		)

	paid_from = None
	if party:
		paid_from = frappe.db.get_value("Company", company, "default_receivable_account")
		if not paid_from:
			paid_from = frappe.db.get_value(
				"Account",
				{"company": company, "account_type": "Receivable", "is_group": 0},
				"name",
			)

	if not paid_from:
		paid_from = (
			frappe.db.get_value(
				"Account",
				{"company": company, "account_type": "Temporary", "is_group": 0},
				"name",
			)
			or frappe.db.get_value(
				"Account",
				{"company": company, "root_type": "Income", "is_group": 0},
				"name",
			)
		)

	if not paid_from:
		frappe.throw(
			_(
				"Could not resolve a 'Paid From' account for Company {0}. Configure "
				"a Default Receivable Account on the Company, or add a Customer "
				"link to {1}, or call the API with create_payment_entry=0."
			).format(company, transaction.reference_doctype),
		)

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Receive"
	pe.company = company
	pe.posting_date = frappe.utils.nowdate()
	pe.mode_of_payment = transaction.mode_of_payment
	pe.paid_amount = flt(transaction.amount)
	pe.received_amount = flt(transaction.amount)
	pe.source_exchange_rate = 1
	pe.target_exchange_rate = 1
	pe.paid_from = paid_from
	pe.paid_to = mode_account
	pe.reference_no = transaction.payment_id
	pe.reference_date = (transaction.completed_at or now_datetime())
	pe.remarks = (
		f"Pine Labs {transaction.flow_type} payment "
		f"({transaction.payment_method or 'unknown method'}) "
		f"for {transaction.reference_doctype} {transaction.reference_name}. "
		f"Pinelabs Transaction: {transaction.name}."
	)
	if party:
		pe.party_type = "Customer"
		pe.party = party

	# Same bypass flag as the PE-native path so conflict_guard doesn't block
	# our own insertion while THIS Pinelabs Transaction is still PENDING.
	pe.flags.pinelabs_internal_pe = True
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	return pe.name


def _get_payable_config(reference_doctype):
	"""Return the Payable Doctype config dict for the given source doctype, or None."""
	if not reference_doctype:
		return None
	try:
		settings = frappe.get_single("Pinelabs Settings")
	except Exception:
		return None
	for row in (settings.payable_doctypes or []):
		if row.doctype_name != reference_doctype:
			continue
		return {
			"doctype_name": row.doctype_name,
			"is_enabled": cint(row.is_enabled),
			# Per-flow toggles. Read by api/access.py so switching one off in
			# Settings actually refuses the API call, not just hides the button.
			"enable_pay_on_machine": cint(getattr(row, "enable_pay_on_machine", 1) or 0),
			"enable_send_payment_link": cint(getattr(row, "enable_send_payment_link", 1) or 0),
			"show_on_docstatus": row.show_on_docstatus,
			"amount_field": row.amount_field,
			"link_payment_entry_field": row.link_payment_entry_field,
			"finalize_via_payment_entry": cint(getattr(row, "finalize_via_payment_entry", 1) or 0),
		}
	return None


def resolve_payment_amount(source_doc):
	"""Return the amount to charge for ``source_doc``, honouring Settings.

	Precedence:

	1. The ``Amount Field`` configured on the doctype's Payable Documents row.
	   Settings *requires* this field to be filled, but until now nothing ever
	   read it — both flows hardcoded the fallback chain below, so a site that
	   configured ``Amount Field = custom_part_payment`` still charged the full
	   ``grand_total``. That silent override is the bug this fixes.
	2. ``outstanding_amount`` on a submitted doc — authoritative INCLUDING when
	   it is 0. Returning 0 makes the caller refuse with "No outstanding
	   amount"; the document-total fallback must not apply here or an
	   already-settled invoice would be charged in full a second time.
	3. ``rounded_total`` / ``grand_total`` / ``amount`` — for drafts (whose
	   ``outstanding_amount`` is only computed at submit) and for submittable
	   doctypes that have no ``outstanding_amount`` field at all.

	The settled-document guard applies to the configured field too: a
	submitted doc whose ``outstanding_amount`` has reached 0 is refused
	regardless of what the configured field still reads, so honouring the
	setting can never re-open the double-charge hole. Partial amounts BELOW
	the outstanding balance are allowed — that is the point of the setting.
	"""
	is_settled = _is_settled(source_doc)

	config = _get_payable_config(getattr(source_doc, "doctype", None))
	amount_field = ((config or {}).get("amount_field") or "").strip()
	if amount_field:
		val = getattr(source_doc, amount_field, None)
		if val is not None and flt(val) > 0:
			# Never let a configured field resurrect a fully-paid document.
			return 0.0 if is_settled else flt(val)

	if cint(getattr(source_doc, "docstatus", 0)) == 1:
		outstanding = getattr(source_doc, "outstanding_amount", None)
		if outstanding is not None:
			return flt(outstanding) if flt(outstanding) > 0 else 0.0

	for field in ("rounded_total", "grand_total", "amount"):
		val = getattr(source_doc, field, None)
		if val is not None and flt(val) > 0:
			return flt(val)
	return 0.0


def _is_settled(source_doc):
	"""True when a submitted doc reports a non-positive outstanding balance."""
	if cint(getattr(source_doc, "docstatus", 0)) != 1:
		return False
	outstanding = getattr(source_doc, "outstanding_amount", None)
	return outstanding is not None and flt(outstanding) <= 0


def _write_transaction_reference(transaction):
	"""Write this transaction's name onto the source doc's reference field.

	Optional, per-doctype: the ``Transaction Reference Field`` on the Payable
	Documents row names a field on the source doctype that receives the
	Pinelabs Transaction id (``PLT-…``) once the payment succeeds. Blank (the
	default) means "don't write anything".

	Called from ``mark_success`` for EVERY finalization path — Payment Entry
	or not. It used to live inside the status-mapping path, which meant a
	configured reference field was silently never filled whenever a Payment
	Entry was created (i.e. the common case).

	Failures here are logged, not raised. The customer has already been
	charged and the Payment Entry may already be posted; a bad field name in
	config must not roll the whole SUCCESS back. ``db_set`` is used rather
	than ``save`` so a submitted source doc can be updated without tripping
	Frappe's update-after-submit validation.
	"""
	config = _get_payable_config(transaction.reference_doctype)
	link_field = ((config or {}).get("link_payment_entry_field") or "").strip()
	if not link_field:
		return

	try:
		meta = frappe.get_meta(transaction.reference_doctype)
		if not meta.has_field(link_field):
			frappe.log_error(
				title="Pinelabs reference field missing",
				message=(
					f"Payable Documents row for {transaction.reference_doctype} names "
					f"Transaction Reference Field '{link_field}', but that field does not "
					f"exist on the doctype. Payment {transaction.name} succeeded; the "
					f"reference was not written."
				),
			)
			return

		source = frappe.get_doc(transaction.reference_doctype, transaction.reference_name)
		source.flags.ignore_permissions = True
		source.db_set(link_field, transaction.name, update_modified=True, notify=True)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Pinelabs reference write failed for {transaction.name}",
		)


# ──────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────


def _reload(transaction):
	"""Accept either a doc or a name; return a fresh doc."""
	if isinstance(transaction, str):
		return frappe.get_doc("Pinelabs Transaction", transaction)
	return frappe.get_doc("Pinelabs Transaction", transaction.name)


def _lock_and_reload(transaction):
	"""Take a row-level lock on the transaction, then return a fresh doc.

	The foreground poll, the realtime-driven cron reconcile, and the webhook
	can all reach a transition function concurrently. Without locking, two
	workers can both pass the "is this terminal?" check before either commits,
	and the loser hits TimestampMismatchError on save. SELECT … FOR UPDATE
	serializes them: the second caller blocks until the first commits, then
	loads the doc with the new (terminal) status and the idempotency check
	at the top of each transition returns early.

	REPEATABLE-READ caveat: under MariaDB's default isolation, a regular
	SELECT after the FOR UPDATE still reads from the transaction snapshot
	(the view captured at the request's first SELECT). That snapshot pre-dates
	the other writer's commit, so ``frappe.get_doc`` returns a doc with the
	stale ``modified`` timestamp — and the subsequent ``save()`` fails the
	optimistic-lock check ("Document has been modified after you have opened
	it…"). To bypass the snapshot for the timestamp check, re-read
	``modified`` with FOR UPDATE (which always reads the latest committed
	version) and stamp it onto the doc. All non-timestamp columns are still
	read from the snapshot, which is fine — the transition functions overwrite
	the columns they care about and the save uses the corrected timestamp.
	"""
	name = transaction if isinstance(transaction, str) else transaction.name
	# Acquire the row lock + read the latest committed `modified` in one shot.
	# `frappe.db.get_value(..., for_update=True)` does SELECT … FOR UPDATE and
	# bypasses the REPEATABLE-READ snapshot — so the value here reflects what
	# the DB actually has, not what our transaction's snapshot remembers.
	latest_modified = frappe.db.get_value(
		"Pinelabs Transaction", name, "modified", for_update=True,
	)
	doc = frappe.get_doc("Pinelabs Transaction", name)
	# Override the snapshot's stale `modified` with the real latest one so
	# Frappe's optimistic-lock check inside save() passes. The lock itself
	# guarantees no other writer can advance `modified` further until we
	# release it (at request commit).
	if latest_modified is not None:
		doc.modified = latest_modified
		# Also keep the in-doc cached copy in sync — Frappe's check_if_latest
		# reads doc.modified and compares against the DB; without updating
		# this we'd still trip.
		doc._original_modified = str(latest_modified)
	return doc


def _to_json_str(value):
	if isinstance(value, str):
		return value
	try:
		return json.dumps(value, indent=2, default=str)
	except Exception:
		return str(value)


def _truncate_with_ellipsis(text, max_length):
	"""Truncate text to max_length, appending an ellipsis when something was cut.

	Without the marker, an operator reading a 1000-char error_message has no
	way to know the gateway actually returned more — `response_payload` has
	the full body, but only if they know to look there.
	"""
	if text is None:
		return ""
	text = str(text)
	if len(text) <= max_length:
		return text
	# Leave 1 char of headroom for the ellipsis.
	return text[: max_length - 1] + "…"
