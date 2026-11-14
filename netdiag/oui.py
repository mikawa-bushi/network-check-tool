"""MAC address vendor lookup from an IEEE OUI database already on the system.

Knowing that 192.168.3.39 answers with a MAC registered to the PLC maker you
expect (and not to a laptop vendor) is strong evidence on the shop floor.
netdiag does not bundle a vendor table — a stale copy would be wrong exactly
when it matters — but reads one that is present (ieee-data, hwdata, nmap,
Wireshark) or the file named by ``NETDIAG_OUI_FILE``. Without one, lookups
simply return None.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from .netinfo.model import mac_is_null, normalize_mac

CANDIDATES: List[str] = [
    "/usr/share/ieee-data/oui.txt",
    "/var/lib/ieee-data/oui.txt",
    "/usr/share/hwdata/oui.txt",
    "/usr/share/misc/oui.txt",
    "/usr/share/nmap/nmap-mac-prefixes",
    "/usr/local/share/nmap/nmap-mac-prefixes",
    "/opt/homebrew/share/nmap/nmap-mac-prefixes",
    "/usr/share/wireshark/manuf",
    "/opt/homebrew/share/wireshark/manuf",
    r"C:\Program Files (x86)\Nmap\nmap-mac-prefixes",
    r"C:\Program Files\Nmap\nmap-mac-prefixes",
    r"C:\Program Files\Wireshark\manuf",
]

_IEEE_RE = re.compile(
    r"^([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})-([0-9A-Fa-f]{2})\s+\(hex\)\s+(.+)$"
)
_HEX6_RE = re.compile(r"^([0-9A-Fa-f]{6})\s+(.+)$")
_MANUF_RE = re.compile(
    r"^([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\s+(\S+)(?:\s+(.+))?$"
)

_lock = threading.Lock()
_table: Optional[Dict[str, str]] = None


def parse_oui_text(text: str) -> Dict[str, str]:
    """Parse IEEE ``oui.txt``, nmap ``nmap-mac-prefixes`` or Wireshark ``manuf``.

    An indented line is part of an IEEE entry's postal address (its postcode
    can look like a prefix), never an entry of its own.
    """
    table: Dict[str, str] = {}
    for raw in text.splitlines():
        if raw[:1].isspace():
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _IEEE_RE.match(line)
        if m:
            table["".join(m.group(1, 2, 3)).lower()] = m.group(4).strip()
            continue
        m = _MANUF_RE.match(line)
        if m:
            table["".join(m.group(1, 2, 3)).lower()] = (
                m.group(5) or m.group(4)
            ).strip()
            continue
        m = _HEX6_RE.match(line)
        if m and "(base 16)" not in line:
            table[m.group(1).lower()] = m.group(2).strip()
    return table


def database_path() -> Optional[str]:
    env = os.environ.get("NETDIAG_OUI_FILE")
    if env and Path(env).is_file():
        return env
    for c in CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def _load() -> Dict[str, str]:
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                path = database_path()
                table: Dict[str, str] = {}
                if path:
                    try:
                        table = parse_oui_text(
                            Path(path).read_text(encoding="utf-8", errors="replace")
                        )
                    except OSError:
                        table = {}
                _table = table
    return _table


# Blocks the IEEE sells in smaller parts (MA-M, MA-S): the table names the
# registry, not the maker, so the honest answer is "unknown".
_REGISTRY_BLOCK = "ieee registration authority"


def lookup(mac: Optional[str]) -> Optional[str]:
    """Vendor name for a MAC (``aa:bb:cc:..``), or None if unknown/unavailable."""
    normal = normalize_mac(mac.replace(".", "") if mac else None)
    if mac_is_null(normal):  # loopback's 00:00:00:00:00:00 is not Xerox's
        return None
    vendor = _load().get(normal.replace(":", "")[:6])
    if vendor and vendor.lower() == _REGISTRY_BLOCK:
        return None
    return vendor


def reset() -> None:
    """Forget the loaded table (tests / after changing NETDIAG_OUI_FILE)."""
    global _table
    with _lock:
        _table = None
