# netdiag — design and command reference

`netdiag` is a **diagnosis** tool for production engineers. Where a monitor
answers *"is it reachable?"*, netdiag answers **"what is broken, why, and what
should I check next?"** — for this PC and for every device on the line (PLCs,
HMIs, robot controllers, sensors, gateways). It runs offline, unprivileged, on
Windows, Linux and macOS, with the standard library and `click` only.

It ships in the same package as `network-checker`; installing the package gives
you both commands. For a task-oriented introduction (in Japanese) see the
[README](../README.md).

## Design in one picture

```
 collect facts                  measure precisely               interpret                    present
 netinfo/ (per OS)              measure/                        diagnosis/                   report/
  NICs, speed/duplex,  ─┐        ICMP socket / system ping ─┐    dependency-graph engine:     conclusion-first text (ja/en)
  error counters,       │        TCP ports (open/closed/     │    lowest failing layer =  ──▶ JSON (stable codes)
  IPs + flags, routes,  ├──▶     silent), DNS client,        ├──▶ root cause; the rest are    self-contained HTML
  ARP, DNS servers      │        loss CI, percentiles, jitter│    consequences                 CSV for Excel
 inventory.py (devices) ┘                                    ┘    probes give contextual advice
```

- **`netinfo/`** collects this host's network configuration once per run into
  one OS-neutral model (`HostNetwork`: interfaces, addresses with flags such as
  *duplicate*, routes, ARP, resolvers). Linux uses `ip -j` + sysfs, Windows one
  PowerShell call with `ConvertTo-Json` (falling back to `route print`/`arp -a`,
  parsed language-independently), macOS `ifconfig`/`netstat`/`arp`/`scutil`.
- **`measure/`** holds the measurement primitives every probe shares:
  - `icmp.py` — an fping-style pinger on an unprivileged ICMP datagram socket
    (one thread, many targets, paced at ≤200 packets/s, microsecond timing,
    duplicate-reply detection), falling back to the system `ping`, whose output
    is parsed per reply line in any language (Japanese/English Windows,
    iputils, BusyBox, macOS). Windows' own statistics count "destination host
    unreachable" replies as received; netdiag does not.
  - `tcp.py` — TCP connect checks with an honest three-way verdict.
  - `dns.py` — a minimal UDP DNS client to query each resolver directly.
  - `stats.py` — Wilson 95 % interval for loss, percentiles, jitter.
- **`probes/`** each answer one question about one layer and only record *what*
  they saw (plus a stable `code` such as `reach.no_arp`). Every probe degrades to
  `SKIP` with a reason when a tool or permission is missing — "could not
  measure" never reads as "device down".
- **`diagnosis/`** runs probes in dependency order. A probe whose dependency
  failed — directly or transitively — is recorded as a consequence instead of
  being measured as a competing failure; the lowest failing probe is the root
  cause. The cause probe's `advice()` turns the evidence into a plain-language
  explanation and numbered next steps (with the real addresses and names).
- **`runner.py`** runs one complete check: the host sweep, one ping sweep for all
  devices with ARP read before and after, a `route → reach → ports` diagnosis per
  device (in parallel), and a final look at NIC error counters that grew during
  the measurement.
- **`report/`** renders conclusion-first; all human text is Japanese or English
  (`i18n.py`), machine output stays English.

## Checks

### This PC (default sweep of `netdiag check`)

| Probe | Layer | What it finds |
|---|---|---|
| `interface` | L2 | No link, no IPv4, only 169.254.x.x (no DHCP on an equipment network), address unusable because of an IP conflict |
| `link` | L1 | Half duplex (auto-negotiation mismatch), 10 Mbps links, NIC error ratio (CRC/collisions) |
| `link_errors` | L1 | NIC errors that **increased during the test** (cable/connector/noise/port) |
| `ipconfig` | L3 | Duplicate IP flagged by the OS, the same subnet on two NICs, default gateways on several NICs, masks broader than the private block |
| `gateway` | L3 | Default gateway reachability with full ping statistics; "no gateway" is *not applicable* on a closed network |
| `neighbor` | L2 | ARP table: duplicate IPs, unresolved/failed gateway MAC |
| `firewall` | L4 | Local drop/reject rules (nftables/iptables) or Windows Firewall profiles |
| `system`, `clock` | SYSTEM | Memory/CPU/conntrack exhaustion; NTP synchronisation |
| `wifi` | L1 | Signal, SSID, rate (`iw` on Linux, `netsh wlan` on Windows, any language) |
| `tcp`, `http`, `tls` | L4/L7 | Only when `--tcp` / `--http-url` are given |
| `external`, `dns`, `dualstack` | L3/L7 | Only with `--internet` (or `--external-ip` / `--dns-name`) |
| `traceroute`, `icmp` | L3 | Opt-in (`--only traceroute`); `icmp` backs ad-hoc pings |

Closed equipment networks are the default: without `--internet`, no internet
reachability or DNS is expected, and a missing default gateway is normal.

### Each device (`route → reach → ports`)

| Probe | What it decides |
|---|---|
| `route` | Is the device in one of this PC's subnets (which NIC), behind a gateway, or unreachable by design (**subnet mismatch** — with the address range to configure)? Also: link-down NIC for that subnet, the PC's own address, network/broadcast addresses, overlapping NICs, unresolvable names |
| `reach` | Ping statistics (loss with a 95 % upper bound, median/p95/max, jitter) plus **fresh ARP evidence**: no ARP answer = powered off / miswired / wrong IP; ARP answer without ping = alive but ICMP disabled. Duplicate replies or a MAC different from the device list = IP conflict or replaced device. Per-device `rtt_warn_ms` / `loss_warn_pct` |
| `ports` | Each listed TCP port: open, closed (alive, nothing listening) or no answer; flaky ports. With no sign of life at all (no ping, no ARP, no TCP answer) the verdict is "no response", not "port blocked". Open ports settle a device that merely ignores ping |

ARP evidence counts only if it is fresh: an entry that appeared during the test,
or one Linux reports as confirmed within the test window. A cache entry left over
from before proves nothing — the device may have died seconds ago.

## Command reference

| Command | Purpose |
|---|---|
| `netdiag check [TARGET...] [-f FILE] [-p PORT]` | Check this PC and devices. `TARGET` = `192.168.3.39`, `192.168.3.39:502`, `PLC1=192.168.3.39:modbus,4840` |
| `netdiag scan [NETWORK...] [--save FILE]` | Discover devices (ICMP + ARP; ports only with `-p`). `NETWORK` = CIDR, `a.b.c.d-e`, or the PC's own subnets |
| `netdiag ping HOST [-c N] [-i S] [--json]` | Precise ping statistics |
| `netdiag explain [PROBE]` / `list-probes` | What each check does, the manual commands, and the next steps |
| `netdiag scenario FILE` | A YAML/JSON scenario: `checks` (host probes), `devices` (device list), `config` |
| `netdiag snapshot save/list/diff [NAME]` | Save the network configuration while it works; diff later ("what changed?") |
| `netdiag peer serve` / `peer test ADDR` | Two-host LAN quality (loss/jitter/throughput) for closed networks |
| `netdiag watch` | Always-on: light loop, alerts only on state change (`--no-internet` on closed networks), webhook/Slack/desktop sinks |
| `netdiag status` / `history` / `prune` / `monitor` | Watcher state, baseline & change-point analysis, retention, periodic recording |
| `netdiag service print/install` | systemd unit for `watch` |

Global options: `--lang ja|en` (default: `NETDIAG_LANG`, then the locale, else
Japanese), `-v/--verbose`, `--log-file`. `python -m netdiag` works where the
`netdiag` script is not on `PATH`.

### Useful `check` options

- `-f/--file` (repeatable): device list — CSV saved from Excel (Shift_JIS,
  UTF-8 with/without BOM, UTF-16; comma/tab/semicolon), YAML, JSON or TOML.
  Japanese or English headers; header-less IP lists work too.
- `-p/--port` (repeatable): port added to every device; numbers or names
  (`modbus` 502, `enip` 44818, `opcua` 4840, `s7` 102, `melsoft` 5007,
  `fins` 9600, `iec104` 2404, `dnp3` 20000, `mqtt`, `http`, `https`, `ssh`,
  `telnet`, `ftp`, `vnc`, `rdp`, `smb`).
- `--count N` / `--interval S` / `--timeout S` / `--duration S`: sampling.
  Loss-free samples needed for a 95 % bound below 5 % / 1 % / 0.1 %:
  ~73 / ~380 / ~3,800.
- `--format text|json|html|csv` and `-o/--output FILE` (format inferred from
  the extension; CSV is written with a BOM for Excel).
- `--all` shows not-applicable checks; `--reasoning` shows the engine's steps;
  `--save` records metrics (per device, keyed by address) for `history`;
  `--no-host` checks devices only; `--jobs N` parallelises the host sweep.

Exit codes: **0** healthy, **1** a failure was found, **2** warnings only.

### Machine-readable output

`--format json` returns `status`, `exit_code`, `host` (results + hypotheses),
`devices[]` (device, status, diagnosis) and `device_counts`. Every result has a
stable English `code` (`route.no_route`, `reach.no_arp`, `ports.closed`,
`ipconfig.duplicate_ip`, `link.half_duplex`, …); match on codes, not on the
localised `summary`.

## Device lists and scenarios

```csv
名前,IPアドレス,ポート,グループ,MACアドレス,備考
PLC-1,192.168.3.39,melsoft,1ライン,,
```

```yaml
# netdiag scenario line1.yaml
name: 1ライン ネットワーク点検
config: {count: 50}
checks: [interface, link, ipconfig, neighbor]
devices:
  - {name: PLC-1, host: 192.168.3.39, ports: melsoft}
  - {name: IoTゲートウェイ, host: 192.168.3.50, ports: [modbus, opcua], loss_warn_pct: 1}
```

Recognised columns/keys: name, host, ports, group, note, mac, rtt_warn_ms,
loss_warn_pct, interval (Japanese aliases such as 名前・IPアドレス・ポート・
ライン・備考・MACアドレス・遅延上限・ロス上限・監視間隔 are accepted). The same
files drive `network-checker start-monitoring --config`.

## Always-on watching (`netdiag watch`)

- **Tier 1 — every tick, cheap:** `interface, gateway, external, dns` (or
  `interface, gateway, neighbor` with `--no-internet`).
- **Tier 2 — on confirmed degradation, once:** the full sweep runs a single time
  and its root cause is attached to the alert.
- **Flap damping:** `--fail-threshold` / `--recover-threshold` consecutive ticks.
- Alerts: `~/.netdiag/alerts.jsonl` (+ `.log`), optional `--webhook-url`,
  `--slack-url`, `--desktop`; state in `~/.netdiag/state.json`.
- History is downsampled hourly after 7 days and kept 90 days (`prune`).

For per-device long-term monitoring with a dashboard, use
`network-checker start-monitoring --config devices.csv --diagnose`: when a
device degrades, `netdiag.runner.diagnose_target()` checks this PC's network
first and then that device, and the root cause is attached to the alert.

## Extending with plugins

A separate package can add probes via an entry point — no fork required:

```toml
[project.entry-points."netdiag.probes"]
my_probe = "mypkg.probes"
```

The module's `@register`-decorated `Probe` subclasses join the registry. Set
`scope = "target"` for a per-device probe, `default = False` for opt-in,
`requires_internet = True` for internet-only checks, and override
`advice(result, results)` (or set `means` / `runbook`) to supply the diagnosis
narrative. Use `netdiag.i18n.Text(ja, en)` / `tr(ja, en)` for bilingual text.

## Architecture map

```
netdiag/
├── core/          CheckResult (+ stable code), Status, Probe (scope/default/advice), registry, safe subprocess
├── netinfo/       model.py + linux.py / windows.py / macos.py collectors, snapshot cache
├── measure/       icmp.py (socket + system ping), tcp.py, dns.py, stats.py
├── probes/        interface, link, ipconfig, gateway, neighbor, icmp/external, dns, dualstack,
│                  tcp, firewall, http, tls, system/clock, wifi, traceroute, device (route/reach/ports)
├── diagnosis/     engine (dep-graph, transitive gating), rules (advice), baseline, changepoint
├── runner.py      run_check(), diagnose_target(), live NIC-error probe
├── inventory.py   device lists (CSV/YAML/JSON/TOML), port presets
├── scan.py        subnet discovery
├── oui.py         MAC vendor lookup from an OUI database already on the system
├── report/        text / JSON / HTML / CSV
├── i18n.py        ja / en
├── cli/           click commands (check, scan, ping, explain, scenario, snapshot, peer, watch …)
├── snapshot/ peer/ scenario/ alerting.py watch.py storage.py scheduler.py plugins.py
```
