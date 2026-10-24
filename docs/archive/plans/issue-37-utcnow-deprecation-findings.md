# Findings: issue-37-utcnow-deprecation

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 0 (the one raised was re-categorized to informational by the reviewer)
- Informational: 8
- Key Issues:
  - Pre-existing version drift: `network_check_tool/__init__.py` `__version__ = "0.1.1"` vs
    `pyproject.toml` now `0.2.5`. **Verified it is unused anywhere** in the package/tests (only
    `plotly.__version__` and a comment matched a search). Pre-existing and out of M7's scope, so
    left as carry-forward rather than creeping this PR.
  - Verified (no change): `datetime.now(timezone.utc).replace(tzinfo=None)` is behavior-identical to
    `datetime.utcnow()` (same clock, tz stripped, no arithmetic); `default=utcnow` is the correct
    callable ref (called at INSERT time); naive/naive comparisons in `timestamp >= since` stay
    consistent; the AST regression guard correctly catches `.utcnow` attribute access (calls and
    bare refs) without false-positiving on the helper's own `def utcnow` or its docstring.
  - `tests/test_network_check_tool_fixes.py` keeps a deliberate fixed `datetime(2026,…)` constructor
    (a fixed-date status-display test, not a time-range query) — correctly left untouched.
- Judgment: Clean sweep, accurate review; no actionable code changes.

### Doc Parrot
- Divergences Found: 0
- Details: the one changed callable with a docstring is `utils.utcnow()`. Prose ("current UTC time
  as a naive datetime … same instant as the deprecated `datetime.utcnow()`") matches the
  implementation; verified by `test_utcnow_is_naive_utc_and_current` (`tzinfo is None`, within ~2s).
  The other changed callables are test seed helpers (no docstrings).
- Judgment: Nothing to flag.

### Actions taken (Round 1)
- None (review found no actionable in-scope issues).

### Carry-forward (informational, not changed)
- `network_check_tool/__init__.py` `__version__` is stale (`0.1.1`) and unused; a trivial separate
  hygiene fix could sync it to the pyproject version (the netdiag CLI resolves its version via
  `importlib.metadata`, so this dead constant has no functional effect today).
- A full timezone-aware timestamp redesign (column `timezone=True`, "Z" in the API) remains a
  deliberate non-goal of this deprecation cleanup.
