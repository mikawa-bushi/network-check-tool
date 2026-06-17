# netdiag — local-first network & OS trouble isolation

`netdiag` is a **diagnosis** tool, not just a monitor. Where the original
`network-checker` answers *"is the host reachable?"*, `netdiag` answers
**"which layer is broken, why, and what should I check next?"** — so a user can
isolate network and OS problems entirely on their own machine, offline, without
root.

It ships in the same package as `network-check-tool`; installing the package
gives you both the `network-checker` and the `netdiag` commands.

## Design in one picture

```
probes/            diagnosis/              report/
  (collect facts)    (interpret)             (present)
  interface  ─┐
  gateway   ──┤    dependency-graph         conclusion-first text
  dns       ──┼──▶ engine: lowest      ──▶  JSON  (scripts/CI, exit codes)
  tcp/http  ──┤    failing layer = root      self-contained HTML (offline)
  system    ──┘    cause; everything
                   above it = consequence
```

The three layers are deliberately decoupled:

- **Probes** (`netdiag/probes/`) each answer one narrow question about one layer
  (L1 Wi-Fi → L2 ARP → L3 routing/reachability → L4 ports/firewall → L7
  DNS/HTTP/TLS, plus a SYSTEM layer for host resources/clock). A probe only
  records *what* it saw; it never decides *why*. Every probe degrades to a
  `SKIP` (with a reason) when its tool is missing or it lacks permission, so a
  diagnosis on a locked-down host is still useful.
- **Diagnosis** (`netdiag/diagnosis/`) evaluates probes in dependency order. A
  probe runs only after its dependencies pass; if a dependency failed, the
  dependent is recorded as a *consequence*, not re-measured as a competing
  failure. The lowest failing layer is the root cause.
- **Report** (`netdiag/report.py`) renders the result leading with the cause and
  the next steps. HTML output is fully self-contained (no CDN/JS) for offline use.

Everything runs on the **standard library**; `click` is the only hard
dependency. `pyyaml` is optional and only used for YAML scenarios.

## Quick start

```bash
# Full layered sweep — prints the root cause and what to do next.
netdiag check

# Add service-level checks.
netdiag check --tcp api.internal:443 --http-url https://api.internal/health

# Learn how a check works and the manual commands that do the same thing.
netdiag explain gateway
```

`netdiag check` exits **0** when healthy, **1** when a cause was found, **2** for
warnings only — so it drops straight into scripts and CI.

## Worked example

When DNS is the problem but the network is fine, the engine says so and folds the
symptoms away:

```
✔ [L3]     external    1.1.1.1 reachable (16.0ms, 0% loss)
✘ [L7]     dns         resolution of intranet.corp failed (resolvers: 10.0.0.53)
— [L3]     dualstack   not evaluated: upstream dns failed
— [L7]     http        not evaluated: upstream dns failed

━━━ 診断 (Diagnosis) ━━━
1. 推定原因: dns — Connectivity is fine but names don't resolve. [確信度: 高]
     根拠: External reachability is OK, so the fault is above it.
     次の確認手順:
       - Test the resolver directly: nslookup example.com <resolver-ip>
       - Try a public resolver: nslookup example.com 1.1.1.1
     これに連動して失敗した項目 (原因ではなく結果): dualstack, http
```

## Always-on watching (`netdiag watch`)

`netdiag check` is a *spot* diagnosis. For continuous operation, `netdiag watch`
runs as a low-overhead daemon built on a **two-tier** model:

- **Tier 1 — every tick, cheap:** only a small probe set
  (`interface, gateway, external, dns`) runs, so it is safe to run continuously
  even on a busy/closed network. No heavy or intrusive probes in the steady state.
- **Tier 2 — on confirmed degradation, once:** the moment a *debounced* state
  machine flips to `DEGRADED`, the **full** diagnosis runs a single time to
  capture the root cause, which is attached to the alert. You pay the heavy cost
  only when something is actually wrong.

It alerts **only when the verdict changes** (healthy → degraded, degraded →
recovered), not every tick — so it is quiet until it has something to say.

```bash
netdiag watch --interval 30 --external-ip 10.0.0.1 --dns-name intranet.corp
# alerts → ~/.netdiag/alerts.jsonl  (+ human-readable ~/.netdiag/alerts.log)
# current state → ~/.netdiag/state.json
```

**Flap damping.** A single dropped ping never fires an alert. `--fail-threshold`
(default 2) consecutive bad ticks are required to declare `DEGRADED`, and
`--recover-threshold` (default 2) good ticks to call it recovered — so a flapping
link does not produce an alert storm.

**Alert format (file/JSONL).** Zero dependencies, offline, easy to tail or ship.
One self-contained JSON object per transition:

```json
{"event":"degraded","cause":"dns","confidence":"high",
 "summary":"Connectivity is fine but names don't resolve — a DNS/resolver fault.",
 "evidence":["External reachability is OK, so the fault is above it."],
 "consequences":["http","tls"],
 "runbook":["Test the resolver directly: nslookup ..."],
 "iso":"2026-06-10 20:36:17"}
```

A recovery event records how long the outage lasted (`duration_s`). The sink is
pluggable (`AlertSink`), so webhook / syslog / email delivery can be added later
without touching the watcher.

Run a single tick (for testing or cron-style use) with `--once`; disable the
Tier-2 escalation with `--no-escalate`.

### Running it as a service, checking status, bounding disk

```bash
# Install a user systemd unit and enable it (commands are printed, not run for you).
netdiag service install --external-ip 10.0.0.1 --dns-name intranet.corp
systemctl --user daemon-reload && systemctl --user enable --now netdiag-watch

# See the current state and recent alerts from any process.
netdiag status            # ✔/✘ current state, since when, last alerts
netdiag status --format json
```

A long-running watcher manages its own disk: every hour it **downsamples** raw
samples older than 7 days into hourly rollups (count/avg/min/max) and drops
rollups older than 90 days, so full-resolution recent data stays available for
baselining while history stays bounded over months. Run it by hand any time with
`netdiag prune --raw-days 7 --rollup-days 90`.

## Command reference

| Command | Purpose |
|---|---|
| `netdiag check` | Run the full layered diagnosis; report root cause + runbook. |
| `netdiag ping <host>` | Ad-hoc ICMP ping (system `ping`, no root). |
| `netdiag explain [probe]` | Teaching mode: what/why each check does + manual commands. |
| `netdiag list-probes` | List registered probes (incl. plugins) by layer. |
| `netdiag snapshot save [name]` | Save current network config (routes/DNS/firewall/...). |
| `netdiag snapshot diff [name]` | Diff current config vs a saved snapshot — *"what changed?"*. |
| `netdiag peer serve` / `peer test <addr>` | Two-host LAN quality test (loss/jitter/throughput), offline. |
| `netdiag scenario <file>` | Run a declarative YAML/JSON diagnosis scenario. |
| `netdiag watch` | **Always-on**: light loop + alert (file/JSONL) only when the verdict changes. |
| `netdiag status` | Live view of the watcher: current state + recent alerts (text/JSON). |
| `netdiag prune` | Retention: downsample old raw samples to hourly rollups. |
| `netdiag service print/install` | Generate a systemd unit to run `watch` as a service. |
| `netdiag monitor` | Periodically run the diagnosis and record metrics to history. |
| `netdiag history` | Baseline (median/MAD), change-point and flapping analysis. |

### Useful flags on `check`

- `--only a,b,c` / `--skip a,b` — restrict the probe set.
- `--format text|json|html` and `--output FILE`.
- `--reasoning` — show the engine's step-by-step reasoning in the report.
  (The global `-v`/`--verbose` is separate: it controls netdiag's logging.)
- `--save` — record every metric to the history DB for baselining.

## Key features, and what they isolate

- **Dependency-graph diagnosis** — names the *one* layer at fault instead of a
  wall of red, and lists the consequent failures separately.
- **Path trace** (`--only traceroute`) — *where* the path breaks (your router vs
  the ISP's third hop), which decides who you call.
- **HTTP phase timing** — splits "slow web" into DNS / connect / TLS / first byte,
  so you can tell a network problem from a slow server.
- **TLS/clock checks** — catch expired certs and clock skew that masquerade as
  network failures.
- **Dual-stack parity** — finds the common "IPv6 broken, IPv4 fine" asymmetry.
- **Firewall correlation** — attributes a filtered port to the host's own rules
  vs the remote side.
- **System layer** — memory/conntrack/load/NTP problems that present as network
  faults but aren't.
- **Snapshot diff** — surfaces the *config change* (a VPN rewriting the default
  route, an overwritten `resolv.conf`) behind a "suddenly broke" incident.
- **Baseline + change-point** — judges latency against *this host's* normal, and
  reports *when* it degraded; distinguishes flapping from a clean outage.
- **Peer mode** — measures LAN quality between two of your own machines, the only
  way to assess the network in a fully closed environment.
- **`explain` teaching mode** — shows the manual commands so users learn to
  isolate faults by hand, not just read a verdict.

## Scenarios

Encode an environment's triage steps once and hand them out:

```yaml
# office.yaml  (run: netdiag scenario office.yaml)
name: 社内ネットワーク診断
description: Office network checks
checks:
  - probe: gateway
  - probe: tcp
    target: proxy.corp.local:8080
    label: 社内プロキシ
  - probe: dns
    resolver: 10.0.0.53
    expect: intranet.corp.local
```

JSON works with no extra dependency; `.yaml`/`.yml` needs the `scenario` extra
(`pip install network-check-tool[scenario]`).

## Extending with plugins

A separate package can add probes via an entry point — no fork required:

```toml
[project.entry-points."netdiag.probes"]
my_probe = "mypkg.probes"
```

The named module's `@register`-decorated probe classes join the registry exactly
like the built-ins.

## Architecture map

```
netdiag/
├── core/            CheckResult, Status, Probe base, registry, safe subprocess util
├── probes/          interface, neighbor, gateway, icmp, traceroute, dns, dualstack,
│                    tcp, firewall, http, tls, system, wifi
├── diagnosis/       engine (dep-graph), rules/runbook, baseline, changepoint
├── snapshot/        capture + diff of network configuration
├── peer/            UDP/TCP responder + client (loss/jitter/throughput)
├── scenario/        YAML/JSON scenario loader
├── alerting.py      debounced state machine + file/JSONL alert sinks
├── watch.py         always-on watcher: tier-1 loop + diagnose-on-event escalation
├── storage.py       stdlib sqlite3 time-series + retention (downsample/rollup)
├── scheduler.py     threading-based periodic monitor
├── report.py        text / JSON / self-contained HTML
├── plugins.py       entry-point probe discovery
└── cli.py           click CLI (check/ping/explain/snapshot/peer/scenario/
                     watch/status/prune/service/monitor/history)
```
