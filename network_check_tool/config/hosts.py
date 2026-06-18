"""Declarative monitoring-host configuration (TOML or JSON).

Lets ``network-checker start-monitoring --config hosts.toml`` declare hosts with a
per-host ping interval and (consumed by the threshold-alert feature) warning
thresholds, instead of a single flat ``--interval`` over a comma-separated list.

TOML is parsed with the stdlib ``tomllib`` on 3.11+ and the declared ``toml``
dependency on 3.10, so the loader works on both supported interpreters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

try:  # Python 3.11+
    import tomllib

    def _loads_toml(text: str) -> dict:
        return tomllib.loads(text)

except ModuleNotFoundError:  # Python 3.10 -- use the declared `toml` dependency

    import toml

    def _loads_toml(text: str) -> dict:
        return toml.loads(text)


@dataclass
class HostConfig:
    """One monitored host. Thresholds are optional and consumed by alerting."""

    name: str
    interval: int = 60
    rtt_warn_ms: Optional[float] = None
    loss_warn_pct: Optional[float] = None


def load_hosts(path, default_interval: int = 60) -> List[HostConfig]:
    """Parse a hosts config file into ``HostConfig`` entries.

    Accepts a TOML ``[[host]]`` array or a JSON ``{"host": [...]}`` list; each
    entry may be a bare hostname string or a table with
    ``name``/``interval``/``rtt_warn_ms``/``loss_warn_pct``. Raises ``ValueError``
    on a malformed file so the CLI can fail loudly (a silent empty config would
    look healthy while monitoring nothing).
    """
    p = Path(path)
    text = p.read_text()
    data = json.loads(text) if p.suffix.lower() == ".json" else _loads_toml(text)

    entries = data.get("host", data.get("hosts"))
    if entries is None:
        raise ValueError("config must define a [[host]] array (or a 'hosts' list)")
    if not isinstance(entries, list):
        raise ValueError("'host'/'hosts' must be a list")

    hosts: List[HostConfig] = []
    for entry in entries:
        if isinstance(entry, str):
            hosts.append(HostConfig(name=entry, interval=default_interval))
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"invalid host entry: {entry!r}")
        name = entry.get("name") or entry.get("host")
        if not name:
            raise ValueError(f"host entry missing 'name': {entry!r}")
        hosts.append(
            HostConfig(
                name=str(name),
                interval=int(entry.get("interval", default_interval)),
                rtt_warn_ms=entry.get("rtt_warn_ms"),
                loss_warn_pct=entry.get("loss_warn_pct"),
            )
        )
    if not hosts:
        raise ValueError("config defines no hosts")
    return hosts
