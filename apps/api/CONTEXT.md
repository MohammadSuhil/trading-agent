# ta_api (+ ta_cli) — orientation

FastAPI read model for the dashboard, plus the `ta` operator CLI. **The API
computes nothing**: every number was decided and recorded by the nightly run.

Signatures and routes: [docs/CODEMAP.md](../../docs/CODEMAP.md) § apps/api and
§ packages/cli. Endpoint docs: [docs/technical/API.md](../../docs/technical/API.md).

## API (`main.py`, `serve.py`)
- **One SELECT per endpoint** from a pre-aggregated table (heat map p95 < 30 ms).
  A panel needing per-request computation must first add a nightly pre-aggregate.
- **PIT-bounded reads** with a literal as-of — a past recommendation is served as
  recorded, never recomputed.
- **Every `*_pct` field is a fraction of one** (`0.235` = 23.5%). See the module
  docstring before adding a field.
- `serve.py` binds both `127.0.0.1` and `::1` (browsers try `::1` first), and only
  loopback — never `0.0.0.0`.
- Serves the built SPA from `apps/ui/dist` with explicit `Cache-Control`.
- `EQUITY = 500_000.0` and the portfolio are **hardcoded** until the M2 ledger
  lands; `calibration()` honestly reports nothing resolved.

## CLI (`packages/cli/src/ta_cli/__main__.py`)
`ta cost | ingest | daily | constituents | symbols | screen | calendar | status`.
Command bodies import lazily; `tests/test_cli_commands.py` imports every body so
a missing function fails the build.

**Tests:** `tests/test_api_contract.py`, `tests/test_action_semantics.py`,
`tests/test_cli_commands.py`.

**Before changing:** `main.py` is 619 lines; when M2–M5 add ~30 endpoints, split it
into routers rather than growing it.
