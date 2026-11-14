"""Diff two configuration snapshots and highlight what changed.

A set diff of stable lines per source, framed for triage: the headline is
*which subsystem changed* (routes, DNS, firewall...), because that maps
directly to a class of failure. A source that one side could not read is
reported as not compared, never as everything added or removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..i18n import Text, render, tr
from .capture import MODEL_SOURCES


@dataclass(frozen=True)
class SourceSpec:
    label: Text
    why: Text
    # Lines are "<key> <value>" from a cache (ARP): entries come and go on
    # their own, so only a key whose value changed is a change.
    keyed: bool = False


SOURCES: Dict[str, SourceSpec] = {
    "routes": SourceSpec(
        Text("ルーティング表（IPv4）", "Routing table (IPv4)"),
        Text(
            "デフォルトルートが変わると全通信が届かなくなることがあります",
            "a changed default route can blackhole all traffic",
        ),
    ),
    "routes6": SourceSpec(
        Text("ルーティング表（IPv6）", "Routing table (IPv6)"),
        Text("IPv6 の経路の変化", "IPv6 routing change"),
    ),
    "interfaces": SourceSpec(
        Text("ネットワークアダプタ", "Network adapters"),
        Text(
            "リンクの切断・速度や二重化の変化・アダプタの交換",
            "a link went up/down, changed speed/duplex, or an adapter was swapped",
        ),
    ),
    "addresses": SourceSpec(
        Text("IPアドレス", "Interface addresses"),
        Text("IPアドレス・サブネットの変化", "an IP/interface change"),
    ),
    "neighbours": SourceSpec(
        Text("ARP（隣接機器）", "ARP/neighbours"),
        Text(
            "同じIPのMACアドレスが変わった（機器の交換・IP重複）",
            "an address now answers with another MAC (device swap or IP conflict)",
        ),
        keyed=True,
    ),
    "dns_resolvers": SourceSpec(
        Text("DNSサーバー", "DNS resolvers"),
        Text(
            "DNSサーバーが変わると名前解決ができなくなることがあります",
            "a changed resolver breaks name resolution",
        ),
    ),
    "firewall": SourceSpec(
        Text("ファイアウォール", "Firewall profiles"),
        Text(
            "受信の既定動作やプロファイルの有効・無効の変化",
            "a profile was switched on/off or its default action changed",
        ),
    ),
    "hosts": SourceSpec(
        Text("hosts ファイル", "hosts file"),
        Text("名前の上書きの変化", "a host override change"),
    ),
    "nsswitch": SourceSpec(
        Text("nsswitch.conf", "nsswitch.conf"),
        Text("名前解決の順序の変化", "name-resolution order change"),
    ),
    "sysctl_net": SourceSpec(
        Text("カーネルのネットワーク設定", "Network sysctls"),
        Text("カーネルのネットワーク設定の変化", "kernel networking parameter change"),
    ),
    "listening": SourceSpec(
        Text("待ち受けポート", "Listening sockets"),
        Text(
            "サービスの起動・停止で待ち受けが変わった",
            "a service started/stopped listening",
        ),
    ),
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
class NotCompared:
    key: str
    label: str
    reason: str


@dataclass
class SnapshotDiff:
    diffs: List[SourceDiff]
    baseline_time: str
    current_time: str
    not_compared: List[NotCompared] = field(default_factory=list)

    @property
    def changed(self) -> List[SourceDiff]:
        return [d for d in self.diffs if d.changed]

    @property
    def any_change(self) -> bool:
        return any(d.changed for d in self.diffs)

    def to_dict(self) -> Dict:
        """JSON-friendly view: the changed sources, framed for triage.

        Note: this is a per-source diff of configuration lines, not an ok->fail
        status transition -- snapshots capture configuration, not health.
        """
        return {
            "baseline_time": self.baseline_time,
            "current_time": self.current_time,
            "any_change": self.any_change,
            "changed": [
                {
                    "key": d.key,
                    "label": d.label,
                    "why": d.why,
                    "added": d.added,
                    "removed": d.removed,
                }
                for d in self.changed
            ],
            "not_compared": [
                {"key": n.key, "label": n.label, "reason": n.reason}
                for n in self.not_compared
            ],
        }


def _lines(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


def _set_diff(old: str, new: str) -> Tuple[List[str], List[str]]:
    a, b = set(_lines(old)), set(_lines(new))
    return sorted(b - a), sorted(a - b)


def _keyed_diff(old: str, new: str) -> Tuple[List[str], List[str]]:
    a = dict(ln.split(" ", 1) for ln in _lines(old) if " " in ln)
    b = dict(ln.split(" ", 1) for ln in _lines(new) if " " in ln)
    moved = sorted(k for k in a.keys() & b.keys() if a[k] != b[k])
    return [f"{k} {b[k]}" for k in moved], [f"{k} {a[k]}" for k in moved]


def _why_not(old: Optional[str], new: Optional[str], collector_changed: bool) -> str:
    if collector_changed:
        return tr(
            "保存時と今とで読み取り方法が違うため",
            "read by a different method than when it was saved",
        )
    if old is None:
        return tr("保存時に読めなかったため", "it could not be read when saved")
    return tr("今は読めないため", "it cannot be read now")


def diff_snapshots(baseline: Dict, current: Dict) -> SnapshotDiff:
    base_src = baseline.get("sources", {})
    cur_src = current.get("sources", {})
    collector_changed = baseline.get("collector") != current.get("collector")
    diffs: List[SourceDiff] = []
    not_compared: List[NotCompared] = []
    for key in sorted(set(base_src) | set(cur_src)):
        spec = SOURCES.get(key)
        label = render(spec.label) if spec else key
        old, new = base_src.get(key), cur_src.get(key)
        if old is None and new is None:
            continue  # not present on this PC at either time
        stale = collector_changed and key in MODEL_SOURCES
        if old is None or new is None or stale:
            not_compared.append(NotCompared(key, label, _why_not(old, new, stale)))
            continue
        compare = _keyed_diff if spec and spec.keyed else _set_diff
        added, removed = compare(old, new)
        diffs.append(
            SourceDiff(
                key=key,
                label=label,
                why=render(spec.why) if spec else "",
                added=added,
                removed=removed,
            )
        )
    return SnapshotDiff(
        diffs=diffs,
        baseline_time=baseline.get("captured_at_iso", "?"),
        current_time=current.get("captured_at_iso", "now"),
        not_compared=not_compared,
    )
