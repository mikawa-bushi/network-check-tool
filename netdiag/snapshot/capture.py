"""Capture the host's network configuration into one comparable document.

The premise: most 'it suddenly broke' incidents are caused by a config change
(a VPN client rewriting the default route, resolv.conf overwritten, a new
firewall rule). Save a snapshot while things work, diff against it when they
don't, and the culprit change is usually right there.
"""

from __future__ import annotations

import json
import platform
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..core.util import is_linux, run

DEFAULT_DIR = Path.home() / ".netdiag" / "snapshots"


# Each source is (key, command). Commands that aren't present are recorded as
# null so a diff doesn't spuriously flag a missing tool as a change.
_LINUX_SOURCES = [
    ("routes", ["ip", "route", "show"]),
    ("routes6", ["ip", "-6", "route", "show"]),
    ("addresses", ["ip", "-o", "addr", "show"]),
    ("links", ["ip", "-o", "link", "show"]),
    ("neighbours", ["ip", "neigh", "show"]),
    ("dns_resolvers", ["cat", "/etc/resolv.conf"]),
    ("hosts", ["cat", "/etc/hosts"]),
    ("nsswitch", ["cat", "/etc/nsswitch.conf"]),
    ("sysctl_net", ["sysctl", "-a", "--pattern", "net.ipv4.(ip_forward|conf.all)"]),
    ("listening", ["ss", "-tlnp"]),
]


def _capture_sources() -> Dict[str, Optional[str]]:
    data: Dict[str, Optional[str]] = {}
    sources = _LINUX_SOURCES if is_linux() else []
    for key, cmd in sources:
        res = run(cmd, timeout=8)
        data[key] = res.stdout.strip() if res.found and res.ok else None
    return data


def capture() -> Dict:
    """Build a snapshot dict (JSON-serialisable)."""
    return {
        "version": 1,
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "sources": _capture_sources(),
    }


def save(name: str = "default", directory: Optional[Path] = None) -> Path:
    directory = Path(directory) if directory else DEFAULT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps(capture(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def load(name: str = "default", directory: Optional[Path] = None) -> Optional[Dict]:
    directory = Path(directory) if directory else DEFAULT_DIR
    path = directory / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def list_snapshots(directory: Optional[Path] = None) -> List[str]:
    directory = Path(directory) if directory else DEFAULT_DIR
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
