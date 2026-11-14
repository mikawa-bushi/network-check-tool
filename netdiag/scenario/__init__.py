"""Declarative scenarios (YAML/JSON/TOML) for environment-specific diagnosis."""

from .loader import (
    Scenario,
    ScenarioCheck,
    ScenarioPass,
    load_scenario,
    parse_scenario,
)

__all__ = [
    "Scenario",
    "ScenarioCheck",
    "ScenarioPass",
    "load_scenario",
    "parse_scenario",
]
