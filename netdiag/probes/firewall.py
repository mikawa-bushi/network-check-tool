"""Self-host firewall inspection.

When TCP is refused/filtered, the question is 'their firewall or mine?'. This
probe reports the local DROP/REJECT policy so the diagnosis can attribute a
blocked port to the host's own rules instead of the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network
from typing import Dict, List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, is_root, run
from ..i18n import Text, tr


@dataclass
class Rule:
    blocks: bool  # it drops or rejects traffic this PC really sends or receives
    jumps: List[str] = field(default_factory=list)  # chains it continues in
    established: bool = False  # it names established connections


@dataclass
class Chain:
    hook: Optional[str] = None  # input / output / forward for a base filter chain
    policy: str = "accept"
    rules: List[Rule] = field(default_factory=list)


# IPv6 destinations this PC never sends to: IPv4-compatible and IPv4-mapped
# addresses, and the 6to4 form of IPv4 ranges no 6to4 relay can reach
# (RFC 3964 5.1). Stock firewalld rejects these on output.
_NEVER_SENT_TO = [IPv6Network("::/96"), IPv6Network("::ffff:0:0/96")] + [
    IPv6Network((0x2002 << 112 | int(v4.network_address) << 80, 16 + v4.prefixlen))
    for v4 in map(
        IPv4Network,
        (
            "0.0.0.0/8",
            "10.0.0.0/8",
            "127.0.0.0/8",
            "169.254.0.0/16",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "224.0.0.0/3",
        ),
    )
]
_NFT_DADDR6 = re.compile(r"\bip6\s+daddr\s+(\{[^}]*\}|\S+)")


def _only_never_sent_to(text: str) -> bool:
    """The rule matches only IPv6 destinations this PC never sends to."""
    m = _NFT_DADDR6.search(text)
    if m is None:
        return False
    try:
        nets = [
            IPv6Network(a.strip(), strict=False)
            for a in m.group(1).strip("{}").split(",")
            if a.strip()
        ]
    except ValueError:  # a negation, a named set or a range may reach anything
        return False
    return bool(nets) and all(
        any(n.subnet_of(never) for never in _NEVER_SENT_TO) for n in nets
    )


def _rule(blocks: bool, text: str, jumps: List[str]) -> Rule:
    # "ct state invalid drop" only discards packets that belong to no connection.
    lowered = text.lower()
    return Rule(
        blocks=blocks and "invalid" not in lowered and not _only_never_sent_to(text),
        jumps=jumps,
        established="established" in lowered,
    )


_NFT_JUMP = re.compile(r"\b(?:jump|goto)\s+([\w.-]+)")
_IPT_HOOKS = {"INPUT": "input", "OUTPUT": "output", "FORWARD": "forward"}


def parse_nft_ruleset(text: str) -> Dict[str, Chain]:
    """``nft list ruleset`` -> chains keyed ``"<family> <table> <chain>"``."""
    chains: Dict[str, Chain] = {}
    table = ""
    chain: Optional[Chain] = None
    rule_text = ""
    depth = 0
    for raw in text.splitlines():
        line = re.sub(r'"[^"]*"', '""', raw).split("#", 1)[0].strip()
        words = line.split()
        if depth == 0 and words[:1] == ["table"] and line.endswith("{"):
            table = " ".join(words[1:-1])
        elif depth == 1 and words[:1] == ["chain"] and line.endswith("{"):
            chain = chains.setdefault(f"{table} {words[1]}", Chain())
        elif chain is not None and depth >= 2 and line not in ("", "}"):
            rule_text = line if depth == 2 else f"{rule_text} {line}"
            hook = re.search(r"\btype\s+filter\s+hook\s+(\w+)", line)
            policy = re.search(r"\bpolicy\s+(\w+)", line)
            if hook or policy:
                chain.hook = hook.group(1) if hook else chain.hook
                chain.policy = policy.group(1) if policy else chain.policy
            else:
                rule = _rule(
                    re.search(r"\b(?:drop|reject)\b", rule_text) is not None,
                    rule_text,
                    [f"{table} {j}" for j in _NFT_JUMP.findall(rule_text)],
                )
                if depth == 2:
                    chain.rules.append(rule)
                elif chain.rules:  # a rule continued over several lines
                    chain.rules[-1] = rule
        depth += line.count("{") - line.count("}")
        if depth < 2:
            chain = None
    return chains


def parse_iptables_rules(text: str) -> Dict[str, Chain]:
    """``iptables -S`` (the filter table) -> chains keyed by name."""
    chains: Dict[str, Chain] = {}
    for line in text.splitlines():
        w = line.split()
        if len(w) < 2 or w[0] not in ("-P", "-N", "-A"):
            continue
        chain = chains.setdefault(w[1], Chain(hook=_IPT_HOOKS.get(w[1])))
        if w[0] == "-P" and len(w) > 2:
            chain.policy = w[2].lower()
        elif w[0] == "-A":
            target = next(
                (w[i + 1] for i in range(len(w) - 1) if w[i] in ("-j", "-g")), ""
            )
            chain.rules.append(_rule(target in ("DROP", "REJECT"), line, [target]))
    return chains


def reachable(chains: Dict[str, Chain], hook: str) -> List[str]:
    """Every chain a packet on *hook* can pass through: base chains and jumps."""
    seen = [k for k, c in chains.items() if c.hook == hook]
    for key in seen:
        for rule in chains[key].rules:
            seen.extend(j for j in rule.jumps if j in chains and j not in seen)
    return seen


@register
class FirewallProbe(Probe):
    name = "firewall"
    title = Text("このPCのファイアウォール", "Local firewall")
    layer = "L4"
    depends_on: List[str] = []
    requires_root = True
    what = Text(
        "このPC自身のファイアウォール設定（遮断ルール）を確認します。",
        "Reads nftables/iptables policy to see if the host itself is blocking traffic.",
    )
    why = Text(
        "『相手側で遮断』と『自分のFWが遮断』を区別します。",
        "Distinguishes 'remote service blocked us' from 'our own firewall drops it'. "
        "A local default-DROP with no matching allow explains a filtered port.",
    )
    manual_commands = ["nft list ruleset", "iptables -S"]

    def check(self, ctx: Context) -> CheckResult:
        if not is_linux():
            return CheckResult.skip(
                self.name,
                tr(
                    "ファイアウォール確認はLinuxのみ対応",
                    "firewall inspection only on Linux",
                ),
                code="firewall.unsupported",
            )

        for backend, args, parse in (
            ("nftables", ["nft", "list", "ruleset"], parse_nft_ruleset),
            ("iptables", ["iptables", "-S"], parse_iptables_rules),
        ):
            if not have(args[0]):
                continue
            res = run(args)
            if res.found and not res.ok and not is_root():
                return CheckResult.skip(
                    self.name,
                    tr(
                        f"{backend} の確認には root 権限が必要",
                        f"need root to read {backend} rules",
                    ),
                    code="firewall.need_root",
                )
            if res.ok:
                return self._judge(backend, parse(res.stdout))

        return CheckResult.skip(
            self.name,
            tr("nft/iptables コマンドがありません", "no nft/iptables command found"),
            code="firewall.none",
        )

    def _judge(self, backend: str, chains: Dict[str, Chain]) -> CheckResult:
        out_rules = sum(
            1 for c in reachable(chains, "output") for r in chains[c].rules if r.blocks
        )
        out_policy = any(
            c.policy == "drop" for c in chains.values() if c.hook == "output"
        )
        # Without an "established" accept on the input path, an input
        # default-drop also eats the replies to this PC's own connections.
        # Any rule on that path that names established connections counts,
        # whatever else it matches.
        replies = any(
            c.policy == "drop" for c in chains.values() if c.hook == "input"
        ) and not any(
            r.established for c in reachable(chains, "input") for r in chains[c].rules
        )
        detail = {
            "backend": backend,
            "outbound_drop_rules": out_rules,
            "outbound_default_drop": out_policy,
            "replies_dropped": replies,
        }
        parts = []
        if out_policy:
            parts.append(tr("送信を既定で遮断", "blocks outbound traffic by default"))
        if out_rules:
            parts.append(
                tr(
                    f"送信を遮断するルール {out_rules} 件",
                    f"{out_rules} rule(s) drop or reject outbound traffic",
                )
            )
        if replies:
            parts.append(
                tr(
                    "受信を既定で遮断し、通信の応答（established）を許可していません",
                    "drops incoming traffic by default without allowing replies (established)",
                )
            )
        if not parts:
            return CheckResult.ok(
                self.name,
                tr(
                    f"{backend}: 送信とその応答は許可されています",
                    f"{backend}: outbound traffic and its replies are allowed",
                ),
                detail=detail,
                code="firewall.ok",
            )
        return CheckResult.warn(
            self.name,
            f"{backend}: "
            + tr("、", "; ").join(parts)
            + tr(
                " — このPCが通信を止めている可能性",
                " — this PC may be blocking the traffic",
            ),
            metrics={"outbound_drop_rules": float(out_rules)},
            detail=detail,
            code=(
                "firewall.outbound_block"
                if out_policy or out_rules
                else "firewall.replies_blocked"
            ),
        )

    def advice(self, result, results=None):
        if result is not None and result.code == "firewall.replies_blocked":
            means = tr(
                "このPCのファイアウォールが、自分の通信への応答を受信で捨てています。",
                "The local firewall drops the replies to this PC's own connections.",
            )
        else:
            means = tr(
                "このPCのファイアウォールが送信を止めている可能性があります。",
                "The local firewall may be blocking traffic this PC sends.",
            )
        return (
            means,
            [
                tr(
                    "ルールを確認: sudo nft list ruleset / sudo iptables -S",
                    "Review the ruleset: sudo nft list ruleset / sudo iptables -S",
                ),
                tr(
                    "一時的に該当ポートを許可して、ファイアウォールが原因か確かめる",
                    "Temporarily allow the port to confirm the firewall is the cause.",
                ),
            ],
        )
