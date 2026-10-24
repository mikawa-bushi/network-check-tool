"""Learn a per-target 'normal' from history and flag deviations.

Absolute thresholds ('>100ms is bad') are wrong everywhere at once: 100ms is
fine for a satellite link and terrible for a LAN. Instead we learn each target's
own median RTT and dispersion (MAD) and judge new samples relative to that.

Uses the median and Median Absolute Deviation (robust to outliers) computed with
the standard library only — no numpy/pandas.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass
class Baseline:
    median: float
    mad: float
    n: int

    def deviation(self, value: float) -> float:
        """How many robust sigmas *value* sits from the median (signed)."""
        if self.mad == 0:
            return 0.0 if value == self.median else float("inf")
        # 1.4826 scales MAD to be a consistent estimator of stddev for normals.
        return (value - self.median) / (1.4826 * self.mad)

    def is_anomalous(self, value: float, threshold: float = 4.0) -> bool:
        return abs(self.deviation(value)) >= threshold


def compute_baseline(values: Sequence[float]) -> Optional[Baseline]:
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return None
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    return Baseline(median=med, mad=mad, n=len(vals))


def classify(value: float, baseline: Optional[Baseline]) -> str:
    """Return 'normal' | 'elevated' | 'anomalous' | 'unknown'."""
    if baseline is None:
        return "unknown"
    dev = baseline.deviation(value)
    if dev >= 6:
        return "anomalous"
    if dev >= 3:
        return "elevated"
    return "normal"
