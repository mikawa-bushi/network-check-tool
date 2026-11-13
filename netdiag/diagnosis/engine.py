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

import difflib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, Type

from ..core import (
    CheckResult,
    Context,
    InputError,
    Probe,
    Status,
    all_probes,
    get_probe,
)
from ..i18n import tr

# Upper bound on probe concurrency. Active probes (gateway/external/dualstack)
# spawn subprocess pings; running too many at once would have them contend for
# the link and skew each other's timings. 3-4 is the doc's recommended ceiling.
MAX_PARALLEL_JOBS = 4


@dataclass
class Hypothesis:
    cause: str  # probe name that is the suspected root
    confidence: str  # "high" | "medium" | "low"
    summary: str  # human explanation
    evidence: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)  # failures explained by this
    runbook: List[str] = field(
        default_factory=list
    )  # next steps, built from the result
    title: str = ""  # the cause probe's display title
    code: str = ""  # the cause result's stable outcome code

    @property
    def is_fault(self) -> bool:
        return self.confidence != "low"


@dataclass
class Diagnosis:
    """The outcome of one run: every result, the ranked causes, the verdict.

    ``healthy`` means no failure and no suspected cause was found. It says
    nothing about whether everything was measured; ``status`` is the verdict.
    """

    results: List[CheckResult]
    hypotheses: List[Hypothesis]
    healthy: bool
    reasoning: List[str] = field(default_factory=list)  # step-by-step (verbose mode)

    @property
    def status(self) -> Status:
        """FAIL > UNKNOWN > WARN > OK; UNKNOWN when no check produced a verdict."""
        if any(h.is_fault for h in self.hypotheses):
            return Status.FAIL
        found = [r.status for r in self.results]
        if self.hypotheses:
            found.append(Status.WARN)
        return Status.worst(found)

    @property
    def primary(self) -> Optional[Hypothesis]:
        return self.hypotheses[0] if self.hypotheses else None

    def results_by_name(self) -> Dict[str, CheckResult]:
        return {r.name: r for r in self.results}


def _with_suggestion(name: str, valid: List[str]) -> str:
    close = difflib.get_close_matches(name, valid, n=1)
    if not close:
        return name
    return tr(
        f"{name}（{close[0]} のことですか？）", f"{name} (did you mean {close[0]}?)"
    )


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


def _dependency_tiers(order: List[str], names: List[str]) -> List[List[str]]:
    """Group probe names into dependency tiers for parallel execution.

    Dependencies are read from each probe's registered ``depends_on`` (via the
    global registry), restricted to names in ``names`` — not derived from the
    arguments; an unregistered name is treated as having no dependencies.

    A probe's tier (level) is ``0`` when it has no selected dependency, otherwise
    ``1 + max(level of its selected dependencies)``. Every dependency therefore
    sits in a strictly earlier tier, so the tiers can be run one after another
    while a whole tier executes concurrently. Probes are leveled in ``order`` (a
    topological sort) so each dependency's level is known first; a cycle leftover
    (no leveled deps) falls back to level ``0``. Within a tier, names keep their
    ``order`` position so output stays deterministic.
    """
    selected = set(names)
    level: Dict[str, int] = {}
    for n in order:
        cls = get_probe(n)
        deps = [x for x in (cls.depends_on if cls else []) if x in selected]
        known = [level[d] for d in deps if d in level]
        level[n] = 1 + max(known) if known else 0

    max_level = max(level.values(), default=0)
    tiers: List[List[str]] = []
    for lvl in range(max_level + 1):
        tier = [n for n in order if level.get(n) == lvl]
        if tier:
            tiers.append(tier)
    return tiers


class Engine:
    """Runs a selection of probes and produces a :class:`Diagnosis`."""

    def __init__(self, config: Optional[Dict] = None, verbose: bool = False) -> None:
        self.config = config or {}
        self.verbose = verbose

    def select(
        self, only: Optional[List[str]] = None, skip: Optional[List[str]] = None
    ) -> List[str]:
        """Probe names to run: ``only`` if given, else the default host sweep.

        The default sweep is every ``host``-scoped probe that is not opt-in
        (``default = False``, e.g. traceroute) and, unless the config says the
        network should reach the internet (``internet``, default True for library
        use), none of the internet-only probes. Target-scoped probes need a
        device and only run when named explicitly (see :mod:`netdiag.runner`).

        Raises :class:`InputError` for a name that is not a check, or for a
        device check when the config has no device (``target``).
        """
        probes = all_probes()
        self._reject_unusable_names(probes, list(only or []) + list(skip or []))
        if only:
            wanted = set(only)
            names = [n for n in probes if n in wanted]
        else:
            internet = bool(self.config.get("internet", True))
            names = [
                n
                for n, cls in probes.items()
                if getattr(cls, "scope", "host") == "host"
                and getattr(cls, "default", True)
                and (internet or not getattr(cls, "requires_internet", False))
            ]
        if skip:
            names = [n for n in names if n not in set(skip)]
        return names

    def _reject_unusable_names(
        self, probes: Dict[str, Type[Probe]], names: List[str]
    ) -> None:
        has_target = bool(self.config.get("target"))
        usable = [
            n
            for n, cls in probes.items()
            if has_target or getattr(cls, "scope", "host") == "host"
        ]
        unknown = [n for n in dict.fromkeys(names) if n not in probes]
        device = [n for n in dict.fromkeys(names) if n in probes and n not in usable]
        problems: List[str] = []
        if unknown:
            problems.append(
                tr("不明なチェック名: ", "unknown check name: ")
                + ", ".join(_with_suggestion(n, usable) for n in unknown)
                + tr(
                    f"。使えるチェック名: {', '.join(sorted(usable))}",
                    f". Valid names: {', '.join(sorted(usable))}",
                )
            )
        if device:
            names_text = ", ".join(device)
            problems.append(
                tr(
                    f"{names_text} は機器ごとのチェックです。"
                    "機器を指定すると機器ごとに自動で実行されます",
                    f"{names_text}: device checks, which run automatically "
                    "for every device you check",
                )
            )
        if problems:
            raise InputError(tr("。", ". ").join(problems) + tr("。", "."))

    def _evaluate_probe(
        self, name: str, ctx: Context
    ) -> Tuple[Optional[CheckResult], Optional[str]]:
        """Evaluate one probe against ``ctx`` and return ``(result, reasoning)``.

        Applies the dependency gate (a probe whose hard dependency already FAILED
        is skipped as a consequence rather than re-measuring a symptom), otherwise
        runs the probe. ``reasoning`` is the verbose line for this probe, or
        ``None`` when not in verbose mode. Returns ``(None, None)`` for an unknown
        probe name. Reads ``ctx`` but never writes ``ctx.results`` — the caller
        records the result, which lets a tier of independent probes be evaluated
        concurrently and then committed together.
        """
        cls: Optional[Type[Probe]] = get_probe(name)
        if cls is None:
            return None, None
        probe = cls()

        # A dependency that FAILED, or that was itself skipped because *its*
        # dependency failed, gates this probe: either way any failure here would
        # be a symptom of the same root cause (transitively down the chain).
        failed_deps = [
            d
            for d in probe.depends_on
            if d in ctx.results
            and (
                ctx.results[d].status == Status.FAIL
                or ctx.results[d].code == f"{d}.not_evaluated"
            )
        ]
        if failed_deps:
            res = CheckResult.skip(
                name,
                tr(
                    f"未評価: 前提の {', '.join(failed_deps)} が失敗",
                    f"not evaluated: upstream {', '.join(failed_deps)} failed",
                ),
                depends_on=list(probe.depends_on),
                code=f"{name}.not_evaluated",
            )
            res.title = str(cls.title or name)
            res.layer = cls.layer
            reason = (
                f"skip {name}: its dependency {failed_deps[0]} already failed, "
                f"so any failure here would be a symptom."
                if self.verbose
                else None
            )
            return res, reason

        res = probe.run(ctx)
        reason = (
            f"{res.status.symbol} {name} ({cls.layer}): {res.summary}"
            if self.verbose
            else None
        )
        return res, reason

    def run(
        self,
        only: Optional[List[str]] = None,
        skip: Optional[List[str]] = None,
        jobs: int = 1,
        on_probe: Optional[Callable[[str], None]] = None,
    ) -> Diagnosis:
        """Run the selected probes and diagnose.

        ``on_probe(name)``, if given, is called in the main thread just before each
        probe is dispatched -- a hook for CLI progress feedback. It never runs
        inside a worker thread, so it can safely write to a shared stream.
        """
        ctx = Context(dict(self.config))
        names = self.select(only=only, skip=skip)
        order = _topo_order(names)
        # Clamp: <=1 (or None) is serial; never exceed the active-probe ceiling.
        jobs = max(1, min(int(jobs or 1), MAX_PARALLEL_JOBS))

        collected: Dict[str, Tuple[CheckResult, Optional[str]]] = {}

        tiers = [[n] for n in order] if jobs == 1 else _dependency_tiers(order, names)
        for tier in tiers:
            if on_probe is not None:
                for name in tier:
                    on_probe(name)
            if len(tier) == 1:
                name = tier[0]
                tier_out = {name: self._evaluate_probe(name, ctx)}
            else:
                with ThreadPoolExecutor(max_workers=min(jobs, len(tier))) as pool:
                    tier_out = dict(
                        zip(
                            tier,
                            pool.map(lambda n: self._evaluate_probe(n, ctx), tier),
                        )
                    )
            for name in tier:
                res, reason = tier_out[name]
                if res is not None:
                    ctx.results[name] = res
                    collected[name] = (res, reason)

        # Emit in topological order regardless of completion order, so the
        # Diagnosis is identical to a serial run for any value of ``jobs``.
        results: List[CheckResult] = []
        reasoning: List[str] = []
        for name in order:
            if name in collected:
                res, reason = collected[name]
                results.append(res)
                if reason is not None:
                    reasoning.append(reason)

        return self.diagnose(results, reasoning)

    def diagnose(
        self, results: List[CheckResult], reasoning: Optional[List[str]] = None
    ) -> Diagnosis:
        """Build a :class:`Diagnosis` from already-collected results.

        Public so callers that add evidence after a run (e.g. NIC error counters
        sampled over a whole device sweep) can re-derive the verdict.
        """
        reasoning = list(reasoning or [])
        hypotheses = self._diagnose(results, reasoning)
        healthy = not any(r.status == Status.FAIL for r in results) and not hypotheses
        return Diagnosis(
            results=results, hypotheses=hypotheses, healthy=healthy, reasoning=reasoning
        )

    # -- diagnosis logic ------------------------------------------------------
    def _diagnose(
        self, results: List[CheckResult], reasoning: List[str]
    ) -> List[Hypothesis]:
        from .rules import advice_for, explain_failure

        by_name = {r.name: r for r in results}
        failed = [r for r in results if r.status == Status.FAIL]
        if not failed:
            # No hard failure: surface warnings as low-confidence advisories.
            warns = [r for r in results if r.status == Status.WARN]
            out = []
            for w in warns:
                means, steps = advice_for(w, by_name)
                out.append(
                    Hypothesis(
                        cause=w.name,
                        confidence="low",
                        summary=means or f"{w.title}: {w.summary}",
                        evidence=[w.summary],
                        runbook=steps,
                        title=str(w.title),
                        code=w.code,
                    )
                )
            return out

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
            summary, evidence = explain_failure(root, by_name)
            hypotheses.append(
                Hypothesis(
                    cause=root.name,
                    confidence=conf,
                    summary=summary,
                    evidence=evidence,
                    consequences=[c for c in consequences if c != root.name],
                    runbook=advice_for(root, by_name)[1],
                    title=str(root.title),
                    code=root.code,
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
