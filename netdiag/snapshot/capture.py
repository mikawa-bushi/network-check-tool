"""Capture the host's network configuration into one comparable document.

The premise: most 'it suddenly broke' incidents are caused by a config change
(a VPN client rewriting the default route, resolv.conf overwritten, a new
firewall rule). Save a snapshot while things work, diff against it when they
don't, and the culprit change is usually right there.

Each source is a set of stable lines: configuration only, never a counter, a
lease time or a cache state, so two captures of an unchanged PC are equal.
Most sources come from the same OS-neutral model ``netdiag check`` reads
(:mod:`netdiag.netinfo`), so Windows, macOS and Linux are captured alike. A
source that could not be read is ``None``: unknown, never "empty".
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .. import netinfo
from ..core import InputError
from ..core.util import os_name, run
from ..i18n import tr
from ..netinfo.model import KIND_WIFI, HostNetwork, mac_is_null

FORMAT_VERSION = 2
DEFAULT_DIR = Path.home() / ".netdiag" / "snapshots"
SUPPORTED_OS = ("linux", "windows", "macos")

# Sources derived from the netinfo model; compared only between captures made
# by the same collector (a fallback collector sees less).
MODEL_SOURCES = ("interfaces", "addresses", "routes")

Sources = Dict[str, Optional[str]]


def _text(lines: Iterable[str]) -> str:
    return "\n".join(sorted(set(lines)))


def _interface_lines(hn: HostNetwork) -> List[str]:
    out = []
    for i in hn.interfaces:
        line = f"{i.name}: {'up' if i.up else 'down'}"
        if i.admin_up is False:
            line += " (disabled)"
        if i.speed_mbps and i.kind != KIND_WIFI:  # a Wi-Fi rate moves constantly
            line += f" {i.speed_mbps:g} Mb/s"
        if i.duplex:
            line += f" {i.duplex}-duplex"
        if i.mtu:
            line += f" mtu {i.mtu}"
        if i.mac:
            line += f" mac {i.mac}"
        out.append(line)
    return out


def _address_lines(hn: HostNetwork) -> List[str]:
    out = []
    for i in hn.interfaces:
        for a in i.addresses:
            if a.family == 6:
                # IPv6 privacy addresses rotate on their own; the prefix is
                # the configuration.
                out.append(f"{i.name} {a.network}")
                continue
            flags = [f for f in a.flags if f in ("dhcp", "manual", "duplicate")]
            out.append(" ".join([i.name, a.cidr, *flags]))
    return out


def _route_lines(hn: HostNetwork) -> List[str]:
    out = []
    for r in hn.routes:
        line = r.destination
        if r.gateway:
            line += f" via {r.gateway}"
        if r.interface:
            line += f" dev {r.interface}"
        if r.metric is not None:
            line += f" metric {r.metric}"
        out.append(line)
    return out


def _model_sources(hn: HostNetwork) -> Sources:
    sources: Sources = dict.fromkeys(MODEL_SOURCES)
    if hn.interfaces:  # none at all means the collection itself failed
        sources["interfaces"] = _text(_interface_lines(hn))
        sources["addresses"] = _text(_address_lines(hn))
        sources["routes"] = _text(_route_lines(hn))
    # Order matters for resolvers: the first one is asked first.
    sources["dns_resolvers"] = "\n".join(
        f"{n}. {s}" for n, s in enumerate(hn.resolvers, 1)
    )
    # Neighbours are a cache: entries come and go. Only an address whose MAC
    # changed is a change (see the diff), so state and age are left out.
    sources["neighbours"] = _text(
        f"{n.ip} {n.mac}" for n in netinfo.neighbors() if not mac_is_null(n.mac)
    )
    if "firewall" in hn.extras:
        sources["firewall"] = _text(
            f"{p.get('Name')}: enabled={p.get('Enabled')} "
            f"inbound={p.get('DefaultInboundAction')} "
            f"outbound={p.get('DefaultOutboundAction')}"
            for p in hn.extras["firewall"] or []
        )
    return sources


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _config_lines(text: Optional[str]) -> Optional[str]:
    """A config file without comments, blank lines or spacing differences."""
    if text is None:
        return None
    lines = (" ".join(ln.split("#", 1)[0].split()) for ln in text.splitlines())
    return _text(ln for ln in lines if ln)


def _hosts_path(kind: str) -> Path:
    if kind == "windows":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        return Path(root) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def _command(args: List[str]) -> Optional[str]:
    res = run(args, timeout=8)
    return res.stdout if res.found and res.ok else None


def _linux_sources() -> Sources:
    sources: Sources = {"nsswitch": _config_lines(_read(Path("/etc/nsswitch.conf")))}
    routes6 = _command(["ip", "-6", "route", "show"])
    sources["routes6"] = (
        None
        if routes6 is None
        else _text(
            " ".join(re.sub(r"\bexpires \S+", "", ln).split())
            for ln in routes6.splitlines()
            if ln.strip()
        )
    )
    sysctl = _command(["sysctl", "-a", "--pattern", r"net.ipv4.(ip_forward|conf.all)"])
    sources["sysctl_net"] = None if sysctl is None else _text(sysctl.splitlines())
    # Only the listening address: ss pads its columns to the widest one and
    # Recv-Q moves with the accept queue.
    ss = _command(["ss", "-Htln"])
    sources["listening"] = (
        None
        if ss is None
        else _text(
            f"tcp {ln.split()[3]}" for ln in ss.splitlines() if len(ln.split()) > 3
        )
    )
    return sources


def capture() -> Dict:
    """Build a snapshot dict (JSON-serialisable); InputError on an unsupported OS."""
    kind = os_name()
    if kind not in SUPPORTED_OS:
        raise InputError(
            tr(
                f"snapshot はこのOS（{platform.system()}）では使えません"
                "（Windows・macOS・Linux のみ）",
                f"snapshot is not supported on {platform.system()} "
                "(Windows, macOS and Linux only)",
            )
        )
    hn = netinfo.collect()
    sources = _model_sources(hn)
    sources["hosts"] = _config_lines(_read(_hosts_path(kind)))
    if kind == "linux":
        sources.update(_linux_sources())
    return {
        "version": FORMAT_VERSION,
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "collector": hn.source,
        "errors": list(hn.errors),
        "sources": sources,
    }


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _path(name: str, directory: Optional[Path]) -> Path:
    if not name or _BAD_NAME.search(name) or name != name.strip(" ."):
        raise InputError(
            tr(
                f"スナップショット名 '{name}' は使えません。"
                '\\ / : * ? " < > | と先頭・末尾の空白や「.」を含まない名前'
                "（例: 2026-11-13）にしてください",
                f"'{name}' cannot be a snapshot name: leave out "
                '\\ / : * ? " < > | and leading or trailing spaces and dots '
                "(for example 2026-11-13)",
            )
        )
    return (Path(directory) if directory else DEFAULT_DIR) / f"{name}.json"


class NothingCaptured(Exception):
    """The capture read none of this PC's network configuration."""


def save(name: str = "default", directory: Optional[Path] = None) -> Path:
    """Capture now and write it; NothingCaptured instead of writing an empty capture."""
    path = _path(name, directory)
    snap = capture()
    if snap["sources"].get("interfaces") is None:
        reason = "; ".join(snap["errors"]) or tr("原因不明", "unknown reason")
        raise NothingCaptured(
            tr(
                f"このPCのネットワーク設定を読めなかったため、保存しませんでした（{reason}）",
                f"could not read this PC's network configuration, so nothing "
                f"was saved ({reason})",
            )
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        raise InputError(
            tr(
                f"スナップショットを書き込めません: {path}（{e.strerror or e}）",
                f"cannot write the snapshot to {path} ({e.strerror or e})",
            )
        ) from e
    return path


def load(name: str = "default", directory: Optional[Path] = None) -> Optional[Dict]:
    """The saved snapshot, None if there is none; InputError if it cannot be used."""
    path = _path(name, directory)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        raise InputError(
            tr(
                f"スナップショット '{name}' が壊れています（{path}）。"
                f"正常なときに 'netdiag snapshot save {name}' で保存し直してください",
                f"snapshot '{name}' is damaged ({path}); save it again with "
                f"'netdiag snapshot save {name}' while the network works",
            )
        ) from e
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        raise InputError(
            tr(
                f"スナップショット '{name}' は古い netdiag で保存されたため比較できません。"
                f"正常なときに 'netdiag snapshot save {name}' で保存し直してください",
                f"snapshot '{name}' was saved by an older netdiag and cannot be "
                f"compared; save it again with 'netdiag snapshot save {name}' "
                "while the network works",
            )
        )
    return data


def list_snapshots(directory: Optional[Path] = None) -> List[str]:
    directory = Path(directory) if directory else DEFAULT_DIR
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
