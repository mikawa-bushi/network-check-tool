# netdiag — design and command reference

`netdiag` is a **diagnosis** tool for production engineers. Where a monitor
answers *"is it reachable?"*, netdiag answers **"what is broken, why, and what
should I check next?"** — for this PC and for every device on the line (PLCs,
HMIs, robot controllers, sensors, gateways). It runs offline, unprivileged, on
Windows, Linux and macOS, with the standard library and `click` only.

It is installed with `pip install network-check-tool`. For a task-oriented
introduction (in Japanese) see the [README](../README.md).

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
| `netdiag` | Same as `netdiag check` (this PC only), then the next commands to try |
| `netdiag check [TARGET...] [-f FILE] [-p PORT]` | Check this PC and devices. `TARGET` = `192.168.3.39`, `192.168.3.39:502`, `PLC1=192.168.3.39:modbus,4840` |
| `netdiag scan [NETWORK...] [--save FILE]` | Discover devices (ICMP + ARP; ports only with `-p`). `NETWORK` = CIDR, `a.b.c.d-e`, or the PC's own subnets |
| `netdiag ping HOST [-c N] [-i S] [--json]` | Precise ping statistics |
| `netdiag soak [TARGET...] [-f FILE] [--duration 2h]` | Hours-long continuous measurement: outage timeline, correlation across devices, this PC's link drops/NIC errors |
| `netdiag collect [--target IP] [--no-mask] [--no-ping]` | Zip the raw output of the OS commands netdiag parses (masked) for bug reports |
| `netdiag explain [PROBE]` | With no `PROBE`, every check with its layer; with one, what it does, the manual commands, and the next steps |
| `netdiag scenario FILE` | A YAML/JSON/TOML scenario: `checks` (host probes), `devices` (device list), `config`; runs and exits like `check` |
| `netdiag snapshot save/list/diff [NAME]` | Save the network configuration while it works; diff later ("what changed?"). Windows, macOS and Linux |
| `netdiag peer serve` / `peer test ADDR` | Two-host LAN quality (loss/jitter/throughput) for closed networks; the `test` end must be 0.6.0 or later |
| `netdiag watch` | Always-on: light loop, alerts only on state change (`--no-internet` on closed networks), webhook/Slack/desktop sinks; records every sweep to the history DB (`--no-save` to skip) |
| `netdiag status` / `history` / `prune` | Watcher state, baseline & change-point analysis, retention |
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

Exit codes, the same for every command:

| code | meaning |
| --- | --- |
| 0 | healthy |
| 1 | a problem was found |
| 2 | warnings only, and everything wanted was measured |
| 3 | incomplete: no problem found, but something wanted could not be measured (shown as `?`) |
| 4 | usage error: a bad option, an unknown check name, an unreadable or invalid input file, an output path that cannot be used |
| 5 | internal error: one sentence, then the traceback on stderr |
| 130 | interrupted with Ctrl-C before a verdict existed |

### Machine-readable output

`--format json` returns `status`, `exit_code`, `host` (results + hypotheses),
`devices[]` (device, status, diagnosis) and `device_counts`. Every result has a
stable English `code` (`route.no_route`, `reach.no_arp`, `ports.closed`,
`ipconfig.duplicate_ip`, `link.half_duplex`, …); match on codes, not on the
localised `summary`.

## Intermittent drops (`netdiag soak`)

`netdiag soak -f devices.csv --duration 2h` pings every device continuously
(default every 0.2 s, 1 s timeout, ≤200 pps overall) and keeps per-device state
with bounded memory, so it can run for a shift:

- **Outages**: two or more consecutive losses (`--min-lost`) after a device has
  replied once, with start time and length. Isolated single losses are counted
  as sporadic loss instead.
- **Correlation**: outages that overlap across devices are grouped into
  incidents — *all* devices (≥80 % of those alive) → this PC's side (cable,
  NIC, the switch next to the PC); *some* → a shared switch / cable run (the
  groups of the device list are named); *single* → that device or its cable.
- **This PC**: NIC error counters that grew, and on Linux link drops
  (`carrier_changes`), are put on the same timeline (per `--bucket`). On
  Windows/macOS a drop of the PC's own link shows up as an *all devices*
  incident, which already points at this PC's side.
- **Live**: outage start/end lines on stderr as they happen; per-minute
  summaries (`--bucket`). Ctrl-C stops and still writes the report.
- **Output**: text, JSON (`kind: "soak"`), CSV (one row per device and minute,
  written progressively with `-o x.csv` so a crash keeps the data) and a
  self-contained HTML heatmap (device × time).

Exit codes: 0 clean, 1 outages, 2 sporadic loss only, 3 a device or a time
window was not measured; 4 and 5 as for `check`. A soak stopped with Ctrl-C
still reports and exits with its verdict.

## Field data (`netdiag collect`)

`netdiag collect` runs the commands netdiag itself parses on this OS (Windows:
the PowerShell collector, `ipconfig /all`, `route print`, `arp -a`, `netsh`,
pings to the gateway, an unused TEST-NET address, an unknown host and loopback;
Linux/macOS: `ip -j`, `/sys/class/net`, `ifconfig`, `netstat`, `ping`…) and
stores the **raw bytes** (no decoding, so cp932 output stays inspectable) next
to netdiag's own interpretation (`netinfo.json`, `check.json`, `ping.json`).

Masking is on by default and preserves structure: IPv4 addresses are mapped
so that /16 and /24 relationships and the last octet survive (each /16 gets
its own /16 in the private ranges, from `10.100.x.x` on, and `.0` and `.255`
stay; netmasks, loopback, link-local and public resolvers are kept). Coarser
prefixes such as a `/8` route are not kept. MACs keep the vendor (OUI) half,
in every form: `aa-bb-…`, `aa:bb:…`, the space-separated list in
`route print`, the MAC inside a DHCPv6 DUID or an interface name (`enx…`,
`wlx…`), and a MAC written without separators once it appears in any other
form. The same adapter gets the same masked MAC in every file.
IPv6 keeps the prefix type. Host names, user names, SSIDs (netsh SSID and
Profile lines, `iw`, the Wi-Fi check's results) and internal domains (DNS
suffixes and search domains from `ipconfig`, `resolv.conf` and `scutil`, plus
any name with three or more labels) are replaced in every file of the zip.
`--no-mask` keeps everything (internal use only). An output path that cannot
be written exits 4.

## Ping back-ends

`netdiag.measure.icmp` picks, per run: an unprivileged ICMP socket (Linux with
`net.ipv4.ping_group_range`, macOS), the Windows ICMP API (`IcmpSendEcho` via
ctypes — no admin rights, sub-millisecond timing with `perf_counter`, duplicate
replies counted), else the OS `ping` command (≈1 request/s on Windows; netdiag
adjusts `--duration` accordingly). `NETDIAG_PING_METHOD=socket|win|system`
forces one. On Windows, ARP presence is also asked directly with `SendARP`
instead of reading `arp -a`.

## Windows exe

`netdiag.exe` (built by `.github/workflows/windows-exe.yml` with PyInstaller,
attached to `v*` releases) bundles Python. Started with no arguments it checks
this PC; with a single device-list path (what Windows passes on drag & drop) it
checks those devices. Either way it prints the result, saves an HTML report
(next to the exe / the list, falling back to the home folder) and opens it,
then waits for Enter so the console window stays. Any other arguments run the
normal CLI. Set `NETDIAG_NO_OPEN=1` to skip opening the browser.

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
ライン・備考・MACアドレス・遅延上限・ロス上限・監視間隔 are accepted). A
`[[host]]` / `hosts` list whose entries give only `name` loads too, and each
name is used as the host.

A scenario file is `.yaml`/`.yml` (needs PyYAML), `.json` or `.toml`, in
UTF-8, Shift_JIS or UTF-16 (what Notepad and Excel save). Top-level keys are
`name`, `description`, `checks`, `devices`, `defaults` and `config`; `checks`
is a list of check names or of mappings with `probe` and the check's own
settings (`target`, `resolver`, `url`, …) and an optional `label`, shown as
the check's title. A check may appear more than once with different
settings; each one runs. Targets in `config.tcp_targets` go to a `tcp`
check that has no `target`; when every `tcp` check has one, they join the
first, and no target is checked twice. In YAML, `0012` stays the text
`0012`. A file netdiag cannot use (a syntax error, an unknown key or check
name, `checks` that is not a list, a bad device) exits 4 with the file name
and the problem. `--all` shows not-applicable checks, and the exit codes are
those of `check`.

## Snapshots and peer tests

`netdiag snapshot save NAME` records this PC's adapters (state, speed,
duplex, MTU, MAC), IPv4 addresses and IPv6 prefixes, routes, DNS servers
(the real upstream servers under systemd-resolved), ARP entries, the hosts
file and, on Windows, the firewall profiles; on Linux also IPv6 routes, a few
network sysctls and the listening TCP addresses. `snapshot diff NAME` compares
them as sets of stable lines: counters, lease times, ARP states, IPv6 privacy
addresses, Wi-Fi rates and line order never count as a change, and an ARP
entry counts only when the same address answers with another MAC. Exit codes:
0 no change, 2 changed, 3 nothing changed but a part could not be compared
(read on one side only), 4 no such snapshot, a damaged one, one saved before
0.6.0, a bad name, or an unsupported OS. `snapshot save` exits 3 and saves
nothing when it could not read this PC's configuration.

`netdiag peer serve` answers only netdiag's own 16-byte requests. It never
answers a reply, a privileged source port or its own port, so two responders
cannot loop. Each reply is a fixed-format 16 bytes, no larger than the
request: a marker and a digest of the request, none of the request's own
bytes. It sends at most 200 replies in any second. A request with a forged
source address still gets its reply sent to that address, so run
`peer serve` only while you test. It listens on every adapter so that the
other PC can reach it; `--host` limits it to one address. It refuses to start
(exit 4) when it cannot listen. `netdiag peer test ADDR` matches each reply to
its request, so late or duplicated replies do not count as loss. With no reply
at all it says whether nothing listens there or nothing answered, and exits 3
instead of reporting 100 % loss; a throughput test that cannot connect also
exits 3. A 0.6.0 `peer test` also works against a 0.5 `peer serve`; a 0.5
`peer test` gets no reply from a 0.6.0 `peer serve`, so upgrade the `test`
end too.

## Always-on watching (`netdiag watch`)

- **Tier 1 — every tick, cheap:** `interface, gateway, external, dns` (or
  `interface, gateway, neighbor` with `--no-internet`).
- **Tier 2 — on confirmed degradation, once:** the full sweep runs a single time
  and its root cause is attached to the alert.
- **Flap damping:** `--fail-threshold` / `--recover-threshold` consecutive ticks.
- Alerts: `~/.netdiag/alerts.jsonl` (+ `.log`), optional `--webhook-url`,
  `--slack-url`, `--desktop`; state in `~/.netdiag/state.json`.
- History is downsampled hourly after 7 days and kept 90 days (`prune`).

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
├── measure/       icmp.py (socket / Windows API / system ping, streaming), winicmp.py, tcp.py, dns.py, stats.py
├── probes/        interface, link, ipconfig, gateway, neighbor, icmp/external, dns, dualstack,
│                  tcp, firewall, http, tls, system/clock, wifi, traceroute, device (route/reach/ports)
├── diagnosis/     engine (dep-graph, transitive gating), rules (advice), baseline, changepoint
├── runner.py      run_check(), live NIC-error / link-flap probe
├── soak.py        long-run recorder: outages, incidents, NIC events (report/soak.py renders it)
├── collect.py     raw command capture + structure-preserving masking
├── launcher.py    netdiag.exe: double-click / drag & drop
├── inventory.py   device lists (CSV/YAML/JSON/TOML), port presets
├── scan.py        subnet discovery
├── oui.py         MAC vendor lookup from an OUI database already on the system
├── report/        text / JSON / HTML / CSV
├── i18n.py        ja / en
├── cli/           click commands (check, scan, ping, soak, collect, explain, scenario, snapshot, peer, watch …)
├── snapshot/ peer/ scenario/ alerting.py watch.py storage.py scheduler.py plugins.py
```
