from __future__ import annotations

import json
import random
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol


class PayrollState(StrEnum):
    DRAFT = "draft"
    VALIDATION_FAILED = "validation_failed"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    EXPORTING = "exporting"
    EXPORTED = "exported"
    ACCEPTED = "accepted"
    PAID = "paid"
    REJECTED = "rejected"
    MANUAL_CHECK = "manual_check"


STATES = {state.value for state in PayrollState}
TRANS: dict[str, set[str]] = {
    PayrollState.DRAFT: {PayrollState.VALIDATION_FAILED, PayrollState.READY_FOR_REVIEW},
    PayrollState.VALIDATION_FAILED: {
        PayrollState.VALIDATION_FAILED,
        PayrollState.READY_FOR_REVIEW,
    },
    PayrollState.READY_FOR_REVIEW: {
        PayrollState.APPROVED,
        PayrollState.VALIDATION_FAILED,
    },
    PayrollState.APPROVED: {PayrollState.EXPORTING},
    PayrollState.EXPORTING: {PayrollState.EXPORTED, PayrollState.MANUAL_CHECK},
    PayrollState.EXPORTED: {
        PayrollState.ACCEPTED,
        PayrollState.REJECTED,
        PayrollState.MANUAL_CHECK,
    },
    PayrollState.ACCEPTED: {
        PayrollState.PAID,
        PayrollState.REJECTED,
        PayrollState.MANUAL_CHECK,
    },
    PayrollState.PAID: set(),
    PayrollState.REJECTED: set(),
    PayrollState.MANUAL_CHECK: set(),
}


class PayrollError(RuntimeError):
    code = "PAYROLL_ERROR"


class IllegalStateError(PayrollError, ValueError):
    code = "ILLEGAL_STATE"


class BatchCollisionError(PayrollError):
    code = "BATCH_COLLISION"


class ExportInProgressError(PayrollError):
    code = "EXPORT_IN_PROGRESS"


@dataclass(frozen=True, slots=True)
class Employee:
    id: str
    name: str
    account: str
    salary: Decimal
    active: bool = True
    in_salary_project: bool = True


@dataclass(frozen=True, slots=True)
class PayrollLine:
    employee_id: str
    net_amount: Decimal
    tax_amount: Decimal = Decimal("0")
    account: str = ""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    old_state: str
    new_state: str
    source: str
    reason: str = ""


@dataclass(slots=True)
class Cycle:
    period: str
    employees: list[Employee]
    timesheet_complete: bool
    pending_hr_docs: int = 0
    state: str = PayrollState.DRAFT
    audit: list[dict[str, str] | AuditEntry] = field(default_factory=list)
    batch_id: str | None = None
    export_reference: str | None = None
    validation_digest: str | None = None
    callback_ids: set[str] = field(default_factory=set)
    anomalies: list[Anomaly] = field(default_factory=list)

    def key(self) -> str:
        return f"payroll:{self.period}"

    def content_digest(self) -> str:
        payload = [
            (
                employee.id,
                employee.account,
                format(employee.salary, "f"),
                employee.active,
            )
            for employee in sorted(self.employees, key=lambda value: value.id)
        ]
        raw = json.dumps(
            [self.period, self.timesheet_complete, self.pending_hr_docs, payload]
        )
        return sha256(raw.encode()).hexdigest()

    def move(self, new: str, source: str, reason: str = "") -> None:
        if new == self.state:
            return
        if new not in TRANS.get(self.state, set()):
            raise IllegalStateError(f"illegal {self.state}->{new}")
        old = self.state
        self.state = new
        self.audit.append({"old": old, "new": new, "source": source, "reason": reason})


@dataclass(frozen=True, slots=True)
class Anomaly:
    code: str
    severity: str
    employee: str | None
    description: str
    evidence: dict[str, object]


class RuleEngine:
    def preflight(self, cycle: Cycle) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        if not cycle.timesheet_complete:
            anomalies.append(
                Anomaly("MISSING_TIMESHEET", "error", None, "timesheet incomplete", {})
            )
        if cycle.pending_hr_docs:
            anomalies.append(
                Anomaly(
                    "PENDING_HR_DOCS",
                    "error",
                    None,
                    "unprocessed HR documents",
                    {"count": cycle.pending_hr_docs},
                )
            )
        employee_ids: set[str] = set()
        active_accounts: dict[str, str] = {}
        for employee in cycle.employees:
            if employee.id in employee_ids:
                anomalies.append(
                    Anomaly(
                        "DUPLICATE_EMPLOYEE",
                        "error",
                        employee.id,
                        "employee appears twice",
                        {},
                    )
                )
            employee_ids.add(employee.id)
            if employee.active and not employee.account:
                anomalies.append(
                    Anomaly(
                        "MISSING_BANK_DETAILS",
                        "error",
                        employee.id,
                        "missing bank account",
                        {},
                    )
                )
            if employee.salary < 0:
                anomalies.append(
                    Anomaly(
                        "NEGATIVE_PAYMENT",
                        "error",
                        employee.id,
                        "negative payment",
                        {"salary": str(employee.salary)},
                    )
                )
            if employee.active and employee.account:
                other = active_accounts.get(employee.account)
                if other and other != employee.id:
                    anomalies.append(
                        Anomaly(
                            "DUPLICATE_BANK_ACCOUNT",
                            "error",
                            employee.id,
                            "active employees share a bank account",
                            {"other_employee": other},
                        )
                    )
                active_accounts[employee.account] = employee.id
        return anomalies

    def postcalc(
        self,
        current: dict[str, Decimal],
        previous: dict[str, Decimal],
        threshold: Decimal = Decimal("0.30"),
        taxes: dict[str, Decimal] | None = None,
        expected_tax_ratio: Decimal = Decimal("0.13"),
        tax_tolerance: Decimal = Decimal("0.03"),
    ) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        for employee_id, amount in current.items():
            if amount == 0:
                anomalies.append(
                    Anomaly(
                        "UNEXPECTED_ZERO_SALARY",
                        "warning",
                        employee_id,
                        "zero salary",
                        {},
                    )
                )
            previous_amount = previous.get(employee_id)
            if (
                previous_amount
                and abs(amount - previous_amount) / abs(previous_amount) > threshold
            ):
                anomalies.append(
                    Anomaly(
                        "SALARY_CHANGE",
                        "warning",
                        employee_id,
                        "salary changed above threshold",
                        {"previous": str(previous_amount), "current": str(amount)},
                    )
                )
            if taxes and amount > 0 and employee_id in taxes:
                gross = amount + taxes[employee_id]
                actual_ratio = taxes[employee_id] / gross if gross else Decimal("0")
                if abs(actual_ratio - expected_tax_ratio) > tax_tolerance:
                    anomalies.append(
                        Anomaly(
                            "TAX_RATIO_ANOMALY",
                            "warning",
                            employee_id,
                            "withheld tax ratio outside configured band",
                            {
                                "actual": str(actual_ratio.quantize(Decimal("0.0001"))),
                                "expected": str(expected_tax_ratio),
                            },
                        )
                    )
        return anomalies


class BankAdapter(Protocol):
    def send(self, cycle: Cycle) -> str: ...


def batch_payload(cycle: Cycle) -> dict[str, object]:
    return {
        "schema": "synthetic-payroll-batch.v1",
        "batch_id": cycle.batch_id,
        "period": cycle.period,
        "payments": [
            {
                "employee": employee.id,
                "account": employee.account,
                "amount": str(employee.salary),
            }
            for employee in sorted(cycle.employees, key=lambda value: value.id)
            if employee.active
        ],
    }


class MockBankAdapter:
    def __init__(self) -> None:
        self.sent: dict[str, str] = {}

    def send(self, cycle: Cycle) -> str:
        batch_id = cycle.batch_id or sha256(cycle.key().encode()).hexdigest()[:16]
        cycle.batch_id = batch_id
        self.sent.setdefault(batch_id, "mock-bank-" + batch_id)
        return self.sent[batch_id]


class FileBankAdapter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def send(self, cycle: Cycle) -> str:
        batch_id = cycle.batch_id or sha256(cycle.key().encode()).hexdigest()[:16]
        cycle.batch_id = batch_id
        target = self.path / f"payroll-{batch_id}.json"
        payload = batch_payload(cycle)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        envelope = {
            "payload": payload,
            "checksum": sha256(canonical.encode()).hexdigest(),
        }
        raw = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        if target.exists() and target.read_text(encoding="utf-8") != raw:
            raise BatchCollisionError("existing batch has different content")
        if not target.exists():
            target.write_text(raw, encoding="utf-8")
        return target.name


class PayrollRepository:
    """In-memory single-process repository with atomic approval/export claims."""

    def __init__(self) -> None:
        self.cycles: dict[str, Cycle] = {}
        self._lock = threading.RLock()

    def register(self, cycle: Cycle) -> Cycle:
        with self._lock:
            existing = self.cycles.get(cycle.period)
            if existing and existing.content_digest() != cycle.content_digest():
                raise BatchCollisionError(
                    "period already registered with different source data"
                )
            self.cycles.setdefault(cycle.period, cycle)
            return self.cycles[cycle.period]

    def get(self, period: str) -> Cycle:
        with self._lock:
            return self.cycles[period]

    def save(self, cycle: Cycle) -> None:
        with self._lock:
            self.cycles[cycle.period] = cycle

    def approve_if_ready(self, period: str, actor: str) -> Cycle:
        with self._lock:
            cycle = self.get(period)
            if cycle.state in {
                PayrollState.APPROVED,
                PayrollState.EXPORTING,
                PayrollState.EXPORTED,
                PayrollState.ACCEPTED,
                PayrollState.PAID,
            }:
                return cycle
            if cycle.state != PayrollState.READY_FOR_REVIEW:
                raise IllegalStateError("approval requires reviewed cycle")
            if cycle.validation_digest != cycle.content_digest():
                raise BatchCollisionError("cycle changed after validation")
            cycle.move(PayrollState.APPROVED, actor, "human approval")
            return cycle

    def claim_export(self, period: str) -> Cycle | None:
        with self._lock:
            cycle = self.get(period)
            if cycle.state != PayrollState.APPROVED:
                return None
            cycle.move(
                PayrollState.EXPORTING, "export-claim", "external side effect claimed"
            )
            return cycle


class SQLitePayrollRepository:
    """Durable local persistence for the payroll control plane.

    The repository serializes writers with an RLock and ``BEGIN IMMEDIATE``.
    Claims are therefore single-process safe, while an external bank call still
    requires an idempotent batch ID for end-to-end duplicate protection.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS payroll_cycles (
                period TEXT PRIMARY KEY, content_digest TEXT NOT NULL, state TEXT NOT NULL,
                batch_id TEXT, export_reference TEXT, payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payroll_status_history (
                period TEXT NOT NULL, ordinal INTEGER NOT NULL, old_state TEXT NOT NULL,
                new_state TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
                PRIMARY KEY(period, ordinal)
            );
            CREATE TABLE IF NOT EXISTS payroll_approvals (
                period TEXT PRIMARY KEY, actor TEXT NOT NULL, approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payroll_callbacks (
                period TEXT NOT NULL, callback_id TEXT NOT NULL,
                PRIMARY KEY(period, callback_id)
            );
            CREATE TABLE IF NOT EXISTS payroll_anomalies (
                period TEXT NOT NULL, ordinal INTEGER NOT NULL, code TEXT NOT NULL,
                severity TEXT NOT NULL, employee TEXT, description TEXT NOT NULL,
                evidence TEXT NOT NULL, PRIMARY KEY(period, ordinal)
            );
            """
        )
        self.connection.commit()

    @contextmanager
    def _write_transaction(self):
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @staticmethod
    def _audit_data(entry: dict[str, str] | AuditEntry) -> dict[str, str]:
        if isinstance(entry, AuditEntry):
            return {
                "old": entry.old_state,
                "new": entry.new_state,
                "source": entry.source,
                "reason": entry.reason,
            }
        return dict(entry)

    @staticmethod
    def _payload(cycle: Cycle) -> str:
        return json.dumps(
            {
                "period": cycle.period,
                "employees": [
                    {
                        **asdict(employee),
                        "salary": str(employee.salary),
                    }
                    for employee in cycle.employees
                ],
                "timesheet_complete": cycle.timesheet_complete,
                "pending_hr_docs": cycle.pending_hr_docs,
                "state": cycle.state,
                "audit": [
                    SQLitePayrollRepository._audit_data(entry) for entry in cycle.audit
                ],
                "batch_id": cycle.batch_id,
                "export_reference": cycle.export_reference,
                "validation_digest": cycle.validation_digest,
                "callback_ids": sorted(cycle.callback_ids),
                "anomalies": [
                    {**asdict(anomaly), "evidence": anomaly.evidence}
                    for anomaly in cycle.anomalies
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _decode(payload: str) -> Cycle:
        data = json.loads(payload)
        return Cycle(
            period=data["period"],
            employees=[
                Employee(**{**employee, "salary": Decimal(employee["salary"])})
                for employee in data["employees"]
            ],
            timesheet_complete=data["timesheet_complete"],
            pending_hr_docs=data["pending_hr_docs"],
            state=data["state"],
            audit=data["audit"],
            batch_id=data["batch_id"],
            export_reference=data["export_reference"],
            validation_digest=data["validation_digest"],
            callback_ids=set(data["callback_ids"]),
            anomalies=[Anomaly(**anomaly) for anomaly in data["anomalies"]],
        )

    def _get_unlocked(self, period: str) -> Cycle:
        row = self.connection.execute(
            "SELECT payload FROM payroll_cycles WHERE period=?", (period,)
        ).fetchone()
        if row is None:
            raise KeyError(period)
        return self._decode(row["payload"])

    def _write_unlocked(self, cycle: Cycle) -> None:
        payload = self._payload(cycle)
        audit = [self._audit_data(entry) for entry in cycle.audit]
        self.connection.execute(
            """
            INSERT INTO payroll_cycles(period,content_digest,state,batch_id,export_reference,payload,updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(period) DO UPDATE SET content_digest=excluded.content_digest,
              state=excluded.state,batch_id=excluded.batch_id,
              export_reference=excluded.export_reference,payload=excluded.payload,
              updated_at=excluded.updated_at
            """,
            (
                cycle.period,
                cycle.content_digest(),
                cycle.state,
                cycle.batch_id,
                cycle.export_reference,
                payload,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.connection.execute(
            "DELETE FROM payroll_status_history WHERE period=?", (cycle.period,)
        )
        self.connection.executemany(
            "INSERT INTO payroll_status_history VALUES(?,?,?,?,?,?)",
            [
                (
                    cycle.period,
                    ordinal,
                    str(entry["old"]),
                    str(entry["new"]),
                    str(entry["source"]),
                    str(entry.get("reason", "")),
                )
                for ordinal, entry in enumerate(audit)
            ],
        )
        self.connection.execute(
            "DELETE FROM payroll_callbacks WHERE period=?", (cycle.period,)
        )
        self.connection.executemany(
            "INSERT INTO payroll_callbacks VALUES(?,?)",
            [(cycle.period, callback_id) for callback_id in sorted(cycle.callback_ids)],
        )
        self.connection.execute(
            "DELETE FROM payroll_anomalies WHERE period=?", (cycle.period,)
        )
        self.connection.executemany(
            "INSERT INTO payroll_anomalies VALUES(?,?,?,?,?,?,?)",
            [
                (
                    cycle.period,
                    ordinal,
                    anomaly.code,
                    anomaly.severity,
                    anomaly.employee,
                    anomaly.description,
                    json.dumps(anomaly.evidence, sort_keys=True),
                )
                for ordinal, anomaly in enumerate(cycle.anomalies)
            ],
        )
        approval = next(
            (entry for entry in audit if entry["new"] == PayrollState.APPROVED),
            None,
        )
        if approval:
            self.connection.execute(
                "INSERT OR REPLACE INTO payroll_approvals VALUES(?,?,?)",
                (
                    cycle.period,
                    approval["source"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def register(self, cycle: Cycle) -> Cycle:
        with self._write_transaction():
            try:
                existing = self._get_unlocked(cycle.period)
            except KeyError:
                self._write_unlocked(cycle)
                return cycle
            if existing.content_digest() != cycle.content_digest():
                raise BatchCollisionError(
                    "period already registered with different source data"
                )
            return existing

    def get(self, period: str) -> Cycle:
        with self._lock:
            return self._get_unlocked(period)

    def save(self, cycle: Cycle) -> None:
        with self._write_transaction():
            self._write_unlocked(cycle)

    def approve_if_ready(self, period: str, actor: str) -> Cycle:
        with self._write_transaction():
            cycle = self._get_unlocked(period)
            if cycle.state in {
                PayrollState.APPROVED,
                PayrollState.EXPORTING,
                PayrollState.EXPORTED,
                PayrollState.ACCEPTED,
                PayrollState.PAID,
            }:
                return cycle
            if cycle.state != PayrollState.READY_FOR_REVIEW:
                raise IllegalStateError("approval requires reviewed cycle")
            if cycle.validation_digest != cycle.content_digest():
                raise BatchCollisionError("cycle changed after validation")
            cycle.move(PayrollState.APPROVED, actor, "human approval")
            self._write_unlocked(cycle)
            return cycle

    def claim_export(self, period: str) -> Cycle | None:
        with self._write_transaction():
            cycle = self._get_unlocked(period)
            if cycle.state != PayrollState.APPROVED:
                return None
            cycle.move(
                PayrollState.EXPORTING, "export-claim", "external side effect claimed"
            )
            self._write_unlocked(cycle)
            return cycle


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    unexpected: tuple[dict[str, object], ...]


class PayrollService:
    def __init__(
        self,
        adapter: BankAdapter,
        engine: RuleEngine | None = None,
        repository: PayrollRepository | None = None,
    ) -> None:
        self.adapter = adapter
        self.engine = engine or RuleEngine()
        self.repository = repository or PayrollRepository()
        self.cycles = getattr(self.repository, "cycles", {})

    @staticmethod
    def _sync(target: Cycle, source: Cycle) -> Cycle:
        """Keep the caller's aggregate current after a durable CAS operation."""
        if target is source:
            return target
        for item in fields(Cycle):
            setattr(target, item.name, getattr(source, item.name))
        return target

    def register(self, cycle: Cycle) -> Cycle:
        return self.repository.register(cycle)

    def validate(self, cycle: Cycle) -> list[Anomaly]:
        cycle = self.register(cycle)
        anomalies = self.engine.preflight(cycle)
        cycle.anomalies = anomalies
        if anomalies:
            cycle.move(
                PayrollState.VALIDATION_FAILED, "validation", "blocking anomalies"
            )
            self.repository.save(cycle)
            return anomalies
        cycle.validation_digest = cycle.content_digest()
        cycle.move(
            PayrollState.READY_FOR_REVIEW, "validation", "all blocking rules passed"
        )
        self.repository.save(cycle)
        return []

    def approve(self, cycle: Cycle, actor: str) -> None:
        if not actor.strip():
            raise ValueError("approval actor is required")
        if cycle.validation_digest != cycle.content_digest():
            raise BatchCollisionError("cycle changed after validation")
        approved = self.repository.approve_if_ready(cycle.period, actor)
        self._sync(cycle, approved)

    def export(self, cycle: Cycle) -> str:
        current = self.repository.get(cycle.period)
        if current.state in {
            PayrollState.EXPORTED,
            PayrollState.ACCEPTED,
            PayrollState.PAID,
        }:
            if not current.export_reference:
                raise BatchCollisionError("exported state has no reference")
            self._sync(cycle, current)
            return current.export_reference
        if current.state == PayrollState.EXPORTING:
            raise ExportInProgressError("another worker owns the export claim")
        if current.state != PayrollState.APPROVED:
            raise PermissionError("human approval required")
        claimed = self.repository.claim_export(cycle.period)
        if claimed is None:
            current = self.repository.get(cycle.period)
            if current.export_reference:
                self._sync(cycle, current)
                return current.export_reference
            raise ExportInProgressError("another worker owns the export claim")
        try:
            reference = self.adapter.send(claimed)
        except Exception:
            # The external outcome is unknown; retain the claim for reconciliation.
            self.repository.save(claimed)
            self._sync(cycle, claimed)
            raise
        claimed.export_reference = reference
        claimed.move(PayrollState.EXPORTED, "bank-export", reference)
        self.repository.save(claimed)
        self._sync(cycle, claimed)
        return reference

    def bank_status(
        self, cycle: Cycle, status: str, event_id: str | None = None
    ) -> None:
        current = self.repository.get(cycle.period)
        callback_id = event_id or f"{current.batch_id}:{status}"
        if callback_id in current.callback_ids:
            self._sync(cycle, current)
            return
        current.move(status, "bank-callback", callback_id)
        current.callback_ids.add(callback_id)
        self.repository.save(current)
        self._sync(cycle, current)

    def reconcile_details(
        self, cycle: Cycle, rows: list[dict[str, object]]
    ) -> ReconciliationResult:
        current = self.repository.get(cycle.period)
        expected = {
            (employee.account, str(employee.salary)): employee.id
            for employee in current.employees
            if employee.active
        }
        seen: set[str] = set()
        unexpected: list[dict[str, object]] = []
        for row in rows:
            employee_id = expected.get(
                (str(row.get("account", "")), str(row.get("amount")))
            )
            if employee_id:
                seen.add(employee_id)
            else:
                unexpected.append(row)
        missing = sorted(set(expected.values()) - seen)
        result = ReconciliationResult(
            tuple(sorted(seen)), tuple(missing), tuple(unexpected)
        )
        if result.missing or result.unexpected:
            current.move(
                PayrollState.MANUAL_CHECK,
                "reconciliation",
                "missing or unexpected rows",
            )
        elif current.state == PayrollState.ACCEPTED:
            current.move(PayrollState.PAID, "reconciliation", "all rows matched")
        self.repository.save(current)
        self._sync(cycle, current)
        return result

    def reconcile(
        self, cycle: Cycle, rows: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        return list(self.reconcile_details(cycle, rows).unexpected)


def synthetic_cycle(period: str = "2026-07", count: int = 250, seed: int = 17) -> Cycle:
    random_generator = random.Random(seed)
    employees = [
        Employee(
            id=f"E-{index:04d}",
            name=f"Synthetic Employee {index:04d}",
            account=f"SYNTH-ACCOUNT-{index:06d}",
            salary=Decimal(60_000 + random_generator.randrange(-5_000, 20_000)),
        )
        for index in range(count)
    ]
    return Cycle(period, employees, timesheet_complete=True)
