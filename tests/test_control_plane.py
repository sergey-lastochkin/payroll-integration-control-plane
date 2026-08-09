from decimal import Decimal

import pytest
from payroll_automation.core import (
    BatchCollisionError,
    Cycle,
    Employee,
    FileBankAdapter,
    MockBankAdapter,
    PayrollRepository,
    PayrollService,
    RuleEngine,
    synthetic_cycle,
)


def employee(identifier="E1", account="SYNTH-A1", salary="100"):
    return Employee(identifier, f"Synthetic {identifier}", account, Decimal(salary))


def valid_cycle():
    return Cycle("2026-08", [employee()], True)


def test_tax_ratio_anomaly():
    anomalies = RuleEngine().postcalc(
        {"E1": Decimal("100")},
        {"E1": Decimal("100")},
        taxes={"E1": Decimal("50")},
    )
    assert any(anomaly.code == "TAX_RATIO_ANOMALY" for anomaly in anomalies)


def test_duplicate_bank_account_is_blocking():
    cycle = Cycle("2026-08", [employee("E1"), employee("E2")], True)
    assert any(
        anomaly.code == "DUPLICATE_BANK_ACCOUNT"
        for anomaly in RuleEngine().preflight(cycle)
    )


def test_cycle_changed_after_validation_cannot_be_approved():
    cycle = valid_cycle()
    service = PayrollService(MockBankAdapter())
    service.validate(cycle)
    cycle.employees.append(employee("E2", "SYNTH-A2"))
    with pytest.raises(BatchCollisionError):
        service.approve(cycle, "reviewer")


def test_same_period_different_content_is_rejected():
    repository = PayrollRepository()
    repository.register(valid_cycle())
    changed = valid_cycle()
    changed.employees[0] = employee(salary="101")
    with pytest.raises(BatchCollisionError):
        repository.register(changed)


def test_second_export_returns_same_reference(tmp_path):
    cycle = valid_cycle()
    service = PayrollService(FileBankAdapter(tmp_path))
    service.validate(cycle)
    service.approve(cycle, "reviewer")
    first = service.export(cycle)
    second = service.export(cycle)
    assert first == second and len(list(tmp_path.iterdir())) == 1


def test_reconciliation_detects_missing_employee():
    cycle = Cycle("2026-08", [employee("E1"), employee("E2", "SYNTH-A2")], True)
    service = PayrollService(MockBankAdapter())
    service.validate(cycle)
    service.approve(cycle, "reviewer")
    service.export(cycle)
    service.bank_status(cycle, "accepted")
    details = service.reconcile_details(
        cycle, [{"account": "SYNTH-A1", "amount": "100"}]
    )
    assert details.missing == ("E2",) and cycle.state == "manual_check"


def test_callback_event_replay_is_noop():
    cycle = valid_cycle()
    service = PayrollService(MockBankAdapter())
    service.validate(cycle)
    service.approve(cycle, "reviewer")
    service.export(cycle)
    service.bank_status(cycle, "accepted", "callback-1")
    service.bank_status(cycle, "accepted", "callback-1")
    assert cycle.callback_ids == {"callback-1"}


def test_synthetic_dataset_is_deterministic():
    first = synthetic_cycle(count=300)
    second = synthetic_cycle(count=300)
    assert (
        len(first.employees) == 300
        and first.content_digest() == second.content_digest()
    )
