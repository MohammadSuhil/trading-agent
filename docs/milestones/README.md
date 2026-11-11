# Milestones — read one slice, not the whole plan

[PLAN.md](../PLAN.md) is the full design and its rationale (656 lines). A working
session needs one milestone of it. Each file here holds that milestone's gate,
what already exists, what to build, the decisions only the owner can make, and
the traps already known — plus line pointers into PLAN.md so you can read
**only** the sections that matter (`Read` with `offset`/`limit`).

**Do not advance on a failing gate.** M1–M3 contain zero LLM calls.

| # | Milestone | State (2026-11-09) | Est. sessions | Calendar floor |
| --- | --- | --- | ---: | --- |
| [M0](M0.md) | uv workspace, zero-I/O core, capped DBs, PIT leakage suite | ✅ done | — | — |
| [M1](M1.md) | Real NSE data on screen: heat map + screener | ✅ done (BSE source → M2) | — | — |
| [M2](M2.md) | Ledger, PaperBroker, factors, gates, frozen control — **plus the correctness gaps** | ✅ done | 29 | 3 days |
| [M3](M3.md) | Backtester sharing the paper broker's fill/cost code | ✅ done | 15 | 1 day |
| [M4](M4.md) | LLM plumbing: triple mutex, ladder, validators, generation record, OTel | ❌ | 14 | 2 days |
| [M5](M5.md) | The committee, in shadow | ❌ | 17 (+5 p80) | 3 days |
| [M6](M6.md) | Unattended operation | 🟡 launchd only | 13 | **7+ days** |
| [M7](M7.md) | News + filings | ❌ | 17 | **~90 days** |
| [M8](M8.md) | The learning loop closes | ❌ | 26 (with M9) | **~290 days** |
| [M9](M9.md) | Red team, weekly critic, lesson cards | ❌ | (in M8) | — |
| [M10](M10.md) | Intraday leg, ML ranker, regime HMM | ❌ | 27 (with M11) | 8 days |
| [M11](M11.md) | Broker provisioning (not execution) | ❌ | (in M10) | — |

Estimates come from a scoping pass on 2026-11-09 (one agent per milestone plus a
critic that removed double counting). They are planning numbers, not promises.

**Dependency spine:** M2 ledger + PaperBroker → M3 → (M4 → M5) → M6 → M8.
Corporate-action adjustment (in M2) blocks every number downstream of it.

## Two records that span milestones

- **[INVARIANTS.md](INVARIANTS.md)** — the rules that outlive the milestone that set
  them, each with its enforcing test. Read every session.
- **The `## Built` log** at the bottom of each milestone file — what was actually
  built, where, in which commit, and *why* (including where the build diverged from
  the plan). Later milestones cite these rows, not the plan's intent.

Keep both current **in the same commit as the work**: a Built row, an invariant
moved from Planned to Active, the state column above. `tests/test_milestone_docs.py`
fails if a cited test or file stops existing; it cannot tell whether a stated reason
is still true — that part is on review.
