# Numeric decision boundary

`requires_calculation` alone requires a consumed calculation, but does not make
that calculation decide the CHECK. This is intentional for mixed semantic
checks; it allowed a malformed model proof to pair a false amount comparison
with `SUPPORTED` when no decisive link was provided.

For a CHECK whose entire proposition is one numeric comparison, its trusted
catalog recipe can now specify:

```json
{
  "requires_calculation": true,
  "numeric_decision": {
    "operation": "LTE",
    "true_status": "SUPPORTED",
    "policy_operand": 1
  }
}
```

The existing recipe's resolver and evidence groups remain required. Operations
are `EQUAL`, `GREATER_THAN`, `GTE`, and `LTE`. Operand positions are zero-based;
`policy_operand` is optional for comparisons between business observations.

Runtime seals this configuration into the CHECK hash and sends it to Executor
and Verifier. Kernel requires exactly one decisive link to this CHECK's terminal
comparison, replays its result, and checks operation and polarity. Identical
operand references and singleton observation origins are rejected. When a
policy operand is declared, its calculation leaves must come from admitted
instruction Claims; the other side must come from non-instruction Claims.
Intermediate calculations retain their source lineage. A missing fact can
still produce `NOT_FOUND`.

The comparison-only contract does not prove the semantic choice of a field,
currency, applicable rule or document. The field programs below additionally
fix numeric field selection; semantic source applicability still requires review. Split unrelated
authorization, identity and lifecycle conditions into their own CHECKs before
using this contract. A cancellation may legitimately use `true_status:
CONTRADICTED`; the catalog must fix that direction.

The synthetic treasury probe separates amount and authorization. Catalog schema
8 migrates the default sales, purchase and invoice amount checks to the fixed
programs below. There are still six templates. Legacy contracts omit empty new
fields when serialized, preserving stored hashes and replay; they do not acquire
the new guarantee retroactively.

## Fixed field programs

`numeric_decision.steps` reuses `RegisteredPredicateStep` and the existing Decimal
`compute_witness` operations. Each program contains one to four steps and one
terminal comparison. `RECORD_FIELD` names an exact JSON pointer; `STEP` names an
earlier step. Kernel matches every witness, operand order, target, revision and
field against the sealed program. It rejects extra calculations and a terminal
link to the wrong step. `proposal_fields` maps consumed pointers to proposal value
keys and requires numeric equality with the immutable proposed action.

Each numeric CHECK owns one target. Its admitted record must have `source_id`
equal to that target and `record_model="derived.erp_amount_facts"`. The pointers
below are rooted in that record's `record_fields`; values should be exact decimal
strings. Records and their revisions/fingerprints use the existing admission and
snapshot mechanism. Do not substitute a raw Odoo record with different fields.

| Family | Required field comparisons |
| --- | --- |
| Acceptance | `/quantity * /list_price <= /budget`; `/quantity >= /min_quantity`; `/quantity <= /max_quantity`; `/lead_days >= /min_lead_days` |
| Sales release | `/price_unit == /list_price`; `/quantity * /price_unit <= /budget` |
| Purchase release | `/horizon_quantity >= /min_quantity`; `/horizon_quantity <= /max_quantity`; `/horizon_quantity <= /vendor_cap`; `/unit_cost == /tier_price` |
| Regular invoice | `abs(/amount_untaxed - /order_untaxed_total) <= /currency_tolerance` |
| Fixed downpayment | `abs(/amount_untaxed - /fixed_amount) <= /currency_tolerance` |
| Percentage downpayment | `abs(/amount_untaxed - /order_untaxed_total * /downpayment_ratio) <= /currency_tolerance` |
| Balance invoice | `abs(/amount_untaxed - (/order_untaxed_total - /authorized_downpayment_amount)) <= /currency_tolerance` |
| Every invoice | `abs(/amount_tax - /odoo_amount_tax) <= /currency_tolerance`; `abs(/amount_total - (/amount_untaxed + /odoo_amount_tax)) <= /currency_tolerance` |

These are 16 registered programs, each a separate CHECK. Invoice amount, native
tax amount and gross-total arithmetic can therefore receive different results.
Task Compiler selects the applicable template and source/action bindings. Runtime
expands these programs and their dependencies; neither model authors formulas.

Downpayment `/mode` must be `fixed_amount` or `percentage`. Unknown or absent mode
keeps both registered branches as evidence gaps. Acceptance flags
`/budget_enabled`, `/quantity_range_enabled`, `/minimum_lead_days_enabled`, and
purchase `/vendor_cap_enabled` disable their checks only when explicitly false
in the bound target view. A missing flag does not disable a check. Original policy
admission is separately reviewed, including whether these flags and the mode are
correct. Acceptance uses ALL for confirmation and per-target ANY of failed
eligibility conditions for cancellation; cancellation inverts the sealed numeric
polarity. An empty eligibility group is rejected before Executor starts.

## Executor responsibility and admission limit

The amount view is an admitted input, not an Executor output. The source producer
must supply original records/policies and provenance for normalized numbers,
currency/units, policy limits, selected tier, closed-horizon quantities, existing
downpayments and native Odoo tax results. A 10 percent rule is represented as the
ratio `0.10`; tolerance must be nonnegative and justified by the currency policy.
Semantic CHECKs review those mappings and the action's identity, scope, authority
and lifecycle. They do not calculate taxes or solve a purchasing plan.

Executor reads these frozen sources, binds exact fields, invokes the short
programs and submits evidence. It must not invent a view, reconstruct the Odoo
tax engine, choose an optimal supplier/tier, extend a horizon, or call unavailable
APIs to fill gaps. Missing sources/fields remain `NOT_FOUND`. Hashes establish
snapshot integrity, not the real-world authenticity of admitted data.

This branch does not add the live Odoo-to-view adapter or a production write gate.
Deploying these default templates requires that admission interface; synthetic
native-shaped fixtures only validate the Compiler side of this boundary.

For the durable public entry, an operator may set
`ERP_COMPILER_REVIEW_CATALOG` to a trusted full catalog JSON file. The child saves
its contents in `request.json` on start. The parent tool cannot supply a catalog
or numeric contract, and resume reads the saved request. Without this setting,
the existing default catalog is used.

`stage-usage.json` groups all recorded attempts by Task Compiler, Executor and
Fine Verifier. It is saved at each durable pause/completion/failure; the internal
probe also saves it after each model phase. Input includes cached input; reasoning
is a subset of output and must not be added again to total tokens. Failed or
incompletely reported attempts make the full stage total unknown, while
`known_token_lower_bound` preserves received usage. Kernel makes no model calls.
The Tau probe keeps parent usage separate, including failed runs, and writes its
diagnostic logs under the experiment directory.

Focused regression tests live in `test_kernel_evidence_review.py`,
`test_frontend_entry.py`, `test_usage_coverage.py`, and `test_child_run.py`.
Real probes use synthetic admitted materials and perform no Odoo writes.

## Transport diagnostics

Fine Verifier now streams its response through the existing SDK path, as Executor
already does. A historical failed privacy request returned HTTP 524 after about
127 seconds; the provider message said its upstream model was temporarily
unavailable. The later generic connection failures lacked underlying causes, so
their exact DNS/TLS/socket cause cannot be recovered from those old receipts.
Streaming passed a frozen replay lasting 145 seconds, but is not a guarantee
against provider outages. New failed receipts retain exception types and numeric
HTTP/OS codes through the cause chain, without adding headers or credentials.
The existing single transport retry and unlimited local model durations remain.

## Missing-evidence and tool protocol

A note-only `submit_check` is a valid completed protocol step. Batch admission now
checks actual new submissions, including these gaps, rather than requiring every
CHECK in the Executor summary's `completed_check_ids`. Fine Verifier still judges
the gap; an entirely unsubmitted CHECK still prevents verification. This fixes a
real 11-CHECK invoice candidate that submitted every CHECK but was rolled back
because one shared prerequisite had no evidence.

The model-facing `bind_record_field_claim` tool now receives identity only through
`locator.record_ref`. Runtime derives the identical Claim `source_id` and `subject`
from it. The underlying sandbox API and persisted Claim schema retain all their
identity, revision, scope and observed-value validation. The tool no longer asks
the model to copy one identity three times. Executor instructions permit working
one CHECK or a small independent group at a time, reusing existing grounded facts.
These interface changes have deterministic checks; their token savings have not
been established by a fresh comparative full-agent trial.
