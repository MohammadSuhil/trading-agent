# ta_datafeed — orientation

Gets published exchange files into ClickHouse. **Raw bytes land in
`data/raw/YYYY/MM/DD/` before anything parses** — a parser bug is a re-parse,
never a re-download.

Signatures: [docs/CODEMAP.md](../../docs/CODEMAP.md) § packages/datafeed.

| Module | Owns | Notes |
| --- | --- | --- |
| `http.py` | `PoliteClient` | Per-host token bucket; immutable cache keyed by URL (right for bhavcopies, **wrong for RSS** — see M7) |
| `sources/nse_archive.py` | `NSEArchiveSource` | Published archive files only. Never cookie-warm or defeat bot protection (`www.nseindia.com` is 403; ToS) |
| `parse/udiff.py` | `parse_udiff_bhavcopy`, `bhavcopy_url` | Maps columns by **name**, fails loudly on a missing one. Also extracts `PrvsClsgPric` — the basis for corporate-action inference |
| `parse/constituents.py` | `parse_constituents` | niftyindices CSV; official `Industry` = heat-map grouping |
| `backfill.py` | `ingest_sessions`, `refresh_constituents`, `refresh_symbol_master` | Idempotent; constituents are **dated snapshots**, never "current membership" |
| `store.py` | `GuardedClient` | The only door into ClickHouse. `query_pit` is the correct read; `query_admin` is unguarded and for ops only |

**Writes:** `md.bars_1d`, `md.index_members`, `ref.symbols`.

**Empty placeholders:** `adjust/` (corporate actions — M2's top correctness gap),
`universe/`. No BSE source yet (M2).

**Before changing:**
- Every new table read goes through `GuardedClient.query_pit` with an explicit `AsOf`.
- `md.bars_1d` is ReplacingMergeTree: duplicates survive until a background merge,
  so readers dedupe on `(symbol, d)` (see `ta_quant.nightly.load_panel_inputs`).
- Infra changes: read HANDOFF.md's four silent-failure modes first.
