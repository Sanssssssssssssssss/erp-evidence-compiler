# Compiler Generalization Observation — 2026-08-30

## Status

This is a frozen observation checkpoint, not a successful ERP Compiler design.
Development stopped after task 2042 failed the gate. Task 2016 was not run,
MCP was not connected, and no benchmark-wide evaluation was attempted.

The evidence supports one narrow conclusion:

> Structured action IR and source addressing helped the model understand the
> business contradiction, but the current single-CHECK proof contract could
> not express a Kernel-committable proof of that conclusion.

Do not treat the projected `NOT_FOUND` result as the business verdict. Both
runs ended in `UNCOMMITTED_ROLLBACK`; in the second run the private Fine
Verifier twice classified the proposal as `CONTRADICTED`.

## Where to look

### Product and runtime architecture

| Concern | Authoritative path |
|---|---|
| Compiler orchestration, CHECK frontier, budgets and checkpointing | `backend/app/compiler_runtime/runtime.py` |
| Strict proof commit gate | `backend/app/compiler_runtime/kernel.py` |
| Proof and proposal models | `backend/app/compiler_runtime/models.py` |
| Registered RequirementPack loading and validation | `backend/app/compiler_runtime/requirement_pack.py` |
| Source/proposal freshness | `backend/app/compiler_runtime/freshness.py` |
| Parent-visible checkpoint snapshot | `backend/app/compiler_runtime/snapshot.py` |
| OpenAI Agents SDK call adapter and streaming | `backend/app/runtime/agents_sdk.py` |
| Shared transient-error classification | `backend/app/runtime/retry.py` |
| Durable Tau child-run extension | `src/erp_agent_odoo/compiler_child/extension.py` |
| Registered ERP packs | `policies/*.json` |

The committed execution chain is:

```text
registered RequirementPack + immutable PreparedSource + ActionProposal
                            |
                            v
                      Task Compiler
                            |
                       ordered CHECKs
                            |
             Executor -> focused Fine Verifier
                            |
                       strict Kernel
                  / committed | rejected \
             checkpoint moves | frontier rolls back
```

### Experiment-only code

| Concern | Path |
|---|---|
| Treatment definitions | `tests/compiler_generalization/controls.py` |
| Bounded real-model runner and summary generation | `tests/compiler_generalization/control_probe.py` |
| Fifteen-case synthetic corpus builder | `tests/compiler_generalization/corpus.py` |
| Deterministic treatment checks | `tests/compiler_generalization/test_controls.py` |
| Reasoning/session retry checks | `tests/compiler_generalization/test_executor_reasoning.py` |

The `action_ir`, `action_ir_addressing`, and `action_graph` controls are
experiments. They are not registered product capabilities and must not be
read as the next architecture.

### Frozen raw observations

Two exact experiment directories are intentionally committed even though the
general artifacts directory remains ignored:

```text
tests/compiler_generalization/artifacts/
├─ action_ir_20260829T182830Z_4e06895a/
└─ action_ir_addressing_20260829T185407Z_5c0e2a6c/
```

Each run contains:

| Receipt | Meaning |
|---|---|
| `input.json` | public case plus derived treatment input |
| `requirement-pack.json` | exact frozen proof contract |
| `compiler-run/events.jsonl` | ordered tool lifecycle, hook rejection and frontier events |
| `compiler-run/model-calls.jsonl` | model-call timing, usage and captured reasoning/output |
| `compiler-run/checkpoint.json` | durable uncommitted Compiler state |
| `compiler-run/executor-sessions.sqlite` | authoritative Agents SDK session history |
| `run-summary.json` | offline aggregate; never substitutes for the raw receipts |
| `profile.json` | provider/model profile without a credential |
| `scored-index.json` | batch index and expected-vs-observed status |

The absolute receipt paths inside `run-summary.json` record the capture host.
On another clone, use the equivalent relative subtree above. The artifacts
were scanned before commit for token-like credential material; no credential
is stored. `api_key_present: true` records only a boolean.

## Exact configuration

### Model

| Field | Value |
|---|---|
| provider | `compatible` |
| base URL | `https://api.commandcode.ai/provider/v1` |
| model | `deepseek/deepseek-v4-flash` |
| reasoning | `high` |
| response mode | streamed for Compiler roles |
| full reasoning capture limit | 1,000,000 characters |

The API credential came from an existing external environment/configuration at
runtime. It was not copied into this repository or the observation artifacts.

### Compiler budgets

These values are defined near the top of
`backend/app/compiler_runtime/runtime.py`:

```text
EXECUTOR_MAX_TURNS = 24
CHECK_FRONTIER_ATTEMPT_CAP = 2
CHECK_MODEL_CALL_BUDGET = 4
```

There is one shared four-call model budget per CHECK across Executor and Fine
Verifier. A CHECK can try at most two speculative frontiers. Candidate proof
material remains private until Executor, Fine Verifier and Kernel agree.

### Proof contract under test

The strict `action_ir` run kept the registered general pack semantically
unchanged. Its effective structure was:

```text
complete proposed ERP action plan
  -> one Requirement
  -> one complete_action_plan facet
  -> one independently closable CHECK
  -> minimum proof terms: CLAIM + WITNESS
```

That single CHECK combined quantity lower bound, quantity upper bound, budget
coverage, action-policy conformance and completeness of the whole action plan.

Available arithmetic witness operators were:

```text
SUM, MULTIPLY, SUBTRACT, ABS_DIFF, GREATER_THAN
```

No single operator expressed the conjunction of all those qualitative and
quantitative obligations.

## What changed before these runs

### Deterministic runtime corrections

These changes address shared runtime behavior rather than task 2042:

1. Compiler roles can drain a streamed Agents SDK response.
2. An incomplete stream is retried in the same `SQLiteSession`; the retry adds
   a small runtime observation and retains accepted prior reasoning/tool items.
3. `stream ended before terminal chunk` is a transient transport fingerprint.
4. A same-CHECK frontier rollback retains immutable source-read state while
   rolling back speculative Claim, Binding, Witness and Submission material.

The source-read correction prevents a conversation that remembers a source
from being paired with a sandbox that falsely says the source was never read.
It does not weaken source admission or the Kernel.

### Treatments, not fixes

`action_ir`:

- converts the report-shaped proposal to an atomic executable action list;
- removes report-only fields;
- creates target source references for each action;
- leaves the original general proof pack unchanged.

`action_ir_addressing`:

- applies the same Action IR;
- renders JSON values as stable pointer lines for exact source addressing;
- still leaves the original general proof pack unchanged.

`action_graph` also changes proof signatures. Because it changes more than one
independent variable, it is not the strict Experiment A and is not part of the
two-run conclusion here.

## Result 1 — strict Action IR

Run:
`tests/compiler_generalization/artifacts/action_ir_20260829T182830Z_4e06895a/05_screened_order_flipped/`

Task and case:

```text
task: 2042_medium_06_screened_buy_only_mixed_seeded
case: 05_screened_order_flipped
control: action_ir
```

| Metric | Observed |
|---|---:|
| lifecycle | `UNCOMMITTED_ROLLBACK` |
| projected decision | `NOT_FOUND` |
| plan CHECKs | 1 |
| ordered events | 642 |
| `list_sources` | 1 |
| `read_source` | 12 |
| `bind_claim` | 222 |
| `compute_witness` | 73 |
| `submit_check` | 4 |
| wall time | 1,338,758.64 ms |

Hook rejections:

| Fingerprint | Count |
|---|---:|
| `LOCATOR_QUOTE_MISMATCH` | 73 |
| `SOURCE_NOT_READ` | 10 |
| `CLAIM_REFERENCE_NOT_FOUND` | 4 |
| `BINDING_REFERENCE_NOT_SUBMITTED` | 2 |
| `WITNESS_COMPUTATION_REJECTED` | 1 |
| `QUOTE_NOT_IN_SOURCE` | 1 |

Both Executor calls ended with `MaxTurnsExceeded: Max turns (24) exceeded`.
Only the completed Task Compiler call reported token usage in the aggregate:

| Usage | Observed |
|---|---:|
| prompt | 8,555 |
| cached input | 8,448 |
| output | 3,542 |
| reasoning | 3,280 |

Executor usage is `null`, not zero. The exception path did not expose final
aggregate usage for those calls. The session database retains accepted
conversation items but cannot manufacture missing provider totals.

Interpretation: the proposal became smaller, but the proof closure did not.
The model generated large amounts of mutually inconsistent speculative proof
material and never produced a legal terminal submission.

## Result 2 — Action IR plus structured addressing

Run:
`tests/compiler_generalization/artifacts/action_ir_addressing_20260829T185407Z_5c0e2a6c/05_screened_order_flipped/`

| Metric | Observed |
|---|---:|
| lifecycle | `UNCOMMITTED_ROLLBACK` |
| projected decision | `NOT_FOUND` |
| private Fine Verifier decision | `CONTRADICTED` twice |
| ordered events | 270 |
| model calls | 5 |
| `read_source` | 9 |
| `bind_claim` | 81 |
| `compute_witness` | 30 |
| `submit_check` | 4 |
| wall time | 1,283,950.06 ms |

Hook rejections:

| Fingerprint | Count |
|---|---:|
| `QUOTE_NOT_IN_SOURCE` | 12 |
| `LOCATOR_QUOTE_MISMATCH` | 6 |
| `CLAIM_REFERENCE_NOT_FOUND` | 6 |
| `SOURCE_NOT_READ` | 0 |

Complete provider-reported usage:

| Usage | Observed |
|---|---:|
| prompt | 911,674 |
| cached input | 775,552 |
| output | 166,713 |
| reasoning | 140,251 |
| summed call latency | 1,283,612.97 ms |

The addressing treatment reduced `LOCATOR_QUOTE_MISMATCH` from 73 to 6 and
the runtime correction removed `SOURCE_NOT_READ`. It also allowed the Fine
Verifier to recover the correct business contradiction:

```text
o01 quantity = 15, within inclusive range 15..23
15 * 203.87 = 3058.05
customer budget 3219.41 covers 3058.05
therefore o01 is eligible
proposal cancels o01
therefore the proposal contradicts the admitted policy
```

Both frontiers still failed Kernel commit with:

```text
TERMINAL_WITNESS_REQUIRED
KERNEL_RESULT_MISMATCH
```

The Fine Verifier emitted no `strong_status_links`. The individual arithmetic
witnesses established parts of the reasoning, but none represented the truth
status of the entire mixed business proposition. The strict Kernel therefore
failed closed as designed.

## Failure chain

```text
ActionProposal was readable
  -> source addressing mostly worked
  -> Executor produced facts and arithmetic witnesses
  -> Fine Verifier understood the contradiction
  -> no legal proof term denoted the whole CHECK status
  -> no strong terminal status link
  -> Kernel rejected the candidate
  -> both frontier attempts exhausted
  -> checkpoint remained uncommitted
  -> external projection displayed NOT_FOUND
```

The smallest failing layer in the second run is the proof-contract boundary
between semantic verification and Kernel commit. It is not provider auth,
MCP, Odoo, Harbor or benchmark scoring.

## Provider observation

An earlier local proxy route used `http://127.0.0.1:8317/v1`. It had one
CommandCode credential, an effective credential cooldown around 60 seconds,
and a maximum retry wait of 30 seconds. A long interrupted stream could put
the only credential into cooldown and make the next call return
`auth_unavailable`.

The two frozen runs above used CommandCode directly. A separate minimal direct
probe returned HTTP 200 with reasoning usage. The final Compiler rollback is
therefore not attributed to the proxy or provider.

## Verification performed

The final pre-push deterministic gate covered the changed Runtime, Compiler,
child-run, benchmark adapter and generalization tests:

```text
174 passed in 10.49s
```

Earlier focused gates were `56 passed` for Compiler/runtime and `10 passed`
for source-rollback/control behavior; the final 174-test gate supersedes them.

The first Windows test invocation used the global temporary directory and hit
`PermissionError`. Re-running with repository-local `--basetemp` passed. This
was a local test-filesystem issue, not a Compiler failure.

The Kernel was not relaxed, the turn budget was not increased, the verifier
was not modified to force a pass, and missing usage was not rewritten as zero.

## Current decision boundary

The shared runtime corrections are independently defensible. The experiment
controls are observations only. Continuing to add retries, prompt clauses,
locator fallbacks or turn budget around the single giant CHECK would be
symptom patching.

Before another model run, decide whether the Compiler frontend should replace
the universal whole-plan CHECK with registered ERP obligations that are:

1. deterministically selected from a tested RequirementPack;
2. independently closable;
3. explicit about qualitative versus quantitative proof terms;
4. bound to proposal, source snapshot and policy hashes;
5. small enough that the Kernel can validate a terminal relation directly.

No implementation of that new frontend is included in this checkpoint.
