# ADR-0002 — "Byte-identical replay" means replay-from-record, never regenerate

- **Status:** Accepted
- **Date:** 2026-10-31
- **Resolves:** an unflagged internal contradiction between PLAN.md §Daily schedule and
  PLAN.md §Verification

## Context

Two requirements in the plan could not both be satisfied, and nothing said so:

- **§Daily schedule** makes prefix-cache reuse the load-bearing optimisation of the committee
  stage — the shared market-context block is prefilled once (2.7 s) instead of 25 times (67 s).
  Without it the committee does not fit its 20:45–22:05 window.
- **§Verification** requires `ta daily --date 2026-10-15` to reproduce that day's recommendations
  **byte-identically**.

These are incompatible under re-execution. llama.cpp guarantees seed-reproducibility only for an
identical decode schedule, and continuous batching varies that schedule. The batch-invariant PR
(**#16016**) has been an open draft since 2025-09-15 and is **CUDA-only**, so it will not serve
this Metal machine at any future date. The maintainer's stated position: *"I don't want to
maintain guarantees for bit-for-bit identical results as the batch size is varied."* The
suggested workaround — a single server slot with **prompt caching disabled** — surrenders exactly
the optimisation the schedule depends on.

Left unresolved, M9 would ship with a gate that cannot pass, discovered at the worst moment.

## Decision

**Byte-identical replay is replay-from-record.** The sampled outputs of every generation are
stored, and replay re-derives from them. Regeneration is never attempted.

```
ta daily --date 2026-10-15 --replay
```

re-runs every **deterministic** stage — dossier render, risk gates, sizing, cost model, V1–V9
validators — against the **recorded** LLM outputs, and asserts byte-equality with what was
published, raising on divergence.

**It must re-derive, never recall.** A replay that returns the stored final answer passes the
gate while proving nothing: a regression in the dossier renderer, the risk gates, the cost model
or a validator would sail through undetected. This failure mode is not hypothetical — it is
exactly what DBOS's workflow-ID memoization does, and it was the reason that option scored poorly
on the audit lens.

## What must be recorded, per generation

| Field | Why it is load-bearing |
| --- | --- |
| prompt bytes + sha256 | the input V2 checks numeric literals against |
| GBNF grammar sha256 | hashing the committee source does **not** catch prompt or grammar drift |
| sampling params + seed | insufficient alone, but nothing is reproducible without it |
| sampled token stream | the only artifact that makes replay possible at all |
| GGUF sha256 + llama.cpp build tag | **no lockfile covers the model binary**; reproducibility holds only within one build tag |
| `uv.lock` sha256 + git sha + stage-tuple hash | library skew moves float arithmetic under V2's 0.5% tolerance; the stage hash proves pipeline shape |
| `model_tier`, `degraded_mode` | **recorded inputs, never re-measured** |

The last row is the subtle one. The adaptive ladder probes free RAM at load time; a replay in
2027 probes a different machine, selects a different tier, and diverges. The same rule applies to
`datetime.now()`, `PYTHONHASHSEED`-dependent set iteration, and the `random` arm's draw: anything
sampled from the environment is an **input to be recorded**, not a value to be recomputed.

These are written as the **`RunManifest`, the first ledger event of every run.**

## Consequences

- The ledger spec in PLAN.md §Repo layout was **wrong by omission** — it described business
  events only. `packages/ledger` gains `manifest` and `replay` modules.
- Storage is **canonical JSON, never pickle.** Rename or re-type a Pydantic model and last year's
  record stops loading — a record that must be readable in 2027 cannot depend on today's class
  definitions.
- The replay test joins `tests/pit/`, `test_ledger_rebuild.py` and `test_backtest_matches_paper.py`
  as the fourth member of the correctness family.
- This ADR is the reason no agent framework helps — see [ADR-0001](0001-plain-python-orchestration.md).
  Once this recorder exists it is already a step journal, so a framework's checkpointer is a
  duplicate store of the same facts without a hash chain.
- **The prefix-cache optimisation is kept.** It is incompatible with reproduce-by-regeneration,
  which we no longer attempt, and it is what makes the nightly window feasible.

## Honest limitation

Replay-from-record proves that *the deterministic half of the system* is reconstructible and has
not regressed. It does **not** prove the model would emit the same tokens again — nothing
available on this hardware can prove that. The claim the audit makes is therefore precise:
*given these recorded generations, this is exactly the recommendation the system produced, and
every number in it still derives from the dossier it cited.* That is the honest guarantee, and
it should be stated that way rather than as "the system is reproducible."
