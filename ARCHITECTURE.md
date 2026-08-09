# Architecture

`Cycle` is the stateful aggregate. `RuleEngine` is a pure policy component; it returns anomalies and never updates ZUP-calculated values. `PayrollRepository` owns period uniqueness. `PayrollService` coordinates validation digest, human approval, export and callbacks. Bank adapters are side-effect boundaries; the default demonstrations write only synthetic local files. Reconciliation is explicit because bank acceptance is not proof that every employee payment settled.
