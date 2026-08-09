# Testing

After the README test install, run `.venv/bin/python -m pytest -q`. Tests cover incomplete timesheets, HR documents, missing/duplicate accounts and employees, negative/zero payouts, salary delta and tax ratio anomalies; state guards; approval; changed content; idempotent export/callback; rejection; paid and manual-check reconciliation; and deterministic synthetic data.

BSL is statically inspected only; no 1C runtime is available in the local demo.
