# Plan: issue #33 — serve Plotly locally for offline dashboard (U1)

## Goal (verbatim from issue #33)

> The monitoring dashboard works in a fully offline environment and no longer depends on an
> unpinned third-party CDN for its charting library.

## Current state

`templates/dashboard.html:7` loads `https://cdn.plot.ly/plotly-latest.min.js` (CDN, unpinned). The
chart is drawn by `Plotly.newPlot(...)` (line ~231). No `static/` directory exists.

## Approach

Serve the Plotly bundle that ships with the installed `plotly` package, rather than vendoring a
~4.8 MB file into the repo. `plotly.offline.get_plotlyjs()` returns the bundled minified JS as a
string (the version locked to the dependency).

1. **Route** in the dashboard blueprint: `GET /vendor/plotly.min.js` returns the JS with a
   JavaScript mimetype. Cache the string at module level (load once) and set a moderate
   `Cache-Control` (e.g. `max-age=86400` — not `immutable`, so a `plotly` upgrade is picked up).
2. **Template**: replace the CDN `<script src=...>` with
   `<script src="{{ url_for('dashboard.plotly_js') }}"></script>` so it routes through the app
   (blueprint `url_prefix="/"` → `/vendor/plotly.min.js`).
3. Import `Response` from flask in `views/dashboard.py`.

## Alternatives considered / rejected

- **Vendor `plotly.min.js` into `static/`**: rejected — commits a multi-MB asset that drifts from
  the installed `plotly` version; serving the package's own bundle keeps them in lockstep.
- **Pin the CDN URL to a version** (`plotly-2.x.x.min.js`): rejected — still needs the network, so
  it doesn't satisfy the offline requirement.

## Out of scope

- Chart appearance / the `/api/ping_chart` payload; offline-bundling any other asset.

## Testing (acceptance scenarios → tests) — `tests/test_dashboard_offline_plotly.py`

Built via the real `create_app()` + tmp DB (the `test_dashboard_api.py` pattern).

- **Local bundle served**: `GET /vendor/plotly.min.js` → `200`, a JavaScript content type, body
  contains a Plotly marker (e.g. `"Plotly"` / `"plotly.js"`).
- **Index uses local, not CDN**: `GET /` → body references `/vendor/plotly.min.js` and contains
  neither `cdn.plot.ly` nor `plotly-latest`.

## Versioning

Runtime/user-facing change → bump root `pyproject.toml` (0.2.3 → 0.2.4) and add a CHANGELOG entry.
