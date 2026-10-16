# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Terminal payment flow.

Whitelisted entry points for the "Pay on Machine" button. Each method
takes the source document + selected Mode of Payment, drives the Pine
Labs Cloud API, and updates the Pinelabs Transaction via the helpers
in transaction.py.

State flow:
    initiate → INITIATED              (transaction created, API call sent)
    initiate response is sync:
        ResponseCode 0    → mark_pending (terminal is processing)
        ResponseCode 1/2  → mark_failed (rejected immediately)
    poll → mark_success | mark_pending | mark_failed
    cancel → mark_cancelled

Response shape — every whitelisted method returns a dict with:
    success           bool   — "we know the outcome", NOT "your action succeeded".
                              True even for FAILED/declined states; only
                              False when the call itself could not produce
                              a definitive answer (e.g. missing payment_id
                              on a poll). Callers should branch on `status`,
                              not `success`, to decide what happened.
    transaction_name  str    — the Pinelabs Transaction id (PLT-...).
    status            str    — INITIATED | PENDING | SUCCESS | FAILED | CANCELLED.
    response_code     int    — Pine Labs raw ResponseCode (may be None
                              if no call was made, e.g. cancel of a txn
                              with no payment_id).
    message           str    — human-readable summary, falls back to
                              Pine Labs ResponseMessage when available.

The `status` reflects the txn AFTER the call — callers don't need to reload
from the DB.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, time_diff_in_seconds

from pinelabs_fin.api.pinelabs_client import _debug_print

# Grace added to the gateway's own auto-cancel timeout to get the app-side
# age-out (see _auto_cancel_minutes). The terminal cancels at its timeout and
# the next poll observes that; the foreground dialog polls every ~3s and the
# cron every 60s, so two minutes is ample headroom for the app to see a
# gateway-side cancellation before deciding to force one itself.
AGE_OUT_GRACE_MINUTES = 2


@frappe.whitelist()
def initiate_terminal_payment(
	reference_doctype: str,
	reference_name: str,
	mode_of_payment: str,
	amount: float | None = None,
	create_payment_entry: int | None = None,
	client_request_id: str | None = None,
) -> dict:
	"""Start a terminal payment.

	Returns {success, transaction_name, response_code, message}.
	The frontend should then poll check_terminal_status until terminal.

	``create_payment_entry`` (optional): per-call override of the Payable
	Documents row's Create Payment Entry setting.

	- ``1`` → always create a PE on success (and run the account-mapping
	  pre-flight now)
	- ``0`` → never create a PE; the payment is still recorded on the
	  Pinelabs Transaction and the source doc is left untouched
	- omitted / ``None`` → use the row's setting (current behaviour)
	"""
	from pinelabs_fin.api import access
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client
	from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
		resolve_machine_for_context,
	)

	source_doc = _load_eligible_source(reference_doctype, reference_name)
	# Settings switches + per-doctype row + permission, all BEFORE the gateway
	# is contacted. Previously these were enforced only in the browser, so the
	# endpoint charged cards for doctypes that were switched off and for users
	# with no rights to the document.
	access.assert_initiate_allowed(
		reference_doctype, reference_name, flow="Terminal", source_doc=source_doc
	)
	# Fail fast BEFORE we contact Pine Labs — if finalization won't work for
	# this doctype, the customer should never be asked to tap their card.
	txn_service.validate_finalization_ready(reference_doctype)
	mode_doc = _load_pinelabs_mode(mode_of_payment, expected_flows=("Terminal", "Both"))
	# The Mode of Payment must have a default account for the source doc's
	# company — but only if the configured finalization path actually creates
	# a Payment Entry. Mapped-field finalization (custom doctypes) and the
	# permissive no-config path don't post to a ledger account.
	txn_service.assert_mode_has_account_mapped(
		mode_of_payment,
		getattr(source_doc, "company", None),
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		create_payment_entry=create_payment_entry,
	)
	resolved_amount = flt(amount) if amount else _amount_for(source_doc)
	if resolved_amount <= 0:
		frappe.throw(_("No outstanding amount on {0} {1}.").format(reference_doctype, reference_name))

	# Serialize concurrent initiates on this source doc, then refuse a second
	# attempt while one is in flight. The lock closes the check-then-act window
	# so a double-click / double request can't create two charges.
	txn_service.lock_source_for_initiate(reference_doctype, reference_name)
	existing = txn_service.get_active_for_doc(reference_doctype, reference_name, for_update=True)
	if existing:
		frappe.throw(
			_("A {0} payment ({1}) is already in progress for this document.").format(
				existing.flow_type, existing.name
			),
		)

	machine = resolve_machine_for_context(
		reference_doctype, reference_name, source_doc=source_doc,
	)
	if not machine:
		frappe.throw(
			_("No Pinelabs machine resolves for this transaction. Configure machine mappings in Pinelabs Machine Mapping, or mark one machine as Is Default in Pinelabs Settings."),
		)

	terminal_method = _terminal_method_label(mode_doc)
	terminal_code = cint(mode_doc.get("pinelabs_terminal_code"))
	if terminal_code not in (1, 10, 11):
		frappe.throw(
			_("Mode of Payment {0} has invalid terminal code {1}; expected 1 (Card), 10 (UPI Sale) or 11 (UPI Bharat QR).").format(
				mode_of_payment, terminal_code
			),
		)

	txn = txn_service.create_transaction(
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		flow_type="Terminal",
		amount=resolved_amount,
		payment_method=terminal_method,
		machine=machine.machine_name,
		create_payment_entry=create_payment_entry,
		client_request_id=client_request_id,
	)

	_debug_print(
		"INITIATE",
		f"{txn.name} | {reference_doctype} {reference_name} | mode={mode_of_payment} "
		f"method={terminal_method} AllowedPaymentMode={terminal_code} amount={resolved_amount} "
		f"machine={machine.machine_name}",
	)
	client = get_pinelabs_client(machine_name=machine.machine_name)
	response = client.upload_billed_raw(
		transaction_ref=txn.name,
		amount=resolved_amount,
		allowed_payment_mode=terminal_code,
	)
	response_data = response.get("data") or {}
	response_code = response_data.get("ResponseCode")
	plutus_ref = response_data.get("PlutusTransactionReferenceID")
	_debug_print(
		"INITIATE RESULT",
		f"{txn.name} | success={response.get('success')} ResponseCode={response_code} "
		f"Message={response_data.get('ResponseMessage') or response.get('error')} PlutusRef={plutus_ref}",
	)
	plutus_ref_str = str(plutus_ref) if plutus_ref else None
	# Surfaced by the client so we can persist exactly what the app sent
	# alongside what Pine Labs returned. Same shape Plural already stores
	# at create_transaction time — this brings the Terminal flow to parity.
	request_payload = response.get("request_payload")

	# ResponseCode mapping (UploadBilledTransaction):
	#   0    = accepted, terminal is now processing → PENDING
	#   1, 2 = rejected synchronously → FAILED
	if not response.get("success"):
		# Capture the return value: mark_* rebind their local `transaction`
		# via _lock_and_reload, so our outer `txn` would otherwise stay at
		# the pre-transition status and _shape would return stale data.
		error = response.get("error") or _("Pine Labs API call failed")
		if not response.get("status_code"):
			# Timeout / connection drop: Pine Labs may still have received the
			# upload, and we have no PlutusTransactionReferenceID to cancel it.
			error = _("{0}. The request may still have reached the terminal — check the device and cancel the payment there before retrying.").format(error)
		txn = txn_service.mark_failed(
			txn,
			error_message=error,
			response_payload=response_data or response,
			request_payload=request_payload,
		)
		return _shape(success=False, txn=txn, response_code=response_code, message=error)

	if response_code in (0, "0"):
		# Pine Labs accepted the request but didn't return a
		# PlutusTransactionReferenceID — we'd have nothing to poll/cancel with,
		# so the txn would sit PENDING forever (the cron skips rows with no
		# payment_id). Fail fast so the operator sees it.
		if not plutus_ref_str:
			error = _(
				"Pine Labs accepted the transaction but returned no "
				"PlutusTransactionReferenceID. Cannot track this txn."
			)
			txn = txn_service.mark_failed(
				txn,
				error_message=error,
				response_payload=response_data,
				request_payload=request_payload,
			)
			return _shape(success=False, txn=txn, response_code=response_code, message=error)
		# Pass payment_id through mark_pending — the doc gets reloaded inside,
		# so an in-memory `txn.payment_id = ...` here would be lost.
		txn = txn_service.mark_pending(
			txn,
			payment_id=plutus_ref_str,
			response_payload=response_data,
			request_payload=request_payload,
		)
		return _shape(success=True, txn=txn, response_code=response_code, message=response_data.get("ResponseMessage"))

	# Synchronous decline.
	txn = txn_service.mark_failed(
		txn,
		error_message=response_data.get("ResponseMessage") or _("Terminal declined the request"),
		response_payload=response_data,
		request_payload=request_payload,
	)
	return _shape(success=False, txn=txn, response_code=response_code, message=response_data.get("ResponseMessage"))


@frappe.whitelist()
def poll_terminal_status(transaction_name: str, create_payment_entry: int | None = None) -> dict:
	"""Same behaviour as :func:`check_terminal_status`, distinct method name.

	The locked "Waiting for terminal" dialog's internal poll calls this
	endpoint instead of ``check_terminal_status`` so a consumer-app
	defensive shim that intercepts ``check_terminal_status`` by method
	name (e.g. fin_pos's `pinelabs check_terminal_status polling
	neutralized` browser-side wrapper) does NOT silently swallow our
	polls. The shim was designed to prevent two browser-side pollers
	racing on the same txn — but it also blocks our dialog from
	driving state when the consumer's own poller is not running for the
	current txn (e.g. after our Retry button creates a NEW txn that the
	consumer's PaymentModal does not know about).

	Race tolerance: when two pollers concurrently transition the same
	Pinelabs Transaction, ``check_terminal_status`` may raise
	``frappe.TimestampMismatchError`` from the inner ``transaction.save``.
	We catch it here and return the row's current persisted state — the
	first poller's mutation has already landed, so the second poller has
	nothing to add.
	"""
	try:
		return check_terminal_status(transaction_name, create_payment_entry)
	except frappe.TimestampMismatchError:
		# The other poller won the race — the row already reflects the
		# latest state. Return the persisted snapshot so the dialog can
		# react via realtime / its next poll instead of surfacing an error
		# popup to the operator.
		#
		# frappe.throw queues the "Document has been modified…" popup to
		# frappe.local.message_log BEFORE raising the exception, so simply
		# catching the exception isn't enough to suppress the popup. Strip
		# the specific entry so the dialog doesn't show a confusing error
		# the operator can't act on — but leave any other queued messages
		# intact (e.g. real validation errors the operator does need to see).
		_strip_timestamp_mismatch_popup()
		cached = (
			frappe.db.get_value(
				"Pinelabs Transaction",
				transaction_name,
				("status", "error_message"),
				as_dict=True,
			) or {}
		)
		return {
			"success": True,
			"status": cached.get("status") or "PENDING",
			"response_code": None,
			"message": cached.get("error_message") or "Poll raced; returned cached state.",
		}


@frappe.whitelist()
def check_terminal_status(transaction_name: str, create_payment_entry: int | None = None) -> dict:
	"""Poll Pine Labs for the latest terminal status of a transaction.

	Frontend calls this every ~3s while its modal is open.
	Returns {success, status, response_code, message}.

	``create_payment_entry`` (optional): late override of the same flag set
	at initiate time. ``1`` / ``0`` updates the stored decision on the
	Pinelabs Transaction so the finalize path uses it. Omit to leave the
	stored value (or the Payable Documents config) untouched.
	"""
	from pinelabs_fin.api import access
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	# Whoever may take the payment may watch it. Without this, any logged-in
	# user could read another user's payment records by guessing a PLT- name.
	# The cron reconciler reaches this as Administrator and bypasses the check.
	access.assert_txn_access(txn)
	# Late override — caller can flip the create-PE decision before the
	# poll triggers mark_success. Stored on the row so the cron/webhook
	# paths also honor the latest value.
	override = txn_service.normalize_create_payment_entry(create_payment_entry)
	if override and txn.create_payment_entry_override != override and txn.status in ("INITIATED", "PENDING"):
		txn.create_payment_entry_override = override
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)
	if txn.flow_type != "Terminal":
		frappe.throw(_("Transaction {0} is not a Terminal transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "response_code": None, "message": "terminal state"}
	if not txn.payment_id:
		return {"success": False, "status": txn.status, "response_code": None, "message": "PlutusTransactionReferenceID missing"}

	client = get_pinelabs_client(machine_name=txn.machine)
	response = client.get_status_raw(txn.payment_id)
	response_data = response.get("data") or {}
	response_code = response_data.get("ResponseCode")
	_debug_print(
		"STATUS",
		f"{txn.name} | PlutusRef={txn.payment_id} age={_txn_age_minutes(txn):.1f}min "
		f"success={response.get('success')} ResponseCode={response_code} "
		f"Message={response_data.get('ResponseMessage') or response.get('error')}",
	)

	# ResponseCode mapping (GetCloudBasedTxnStatus):
	#   0    = APPROVED                              → SUCCESS
	#   1, 2 = DECLINED                              → FAILED
	#   1001 = TXN UPLOADED (still pending)          → keep polling
	#   1006 = NO RESPONSE FROM PLUTUS (timeout —    → FAILED
	#          terminal didn't respond in time;
	#          definitively dead)
	#   1008 = TXN VOIDED (cancelled externally,     → CANCELLED
	#          e.g. via the terminal UI or another
	#          system call)
	if response_code in (0, "0"):
		txn_service.mark_success(
			txn,
			payment_id=txn.payment_id,
			payment_method=txn.payment_method,
			response_payload=response_data,
		)
		return {"success": True, "status": "SUCCESS", "response_code": response_code, "message": "Approved"}

	if response_code in (1001, "1001"):
		# Still waiting on the terminal — refresh response_payload for visibility.
		# Lock the row so the foreground 3s poll and the 1-min cron can't stomp
		# each other's response_payload writes. If another worker already finalized
		# us in the meantime, bail out and report the new state.
		txn = txn_service._lock_and_reload(txn)
		if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
			return {"success": True, "status": txn.status, "response_code": response_code, "message": "already finalized"}
		txn.response_payload = frappe.as_json(response_data, indent=2)
		txn.flags.pinelabs_internal_transition = True
		txn.save(ignore_permissions=True)

		# Safety net: terminal keeps reporting "still waiting" past our cutoff.
		# Force a settlement (cancel + final poll) so the row doesn't sit
		# PENDING forever.
		age_minutes = _txn_age_minutes(txn)
		cutoff = _auto_cancel_minutes(txn)
		if age_minutes >= cutoff:
			return _attempt_age_out_settlement(
				txn,
				response_code,
				response_data,
				response_data.get("ResponseMessage") or "Terminal still reporting TXN UPLOADED",
			)
		return {"success": True, "status": "PENDING", "response_code": response_code, "message": "Waiting on terminal"}

	if response_code in (1, 2, "1", "2"):
		message = response_data.get("ResponseMessage") or _("Terminal declined")
		txn_service.mark_failed(txn, error_message=message, response_payload=response_data)
		return {"success": True, "status": "FAILED", "response_code": response_code, "message": message}

	if response_code in (1006, "1006"):
		# Definitive timeout from Pine Labs — terminal didn't respond in time.
		# Treat as FAILED immediately rather than waiting out the age-out
		# cutoff: the gateway has already declared the transaction dead.
		message = response_data.get("ResponseMessage") or _("Terminal timeout — no response from Pine Labs")
		txn_service.mark_failed(txn, error_message=message, response_payload=response_data)
		return {"success": True, "status": "FAILED", "response_code": response_code, "message": message}

	if response_code in (1008, "1008"):
		# Transaction was voided externally (cancelled at the terminal UI or
		# by another system call). Reflect that as CANCELLED here so the
		# dialog closes and the source doc reconciles properly.
		message = response_data.get("ResponseMessage") or _("Transaction voided at terminal")
		txn_service.mark_cancelled(txn, reason=message)
		return {"success": True, "status": "CANCELLED", "response_code": response_code, "message": message}

	# Unknown response_code — Pine Labs returned something outside the documented
	# set (or the underlying HTTP call failed and response_data is empty).
	# Capture what we can on the txn for visibility AND age-out via the
	# settlement helper so we don't lose a customer-paid race.
	unknown_msg = (
		response_data.get("ResponseMessage")
		or response.get("error")  # propagate timeout/connection error text
		or "Unknown response code"
	)
	txn = txn_service._lock_and_reload(txn)
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {"success": True, "status": txn.status, "response_code": response_code, "message": "already finalized"}
	txn.response_payload = frappe.as_json(response_data, indent=2)
	txn.error_message = txn_service._truncate_with_ellipsis(
		f"Unknown ResponseCode {response_code}: {unknown_msg}", 1000
	)
	txn.flags.pinelabs_internal_transition = True
	txn.save(ignore_permissions=True)

	age_minutes = _txn_age_minutes(txn)
	cutoff = _auto_cancel_minutes(txn)
	if age_minutes >= cutoff:
		return _attempt_age_out_settlement(txn, response_code, response_data, unknown_msg)

	return {
		"success": True,
		"status": txn.status,
		"response_code": response_code,
		"message": unknown_msg,
	}


@frappe.whitelist()
def retry_failed_terminal_payment(transaction_name: str) -> dict:
	"""One-call retry of a previously failed Terminal payment.

	Used by the dialog's Retry button. The browser would otherwise need two
	round-trips (frappe.client.get_value to look up the source doc + payment
	method, then initiate_terminal_payment) — that adds visible lag compared
	to the first initiate. This endpoint reads the original txn's source
	doc + payment method server-side and immediately re-runs
	initiate_terminal_payment, so retry feels as snappy as the first attempt.

	Returns the same 5-key envelope as initiate_terminal_payment.
	"""
	from pinelabs_fin.api import access
	from pinelabs_fin.api.transaction import PINELABS_METHOD_TO_MODE

	if not frappe.db.exists("Pinelabs Transaction", transaction_name):
		frappe.throw(
			_("Pinelabs Transaction {0} does not exist.").format(transaction_name),
		)
	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	# Retry starts a REAL new charge, so gate it here as well as inside
	# initiate_terminal_payment — this refuses before we read the source doc.
	access.assert_txn_access(txn)
	if txn.flow_type != "Terminal":
		frappe.throw(
			_("Transaction {0} is not a Terminal transaction.").format(transaction_name),
		)
	if not (txn.reference_doctype and txn.reference_name):
		frappe.throw(
			_("Transaction {0} has no source document reference.").format(transaction_name),
		)
	mode_of_payment = PINELABS_METHOD_TO_MODE.get(str(txn.payment_method or "").upper())
	if not mode_of_payment:
		frappe.throw(
			_("Transaction {0} has no recognised payment method ({1}).").format(
				transaction_name, txn.payment_method or "—",
			),
		)
	# Carry over what the original caller decided. POS payments reference a
	# POS Profile (no outstanding to derive an amount from), so reuse the
	# original amount there.
	override = (txn.create_payment_entry_override or "").strip()
	return initiate_terminal_payment(
		reference_doctype=txn.reference_doctype,
		reference_name=txn.reference_name,
		mode_of_payment=mode_of_payment,
		amount=txn.amount if txn.reference_doctype == "POS Profile" else None,
		create_payment_entry={"Yes": 1, "No": 0}.get(override),
		client_request_id=txn.get("client_request_id"),
	)


@frappe.whitelist()
def cancel_terminal_payment(transaction_name: str, reason: str | None = None) -> dict:
	"""User-driven cancel of an in-flight terminal transaction."""
	from pinelabs_fin.api import access
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	txn = frappe.get_doc("Pinelabs Transaction", transaction_name)
	# Cancelling is destructive to a live payment — same bar as taking it.
	access.assert_txn_access(txn)
	if txn.flow_type != "Terminal":
		frappe.throw(_("Transaction {0} is not a Terminal transaction.").format(transaction_name))
	if txn.status in ("SUCCESS", "FAILED", "CANCELLED"):
		return {
			"success": True,
			"transaction_name": txn.name,
			"status": txn.status,
			"response_code": None,
			"message": "Already terminal",
		}

	server_message = None
	cancel_response_code = None
	if txn.payment_id:
		client = get_pinelabs_client(machine_name=txn.machine)
		response = client.cancel_raw(txn.payment_id, amount=txn.amount)
		response_data = response.get("data") or {}
		server_message = response_data.get("ResponseMessage")
		cancel_response_code = response_data.get("ResponseCode")
		_debug_print(
			"CANCEL",
			f"{txn.name} | PlutusRef={txn.payment_id} ResponseCode={cancel_response_code} "
			f"Message={server_message or response.get('error')}",
		)

	# If Pine Labs rejected the cancel (e.g. "transaction already processed"),
	# the customer may have actually paid in the race window between user click
	# and our cancel call. Poll status once before forcing CANCELLED — if the
	# txn is APPROVED, mark SUCCESS so we don't drop a real payment.
	if cancel_response_code is not None and cancel_response_code not in (0, "0"):
		try:
			status_client = get_pinelabs_client(machine_name=txn.machine)
			status_response = status_client.get_status_raw(txn.payment_id)
			status_data = status_response.get("data") or {}
			status_code = status_data.get("ResponseCode")
			if status_code in (0, "0"):
				txn = txn_service.mark_success(
					txn,
					payment_id=txn.payment_id,
					payment_method=txn.payment_method,
					response_payload=status_data,
				)
				return {
					"success": True,
					"transaction_name": txn.name,
					"status": "SUCCESS",
					"response_code": status_code,
					"message": _("Cancel rejected by gateway: customer already paid. Marked SUCCESS."),
				}
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs cancel-time final status check failed for {transaction_name}",
			)

	txn = txn_service.mark_cancelled(txn, reason=reason or server_message or "user cancelled")
	return {
		"success": True,
		"transaction_name": txn.name,
		"status": "CANCELLED",
		"response_code": cancel_response_code,
		"message": server_message or "Cancelled",
	}


def reconcile_pending_terminal_payments() -> None:
	"""Cron task — reconcile every PENDING Terminal transaction with Pine Labs.

	Why this exists: the browser dialog drives `check_terminal_status` while
	the user watches it, but if the user closes the tab, refreshes, or any
	dialog dismisses unexpectedly, the in-flight transaction would be stuck
	at PENDING forever (Pine Labs Cloud doesn't push terminal webhooks). This
	job pulls the latest status server-side every minute so the doc gets
	finalized regardless of what the browser is doing.

	Idempotent: piggy-backs on `check_terminal_status`, which is already
	idempotent (mark_success / mark_failed both no-op on terminal states).
	Errors per row are logged but never abort the whole batch.
	"""
	rows = frappe.get_all(
		"Pinelabs Transaction",
		filters={
			"flow_type": "Terminal",
			"status": "PENDING",
		},
		fields=["name", "payment_id"],
	)
	for row in rows:
		# Skip rows that don't yet have a Plutus reference — the foreground
		# request that creates them sets payment_id during initiate; if it's
		# missing here we can't query Pine Labs anyway.
		if not row.payment_id:
			continue
		try:
			check_terminal_status(row.name)
			# Per-row commit in a batch cron: a successfully reconciled
			# transaction must be durable before moving to the next row, so
			# one bad row's rollback (in the except below) can't undo the
			# rows already finalized in this run.
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs reconcile_pending_terminal_payments failed for {row.name}",
			)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _strip_timestamp_mismatch_popup():
	"""Remove Frappe's auto-queued 'Document has been modified…' popup from
	``frappe.local.message_log`` so callers that gracefully recover from a
	TimestampMismatchError don't leak a confusing error popup to the user.
	Other queued messages are preserved.
	"""
	import json as _json
	try:
		log = list(getattr(frappe.local, "message_log", []) or [])
	except Exception:
		return
	filtered = []
	for entry in log:
		try:
			# Each entry is a JSON-stringified dict like
			# {"message": "...", "title": "...", "raise_exception": 1, ...}
			text = entry if isinstance(entry, str) else str(entry)
			parsed = None
			try:
				parsed = _json.loads(text) if isinstance(entry, str) else entry
			except Exception:
				parsed = {"message": text}
			msg = (parsed or {}).get("message") if isinstance(parsed, dict) else text
			if msg and "Document has been modified after you have opened it" in str(msg):
				continue
		except Exception:
			pass
		filtered.append(entry)
	try:
		frappe.local.message_log = filtered
	except Exception:
		pass


def _attempt_age_out_settlement(txn, last_response_code, last_response_data, last_msg):
	"""Force a settlement when polling hasn't reached a definitive state.

	Customer's tap may have raced our decision to give up. We:
	  1. Cancel the terminal-side reservation so no further taps can land.
	  2. Poll status one last time — if the customer paid in the race
	     window, mark SUCCESS instead of FAILED.
	  3. Otherwise, mark FAILED with the last-known reason.

	Best-effort: cancel / final-poll failures don't block the FAILED
	transition (we still need a terminal state).
	"""
	from pinelabs_fin.api import transaction as txn_service
	from pinelabs_fin.api.pinelabs_client import get_pinelabs_client

	if txn.payment_id:
		# Stop the terminal from accepting further taps. If the regular
		# Cancel returns a non-zero ResponseCode (terminal frozen, already
		# processed, etc.), try the Force Cancel API as a fallback. Both
		# are best-effort — failures here don't block the final-poll +
		# state transition below.
		try:
			cancel_client = get_pinelabs_client(machine_name=txn.machine)
			cancel_response = cancel_client.cancel_raw(txn.payment_id, amount=txn.amount)
			cancel_code = (cancel_response.get("data") or {}).get("ResponseCode")
			if cancel_code not in (0, "0", None):
				# Regular cancel was rejected — fall back to Force Cancel if
				# the endpoint is configured. force_cancel_raw returns None
				# (no-op) when the endpoint URL is blank on the Pinelabs API
				# Endpoint row, keeping this safe for sites that haven't
				# opted into the v1.36+ Force Cancel API.
				try:
					cancel_client.force_cancel_raw(txn.payment_id, amount=txn.amount)
				except Exception:
					frappe.log_error(
						frappe.get_traceback(),
						f"Pinelabs age-out force-cancel failed for {txn.name}",
					)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs age-out cancel failed for {txn.name}",
			)

		# Final status check — did the customer pay in the race window?
		try:
			status_client = get_pinelabs_client(machine_name=txn.machine)
			final = status_client.get_status_raw(txn.payment_id)
			final_data = final.get("data") or {}
			final_code = final_data.get("ResponseCode")
			if final_code in (0, "0"):
				txn_service.mark_success(
					txn,
					payment_id=txn.payment_id,
					payment_method=txn.payment_method,
					response_payload=final_data,
				)
				return {
					"success": True,
					"status": "SUCCESS",
					"response_code": final_code,
					"message": "Approved (caught at age-out)",
				}
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"Pinelabs age-out final status check failed for {txn.name}",
			)

	# No SUCCESS race detected — mark FAILED with the last-known reason.
	txn_service.mark_failed(
		txn,
		error_message=f"Aged out. Last ResponseCode={last_response_code}: {last_msg}",
		response_payload=last_response_data,
	)
	return {
		"success": True,
		"status": "FAILED",
		"response_code": last_response_code,
		"message": "Aged out",
	}


def _load_eligible_source(reference_doctype, reference_name):
	if not (reference_doctype and reference_name):
		frappe.throw(_("reference_doctype and reference_name are required."))
	if not frappe.db.exists(reference_doctype, reference_name):
		frappe.throw(_("{0} {1} does not exist.").format(reference_doctype, reference_name))
	doc = frappe.get_doc(reference_doctype, reference_name)
	# Drafts are accepted: mark_success auto-submits the draft before creating
	# the Payment Entry (see _create_payment_entry_idempotent). Cancelled docs
	# are still rejected — they're immutable in Frappe, so we can't book a PE
	# against them.
	if cint(getattr(doc, "docstatus", 0)) == 2:
		frappe.throw(
			_("{0} {1} is cancelled; cannot take a terminal payment.").format(
				reference_doctype, reference_name
			),
		)
	return doc


def _amount_for(source_doc):
	"""Thin alias — the resolution rules live in one place for both flows.

	See ``transaction.resolve_payment_amount``: it honours the Payable
	Documents ``Amount Field`` (which used to be silently ignored) and keeps
	the settled-document guard that stops a fully-paid invoice being charged
	again.
	"""
	from pinelabs_fin.api import transaction as txn_service

	return txn_service.resolve_payment_amount(source_doc)


def _load_pinelabs_mode(mode_of_payment, expected_flows):
	"""Validate that ``mode_of_payment`` is a Pine Labs Terminal mode.

	Pine Labs modes are identified by name (see ``PINELABS_TERMINAL_MODES``
	in transaction.py) — no custom-field lookup is needed. Returns a small
	dict the rest of the file can read like the legacy ``mode_doc``:
	``{"pinelabs_terminal_code": <int>}``.

	``expected_flows`` is kept for signature compatibility with the legacy
	flow-type check; this function only ever runs against the Terminal
	dialog, so the only valid input is ``("Terminal", "Both")``.
	"""
	from pinelabs_fin.api.transaction import PINELABS_TERMINAL_MODES, get_terminal_code

	if not mode_of_payment:
		frappe.throw(_("Mode of Payment is required."))
	if not frappe.db.exists("Mode of Payment", mode_of_payment):
		frappe.throw(_("Mode of Payment {0} does not exist.").format(mode_of_payment))

	terminal_code = get_terminal_code(mode_of_payment)
	if terminal_code is None:
		frappe.throw(
			_(
				"Mode of Payment {0} is not a Pine Labs Terminal mode. "
				"Pick one of: {1}."
			).format(mode_of_payment, ", ".join(PINELABS_TERMINAL_MODES.keys())),
		)
	return {"pinelabs_terminal_code": terminal_code}


def _terminal_method_label(mode_doc):
	"""Map terminal_code → CARD / UPI string used for payment_method."""
	code = cint(mode_doc.get("pinelabs_terminal_code"))
	return {1: "CARD", 10: "UPI", 11: "UPI"}.get(code, "")


def _shape(*, success, txn, response_code, message):
	return {
		"success": bool(success),
		"transaction_name": txn.name,
		"status": txn.status,
		"response_code": response_code,
		"message": message,
	}


def _txn_age_minutes(txn) -> float:
	"""Minutes since the Pinelabs Transaction was created."""
	created = txn.created_at or txn.creation
	if not created:
		return 0.0
	return time_diff_in_seconds(get_datetime(), get_datetime(created)) / 60.0


def _auto_cancel_minutes(txn) -> int:
	"""How long the APP waits before force-settling a stuck transaction.

	Always the gateway's own auto-cancel timeout plus a grace margin, never
	an independent number. Pine Labs cancels the transaction at the terminal
	after ``AutoCancelDurationInMinutes``; the next status poll then reports a
	terminal state, so in the normal case the app never has to intervene at
	all. The age-out exists only for when the gateway never returns a
	definitive answer (unknown ResponseCode, or "TXN UPLOADED" forever).

	Deriving it guarantees the invariant that matters: the app must never give
	up while the terminal is still legitimately waiting for the customer.
	Previously this was a hardcoded 5 minutes with a broken machine lookup, so
	any configured value above 5 — including the 10 / 20 / 100 / 120 that Pine
	Labs uses in its own documentation examples — made the app fire
	``cancel`` + ``force_cancel`` mid-transaction.
	"""
	from pinelabs_fin.pinelabs_fin.doctype.pinelabs_settings.pinelabs_settings import (
		resolve_auto_cancel_minutes,
	)

	return resolve_auto_cancel_minutes(txn.machine) + AGE_OUT_GRACE_MINUTES
