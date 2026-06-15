# Findings: issue-33-offline-plotly

## Round 1

### Code Reviewer
- Blocking: 1
- Should Fix: 4
- Informational: 6
- Key Issues:
  - **Blocking**: lazy init of the module-global `_PLOTLY_JS` was a check-then-set race (under the
    threaded server two cold requests could each rebuild the ~5MB string). Fixed with double-checked
    locking on a module-level `threading.Lock`.
  - **Should Fix**: added `X-Content-Type-Options: nosniff` to the JS response.
  - **Should Fix**: added an `ETag` (keyed on `plotly.__version__`) + `make_conditional(request)`,
    so a same-version reload after the cache TTL revalidates with a cheap 304 instead of re-sending
    4.8MB; an upgrade changes the tag. New test asserts the 304 path.
  - **Should Fix**: test now asserts the `Cache-Control` header.
  - **Should Fix**: test now pins the served bytes to `get_plotlyjs()` (the installed package's
    bundle) rather than a weak `"Plotly" in body` check.
- Judgment: Strong review. The race fix and the ETag/headers are real improvements. Note: the
  reviewer's suggested `assert plotly.__version__ in body` would be wrong (the JS bundle embeds the
  plotly.js version `3.6.0`, not the plotly.py package version `6.8.0`); the equality-to-
  `get_plotlyjs()` assertion is the correct, robust version pin.

### Doc Parrot
- Divergences Found: 0
- Details: the collection script didn't auto-detect `plotly_js` (a function added mid-file — a
  lineno-matching limitation), so it was reviewed manually. Its docstring ("serve the Plotly JS
  bundle shipped with the installed plotly package … offline … version-locked") matches the
  implementation, which returns exactly `get_plotlyjs()` (verified by the `body == get_plotlyjs()`
  test). Judgment: **OK**.

### Actions taken (Round 1)
- `dashboard.py`: double-checked lock for `_PLOTLY_JS`; `nosniff` header; ETag + conditional GET.
- `tests`: assert `Cache-Control`; pin served bytes to `get_plotlyjs()`; add the 304-revalidation
  test. Removed a misplaced `"cdn.plot.ly" not in body` assertion on the bundle (the Plotly bundle
  legitimately embeds `cdn.plot.ly/un/` for default map tiles; the index test covers the page not
  loading the script from the CDN).

### Carry-forward (informational, not changed)
- `Response(...)` buffers the full ~4.8MB string (no streaming). Fine for a local single-user
  dashboard; the ETag/304 path now avoids most repeat transfers.
