"""The milestone docs must not drift from the repo they describe.

Sessions read `docs/milestones/` *instead of* the whole plan and codebase, so a
stale entry is acted on with no second look. Two drifts are cheap to catch and
expensive to miss:

1. An invariant names its enforcing test, the test is later renamed or deleted,
   and the rule reads as guarded while nothing guards it.
2. A "Built" row lists files that were moved or removed, so a later milestone
   builds on code that is no longer where the record says.

What this cannot check: whether a "Decision and why" is still *true*. That stays
a review responsibility -- which is why the row is written in the same commit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MILESTONES = ROOT / "docs" / "milestones"
INVARIANTS = MILESTONES / "INVARIANTS.md"

_TICKED = re.compile(r"`([^`\s]+)`")
_TEST_REF = re.compile(r"`(tests/[^`:\s]+\.py)::([A-Za-z_][A-Za-z0-9_]*)`")
_COMMIT = re.compile(r"^(?:[0-9a-f]{7,40}|pending)$")


def _section(text: str, heading: str) -> str:
    """Body of `## heading` up to the next `## `; empty if absent."""
    m = re.search(rf"^## {re.escape(heading)}\b.*?$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def _rows(table_text: str) -> list[list[str]]:
    """Data rows of every markdown table in `table_text`, cells stripped.

    >>> _rows("| a | b |\\n| --- | --- |\\n| 1 | `x` |")
    [['1', '`x`']]
    """
    rows = []
    for line in table_text.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= {"|", "-", " ", ":"}:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    return [r for i, r in enumerate(rows) if not (i == 0 or r == rows[0])]


def _looks_like_path(token: str) -> bool:
    return "/" in token or token.endswith((".py", ".md", ".json", ".toml", ".lock", ".sh", ".sql", ".xml"))


def _milestone_files() -> list[Path]:
    return sorted(MILESTONES.glob("M*.md"), key=lambda p: int(p.stem[1:]))


def test_every_enforcing_test_named_in_invariants_exists() -> None:
    active = _section(INVARIANTS.read_text(encoding="utf-8"), "Active")
    refs = _TEST_REF.findall(active)
    assert refs, "INVARIANTS.md names no enforcing tests -- parser or file is broken"
    for path, name in refs:
        f = ROOT / path
        assert f.exists(), f"INVARIANTS.md cites {path}, which does not exist"
        assert re.search(rf"^\s*def {name}\(", f.read_text(encoding="utf-8"), re.MULTILINE), (
            f"INVARIANTS.md cites {path}::{name}, which no longer exists -- "
            "the rule now reads as guarded while nothing guards it"
        )


def test_every_non_test_enforcer_path_exists() -> None:
    for row in _rows(_section(INVARIANTS.read_text(encoding="utf-8"), "Active")):
        for token in _TICKED.findall(row[-1]):
            if "::" not in token and _looks_like_path(token):
                assert (ROOT / token).exists(), f"INVARIANTS.md enforcer {token} does not exist"


def test_invariant_ids_are_unique() -> None:
    ids = re.findall(r"^\| ([IP]-\d{2}) \|", INVARIANTS.read_text(encoding="utf-8"), re.MULTILINE)
    assert len(ids) == len(set(ids)), f"duplicate invariant ids: {sorted(i for i in ids if ids.count(i) > 1)}"


@pytest.mark.parametrize("doc", _milestone_files(), ids=lambda p: p.stem)
def test_built_rows_cite_files_that_exist(doc: Path) -> None:
    built = _section(doc.read_text(encoding="utf-8"), "Built")
    assert built, f"{doc.name} has no '## Built' section"
    for row in _rows(built):
        component, files, commit = row[0], row[1], row[2]
        assert _COMMIT.match(commit), f"{doc.name} / {component}: commit {commit!r} is not a sha or 'pending'"
        for token in _TICKED.findall(files):
            if _looks_like_path(token):
                assert (ROOT / token).exists(), f"{doc.name} / {component}: {token} does not exist"


@pytest.mark.parametrize("doc", [p for p in _milestone_files() if int(p.stem[1:]) >= 2], ids=lambda p: p.stem)
def test_open_milestones_point_at_the_invariants(doc: Path) -> None:
    """A slice that does not link the invariants is exactly how a session misses one."""
    assert "](INVARIANTS.md)" in doc.read_text(encoding="utf-8"), f"{doc.name} does not link INVARIANTS.md"
