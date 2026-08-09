# Project status

## IMPLEMENTED

Payroll cycle model and state machine; validation/anomaly rule engine; period/content idempotency; approval guard; deterministic mock/file batch adapters; checksum envelope; callback replay protection; reconciliation; 250-employee synthetic generator; BSL orchestration reference.

## TESTED

Python 3.12 unit and failure-path tests for every lifecycle gate, rule groups, batch/callback idempotency, mutation detection and reconciliation.

## NOT TESTED

BSL is not runtime-tested on a 1C/ZUP platform. Real ZUP documents, bank protocols, n8n and statutory calculation were not executed.

## EXTERNAL DEPENDENCIES

Python 3.12+ and `pytest` for tests; standard library for runtime core.

## KNOWN LIMITATIONS

The in-memory repository is a reference boundary, not durable production storage. Tax-ratio control is anomaly detection over ZUP results, not tax calculation.

## NEXT PRODUCTION STEPS

Map actual ZUP registers/documents in a test infobase, add durable storage and authenticated bank exchange, formalize approval roles, validate bank formats and run payroll acceptance cases with anonymized test copies.
