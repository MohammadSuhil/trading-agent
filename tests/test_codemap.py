"""docs/CODEMAP.md must match the code it describes.

Agents are told to look signatures up in the map *instead of* opening the
source. That is what makes the map save tokens, and it is also what makes a
stale map dangerous: a wrong signature read from it is acted on with no second
look. The pre-commit hook regenerates the map, but hooks are per-clone and can
be skipped with --no-verify, so this test is the backstop that cannot be.
"""
from __future__ import annotations

import doctest
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("gen_codemap", ROOT / "scripts" / "gen_codemap.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_codemap_is_current() -> None:
    gen = _load()
    assert gen.OUT.read_text(encoding="utf-8") == gen.render(), (
        "docs/CODEMAP.md is stale: run `uv run --frozen python scripts/gen_codemap.py`"
    )


def test_generator_doctests() -> None:
    """scripts/ is outside pytest's testpaths, so its doctests run from here."""
    result = doctest.testmod(_load())
    assert result.attempted > 0
    assert result.failed == 0


def test_every_source_root_is_mapped() -> None:
    """A package added to the workspace but not to SOURCE_ROOTS would be
    silently absent from the map -- which reads as "nothing to see there"."""
    gen = _load()
    # Python source roots only: every workspace package is a `src/ta_*` dir.
    # apps/ui/src is TypeScript and deliberately outside the map.
    roots = {p.parent.parent.relative_to(ROOT).as_posix()
             for pattern in ("packages/*/src/ta_*", "apps/*/src/ta_*")
             for p in ROOT.glob(pattern)}
    mapped = {r.removesuffix("/src") for r in gen.SOURCE_ROOTS}
    assert roots <= mapped, f"unmapped source roots: {sorted(roots - mapped)}"
