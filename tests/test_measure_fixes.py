"""Regression tests for the measurement layer: statistics, DNS, TCP and ping."""

from netdiag.measure.stats import summarize


def test_p95_of_zero_rtts_is_zero_not_the_first_sample():
    assert summarize([7.0] + [0.0] * 99).p95 == 0.0
