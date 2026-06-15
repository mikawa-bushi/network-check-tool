# Plan: issue-14 daemon observability / silent except logging (E1)

## Goal (verbatim from issue #14)

`netdiag` の常駐系コードにある「沈黙する握りつぶし」（`except Exception: pass`）を観測可能にする。
1つの障害で常駐を殺さない方針は維持しつつ、握りつぶす箇所で必ず警告ログ（スタックトレース付き）を
残し、そのログを CLI から表示できるようにする。

## Findings (swallow sites — netdiag has NO logging today)

- `alerting.py:213-214` `MultiSink.emit` — sink failure swallowed.
- `watch.py:136-137` `_maybe_maintain` (retention failure), `:145-147` `_loop` (tick failure).
- `scheduler.py:45-47` `Monitor._loop` (tick failure).
- `cli.py:556-557` `status` (`Storage().counts()` failure).
`grep` confirms zero `getLogger`/`import logging` in `netdiag/` currently.

## Approach

1. Each affected module gets `import logging` + `logger = logging.getLogger(__name__)`.
2. Replace each `except Exception: pass` (keep swallowing for daemon survival) with
   `logger.warning("<context>", exc_info=True)` so failures are recorded, not lost.
3. `netdiag/__init__.py`: attach a `logging.NullHandler()` to the `"netdiag"` logger — the
   standard library idiom that keeps output quiet by default (no "lastResort" stderr) without
   the library ever calling `basicConfig`.
4. CLI: add a `_setup_logging(verbose, log_file)` helper and group-level `--verbose/-v` and
   `--log-file` options on the `cli` group. Only when one is given does it attach a real
   handler (stderr or file) at WARNING (or DEBUG with --verbose). Default stays silent.

## Alternatives considered / rejected
- **Per-command options** instead of group-level: the daemon commands (watch/monitor) are the
  main consumers, but group-level makes the logs available to every subcommand uniformly and
  runs before the subcommand. Chosen. (Note: `check` already has its own command-level
  `--verbose` for reasoning steps; the group-level one is separate and operates at a different
  parse position — no conflict.)
- **`logging.basicConfig` in CLI**: would reconfigure the root logger globally; instead configure
  only the `"netdiag"` logger. Chosen (matches the library-doesn't-touch-globals principle).
- **Narrowing the `except` clauses**: out of scope; E1 is about observability, not changing which
  exceptions are caught.

## Out of scope
- `network_check_tool` logging / `basicConfig` (E2 — separate package/Issue). E3/M5/M7.
- Changing swallow semantics (loops must still survive a single failure).

## Test strategy (acceptance scenarios → tests)
New `tests/test_daemon_logging.py` (autouse fixture snapshots/restores the `"netdiag"` logger):
- **MultiSink**: a failing sink + a recording sink → recording sink still receives the event AND
  a `netdiag.alerting` warning is logged (caplog). (Red: today nothing is logged.)
- **Monitor loop**: engine that raises → background `start()`, a `netdiag.scheduler` warning is
  logged within a deadline, loop survives, `stop()`. (Red: today silent.)
- **quiet by default**: after import, the `"netdiag"` logger has a `NullHandler` and no
  stream/file handler (library adds no real handler).
- **_setup_logging**: `--verbose` attaches a stderr `StreamHandler` (DEBUG); `--log-file`
  writes warnings to the file.

## Version / changelog
- Bump `netdiag.__version__` 0.1.4 → 0.1.5 (minor-ish feature: logging + CLI options; patch is
  also defensible. Use 0.1.5 patch — additive, backward compatible).
- Add a v0.1.5 entry to `netdiag/CHANGELOG.md`.
