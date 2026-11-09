# Diagrams

Mermaid diagrams in Markdown, so GitHub renders them inline. Several focused diagrams rather than
one unreadable one.

| File | Shows |
| --- | --- |
| [context.md](context.md) | C4 level 1 — the system, its one user, and the external data sources it touches |
| [containers.md](containers.md) | C4 level 2 — processes, packages, datastores, and what talks to what |
| [nightly-sequence.md](nightly-sequence.md) | the 18:45 run, stage by stage, including the gates that reject |
| [data-flow.md](data-flow.md) | one bhavcopy's journey from the NSE archive to a card on the dashboard |
| [clickhouse-er.md](clickhouse-er.md) | the ClickHouse schema, its join keys, and which tables are point-in-time |

All diagrams reflect **what is built**, not what PLAN.md describes. Components that exist only as
plans are drawn with dashed borders and labelled, so the gap is visible rather than implied. See
[../LLD.md §9](../LLD.md#9-what-is-deliberately-not-built-and-what-that-costs-you) for the full
inventory of what is missing.

Verified against the running system on 2026-10-31.
