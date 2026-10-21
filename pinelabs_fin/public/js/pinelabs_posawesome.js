// Pine Labs terminal payments inside POS Awesome.
//
// POS Awesome creates and submits the invoice in one call, so there is no
// invoice to reference while the customer pays. Flow:
//
//   1. Cashier puts the amount on a "PineLabs - Card" / "PineLabs - UPI"
//      payment row and clicks Submit.
//   2. POS Awesome runs window.posawesome_before_submit_payment_hooks (added
//      in Payments.vue). Our hook sends each PineLabs row to the terminal
//      (reference = the POS Profile, no Payment Entry — the payment is
//      recorded on the POS invoice's own payments table) and waits for
//      SUCCESS. Anything else throws, which aborts the submit.
//   3. On invoice submit, the server re-points the Pinelabs Transactions at
//      the invoice (doc_events → api/posawesome.link_on_invoice_submit).
//
// Every payment carries the sale's posa_client_request_id, and the server is
// asked what this sale already has before charging — so a retry after a
// failed submit, a page refresh or a new tab never charges twice, and a
// payment still running on the terminal is resumed instead of restarted.
//
// Payments that succeeded but never got an invoice are listed to the cashier
// when the POS page opens (and logged every 15 min by the scheduler).

(function () {
	"use strict";

	// Keep in sync with PINELABS_TERMINAL_MODES in pinelabs_fin/api/transaction.py.
	const TERMINAL_MODES = ["PineLabs - Card", "PineLabs - UPI"];
	const API = "pinelabs_fin.api.posawesome";
	const UNLINKED_CHECK_INTERVAL_MS = 5 * 60 * 1000;

	const flt2 = (v) => Math.round((parseFloat(v) || 0) * 100) / 100;
	// Plain text (frappe.format returns HTML, which shows up escaped in tables and toasts).
	const fmt = (v) => (typeof format_currency === "function" ? format_currency(flt2(v)) : flt2(v).toFixed(2));

	// POS Awesome (Vuetify) dialogs sit at ~2400, above Bootstrap's 1050.
	const POS_MODAL_Z_INDEX = 3000;
	const raise = (dialog) => frappe.pinelabs && frappe.pinelabs.raise_dialog && frappe.pinelabs.raise_dialog(dialog);

	function modal_callbacks(mode_of_payment, resolve, reject) {
		return {
			on_settled(status, transaction_name) {
				if (status === "SUCCESS") {
					resolve(transaction_name);
				} else {
					reject(new Error(__("Pine Labs {0} payment {1}", [mode_of_payment, status])));
				}
			},
			on_cancel() {
				reject(new Error(__("Pine Labs payment cancelled")));
			},
			on_error(message) {
				reject(new Error(message || __("Pine Labs payment could not be started")));
			},
		};
	}

	// First message from a frappe.throw response (HTTP 417), as plain text.
	function server_error_message(r) {
		try {
			const messages = JSON.parse((r && r._server_messages) || "[]");
			const first = messages.length ? JSON.parse(messages[0]) : null;
			const text = first && (first.message || first);
			if (text) return frappe.utils.strip_html(String(text));
		} catch (e) {
			/* fall through */
		}
		return (r && r.exception) || __("Pine Labs payment could not be started");
	}

	// Calls initiate directly (not start_terminal_payment) so a server-side
	// refusal (permission, no mapped machine, …) rejects the promise instead
	// of leaving POS Awesome's submit waiting forever.
	function take_terminal_payment({ pos_profile, mode_of_payment, amount, client_request_id }) {
		return new Promise((resolve, reject) => {
			if (!frappe.pinelabs || !frappe.pinelabs.open_terminal_modal) {
				reject(new Error(__("Pine Labs is not loaded. Reload the page and try again.")));
				return;
			}
			const callbacks = modal_callbacks(mode_of_payment, resolve, reject);
			frappe.call({
				method: "pinelabs_fin.api.terminal.initiate_terminal_payment",
				args: {
					reference_doctype: "POS Profile",
					reference_name: pos_profile,
					mode_of_payment,
					amount,
					create_payment_entry: 0,
					client_request_id,
				},
				freeze: true,
				freeze_message: __("Sending request to terminal..."),
				callback(r) {
					const msg = (r && r.message) || {};
					if (!msg.success || !msg.transaction_name) {
						callbacks.on_error(msg.message || msg.error);
						return;
					}
					frappe.pinelabs.open_terminal_modal(msg.transaction_name, callbacks);
				},
				error(r) {
					callbacks.on_error(server_error_message(r));
				},
			});
		});
	}

	// A payment for this sale is still running on the terminal (page was
	// refreshed, tab closed, …) — reopen its dialog instead of charging again.
	function resume_terminal_payment(transaction_name, mode_of_payment) {
		return new Promise((resolve, reject) => {
			frappe.pinelabs.open_terminal_modal(
				transaction_name,
				modal_callbacks(mode_of_payment, resolve, reject),
			);
		});
	}

	async function get_request_payments(client_request_id) {
		const r = await frappe.call({
			method: `${API}.get_request_payments`,
			args: { client_request_id },
		});
		return (r && r.message) || [];
	}

	async function before_submit({ invoice, pos_profile }) {
		if (frappe.pinelabs) frappe.pinelabs.modal_z_index = POS_MODAL_Z_INDEX;
		const rows = (invoice.payments || []).filter(
			(p) => TERMINAL_MODES.includes(p.mode_of_payment) && flt2(p.amount) !== 0,
		);
		const request_id = invoice.posa_client_request_id;
		let existing = [];
		try {
			existing = await get_request_payments(request_id);
		} catch (err) {
			// Cash-only sales must keep working offline; only a Pine Labs
			// payment needs the server.
			if (rows.length) throw err;
			return;
		}
		const paid = existing.filter((t) => t.status === "SUCCESS");

		// Customer already paid by Pine Labs for this sale, but the row was
		// removed or switched to another mode — don't let the money go
		// unrecorded.
		for (const txn of paid) {
			if (!rows.some((p) => p.mode_of_payment === txn.mode_of_payment)) {
				throw new Error(
					__("Pine Labs already charged {0} by {1} for this sale ({2}). Put {1} back with that amount, or refund it through Pine Labs.", [
						fmt(txn.amount), txn.mode_of_payment, txn.name,
					]),
				);
			}
		}
		if (!rows.length) return;

		if (invoice.is_return || rows.some((p) => flt2(p.amount) < 0)) {
			throw new Error(
				__("Pine Labs refunds can't be taken from POS. Use another mode of payment for returns."),
			);
		}

		const profile_name = (pos_profile && pos_profile.name) || invoice.pos_profile;
		if (!profile_name) {
			throw new Error(__("POS Profile not found for Pine Labs payment."));
		}

		for (const row of rows) {
			const amount = flt2(row.amount);
			const done = paid.find((t) => t.mode_of_payment === row.mode_of_payment);
			if (done) {
				if (flt2(done.amount) === amount) {
					continue; // charged on an earlier attempt of this sale
				}
				throw new Error(
					__("Pine Labs already charged {0} by {1} for this sale ({2}). Set {1} to {0}, or refund it through Pine Labs first.", [
						fmt(done.amount), row.mode_of_payment, done.name,
					]),
				);
			}

			const running = existing.find(
				(t) => ["INITIATED", "PENDING"].includes(t.status)
					&& (t.mode_of_payment === row.mode_of_payment
						|| (!t.mode_of_payment && t.payment_method
							&& row.mode_of_payment.toUpperCase().endsWith(String(t.payment_method).toUpperCase()))),
			);
			if (running) {
				await resume_terminal_payment(running.name, row.mode_of_payment);
				continue;
			}

			await take_terminal_payment({
				pos_profile: profile_name,
				mode_of_payment: row.mode_of_payment,
				amount,
				client_request_id: request_id,
			});
		}
	}

	// ─────────────────────────────────────────────────────────────────
	// Till notice: payments taken with no invoice
	// ─────────────────────────────────────────────────────────────────

	let last_unlinked_check = 0;

	function on_pos_page() {
		const route = (frappe.get_route && frappe.get_route()) || [];
		return route[0] === "posapp" || route[0] === "pos";
	}

	function check_unlinked_payments() {
		if (!on_pos_page() || Date.now() - last_unlinked_check < UNLINKED_CHECK_INTERVAL_MS) return;
		last_unlinked_check = Date.now();
		frappe.call({
			method: `${API}.get_unlinked_pos_payments`,
			callback(r) {
				show_unlinked_banner((r && r.message) || []);
			},
		});
	}

	// Small, non-blocking banner (never a modal on page load — POS Awesome's
	// own lock screen / dialogs must stay usable). Click it to open the list.
	function show_unlinked_banner(rows) {
		$("#pinelabs-unlinked-banner").remove();
		if (!rows.length || !on_pos_page()) return;
		const $banner = $(`
			<div id="pinelabs-unlinked-banner" role="button" style="position: fixed; bottom: 16px; left: 16px; z-index: 1500;
				background: #fff3cd; color: #664d03; border: 1px solid #ffda6a; border-radius: 8px;
				padding: 8px 12px; font-size: 13px; cursor: pointer; box-shadow: 0 2px 8px rgba(0,0,0,.15);">
				&#9888; ${__("{0} Pine Labs payment(s) without an invoice", [rows.length])} — <u>${__("View")}</u>
			</div>`);
		$banner.on("click", () => show_unlinked_dialog(rows));
		$("body").append($banner);
	}

	function show_unlinked_dialog(rows) {
		const esc = frappe.utils.escape_html;
		const body = rows.map((t) => `
			<tr>
				<td><a href="/app/pinelabs-transaction/${encodeURIComponent(t.name)}" target="_blank">${esc(t.name)}</a></td>
				<td>${esc(fmt(t.amount))}</td>
				<td>${esc(t.mode_of_payment || t.payment_method || "")}</td>
				<td>${esc(t.payment_id || "")}</td>
				<td>${esc(t.completed_at ? frappe.datetime.str_to_user(t.completed_at) : "")}</td>
				<td><button class="btn btn-xs btn-default pinelabs-link-invoice" data-txn="${esc(t.name)}">${__("Link to Invoice")}</button></td>
			</tr>`).join("");

		const dialog = new frappe.ui.Dialog({
			title: __("Pine Labs payments without an invoice"),
			size: "large",
			fields: [{
				fieldtype: "HTML",
				fieldname: "body",
				options: `
					<p>${__("These customers paid on the Pine Labs terminal, but the POS sale was not completed. For each one, create the sale in POS with the same Pine Labs payment and link it here, or refund the customer through Pine Labs.")}</p>
					<table class="table table-bordered table-condensed">
						<thead><tr>
							<th>${__("Transaction")}</th><th>${__("Amount")}</th><th>${__("Mode")}</th>
							<th>${__("Plutus Ref")}</th><th>${__("Paid At")}</th><th></th>
						</tr></thead>
						<tbody>${body}</tbody>
					</table>`,
			}],
			primary_action_label: __("Close"),
			primary_action() {
				dialog.hide();
			},
		});
		dialog.show();
		raise(dialog);

		dialog.$wrapper.on("click", ".pinelabs-link-invoice", function () {
			const txn = $(this).attr("data-txn");
			const prompt = frappe.prompt(
				[{
					fieldname: "invoice",
					fieldtype: "Link",
					options: "Sales Invoice",
					label: __("Sales Invoice"),
					reqd: 1,
				}],
				(values) => {
					frappe.call({
						method: `${API}.link_pos_invoice`,
						args: { transactions: [txn], invoice_doctype: "Sales Invoice", invoice_name: values.invoice },
						callback() {
							frappe.show_alert({ message: __("{0} linked to {1}", [txn, values.invoice]), indicator: "green" });
							dialog.hide();
							last_unlinked_check = 0;
							check_unlinked_payments();
						},
					});
				},
				__("Link {0} to Invoice", [txn]),
				__("Link"),
			);
			raise(prompt);
		});
	}

	// ─────────────────────────────────────────────────────────────────
	// Register
	// ─────────────────────────────────────────────────────────────────

	window.posawesome_before_submit_payment_hooks = window.posawesome_before_submit_payment_hooks || [];
	if (!window.__pinelabs_posa_hook_registered) {
		window.posawesome_before_submit_payment_hooks.push(before_submit);
		window.__pinelabs_posa_hook_registered = true;
	}

	// While on the POS page, lift EVERY Bootstrap dialog (Frappe error
	// messages, our terminal dialog, prompts) above POS Awesome's Vuetify
	// screens — each together with its own backdrop only.
	$(document).on("shown.bs.modal", ".modal", function () {
		if (!on_pos_page()) return;
		$(this).css("z-index", POS_MODAL_Z_INDEX + 1);
		const bs_modal = $(this).data("bs.modal");
		if (bs_modal && bs_modal._backdrop) $(bs_modal._backdrop).css("z-index", POS_MODAL_Z_INDEX);
	});

	function on_route_change() {
		if (frappe.pinelabs) {
			frappe.pinelabs.modal_z_index = on_pos_page() ? POS_MODAL_Z_INDEX : null;
		}
		if (!on_pos_page()) {
			$("#pinelabs-unlinked-banner").remove();
			return;
		}
		setTimeout(check_unlinked_payments, 1500);
	}

	$(document).on("app_ready", on_route_change);
	if (frappe.router && frappe.router.on) {
		frappe.router.on("change", on_route_change);
	}
})();
