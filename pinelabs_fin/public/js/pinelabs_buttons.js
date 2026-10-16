// -*- coding: utf-8 -*-
// Copyright (c) 2026, Pinelabs Fin contributors
// For license information, please see license.txt
//
// Phase 1 frontend driver. Adds two buttons to every doctype declared
// in Pinelabs Settings > Payable Documents:
//
//   1. Pay on Machine    -> Card / UPI selector -> polls until terminal status
//   2. Send Payment Link -> generates a Plural hosted link covering all
//                           enabled payment methods (no per-method dialog).
//
// All flow gating happens server-side. The buttons render whenever the
// doctype is in the payable list — the backend rejects with a clear
// error if eligibility/credentials/customer details are missing.
//
// Also exposes reusable utilities on `frappe.pinelabs` for any custom
// button / page to call:
//   frappe.pinelabs.start_terminal_payment(args, options)
//   frappe.pinelabs.open_terminal_modal(transaction_name, options)

// Namespace is declared OUTSIDE the IIFE so any other script that loads
// in the same desk session can read `frappe.pinelabs` even if the IIFE
// below hasn't finished executing yet. The utility functions get
// assigned to it from inside the IIFE.
window.frappe = window.frappe || {};
frappe.pinelabs = frappe.pinelabs || {};

// Extension registry — any app can push a `{ id, render(ctx) }` object
// here to inject extra DOM, buttons, or behaviour into the locked
// "Waiting for terminal" dialog. Declared on the namespace BEFORE the
// IIFE runs so extensions can register at any point, including before
// pinelabs_buttons.js finishes loading.
//
// Each extension's `render(ctx)` is called every time the dialog opens.
// `ctx` exposes:
//   - transaction_name: string  (the Pinelabs Transaction PLT-...)
//   - modal:            frappe.ui.Dialog instance
//   - body_$el:         jQuery handle to the dialog body (append into this)
//   - get_status_el():  the inner "status text" <div> (replaceable)
//   - add_secondary_button(label, onclick): helper to add a non-primary button
//
// Usage:
//   frappe.pinelabs.register_dialog_extension({
//     id: "fin_pos_show_amount",
//     render(ctx) {
//       ctx.body_$el.append(`<div class="my-2">Charging ₹${amount}</div>`);
//     },
//   });
frappe.pinelabs.dialog_extensions = frappe.pinelabs.dialog_extensions || [];
frappe.pinelabs.register_dialog_extension = function (ext) {
	if (!ext || typeof ext.render !== "function") {
		console.error("[Pinelabs] register_dialog_extension: ext.render must be a function");
		return;
	}
	// Replace any prior registration with the same id (dev re-load safety).
	if (ext.id) {
		frappe.pinelabs.dialog_extensions = frappe.pinelabs.dialog_extensions.filter(
			(e) => e.id !== ext.id,
		);
	}
	frappe.pinelabs.dialog_extensions.push(ext);
};

(function () {
	if (!window.frappe) return;

	const LOG = (...a) => console.log("[Pinelabs]", ...a);

	// Tracks currently-open "Waiting for terminal" modals, keyed by
	// transaction_name. Makes open_terminal_modal idempotent so the helper
	// callback + the frappe.call auto-intercept don't stack two modals for
	// the same txn.
	const OPEN_TERMINAL_MODALS = new Map();

	// In-memory cache of doctypes the user has marked payable, keyed by
	// doctype name. Each entry carries the per-button visibility flags and
	// the allowed-docstatus set from the Payable Doctype row. Pre-seed with
	// the three doctypes most commonly used so the first-form-render race
	// never bites — the async fetch backfills any custom ones and overrides
	// the entry with the saved values. allowed_docstatus defaults to {1}
	// (Submitted only), matching the JSON default for show_on_docstatus.
	const defaultFlags = () => ({
		pay_on_machine: true,
		send_payment_link: true,
		allowed_docstatus: new Set([1]),
	});
	const PAYABLE = {
		flags: new Map([
			["Sales Invoice", defaultFlags()],
			["POS Invoice", defaultFlags()],
			["Payment Request", defaultFlags()],
		]),
		fetched: false,
	};

	// Parse `"0"`, `"1"`, or `"0,1"` from show_on_docstatus into a Set of
	// integers. Cancelled (2) is never accepted, even if the row somehow
	// contains it. Empty / unparseable input falls back to {1}.
	function parse_allowed_docstatus(raw) {
		const allowed = new Set();
		String(raw || "")
			.split(",")
			.forEach((part) => {
				const n = parseInt(String(part).trim(), 10);
				if (n === 0 || n === 1) allowed.add(n);
			});
		if (allowed.size === 0) allowed.add(1);
		return allowed;
	}

	// ─────────────────────────────────────────────────────────────────
	// Pre-register form handlers for the seeded doctypes synchronously.
	// This MUST run at script-load time so handlers are in place before
	// any form first renders.
	// ─────────────────────────────────────────────────────────────────

	for (const dt of PAYABLE.flags.keys()) register_handler(dt);

	// Async refresh from server — captures custom doctypes the user added.
	frappe.after_ajax(refresh_payable_list);

	// The deprecated current-form global is the only handle to a form that
	// is ALREADY on screen when this desk-wide script initialises (before
	// any form 'refresh' fires). It's wrapped once here so the rest of the
	// file never touches that global directly.
	function open_form() {
		return window.cur_frm || null; // nosemgrep: frappe-cur-frm-usage
	}

	function render_if_payable(frm, log_label) {
		if (frm && PAYABLE.flags.has(frm.doctype)) {
			if (log_label) LOG(log_label, frm.doctype);
			render_pinelabs_buttons(frm);
		}
	}

	// Also re-render on route change (helps if the JS loaded after the
	// initial form render — opening another doc and coming back works).
	if (frappe.router && frappe.router.on) {
		frappe.router.on("change", () => {
			render_if_payable(open_form());
		});
	}

	// Belt-and-braces: if a form is already on screen when this script
	// runs, render against it immediately.
	$(document).ready(() => {
		render_if_payable(open_form(), "rendering buttons for current form on doc-ready");
	});

	function register_handler(doctype) {
		LOG("registering form handler for", doctype);
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				render_pinelabs_buttons(frm);
			},
		});
	}

	function refresh_payable_list() {
		// Fetch the parent Singleton — `frappe.db.get_list` on a child table
		// silently drops Link/custom fields (returns rows with only `name`),
		// so we'd never see doctype_name or the per-button flags. Pulling the
		// parent doc returns the full child rows.
		frappe.call({
			method: "frappe.client.get",
			args: { doctype: "Pinelabs Settings", name: "Pinelabs Settings" },
		})
			.then((r) => {
				const rows = (r && r.message && r.message.payable_doctypes) || [];
				PAYABLE.fetched = true;
				const enabled = rows.filter((row) => Number(row.is_enabled) === 1);

				LOG("payable doctypes from Settings:", enabled.map((row) => row.doctype_name));

				enabled.forEach((row) => {
					const dt = row.doctype_name;
					if (!dt) return;
					const flags = {
						pay_on_machine: Number(row.enable_pay_on_machine) === 1,
						send_payment_link: Number(row.enable_send_payment_link) === 1,
						allowed_docstatus: parse_allowed_docstatus(row.show_on_docstatus),
					};
					const isNew = !PAYABLE.flags.has(dt);
					PAYABLE.flags.set(dt, flags);
					if (isNew) register_handler(dt);
				});

				// If the current form just became payable, render now.
				render_if_payable(open_form());
			})
			.catch((err) => {
				console.error("[Pinelabs] failed to fetch Pinelabs Settings:", err);
			});
	}

	// ─────────────────────────────────────────────────────────────────
	// Buttons
	// ─────────────────────────────────────────────────────────────────

	function render_pinelabs_buttons(frm) {
		if (!frm || !frm.doc) return;

		// Frappe wipes custom buttons on every refresh, so always clean up
		// any leftovers before deciding whether to add ours back. This also
		// makes the buttons disappear correctly when the doc transitions out
		// of an eligible state (e.g. fully paid, cancelled).
		frm.remove_custom_button(__("Pay on Machine"));
		frm.remove_custom_button(__("Send Payment Link"));

		// Per-row config from the Payable Doctype row. Until the async fetch
		// lands, the seeded entry uses defaultFlags() (both buttons on,
		// allowed_docstatus = {1}) — matches the legacy behavior.
		const flags = PAYABLE.flags.get(frm.doctype) || defaultFlags();

		if (!flags.allowed_docstatus.has(frm.doc.docstatus)) {
			LOG(
				"skip: docstatus not allowed by row config",
				frm.doctype,
				frm.docname,
				"docstatus=",
				frm.doc.docstatus,
				"allowed=",
				Array.from(flags.allowed_docstatus),
			);
			return;
		}
		const amount = outstanding_amount(frm);
		if (amount <= 0) {
			LOG("skip: no outstanding amount on", frm.doctype, frm.docname);
			return;
		}

		LOG("rendering Pine Labs buttons on", frm.doctype, frm.docname, "amount=", amount, "flags=", flags);

		// add_custom_button is idempotent (shows the existing button when
		// called twice with the same label), so no extra dedup guard needed.
		if (flags.pay_on_machine) {
			frm.add_custom_button(__("Pay on Machine"), () => open_terminal_dialog(frm));
		}
		if (flags.send_payment_link) {
			frm.add_custom_button(__("Send Payment Link"), () => start_payment_link(frm, "all_methods"));
		}
	}

	function outstanding_amount(frm) {
		const candidates = ["outstanding_amount", "grand_total", "amount", "paid_amount"];
		for (const f of candidates) {
			const v = frm.doc[f];
			if (v != null) return Number(v) || 0;
		}
		return 0;
	}

	// ─────────────────────────────────────────────────────────────────
	// Terminal flow
	// ─────────────────────────────────────────────────────────────────

	function open_terminal_dialog(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __("Pay on Machine"),
			fields: [
				{
					fieldname: "mode_of_payment",
					fieldtype: "Link",
					label: __("Mode of Payment"),
					options: "Mode of Payment",
					reqd: 1,
					get_query() {
						// Pine Labs Terminal modes are identified by name — no
						// custom fields required. Keep this list in sync with
						// PINELABS_TERMINAL_MODES in pinelabs_fin/api/transaction.py.
						return {
							filters: {
								enabled: 1,
								name: ["in", ["PineLabs - Card", "PineLabs - UPI"]],
							},
						};
					},
				},
			],
			primary_action_label: __("Start Payment"),
			primary_action(values) {
				// Guard against a rapid double-click firing two payments before
				// the dialog finishes hiding (belt-and-braces on top of the
				// server-side source-doc lock).
				if (dialog.__pinelabs_submitting) return;
				dialog.__pinelabs_submitting = true;
				dialog.disable_primary_action();
				dialog.hide();
				start_terminal_payment(frm, values.mode_of_payment);
			},
		});
		dialog.show();
	}

	function start_terminal_payment(frm, mode_of_payment) {
		frappe.call({
			method: "pinelabs_fin.api.terminal.initiate_terminal_payment",
			args: {
				reference_doctype: frm.doctype,
				reference_name: frm.docname,
				mode_of_payment,
			},
			freeze: true,
			freeze_message: __("Sending request to terminal..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					show_error(__("Terminal payment failed"), msg.message || msg.error);
					if (msg.transaction_name) frm.reload_doc();
					return;
				}
				open_polling_modal(frm, msg.transaction_name);
			},
		});
	}

	// Thin wrapper retained for the auto-injected Sales Invoice / POS Invoice
	// button path — defers to the global utility below.
	function open_polling_modal(frm, transaction_name) {
		frappe.pinelabs.open_terminal_modal(transaction_name, {
			on_settled: () => frm && frm.reload_doc && frm.reload_doc(),
			on_cancel: () => frm && frm.reload_doc && frm.reload_doc(),
		});
	}

	// ─────────────────────────────────────────────────────────────────────
	// Public utilities — callable from any custom button, any custom page,
	// any external code. The auto-button uses these too; there is no
	// behavioural fork.
	//
	//   frappe.pinelabs.open_terminal_modal(transaction_name, options)
	//     Show the locked "Waiting for terminal" dialog for an existing
	//     Pinelabs Transaction. Polls + listens for realtime + Cancel.
	//
	//   frappe.pinelabs.start_terminal_payment(args, options)
	//     One call to (a) initiate the payment via the API and (b) open
	//     the modal automatically on success. The preferred high-level
	//     entry point for new code.
	// ─────────────────────────────────────────────────────────────────────
	// (Namespace `frappe.pinelabs` is set up at the top of the file,
	// before this IIFE runs, so callers can always assume it exists.)

	// Pages with their own overlays (POS Awesome / Vuetify) set
	// frappe.pinelabs.modal_z_index so our dialogs sit above them. Raise the
	// dialog together with ITS OWN backdrop only — touching other dialogs'
	// backdrops leaves a dark layer over the page that blocks every click.
	frappe.pinelabs.raise_dialog = function (dialog) {
		const z = frappe.pinelabs.modal_z_index;
		if (!z || !dialog || !dialog.$wrapper) return;
		const apply = () => {
			dialog.$wrapper.css("z-index", z + 1);
			const bs_modal = dialog.$wrapper.data("bs.modal");
			if (bs_modal && bs_modal._backdrop) $(bs_modal._backdrop).css("z-index", z);
		};
		apply();
		dialog.$wrapper.one("shown.bs.modal", apply);
	};

	frappe.pinelabs.open_terminal_modal = function (transaction_name, options) {
		LOG("[DEBUG] open_terminal_modal called for", transaction_name, "options=", options);
		// Already showing a modal for this txn — return the existing one so
		// the frappe.call auto-intercept doesn't stack a duplicate on top of
		// the helper-callback open (or vice versa).
		if (OPEN_TERMINAL_MODALS.has(transaction_name)) {
			LOG("[DEBUG] open_terminal_modal: registry hit, returning existing modal for", transaction_name);
			const existing = OPEN_TERMINAL_MODALS.get(transaction_name);
			// The realtime listener can open the modal before the initiate
			// response reaches the caller — attach the caller's callbacks
			// to the existing modal so they still fire on settle/cancel.
			if (options && existing.__pinelabs_add_listeners) {
				existing.__pinelabs_add_listeners(options);
			}
			return existing;
		}
		LOG("[DEBUG] open_terminal_modal: registry miss, creating new modal for", transaction_name);
		options = options || {};
		const settled_listeners = [];
		const cancel_listeners = [];
		const add_listeners = (opts) => {
			if (typeof opts.on_settled === "function") settled_listeners.push(opts.on_settled);
			if (typeof opts.on_cancel === "function") cancel_listeners.push(opts.on_cancel);
		};
		add_listeners(options);
		const call_all = (listeners, args) => listeners.forEach((fn) => {
			try {
				fn(...args);
			} catch (err) {
				console.error("[Pinelabs] terminal modal listener threw:", err);
			}
		});
		const on_settled = (...args) => call_all(settled_listeners, args);
		const on_cancel = (...args) => call_all(cancel_listeners, args);
		const show_alert = options.show_alert !== false;
		// `poll: false` opts out of the modal's internal 3s setInterval poll.
		// Useful when a wrapper API (e.g. fin_pos) already runs its own poll
		// against `check_terminal_status` — without this, both pollers hit
		// Pine Labs in parallel, doubling the gateway API rate. The modal
		// still finalizes correctly via the `pinelabs_txn_update` realtime
		// event (which mark_success / mark_failed / mark_cancelled publish
		// after_commit). Default is true to preserve existing behaviour.
		const enable_poll = options.poll !== false;

		let settled = false;  // true once a terminal status is observed (success/fail/cancel)
		const t0 = performance.now();
		const t = () => `+${Math.round(performance.now() - t0)}ms`;
		LOG(t(), "polling modal opened for", transaction_name);
		const modal = new frappe.ui.Dialog({
			title: __("Waiting for terminal"),
			static: true,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "body",
					options: `
						<div style="text-align:center; padding: 16px 8px;">
							<div class="text-muted" style="font-size: 0.85em;">${__("Transaction")}: ${frappe.utils.escape_html(transaction_name)}</div>
							<div style="margin: 16px 0;"><i class="fa fa-spinner fa-spin fa-2x"></i></div>
							<div id="pinelabs-modal-status" class="text-muted">${__("Waiting for customer to complete payment...")}</div>
						</div>
					`,
				},
			],
			primary_action_label: __("Cancel"),
			primary_action() {
				settled = true;
				cleanup();
				cancel_terminal_payment(transaction_name, (status) => {
					modal.hide();
					// The customer can pay in the gap between the click and
					// the cancel reaching Pine Labs — the server then marks
					// the txn SUCCESS. Report that, never "cancelled".
					if (status === "SUCCESS") {
						if (show_alert) {
							frappe.show_alert({ message: __("Customer already paid — payment SUCCESS"), indicator: "green" });
						}
						on_settled("SUCCESS", transaction_name);
						return;
					}
					on_cancel(transaction_name);
				});
			},
		});
		modal.show();
		frappe.pinelabs.raise_dialog(modal);
		modal.__pinelabs_add_listeners = add_listeners;
		OPEN_TERMINAL_MODALS.set(transaction_name, modal);

		// Lock the modal: backdrop click / Esc / X must NOT dismiss it.
		// `static: true` doesn't always cover every Bootstrap path, so
		// belt-and-brace with a hide.bs.modal guard.
		modal.$wrapper.modal({ backdrop: "static", keyboard: false });
		modal.$wrapper.find(".modal-header .btn-modal-close").hide();
		modal.$wrapper.on("hide.bs.modal.pinelabs", (e) => {
			if (!settled) {
				e.preventDefault();
				return false;
			}
		});

		// Run registered extensions so consumer apps can inject custom
		// content/buttons into the locked dialog (amount, terminal info,
		// branding, etc). Errors in one extension never abort the rest.
		const ext_ctx = {
			transaction_name,
			modal,
			body_$el: $(modal.body),
			get_status_el: () => document.getElementById("pinelabs-modal-status"),
			add_secondary_button(label, onclick) {
				return modal.set_secondary_action_label
					? modal.set_secondary_action_label(label, onclick)
					: modal.$wrapper.find(".modal-footer").prepend(
						$(`<button class="btn btn-default btn-sm pinelabs-ext-btn"></button>`)
							.text(label)
							.on("click", onclick),
					);
			},
		};
		(frappe.pinelabs.dialog_extensions || []).forEach((ext) => {
			try {
				ext.render(ext_ctx);
			} catch (err) {
				console.error(`[Pinelabs] dialog extension "${ext.id || "anonymous"}" threw:`, err);
			}
		});

		const status_el = () => document.getElementById("pinelabs-modal-status");
		const update_status_text = (status) => {
			const el = status_el();
			if (!el) return;
			el.textContent = status === "PENDING" || !status
				? __("Waiting for customer to complete payment...")
				: __("Status: {0}", [status]);
		};

		const finalize = (status, source, message) => {
			LOG(t(), "finalize called", { status, source, message, already_settled: settled });
			if (settled) return;
			settled = true;
			cleanup();

			// On FAILED (declined, aged-out, timeout — all roads converge
			// here), keep the modal open and offer Retry / Close so the
			// operator can re-try without navigating back to the source doc.
			// SUCCESS and CANCELLED auto-hide as before.
			if (status === "FAILED") {
				show_failure_state(message);
				return;
			}

			modal.hide();
			if (show_alert) {
				const indicator = status === "SUCCESS" ? "green" : "red";
				frappe.show_alert({ message: __("Payment {0}", [status]), indicator });
			}
			try {
				on_settled(status, transaction_name);
			} catch (err) {
				console.error("[Pinelabs] on_settled threw:", err);
			}
		};

		// Replace the spinner + status text with a failure card and swap
		// the modal's buttons to Retry (primary) / Close (secondary). The
		// modal stays LOCKED — the operator can't click around the rest of
		// the page until they explicitly choose one or the other.
		function show_failure_state(message) {
			LOG(t(), "showing failure state with message:", message);
			const reason = message || __("The terminal didn't complete the payment.");
			$(modal.body).html(`
				<div style="text-align:center; padding: 16px 8px;">
					<div class="text-muted" style="font-size: 0.85em;">${__("Transaction")}: ${frappe.utils.escape_html(transaction_name)}</div>
					<div style="margin: 16px 0; font-size: 32px; color: #d9534f;">
						<i class="fa fa-times-circle"></i>
					</div>
					<div style="font-weight: 600; margin-bottom: 8px;">${__("Payment failed")}</div>
					<div class="text-muted" style="word-break: break-word;">${frappe.utils.escape_html(reason)}</div>
				</div>
			`);
			// Swap primary action: Cancel-while-running → Retry-after-failure.
			modal.set_primary_action(__("Retry"), () => {
				retry_payment();
			});
			// Secondary: Close (acknowledges the failure, fires on_settled).
			modal.set_secondary_action_label(__("Close"));
			modal.set_secondary_action(() => {
				modal.hide();
				if (show_alert) {
					frappe.show_alert({ message: __("Payment FAILED"), indicator: "red" });
				}
				try {
					on_settled("FAILED", transaction_name);
				} catch (err) {
					console.error("[Pinelabs] on_settled threw:", err);
				}
			});
		}

		// Re-initiate the same payment (same source doc + Mode of Payment)
		// after a failure. Single round-trip via the server-side
		// `retry_failed_terminal_payment` endpoint — it looks up the
		// source doc + payment method on the server and re-runs initiate
		// atomically, so retry feels as snappy as the first attempt
		// (the old two-step get_value → initiate cost ~2x the latency).
		// The original on_settled / on_cancel are piped through so the
		// API-caller page sees the FINAL outcome, not the intermediate
		// failure.
		function retry_payment() {
			// Re-entrancy guard: a double-click on Retry must not fire two
			// initiate calls (each would start a fresh terminal payment).
			if (retry_payment.__inflight) return;
			retry_payment.__inflight = true;
			modal.hide();
			frappe.call({
				method: "pinelabs_fin.api.terminal.retry_failed_terminal_payment",
				args: { transaction_name },
				freeze: true,
				freeze_message: __("Restarting terminal payment..."),
				callback(r) {
					const msg = (r && r.message) || {};
					if (!msg.success || !msg.transaction_name) {
						frappe.msgprint({
							title: __("Retry failed"),
							message: msg.message || msg.error || __("Unknown error"),
							indicator: "red",
						});
						try { on_settled("FAILED", transaction_name); } catch (_e) {}
						return;
					}
					// Open the new modal explicitly so the original
					// on_settled / on_cancel are chained onto it. The
					// frappe.call interceptor's auto-open is scoped to
					// methods matching /initiate/i and won't fire for
					// `retry_failed_terminal_payment`, so this is the
					// single dialog-open call (no registry race).
					frappe.pinelabs.open_terminal_modal(msg.transaction_name, {
						on_settled,
						on_cancel,
					});
				},
			});
		}

		// Realtime: instant cross-context updates (e.g. cron reconcile,
		// other tab). Status transitions on our server publish this event.
		const realtime_handler = (data) => {
			LOG(t(), "realtime event received", data);
			if (!data || data.transaction_name !== transaction_name) {
				LOG(t(), "realtime event ignored (txn mismatch)");
				return;
			}
			update_status_text(data.status);
			if (["SUCCESS", "FAILED", "CANCELLED"].includes(data.status)) {
				finalize(data.status, "realtime", data.error_message);
			}
		};
		frappe.realtime.on("pinelabs_txn_update", realtime_handler);
		LOG(t(), "realtime subscribed to pinelabs_txn_update");

		// Fixed-interval poll using setInterval — fires every 3s regardless
		// of whether the previous response is back. This matches the old
		// fast-feedback pattern: chained setTimeout was effectively
		// (round-trip + 2s) per cycle, which stretched out the perceived
		// lag once the terminal finished. check_terminal_status is
		// idempotent, so overlapping in-flight requests are safe.
		const POLL_INTERVAL_MS = 3000;
		let poll_seq = 0;
		const tick = () => {
			if (settled) return;
			const seq = ++poll_seq;
			const tick_start = performance.now();
			LOG(t(), `poll #${seq} -> poll_terminal_status`);
			frappe.call({
				// Use the distinct `poll_terminal_status` alias (server-side
				// it just delegates to check_terminal_status) so consumer-app
				// shims that intercept frappe.call by method name don't
				// silently swallow our polls. Important when the consumer's
				// own poller isn't tracking THIS txn — e.g. after our Retry
				// button creates a new txn the consumer's PaymentModal does
				// not know about.
				method: "pinelabs_fin.api.terminal.poll_terminal_status",
				args: { transaction_name },
				callback(r) {
					const round_trip = Math.round(performance.now() - tick_start);
					LOG(t(), `poll #${seq} <- response (round-trip ${round_trip}ms)`, r && r.message);
					if (settled) {
						LOG(t(), `poll #${seq} dropped (already settled)`);
						return;
					}
					const msg = r.message || {};
					update_status_text(msg.status);
					if (["SUCCESS", "FAILED", "CANCELLED"].includes(msg.status)) {
						finalize(msg.status, `poll#${seq}`, msg.message);
					}
				},
			});
		};
		// First poll fires almost immediately to catch any state that
		// settled in the gap between transaction creation and the
		// realtime subscription being live.
		// `enable_poll === false` (set when the wrapper API marks the
		// response with `_pinelabs_external_poll: true`) skips the entire
		// internal poll loop; the modal then relies solely on realtime for
		// state transitions.
		let poll_interval = null;
		if (enable_poll) {
			setTimeout(tick, 500);
			poll_interval = setInterval(tick, POLL_INTERVAL_MS);
			LOG(t(), `polling armed (interval=${POLL_INTERVAL_MS}ms)`);
		} else {
			LOG(t(), "polling DISABLED via options.poll=false — realtime is sole state source");
		}

		function cleanup() {
			LOG(t(), "cleanup: detaching realtime + clearing interval");
			frappe.realtime.off("pinelabs_txn_update", realtime_handler);
			clearInterval(poll_interval);
			modal.$wrapper.off("hide.bs.modal.pinelabs");
			OPEN_TERMINAL_MODALS.delete(transaction_name);
			LOG(t(), "cleanup: done");
		}

		return modal;
	}

	// Calls on_done(final_status). If the cancel call itself fails, ask the
	// server for the txn's real status so a paid txn is never reported as
	// cancelled.
	function cancel_terminal_payment(transaction_name, on_done) {
		const done = (status) => on_done && on_done(status);
		frappe.call({
			method: "pinelabs_fin.api.terminal.cancel_terminal_payment",
			args: { transaction_name, reason: "user cancelled from modal" },
			freeze: true,
			freeze_message: __("Cancelling..."),
			always(r) {
				const status = r && r.message && r.message.status;
				if (status) {
					done(status);
					return;
				}
				frappe.call({
					method: "pinelabs_fin.api.terminal.poll_terminal_status",
					args: { transaction_name },
					always(r2) {
						done((r2 && r2.message && r2.message.status) || "CANCELLED");
					},
				});
			},
		});
	}

	// High-level helper: call the initiate API and, on success, open the
	// polling modal automatically. Use this from any custom button to get
	// the same UX as the built-in "Pay on Machine".
	//
	// Usage:
	//   frappe.pinelabs.start_terminal_payment(
	//     { reference_doctype, reference_name, mode_of_payment, amount? },
	//     { on_settled(status, txn) {...}, on_cancel(txn) {...}, on_error(msg) {...} }
	//   );
	frappe.pinelabs.start_terminal_payment = function (args, options) {
		options = options || {};
		const on_settled = options.on_settled || function () {};
		const on_cancel = options.on_cancel || function () {};
		const on_error = options.on_error || ((msg) => show_error(__("Terminal payment failed"), msg));

		return frappe.call({
			method: "pinelabs_fin.api.terminal.initiate_terminal_payment",
			args: args,
			freeze: true,
			freeze_message: __("Sending request to terminal..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					on_error(msg.message || msg.error || __("Unknown error"));
					return;
				}
				frappe.pinelabs.open_terminal_modal(msg.transaction_name, {
					on_settled: (status, txn) => on_settled(status, txn),
					on_cancel: (txn) => on_cancel(txn),
				});
			},
		});
	};

	// ─────────────────────────────────────────────────────────────────
	// Send Payment Link flow
	// ─────────────────────────────────────────────────────────────────

	// Thin wrapper for the auto-injected Send Payment Link button.
	// Defers to the global utility below — no behavioural fork.
	function start_payment_link(frm, flow) {
		frappe.pinelabs.start_payment_link(
			{
				reference_doctype: frm.doctype,
				reference_name: frm.docname,
				flow,
			},
			{
				on_link: () => frm.reload_doc(),
				on_error: (msg) => {
					show_error(__("Payment link failed"), msg);
					frm.reload_doc();
				},
			},
		);
	}

	// Public utility: call the initiate API for the Plural Pay-by-Link flow.
	// On success, shows the standard "Payment Link Generated" popup with the
	// URL. No polling modal — the customer pays asynchronously (SMS/email),
	// the webhook + 1-min cron finalize the transaction in the background.
	//
	// Usage:
	//   frappe.pinelabs.start_payment_link(
	//     { reference_doctype, reference_name, flow?: "all_methods", expiry_minutes?: 60 },
	//     {
	//       on_link(url, txn) { ... },    // success — link generated
	//       on_error(msg)     { ... },    // API rejected (validation, OAuth, ...)
	//       show_popup: true,             // default true; set false to suppress the standard popup
	//     }
	//   );
	frappe.pinelabs.start_payment_link = function (args, options) {
		options = options || {};
		const on_link = typeof options.on_link === "function" ? options.on_link : function () {};
		const on_error = typeof options.on_error === "function"
			? options.on_error
			: (msg) => show_error(__("Payment link failed"), msg);
		const show_popup = options.show_popup !== false;

		return frappe.call({
			method: "pinelabs_fin.api.plural.initiate_payment_link",
			args: args,
			freeze: true,
			freeze_message: __("Generating payment link..."),
			callback(r) {
				const msg = r.message || {};
				if (!msg.success) {
					on_error(msg.message || msg.error || __("Unknown error"));
					return;
				}
				if (show_popup && msg.payment_link_url) {
					show_link_result(msg.payment_link_url);
				}
				on_link(msg.payment_link_url, msg.transaction_name);
			},
		});
	};

	function show_link_result(url) {
		frappe.msgprint({
			title: __("Payment Link Generated"),
			message: `
				<p>${__("Pine Labs has sent the link to the customer via SMS/Email.")}</p>
				<p><a href="${frappe.utils.escape_html(url)}" target="_blank">${frappe.utils.escape_html(url)}</a></p>
			`,
			indicator: "green",
		});
	}

	// ─────────────────────────────────────────────────────────────────
	// Shared
	// ─────────────────────────────────────────────────────────────────

	function show_error(title, message) {
		frappe.msgprint({
			title,
			message: message || __("Unknown error"),
			indicator: "red",
		});
	}

	// ─────────────────────────────────────────────────────────────────
	// Auto-open the locked "Waiting for terminal" modal for ANY code path
	// that produces a Pine Labs Terminal initiate response — direct calls,
	// wrapper APIs (e.g. fin_pos.api.payments.initiate_terminal_payment),
	// any future wrapper. We detect by response shape, not method name:
	// any frappe.call whose response message has `success: true` and a
	// `transaction_name` matching the PLT- naming series is treated as a
	// terminal initiate. `open_terminal_modal` is idempotent, so callers
	// that already open it in their callback don't get a duplicate.
	// ─────────────────────────────────────────────────────────────────
	const PLT_TXN_RE = /^PLT-/i;
	const INITIATE_METHOD = "pinelabs_fin.api.terminal.initiate_terminal_payment";

	if (frappe.call && !frappe.call.__pinelabs_wrapped) {
		const __original_frappe_call = frappe.call;
		const wrapped = function (opts) {
			if (opts && typeof opts === "object") {
				// Belt-and-braces freeze for the direct method call — covers
				// the click→response gap even if the caller forgot it.
				if (opts.method === INITIATE_METHOD && opts.freeze === undefined) {
					opts.freeze = true;
					opts.freeze_message = opts.freeze_message || __("Sending request to terminal...");
				}
				const original_callback = opts.callback;
				const method_name = String((opts && opts.method) || "");
				// Scope the auto-open to INITIATE responses only. Poll
				// (check_*_status) and cancel (cancel_*) responses also
				// carry success=true + a PLT- txn name, but the dialog UX
				// for those is owned by the already-open dialog. Without
				// this filter, every fin_pos poll (every ~3s) tries to
				// re-open the modal — and once the modal has finalized,
				// each subsequent poll opens a NEW one, flashing a
				// "Waiting for terminal..." spinner over a terminated txn.
				const is_initiate_method = /\binitiate(_|\.)/i.test(method_name)
					|| /\.initiate_terminal_payment\b/i.test(method_name);
				opts.callback = function (r) {
					// Run caller's callback first so any options it passes to
					// open_terminal_modal (on_settled, on_cancel) win — our
					// fallback open below then no-ops via the registry.
					if (typeof original_callback === "function") {
						try {
							original_callback.apply(this, arguments);
						} catch (err) {
							console.error("[Pinelabs] original callback threw:", err);
						}
					}
					if (!is_initiate_method) return;
					// Response-shape detect: PLT-prefixed txn name + success
					// flag. Catches direct + wrapped APIs in one rule.
					const msg = (r && r.message) || {};
					const txn_name = msg.transaction_name || msg.transaction_reference;
					if (msg.success && typeof txn_name === "string" && PLT_TXN_RE.test(txn_name)) {
						// Wrapper APIs that run their own poll loop set
						// `_pinelabs_external_poll: true` on the response —
						// the modal then skips its internal setInterval and
						// relies solely on the realtime channel for state
						// transitions. This avoids doubling the rate of
						// `check_terminal_status` (and therefore the rate of
						// upstream Pine Labs `GetCloudBasedTxnStatus` calls)
						// when fin_pos and our modal both poll the same txn.
						const external_poll = !!msg._pinelabs_external_poll;
						LOG(
							"response-shape match → opening modal for",
							txn_name,
							"(method:", method_name + ",",
							"external_poll:", external_poll + ")",
						);
						try {
							frappe.pinelabs.open_terminal_modal(txn_name, {
								poll: !external_poll,
							});
						} catch (err) {
							console.error("[Pinelabs] auto-modal open failed:", err);
						}
					}
				};
			}
			return __original_frappe_call.apply(this, arguments);
		};
		wrapped.__pinelabs_wrapped = true;
		frappe.call = wrapped;
		LOG("frappe.call wrapped — auto-modal armed (response-shape match)");
	}

	// ─────────────────────────────────────────────────────────────────
	// Global realtime listener — covers callers that go through their own
	// server-side wrapper API (so `frappe.call` from the browser hits some
	// other method name and the interceptor above doesn't match). When
	// `mark_pending` flips a Terminal txn into PENDING, the server fires
	// `pinelabs_txn_update` to the owning user. We auto-open the locked
	// modal here. open_terminal_modal is idempotent, so callers that
	// already opened it (the helpers, the frappe.call auto-intercept)
	// don't get a duplicate.
	// ─────────────────────────────────────────────────────────────────
	LOG("[DEBUG] checking frappe.realtime:", {
		exists: !!frappe.realtime,
		has_on: !!(frappe.realtime && frappe.realtime.on),
		already_subscribed: !!(frappe.realtime && frappe.realtime.__pinelabs_initiated_subscribed),
	});
	if (frappe.realtime && frappe.realtime.on && !frappe.realtime.__pinelabs_initiated_subscribed) {
		frappe.realtime.on("pinelabs_txn_update", (data) => {
			LOG("[DEBUG] GLOBAL listener received pinelabs_txn_update:", data);
			if (!data || !data.transaction_name) {
				LOG("[DEBUG] GLOBAL listener SKIP: no transaction_name in data");
				return;
			}
			if (data.flow_type !== "Terminal") {
				LOG("[DEBUG] GLOBAL listener SKIP: flow_type is", data.flow_type, "(expected Terminal)");
				return;
			}
			if (data.status !== "PENDING") {
				LOG("[DEBUG] GLOBAL listener SKIP: status is", data.status, "(expected PENDING)");
				return;
			}
			LOG("[DEBUG] GLOBAL listener MATCH — opening modal for", data.transaction_name);
			try {
				frappe.pinelabs.open_terminal_modal(data.transaction_name);
			} catch (err) {
				console.error("[Pinelabs] global auto-modal open failed:", err);
			}
		});
		frappe.realtime.__pinelabs_initiated_subscribed = true;
		LOG("global pinelabs_txn_update listener armed for auto-modal");
	} else {
		LOG("[DEBUG] global listener NOT armed (already subscribed or frappe.realtime missing)");
	}

	LOG("pinelabs_buttons.js loaded");
})();
