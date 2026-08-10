# 1C ZUP Payroll Automation

## What

A Python payroll-cycle control plane designed to consume ZUP-owned payroll results. ZUP remains authoritative for payroll and statutory tax calculation; this repository adds validation evidence, human approval, idempotent export, callbacks and reconciliation around that calculation.

## Why

Payroll exchange fails dangerously when validation, approval, export attempts and bank acceptance are represented by one flag. The explicit state machine prevents export before review, detects post-validation changes and preserves missing/unexpected reconciliation evidence.

## Architecture

- Payroll aggregate with guarded lifecycle from `draft` to `paid` or a terminal review state.
- Rule engine for timesheets, employee/account integrity, negative/zero payments, salary deltas and tax-ratio anomalies.
- In-memory and durable SQLite repositories enforcing one immutable dataset per payroll period, with persisted status history, approvals, callbacks and anomalies.
- Mock/file bank adapters with deterministic batch ID and checksum.
- Reconciliation service comparing expected active employees with synthetic statement rows.
- BSL reference module that orchestrates ZUP-owned results without recalculating tax.

## Key engineering decisions

- Human approval is a required transition, not a UI convention.
- A content digest prevents exporting a cycle changed after validation.
- Batch export and callback processing are idempotent.
- `approved → exporting` is a local claim before the external adapter; concurrent workers produce one local bank call and later callers receive the stored reference or an in-progress result.
- Python and illustrative BSL state maps are checked against a canonical JSON transition fixture.
- Rules report evidence and never silently alter financial values.
- Missing expected rows and unexpected bank rows both force `manual_check`.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
```

## Test

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

All employee names, accounts and amounts are generated synthetic fixtures; the 250-employee generator is a functional demo dataset, not a load benchmark.

CI also runs `ruff` and `compileall`; it does not call ZUP, a bank, or an external payroll service.

## Limitations

- BSL is **not runtime-tested on a 1C/ZUP platform**.
- The project deliberately does not reproduce ZUP payroll or NDFL calculation.
- No real bank protocol, signing, production storage, auth or scheduler is included.
- SQLite claim safety is tested for one process. A crash after a bank call and before local persistence remains at-least-once unless the downstream bank honours the deterministic batch ID.
