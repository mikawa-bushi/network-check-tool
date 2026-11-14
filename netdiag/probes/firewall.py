"""Self-host firewall inspection.

When TCP is refused/filtered, the question is 'their firewall or mine?'. This
probe reports the local DROP/REJECT policy so the diagnosis can attribute a
blocked port to the host's own rules instead of the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, is_root, is_windows, run
from ..i18n import Text, tr

_WIN_FW_SCRIPT = (
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
    "@(Get-NetFirewallProfile | Select-Object Name,"
    "@{n='Enabled';e={\"$($_.Enabled)\"}},"
    "@{n='DefaultInboundAction';e={\"$($_.DefaultInboundAction)\"}},"
    "@{n='DefaultOutboundAction';e={\"$($_.DefaultOutboundAction)\"}})"
    " | ConvertTo-Json -Compress"
)


@dataclass
class Rule:
    blocks: bool  # its verdict drops or rejects the packet
    jumps: List[str] = field(default_factory=list)  # chains it continues in


@dataclass
class Chain:
    hook: Optional[str] = None  # input / output / forward for a base filter chain
    policy: str = "accept"
    rules: List[Rule] = field(default_factory=list)


def _rule(blocks: bool, text: str, jumps: List[str]) -> Rule:
    # "ct state invalid drop" only discards packets that belong to no connection.
    return Rule(blocks=blocks and "invalid" not in text.lower(), jumps=jumps)


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


def parse_windows_profiles(data) -> List[dict]:
    from ..netinfo.windows import _as_list

    return [
        {
            "name": str(p.get("Name")),
            "enabled": str(p.get("Enabled")).lower() in ("true", "1"),
            "inbound": str(p.get("DefaultInboundAction") or ""),
            "outbound": str(p.get("DefaultOutboundAction") or ""),
        }
        for p in _as_list(data)
    ]


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
    manual_commands = ["nft list ruleset", "iptables -S", "Get-NetFirewallProfile"]

    def check(self, ctx: Context) -> CheckResult:
        if is_windows():
            return self._check_windows(ctx)
        if not is_linux():
            return CheckResult.skip(
                self.name,
                tr(
                    "ファイアウォール確認はLinux/Windowsのみ対応",
                    "firewall inspection only on Linux/Windows",
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
                return self._judge(backend, parse(res.stdout), res.stdout)

        return CheckResult.skip(
            self.name,
            tr("nft/iptables コマンドがありません", "no nft/iptables command found"),
            code="firewall.none",
        )

    def _judge(self, backend: str, chains: Dict[str, Chain], text: str) -> CheckResult:
        out_rules = sum(
            1 for c in reachable(chains, "output") for r in chains[c].rules if r.blocks
        )
        out_policy = any(
            c.policy == "drop" for c in chains.values() if c.hook == "output"
        )
        # Without an "established" accept, an input default-drop also eats the
        # replies to this PC's own connections. The word anywhere counts, so a
        # rule this parser cannot follow never raises a false warning.
        replies = "established" not in text.lower() and any(
            c.policy == "drop" for c in chains.values() if c.hook == "input"
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

    def _check_windows(self, ctx: Context) -> CheckResult:
        from ..netinfo.windows import run_powershell

        raw = ctx.network().extras.get("firewall")
        profiles = parse_windows_profiles(
            raw if raw is not None else run_powershell(_WIN_FW_SCRIPT)
        )
        if not profiles:
            return CheckResult.skip(
                self.name,
                tr(
                    "Windowsファイアウォールの状態を取得できません",
                    "could not read Windows Firewall profiles",
                ),
                code="firewall.unavailable",
            )
        detail = {"backend": "windows", "profiles": profiles}
        blocked_out = [
            p["name"]
            for p in profiles
            if p["enabled"] and p["outbound"].lower() == "block"
        ]
        enabled = [p["name"] for p in profiles if p["enabled"]]
        if blocked_out:
            return CheckResult.warn(
                self.name,
                tr(
                    f"Windowsファイアウォールが送信を既定で遮断（{', '.join(blocked_out)}）",
                    f"Windows Firewall blocks outbound by default ({', '.join(blocked_out)})",
                ),
                detail=detail,
                code="firewall.outbound_block",
            )
        return CheckResult.ok(
            self.name,
            tr(
                f"Windowsファイアウォール 有効: {', '.join(enabled) or 'なし'}（送信は許可）",
                f"Windows Firewall enabled: {', '.join(enabled) or 'none'} (outbound allowed)",
            ),
            detail=detail,
            code="firewall.ok",
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
                    "ルールを確認: sudo nft list ruleset / sudo iptables -S / wf.msc (Windows)",
                    "Review the ruleset: sudo nft list ruleset / sudo iptables -S / wf.msc (Windows)",
                ),
                tr(
                    "一時的に該当ポートを許可して、ファイアウォールが原因か確かめる",
                    "Temporarily allow the port to confirm the firewall is the cause.",
                ),
            ],
        )
