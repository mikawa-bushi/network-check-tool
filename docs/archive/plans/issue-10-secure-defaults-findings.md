# Findings: issue-10-secure-defaults

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 4
- Informational: 6
- Key Issues:
  - `_is_loopback` docstring didn't mention `::` (IPv6 wildcard) as a non-loopback address.
  - Test warning assertion used `.upper()` — fragile to rewording.
  - Missing negative-CORS test: non-allowed origin rejected when `CORS_ORIGINS` is set.
  - `CORS_ORIGINS` evaluated at import time (consistent with other settings) — note for ops.
- Judgment: Verified the guard ordering (debug refusal before `create_app`), that CORS scoping
  to `/api/*` is correct (dashboard is same-origin), and the semver MINOR bump for a breaking
  default. All four Should-Fix are real, cheap improvements; addressed this round. Residual
  `_is_loopback` host-classification gaps all fail safe (treated as public → warn/refuse).

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_is_loopback()` (network_check_tool/cli.py) — **OK**. Prose (True for localhost/::1/127.x,
    False for 0.0.0.0/::) matches `host in {...} or host.startswith("127.")` exactly.
  - `run_server()` (network_check_tool/cli.py) — **OK** (parrotted manually; its `def` line was
    unchanged so the collector skipped it). Docstring accurately states the localhost default
    and unauthenticated-exposure caveat; the `--debug` loopback-only rule is in the option help.
  - Test callables/helpers have no docstrings → skipped.
- Judgment: No docstring divergence.

### Actions taken (Round 1)
- `_is_loopback` docstring: documented `0.0.0.0`/`::` as non-loopback (warn/refuse) paths.
- Tightened the warning-string assertion (dropped `.upper()`).
- Added `test_configured_cors_rejects_other_origin` (non-allowed origin gets no CORS grant).
- Added an import-time NOTE comment on `Config.CORS_ORIGINS`.

### Carry-forward (out of scope)
- Reviewer's "CHANGELOG dated in the future" note was based on a stale date; today is
  2026-06-15 (matches merge timestamps), so the entry is correct.
