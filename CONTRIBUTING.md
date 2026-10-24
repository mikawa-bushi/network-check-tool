# Contributing to network-check-tool

Thanks for helping! This project ships two tools from one package:

- **`netdiag`** — network checks for production engineers: this PC plus every
  device on the line (route, ping statistics, ARP presence, service ports),
  diagnosed down to a root cause and next steps. Standard library + `click` only.
- **`network-checker`** — long-term ping monitoring with a Flask/Plotly dashboard.

Please read the [Code of Conduct](CODE_OF_CONDUCT.md) first.

## Development setup

```bash
git clone https://github.com/mikawa-bushi/network-check-tool.git
cd network-check-tool
poetry install --all-extras      # runtime + dev (pytest, black, pre-commit)
pre-commit install               # secret scan, black, ruff, identity checks
```

Run the tests and the same checks CI runs:

```bash
poetry run pytest
poetry run black --check netdiag/ network_check_tool/ tests/
poetry run ruff check .          # import order (isort rules)
```

CI runs on Python 3.10; please keep the code 3.10-compatible.

## Where things live (netdiag)

```
netdiag/
├── netinfo/     this host's NICs, routes, ARP, DNS — linux / windows / macos collectors
├── measure/     precise ping (ICMP socket or system ping), TCP ports, DNS client, statistics
├── probes/      one question per probe; device.py = per-device route → reach → ports
├── diagnosis/   dependency-graph engine: lowest failing layer = root cause
├── runner.py    one complete check: this PC + all devices
├── inventory.py device lists (Excel CSV / YAML / JSON / TOML)
├── report/      text / JSON / HTML / CSV renderers
├── cli/         click commands (one module per command group)
└── i18n.py      Japanese / English text
```

## Guidelines

- **Never crash on a missing tool or permission.** A probe that cannot run
  returns `SKIP` with a reason; "could not measure" must never read as "device
  down".
- **Language-independent parsing.** Windows tools print localised text
  (Japanese, English, …). Parse numbers, IPs and MACs, or use
  `ConvertTo-Json`, never the wording. Add fixtures for both languages.
- **Human text in both languages.** Use `tr(ja, en)` at run time and
  `Text(ja, en)` for class attributes. Machine output (JSON keys, result
  `code`, metric names) stays English.
- **Gentle on equipment.** Keep send rates capped; never touch TCP ports that
  were not asked for.
- **Tests without the network.** Feed fixtures or fakes (see `tests/`); a live
  test must skip cleanly when the OS does not allow it.
- Format with **black**; keep imports sorted (**ruff** `I`).

## Pull requests

1. Branch from `main` (`git checkout -b feature/short-description`).
2. Add or update tests with the change.
3. Update `README.md` / `docs/netdiag.md` and the CHANGELOGs when behaviour
   changes.
4. Make sure `pytest`, `black --check` and `ruff check .` pass.

Bug reports are most useful with the output of `netdiag check --format json`
(remove anything confidential) and the OS / language of the PC.
