# Plan: issue #20 — parallelize independent diagnostic probes (P2)

## Goal (verbatim from issue #20)

> `netdiag` diagnostic checks finish noticeably faster by running mutually-independent probes
> concurrently instead of strictly one-at-a-time, while keeping results identical and
> deterministic. (Improvements doc item **P2**.)

## Key invariant

Parallelism must change **only wall-clock time**, never output. For any `jobs` value the
`Diagnosis` (the ordered `results`, the `reasoning`, the `hypotheses`, the `healthy` flag) must be
identical to today's serial run. This is achievable because probes are read-only against the
shared `Context` (`core/probe.py`: probes read `ctx.config` and `ctx.results` for *already-run*
dependencies; the engine writes `ctx.results[name]` only **after** `probe.run` returns), and
probes within one dependency tier are independent by construction.

## Approach

In `netdiag/diagnosis/engine.py`:

1. **Factor the per-probe step** into `_evaluate_probe(name, ctx) -> (CheckResult | None, reason | None)`:
   instantiate the probe, apply the failed-hard-dependency gate (skip-as-consequence), otherwise
   `probe.run(ctx)`. It does **not** write `ctx.results` — the caller does. This is the exact body
   of today's loop, lifted verbatim, so the serial path is behaviour-preserving.

2. **Compute execution tiers** with `_dependency_tiers(order, names)`: assign each probe a level
   (`0` if it has no selected dependency, else `1 + max(level of its selected deps)`), iterating in
   `_topo_order` so a dependency's level is known before its dependents'; cycle leftovers fall back
   to level 0. Group probes by level, ordered within a tier by their `_topo_order` index. Every
   dependency is therefore in a strictly earlier tier.

3. **`Engine.run(self, only=None, skip=None, jobs=1)`**:
   - Clamp `jobs` to `[1, MAX_PARALLEL_JOBS]` (`MAX_PARALLEL_JOBS = 4`, so active ping probes can't
     saturate the link; `jobs<=1`/`None` → serial).
   - `jobs == 1`: the existing serial loop over `_topo_order`, now calling `_evaluate_probe`.
   - `jobs > 1`: process tiers in level order; run each tier's probes on a
     `ThreadPoolExecutor(max_workers=min(jobs, len(tier)))`; **write the whole tier's results to
     `ctx.results` before starting the next tier** (so the gate and probe reads always see complete
     earlier tiers). Probe exceptions can't escape — `Probe.run` already converts them to SKIP.
   - **Emit `results`/`reasoning` in `_topo_order` index order** regardless of completion order, so
     output is identical to serial.

4. **CLI**: add `--jobs N` (default `1`) to `netdiag check` (`cli.py:138` `check`), forwarded as
   `engine.run(..., jobs=jobs)`. Help text recommends 3–4. No other `Engine.run` caller changes —
   `scheduler.py`, `watch.py`, and the `replay` command keep the serial default.

## Alternatives considered / rejected

- **`ThreadPoolExecutor(jobs)` over the whole topo order at once** (one global pool, futures gated
  on dependency futures): rejected — more complex to keep the failed-dep skip and deterministic
  ordering correct; tier-by-tier is simpler and the tiers are coarse enough that the wasted
  idle-between-tiers time is negligible for this probe set.
- **`jobs>1` as the default**: rejected — the doc keeps serial the default to preserve measurement
  purity (active probes timing each other out under contention). Opt-in via `--jobs`.
- **No upper cap on `jobs`**: rejected — capping at 4 protects the link from active-probe
  contention (the doc's explicit guidance) while still giving the full speedup for this probe set.

## Out of scope

- The `network_check_tool` Flask scheduler pool (already right-sized in #18 / P3).
- Adding/removing/altering individual probes or their `depends_on` graph.

## Testing (acceptance scenarios → tests)

`tests/test_engine_parallel.py`, reusing the `isolated_registry` + `_make_probe` pattern from
`tests/test_engine.py` (extended with an optional `sleep`):

- **Speedup**: three independent probes that each sleep ~0.15 s run in well under the serial sum
  with `jobs=4`; serial (`jobs=1`) takes ~the sum.
- **Identical output**: a small dep graph (`a`; `b,c` ← `a`; `d` ← `b,c`) yields the same ordered
  `(name, status)` list and same `reasoning` for `jobs=1` and `jobs=4`.
- **Dependencies respected**: a dependent probe sees its dependency's result in `ctx` when it runs
  (asserts inside `check`; would degrade to SKIP otherwise) under `jobs=4`.
- **Failed-dep skip preserved**: with a FAILED dependency, the dependent is SKIPped with the
  "upstream … failed" summary under `jobs=4`, same as serial.
- **Clamping**: `jobs=0`/negative behaves as serial (no crash) and still produces correct results.
- **CLI**: `netdiag check --help` lists `--jobs`; invoking with `--jobs` is accepted.
