# Compiler checkpoint before executable-evidence repair

This is a rollback checkpoint, **not a completed Compiler release**.

The current CommandCode validation covers ten public ERP-Bench material sets and
one external-catalog scenario. Routing/schema checks pass after the sales-stage
fix, but CHECK data dependencies, evidence extraction instructions and the ERP
execution contract are not yet complete. Executor and Fine Verifier invocations
in that validation are both zero.

## Reproduce the observation

- Report: `docs/COMPILER_CURRENT_VALIDATION_20260903.md`
- Totals: `tests/compiler_v1/artifacts/compiler_current_validation_20260903/observation-summary.json`
- Per-call sources, model output, reasoning, SDK responses, events and SQLite:
  the `receipt/` directories under that same folder.
- Test entry point: `tests/compiler_v1/proof_corpus/validate_current_compiler.py`

The checkpoint preserves existing Compiler-related working-tree changes and
this validation's receipts. Disposable pytest directories, local credentials,
unrelated compose changes and older untracked experiment outputs remain local.
No test was rerun to make this checkpoint.

## Repair acceptance boundary

Retain the DAG and independent Executor / Fine Verifier / Kernel roles. Reuse
existing source, claim and calculation tools instead of adding one bespoke
business resolver per CHECK. A published result requires source-grounded
evidence, complete dependencies and independent verification, not routing alone.
Pure structural checks and replay of existing receipts precede bounded new
CommandCode calls. Missing evidence remains missing; no oracle, guessed Odoo
state or default-success tool is admissible.
