"""Self-host firewall inspection.

When TCP is refused/filtered, the question is 'their firewall or mine?'. This
probe reports the local DROP/REJECT policy so the diagnosis can attribute a
blocked port to the host's own rules instead of the network.
"""

from __future__ import annotations

from typing import List

from ..core import CheckResult, Context, Probe, register
from ..core.util import have, is_linux, is_root, run


@register
class FirewallProbe(Probe):
    name = "firewall"
    title = "Local firewall"
    layer = "L4"
    depends_on: List[str] = []
    requires_root = True
    what = "Reads nftables/iptables policy to see if the host itself is blocking traffic."
    why = (
        "Distinguishes 'remote service blocked us' from 'our own firewall drops it'. "
        "A local default-DROP with no matching allow explains a filtered port."
    )
    manual_commands = ["nft list ruleset", "iptables -S", "ufw status verbose"]

    def check(self, ctx: Context) -> CheckResult:
        if not is_linux():
            return CheckResult.skip(self.name, "firewall inspection only on Linux")

        if have("nft"):
            res = run(["nft", "list", "ruleset"])
            if res.found and not res.ok and not is_root():
                return CheckResult.skip(self.name, "need root to read nftables ruleset")
            if res.ok:
                text = res.stdout
                drops = sum(1 for ln in text.splitlines()
                            if ("drop" in ln or "reject" in ln) and "#" not in ln)
                policy_drop = "policy drop" in text
                detail = {"backend": "nftables", "drop_rules": drops}
                if policy_drop or drops:
                    return CheckResult.warn(
                        self.name,
                        f"nftables active ({drops} drop/reject rule(s)"
                        + (", default-drop policy" if policy_drop else "")
                        + ") — local rules may be blocking the port",
                        metrics={"drop_rules": float(drops)}, detail=detail,
                    )
                return CheckResult.ok(self.name, "nftables present, no drop/reject rules",
                                      detail=detail)

        if have("iptables"):
            res = run(["iptables", "-S"])
            if res.found and not res.ok and not is_root():
                return CheckResult.skip(self.name, "need root to read iptables rules")
            if res.ok:
                drops = sum(1 for ln in res.stdout.splitlines()
                            if ln.startswith("-A") and ("DROP" in ln or "REJECT" in ln))
                policy_drop = "-P INPUT DROP" in res.stdout or "-P OUTPUT DROP" in res.stdout
                if policy_drop or drops:
                    return CheckResult.warn(
                        self.name,
                        f"iptables active ({drops} DROP/REJECT rule(s)"
                        + (", default-drop policy" if policy_drop else "") + ")",
                        metrics={"drop_rules": float(drops)},
                        detail={"backend": "iptables", "drop_rules": drops},
                    )
                return CheckResult.ok(self.name, "iptables present, no DROP/REJECT rules",
                                      detail={"backend": "iptables"})

        return CheckResult.skip(self.name, "no nft/iptables command found")
