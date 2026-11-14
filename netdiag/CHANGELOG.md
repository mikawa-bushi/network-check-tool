# Changelog — netdiag

All notable changes to the `netdiag` package are documented in this file.

## [Unreleased]

netdiag's verdicts now follow what it measured. A check that could not
measure says "not measured" instead of passing, several exit codes changed,
and the unattended-monitoring commands are gone. Read "Upgrading from 0.5"
before you update scripts, or a PC that ran `netdiag service install`.

### Fixed

#### Verdicts that were wrong

- A check that measured nothing no longer passes. A probe that crashed, tool
  output netdiag could not parse, a missing tool or an empty run is now
  "not measured" (`?`, 測定不可), and `check` exits 3 instead of printing
  "no problems found" and exiting 0.
- A misspelled `--only` or `--skip` name, or a device check named without a
  device, exits 4 with a hint. It used to run nothing and pass.
- A check is skipped only when a check on its own path failed, and the
  "not evaluated" line names that check.
- An unplugged PC with virtual adapters (VMnet, vEthernet) is no longer
  healthy. A line NIC stuck on 169.254.x.x is named even while Wi-Fi works.
- A router that ignores ping but answers ARP or a TCP connection is
  `gateway.icmp_blocked`, a warning, and the external, DNS and TCP checks
  still run. A router that died seconds before the check still fails. A PPP
  or full-tunnel default route has no router to ping.
- A DNS server that times out, refuses or sends a damaged reply counts as
  down, and all servers down is a failure even when a cached answer exists.
- The firewall check counts only rules that block this PC's own traffic.
  Stock firewalld rules and Docker's forwarding rule no longer cause a
  warning.
- Normal states are no longer warnings: idle Wi-Fi, no NTP on a closed line,
  and one multipath default route read as two rival gateways. An IPv4-only PC
  is single-stack again.
- Routes follow the OS. On Linux, metrics, policy rules (Tailscale, wg-quick)
  and multipath defaults are read as the kernel uses them, and a PC without
  iproute2 reads `/proc` and `/sys`. Windows reads only active routes and
  ignores disconnected adapters. macOS reads abbreviated routes and utun
  addresses.
- WireGuard, PPP and other links without ARP are never judged by ARP. Invalid
  (無効) rows of Windows `arp -a` count as no answer. CRC errors are counted
  once in the NIC error total.
- Ping through the system `ping` no longer stops at "Network is unreachable"
  and reports no loss: each request that falls due counts as lost. Requests
  that were never sent, or whose wait Ctrl-C cut short, are not counted.
- TCP connect times no longer include the name lookup. A port outside 1 to
  65535 is an error instead of being tested as another port.
- A loss above 99.9 % that is not total shows as ">99.9%", not "100.0%".
- `--reasoning` shows the engine's steps, for `check` and for every pass of a
  scenario.

#### The wrong device checked

- A zero-padded address such as `192.168.001.010` means `192.168.1.10`
  everywhere: ledger cells, device arguments, `--tcp`, `--external-ip`, scan
  ranges and `netdiag ping`. The report shows that form. The OS used to read
  each padded part as octal and check another device.
- A device named `#1プレス` is a device, not a comment. A row number in a
  ledger without a header is not read as a port. TCP ports come only from a
  ports column, and a スイッチポート or HUBポート column is skipped with a
  notice on stderr.
- The header row is found below a title row and in full-width letters. A host
  column named for its device, such as 機器IP, PLC IP or IPアドレス(PLC), is
  read as the host.
- A silent device that only the default gateway leads to fails on the route
  ("subnet mismatch?") instead of showing "route OK".
- ARP counts as evidence only on a link that uses ARP, and a device counts as
  present only when its entry was confirmed during the test.
- HTTP tries addresses in the OS's order, follows redirects and encodes
  non-ASCII paths. TLS skips an `http://` URL and gives the dates of an expired
  or not-yet-valid certificate. Only an `https://` `--http-url` is the TLS
  target.
- Traceroute counts a host name or IPv6 target as reached, and says so when a
  trace is cut off at 60 s.
- `scan` sweeps every address of a range, limits the default networks per NIC,
  refuses IPv6 ranges by name, and no longer reports one address on two NICs
  as an IP conflict. An all-zero MAC (loopback) has no vendor.

#### Results lost

- A report that cannot be saved goes to stdout with one sentence, and the
  exit code is 4. `soak` saves it next to the planned file instead, or prints
  it.
- `check`, `soak` and `scenario` check `-o` before measuring. A missing folder,
  or the device list or scenario being read, exits 4. `-o -` is stdout.
- `scan --save` refuses an existing file and a missing folder before the
  sweep, prints the results first, and keeps the ports found open with `-p`.
- `--log-file` naming a file being read exits 4. A crashed check used to
  append its traceback to the device list.
- The report no longer depends on a history database, so a damaged or locked
  `history.db` cannot lose it (`check --save` is removed).
- The exe never replaces an earlier report. Report names carry the seconds,
  and `-2`, `-3` on a clash. A dropped workbook, several files or a folder
  get a sentence that says what to drop. After an internal error the window
  waits too. It waits for Enter only when stdin is a console, so a batch job
  no longer hangs.

#### Windows and Japanese output

- html, json and csv sent to a redirected stdout are the UTF-8 bytes a file
  gets, for `check`, `scan`, `soak` and `scenario`. Japanese Windows used to
  write cp932 under a utf-8 header, and Excel showed an empty row between CSV
  rows.
- Colour and the progress line first switch a Windows console to VT mode, or
  stay off. Raw escape codes no longer appear.
- Table columns line up on a Japanese console, including an interactive one
  that Python reports as utf-8. The device table no longer cuts "1000/1000".
- `--lang` takes effect before the command line is parsed, so help and usage
  errors follow it. Under `LANG=C`, netdiag stays Japanese. Each help text has
  one translation, so help no longer shows another command's wording.
- In Japanese, a usage error starts with a Japanese sentence that points at
  `--help`.

#### soak

- Time and devices that were not measured are not clean. A device with no
  samples (`soak.not_measured`), more than 5 s with no request
  (`soak.unmeasured_time`, such as sleep or a frozen process) and a pinger
  that stopped before the planned end (`soak.stopped_early`) give exit 3, or
  1 when an outage was also found. A list with no resolvable name ends at once.
- The heatmap draws every cell of the run, grey where nothing was measured.
  The HTML no longer draws a "not measured" finding as a failure.
- Drops are grouped by address and judged against the devices answering at
  that moment. With none to compare against, as in any one-device soak,
  `soak.outage_unlocated` says where the drop was cannot be told. A device
  down from the start gets an outage, not "sporadic loss", and a live
  "dropped" line.
- The run is timed on a clock that counts sleep, so a clock change neither
  ends nor stretches it. On Windows, where that clock may stop during sleep,
  a forward clock step of more than 30 s that it did not see counts as sleep.
- Bad options and an unusable `-o` are refused before measuring. A CSV that
  becomes unwritable during the run gives a warning and the run goes on. A
  second Ctrl-C no longer loses the report.

#### scenario, snapshot, peer and collect

- `scenario` and `explain` ran no checks at all, because only `check`
  registered the built-in probes. Every command registers them again, as
  before 0.3.0.
- In a scenario, a check listed twice, or one with settings that conflict
  with another check, runs in its own pass, so every check runs. Every
  `config.tcp_targets` entry is checked once. An unusable file exits 4 with
  the file name and the problem. Shift_JIS and UTF-16 files load, and YAML
  `0012` stays `"0012"`. `--format csv` writes CSV, and `--all` exists.
- Windows and macOS snapshots record the network. They recorded nothing, so
  `snapshot diff` always said "no change". DHCP lease countdowns, counters
  and line order are not changes.
- `peer serve` answers only netdiag's own requests, and `peer test`
  matches each reply to its request, so late or duplicate replies are not
  loss.
- `collect` masks MACs, SSIDs and DNS suffixes in every file and in every
  form, including `enx…` interface names and DHCPv6 DUIDs. Masked IPv4
  addresses keep their /16 and /24 relationships. An unwritable `-o` exits 4.
- `explain link_errors` works.

### Changed

- Exit codes. 3, 4, 5 and 130 mean the same for every command:

  | code | meaning | what 0.5 did |
  | --- | --- | --- |
  | 0 | healthy | the same, and also for a run that measured nothing |
  | 1 | a problem was found | the same, and also for Ctrl-C and crashes |
  | 2 | warnings only, and everything wanted was measured | the same, and also for usage errors |
  | 3 | incomplete: no problem found, but something wanted was not measured | exited 0 |
  | 4 | usage error: a bad option, an unknown check name, an unreadable or invalid input file, an output path that cannot be used | exited 2, or 1 |
  | 5 | internal error: one sentence, then the traceback on stderr | exited 1 |
  | 130 | interrupted with Ctrl-C before a verdict existed | exited 1 |

- Exit codes per command:
  - `check` and `scenario` follow the table.
  - `ping`: 0 every reply, 2 some replies lost (was 0), 1 no reply, 3 the ping
    could not run (was 1).
  - `soak`: 0 clean, 1 outages, 2 sporadic loss only, 3 a device or a time
    window not measured (new), 4 a usage error or a report it could not save.
  - `snapshot diff`: 0 no change, 2 changed, 3 nothing changed but a part could
    not be compared (new), 4 no such snapshot, a damaged one or one saved
    before 0.6.0. `snapshot save` exits 3 and saves nothing when it could not
    read this PC's settings.
  - `peer test`: 0 loss below 5 %, 1 loss of 5 % or more or no route, 3 no
    reply or a throughput test that could not connect (new). `peer serve`
    exits 4 when it cannot listen.
  - `explain` with an unknown name exits 4 (was 1). The exe exits 4 for a
    dropped list it cannot read (was 2) and for a report it cannot save
    anywhere.
- `check --only wifi` on a PC without a Wi-Fi tool exits 3, because nothing
  that was asked for was measured.
- CSV on stdout starts with a UTF-8 BOM, as an `-o` file does, so Excel reads a
  redirected file. A script that reads it as plain UTF-8 sees U+FEFF before
  the first header. Read it as `utf-8-sig`, or use `--format json`.
- `netdiag` with no command runs `netdiag check` (this PC) and then lists the
  next commands to try. `netdiag --help` still prints the help.
- A report of `check --no-host` leaves out this PC. `--no-host` with no device
  exits 4.
- A ledger row without an address, an unknown port name or a MAC netdiag
  cannot read is an error that names the file and line (exit 4). netdiag used
  to guess. Ledger and scenario errors are in the user's language.
- The snapshot file format is version 2. `snapshot diff` refuses a snapshot
  saved by 0.5 (exit 4) and asks you to save it again.
- A 0.6 `peer test` works against a 0.5 `peer serve`. A 0.5 `peer test` gets no
  reply from a 0.6 `peer serve`.
- Result codes. New: `gateway.icmp_blocked`, `gateway.unreachable_closed`,
  `gateway.routes_unknown`, `gateway.via_link`, `dns.all_servers_down`,
  `clock.unsynced_closed`, `firewall.replies_blocked`, `route.via_default`,
  `route.default_only`, `http.dns_failed`, `http.connect_failed`,
  `http.redirect`, `tcp.bad_target`, `traceroute.timeout`,
  `soak.not_measured`, `soak.unmeasured_time`, `soak.stopped_early`,
  `soak.outage_unlocated`, and `<check>.error` for a check that crashed.
  Removed: `firewall.rules` (Linux now gives `firewall.outbound_block`),
  `neighbor.gateway_arp_failed` and `neighbor.gateway_unresolved`.
  `gateway.unmeasured`, and reach, external and ICMP results that measured
  nothing, are now "not measured" (`unknown`).
- JSON of `check`: new top-level `host_status`, a `status` in each diagnosis
  and `unknown` in `device_counts`. `host` is `null` with `--no-host`, and
  `device.interval` is gone. JSON of `soak`: new `stopped_early`,
  `planned_end`, `clock_step_s`, `devices[].unmeasured` and
  `devices[].unmeasured_s`, and a device that could not be measured has status
  `unknown` (was `skip`). In a `collect` bundle, `netinfo.json` gains `rules`,
  a `table` for each route and `arp` for each interface.

### Removed

- The commands `watch`, `status`, `history`, `prune` and `service`, and
  `check --save` and `--no-save`. Each is a usage error (exit 4). netdiag is an
  analysis tool. Each command answers a question and exits.
- The modules `netdiag.watch`, `netdiag.periodic` (with `PeriodicLoop`, public
  since 0.4.0), `netdiag.alerting` and `netdiag.storage`, and the
  `netdiag.diagnosis` names `Baseline`, `compute_baseline`, `classify`,
  `ChangePoint`, `detect_changepoint` and `detect_flapping`.
- `netdiag monitor`. Its successor `watch` is removed too.
- `netdiag list-probes`. `netdiag explain` with no argument lists every check
  with its layer.
- `netdiag.scheduler.Monitor`, which only `monitor` used.
- `netdiag.runner.diagnose_target()` and `diagnose_targets()`, which only the
  removed `network-checker` monitor used.
- The unused `interval` ledger column.

### Security

- `peer serve` answered any packet, so it could reflect traffic at a third
  address, and one forged datagram made two responders answer each other
  forever. It now answers only a 16-byte netdiag request, never a reply, a
  privileged source port or its own port. Its reply is a fixed 16 bytes (a
  marker and a digest of the request) that carry none of the sender's bytes,
  and it sends at most 200 replies per second.
- `collect` left MACs, SSIDs and DNS suffixes that it reported as masked in
  some files. They are now masked everywhere.

### Upgrading from 0.5

- In scripts that read exit codes, 2 now means warnings only, and a usage
  error exits 4. Treat 3 as incomplete, 5 as an internal error and 130 as
  Ctrl-C, which used to exit 1. See the tables above for `ping`, `soak`,
  `snapshot diff` and `peer test`.
- On a PC that ran `netdiag service install`, the unit now fails at every
  start and systemd restarts it every 10 s. Run
  `systemctl --user disable --now netdiag-watch`, then delete
  `~/.config/systemd/user/netdiag-watch.service`.
- The files `watch` wrote under `~/.netdiag` (`history.db`, `state.json`,
  `alerts.jsonl`, `alerts.log`) stay on disk. Delete them if you do not need
  them.
- Save your snapshots again, because 0.6 cannot compare a snapshot saved by
  0.5.
- Upgrade the PC that runs `peer test` as well as the one that runs
  `peer serve`.
- Remove imports of the modules and names listed under Removed. They have no
  replacement.
- In scripts that match result codes, replace `firewall.rules`,
  `neighbor.gateway_arp_failed` and `neighbor.gateway_unresolved` (see
  Changed).
- In scripts that read CSV from stdout, read it as `utf-8-sig`, or use JSON.
- `--only`, `--skip` and scenario check names that 0.5 ignored now exit 4. Fix
  the spelling.
- Ledgers with a row that has no address, an unknown port name or a MAC netdiag
  cannot read now exit 4 and name the line.

### Known limits

- On Windows, a batch ping of an IPv6 target runs `ping.exe` without a
  console, so Ctrl-C cannot reach it. netdiag stops waiting after 2 s, and
  that `ping.exe` finishes its count in the background.
- Error text from the OS `ping`, such as "Network is unreachable", stays in
  the OS language, whatever `--lang` says.
- Linux `throw` routes are ignored. Only Tailscale's exit-node LAN access uses
  them, and netdiag resolves local subnets first.
- One multipath default route is recognised on Linux only. Windows and macOS
  defaults with equal metrics still warn, and two Linux DHCP defaults with one
  metric read as one route.
- The duplicate-IP check of `neighbor` rarely fires, because the OS keeps one
  MAC per address and link.
- A linked NIC with no IPv4 address stays quiet while another NIC works, as
  bridge ports and EtherCAT NICs look the same.
- A device that answers only ARP reads as present on the first run. A second
  run within about 45 s can read "no reply".
- With more than 16 on-link devices that ignore ping, the ARP answer of one
  of them can be missed, so it is not reported as present. No device is
  reported as present by mistake.
- In a soak, a drop shared by several devices that starts within about two ping
  intervals of the end can be blamed on one device.
- On Windows, a forward clock step of more than 30 s during a soak counts as
  time not measured, even when the PC did not sleep.
- The Windows and macOS parsers, the Windows console widths and the Windows
  sleep handling are tested with reconstructed output and stubs, not on real
  machines.
- `peer serve` still replies to a request with a forged source address. Run
  it only while you test.

## [0.4.0] - 2026-11-09

### Fixed
- On an English Windows, the first Japanese alert stopped `watch` with
  `UnicodeEncodeError`. Alerts, the alert log, the watch state, snapshots and
  the daemon log are now always UTF-8. Older files written in the PC's own
  encoding still load, with any characters that don't fit shown as `�`.

### Added
- The Windows exe zip includes `LICENSE.txt` and `THIRD_PARTY_NOTICES.txt` (the
  licenses of Python, OpenSSL, click, colorama, PyYAML and PyInstaller it
  bundles).
- `netdiag.runner.diagnose_targets(hosts)`: why several hosts are unreachable,
  from one check (this PC's network once, the devices side by side).

### Changed
- `ping_many` resolves host names side by side instead of one after another
  (with DNS down, each name used to add its full timeout before any ping).
- The periodic loop behind `monitor` / `watch` is public as
  `netdiag.periodic.PeriodicLoop` (network-checker's monitor uses it too), with
  `next_run` (when the next tick is due) and `stop(wait=False)`.
- `netdiag.core.util.run_bytes()` runs a command without decoding; `run()` and
  `collect` both build on it.

### Removed
- Unused code with no caller: `netdiag.core.registry_names()` (use
  `sorted(all_probes())`), `Status.ok`, `netinfo.model.Interface.label`,
  `watch.Watcher.save` (the `save` argument stays), and the
  `evidence`, `consequences` and `runbook` parameters of
  `alerting.StateMachine.observe` (`watch` still fills those fields in when it
  escalates).

## [0.3.0] - 2026-10-28

### Added
- `netdiag.soak` + `netdiag soak`: long-run recorder (per-device outages,
  per-minute buckets; raw RTTs only for the open minute plus a fixed-size
  reservoir sample), incident grouping across
  devices, findings with stable codes (`soak.common_outage`, `soak.group_outage`,
  `soak.device_outage`, `soak.never_replied`, `soak.sporadic_loss`,
  `soak.nic_errors`, `soak.pc_link_flap`); `netdiag.report.soak` renderers.
- `netdiag.collect` + `netdiag collect`: raw command capture per OS and
  structure-preserving masking of IPv4/IPv6/MAC/host/user/SSID/domain values on
  raw bytes (Shift_JIS output stays intact).
- `netdiag.measure.winicmp`: Windows `IcmpSendEcho` pinger (no admin, perf_counter
  timing, duplicate replies, global rate limit) and `SendARP` presence check, used
  automatically on Windows; `NETDIAG_PING_METHOD=socket|win|system` overrides.
- `netdiag.measure.ping_stream()`: continuous per-request callbacks for every
  back-end; `precise_ping_available()`, `win_icmp_available()`.
- `netdiag.launcher`: the entry point of `netdiag.exe` (double-click / drag & drop).
- `LinkStats.carrier_changes` / `link_drops()` (Linux sysfs); `link_errors`
  reports a link that dropped during the test (`link_errors.link_flap`).
- `Transition.host` and `Transition.diagnosis` (the monitored device and the
  human-readable root cause), included in JSONL alerts; `StateMachine(host=...)`
  stamps the host on every transition.
- `Status.worst()` and `Status.exit_code`, shared by the check and soak reports.

### Changed
- Silent on-link devices get a direct ARP answer (`SendARP` on Windows) instead of
  relying on the OS ARP table alone.
- The CLI never crashes on output the console/pipe encoding cannot represent.

## [0.2.0] - 2026-10-21

### Added
- **Device checks**: target-scoped probes `route` (subnet mismatch, link-down NIC,
  special addresses, overlapping NICs), `reach` (ping statistics + fresh ARP
  evidence: device absent vs ICMP disabled; duplicate replies / MAC mismatch = IP
  conflict) and `ports` (open / closed / no answer / no response at all), run by
  `netdiag.runner.run_check()` for many devices with one paced ping sweep.
- **Host checks**: `link` (speed, half duplex, NIC error ratio), `ipconfig` (OS-
  detected duplicate IP, overlapping subnets, multiple default gateways, broad
  masks), `link_errors` (NIC errors that grew during the test); `interface`
  explains APIPA (169.254.x.x) and IP-conflict states.
- `netdiag.netinfo`: OS-neutral host network model with Linux (`ip -j` + sysfs),
  Windows (one PowerShell call; `route print`/`arp -a` fallbacks parsed
  language-independently) and macOS collectors.
- `netdiag.measure`: unprivileged ICMP socket pinger (fping-style, duplicate
  detection, ≤200 pps), locale-independent system-ping parsers (Japanese/English
  Windows, iputils, BusyBox, macOS), TCP checks, a stdlib DNS client, and loss
  confidence intervals / percentiles / jitter.
- `netdiag.inventory` (device lists), `netdiag.scan` + `netdiag scan`,
  `netdiag.oui` (vendor names from an OUI database already on the system),
  `netdiag.i18n` (Japanese/English), result `code`s, `Hypothesis.runbook`,
  `Probe.advice()` / `scope` / `default` / `requires_internet`.
- `netdiag scenario` accepts `devices:`; JSON/HTML/CSV output and `-o` for
  `check`/`scenario`; `netdiag ping` precise statistics; `watch --no-internet`.

### Changed
- `check` defaults to a closed network (internet probes need `--internet`).
- Engine gating is transitive: a probe whose dependency was itself skipped as a
  consequence is skipped too (previously it ran and could be reported as a second,
  unrelated cause).
- The DNS probe queries each configured resolver directly (a dead secondary DNS is
  a warning) instead of shelling out to `nslookup`.
- Command output is decoded robustly (UTF-8, then the console code page) and
  POSIX tools run under `LC_ALL=C`.
- `cli.py` and `report.py` became packages (`netdiag.cli`, `netdiag.report`);
  public names are re-exported.
- Scenario fix: `external` checks' `target` now sets `external_ip`.

### Fixed
- `dualstack` reported "neither IPv4 nor IPv6 reachable" (FAIL) on IPv4-only PCs,
  where IPv6 is simply unrouted; that is now OK ("single stack"), and a host that
  only drops ICMP gets a TCP 443 second opinion before anything is called broken.
- Windows `ping` replies such as "宛先ホストに到達できません" / "Destination host
  unreachable" were counted by ping.exe (and the old parser) as received; Japanese
  Windows output was not parsed at all (every ping read as 100 % loss).
- The Windows `netstat -rn` fallback could return the netmask `0.0.0.0` as the
  default gateway (replaced by PowerShell / `route print` parsing).
- `--duration` on Windows used a 0.2 s cadence although `ping.exe` sends one
  request per second (a 10-minute test would have taken 50 minutes).

### Removed
- `diagnosis.rules.RUNBOOKS` (narratives now live on the probes; `runbook_steps()`
  and `explain_failure()` remain) and `probes.icmp.classify_ping`
  (`probes.icmp.ping()` is kept as a compatibility wrapper).

## [0.1.7] - 2026-06-15

### Fixed (issue #35)

- **M5**: a scenario's custom DNS `resolver:` is now actually honored. It reaches `DnsProbe` as
  `ctx["_resolver"]`, which previously no probe read, so the setting was silently ignored. The DNS
  probe now queries the specified server with `nslookup` (parsing answer addresses, ignoring the
  server's own `#53` line) and surfaces the resolver in its result. If `nslookup` isn't available
  the probe warns rather than silently resolving against the system configuration. The default
  (no custom resolver) path is unchanged.

## [0.1.6] - 2026-06-15

### Performance (issue #20)

- **Parallel probes (P2)**: `Engine.run` accepts `jobs=` to run mutually-independent probes
  (those in the same dependency tier) concurrently instead of strictly one at a time. A full
  sweep that includes the active ping probes (`gateway`/`external`/`dualstack`) is much faster
  when parallelised. Dependencies are still honoured (each tier completes before the next), and
  the resulting `Diagnosis` — ordered results, reasoning, hypotheses, `healthy` — is identical to
  a serial run for any `jobs` value. `netdiag check --jobs N` exposes it; the default stays serial
  (`jobs=1`) for measurement purity, and concurrency is capped at `MAX_PARALLEL_JOBS = 4` so active
  probes don't contend for the link. Other callers (scheduler, watcher, replay) are unchanged.

## [0.1.5] - 2026-06-15

### Added / Fixed (issue #14)

- **Observable daemon error handling (E1)**: the long-running loops and fan-out sinks still
  swallow exceptions (so one failure can't kill the daemon), but now log a warning with a
  traceback instead of failing silently — `MultiSink.emit`, `Monitor`/`Watcher` tick loops,
  storage maintenance (retention), and `status`. Each module logs via
  `logging.getLogger(__name__)`.
- The package attaches a `NullHandler` to the `netdiag` logger so it stays quiet by default,
  and the CLI gains `--verbose/-v` and `--log-file` (via `_setup_logging`) to surface those
  logs on demand. The library never calls `logging.basicConfig`.

## [0.1.4] - 2026-06-15

### Fixed (issue #12)

- **Thread safety (M4)**: `Storage` now opens its sqlite connection with
  `check_same_thread=False` and serializes every operation behind a re-entrant lock, with
  WAL journaling and a busy timeout enabled. Previously `Monitor.start()` / `Watcher.start()`
  (which default to `background=True`) wrote from a worker thread and hit a cross-thread
  `sqlite3.ProgrammingError` that the loops silently swallowed — so nothing was recorded.
  The threaded background loops now record correctly; public signatures and CLI behaviour
  are unchanged.

## [0.1.3] - 2026-06-15

### Changed (issue #8)

- **Dead-code removal (M6)**: dropped the unused `user_line = "" if system else ""` local in
  `cli.py`'s `_unit_text`. No change to the generated systemd unit. Added characterization
  tests for `_unit_text` (user/system variants) plus a guard against reintroducing the dead
  local.

## [0.1.2] - 2026-06-14

### Fixed (issue #6)

- **Test reliability**: `tests/test_retention.py::test_downsample_collapses_old_keeps_recent`
  was time-dependent and failed ~1.6% of the time — when `now` fell in the last ~59 seconds of
  an epoch-hour, the 60-second seed window straddled an `ts // 3600` bucket boundary and the
  60 old samples split across two rollups. The seed window is now snapped to the start of an
  hour bucket so it always collapses into one rollup, and a clock-pinned guard test locks the
  worst case. Test-only change; `netdiag/storage.py` behaviour is unchanged.

## [0.1.1]

- Earlier releases.
