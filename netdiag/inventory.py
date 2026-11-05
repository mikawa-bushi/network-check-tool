"""Device list ("台帳") loading: CSV from Excel, YAML, JSON or TOML.

Production engineers already keep an IP list of the line's PLCs, HMIs and
robot controllers — usually in Excel. ``netdiag check -f devices.csv`` reads
that list as-is:

* CSV saved by Japanese Excel is Shift_JIS (cp932); UTF-8 (with or without
  BOM) and UTF-16 "Unicode text" also work. Comma, tab or semicolon separated.
* Headers may be English or Japanese (``名前``/``IPアドレス``/``ポート``/``備考``
  ...); a header-less list is understood too (the cell that looks like an IP
  address is the host).
* Ports may be numbers (``502``, ``502;4840``) or protocol names
  (``modbus``, ``opcua``, ``enip`` ...).

The same list drives ``netdiag check`` and ``network-checker start-monitoring``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Well-known TCP service ports for FA/OT equipment and common maintenance access.
PORT_PRESETS: Dict[str, List[int]] = {
    "modbus": [502],
    "modbus-tcp": [502],
    "modbustcp": [502],
    "enip": [44818],
    "ethernet-ip": [44818],
    "ethernetip": [44818],
    "opcua": [4840],
    "opc-ua": [4840],
    "s7": [102],
    "iso-tsap": [102],
    "melsoft": [5007],
    "fins": [9600],
    "iec104": [2404],
    "dnp3": [20000],
    "mqtt": [1883],
    "mqtts": [8883],
    "http": [80],
    "https": [443],
    "ssh": [22],
    "telnet": [23],
    "ftp": [21],
    "vnc": [5900],
    "rdp": [3389],
    "smb": [445],
}

_ALIASES: Dict[str, Sequence[str]] = {
    "name": (
        "name",
        "名前",
        "機器名",
        "装置名",
        "設備名",
        "名称",
        "device",
        "機器",
        "label",
    ),
    "host": (
        "host",
        "ip",
        "ipアドレス",
        "ip address",
        "ipaddress",
        "ipaddr",
        "address",
        "アドレス",
        "ホスト",
        "ホスト名",
        "target",
    ),
    "ports": (
        "ports",
        "port",
        "ポート",
        "ポート番号",
        "service",
        "services",
        "サービス",
        "プロトコル",
        "protocol",
    ),
    "group": (
        "group",
        "グループ",
        "ライン",
        "line",
        "工程",
        "area",
        "エリア",
        "場所",
        "location",
    ),
    "note": (
        "note",
        "notes",
        "備考",
        "メモ",
        "comment",
        "コメント",
        "説明",
        "description",
    ),
    "mac": ("mac", "macアドレス", "mac address", "macaddress", "物理アドレス"),
    "rtt_warn_ms": ("rtt_warn_ms", "rtt上限", "遅延上限", "遅延しきい値", "rtt_ms"),
    "loss_warn_pct": ("loss_warn_pct", "ロス上限", "ロス率上限", "loss_pct"),
    "interval": ("interval", "間隔", "監視間隔", "interval_s"),
}
_HEADER_LOOKUP = {
    re.sub(r"[\s_()（）%]", "", alias.lower()): key
    for key, aliases in _ALIASES.items()
    for alias in aliases
}


class InventoryError(ValueError):
    """The device list could not be understood (message says where/why)."""


@dataclass
class Device:
    name: str
    host: str
    ports: List[int] = field(default_factory=list)
    group: str = ""
    note: str = ""
    mac: Optional[str] = None
    rtt_warn_ms: Optional[float] = None
    loss_warn_pct: Optional[float] = None
    interval: Optional[int] = None

    @property
    def label(self) -> str:
        return (
            f"{self.name} ({self.host})"
            if self.name and self.name != self.host
            else self.host
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------- parsing


def parse_ports(value: Any) -> List[int]:
    """``502`` / ``"502;4840"`` / ``"modbus, opcua"`` / ``[502, "enip"]`` -> ports."""
    if value is None or value == "":
        return []
    items: Iterable[Any]
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, (int, float)):
        items = [int(value)]
    else:
        cleaned = re.sub(r"/tcp\b", "", str(value).strip(), flags=re.IGNORECASE)
        items = [p for p in re.split(r"[\s,;/|、・+]+", cleaned) if p]
    out: List[int] = []
    for item in items:
        if isinstance(item, (int, float)):
            ports = [int(item)]
        else:
            token = str(item).strip().lower()
            if token.isdigit():
                ports = [int(token)]
            elif token in PORT_PRESETS:
                ports = PORT_PRESETS[token]
            else:
                raise InventoryError(f"unknown port or protocol: {item!r}")
        for p in ports:
            if not 1 <= p <= 65535:
                raise InventoryError(f"port out of range: {p}")
            if p not in out:
                out.append(p)
    return out


def _looks_like_host(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    try:
        ipaddress.ip_address(v.split("%", 1)[0])
        return True
    except ValueError:
        pass
    return bool(re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}(?::[\w;,+]+)?", v))


def parse_target_spec(spec: str) -> Device:
    """A command-line device: ``192.168.3.39``, ``192.168.3.39:502,4840``,
    ``PLC1=192.168.3.39:modbus`` or ``[fe80::1]:502``."""
    spec = spec.strip()
    if not spec:
        raise InventoryError("empty device specification")
    name = ""
    if "=" in spec:
        name, spec = (s.strip() for s in spec.split("=", 1))
    ports: List[int] = []
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        if rest.startswith(":"):
            ports = parse_ports(rest[1:])
    elif spec.count(":") == 1:
        host, port_s = spec.split(":", 1)
        ports = parse_ports(port_s)
    else:
        host = spec  # bare host, or an IPv6 literal without brackets
    if not host:
        raise InventoryError(f"missing host in {spec!r}")
    return Device(name=name or host, host=host, ports=ports)


def _num(value: Any, kind=float) -> Optional[Any]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return kind(str(value).strip())
    except ValueError as e:
        raise InventoryError(f"not a number: {value!r}") from e


def device_from_mapping(row: Dict[str, Any], where: str = "") -> Device:
    """Build a Device from a dict with canonical or aliased keys."""
    canon: Dict[str, Any] = {}
    for k, v in row.items():
        if k is None:
            continue
        key = _HEADER_LOOKUP.get(re.sub(r"[\s_()（）%]", "", str(k).lower()))
        if key and key not in canon:
            canon[key] = v
    host = _cell(canon.get("host"))
    name = _cell(canon.get("name"))
    if "host" not in canon and name:
        host = name  # network-checker hosts.toml: ``name`` *is* the host
    if not host:
        raise InventoryError(f"{where}missing IP address/host")
    try:
        ports = parse_ports(canon.get("ports"))
        if ":" in host and host.count(":") == 1 and not host.startswith("["):
            host, extra = host.split(":", 1)
            ports = ports + [p for p in parse_ports(extra) if p not in ports]
        return Device(
            name=name or host,
            host=host,
            ports=ports,
            group=_cell(canon.get("group")),
            note=_cell(canon.get("note")),
            mac=(str(canon.get("mac")).strip() or None) if canon.get("mac") else None,
            rtt_warn_ms=_num(canon.get("rtt_warn_ms")),
            loss_warn_pct=_num(canon.get("loss_warn_pct")),
            interval=_num(canon.get("interval"), int),
        )
    except InventoryError as e:
        raise InventoryError(f"{where}{e}") from e


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_csv_text(text: str) -> List[Device]:
    # Keep each data line's original line number for error messages.
    numbered = [
        (n, ln)
        for n, ln in enumerate(text.splitlines(), start=1)
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not numbered:
        raise InventoryError("the device list is empty")
    lines = [ln for _n, ln in numbered]
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in sample else ","
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    line_no = [n for n, _ln in numbered]
    header = [c.strip() for c in rows[0]]
    mapped = [
        _HEADER_LOOKUP.get(re.sub(r"[\s_()（）%]", "", h.lower())) for h in header
    ]
    devices: List[Device] = []
    if "host" in mapped or ("name" in mapped and len(header) == 1):
        for i, row in enumerate(rows[1:], start=1):
            if not any(c.strip() for c in row):
                continue
            n = line_no[i] if i < len(line_no) else i + 1
            devices.append(
                device_from_mapping(dict(zip(header, row)), where=f"line {n}: ")
            )
    else:
        for i, row in enumerate(rows):
            n = line_no[i] if i < len(line_no) else i + 1
            cells = [c.strip() for c in row]
            if not any(cells):
                continue
            host_i = next((j for j, c in enumerate(cells) if _looks_like_host(c)), None)
            if host_i is None:
                raise InventoryError(
                    f"line {n}: no IP address found (add a header row such as 'name,host,ports')"
                )
            rest = [c for j, c in enumerate(cells) if j != host_i and c]
            ports_cell = next((c for c in rest if re.fullmatch(r"[\d\s,;/]+", c)), "")
            name = next((c for c in rest if c != ports_cell), "")
            devices.append(
                device_from_mapping(
                    {"host": cells[host_i], "name": name, "ports": ports_cell},
                    where=f"line {n}: ",
                )
            )
    if not devices:
        raise InventoryError("the device list has a header but no devices")
    return devices


def _load_toml(text: str) -> Any:
    try:
        import tomllib  # Python 3.11+

        return tomllib.loads(text)
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import toml

        return toml.loads(text)


def parse_structured(data: Any) -> List[Device]:
    """Devices from parsed YAML/JSON/TOML: a list, or a mapping with
    ``devices``/``hosts``/``host`` (+ optional ``defaults``)."""
    defaults: Dict[str, Any] = {}
    items: Any = data
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        for key in ("devices", "hosts", "host", "機器"):
            if key in data:
                items = data[key]
                break
        else:
            raise InventoryError("expected a 'devices' list")
    if not isinstance(items, list):
        raise InventoryError("'devices' must be a list")
    devices: List[Device] = []
    for n, item in enumerate(items, start=1):
        where = f"device #{n}: "
        if isinstance(item, str):
            dev = parse_target_spec(item)
        elif isinstance(item, dict):
            dev = device_from_mapping({**defaults, **item}, where=where)
        else:
            raise InventoryError(f"{where}unsupported entry {item!r}")
        if not dev.ports and defaults.get("ports"):
            dev.ports = parse_ports(defaults["ports"])
        devices.append(dev)
    if not devices:
        raise InventoryError("the device list is empty")
    return devices


def load_devices(path: str | Path) -> List[Device]:
    """Load a device list; raises :class:`InventoryError` with a clear reason."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise InventoryError(f"cannot read {p}: {e}") from e
    text = _decode(raw)
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as e:
                raise InventoryError(
                    "YAML needs PyYAML (pip install 'network-check-tool[yaml]'); "
                    "CSV/JSON/TOML work without it"
                ) from e
            return parse_structured(yaml.safe_load(text))
        if suffix == ".json":
            return parse_structured(json.loads(text))
        if suffix == ".toml":
            return parse_structured(_load_toml(text))
    except InventoryError:
        raise
    except Exception as e:  # syntax errors from the respective parsers
        raise InventoryError(f"{p.name}: {e}") from e
    return parse_csv_text(text)


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def excel_safe(value: Any) -> Any:
    """Neutralise text Excel would run as a formula (CSV injection).

    A leading apostrophe makes Excel show the cell as text; :func:`_cell`
    strips it again when such a file is read back as a device list.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _cell(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("'") and text[1:2] and text[1:].startswith(_FORMULA_PREFIXES):
        return text[1:]
    return text


def devices_to_csv(devices: Iterable[Device]) -> str:
    """CSV text (Japanese headers) that Excel opens correctly when written as
    UTF-8 with BOM — the format ``netdiag scan --save`` produces."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["名前", "IPアドレス", "ポート", "グループ", "MACアドレス", "備考"])
    for d in devices:
        row = [
            d.name,
            d.host,
            ";".join(map(str, d.ports)),
            d.group,
            d.mac or "",
            d.note,
        ]
        w.writerow([excel_safe(c) for c in row])
    return buf.getvalue()
