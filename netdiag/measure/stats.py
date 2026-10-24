"""Small, dependency-free statistics for measurement results.

Two ideas make a short field test trustworthy:

* **Loss needs a confidence interval.** "0 lost out of 10" does not mean the
  link is loss-free — it only rules out loss above ~28 %. The Wilson score
  interval states that honestly, so the report can say "0 % (95 % upper bound
  28 %)" and tell the user to measure longer when intermittent loss matters.
* **Latency needs more than an average.** One slow reply hides in a mean;
  median, p95, max and jitter (variation between consecutive replies) expose
  the spikes that make PLC communication time out.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Sequence, Tuple

Z_95 = 1.959963984540054  # two-sided 95 %


def wilson_interval(successes: int, n: int, z: float = Z_95) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion, as fractions (0..1).

    Well-behaved at 0 and n (unlike the normal approximation), which is exactly
    the "no loss observed" case a field engineer cares about.
    """
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def loss_upper_bound_pct(lost: int, sent: int) -> Optional[float]:
    """95 % upper bound of the loss rate in percent (None when nothing was sent)."""
    if sent <= 0:
        return None
    return wilson_interval(lost, sent)[1] * 100.0


def samples_for_loss_bound(target_pct: float) -> int:
    """Loss-free samples needed before the 95 % upper bound drops below target.

    Used to tell the user how long to measure, e.g. ~380 pings to show loss
    < 1 % with 95 % confidence.
    """
    if target_pct <= 0:
        raise ValueError("target_pct must be positive")
    n = 1
    while loss_upper_bound_pct(0, n) > target_pct:
        n = n * 2 if n < 64 else n + max(1, n // 8)
    lo, hi = n // 2 if n > 1 else 1, n
    while lo < hi:  # tighten to the smallest n
        mid = (lo + hi) // 2
        if loss_upper_bound_pct(0, mid) <= target_pct:
            hi = mid
        else:
            lo = mid + 1
    return lo


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (``pct`` in 0..100); None for no data."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    k = (len(data) - 1) * (pct / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return data[int(k)]
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def jitter(values: Sequence[float]) -> Optional[float]:
    """Mean absolute difference between consecutive samples (RFC 3550 style).

    Captures delay *variation* in arrival order, which is what disturbs cyclic
    industrial traffic, independent of the absolute latency.
    """
    data = [v for v in values if v is not None]
    if len(data) < 2:
        return None
    return statistics.fmean(abs(b - a) for a, b in zip(data, data[1:]))


@dataclass
class RttSummary:
    count: int
    min: float
    avg: float
    median: float
    p95: float
    max: float
    stdev: float
    jitter: float

    def to_dict(self) -> Dict[str, float]:
        return {k: round(v, 3) for k, v in asdict(self).items()}


def summarize(values: Sequence[float]) -> Optional[RttSummary]:
    data = [float(v) for v in values if v is not None]
    if not data:
        return None
    return RttSummary(
        count=len(data),
        min=min(data),
        avg=statistics.fmean(data),
        median=statistics.median(data),
        p95=percentile(data, 95.0) or data[0],
        max=max(data),
        stdev=statistics.pstdev(data) if len(data) > 1 else 0.0,
        jitter=jitter(data) or 0.0,
    )
