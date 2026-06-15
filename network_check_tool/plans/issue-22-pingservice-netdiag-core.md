# Plan: issue #22 — back PingService with netdiag's ping core, drop ping3 (M1 phase 1)

## Goal (verbatim from issue #22)

> `network_check_tool` stops maintaining its own ICMP measurement and instead obtains ping
> measurements from `netdiag`'s shared ping core, removing the duplicate implementation (and the
> `ping3` dependency). This is the first, focused step of **M1**; storage and scheduler
> unification are explicitly deferred.

## Current state

`PingService.ping_host` uses the `ping3` library (raw-socket ICMP, often needs elevated
privileges) and converts seconds → ms. `netdiag/probes/icmp.py` already has a portable,
stdlib-only `ping(host, count=4, timeout=2.0) -> (avg_rtt_ms | None, loss_pct, raw_output)` that
shells out to the OS `ping`. Two implementations of the same concept (M1).

## Approach

Rewrite **only** `PingService.ping_host` to delegate to netdiag's `ping`; everything else
(`save_ping_result`, `ping_and_save`, the `timeout` attribute and constructor) stays as-is so the
M2 timeout wiring and all consumers are unaffected.

- Import netdiag's measurement: `from netdiag.probes.icmp import ping as _measure_ping` and the
  `have` command-probe helper.
- `ping_host(host)`:
  - If the `ping` command isn't available → `response_time=None`, `packet_loss=True`,
    `error_message="ping command not available"` (mirrors netdiag's icmp probe), same dict shape.
  - Otherwise `rtt, loss, _raw = _measure_ping(host, count=1, timeout=self.timeout)`.
    `count=1` keeps one measurement per sample (current cadence/cost).
  - **Success** (`rtt is not None and loss < 100`): `response_time=rtt` (already ms — no `*1000`),
    `packet_loss=False`, `error_message=None`.
  - **Failure** (`rtt is None or loss >= 100`): `response_time=None`, `packet_loss=True`,
    `error_message="Request timeout"` (parity with the prior message).
  - Wrap in `try/except` returning the same failure dict with `error_message=str(e)`, as today.
- Remove `import ping3` and the `ping3.EXCEPTIONS = True` line; remove `ping3` from
  `pyproject.toml` dependencies and regenerate the lock if present.

## Alternatives considered / rejected

- **Keep ping3, just share storage/scheduler first**: rejected — the doc names ping measurement as
  the recommended first step, and ping3's privilege requirement is itself a wart that netdiag's
  subprocess approach removes.
- **Send `count=4` like netdiag's default**: rejected — that quadruples per-sample traffic and
  latency vs the current single ping; out of scope to change cadence.
- **Richer `error_message` parsed from raw output**: rejected for now — keep parity with the
  existing "Request timeout" string to avoid changing stored data semantics.

## Out of scope

- Unifying time-series storage (`flask_sqlalchemy` ↔ `netdiag/storage.py`) and the scheduler
  (APScheduler ↔ `netdiag/scheduler.py`) — later M1 phases.
- Any change to `PingResult`, the dashboard, or the scheduler.

## Testing (acceptance scenarios → tests) — `tests/test_pingservice_netdiag.py`

- **Success mapping**: monkeypatch `_measure_ping` to return `(12.5, 0.0, "raw")`; assert
  `ping_host` → `response_time == 12.5` (ms, no doubling), `packet_loss is False`,
  `error_message is None`, and the `host`/`timestamp` keys present.
- **Loss/timeout mapping**: `_measure_ping` → `(None, 100.0, "raw")` ⇒ `response_time is None`,
  `packet_loss is True`, non-empty `error_message`.
- **Exception mapping**: `_measure_ping` raises ⇒ failure dict with `error_message == str(e)`.
- **ping unavailable**: monkeypatch `have` → False ⇒ failure dict, `_measure_ping` not called.
- **Timeout forwarded**: `PingService(timeout=2.5).ping_host(...)` calls `_measure_ping` with
  `timeout=2.5` (captured via monkeypatch).
- **No ping3**: `ping_service` module namespace has no `ping3`; `pyproject.toml` no longer lists it.
