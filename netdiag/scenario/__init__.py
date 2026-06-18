"""Declarative scenarios (YAML/JSON) for environment-specific diagnosis."""

from .loader import (
    Scenario,
    ScenarioCheck,
    load_scenario,
    merged_config,
    parse_scenario,
)

__all__ = [
    "Scenario",
    "ScenarioCheck",
    "load_scenario",
    "parse_scenario",
    "merged_config",
]
