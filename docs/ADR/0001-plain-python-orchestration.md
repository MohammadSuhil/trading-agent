# ADR-0001 — Committee orchestration is plain Python, not an agent framework

- **Status:** Accepted
- **Date:** 2026-10-31
- **Supersedes:** the unargued one-paragraph rationale previously at PLAN.md §Architecture

## Context

The committee is a fixed sequence of structured passes over one resident model:
Bull → Bear → one Rebuttal → deterministic Risk gates → Risk narrative → Portfolio Manager
(two-stage) → V1–V9 validators → Position Reviewer.

The question was raised directly: *"Shouldn't we use an agent framework? Isn't it easier with a
framework to fix and audit the workflow?"* That is a reasonable question, and the first draft of
the plan answered it badly — it asserted that "every agent framework's core value is letting an
LLM choose control flow," which is false, and that a framework would "remove determinism," which
is backwards.

Four options were steelmanned by independent advocates, fact-checked against primary sources, and
scored by three judges using different lenses (auditability, debuggability, solo-maintainer cost).

## Decision

**Orchestration between personas is plain Python.** Prefect 3.9 remains, but only for wall-clock
scheduling of the nightly DAG — it is not load-bearing for control flow inside the committee.

## Why

1. **The audit substrate is mandatory regardless.** No framework supplies byte-identical replay
   (see [ADR-0002](0002-replay-from-record.md)). The hash-chained ledger plus a per-generation
   record must exist whatever we choose. Once it exists it *is* a step journal —
   `if (run_id, symbol, step) in ledger: return ledger[...]` — so a framework's persistence layer
   is duplicate work, not saved work.

2. **A second durable store is an audit liability, not an asset.** LangGraph's channel values and
   DBOS's pickled operation outputs record the same facts without a hash chain, in their own
   schema, with their own retention and migration story. A second record that is not
   tamper-evident is either redundant (it agrees) or an incident (it disagrees).

3. **Resume actively damages the debug loop.** Measured 2026-10-31. Fix the persona that emitted
   a bad number, then resume:
   - **LangGraph** — the fix never ran. The upstream node had already committed `1187.40` to
     checkpoint state; the repaired validator stopped raising; the PM received the exact literal
     V2 exists to reject, in a **green run with no warning**.
   - **DBOS** — the resume replayed the recorded *error*. Neither persona executed, the failure
     message was byte-identical, and the fix was invisible.
   - **Plain Python** — re-ran current code and produced the corrected value.

   Having no resume is a safety property here. Both failures are the same shape as all four
   entries in HANDOFF.md: *looked fine, started cleanly, and was wrong.*

4. **LangGraph's versioning policy is adverse to reconstruction.** Its own docs: *"Unlike workflow
   engines that pin a run to the version of code it started with, LangGraph applies the latest
   graph immediately to every thread."* Reconstructing 2026-10-15 would run today's code.

5. **The resume benefit is largely illusory here.** The expensive resource is the 22.4 GB resident
   model, not the node. Any llama-server-shaped crash pays the reload regardless.

## Consequences

**Accepted costs.**

- No trace viewer out of the box. Addressed at M4 by OTel spans plus a locally-served Phoenix
  instance — see PLAN.md §Observability. This was the real gap behind the original question.
- Determinism is not free. `PYTHONHASHSEED` randomises set iteration, which reorders candidates,
  which changes prompt bytes; `datetime.now()` inside the committee breaks replay. ~200 lines of
  discipline, which must be written and tested rather than assumed.
- Durable resume, if wanted later, is ours to build — bounded at ~60–150 lines on top of the
  ledger, with the stamp rule below.

**Rules adopted from the losing options.**

- `COMMITTEE_STEPS` is a module-level tuple driven by a `for` loop containing no `if`.
  `tests/test_committee_shape.py` asserts the stage-name sequence equals a golden constant and
  AST-walks the runner for conditional branching. Its hash goes in the `RunManifest`. This is
  LangGraph's `assert len(branches) == 0` with zero packages, and better, because the hash lands
  in the tamper-evident ledger.
- The committee diagram is rendered *from the tuple*, never from a comment.
- **Any replay or resume path must re-execute everything downstream of an edit, or refuse
  loudly.** Stamp code identity (git sha, persona prompt sha, GBNF sha, model sha) on every
  recorded step and fail when stamps differ from current.

**Revisit if** the committee ever needs genuinely dynamic routing, or if control flow stops being
fixed. Both would invalidate the premise rather than the reasoning.

## Alternatives considered

| Option | Auditability | Debuggability | Solo cost |
| --- | --- | --- | --- |
| Plain Python + Prefect | 8 | 7.5 | **8** |
| Plain Python + OTel tracing layer | **9** | **8.5** | 7.5 |
| Durable execution (DBOS / Temporal) | 5 | 3 | 4.5 |
| Graph framework (LangGraph 1.2.19) | 3 | 5 | 3 |

Temporal was rejected additionally because self-hosted namespace retention defaults to **3 days**
with archival experimental and disabled — the one engine with genuine event sourcing deletes the
history that made replay possible after 72 hours.

## Corrections this ADR forces elsewhere

- PLAN.md's Prefect justification ("the only orchestrator with cross-subprocess global concurrency
  limits without a broker") is stale: DBOS's `Queue(global_concurrency=N)` is database-backed and
  broker-free in 12 packages. Prefect is retained on scheduling maturity and DLQ instead.
- The claim that CrewAI "caps at `<3.14`" is true but irrelevant — we pin 3.13, inside its range.
  The real disqualifiers are telemetry-on-by-default and 137 resolved packages.
- `--models-max 1` is **not** a structural invariant. See PLAN.md §The stack.
- The no-telemetry constraint is already violated by Prefect itself. See PLAN.md §Telemetry.
