"""Language selection and measurement statistics."""

import math

import pytest

from netdiag import i18n
from netdiag.measure.stats import (
    jitter,
    loss_upper_bound_pct,
    percentile,
    samples_for_loss_bound,
    summarize,
    wilson_interval,
)

# ------------------------------------------------------------------------ i18n


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"NETDIAG_LANG": "en"}, "en"),
        ({"NETDIAG_LANG": "ja", "LANG": "en_US.UTF-8"}, "ja"),
        ({"LANG": "ja_JP.UTF-8"}, "ja"),
        ({"LC_ALL": "en_GB.UTF-8", "LANG": "ja_JP.UTF-8"}, "en"),
        ({"LANG": "C.UTF-8"}, None),  # undetermined -> OS locale / Japanese default
    ],
)
def test_detect_language(env, expected, monkeypatch):
    monkeypatch.setattr(i18n.locale, "getlocale", lambda *a: (None, None))
    got = i18n.detect(env)
    assert got == (expected or i18n.DEFAULT)


def test_windows_style_locale_names_are_understood():
    assert i18n.normalize("Japanese_Japan") == "ja"
    assert i18n.normalize("English_United States") == "en"
    assert i18n.normalize("C") is None


def test_tr_and_lazy_text_follow_the_active_language():
    t = i18n.Text("ゲートウェイ", "gateway")
    i18n.set_language("ja")
    assert i18n.tr("あ", "a") == "あ"
    assert str(t) == "ゲートウェイ" and f"{t}" == "ゲートウェイ"
    i18n.set_language("en")
    assert i18n.tr("あ", "a") == "a"
    assert str(t) == "gateway"
    assert t == "gateway" and bool(t)


# ----------------------------------------------------------------------- stats


def test_wilson_interval_zero_loss_is_not_zero():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert 0.27 < hi < 0.28  # 0/10 only rules out loss above ~28 %


def test_loss_upper_bound_shrinks_with_samples():
    assert loss_upper_bound_pct(0, 0) is None
    bounds = [loss_upper_bound_pct(0, n) for n in (10, 100, 1000)]
    assert bounds[0] > bounds[1] > bounds[2]
    assert math.isclose(bounds[1], 3.7, abs_tol=0.05)


def test_samples_needed_for_one_percent():
    n = samples_for_loss_bound(1.0)
    assert loss_upper_bound_pct(0, n) <= 1.0 < loss_upper_bound_pct(0, n - 1)
    assert 370 < n < 390


def test_percentile_and_jitter():
    assert percentile([], 50) is None
    assert percentile([5], 95) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert jitter([1.0]) is None
    assert jitter([1.0, 3.0, 2.0]) == 1.5


def test_summarize_exposes_spikes_an_average_hides():
    s = summarize([0.3, 0.4, 0.35, 5.0, 0.33])
    assert s.median == 0.35
    assert s.max == 5.0
    assert s.p95 > 4.0
    assert s.count == 5
