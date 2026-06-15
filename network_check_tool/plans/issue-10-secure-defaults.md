# Plan: issue-10 secure-by-default web server (U3)

## Goal (verbatim from issue #10)

`network_check_tool` の Web ダッシュボード/データAPI を、既定で安全な姿勢に変更する
（`docs/improvements.md` U3）。既定では外部公開せず、CORS を絞り、対話デバッガが不用意に
外部へ露出しないようにする。外部公開や緩和は明示的なオプトインでのみ可能にする。

## Findings (current state)

- `cli.py:15` `run-server --host` default `0.0.0.0` → binds all interfaces by default.
- `app.py:19` `CORS(app)` → all-origins permissive CORS on every route incl. `/api/*`.
- `cli.py:17,20-21` `--debug` → `app.run(debug=True)` → Werkzeug interactive debugger (RCE).
- `config/config.py` has no CORS configuration.

## Approach

### 1. Default bind → localhost (cli.py)
- `run-server --host` default `0.0.0.0` → `127.0.0.1`.
- If the chosen host is not loopback, print a prominent stderr WARNING (no auth, exposed).

### 2. Debug gating (cli.py)
- Add a pure helper `_is_loopback(host) -> bool` (`127.0.0.1` / `localhost` / `::1` / `127.*`).
- If `--debug` and host is not loopback → abort with `click.ClickException` (exit 1) before
  creating the app. Effectively "debugger is loopback-only" — closes the RCE-on-LAN path.

### 3. CORS restriction (app.py + config.py)
- `Config.CORS_ORIGINS`: parsed from env `CORS_ORIGINS` (comma-separated), default `[]`.
- In `create_app`, only enable CORS when `CORS_ORIGINS` is non-empty, scoped to `/api/*`:
  `CORS(app, resources={r"/api/*": {"origins": origins}})`. Empty → no permissive headers
  (same-origin requests are unaffected; the browser doesn't need CORS for same-origin).

### 4. Docs
- README: change the `--host 0.0.0.0` example to note it exposes the server, and add a short
  security note (localhost default; for remote use, put behind a reverse proxy with auth;
  don't run the dev server publicly / don't use --debug publicly).

## Alternatives considered / rejected
- **Warn-only for debug on public host**: weaker; the issue allows "force localhost-only OR
  warn+opt-in". Forcing loopback-only for the debugger is the safer reading — chosen.
- **Add authentication**: explicitly out of scope ("既定で公開しない" is the bar). Rejected.
- **Default CORS to same-origin via an allowlist of one**: simpler to just not register CORS
  when no origins configured; same-origin works without CORS headers. Chosen.

## Out of scope
- Real auth/authz, U1 (Plotly bundling), M4 (sqlite thread safety), gunicorn packaging.
- Changing `--port` default or other CLI behaviour.

## Testability notes
- `run-server` calls `app.run()` (blocking). Tests use Click's `CliRunner` with
  `network_check_tool.cli.create_app` monkeypatched to a fake whose `.run(**kwargs)` records
  args; assert host/debug propagation and that unsafe combos abort before `.run`.
- CORS: build app via `create_app("development")` with `DevelopmentConfig.SQLALCHEMY_DATABASE_URI`
  monkeypatched to `sqlite:///:memory:`; use the test client with an `Origin` header.

## Test strategy (acceptance scenarios → tests)
- Default localhost bind → `run-server` (no args) calls `app.run(host="127.0.0.1", ...)`.
- Public opt-in → `run-server --host 0.0.0.0` calls `app.run(host="0.0.0.0", ...)` (+ warning).
- Debug gating → `--host 0.0.0.0 --debug` exits non-zero and never calls `app.run`;
  `--debug` (default loopback host) calls `app.run(debug=True)`.
- No permissive CORS by default → `/api/*` response has no `Access-Control-Allow-Origin: *`
  and does not echo an arbitrary `Origin`.
- Configured CORS → with `CORS_ORIGINS=["http://allowed.example"]`, that origin is allowed.
- Local dev unaffected → default `run-server` still serves on localhost without extra config.

## Version / changelog
- Bump `pyproject.toml` (network_check_tool) 0.1.2 → 0.2.0 (intentional behaviour change:
  default bind is now localhost). Document the breaking default prominently in CHANGELOG.
