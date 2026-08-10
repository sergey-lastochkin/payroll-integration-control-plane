from decimal import Decimal

from payroll_automation.core import (
    Cycle,
    Employee,
    FileBankAdapter,
    MockBankAdapter,
    PayrollService,
    RuleEngine,
)


def emp(i="1", account="A", salary="100"):
    return Employee(i, "Employee " + i, account, Decimal(salary))


def cycle(**kw):
    d = dict(period="2026-07", employees=[emp()], timesheet_complete=True)
    d.update(kw)
    return Cycle(**d)


def test_missing_timesheet():
    assert (
        RuleEngine().preflight(cycle(timesheet_complete=False))[0].code
        == "MISSING_TIMESHEET"
    )


def test_missing_account():
    assert any(
        x.code == "MISSING_BANK_DETAILS"
        for x in RuleEngine().preflight(cycle(employees=[emp(account="")]))
    )


def test_duplicate_employee():
    assert any(
        x.code == "DUPLICATE_EMPLOYEE"
        for x in RuleEngine().preflight(cycle(employees=[emp(), emp()]))
    )


def test_anomaly_threshold():
    assert RuleEngine().postcalc({"1": Decimal("150")}, {"1": Decimal("100")})


def test_approval_required():
    c = cycle()
    s = PayrollService(MockBankAdapter())
    s.validate(c)
    try:
        s.export(c)
        assert False
    except PermissionError:
        pass


def test_export_idempotent(tmp_path):
    c = cycle()
    s = PayrollService(FileBankAdapter(tmp_path))
    s.validate(c)
    s.approve(c, "reviewer")
    s.export(c)
    assert len(list(tmp_path.iterdir())) == 1


def test_repeated_callback_and_paid_reconciliation():
    c = cycle()
    s = PayrollService(MockBankAdapter())
    s.validate(c)
    s.approve(c, "r")
    s.export(c)
    s.bank_status(c, "accepted")
    s.bank_status(c, "accepted")
    assert (
        s.reconcile(c, [{"account": "A", "amount": "100"}]) == [] and c.state == "paid"
    )


def test_rejected_batch():
    c = cycle()
    s = PayrollService(MockBankAdapter())
    s.validate(c)
    s.approve(c, "r")
    s.export(c)
    s.bank_status(c, "rejected")
    assert c.state == "rejected"


def test_rerun_period_returns_same_cycle():
    s = PayrollService(MockBankAdapter())
    a = cycle()
    b = cycle()
    assert s.register(a) is s.register(b)
