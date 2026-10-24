# Plan: issue #35 — honor scenario custom DNS resolver (M5)

## Goal (verbatim from issue #35)

> A scenario that specifies a custom DNS resolver actually queries that resolver, instead of the
> setting being silently ignored.

## Root cause

`scenario/loader.py` maps the scenario `resolver:` field to `cfg["_resolver"]`, but
`DnsProbe.check` only uses `socket.getaddrinfo` (the **system** resolver) and never reads
`_resolver`. `getaddrinfo` cannot target a specific server, so honoring `_resolver` needs a
different query path.

## Approach (in `netdiag/probes/dns.py`)

Query the specified server with `nslookup` (already listed in `DnsProbe.manual_commands`, and the
standard "active probe shells out" pattern in netdiag), parsed in a robust, stdlib-only way.

- `_parse_nslookup_addresses(text) -> List[str]`: collect each `Address:`-line value that does
  **not** contain `#`. The server line is `Address: <server>#53` (has the port); answer lines are
  `Address: <ip>` (no port) — so the `#` test cleanly excludes the server's own address and keeps
  A/AAAA answers. Returns sorted unique addresses.
- `_resolve_via_server(name, server, timeout) -> Optional[List[str]]`:
  `run(["nslookup", name, server], timeout=...)`, parse, return the addresses or `None` if none.
- `DnsProbe.check`:
  - `resolver = ctx.get("_resolver")`.
  - If `resolver` is set **and** `have("nslookup")`: resolve via `_resolve_via_server`; put
    `"resolver": resolver` in `detail` and mention it in the summary.
  - If `resolver` is set but `nslookup` is unavailable: return `WARN` with a clear message
    ("custom resolver X requested but nslookup unavailable") — **do not** silently fall back to the
    system resolver (that's the bug we're fixing).
  - Otherwise (no `resolver`): the existing `socket`-based `_resolve` path, unchanged.
  - The OK / slow-WARN(>1000ms) / FAIL result shape and timing are shared across both paths.

## Alternatives considered / rejected

- **Hand-rolled UDP DNS query in stdlib**: rejected — correct binary DNS parsing (name compression,
  TCP fallback, EDNS) is a large, bug-prone surface for a ★★ item; `nslookup` offloads the protocol
  to the OS and matches netdiag's subprocess-probe style.
- **Silently fall back to the system resolver when nslookup is missing**: rejected — that
  reproduces the original "silently ignored" defect.

## Out of scope

- Changing the default resolution path; `DnsProbe`'s name/layer/deps; the loader mapping (it
  already produces `_resolver`).

## Testing (acceptance scenarios → tests) — `tests/test_dns_resolver.py`

- `_parse_nslookup_addresses`: canned Linux `nslookup` output → the two answer IPs (server `#53`
  line excluded); garbage/empty → `[]`.
- Custom resolver honored: monkeypatch `dns.have`→True and `dns.run`→canned output, ctx
  `_resolver="8.8.8.8"` → `OK`, `detail["resolver"]=="8.8.8.8"`, an answer IP present, and `run`
  was called with `nslookup` + the server.
- Default path unchanged: no `_resolver` → monkeypatch `dns._resolve` to a sentinel; assert it's
  used and `dns.run` is **not** called.
- Graceful degrade: `_resolver` set but `have("nslookup")`→False → `WARN` mentioning the resolver,
  no crash, no silent system fallback.

## Versioning

Behaviour fix in `netdiag` → bump `netdiag/__init__.py` (0.1.6 → 0.1.7) + `netdiag/CHANGELOG.md`.
