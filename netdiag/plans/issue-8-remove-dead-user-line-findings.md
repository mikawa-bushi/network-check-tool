# Findings: issue-8-remove-dead-user-line

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 2
- Informational: 5
- Key Issues:
  - `_unit_text` had no docstring — natural moment to add one now that it has tests/version bump.
  - Characterization tests pinned only `WantedBy`/`ExecStart`; the `[Unit]` deps and `[Service]`
    fields (most likely to drift) were not pinned.
- Judgment: Confirmed the removal is behaviour-preserving and no other refs to `user_line`
  exist. Both Should-Fix items are real quality improvements; addressed in this round.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_unit_text()` (netdiag/cli.py) — **OK**. Docstring (added this PR) accurately states
    `exec_start` → `ExecStart=`, `system` → multi-user/default `WantedBy`, and "no `User=`
    emitted". Implementation matches; characterization tests assert exactly these.
    (The parrot collection script only flags callables whose `def` line changed, so it
    missed the in-body docstring addition; reviewed manually.)
  - The three changed `test_*` callables have no docstrings → skipped.
- Judgment: No divergence; docstring is faithful.

### Informational (carry-forward)
- The dead `user_line` likely hinted at an intended `User=` directive for system units.
  Adding one is out of scope for M6 — worth a separate follow-up ticket. The new
  characterization tests (asserting `User=` absent) will make that change safe and visible.

### Actions taken (Round 1)
- Added a docstring to `_unit_text`.
- Strengthened both characterization tests to pin the shared `[Unit]`/`[Service]` directives
  and assert no `User=` line, with cross-variant `WantedBy` exclusivity.
