"""Diagnosis engine: turn a set of facts into a ranked set of causes.

The engine evaluates probes in dependency order (a topological sort of the
``depends_on`` graph). The key idea: **a probe is only run after its dependencies
succeed**. If a dependency failed, the dependent is marked SKIP with reason
"upstream <dep> failed" — so its failure never competes with the real cause.

Root-cause selection then reduces to: among probes that actually FAILED while all
their dependencies were OK, the lowest one in the graph is the prime suspect.
Everything above it is a *consequence*, folded out of the headline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

from ..core import CheckResult, Context, Probe, Status, all_probes, get_probe


@dataclass
class Hypothesis:
    cause: str  # probe name that is the suspected root
    confidence: str  # "high" | "medium" | "low"
    summary: str  # human explanation
    evidence: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)  # failures explained by this


@dataclass
class Diagnosis:
    results: List[CheckResult]
    hypotheses: List[Hypothesis]
    healthy: bool
    reasoning: List[str] = field(default_factory=list)  # step-by-step (verbose mode)

    @property
    def primary(self) -> Optional[Hypothesis]:
        return self.hypotheses[0] if self.hypotheses else None

    def results_by_name(self) -> Dict[str, CheckResult]:
        return {r.name: r for r in self.results}


def _topo_order(names: List[str]) -> List[str]:
    """Topologically sort probe names by their declared dependencies.

    Only dependencies that are themselves in *names* are considered, so a partial
    selection (e.g. ``--only dns``) still orders correctly. Cycles (shouldn't
    happen) fall back to declaration order for the unresolved remainder.
    """
    selected = set(names)
    deps: Dict[str, List[str]] = {}
    for n in names:
        cls = get_probe(n)
        d = [x for x in (cls.depends_on if cls else []) if x in selected]
        deps[n] = d

    ordered: List[str] = []
    placed = set()
    # Repeatedly place any node whose deps are all placed.
    progress = True
    while progress and len(ordered) < len(names):
        progress = False
        for n in names:
            if n in placed:
                continue
            if all(d in placed for d in deps[n]):
                ordered.append(n)
                placed.add(n)
                progress = True
    # Any leftover (cycle) appended in original order.
    for n in names:
        if n not in placed:
            ordered.append(n)
    return ordered


class Engine:
    """Runs a selection of probes and produces a :class:`Diagnosis`."""

    def __init__(self, config: Optional[Dict] = None, verbose: bool = False) -> None:
        self.config = config or {}
        self.verbose = verbose

    def select(
        self, only: Optional[List[str]] = None, skip: Optional[List[str]] = None
    ) -> List[str]:
        names = list(all_probes())
        # Opt-in probes, excluded from the default sweep:
        #   icmp       — ad-hoc single-target ping (use `netdiag ping`)
        #   traceroute — slow, and noisy against hosts that filter it; most useful
        #                on demand or when reachability already failed.
        default_excluded = {"icmp", "traceroute"}
        if not only:
            names = [n for n in names if n not in default_excluded]
        if only:
            names = [n for n in names if n in set(only)]
        if skip:
            names = [n for n in names if n not in set(skip)]
        return names

    def run(
        self, only: Optional[List[str]] = None, skip: Optional[List[str]] = None
    ) -> Diagnosis:
        ctx = Context(dict(self.config))
        names = self.select(only=only, skip=skip)
        order = _topo_order(names)

        results: List[CheckResult] = []
        reasoning: List[str] = []

        for name in order:
            cls: Optional[Type[Probe]] = get_probe(name)
            if cls is None:
                continue
            probe = cls()

            # Gate on dependencies: if a hard dependency FAILED, skip this probe
            # and record it as a consequence rather than re-measuring a symptom.
            failed_deps = [
                d
                for d in probe.depends_on
                if d in ctx.results and ctx.results[d].status == Status.FAIL
            ]
            if failed_deps:
                res = CheckResult.skip(
                    name,
                    f"not evaluated: upstream {', '.join(failed_deps)} failed",
                    depends_on=list(probe.depends_on),
                )
                res.title = cls.title or name
                res.layer = cls.layer
                ctx.results[name] = res
                results.append(res)
                if self.verbose:
                    reasoning.append(
                        f"skip {name}: its dependency {failed_deps[0]} already failed, "
                        f"so any failure here would be a symptom."
                    )
                continue

            res = probe.run(ctx)
            ctx.results[name] = res
            results.append(res)
            if self.verbose:
                reasoning.append(
                    f"{res.status.symbol} {name} ({cls.layer}): {res.summary}"
                )

        hypotheses = self._diagnose(results, reasoning)
        healthy = not any(r.status == Status.FAIL for r in results) and not hypotheses
        return Diagnosis(
            results=results, hypotheses=hypotheses, healthy=healthy, reasoning=reasoning
        )

    # -- diagnosis logic ------------------------------------------------------
    def _diagnose(
        self, results: List[CheckResult], reasoning: List[str]
    ) -> List[Hypothesis]:
        by_name = {r.name: r for r in results}
        failed = [r for r in results if r.status == Status.FAIL]
        if not failed:
            # No hard failure: surface warnings as low-confidence advisories.
            warns = [r for r in results if r.status == Status.WARN]
            return [
                Hypothesis(
                    cause=w.name,
                    confidence="low",
                    summary=f"{w.title}: {w.summary}",
                    evidence=[w.summary],
                )
                for w in warns
            ]

        # A failure is a *root* if none of its dependencies failed.
        roots: List[CheckResult] = []
        for r in failed:
            dep_failed = any(
                by_name.get(d) and by_name[d].status == Status.FAIL
                for d in r.depends_on
            )
            if not dep_failed:
                roots.append(r)

        if not roots:
            roots = failed  # degenerate; shouldn't happen with acyclic graph

        # Order roots by graph depth (lower layer first) so the most fundamental
        # cause leads. Use the static depends_on chain length as a depth proxy.
        roots.sort(key=lambda r: self._depth(r.name))

        hypotheses: List[Hypothesis] = []
        explained: set = set()
        for root in roots:
            consequences = self._downstream_failures(root.name, by_name)
            explained.update(consequences)
            conf = "high" if len(roots) == 1 else "medium"
            from .rules import explain_failure

            summary, evidence = explain_failure(root, by_name)
            hypotheses.append(
                Hypothesis(
                    cause=root.name,
                    confidence=conf,
                    summary=summary,
                    evidence=evidence,
                    consequences=[c for c in consequences if c != root.name],
                )
            )
            if self.verbose:
                reasoning.append(
                    f"root cause candidate: {root.name} failed while its "
                    f"dependencies were OK; it explains "
                    f"{len(consequences)} downstream failure(s)."
                )
        return hypotheses

    @staticmethod
    def _depth(name: str) -> int:
        seen = set()

        def d(n: str) -> int:
            if n in seen:
                return 0
            seen.add(n)
            cls = get_probe(n)
            if not cls or not cls.depends_on:
                return 0
            return 1 + max((d(x) for x in cls.depends_on), default=0)

        return d(name)

    @staticmethod
    def _downstream_failures(root: str, by_name: Dict[str, CheckResult]) -> List[str]:
        """All FAILED/SKIPPED probes that (transitively) depend on *root*."""
        out = [root]
        changed = True
        while changed:
            changed = False
            for name, res in by_name.items():
                if name in out:
                    continue
                if res.status in (Status.FAIL, Status.SKIP) and any(
                    d in out for d in res.depends_on
                ):
                    out.append(name)
                    changed = True
        return out
