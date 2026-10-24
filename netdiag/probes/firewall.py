"""Self-host firewall inspection.

When TCP is refused/filtered, the question is 'their firewall or mine?'. This
probe reports the local DROP/REJECT policy so the diagnosis can attribute a
blocked port to the host's own rules instead of the network.
"""

from __future__ import annotations

from typing import List

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

        if have("nft"):
            res = run(["nft", "list", "ruleset"])
            if res.found and not res.ok and not is_root():
                return CheckResult.skip(
                    self.name,
                    tr(
                        "nftables の確認には root 権限が必要",
                        "need root to read nftables ruleset",
                    ),
                    code="firewall.need_root",
                )
            if res.ok:
                text = res.stdout
                drops = sum(
                    1
                    for ln in text.splitlines()
                    if ("drop" in ln or "reject" in ln) and "#" not in ln
                )
                policy_drop = "policy drop" in text
                detail = {"backend": "nftables", "drop_rules": drops}
                if policy_drop or drops:
                    return CheckResult.warn(
                        self.name,
                        tr(
                            f"nftables 有効（遮断ルール {drops} 件"
                            + ("、既定で遮断" if policy_drop else "")
                            + "）— このPCが通信を止めている可能性",
                            f"nftables active ({drops} drop/reject rule(s)"
                            + (", default-drop policy" if policy_drop else "")
                            + ") — local rules may be blocking the port",
                        ),
                        metrics={"drop_rules": float(drops)},
                        detail=detail,
                        code="firewall.rules",
                    )
                return CheckResult.ok(
                    self.name,
                    tr(
                        "nftables: 遮断ルールなし",
                        "nftables present, no drop/reject rules",
                    ),
                    detail=detail,
                    code="firewall.ok",
                )

        if have("iptables"):
            res = run(["iptables", "-S"])
            if res.found and not res.ok and not is_root():
                return CheckResult.skip(
                    self.name,
                    tr(
                        "iptables の確認には root 権限が必要",
                        "need root to read iptables rules",
                    ),
                    code="firewall.need_root",
                )
            if res.ok:
                drops = sum(
                    1
                    for ln in res.stdout.splitlines()
                    if ln.startswith("-A") and ("DROP" in ln or "REJECT" in ln)
                )
                policy_drop = (
                    "-P INPUT DROP" in res.stdout or "-P OUTPUT DROP" in res.stdout
                )
                if policy_drop or drops:
                    return CheckResult.warn(
                        self.name,
                        tr(
                            f"iptables 有効（遮断ルール {drops} 件"
                            + ("、既定で遮断" if policy_drop else "")
                            + "）",
                            f"iptables active ({drops} DROP/REJECT rule(s)"
                            + (", default-drop policy" if policy_drop else "")
                            + ")",
                        ),
                        metrics={"drop_rules": float(drops)},
                        detail={"backend": "iptables", "drop_rules": drops},
                        code="firewall.rules",
                    )
                return CheckResult.ok(
                    self.name,
                    tr(
                        "iptables: 遮断ルールなし",
                        "iptables present, no DROP/REJECT rules",
                    ),
                    detail={"backend": "iptables"},
                    code="firewall.ok",
                )

        return CheckResult.skip(
            self.name,
            tr("nft/iptables コマンドがありません", "no nft/iptables command found"),
            code="firewall.none",
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
        return (
            tr(
                "このPCのファイアウォールが通信を止めている可能性があります。",
                "The local firewall has rules that may be dropping the traffic.",
            ),
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
