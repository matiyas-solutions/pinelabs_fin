# Copyright (c) 2026, Pinelabs Fin contributors
# For license information, please see license.txt

"""Pinelabs Settings — singleton holding all integration config.

Sections (in form order):
  - Basic Setup        — master enable, terminals (``machines`` table) +
                          routing mode, mapping rules.
  - Plural API         — Pay-by-Link credentials, base URL, expiry.
  - Payable Documents  — per-doctype eligibility + customer-contact mapping.
  - Technical          — endpoint overrides, SSL, debug, auto-cancel.

Public helpers (used outside the controller):
  - ``get_default_machine_config()`` — single-mode machine pick.
  - ``resolve_machine_for_context(...)`` — mapping-mode machine pick with
    fallback to default. Used by ``api/terminal.py``.

The controller's ``validate`` chain enforces: at-most-one default machine,
plural credential length / expiry, payable-doctype amount/status fields,
and the Customer Contact Field Mapping completeness. ``on_update`` busts
the cached Plural OAuth token whenever an identity-bearing field changes.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

_MIN_WEBHOOK_SECRET_LEN = 16

# Auto-cancel duration sent to Pine Labs as ``AutoCancelDurationInMinutes``:
# the gateway cancels the transaction at the terminal if the customer hasn't
# paid within this many minutes. The field is Optional in the Cloud
# Integration Guide with no documented min/max; Pine Labs' own examples use
# 2, 5, 10, 20, 100 and 120. 3 matches the Settings field's description.
DEFAULT_AUTO_CANCEL_MINUTES = 3
# Watched fields — when any of these change, the Plural OAuth token cache must
# be cleared so the next call fetches a fresh token against the new identity.
_PLURAL_CACHE_BUSTING_FIELDS = (
	"plural_client_id",
	"plural_client_secret",
	"plural_base_url",
)


class PinelabsSettings(Document):
	def validate(self):
		self._auto_default_only_machine()
		self._validate_one_default_machine()
		self._resolve_plural_base_url()
		self._validate_plural_credentials()
		self._validate_payable_doctypes()

	def on_update(self):
		self._maybe_bust_plural_token_cache()

	def _auto_default_only_machine(self):
		"""When a single machine row exists, force is_default = 1 on it."""
		machines = self.machines or []
		if len(machines) == 1 and not cint(machines[0].is_default):
			machines[0].is_default = 1

	def _validate_one_default_machine(self):
		"""At most one machine may be default. Empty machines table is allowed."""
		defaults = [m for m in (self.machines or []) if cint(m.is_default)]
		if len(defaults) > 1:
			frappe.throw(
				_("Exactly one machine may be marked Is Default. Found {0}: {1}.").format(
					len(defaults),
					", ".join(m.machine_name or "(unnamed)" for m in defaults),
				)
			)

	def _validate_plural_credentials(self):
		"""Plural credentials and webhook secret are mandatory when payment links are enabled."""
		if not cint(self.enable_payment_links):
			return

		# Frappe's mandatory_depends_on takes care of the empty-field case for
		# plural_client_id / plural_client_secret / payment_link_webhook_secret;
		# we only enforce the secret-length rule here.
		secret = None
		try:
			secret = self.get_password("payment_link_webhook_secret", raise_exception=False)
		except Exception:
			secret = None
		if secret and len(secret) < _MIN_WEBHOOK_SECRET_LEN:
			frappe.throw(
				_("Webhook Secret (HMAC) must be at least {0} characters for security.").format(
					_MIN_WEBHOOK_SECRET_LEN
				)
			)

		# Expiry must be positive when present.
		expiry = cint(getattr(self, "payment_link_expiry_minutes", 0))
		if expiry and expiry <= 0:
			frappe.throw(_("Payment Link Expiry (Minutes) must be greater than 0."))

	def _resolve_plural_base_url(self):
		"""Auto-fill plural_base_url when the user leaves it blank.

		The field is editable — a typed value is preserved. We only fill
		blanks, picking the first available source: site_config override
		(`pine_plural_base_url`) → sandbox default. URL is normalised
		(trailing slash trimmed) so calls don't end up double-slashed.
		"""
		if not cint(self.enable_payment_links):
			return
		from pinelabs_fin.api.plural_client import PLURAL_SANDBOX_URL

		typed = (self.plural_base_url or "").strip()
		if typed:
			self.plural_base_url = typed.rstrip("/")
			return

		site_override = (frappe.conf.get("pine_plural_base_url") or "").strip()
		if site_override:
			self.plural_base_url = site_override.rstrip("/")
			return
		self.plural_base_url = PLURAL_SANDBOX_URL

	def _maybe_bust_plural_token_cache(self):
		"""Clear the cached OAuth token if any identity-bearing field changed.

		on_update fires after the DB write, so `self._doc_before_save` holds
		the previous values. Comparing each field surfaces an environment
		switch, a credential rotation, or a base-URL override change — any
		of which would make the previously-issued token invalid for the
		next API call.
		"""
		before = getattr(self, "_doc_before_save", None)
		if before is None:
			# First save / patch context — nothing to compare; clear anyway
			# since callers have no way to know whether identity changed.
			frappe.cache().delete_value("pine_plural_access_token")
			return
		for field in _PLURAL_CACHE_BUSTING_FIELDS:
			if (getattr(self, field, None) or "") != (getattr(before, field, None) or ""):
				frappe.cache().delete_value("pine_plural_access_token")
				return

	def _validate_payable_doctypes(self):
		"""Require amount_field and no duplicate rows.

		Unchecking Create Payment Entry needs no extra configuration — it
		simply means "record the payment, post nothing to the ledger".
		"""
		seen = set()
		for row in (self.payable_doctypes or []):
			doctype = (row.doctype_name or "").strip()
			if not doctype:
				continue
			if doctype in seen:
				frappe.throw(_("Duplicate Payable Doctype row for {0}.").format(doctype))
			seen.add(doctype)
			if not (row.amount_field or "").strip():
				frappe.throw(_("Payable Doctype {0} requires an Amount Field.").format(doctype))

			# The Customer Contact Field Mapping is Pay-by-Link only (it tells
			# Plural where to read the mobile / email the hosted link is sent
			# to). Its fields are hidden in the grid when Enable Payment Links
			# is off, so validating them there would throw about fields the
			# user cannot see or fix.
			if cint(self.enable_payment_links):
				self._validate_contact_mapping(row, doctype, "mobile", _("Mobile"))
				self._validate_contact_mapping(row, doctype, "email", _("Email"))

	def _validate_contact_mapping(self, row, doctype, field_type, label):
		"""For each contact-field mapping that's been opted into, require its
		dependent fields. Unset (blank fetch_from) is a valid 'use auto-detect'
		state and skips validation.
		"""
		fetch_from = (row.get(f"{field_type}_fetch_from") or "").strip()
		if not fetch_from:
			return

		if fetch_from == "Direct Field":
			if not (row.get(f"{field_type}_direct_field") or "").strip():
				frappe.throw(
					_("Payable Doctype {0}: {1} is set to 'Direct Field' but Field Name is empty.").format(
						doctype, label
					),
				)
			return

		if fetch_from == "Linked DocType":
			missing = [
				name
				for name, value in (
					(_("Link Field"), row.get(f"{field_type}_link_field")),
					(_("Linked DocType"), row.get(f"{field_type}_linked_doctype")),
					(_("Field in Linked DocType"), row.get(f"{field_type}_linked_field")),
				)
				if not (value or "").strip()
			]
			if missing:
				frappe.throw(
					_("Payable Doctype {0}: {1} is set to 'Linked DocType' but these are empty: {2}.").format(
						doctype, label, ", ".join(missing)
					),
				)

def resolve_auto_cancel_minutes(machine_name: str | None = None) -> int:
	"""Return the gateway auto-cancel timeout for a machine, in minutes.

	Resolution order: the machine's ``Auto Cancel (Minutes) Override`` →
	``Pinelabs Settings.auto_cancel_duration_minutes`` → the default.

	Single source of truth for BOTH timers that key off this number:

	- ``pinelabs_client`` sends it to Pine Labs as
	  ``AutoCancelDurationInMinutes`` (the terminal-side timer), and
	- ``terminal._auto_cancel_minutes`` derives the app-side age-out from it.

	Those two used to resolve independently and could contradict each other.
	The app-side lookup also queried ``Pinelabs Machine Config`` by ``name``,
	but that doctype is a child table whose ``name`` is a random hash while
	``txn.machine`` stores ``machine_name`` — so it never matched and always
	fell through to a hardcoded default. Machines are matched by
	``machine_name`` here, the same way ``pinelabs_client._load_machine``
	already did correctly.
	"""
	try:
		settings = frappe.get_single("Pinelabs Settings")
	except Exception:
		return DEFAULT_AUTO_CANCEL_MINUTES

	if machine_name:
		for machine in (settings.machines or []):
			if machine.machine_name != machine_name:
				continue
			override = cint(getattr(machine, "auto_cancel_duration_in_minutes", 0))
			if override > 0:
				return override
			break

	return cint(getattr(settings, "auto_cancel_duration_minutes", 0)) or DEFAULT_AUTO_CANCEL_MINUTES


def get_default_machine_config() -> Document | None:
	"""Return the default Pinelabs Machine Config row, or None if not configured."""
	settings = frappe.get_single("Pinelabs Settings")
	if not cint(settings.enabled):
		return None
	for machine in (settings.machines or []):
		if cint(machine.is_default) and cint(machine.enabled):
			return machine
	# Fallback: if exactly one enabled machine exists, treat it as default.
	enabled = [m for m in (settings.machines or []) if cint(m.enabled)]
	if len(enabled) == 1:
		return enabled[0]
	return None


def resolve_machine_for_context(
	reference_doctype: str | None,
	reference_name: str | None,
	source_doc: Document | None = None,
) -> Document | None:
	"""Pick the Pinelabs Machine Config row for a transaction.

	Resolution depends on Pinelabs Settings.terminal_routing_mode:

	- ``Single Machine`` (default) → returns ``get_default_machine_config()``.
	- ``Mapping`` → walks ``settings.machine_mappings`` (an inline child table)
	  for rows whose ``(mapping_type, reference_name)`` matches a context
	  extracted from the source document and current session, ordered
	  alphabetically by machine name; falls back to
	  ``get_default_machine_config()`` on no match.

	Returns ``None`` if neither path resolves a machine — caller should error
	with the standard "no machine configured" message.
	"""
	settings = frappe.get_single("Pinelabs Settings")
	if not cint(settings.enabled):
		return None

	mode = (settings.terminal_routing_mode or "Single Machine").strip()
	if mode != "Mapping":
		return get_default_machine_config()

	if source_doc is None and reference_doctype and reference_name:
		try:
			source_doc = frappe.get_doc(reference_doctype, reference_name)
		except Exception:
			source_doc = None

	contexts = _extract_routing_contexts(source_doc)
	if contexts:
		context_set = set(contexts)
		# Filter the inline child rows in Python — the table is small enough
		# that a DB query buys nothing; staying in-process also means the
		# rows reflect any unsaved edits in the current request.
		candidates = [
			row
			for row in (settings.machine_mappings or [])
			if cint(row.is_enabled)
			and (row.mapping_type, row.reference_name) in context_set
		]
		# Deterministic alphabetical tie-break on machine name.
		candidates.sort(key=lambda r: (r.machine or ""))
		if candidates:
			machine_name = candidates[0].machine
			# Resolve the child-table row on Settings.machines — terminal.py
			# expects a Machine Config child with all credential fields populated.
			for machine in (settings.machines or []):
				if machine.machine_name == machine_name and cint(machine.enabled):
					return machine

	return get_default_machine_config()


def _extract_routing_contexts(source_doc):
	"""Return [(mapping_type, reference_name), ...] for the current request.

	Always emits a User context for the logged-in user (skip Guest). Pulls
	Warehouse / Company / POS Profile from the source doc's standard fields,
	plus any unique ``items[*].warehouse`` rows.
	"""
	contexts = []

	user = (frappe.session.user if hasattr(frappe, "session") else None) or ""
	if user and user != "Guest":
		contexts.append(("User", user))

	if source_doc is None:
		return contexts

	def _get(field):
		# Works for both Document and dict-like inputs.
		try:
			return source_doc.get(field)
		except Exception:
			return getattr(source_doc, field, None)

	warehouse = _get("set_warehouse") or _get("warehouse")
	if warehouse:
		contexts.append(("Warehouse", warehouse))

	company = _get("company")
	if company:
		contexts.append(("Company", company))

	# POS Awesome payments reference the POS Profile itself.
	pos_profile = source_doc.name if _get("doctype") == "POS Profile" else _get("pos_profile")
	if pos_profile:
		contexts.append(("POS Profile", pos_profile))

	# Per-line warehouses on the items child table.
	seen_warehouses = {warehouse} if warehouse else set()
	items = _get("items") or []
	for item in items:
		try:
			wh = item.get("warehouse") if hasattr(item, "get") else getattr(item, "warehouse", None)
		except Exception:
			wh = None
		if wh and wh not in seen_warehouses:
			contexts.append(("Warehouse", wh))
			seen_warehouses.add(wh)

	return contexts
