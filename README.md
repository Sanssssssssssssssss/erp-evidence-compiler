# ERP Evidence Compiler

A frozen experimental child-run for reviewing high-risk ERP actions before they
are executed.

This repository is a clean extraction from
[`odoo-erp-agent@520207c`](https://github.com/Sanssssssssssssssss/odoo-erp-agent/commit/520207c07622ce9f9216c61afaea2720088d9717).
It contains the Compiler runtime, registered ERP proof templates, the durable Tau
extension, focused tests, and human-written research reports. It does not contain
the Odoo UI, MCP deployment, Harbor entrants, benchmark artifacts, or secrets.

## Status

The current child-run is frozen as an experimental integration boundary:

- one public start interface;
- append-only checkpoints and ordered events;
- model-selected review templates;
- deterministic DAG validation and expansion;
- one logical Executor and one logical Fine Verifier per revision;
- deterministic Kernel replay;
- proposal, source, policy and plan hashes in the final DecisionProof.

It is not yet wired into a product Manager or an Odoo write gate.

## Public Agent interface

The parent Agent starts a review with only:

```json
{
  "task_objective": "Review whether this proposed high-risk action is supported.",
  "proposal_ref": "proposal:example:r1",
  "source_refs": ["policy:example", "evidence:example", "record:example"]
}
```

The parent does not select a Requirement Pack, author checks, or decide the
verdict. The child resolves the admitted proposal and sources, selects from the
registered catalog, saves `PLAN_READY`, and waits for the parent to resume the
same `compiler_run_id`.

## Runtime flow

```text
Manager intent + immutable proposal + admitted source references
  -> Task Compiler selects applicable registered templates
  -> Runtime validates bindings and expands the typed proof DAG
  -> PLAN_READY checkpoint
  -> Executor produces claims, bindings and witnesses with sandbox tools
  -> Fine Verifier independently assesses every CHECK
  -> Kernel deterministically replays topology, closure and hashes
  -> DecisionProof: SUPPORTED / CONTRADICTED / NOT_FOUND
  -> Parent inspects, stops, or creates a bounded revision
```

The model interprets business evidence. Deterministic code owns identity,
freshness, allowed templates, graph structure, transaction boundaries and final
proof integrity. Deterministic code does not manufacture the business verdict.

## Development

Python 3.12 is required.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
```

The optional Tau-backed real-model probe is installed separately:

```powershell
.\.venv\Scripts\python -m pip install -e ".[probe]"
$env:PYTHONPATH = "backend;src"
.\.venv\Scripts\python tests\compiler_child\model_probe.py `
  --env-file .env `
  --thinking high `
  --max-turns 12 `
  --manifest tests\compiler_child\fixtures\cancel_purchase_order.json `
  --artifact-root ..\compiler-archives\live-probes
```

Copy `.env.example` to `.env`; never commit credentials. Generated receipts are
ignored and should remain outside the repository.

## Verified snapshot

The source snapshot passed 141 focused deterministic tests. One CommandCode
trial completed Task Compiler, Executor, Fine Verifier and Kernel with three of
three checks supported. The raw local receipt is intentionally not published.

Known limits and historical observations are recorded under [`docs/`](docs/).

