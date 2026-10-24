"""Find *when* a metric changed — the single most useful fact in triage.

'It broke at 14:23' plus the snapshot diff usually identifies the cause faster
than any amount of live probing. We use a lightweight mean-shift scan (a poor
man's CUSUM/Pettitt) over a time-ordered series: split at each point, compare the
means of the two halves, and report the split with the largest standardised gap.

Also detects flapping (rapid alternation) so a stuttering link is reported
differently from a clean break.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


@dataclass
class ChangePoint:
    index: int
    timestamp: float
    before_mean: float
    after_mean: float
    magnitude: float  # standardised difference of means

    @property
    def worsened(self) -> bool:
        return self.after_mean > self.before_mean


def detect_changepoint(
    series: Sequence[Tuple[float, float]],
    min_segment: int = 5,
    threshold: float = 2.0,
) -> Optional[ChangePoint]:
    """*series* is [(timestamp, value), ...] in time order.

    Returns the most significant mean-shift, or None if nothing crosses
    *threshold* standardised units.
    """
    pts = [(t, v) for t, v in series if v is not None]
    if len(pts) < 2 * min_segment:
        return None
    values = [v for _, v in pts]
    overall_sd = statistics.pstdev(values) or 1e-9

    best: Optional[ChangePoint] = None
    for i in range(min_segment, len(pts) - min_segment):
        left = values[:i]
        right = values[i:]
        lm = statistics.fmean(left)
        rm = statistics.fmean(right)
        mag = abs(rm - lm) / overall_sd
        if best is None or mag > best.magnitude:
            best = ChangePoint(
                index=i,
                timestamp=pts[i][0],
                before_mean=lm,
                after_mean=rm,
                magnitude=mag,
            )
    if best and best.magnitude >= threshold:
        return best
    return None


def detect_flapping(
    series: Sequence[Tuple[float, float]],
    transitions_threshold: int = 4,
) -> Optional[float]:
    """Detect alternation between 'good' and 'bad' states (e.g. loss 0%/100%).

    Returns the transition rate (transitions per sample) if it exceeds the
    threshold count, else None. Used to distinguish a flapping link from a clean
    outage.
    """
    vals = [v for _, v in series if v is not None]
    if len(vals) < 6:
        return None
    med = statistics.median(vals)
    states = [v > med for v in vals]
    transitions = sum(1 for a, b in zip(states, states[1:]) if a != b)
    if transitions >= transitions_threshold:
        return transitions / len(vals)
    return None
