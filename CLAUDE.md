# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`pinelabs_fin` is a **Frappe/ERPNext v15 custom app** that adds Pine Labs payments to any ERPNext document: tap a card on a physical terminal ("Pay on Machine") or send a Plural hosted checkout link ("Send Payment Link"), with the ERPNext Payment Entry created automatically on success.

The app is a directory inside a bench, not a standalone project. Almost every command runs from the **bench root**, not from this app directory:

- Bench root: `/home/finstein-emp/bench-pinelab`
- This app: `<bench>/apps/pinelabs_fin`
- Sites: `pinelab.local`, `pine.local`
- Sibling apps: `frappe`, `erpnext`, `fin_pos` (`fin_pos` is a **consumer** of this app's terminal API — see the shim note in `api/terminal.py:203`)

## Commands

All `bench` commands run from the bench root.

```bash
# Tests (Frappe test runner; site must allow tests once)
bench --site pinelab.local set-config allow_tests true
bench --site pinelab.local run-tests --app pinelabs_fin                        # whole app
bench --site pinelab.local run-tests --module pinelabs_fin.api.test_phase1     # one module
bench --site pinelab.local run-tests --module pinelabs_fin.api.test_phase1 \
      --test test_pending_on_response_code_zero                                # one test

# Apply changes
bench --site pinelab.local migrate    # schema + patches (doctype JSON / patches.txt changes)
bench build                           # rebuild JS/CSS assets (public/js changes)
bench restart                         # reload Python (production/supervisor)

# Dev
bench start                           # dev server; THE primary log channel (see below)
bench --site pinelab.local console    # Python REPL with frappe loaded
bench --site pinelab.local scheduler enable && bench --site pinelab.local scheduler resume

# Lint / format (run from this app dir; matches CI)
pre-commit run --all-files            # ruff, ruff-format, eslint, prettier
```

Style is enforced by ruff: **tabs for indent**, double quotes, line length 110. Do not reformat to spaces.

CI (`.github/workflows/`) also runs **Frappe's semgrep rules** and `pip-audit`. Two rules are deliberately suppressed inline with `# nosemgrep` — the manual `frappe.db.commit()` calls and the guest-allowed webhook. Both are justified in comments; don't "fix" them without reading why.

**`bench start` does not hot-reload Python.** After editing `api/*.py`, kill the web worker (`pkill -f "frappe serve"` — honcho respawns it) or restart `bench start`. It also does not pick up doctype JSON changes — those need `bench migrate`.

Print/log output from the payment flows goes to the **`bench start` terminal**, not reliably to `logs/pinelabs_paybylink.log` (Frappe's named-logger routing is bench-config dependent).

## Architecture

```
ERPNext doc (Sales Invoice, POS Invoice, …)
        │  "Pay on Machine" / "Send Payment Link" button
        ▼
public/js/pinelabs_buttons.js       ← loaded on every desk page (hooks.app_include_js)
        │
        ├──────────────────────┬─────────────────────────┐
        ▼                      ▼                         │
api/terminal.py          api/plural.py                   │
   (Pay on Machine)      (Send Payment Link)             │
        │                      │                         │
        ▼                      ▼                         │
api/pinelabs_client.py   api/plural_client.py            │
  Pine Labs Cloud          Plural REST + OAuth           │
        │                      │                         │
  foreground poll       Plural webhook → api/webhook.py  │
        +                      +                         │
   1-min cron            1-min cron                      │
        └──────────────────────┴─────────────────────────┘
                     ▼
            api/transaction.py   ← state machine + Payment Entry creation
```

### The one rule that matters

**`api/transaction.py` is the only module that writes `Pinelabs Transaction` rows or creates Payment Entries.** `terminal.py`, `plural.py`, and `webhook.py` all delegate to its `mark_pending` / `mark_success` / `mark_failed` / `mark_cancelled` helpers. Do not write transaction status or build a PE anywhere else — the concurrency, idempotency, and guard-rail logic all live in that one file and is easy to defeat by accident.

### State machine

`INITIATED → PENDING → SUCCESS | FAILED | CANCELLED` (terminal states are final). Enforced in two places that must stay in sync: `api/transaction.py` (the transitions) and the doctype controller `pinelabs_transaction.py` (`_ALLOWED_TRANSITIONS`, which rejects anything illegal — including manual UI edits to SUCCESS).

Two magic flags let internal code through the guards. Both are load-bearing:

- `doc.flags.pinelabs_internal_transition = True` — lets a save past the controller's transition check. Legitimate transitions set it; user edits don't have it.
- `pe.flags.pinelabs_internal_pe = True` — lets our own Payment Entry past `api/conflict_guard.py`, which is wired to Payment Entry's `validate` hook and otherwise **blocks any manual PE** while a Pinelabs Transaction for the same source doc is INITIATED/PENDING (prevents double-recording a payment).

### Concurrency

The foreground browser poll, the 1-minute cron, and the Plural webhook can all race to finalize the same transaction. Every transition starts with `_lock_and_reload()`, which does `SELECT … FOR UPDATE` and then **re-stamps `doc.modified` from that locked read**. That re-stamp is not redundant: under MariaDB's REPEATABLE READ, a plain `get_doc` after the lock still reads the pre-lock snapshot, so `save()` would fail its optimistic-lock check. Read the docstring at `api/transaction.py:980` before touching this.

Every transition function is idempotent (no-ops when already in the target state), which is what makes the racing callers safe.

### Payment Entry creation

`mark_success()` is the only path. It is idempotent against an existing PE with the same `reference_no` and against an already-linked PE. It deliberately calls `frappe.db.commit()` — the customer has *already been charged* at that point, so the PE and the SUCCESS flip must survive any later failure in the request. Don't remove it.

Which finalization happens depends on the source doctype and a per-call override:

- **PE-native doctypes** (`Sales Invoice`, `POS Invoice`, `Payment Request`) — built by ERPNext's `get_payment_entry`.
- **Any other doctype with `create_payment_entry=1`** — built by hand in `_build_custom_doctype_pe` (ERPNext's helper doesn't support them).
- **`finalize_via_payment_entry = 0` on the Payable Documents row** — the payment is recorded on the Pinelabs Transaction, but no Payment Entry is posted and the source doc is left untouched.
- **No Payable Documents row, no override** — permissive: the transaction records SUCCESS, the source doc is untouched.

The `create_payment_entry` API parameter (`1` / `0` / omitted) overrides the configured default per call and is stored on the transaction row.

Independently of all four paths, `_write_transaction_reference()` writes the transaction name (`PLT-…`) onto the source doc if the Payable Documents row sets **Transaction Reference Field**. It runs from `mark_success` for every path, and failures are logged rather than raised — the customer is already charged, so a bad field name must not roll back a SUCCESS. There is no longer a "mapped status field" finalization path; `Status Field` / `Paid Status Value` were removed (their DB columns linger, unread).

### Fail-fast before charging

Both `initiate_*` entry points run pre-flight checks **before** contacting Pine Labs, so a misconfiguration can never leave a customer charged against an invoice the app can't finalize:

- `validate_finalization_ready()` — refuses a doctype whose finalization path is misconfigured.
- `assert_mode_has_account_mapped()` — the chosen Mode of Payment must have a default account for the source doc's company (skipped when no PE will be posted), and for custom doctypes a Customer party must resolve.
- `get_active_for_machine()` (Terminal) — Pine Labs allows **one open transaction per terminal**: a new payment is refused while the machine has any INITIATED/PENDING transaction, from any document. The machine's config row is locked first so concurrent initiates serialize.

Preserve this ordering when adding a flow. A check that runs after the gateway call is worthless.

### Modes of Payment are identified by NAME

The app seeds exactly three rows and matches them **by name**, with **zero custom fields** on `Mode of Payment` (or on Sales Invoice / POS Invoice / Payment Entry / Customer / Contact — this is deliberate, for marketplace cleanliness):

`PineLabs - Card` (terminal code 1) · `PineLabs - UPI` (terminal code 10 = UPI Sale by default, or 11 = UPI Bharat QR — set by Pinelabs Settings → UPI Payment Mode Code; always resolve it via `get_terminal_code()`, never the dict) · `PineLabs - Payment Link` (all Plural payments)

Renaming any of them breaks the integration silently. The names are defined in `api/transaction.py` (`PINELABS_TERMINAL_MODES`, `PINELABS_PLURAL_MODE`) and seeded in `install.py` — keep those two in sync. Seeding runs from `after_install` (fresh installs) *and* the `install_pinelabs_modes` patch (upgrades), because Frappe marks patches as already-run on a fresh install without executing them.

### Configuration

Everything lives on the `Pinelabs Settings` **Single** doctype: `machines` (terminal credentials), `machine_mappings` (routing rules), `payable_doctypes` (which doctypes show buttons + customer-contact field mapping), `api_endpoints`. Terminal selection goes through `resolve_machine_for_context()` — Single Machine mode or Mapping mode (by user / warehouse / company / POS profile, falling back to the default machine).

## Landmines

**The whole live surface is small — five modules.** `api/` contains exactly: `transaction.py` (state machine), `terminal.py`, `plural.py`, `webhook.py`, plus the two HTTP clients and `conflict_guard.py` / `config.py` / `customer_contact.py`. A legacy parallel layer (`pos_endpoints.py`, `host_adapter.py`, `payment_bridge.py`, `services.py`, `webhooks.py`, `public/js/pinelabs_payment.js`, `public/js/form_button_injector.js`, the `pinelabs_test_terminal` page) was **deleted** in PRs #10/#11 (commits `fed2bcf`, `e40e3d1`). If you find code or docs referencing `pinelabs_fin.api.pos_endpoints.*`, `Pinelabs Transaction Log`, `Pinelabs Payment Mode`, or `Pinelabs Test Order`, it is stale — those no longer exist. The empty `pinelabs_payment_mode/` and `pinelabs_test_order/` directories linger only as leftover `__pycache__`.

**Documentation is inconsistent and lags the code.** `README.md` is accurate and current. `guide.md` is a detailed reference that is **partially stale** — it describes 4 Modes of Payment carrying custom fields (there are 3, with none), plus doctypes and patches (`pinelabs_bank`, `seed_pinelabs_banks.py`, …) that don't exist. `GUIDE.md` (capitalized — a *different* file on this case-sensitive filesystem) documents the **deleted** `pos_endpoints` API end to end and should be treated as obsolete. Trust the code and `README.md` over both.

**Child-table fields vanish from `frappe.db.get_list`.** Querying `Pinelabs Payable Doctype` with `get_list` silently returns `undefined` for Link and custom fields. `pinelabs_buttons.js` deliberately calls `frappe.client.get` on the `Pinelabs Settings` Single and iterates `doc.payable_doctypes` instead. Don't "optimize" that back to `get_list` — per-row toggles stop working silently.

## POS Awesome

`public/js/pinelabs_posawesome.js` (also in `app_include_js`) takes terminal payments inside POS Awesome. POS Awesome creates and submits the invoice in one call, so the payment runs **before** the invoice exists: it references the **POS Profile**, uses `create_payment_entry=0` (the payment is a row on the invoice's own payments table), and carries POS Awesome's `posa_client_request_id` in `Pinelabs Transaction.client_request_id`. It hooks in via `window.posawesome_before_submit_payment_hooks`, which needs a small patch in POS Awesome's `Payments.vue` (re-apply after updating POS Awesome).

`api/posawesome.py`: the till asks `get_request_payments` before charging (no double charge on retry/refresh), `link_on_invoice_submit` (Sales/POS Invoice `on_submit`) re-points the transaction at the invoice, and `alert_unlinked_pos_payments` (every 15 min) logs payments that never got an invoice. `access.assert_can_pay` lets cashiers listed on the POS Profile pay without write on the profile.

The two JS files are plain `/assets/...` paths, cached by browsers for 12h — bump the `?v=` in `hooks.app_include_js` whenever they change.

## Limitations

INR only. Refunds are not initiated by the app. `Pinelabs Transaction` is named `PLT-.YYYY.-`.
