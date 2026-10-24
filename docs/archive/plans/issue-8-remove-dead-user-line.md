# Plan: issue-8 remove dead code (cli.py user_line)

## Goal (verbatim from issue #8)

`netdiag` から、改善doc `docs/improvements.md` の M6 で指摘されたデッドコードを除去する。
`netdiag/cli.py` の `user_line = "" if system else ""`（両分岐とも空文字で、以降どこからも
参照されない）を削除し、振る舞いを変えずにコードを整理する。

## Findings (confirmed)

`netdiag/cli.py:626`, inside `_unit_text(exec_start, system) -> str`:
```python
user_line = "" if system else ""
```
`user_line` is assigned once and never referenced — the returned f-string only uses `after`
and `install_target`. `system` is still used (drives `install_target`). Removing the line is
behaviour-preserving. (The dead line hints at an intended `User=` directive for system units,
but adding one is a behaviour change and out of scope for M6.)

## Approach

1. Delete the single dead assignment line. No other change to `_unit_text`.
2. Add tests for the previously-untested `_unit_text` (aligns with the M10 gap):
   - characterization test of both `system=True`/`system=False` outputs (behaviour pinned).
   - a guard using `inspect.getsource(_unit_text)` asserting `user_line` no longer appears
     (red before removal, green after; guards against reintroduction).

## Alternatives considered / rejected
- **Wire up a real `User=` line** for system units: plausible original intent, but a behaviour
  change beyond M6's "remove dead code" scope. Rejected — leave for a separate ticket if wanted.
- **Source-file grep test**: `inspect.getsource` on the function is more targeted than grepping
  the whole file. Chosen.

## Out of scope
- M8 (report i18n), M9 (`ping3.EXCEPTIONS` / `socket.setdefaulttimeout` globals) — same doc
  heading but separate concerns.
- Any behaviour change to the generated systemd unit.

## Test strategy (acceptance scenarios → tests)
- "no `user_line` assignment/reference remains" → `inspect.getsource` guard test.
- "output/exit code unchanged" → characterization test of `_unit_text` for both flag values.
- "existing suite stays green" → full `pytest` run.

## Version / changelog
- Bump `netdiag.__version__` 0.1.2 → 0.1.3 (patch; dead-code cleanup, no runtime change).
- Add a v0.1.3 entry to `netdiag/CHANGELOG.md`.
