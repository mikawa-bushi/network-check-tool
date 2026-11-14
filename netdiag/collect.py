"""Collect the raw command output netdiag depends on, for field verification.

netdiag's Windows support rests on parsing localised tools (``ping``,
``arp``, ``route``, PowerShell). ``netdiag collect`` runs exactly those
commands on a real shop-floor PC and stores their **raw bytes** (before any
decoding) plus netdiag's own interpretation in one zip. A developer can turn
the result into test fixtures, and a mismatch between raw output and
interpretation shows up immediately.

Masking (on by default) rewrites identifying values *in the bytes*, so the
original encoding (UTF-8, cp932 ...) is preserved: IPv4 addresses are mapped
consistently while keeping /16 and /24 groupings and the last octet, MACs
(also inside a DHCPv6 DUID) keep their vendor prefix (OUI), IPv6 interface
IDs, the PC's host name, the user name, Wi-Fi SSIDs, the DNS suffixes and
search domains, and names with three or more labels are replaced, in every
file of the bundle. It is best effort — review the files before sending them
outside the company.
"""

from __future__ import annotations

import getpass
import json
import platform
import re
import socket
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import InputError
from .core.util import decode_output, os_name, run_bytes
from .i18n import tr

TEST_NET = "192.0.2.1"  # RFC 5737 documentation address: never answers
UNKNOWN_HOST = "netdiag-nonexistent.invalid"  # RFC 6761: never resolves

KEEP_IPV4 = {
    "0.0.0.0",
    "255.255.255.255",
    TEST_NET,
    "1.1.1.1",
    "1.0.0.1",
    "8.8.8.8",
    "8.8.4.4",
    "9.9.9.9",
}
KEEP_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "netdiag-nonexistent.invalid",
}


# ---------------------------------------------------------------------- masking

_IPV4_RE = re.compile(rb"(?<![\d])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d])")
# A lead byte of a Shift_JIS character must not be taken as part of a token.
_NOT_TOKEN = rb"(?<![0-9A-Fa-f:\-\x81-\x9f\xe0-\xfc])"
_HEX = rb"([0-9A-Fa-f]{1,2})"
_MAC_RE = re.compile(
    _NOT_TOKEN
    + _HEX
    + rb"([:\-])"
    + rb"\2".join([_HEX] * 5)
    + rb"(?![0-9A-Fa-f:]|-[0-9A-Fa-f])"
)
# ``route print`` lists each adapter as ``12...00 15 5d 01 02 03 ......Name``.
_MAC_SPACED_RE = re.compile(
    rb"(?<=\.\.\.)" + _HEX + rb"( )" + rb"\2".join([_HEX] * 5) + rb"(?= \.)"
)
# Seven or more byte groups: a DHCPv6 DUID, whose last six bytes are a MAC.
_HEX_RUN_RE = re.compile(
    _NOT_TOKEN
    + rb"([0-9A-Fa-f]{2}([:\-])[0-9A-Fa-f]{2}(?:\2[0-9A-Fa-f]{2}){5,})"
    + rb"(?![:\-]?[0-9A-Fa-f])"
)
_IPV6_RE = re.compile(
    _NOT_TOKEN
    + rb"((?:[0-9A-Fa-f]{1,4}:){1,7}:?(?:[0-9A-Fa-f]{1,4})?(?::[0-9A-Fa-f]{1,4})*)(%[\w.]+)?(?![0-9A-Fa-f:])"
)
_DOMAIN_RE = re.compile(
    rb"(?<![\w.\-])((?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.){2,}[A-Za-z]{2,24})(?![\w\-])"
)
_DOMAIN_SHAPE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}\.?"
)


def _line_values(key: str) -> bytes:
    """``key`` in UTF-8 or cp932, then ``:`` and the value to the end of the line."""
    keys = {re.escape(key.encode(enc)) for enc in ("utf-8", "cp932")}
    return (
        rb"(?m)^[ \t]*(?:"
        + b"|".join(sorted(keys))
        + rb")[ \t]*:[ \t]*(\S[^\r\n]*?)[ \t]*\r?$"
    )


# Where SSIDs appear: netsh (``SSID :``, ``Profile :``), iw (``ssid X``,
# ``SSID: X``).
_SSID_RES = [
    re.compile(_line_values("SSID")),
    re.compile(_line_values("Profile")),
    re.compile(_line_values("プロファイル")),
    re.compile(rb"(?m)^[ \t]*ssid[ \t]+(\S[^\r\n]*?)[ \t]*\r?$"),
]
# Where internal domains appear: ipconfig DNS suffix lines (and the indented
# lines that continue the search list), resolv.conf, ``scutil --dns``.
_SUFFIX_KEY_RE = re.compile(
    rb"(?i)^[^:\r\n]*(?:dns suffix|"
    + "サフィックス".encode("cp932")
    + b"|"
    + "サフィックス".encode()
    + rb")[^:\r\n]*:[ \t]*(\S*)"
)
_CONTINUATION_RE = re.compile(rb"^[ \t]{8,}(\S+)[ \t]*$")
_RESOLV_RE = re.compile(rb"(?m)^(?:search|domain)[ \t]+([^\r\n#]+)")
_SCUTIL_RE = re.compile(
    rb"(?m)^[ \t]*(?:search domain\[\d+\]|domain)[ \t]*:[ \t]*(\S+)"
)


_NOT_TLDS = {
    b"arpa",
    b"py",
    b"exe",
    b"dll",
    b"json",
    b"txt",
    b"log",
    b"ini",
    b"cfg",
    b"sys",
    b"zip",
}


def _boundary(literal: bytes) -> re.Pattern:
    return re.compile(
        rb"(?<![\w.\-])" + re.escape(literal) + rb"(?![\w\-])", re.IGNORECASE
    )


class Masker:
    """Consistent, structure-preserving replacement of identifying values.

    SSIDs and internal domains are recognised where the OS prints them
    (:meth:`learn`), then replaced wherever they occur in any file, summaries
    and JSON included. :meth:`mask` learns from its input first, so masking
    one buffer on its own works too.
    """

    def __init__(
        self, hostnames: Optional[List[str]] = None, users: Optional[List[str]] = None
    ) -> None:
        self.nets16: Dict[Tuple[int, int], int] = {}
        self.thirds: Dict[Tuple[int, int, int], int] = {}
        self.macs: Dict[bytes, bytes] = {}
        self.v6: Dict[bytes, bytes] = {}
        self.ssids: Dict[str, bytes] = {}
        self.domains: Dict[bytes, bytes] = {}
        self.literals: List[Tuple[bytes, bytes]] = []
        for h in hostnames or []:
            if h and len(h) >= 3:
                self.literals.append((h.encode("utf-8", "ignore"), b"PC-HOST"))
        for u in users or []:
            if u and len(u) >= 3:
                self.literals.append((u.encode("utf-8", "ignore"), b"USER"))

    # learning --------------------------------------------------------------
    def _learn_ssid(self, value: str) -> None:
        value = value.strip()
        if value and value not in self.ssids:
            self.ssids[value] = f"SSID-{len(self.ssids) + 1}".encode()

    def _learn_domain(self, value: bytes) -> None:
        name = value.strip().rstrip(b".").lower()
        text = name.decode("ascii", "ignore")
        if _DOMAIN_SHAPE.fullmatch(text) and text not in KEEP_DOMAINS:
            self.domains.setdefault(
                name, f"domain{len(self.domains) + 1}.example".encode()
            )

    def learn(self, data: bytes) -> None:
        """Remember the SSIDs and internal domains that *data* names."""
        for rx in _SSID_RES:
            for m in rx.finditer(data):
                self._learn_ssid(decode_output(m.group(1)))
        for m in _RESOLV_RE.finditer(data):
            for token in m.group(1).split():
                self._learn_domain(token)
        for m in _SCUTIL_RE.finditer(data):
            self._learn_domain(m.group(1))
        in_suffix = False
        for line in data.splitlines():
            m = _SUFFIX_KEY_RE.match(line)
            if m:
                self._learn_domain(m.group(1))
                in_suffix = True
                continue
            cont = _CONTINUATION_RE.match(line) if in_suffix else None
            if cont:
                self._learn_domain(cont.group(1))
            else:
                in_suffix = False

    def learn_object(self, obj: Any) -> None:
        """Learn from parsed data: ``ssid`` fields and raw tool text inside it."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() == "ssid" and isinstance(value, str):
                    self._learn_ssid(value)
                self.learn_object(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                self.learn_object(value)
        elif isinstance(obj, str):
            self.learn(obj.encode("utf-8"))

    # IPv4 ------------------------------------------------------------------
    def _ipv4(self, m: re.Match) -> bytes:
        octets = [int(x) for x in m.groups()]
        text = m.group(0).decode()
        if any(o > 255 for o in octets):
            return m.group(0)
        a, b, c, d = octets
        if (
            text in KEEP_IPV4
            or a in (0, 127)
            or a >= 224  # multicast / reserved / netmask-shaped (255.x)
            or (a, b) == (169, 254)  # link-local (APIPA) is not identifying
            or self._is_mask(octets)
        ):
            return m.group(0)
        # Each /16 gets its own 10.x block and each /24 its own third octet
        # in it, so /16 and /24 relationships survive. Third octets 0 and 255
        # stay, so a /16's network and broadcast addresses stay valid.
        n = self.nets16.setdefault((a, b), len(self.nets16))
        if c in (0, 255):
            y = c
        else:
            if (a, b, c) not in self.thirds:
                used = sum(1 for k in self.thirds if k[:2] == (a, b))
                self.thirds[(a, b, c)] = used + 1
            y = self.thirds[(a, b, c)]
        return f"{10 + n // 155}.{100 + n % 155}.{y}.{d}".encode()

    @staticmethod
    def _is_mask(octets: List[int]) -> bool:
        bits = "".join(f"{o:08b}" for o in octets)
        return "01" not in bits and bits.startswith("1")  # 255.255.255.0 etc.

    # MAC -------------------------------------------------------------------
    def _mac_groups(self, groups: List[bytes], sep: bytes) -> bytes:
        norm = b"".join(g.zfill(2).lower() for g in groups)
        if norm in (b"ffffffffffff", b"000000000000") or norm.startswith(b"01005e"):
            return sep.join(groups)
        if norm not in self.macs:
            n = len(self.macs) + 1
            self.macs[norm] = (
                f"{(n >> 16) & 0xFF:02x}{(n >> 8) & 0xFF:02x}{n & 0xFF:02x}".encode()
            )
        tail = self.macs[norm]
        upper = any(chr(ch).isupper() for g in groups for ch in g)
        tail_parts = [tail[0:2], tail[2:4], tail[4:6]]
        if upper:
            tail_parts = [t.upper() for t in tail_parts]
        return sep.join(groups[:3] + tail_parts)

    def _mac(self, m: re.Match) -> bytes:
        return self._mac_groups([m.group(i) for i in (1, 3, 4, 5, 6, 7)], m.group(2))

    def _hex_run(self, m: re.Match) -> bytes:
        sep = m.group(2)
        groups = m.group(1).split(sep)
        keep = min(2, len(groups) - 6)  # the DUID type
        head = groups[:keep] + [b"00"] * (len(groups) - 6 - keep)
        return sep.join(head + [self._mac_groups(groups[-6:], sep)])

    # IPv6 ------------------------------------------------------------------
    def _ipv6(self, m: re.Match) -> bytes:
        addr = m.group(1)
        if addr.count(b":") < 2 or (
            b"::" not in addr and not any(len(g) >= 3 for g in addr.split(b":"))
        ):
            return m.group(0)  # not an IPv6 address (MAC-like, times ...)
        if addr in (b"::", b"::1"):
            return m.group(0)
        if addr not in self.v6:
            first = addr.split(b":")[0] or b""
            self.v6[addr] = first + b"::" + f"{len(self.v6) + 1:x}".encode()
        return self.v6[addr] + (m.group(2) or b"")

    def _domain(self, m: re.Match) -> bytes:
        name = m.group(1)
        low = name.lower()
        tld = low.rsplit(b".", 1)[-1]
        if (
            low.decode("ascii", "ignore") in KEEP_DOMAINS
            or tld in _NOT_TLDS
            or len(tld) > 6  # dotted identifiers (Microsoft.PowerShell.Commands ...)
        ):
            return name
        if re.fullmatch(rb"[\d.]+", name):
            return name
        if low not in self.domains:
            self.domains[low] = f"domain{len(self.domains) + 1}.example".encode()
        return self.domains[low]

    def mask(self, data: bytes) -> bytes:
        self.learn(data)
        for old, new in self.literals:
            data = re.sub(re.escape(old), new, data, flags=re.IGNORECASE)
        for ssid, new in self.ssids.items():
            for enc in ("utf-8", "cp932"):
                try:
                    literal = ssid.encode(enc)
                except UnicodeEncodeError:
                    continue
                data = re.sub(
                    rb"(?<![A-Za-z0-9])" + re.escape(literal) + rb"(?![A-Za-z0-9])",
                    new,
                    data,
                )
        data = _IPV6_RE.sub(self._ipv6, data)
        data = _HEX_RUN_RE.sub(self._hex_run, data)
        data = _MAC_RE.sub(self._mac, data)
        data = _MAC_SPACED_RE.sub(self._mac, data)
        data = _IPV4_RE.sub(self._ipv4, data)
        data = _DOMAIN_RE.sub(self._domain, data)
        for name, new in list(self.domains.items()):
            data = _boundary(name).sub(new, data)
        return data


# --------------------------------------------------------------------- commands


@dataclass
class Command:
    name: str
    args: List[str]
    timeout: float = 20.0


@dataclass
class Captured:
    """A planned command plus its :class:`~netdiag.core.util.RawResult` fields."""

    name: str
    args: List[str]
    returncode: Optional[int]
    stdout: bytes = b""
    stderr: bytes = b""
    seconds: float = 0.0
    found: bool = True
    timed_out: bool = False


def run_raw(cmd: Command) -> Captured:
    """Run without decoding; never raises."""
    return Captured(cmd.name, cmd.args, **vars(run_bytes(cmd.args, cmd.timeout)))


def plan(
    os_kind: str, gateway: Optional[str], targets: List[str], ping: bool = True
) -> List[Command]:
    """The commands netdiag uses on *os_kind*, plus pings that show the local
    wording of replies, timeouts and unknown hosts."""
    from .measure.icmp import build_ping_args

    gw = gateway or TEST_NET
    cmds: List[Command] = []
    if os_kind == "windows":
        from .netinfo.windows import COLLECT_SCRIPT, NEIGH_SCRIPT, powershell_args

        cmds += [
            Command("ps_collect.json", powershell_args(COLLECT_SCRIPT), 60),
            Command("ps_neighbors.json", powershell_args(NEIGH_SCRIPT), 60),
            Command("ipconfig_all.txt", ["ipconfig", "/all"]),
            Command("route_print.txt", ["route", "print", "-4"]),
            Command("arp_a.txt", ["arp", "-a"]),
            Command("netsh_wlan.txt", ["netsh", "wlan", "show", "interfaces"]),
            Command(
                "netsh_firewall.txt",
                ["netsh", "advfirewall", "show", "allprofiles", "state"],
            ),
            Command("w32tm_status.txt", ["w32tm", "/query", "/status"]),
            Command("chcp.txt", ["cmd", "/c", "chcp"]),
        ]
    elif os_kind == "macos":
        cmds += [
            Command("ifconfig.txt", ["ifconfig", "-a"]),
            Command("netstat_rn.txt", ["netstat", "-rn", "-f", "inet"]),
            Command("netstat_ibn.txt", ["netstat", "-ibn"]),
            Command("arp_an.txt", ["arp", "-an"]),
            Command("scutil_dns.txt", ["scutil", "--dns"]),
            Command("hardware_ports.txt", ["networksetup", "-listallhardwareports"]),
            Command("route_default.txt", ["route", "-n", "get", "default"]),
        ]
    else:
        cmds += [
            Command("ip_addr.json", ["ip", "-j", "addr", "show"]),
            Command("ip_route.json", ["ip", "-j", "route", "show"]),
            Command("ip_neigh.json", ["ip", "-j", "-s", "neigh", "show"]),
            Command("ip_addr.txt", ["ip", "-o", "addr", "show"]),
            Command("ip_route.txt", ["ip", "route", "show"]),
            Command("ip_neigh.txt", ["ip", "neigh", "show"]),
            Command("iw_dev.txt", ["iw", "dev"]),
            Command("timedatectl.txt", ["timedatectl", "show"]),
            Command("resolv_conf.txt", ["cat", "/etc/resolv.conf"]),
            Command(
                "ping_group_range.txt", ["cat", "/proc/sys/net/ipv4/ping_group_range"]
            ),
        ]
    if not ping:
        return cmds

    def ping_cmd(name: str, host: str, count: int, timeout: float = 1.0) -> Command:
        # Exactly what netdiag's system-ping fallback runs on that OS.
        args = build_ping_args(host, count, timeout, 0.2, os_name=os_kind)
        return Command(name, args)

    cmds += [
        ping_cmd("ping_gateway.txt", gw, 4),
        ping_cmd("ping_testnet.txt", TEST_NET, 2, timeout=0.5),
        ping_cmd("ping_unknown_host.txt", UNKNOWN_HOST, 1),
    ]
    if os_kind == "windows":
        cmds += [
            ping_cmd("ping_loopback.txt", "127.0.0.1", 2),
            Command(
                "tracert_gateway.txt", ["tracert", "-d", "-h", "3", "-w", "500", gw], 40
            ),
        ]
    elif os_kind != "macos":
        cmds.append(
            Command("tracepath_gateway.txt", ["tracepath", "-n", "-m", "3", gw], 40)
        )
    cmds += [ping_cmd(f"ping_target_{i}.txt", t, 4) for i, t in enumerate(targets)]
    return cmds


def _sysfs_links() -> Dict[str, Dict[str, str]]:
    base = Path("/sys/class/net")
    out: Dict[str, Dict[str, str]] = {}
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        rec: Dict[str, str] = {}
        for name in (
            "operstate",
            "carrier",
            "speed",
            "duplex",
            "mtu",
            "type",
            "carrier_changes",
        ):
            try:
                rec[name] = (d / name).read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
        stats = d / "statistics"
        if stats.is_dir():
            for f in stats.iterdir():
                try:
                    rec[f"statistics.{f.name}"] = f.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
        rec["wireless"] = str((d / "wireless").exists())
        rec["device"] = str((d / "device").exists())
        out[d.name] = rec
    return out


README = """netdiag collect — ネットワーク情報の収集結果 / collected network data
================================================================

このzipには、netdiag が使うOSコマンドの「生の出力」（文字コード変換前のバイト列）と、
netdiag 自身の解釈結果が入っています。開発者が実機の出力形式を確認し、
テストデータを作るために使います。

  commands/   コマンドごとの出力（.out = 標準出力, .err = 標準エラー）
  netdiag/    netdiag による解釈（netinfo.json, check.json, ping.json）
  manifest.json  実行したコマンド・終了コード・所要時間・マスク有無

マスク（既定で有効）: IPアドレス（/16・/24 のまとまりと末尾は保持）、MACアドレス
（メーカー部分は保持）、IPv6、PC名、ユーザー名、Wi-Fi の SSID、ドメイン名を置き換えています。
自動処理のため完全ではありません。社外に送る前に内容を確認してください。

This zip holds the raw bytes of the OS commands netdiag relies on and netdiag's
own interpretation, for building test fixtures from real machines. Masking is
best effort (IPs keep their /16 and /24 grouping and last octet, MACs their vendor
prefix; host/user names, SSIDs and domains are replaced) — review before sharing.
"""


@dataclass
class CollectResult:
    path: Path
    commands: List[Captured] = field(default_factory=list)
    masked: bool = True

    @property
    def missing(self) -> List[str]:
        return [c.name for c in self.commands if not c.found]


def run_collect(
    output: Path,
    mask: bool = True,
    targets: Optional[List[str]] = None,
    ping: bool = True,
    progress: Optional[Callable[[str], None]] = None,
    runner: Callable[[Command], Captured] = run_raw,
) -> CollectResult:
    from . import __version__, netinfo
    from .i18n import get_language
    from .measure import ping_many

    targets = list(targets or [])
    kind = os_name()
    hn = netinfo.collect()
    gateways = hn.default_gateways()
    masker = Masker(
        hostnames=list(
            {platform.node(), socket.gethostname(), platform.node().split(".")[0]}
        ),
        users=[_user()],
    )

    # Everything is gathered unmasked first: an SSID or domain learned from
    # one file must be masked in every other file too.
    result = CollectResult(path=output, masked=mask)
    files: Dict[str, bytes] = {}
    parsed: List[Any] = []
    for cmd in plan(kind, gateways[0] if gateways else None, targets, ping=ping):
        if progress:
            progress(cmd.name)
        cap = runner(cmd)
        result.commands.append(cap)
        files[f"commands/{cmd.name}.out"] = cap.stdout
        if cap.stderr:
            files[f"commands/{cmd.name}.err"] = cap.stderr

    def dump(obj) -> bytes:
        parsed.append(obj)
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode(
            "utf-8"
        )

    files["netdiag/netinfo.json"] = dump(hn.to_dict())
    if kind == "linux":
        files["netdiag/sysfs_links.json"] = dump(_sysfs_links())
    if progress:
        progress("netdiag check")
    try:
        from .runner import run_check

        rep = run_check([], config={"internet": False})
        files["netdiag/check.json"] = dump(rep.to_dict())
    except Exception as e:  # interpretation errors are exactly what we want to see
        files["netdiag/check_error.txt"] = f"{type(e).__name__}: {e}".encode()
    if ping:
        if progress:
            progress("netdiag ping")
        try:
            probe = ["127.0.0.1"] + gateways[:1] + targets
            stats = ping_many(probe, count=4, interval=0.2, timeout=1.0)
            files["netdiag/ping.json"] = dump(
                {t: s.to_dict() for t, s in stats.items()}
            )
        except Exception as e:
            files["netdiag/ping_error.txt"] = f"{type(e).__name__}: {e}".encode()

    manifest = {
        "netdiag_version": __version__,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "os": kind,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "language": get_language(),
        "preferred_encoding": _preferred_encoding(),
        "masked": mask,
        "commands": [
            {
                "name": c.name,
                "args": (
                    c.args[:-1] + ["<script>"] if c.name.startswith("ps_") else c.args
                ),
                "returncode": c.returncode,
                "seconds": round(c.seconds, 3),
                "found": c.found,
                "timed_out": c.timed_out,
                "stdout_bytes": len(c.stdout),
                "stderr_bytes": len(c.stderr),
            }
            for c in result.commands
        ],
    }
    files["manifest.json"] = dump(manifest)
    if mask:
        for data in files.values():
            masker.learn(data)
        for obj in parsed:
            masker.learn_object(obj)
        files = {name: masker.mask(data) for name, data in files.items()}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("netdiag-collect/README.txt", README)
            for name, data in sorted(files.items()):
                zf.writestr(f"netdiag-collect/{name}", data)
    except OSError as e:
        raise InputError(
            tr(
                f"{output} に書き込めません（{e.strerror or e}）。-o で別の保存先を指定してください",
                f"cannot write {output} ({e.strerror or e}); choose another path with -o",
            )
        ) from e
    return result


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _preferred_encoding() -> str:
    import locale

    return locale.getpreferredencoding(False)


def default_output() -> Path:
    return Path(f"netdiag-collect-{time.strftime('%Y%m%d-%H%M%S')}.zip")


__all__ = [
    "Masker",
    "Command",
    "Captured",
    "plan",
    "run_collect",
    "run_raw",
    "default_output",
]
