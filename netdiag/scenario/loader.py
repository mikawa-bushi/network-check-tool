"""Declarative, shareable diagnosis scenarios.

A production-engineering or network team can encode 'how to check this line'
as a YAML/JSON file and hand it out; users then run ``netdiag scenario
line1.yaml`` without knowing probe names or addresses. A scenario may contain:

* ``checks`` — host-side probes with their parameters (gateway, tcp, dns ...),
* ``devices`` — the line's devices (same format as a device list file),
* ``config`` — scenario-wide settings (``count``, ``internet`` ...).

YAML is optional (loaded only if PyYAML is installed); JSON always works via the
standard library, keeping the core dependency-free.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ScenarioCheck:
    probe: str
    label: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    name: str
    description: str
    checks: List[ScenarioCheck]
    config: Dict[str, Any] = field(default_factory=dict)  # scenario-wide defaults
    devices: List[Any] = field(default_factory=list)  # netdiag.inventory.Device

    @property
    def probe_names(self) -> List[str]:
        return [c.probe for c in self.checks]


def _load_raw(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional dependency
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "PyYAML is required for .yaml scenarios "
                "(pip install 'network-check-tool[yaml]'); or use a .json scenario."
            ) from e
        return yaml.safe_load(text)
    return json.loads(text)


# Map a scenario check's free-form fields onto the engine's config keys.
_FIELD_TO_CONFIG = {
    "target": {
        "icmp": "target",
        "tcp": "tcp_targets",
        "traceroute": "trace_target",
        "external": "external_ip",
    },
    "resolver": {"dns": "_resolver"},
    "expect": {"dns": "dns_name"},
    "name": {"dns": "dns_name"},
    "url": {"http": "http_url", "tls": "tls_target"},
}

# Probes that only make sense when the network should reach the internet; a
# scenario that lists one of them turns internet checks on.
_INTERNET_PROBES = {"external", "dns", "dualstack"}


def parse_scenario(raw: Dict[str, Any]) -> Scenario:
    checks: List[ScenarioCheck] = []
    for item in raw.get("checks", []) or []:
        if isinstance(item, str):
            checks.append(ScenarioCheck(probe=item))
            continue
        probe = item["probe"]
        cfg: Dict[str, Any] = {}
        for field_name, value in item.items():
            if field_name in ("probe", "label"):
                continue
            # Translate well-known shorthand to config keys; otherwise pass through.
            mapping = _FIELD_TO_CONFIG.get(field_name, {})
            key = mapping.get(probe, field_name)
            if (
                probe == "tcp"
                and field_name == "target"
                and not isinstance(value, list)
            ):
                value = [value]
            cfg[key] = value
        checks.append(ScenarioCheck(probe=probe, label=item.get("label"), config=cfg))
    devices: List[Any] = []
    if raw.get("devices"):
        from ..inventory import parse_structured

        devices = parse_structured(
            {"devices": raw["devices"], "defaults": raw.get("defaults") or {}}
        )
    config = dict(raw.get("config", {}) or {})
    if any(c.probe in _INTERNET_PROBES for c in checks):
        config.setdefault("internet", True)
    return Scenario(
        name=raw.get("name", "scenario"),
        description=raw.get("description", ""),
        checks=checks,
        config=config,
        devices=devices,
    )


def load_scenario(path: str | Path) -> Scenario:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"scenario not found: {p}")
    return parse_scenario(_load_raw(p))


def merged_config(scenario: Scenario) -> Dict[str, Any]:
    """Flatten scenario-wide + per-check config into one engine config dict."""
    cfg: Dict[str, Any] = dict(scenario.config)
    for check in scenario.checks:
        for k, v in check.config.items():
            if k == "tcp_targets" and "tcp_targets" in cfg:
                cfg["tcp_targets"] = list(cfg["tcp_targets"]) + list(v)
            else:
                cfg[k] = v
    return cfg
