"""Diff two configuration snapshots and highlight what changed.

A line-level diff per source, but framed for triage: the headline is *which
subsystem changed* (routes, DNS, firewall...), because that maps directly to a
class of failure.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Human labels + why a change here matters, for the report.
SOURCE_LABELS = {
    "routes": (
        "Routing table (IPv4)",
        "a changed default route can blackhole all traffic",
    ),
    "routes6": ("Routing table (IPv6)", "IPv6 routing change"),
    "addresses": ("Interface addresses", "an IP/interface change"),
    "links": ("Link states", "an interface going up/down"),
    "neighbours": ("ARP/neighbours", "L2 neighbour changes"),
    "dns_resolvers": (
        "DNS resolvers (resolv.conf)",
        "a changed resolver breaks name resolution",
    ),
    "hosts": ("/etc/hosts", "a host override change"),
    "nsswitch": ("nsswitch.conf", "name-resolution order change"),
    "sysctl_net": ("Network sysctls", "kernel networking parameter change"),
    "listening": ("Listening sockets", "a service started/stopped listening"),
}


@dataclass
class SourceDiff:
    key: str
    label: str
    why: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


@dataclass
class SnapshotDiff:
    diffs: List[SourceDiff]
    baseline_time: str
    current_time: str

    @property
    def changed(self) -> List[SourceDiff]:
        return [d for d in self.diffs if d.changed]

    @property
    def any_change(self) -> bool:
        return any(d.changed for d in self.diffs)


def _line_diff(old: Optional[str], new: Optional[str]) -> (List[str], List[str]):
    old_lines = (old or "").splitlines()
    new_lines = (new or "").splitlines()
    added, removed = [], []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("-"):
            removed.append(line[1:])
    return added, removed


def diff_snapshots(baseline: Dict, current: Dict) -> SnapshotDiff:
    base_src = baseline.get("sources", {})
    cur_src = current.get("sources", {})
    keys = sorted(set(base_src) | set(cur_src))
    diffs: List[SourceDiff] = []
    for key in keys:
        label, why = SOURCE_LABELS.get(key, (key, ""))
        added, removed = _line_diff(base_src.get(key), cur_src.get(key))
        diffs.append(
            SourceDiff(key=key, label=label, why=why, added=added, removed=removed)
        )
    return SnapshotDiff(
        diffs=diffs,
        baseline_time=baseline.get("captured_at_iso", "?"),
        current_time=current.get("captured_at_iso", "now"),
    )
