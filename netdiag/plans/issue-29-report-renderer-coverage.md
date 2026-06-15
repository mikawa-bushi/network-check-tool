# Plan: issue #29 — report renderer coverage (M10 phase 3)

## Goal (verbatim from issue #29)

> `netdiag`'s report renderers — terminal, JSON, and HTML — have unit tests covering their output,
> closing the last of the review's named coverage gaps for the output layer.

## Approach

New `tests/test_report.py`. Build a real `Diagnosis` via `Engine().run()` over synthetic probes
registered through the `isolated_registry` + `_make_probe` pattern (as in `test_engine.py` /
`test_engine_parallel.py`); render and assert. Characterization tests of existing, correct output —
no red phase, no runtime change.

Cases:

- **render_json**: a failing diagnosis → `json.loads` round-trips; assert `healthy is False`,
  one `results` entry per probe (names/statuses), and a `hypotheses` list whose entries carry
  `cause`/`confidence`/`summary`/`evidence`/`runbook`. A healthy diagnosis → `healthy True`,
  `hypotheses == []`.
- **render_terminal**: includes each probe name + its status symbol; `color=False` output has no
  `\033` ANSI escapes, `color=True` does; a failing diagnosis contains the cause and the
  "診断 (Diagnosis)" header; `verbose=True` with reasoning adds the "推論過程" section; a healthy
  diagnosis shows the "All checks passed" verdict.
- **render_html**: starts with `<!doctype html>`; **self-contained** — no `http`/`https` URLs and
  no `<script`; includes the verdict text and a `<tr>` per result and the hypothesis cause; a
  result summary containing `<script>` is HTML-escaped (`&lt;` present, raw `<script>` absent).

## Out of scope

- Changing renderer output; the two CLIs (later M10 phase); any runtime change.

## Versioning

Test-only, no runtime change → no version bump / CHANGELOG entry (as in #25, #27).
