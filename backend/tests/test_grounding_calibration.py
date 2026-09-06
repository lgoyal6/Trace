"""Invariants for backend.measurement.run_grounding_calibration (C25).

The catch/miss pair is itself the negative control for the monitor: one that
fired on both shifts would prove nothing, and one that fired on neither would be
inert. Here the clean fold must NOT trip the alarm, the paraphrase shift MUST,
the harder-negatives shift must NOT while really degrading precision.

The calibration metric gets its own control: ECE has to be near zero on a
stream that is calibrated by construction and large on one that is not, or it
is not measuring anything.

    python -m pytest backend/tests/test_grounding_calibration.py -q
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.measurement import run_grounding_calibration as c


@pytest.fixture(scope="module")
def report():
    return c.main()


# ── the calibration metric itself ────────────────────────────────────────────
def test_ece_separates_a_calibrated_stream_from_a_miscalibrated_one():
    rng = np.random.default_rng(7)
    p = rng.uniform(0, 1, 20_000)
    # control: labels drawn AT the stated probability, so ECE must be ~0
    calibrated = (rng.uniform(0, 1, p.size) < p).astype(int)
    assert c.expected_calibration_error(calibrated, p) < 0.02

    # the same scores against labels drawn at half the stated probability: the
    # scores now systematically overstate, and the metric has to say so
    overconfident = (rng.uniform(0, 1, p.size) < p / 2).astype(int)
    assert c.expected_calibration_error(overconfident, p) > 0.2


def test_reliability_curve_is_monotone_enough_to_be_a_curve(report):
    cal = report["calibration"]
    assert len(cal["mean_predicted"]) == len(cal["fraction_positive"])
    assert 0.0 <= cal["ece"] <= 1.0 and 0.0 <= cal["brier"] <= 1.0
    # the top bin must be mostly grounded and the bottom bin mostly not, or the
    # score is not ordering anything and calibration is meaningless
    assert cal["fraction_positive"][0] < 0.5 < cal["fraction_positive"][-1]


# ── error slices ─────────────────────────────────────────────────────────────
def test_slices_report_an_error_rate_for_negative_classes_too():
    scores = np.array([0.9, 0.9, 0.1, 0.1, 0.9, 0.1])
    labels = np.array([1, 1, 0, 0, 0, 1])
    classes = ["pos", "pos", "neg", "neg", "neg", "pos"]
    out = c.per_class(scores, labels, classes, 0.55)

    # control: the clean half of each slice is scored, not skipped
    assert out["pos"]["n"] == 3 and out["neg"]["n"] == 3
    # a negative-only slice has no recall, but its false-grounding rate is real
    assert out["neg"]["recall"] is None
    assert out["neg"]["error_kind"] == "false_grounding"
    assert out["neg"]["errors"] == 1
    assert out["neg"]["error_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert out["pos"]["error_kind"] == "missed_grounding"
    assert out["pos"]["errors"] == 1

    worst = c.worst_slices(out)
    assert {w["class"] for w in worst} == {"pos", "neg"}


def test_a_flawless_slice_set_reports_no_worst_slice():
    # control for worst_slices: it must be empty when nothing is wrong, so a
    # non-empty list is a finding rather than an artefact of always taking k.
    scores = np.array([0.9, 0.1])
    labels = np.array([1, 0])
    assert c.worst_slices(c.per_class(scores, labels, ["a", "b"], 0.55)) == []


def test_the_real_slices_name_where_the_check_is_worst(report):
    slices = report["error_slices"]
    assert {"verbatim", "off_topic", "uncited", "same_condition"} <= set(slices)
    for s in slices.values():
        assert s["error_kind"] in ("missed_grounding", "false_grounding", "mixed")
        assert 0.0 <= s["error_rate"] <= 1.0
    # the aggregate hides them, so the report must surface them by name
    worst = report["worst_slices"]
    assert worst, "no slice carries any error, which contradicts the shift results"
    assert worst == sorted(worst, key=lambda w: -w["error_rate"])


# ── seeded shift ─────────────────────────────────────────────────────────────
def test_the_clean_fold_does_not_false_alarm(report):
    assert report["shift"]["clean_baseline"]["fires_false_alarm"] is False


def test_the_paraphrase_shift_is_caught(report):
    hp = report["shift"]["heavier_paraphrase"]
    assert hp["caught"] is True
    assert hp["grounded_rate_delta"] >= report["monitor"]["alarm_delta"]
    assert hp["recall"] < report["shift"]["clean_baseline"]["recall"] - 0.1


def test_the_harder_negatives_shift_is_missed_but_degrades_precision(report):
    hn = report["shift"]["harder_negatives"]
    assert hn["caught"] is False
    assert hn["grounded_rate_delta"] < report["monitor"]["alarm_delta"]
    assert hn["false_positives_injected"] > 0
    # a miss only counts as a miss if something actually got worse
    assert hn["precision_shifted"] < hn["precision_clean"]


def test_the_report_says_what_the_monitor_cannot_see(report):
    assert len(report["shift"]["what_the_monitor_does_not_catch"]) >= 2


def test_nothing_here_retrains_or_moves_the_threshold(report):
    assert report["shipped_threshold"] == c.SUPPORT_THRESHOLD
    assert "no retraining" in report["retraining_policy"].lower()
