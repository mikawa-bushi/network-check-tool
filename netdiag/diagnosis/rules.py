"""Per-cause explanations and remediation runbooks.

The engine decides *which* probe is the root cause; this module supplies the
human-facing 'what this means and what to do next' for each one. Kept as data
(not buried in the engine) so adding a new probe's narrative is a localised edit.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from ..core import CheckResult, get_probe

# What each failing probe means + the manual steps to confirm/fix it.
RUNBOOKS: Dict[str, Dict] = {
    "interface": {
        "means": "No usable network interface — the link is down or no IP was assigned.",
        "steps": [
            "Check the cable / Wi-Fi association.",
            "Verify the interface is UP: `ip link set <iface> up`.",
            "Renew DHCP: `sudo dhclient -v <iface>` (or reconnect Wi-Fi).",
        ],
    },
    "neighbor": {
        "means": "Layer-2 problem: an IP conflict or the gateway's MAC can't be resolved.",
        "steps": [
            "Look for a duplicate IP on the LAN (another device with the same address).",
            "Flush and re-resolve: `ip neigh flush all` then `ping <gateway>`.",
        ],
    },
    "gateway": {
        "means": "The default gateway is not responding — a local/LAN or router fault.",
        "steps": [
            "From another device on the same LAN, ping the gateway. If that also "
            "fails, the router is the problem; if it works, the fault is this host.",
            "Reboot the router / access point.",
            "Confirm the route exists: `ip route show default`.",
        ],
    },
    "external": {
        "means": "The LAN works but traffic can't reach the outside — a WAN/upstream fault.",
        "steps": [
            "Check the modem/ONU status lights.",
            "Run `traceroute` to see the last responding hop (netdiag --only traceroute).",
            "If it stops at the ISP, contact them with the hop where it breaks.",
        ],
    },
    "dns": {
        "means": "Connectivity is fine but names don't resolve — a DNS/resolver fault.",
        "steps": [
            "Test the resolver directly: `nslookup example.com <resolver-ip>`.",
            "Try a public resolver: `nslookup example.com 1.1.1.1`.",
            "Inspect `/etc/resolv.conf`; a stale or unreachable nameserver is common.",
        ],
    },
    "tcp": {
        "means": "The host resolves and is reachable, but the service port won't connect.",
        "steps": [
            "Confirm the service is listening on the server side: `ss -tlnp`.",
            "Check whether a firewall blocks the port (netdiag --only firewall).",
            "`refused` = nothing listening; `timeout` = a firewall is dropping packets.",
        ],
    },
    "http": {
        "means": "Network reaches the server but the HTTP request failed or 5xx'd.",
        "steps": [
            "Read the phase timing: a slow `ttfb` with fast `connect` is a slow server.",
            "Check the server's own logs / health endpoint.",
        ],
    },
    "tls": {
        "means": "The TLS handshake or certificate validation failed.",
        "steps": [
            "Check the certificate expiry and your system clock (skew breaks TLS).",
            "`openssl s_client -connect <host>:443` to see the chain.",
        ],
    },
    "dualstack": {
        "means": "One IP family (v4 or v6) is broken while the other works.",
        "steps": [
            "If IPv6 is broken, check router advertisements / disable v6 to confirm.",
            "Compare `ping -4` and `ping -6` to the same host.",
        ],
    },
    "firewall": {
        "means": "The local firewall has rules that may be dropping the traffic.",
        "steps": [
            "Review the ruleset: `sudo nft list ruleset` or `sudo iptables -S`.",
            "Temporarily allow the port to confirm the firewall is the cause.",
        ],
    },
    "system": {
        "means": "An OS-side resource problem is degrading the network, not the network itself.",
        "steps": [
            "Check memory and load (`free -h`, `uptime`).",
            "If conntrack is full, raise `net.netfilter.nf_conntrack_max` or find the leak.",
        ],
    },
    "clock": {
        "means": "The system clock is not synchronised, which can break TLS and auth.",
        "steps": ["Enable NTP: `sudo timedatectl set-ntp true`."],
    },
    "wifi": {
        "means": "The Wi-Fi radio link is weak or not associated.",
        "steps": ["Move closer to the AP / reduce interference; reconnect."],
    },
}


def _probe_runbook(cause: str) -> Dict:
    """Narrative pulled from a probe's own class attributes.

    Lets plugin probes (absent from ``RUNBOOKS``) still contribute a 'means' line
    and next-steps, via their ``means``/``runbook`` (falling back to ``why``/
    ``manual_commands``). Returns ``{}`` for an unknown cause.
    """
    cls = get_probe(cause)
    if cls is None:
        return {}
    steps = list(getattr(cls, "runbook", []) or getattr(cls, "manual_commands", []))
    return {
        "means": getattr(cls, "means", "") or getattr(cls, "why", ""),
        "steps": steps,
    }


def _runbook(cause: str) -> Dict:
    """Built-in runbook for ``cause``, else the probe's own (for plugins)."""
    return RUNBOOKS.get(cause) or _probe_runbook(cause)


def explain_failure(
    root: CheckResult, by_name: Dict[str, CheckResult]
) -> Tuple[str, List[str]]:
    """Build a one-line cause summary plus supporting evidence bullets."""
    means = _runbook(root.name).get("means") or root.summary
    evidence = [root.summary]
    # Cite the OK dependency that makes this the root (e.g. gateway OK -> dns is cause).
    for dep in root.depends_on:
        d = by_name.get(dep)
        if d and d.status.value == "ok":
            evidence.append(f"{d.title} is OK ({d.summary}), so the fault is above it.")
    return means, evidence


def runbook_steps(cause: str) -> List[str]:
    return _runbook(cause).get("steps", [])
