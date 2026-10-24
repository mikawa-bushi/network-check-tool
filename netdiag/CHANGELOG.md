# Changelog — netdiag

All notable changes to the `netdiag` package are documented in this file.

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
