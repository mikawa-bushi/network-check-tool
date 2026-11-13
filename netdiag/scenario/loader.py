"""Declarative, shareable diagnosis scenarios.

A production-engineering or network team can encode 'how to check this line'
as a YAML/JSON/TOML file and hand it out; users then run ``netdiag scenario
line1.yaml`` without knowing probe names or addresses. A scenario may contain:

* ``checks`` — host-side probes with their parameters (gateway, tcp, dns ...),
* ``devices`` — the line's devices (same format as a device list file),
* ``config`` — scenario-wide settings (``count``, ``internet`` ...).

YAML is optional (loaded only if PyYAML is installed); JSON and TOML always
work via the standard library, keeping the core dependency-free.

The loader is the input boundary: anything it cannot use raises
:class:`~netdiag.core.InputError` naming the file and the problem.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomllib

from ..core import InputError
from ..i18n import tr


@dataclass
class ScenarioCheck:
    probe: str
    label: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioPass:
    """Checks that can share one engine run: distinct probes, no conflicting settings."""

    checks: List[ScenarioCheck] = field(default_factory=list)

    @property
    def probe_names(self) -> List[str]:
        return [c.probe for c in self.checks]

    @property
    def config(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        for c in self.checks:
            merged.update(c.config)
        return merged

    def accepts(self, check: ScenarioCheck) -> bool:
        if check.probe in self.probe_names:
            return False
        mine = self.config
        return all(mine.get(k, v) == v for k, v in check.config.items())

    def apply_labels(self, results) -> None:
        """Show each check's ``label`` as the title of its result."""
        labels = {c.probe: c.label for c in self.checks if c.label}
        for r in results:
            if r.name in labels:
                r.title = labels[r.name]


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

    def passes(self) -> List[ScenarioPass]:
        """Group the checks into engine runs so each one runs with its own settings.

        The engine takes one config per run, so a probe listed twice, or two
        checks that set the same key differently, go into separate passes.
        """
        out: List[ScenarioPass] = []
        for check in self.checks:
            for p in out:
                if p.accepts(check):
                    p.checks.append(check)
                    break
            else:
                out.append(ScenarioPass([check]))
        return out


_TOP_KEYS = ("name", "description", "checks", "devices", "defaults", "config")

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


def _bad(source: str, ja: str, en: str) -> InputError:
    return InputError(tr(f"シナリオ {source}: {ja}", f"scenario {source}: {en}"))


def _kind(value: Any) -> str:
    return type(value).__name__


def _parse_check(n: int, item: Any, source: str) -> ScenarioCheck:
    if isinstance(item, str) and item.strip():
        return ScenarioCheck(probe=item.strip())
    if not isinstance(item, dict):
        raise _bad(
            source,
            f"チェック #{n}: チェック名か、probe を持つ項目を書いてください（{item!r}）",
            f"check #{n}: expected a check name or a mapping with 'probe', got {item!r}",
        )
    probe = item.get("probe")
    if not isinstance(probe, str) or not probe.strip():
        raise _bad(
            source,
            f"チェック #{n}: probe（チェック名）がありません",
            f"check #{n}: missing 'probe' (the check name)",
        )
    probe = probe.strip()
    cfg: Dict[str, Any] = {}
    for field_name, value in item.items():
        if field_name in ("probe", "label"):
            continue
        # Translate well-known shorthand to config keys; otherwise pass through.
        key = _FIELD_TO_CONFIG.get(field_name, {}).get(probe, field_name)
        if probe == "tcp" and field_name == "target" and not isinstance(value, list):
            value = [value]
        cfg[key] = value
    label = item.get("label")
    return ScenarioCheck(
        probe=probe, label=None if label is None else str(label), config=cfg
    )


def parse_scenario(raw: Any, source: str = "scenario") -> Scenario:
    """Validate parsed file content into a :class:`Scenario` (InputError if unusable)."""
    if raw is None:
        raise _bad(source, "ファイルが空です", "the file is empty")
    if not isinstance(raw, dict):
        raise _bad(
            source,
            f"先頭は checks や devices を持つ項目の集まりにしてください（今は {_kind(raw)}）",
            f"expected a mapping with checks/devices at the top, got a {_kind(raw)}",
        )
    unknown = [k for k in raw if k not in _TOP_KEYS]
    if unknown:
        names = ", ".join(map(str, unknown))
        valid = ", ".join(_TOP_KEYS)
        raise _bad(
            source,
            f"不明な項目: {names}（使える項目: {valid}）",
            f"unknown key: {names} (valid keys: {valid})",
        )
    checks_raw = raw.get("checks") or []
    if not isinstance(checks_raw, list):
        raise _bad(
            source,
            f"'checks' はリストで書いてください（「- interface」のように1行に1つ。今は {_kind(checks_raw)}）",
            f"'checks' must be a list (one '- name' per line), got a {_kind(checks_raw)}",
        )
    checks = [_parse_check(n, item, source) for n, item in enumerate(checks_raw, 1)]
    config = raw.get("config") or {}
    if not isinstance(config, dict):
        raise _bad(
            source,
            f"'config' は「count: 50」の形で書いてください（今は {_kind(config)}）",
            f"'config' must be a mapping (key: value), got a {_kind(config)}",
        )
    config = dict(config)
    devices: List[Any] = []
    if raw.get("devices"):
        from ..inventory import InventoryError, parse_structured

        try:
            devices = parse_structured(
                {"devices": raw["devices"], "defaults": raw.get("defaults") or {}}
            )
        except InventoryError as e:
            raise _bad(source, f"devices: {e}", f"devices: {e}") from e
    if any(c.probe in _INTERNET_PROBES for c in checks):
        config.setdefault("internet", True)
    scenario = Scenario(
        name=str(raw.get("name") or "scenario"),
        description=str(raw.get("description") or ""),
        checks=checks,
        config=config,
        devices=devices,
    )
    _check_names(scenario, source)
    return scenario


def _check_names(scenario: Scenario, source: str) -> None:
    from .. import probes  # noqa: F401  (registers the built-in probes)
    from ..diagnosis import Engine

    for p in scenario.passes():
        try:
            Engine(config={**scenario.config, **p.config}).select(only=p.probe_names)
        except InputError as e:
            raise InputError(
                tr(f"シナリオ {source}: ", f"scenario {source}: ") + str(e)
            )


def _yaml_loader():
    """PyYAML's safe loader, except that ``0012`` stays the string ``"0012"``.

    YAML 1.1 reads a leading zero as octal (``0012`` -> 10) and ``1:30`` as
    base 60; device names and ports written that way must keep their digits.
    """
    import yaml

    int_tag = "tag:yaml.org,2002:int"

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag != int_tag]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver(
        int_tag,
        re.compile(r"^(?:[-+]?(?:0|[1-9][0-9_]*)|0x[0-9a-fA-F_]+)$"),
        list("-+0123456789"),
    )
    return Loader


def _load_yaml(text: str, source: str) -> Any:
    try:
        import yaml
    except ImportError as e:
        raise _bad(
            source,
            "YAML を読むには PyYAML が必要です（pip install 'network-check-tool[yaml]'）。"
            ".json か .toml で保存すれば不要です",
            "reading YAML needs PyYAML (pip install 'network-check-tool[yaml]'); "
            "a .json or .toml scenario works without it",
        ) from e
    try:
        return yaml.load(text, Loader=_yaml_loader())
    except yaml.YAMLError as e:
        raise _bad(source, f"YAML の書式エラー: {e}", f"YAML syntax error: {e}") from e


def _load_raw(path: Path, source: str) -> Any:
    from ..inventory import _decode

    try:
        text = _decode(path.read_bytes())
    except OSError as e:
        raise _bad(
            source,
            f"読めません（{e.strerror or e}）",
            f"cannot read it ({e.strerror or e})",
        ) from e
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return _load_yaml(text, source)
    if not text.strip():
        return None
    if suffix == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise _bad(
                source, f"JSON の書式エラー: {e}", f"JSON syntax error: {e}"
            ) from e
    if suffix == ".toml":
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise _bad(
                source, f"TOML の書式エラー: {e}", f"TOML syntax error: {e}"
            ) from e
    raise _bad(
        source,
        f"拡張子 '{suffix or '(なし)'}' は読めません。.yaml, .yml, .json or .toml で保存してください",
        f"unsupported file type '{suffix or '(none)'}': use .yaml, .yml, .json or .toml",
    )


def load_scenario(path: str | Path) -> Scenario:
    p = Path(path)
    return parse_scenario(_load_raw(p, str(p)), str(p))
