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

The numeric-boundary development branch adds optional catalog-owned comparison
contracts and per-stage usage receipts. See
[`docs/numeric-decision-contract.md`](docs/numeric-decision-contract.md) for its
scope. Catalog schema 8 splits default ERP amount checks into 16 short, exact-field
programs within the existing six templates. Production use requires admitted
amount views and original-source provenance. The read-only native Odoo adapter in
`capabilities/odoo_amounts.py` projects actual captured records into those views;
it does not add a write gate.

Use `native_record_source(model, row, instance=...)` for actual Odoo read results,
then `erp_amount_source(target_ref, native_sources=..., policy=...)`. Keep the
original records and their field provenance in the admitted packet. The policy
must explicitly scope targets, enabled rules and bounds. Missing policy values
stay missing; unsupported records raise an admission error.

The adapter currently accepts one commercial order line, matching currencies
and units, no discounts, an explicitly selected supplier tier and a closed
purchase horizon. Invoice taxes come from native `tax_totals`, not reconstructed
tax arithmetic. Authorized prior downpayments must be posted native downpayment
invoices. It does not select supply plans, infer capacity, grant cancellation
authority or supply missing evidence. Native transition observations must name
the tested actor and record revision; a superuser preflight does not establish
another actor's posting rights.

After a batch rolls back or only partially commits, the durable child requires
an explicit CHECK correction through the existing `recheck_evidence_review`
tool before another resume. Later checkpoint/progress events do not clear that
failure boundary. Normal `PLAN_READY` resumes and interrupted work still use
the same child id.

In evidence-review mode, record-field tools take a locator and let Runtime read
the exact typed value from the admitted snapshot. A CHECK submission selects one
candidate Binding, or supplies a precise gap note without proof terms. Runtime
expands the Binding's Claim/Witness closure, including explicitly selected,
already submitted ancestors; it never inherits an ancestor's verdict. The
Verifier independently accepts or rejects the complete Binding and explicitly
attests whether it examined the full CHECK source scope. Runtime expands those
selections into the existing Kernel artifact; it cannot mark the scope examined
without that attestation. Source, revision, numeric and proof guards still apply.
Executor completion comes from real sandbox submissions, not generated tool
markup or a model-written summary. These changes leave the public parent tools,
six templates and Kernel proof format intact.
