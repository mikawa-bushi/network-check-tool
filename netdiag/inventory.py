"""Device list ("台帳") loading: CSV from Excel, YAML, JSON or TOML.

Production engineers already keep an IP list of the line's PLCs, HMIs and
robot controllers — usually in Excel. ``netdiag check -f devices.csv`` reads
that list as-is:

* CSV saved by Japanese Excel is Shift_JIS (cp932); UTF-8 (with or without
  BOM) and UTF-16 "Unicode text" also work. Comma, tab or semicolon separated.
* Headers may be English or Japanese (``名前``/``IPアドレス``/``ポート``/``備考``
  ...), in full-width letters too, below a title row; a header-less list is
  understood too (the cell that looks like an IP address is the host, and the
  cell after it may hold the ports).
* Ports may be numbers (``502``, ``502;4840``) or protocol names
  (``modbus``, ``opcua``, ``enip`` ...).

Every value is checked and normalised here, at the boundary: a zero-padded
IPv4 address (``192.168.001.010``) is read as decimal, a MAC in any common
form becomes ``aa:bb:cc:dd:ee:ff``, and anything netdiag cannot read is an
:class:`InventoryError` that names the line. Nothing is dropped silently.

The same list drives ``netdiag check -f`` and ``netdiag soak -f``.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from .core.errors import InputError
from .i18n import tr
from .netinfo.model import normalize_mac


class Service(NamedTuple):
    name: str
    aliases: Tuple[str, ...]


SERVICES: Dict[int, Service] = {
    21: Service("FTP", ("ftp",)),
    22: Service("SSH", ("ssh",)),
    23: Service("Telnet", ("telnet",)),
    80: Service("HTTP", ("http",)),
    102: Service("S7/ISO-TSAP", ("s7", "iso-tsap")),
    443: Service("HTTPS", ("https",)),
    445: Service("SMB", ("smb",)),
    502: Service("Modbus/TCP", ("modbus", "modbus-tcp", "modbustcp")),
    1883: Service("MQTT", ("mqtt",)),
    2404: Service("IEC104", ("iec104",)),
    3389: Service("RDP", ("rdp",)),
    4840: Service("OPC UA", ("opcua", "opc-ua")),
    5007: Service("MELSOFT", ("melsoft",)),
    5900: Service("VNC", ("vnc",)),
    8883: Service("MQTTS", ("mqtts",)),
    9600: Service("FINS/TCP", ("fins",)),
    20000: Service("DNP3", ("dnp3",)),
    44818: Service("EtherNet/IP", ("enip", "ethernet-ip", "ethernetip")),
}
SERVICE_NAMES = {port: s.name for port, s in SERVICES.items()}
_PORT_WORDS = {alias: port for port, s in SERVICES.items() for alias in s.aliases}

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
        "ipaddr",
        "ipv4",
        "ipv4アドレス",
        "ipv4 address",
        "address",
        "アドレス",
        "ホスト",
        "ホスト名",
        "target",
    ),
    "ports": (
        "ports",
        "port",
        "port no",
        "port number",
        "tcp port",
        "tcp ports",
        "port (tcp)",
        "ポート",
        "ポート番号",
        "ポートno",
        "使用ポート",
        "tcpポート",
        "ポート(tcp)",
        "通信ポート",
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
    "mac": ("mac", "macアドレス", "mac address", "物理アドレス"),
    "rtt_warn_ms": ("rtt_warn_ms", "rtt上限", "遅延上限", "遅延しきい値", "rtt_ms"),
    "loss_warn_pct": ("loss_warn_pct", "ロス上限", "ロス率上限", "loss_pct"),
}

# Full-width ASCII (ＩＰ, ５０２, １９２．１６８…) as typed with a Japanese IME.
_FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}


def _ascii(text: str) -> str:
    return text.translate(_FULLWIDTH)


def _header_key(header: str) -> str:
    text = unicodedata.normalize("NFKC", header).lower()
    return re.sub(r"[\s_()%.\-#]", "", text)


_HEADER_LOOKUP = {
    _header_key(alias): key for key, aliases in _ALIASES.items() for alias in aliases
}


class InventoryError(InputError, ValueError):
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


def _port(item: Any) -> int:
    port: Optional[int] = None
    if isinstance(item, float) and item.is_integer():
        item = int(item)
    if isinstance(item, int) and not isinstance(item, bool):
        port = item
    elif isinstance(item, str):
        token = _ascii(item).strip().lower()
        if re.fullmatch(r"[0-9]+", token):
            port = int(token)
        else:
            port = _PORT_WORDS.get(token)
    if port is None:
        raise InventoryError(
            tr(
                f"ポート番号またはプロトコル名として読めません: {item!r}",
                f"unknown port or protocol: {item!r}",
            )
        )
    if not 1 <= port <= 65535:
        raise InventoryError(
            tr(
                f"ポート番号は 1〜65535 です: {port}",
                f"port out of range (1-65535): {port}",
            )
        )
    return port


def parse_ports(value: Any) -> List[int]:
    """``502`` / ``"502;4840"`` / ``"modbus, opcua"`` / ``[502, "enip"]`` -> ports."""
    if value is None or value == "":
        return []
    items: Iterable[Any]
    if isinstance(value, (list, tuple)):
        items = [
            p for v in value for p in (parse_ports(v) if isinstance(v, str) else [v])
        ]
    elif isinstance(value, str):
        cleaned = re.sub(r"/tcp\b", "", _ascii(value).strip(), flags=re.IGNORECASE)
        items = [p for p in re.split(r"[\s,;/|、・+]+", cleaned) if p]
    else:
        items = [value]
    out: List[int] = []
    for item in items:
        p = _port(item)
        if p not in out:
            out.append(p)
    return out


def normalize_host(value: str) -> str:
    """The host as netdiag uses it: an IP address in canonical form, or a name.

    A dotted IPv4 address with zero-padded parts (``192.168.001.010``, the way
    Excel ledgers often hold them) means decimal ``192.168.1.10``. Left alone,
    the resolver would read each padded part as octal and check another device.
    A value made of digits and dots that is not four parts of 0-255 is refused.
    """
    host = _ascii(value).strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1].strip()
    if re.fullmatch(r"[0-9.]+", host):
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts):
            return ".".join(str(int(p)) for p in parts)
        raise InventoryError(
            tr(
                f"IPv4アドレスの形式が正しくありません: {value}",
                f"not a valid IPv4 address: {value}",
            )
        )
    if not host or re.search(r"\s", host):
        raise InventoryError(
            tr(
                f"IPアドレスまたはホスト名として読めません: {value!r}",
                f"not an IP address or host name: {value!r}",
            )
        )
    literal, zone = host.partition("%")[::2]
    try:
        addr = ipaddress.ip_address(literal)
    except ValueError:
        return host
    return addr.compressed + (f"%{zone}" if zone else "")


def _split_host_port(text: str) -> Tuple[str, str]:
    """``[v6]:502`` / ``host:502`` / ``host`` / bare IPv6 -> (host, ports text)."""
    text = _ascii(text).strip()
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, rest[1:] if rest.startswith(":") else rest
    if text.count(":") == 1:
        host, _, ports = text.partition(":")
        return host, ports
    return text, ""


def parse_mac(value: Any) -> Optional[str]:
    """A MAC address in any common form -> ``aa:bb:cc:dd:ee:ff`` (None if empty)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InventoryError(
            tr(
                f"MACアドレスが読めません: {value!r}（YAMLでは引用符で囲んでください）",
                f"cannot read the MAC address {value!r} (quote it in YAML)",
            )
        )
    text = _ascii(_cell(value)).strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9A-Fa-f]{4}(\.[0-9A-Fa-f]{4}){2}", text):
        text = text.replace(".", "")
    mac = normalize_mac(text)
    if mac is None:
        raise InventoryError(
            tr(
                f"MACアドレスの形式が正しくありません: {value}",
                f"not a MAC address: {value}",
            )
        )
    return mac


def _spec_fields(spec: str) -> Dict[str, Any]:
    name = ""
    if "=" in spec:
        name, spec = (s.strip() for s in spec.split("=", 1))
    host, ports = _split_host_port(spec)
    if not host.strip():
        raise InventoryError(
            tr(f"ホストがありません: {spec!r}", f"missing host in {spec!r}")
        )
    return {"name": name, "host": normalize_host(host), "ports": parse_ports(ports)}


def parse_target_spec(spec: str) -> Device:
    """A command-line device: ``192.168.3.39``, ``192.168.3.39:502,4840``,
    ``PLC1=192.168.3.39:modbus`` or ``[fe80::1]:502``."""
    spec = spec.strip()
    if not spec:
        raise InventoryError(tr("機器の指定が空です", "empty device specification"))
    fields = _spec_fields(spec)
    return Device(
        name=fields["name"] or fields["host"],
        host=fields["host"],
        ports=fields["ports"],
    )


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _ascii(str(value)).strip()
    if not text:
        return None
    try:
        return float(re.sub(r"\s*(%|ms)$", "", text, flags=re.IGNORECASE))
    except ValueError as e:
        raise InventoryError(
            tr(f"数値として読めません: {value!r}", f"not a number: {value!r}")
        ) from e


def _canonical(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map aliased keys (``ポート``, ``IP Address`` ...) to canonical ones."""
    canon: Dict[str, Any] = {}
    for k, v in row.items():
        if k is None:
            continue
        key = _HEADER_LOOKUP.get(_header_key(str(k)))
        if key and key not in canon:
            canon[key] = v
    return canon


def device_from_mapping(
    row: Dict[str, Any],
    where: str = "",
    defaults: Optional[Dict[str, Any]] = None,
    name_is_host: bool = False,
) -> Device:
    """Build a Device from a dict with canonical or aliased keys.

    Keys in *row* win over *defaults*, whatever alias either uses. With
    *name_is_host* (``hosts.toml``: ``[[host]] name = ...``), an entry without
    a host uses its name as the host.
    """
    canon = _canonical(defaults or {})
    canon.update(_canonical(row))
    name = _cell(canon.get("name"))
    raw_host = _cell(canon.get("host"))
    if not raw_host and name_is_host and "host" not in canon:
        raw_host = name
    try:
        if not raw_host:
            raise InventoryError(
                tr("IPアドレス/ホストがありません", "missing IP address/host")
            )
        host_text, extra = _split_host_port(raw_host)
        host = normalize_host(host_text)
        ports = parse_ports(canon.get("ports"))
        ports += [p for p in parse_ports(extra) if p not in ports]
        return Device(
            name=name or host,
            host=host,
            ports=ports,
            group=_cell(canon.get("group")),
            note=_cell(canon.get("note")),
            mac=parse_mac(canon.get("mac")),
            rtt_warn_ms=_num(canon.get("rtt_warn_ms")),
            loss_warn_pct=_num(canon.get("loss_warn_pct")),
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


def _looks_like_host(value: str) -> bool:
    v = _ascii(value).strip()
    if not v:
        return False
    host, _ports = _split_host_port(v)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        pass
    return bool(re.fullmatch(r"(?:\d+\.){3}\d+", host))


def _column_keys(cells: List[str]) -> List[Optional[str]]:
    return [_HEADER_LOOKUP.get(_header_key(c)) for c in cells if c]


def _is_comment(cells: List[str]) -> bool:
    """``# text``, ``#`` alone, or a commented-out device row.

    A name such as ``#1プレス`` on a device row is kept: only a ``#`` followed
    by a space, another ``#``, an address, or nothing else on the row counts.
    A header row whose first cell is ``#`` alone (a row-number column) is kept
    too.
    """
    first = cells[0] if cells else ""
    if not first.startswith("#"):
        return False
    if first == "#" and "host" in _column_keys(cells):
        return False
    rest = first[1:]
    filled = [c for c in cells if c]
    return not rest or rest[0] in " \t#　" or _looks_like_host(rest) or len(filled) == 1


def _records(text: str) -> List[Tuple[int, List[str]]]:
    """Non-blank, non-comment CSV records with the line each one starts on."""
    sample = "\n".join(
        ln for ln in text.splitlines()[:40] if ln.strip() and not ln.startswith("#")
    )
    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in sample else ","
    reader = csv.reader(io.StringIO(text, newline=None), delimiter=delimiter)
    out: List[Tuple[int, List[str]]] = []
    start = 1
    for row in reader:
        line, start = start, reader.line_num + 1
        cells = [c.strip() for c in row]
        if any(cells) and not _is_comment(cells):
            out.append((line, cells))
    return out


def _header_row(records: List[Tuple[int, List[str]]]) -> Optional[int]:
    """Index of the header row: the first row naming a host column.

    Title rows above it are allowed; a row holding an address before any
    header means the list has no header at all.
    """
    for i, (_n, cells) in enumerate(records):
        keys = _column_keys(cells)
        if "host" in keys or keys == ["name"]:
            return i
        if any(_looks_like_host(c) for c in cells):
            return None
    return None


def _where_line(n: int) -> str:
    return tr(f"{n}行目: ", f"line {n}: ")


# The word "port" in a header such as スイッチポート or "switch port".
# サポート (support) and レポート (report) are other words.
_PORT_WORD = re.compile(r"(?<![サレ])ポート|(?<![a-z])ports?(?![a-z])")


def _note_unread_port_columns(header: List[str]) -> None:
    """Say on stderr which port-like columns are not read as TCP ports."""
    for h in header:
        if _HEADER_LOOKUP.get(_header_key(h)) is None and _PORT_WORD.search(
            unicodedata.normalize("NFKC", h).lower()
        ):
            print(
                tr(
                    f"注意: 列「{h}」はTCPポートとして読みません"
                    "（TCPポートは見出しが「ポート」の列から読みます）",
                    f"Note: column '{h}' is not read as TCP ports "
                    "(netdiag reads them from a column headed 'ports')",
                ),
                file=sys.stderr,
            )


def _headerless_device(n: int, cells: List[str]) -> Device:
    """A row without a header: the address cell is the host, the first cell
    after it that reads as ports holds the ports, and the first other text
    cell is the name. A number before the address is a row number."""
    host_i = next((j for j, c in enumerate(cells) if _looks_like_host(c)), None)
    if host_i is None:
        raise InventoryError(
            _where_line(n)
            + tr(
                "IPアドレスが見つかりません（1行目に「名前,IPアドレス,ポート」のような見出しを付けてください）",
                "no IP address found (add a header row such as 'name,host,ports')",
            )
        )
    ports_i = None
    for j in range(host_i + 1, len(cells)):
        try:
            if parse_ports(cells[j]):
                ports_i = j
                break
        except InventoryError:
            continue
    others = [
        c
        for j, c in enumerate(cells)
        if j not in (host_i, ports_i) and c and not c.isdigit()
    ]
    return device_from_mapping(
        {
            "host": cells[host_i],
            "name": others[0] if others else "",
            "ports": cells[ports_i] if ports_i is not None else "",
        },
        where=_where_line(n),
    )


def parse_csv_text(text: str) -> List[Device]:
    records = _records(text)
    if not records:
        raise InventoryError(tr("機器の一覧が空です", "the device list is empty"))
    h = _header_row(records)
    devices: List[Device] = []
    if h is None:
        devices = [_headerless_device(n, cells) for n, cells in records]
    else:
        header = records[h][1]
        _note_unread_port_columns(header)
        names_only = "host" not in _canonical(dict.fromkeys(header, ""))
        for n, cells in records[h + 1 :]:
            devices.append(
                device_from_mapping(
                    dict(zip(header, cells)),
                    where=_where_line(n),
                    name_is_host=names_only,
                )
            )
    if not devices:
        raise InventoryError(
            tr(
                "見出しはありますが機器がありません",
                "the device list has a header but no devices",
            )
        )
    return devices


def _load_toml(text: str) -> Any:
    import tomllib

    return tomllib.loads(text)


_YAML_TYPED_TAGS = {
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:timestamp",
}


def load_yaml_text(text: str) -> Any:
    """YAML with every plain scalar kept as written.

    YAML 1.1 would read ``ports: 0502`` as octal 322 and an unquoted MAC
    ``12:34:56:01:23:45`` as a base-60 number; the ledger parser reads the
    text instead.
    """
    try:
        import yaml
    except ImportError as e:
        raise InventoryError(
            tr(
                "YAMLの台帳には PyYAML が必要です（pip install 'network-check-tool[yaml]'）。"
                "CSV/JSON/TOML はそのまま使えます",
                "YAML needs PyYAML (pip install 'network-check-tool[yaml]'); "
                "CSV/JSON/TOML work without it",
            )
        ) from e

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.yaml_implicit_resolvers = {
        first: [(tag, rx) for tag, rx in resolvers if tag not in _YAML_TYPED_TAGS]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    return yaml.load(text, Loader=_Loader)


def parse_structured(data: Any) -> List[Device]:
    """Devices from parsed YAML/JSON/TOML: a list, or a mapping with
    ``devices``/``hosts``/``host`` (+ optional ``defaults``)."""
    defaults: Dict[str, Any] = {}
    items: Any = data
    name_is_host = False
    if isinstance(data, dict):
        defaults = data.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise InventoryError(
                tr(
                    "'defaults' はキーと値の組で書いてください",
                    "'defaults' must be a mapping",
                )
            )
        for key in ("devices", "hosts", "host", "機器"):
            if key in data:
                items = data[key]
                name_is_host = key in ("hosts", "host")
                break
        else:
            raise InventoryError(
                tr("'devices' の一覧がありません", "expected a 'devices' list")
            )
    if not isinstance(items, list):
        raise InventoryError(
            tr("'devices' は一覧で書いてください", "'devices' must be a list")
        )
    devices: List[Device] = []
    for n, item in enumerate(items, start=1):
        where = tr(f"{n}番目の機器: ", f"device #{n}: ")
        if isinstance(item, str):
            try:
                row = _spec_fields(item)
            except InventoryError as e:
                raise InventoryError(f"{where}{e}") from e
            if not row["ports"]:
                del row["ports"]
            dev = device_from_mapping(row, where=where, defaults=defaults)
        elif isinstance(item, dict):
            dev = device_from_mapping(
                item, where=where, defaults=defaults, name_is_host=name_is_host
            )
        else:
            raise InventoryError(
                where + tr(f"読めない項目です: {item!r}", f"unsupported entry {item!r}")
            )
        devices.append(dev)
    if not devices:
        raise InventoryError(tr("機器の一覧が空です", "the device list is empty"))
    return devices


_WORKBOOK_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods")
_WORKBOOK_MAGIC = (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")


def load_devices(path: str | Path) -> List[Device]:
    """Load a device list; raises :class:`InventoryError` with a clear reason."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise InventoryError(
            tr(f"{p} を読めません: {e}", f"cannot read {p}: {e}")
        ) from e
    if p.suffix.lower() in _WORKBOOK_SUFFIXES or raw.startswith(_WORKBOOK_MAGIC):
        raise InventoryError(
            tr(
                f"{p.name} はExcelのブックです。Excelで「名前を付けて保存」→"
                "「CSV（コンマ区切り）」で保存し、その .csv を指定してください",
                f"{p.name} is an Excel workbook; save it as CSV in Excel "
                "(File > Save As > CSV) and pass the .csv file",
            )
        )
    text = _decode(raw)
    suffix = p.suffix.lower()
    try:
        if suffix in (".yaml", ".yml"):
            return parse_structured(load_yaml_text(text))
        if suffix == ".json":
            return parse_structured(json.loads(text))
        if suffix == ".toml":
            return parse_structured(_load_toml(text))
        return parse_csv_text(text)
    except InventoryError as e:
        raise InventoryError(f"{p.name}: {e}") from e
    except Exception as e:  # syntax errors from the respective parsers
        raise InventoryError(f"{p.name}: {e}") from e


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
    text = str(value if value is not None else "").strip()
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
