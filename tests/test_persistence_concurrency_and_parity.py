import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from payroll_automation.core import (
    TRANS,
    Cycle,
    Employee,
    ExportInProgressError,
    MockBankAdapter,
    PayrollService,
    SQLitePayrollRepository,
)

ROOT = Path(__file__).parents[1]


def valid_cycle():
    return Cycle(
        "2026-08",
        [Employee("E1", "Synthetic E1", "SYNTH-A1", Decimal("100"))],
        True,
    )


def test_sqlite_repository_persists_cycle_approval_export_and_callback(tmp_path):
    path = str(tmp_path / "payroll.sqlite")
    service = PayrollService(
        MockBankAdapter(), repository=SQLitePayrollRepository(path)
    )
    cycle = valid_cycle()
    service.validate(cycle)
    service.approve(cycle, "reviewer")
    reference = service.export(cycle)
    service.bank_status(cycle, "accepted", "callback-1")

    reopened = SQLitePayrollRepository(path)
    restored = reopened.get(cycle.period)
    assert restored.state == "accepted"
    assert restored.export_reference == reference
    assert restored.callback_ids == {"callback-1"}
    assert len(restored.anomalies) == 0

    PayrollService(MockBankAdapter(), repository=reopened).bank_status(
        restored, "accepted", "callback-1"
    )
    assert (
        len(
            [
                entry
                for entry in reopened.get(cycle.period).audit
                if entry["new"] == "accepted"
            ]
        )
        == 1
    )


def test_parallel_approve_export_has_one_external_call():
    class BlockingAdapter(MockBankAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0
            self.lock = threading.Lock()
            self.entered = threading.Event()
            self.release = threading.Event()

        def send(self, cycle):
            with self.lock:
                self.calls += 1
            self.entered.set()
            assert self.release.wait(2)
            return super().send(cycle)

    adapter = BlockingAdapter()
    service = PayrollService(adapter, repository=SQLitePayrollRepository(":memory:"))
    cycle = valid_cycle()
    service.validate(cycle)
    workers = 8
    start = threading.Barrier(workers + 1)

    def approve_and_export():
        start.wait()
        service.approve(cycle, "reviewer")
        return service.export(cycle)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(approve_and_export) for _ in range(workers)]
        start.wait()
        assert adapter.entered.wait(2)
        adapter.release.set()
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except ExportInProgressError:
                outcomes.append("in-progress")

    assert adapter.calls == 1
    assert set(outcomes) <= {"mock-bank-" + cycle.batch_id, "in-progress"}
    assert "mock-bank-" + cycle.batch_id in outcomes
    assert service.repository.get(cycle.period).state == "exported"


def test_python_and_bsl_follow_canonical_transition_spec():
    spec = json.loads(
        (ROOT / "fixtures" / "payroll_transition_spec.json").read_text(encoding="utf-8")
    )
    expected = {
        state: sorted(targets) for state, targets in spec["transitions"].items()
    }
    python = {
        str(state): sorted(str(target) for target in targets if target != state)
        for state, targets in TRANS.items()
    }
    assert python == expected

    bsl = (ROOT / "bsl" / "PayrollControl.bsl").read_text(encoding="utf-8")
    assert "Если СтароеСостояние = НовоеСостояние Тогда Возврат Истина" in bsl
    for state, targets in expected.items():
        if targets:
            match = re.search(rf'Переходы\.Вставить\("{state}", "([^"]*)"\);', bsl)
            assert match and set(match.group(1).split(",")) == set(targets)
