"""analysis-plan 6.1 as code: which headline the ablation licenses (C2).

The frozen rule reads "separated: Headline A; not separated: Headline B". The second half is
the classic error of reading an interval that includes zero as evidence of no effect: with
three repeats almost everything is "not separated", so B would win by default whenever the
campaign was too small to tell. An equivalence claim needs a margin fixed before the data,
and a third outcome for an interval that shows neither.

The statistic is the paired ratio of `wjsq` to `jsq_fastfirst` on mean end-to-end latency, in
the ablation's baseline arm (`cap157`), with its 95% interval [lo, hi]. With margin d:

    A             hi < 1 - d          calibrated magnitude buys more than d, shown
    B             1 - d <= lo and     whatever magnitude buys or costs is within d, shown
                  hi <= 1 + d
    reversed      lo > 1 + d          magnitude costs more than d, shown
    inconclusive  anything else       the interval crosses a margin

A separated gain smaller than the margin is B, not A. Headline A says the router "gains X%
of its mean latency from calibrated capability", and a gain inside the margin is the thing
Headline B's "adds no more than X%" was written to cover.

d is 0.05. Every queue-aware calibration gain measured on the first pair is 10% or more
(WJSQ/JSQ between 0.738 and 0.900), so an effect under 5% is less than half the smallest one
we have seen, and a 5% band is wide enough for three seeded repeats to land inside it; a
margin the design cannot reach turns every outcome into "inconclusive".

Written before the code. The interface:

    campaign_summary.EQUIVALENCE_MARGIN = 0.05
    campaign_summary.headline_verdict(lo, hi, margin=EQUIVALENCE_MARGIN) -> str
    point["headline_6_1"] = {"arm", "control", "ratio", "ratio_ci95", "margin", "outcome"}
        present at a point holding either cell, and {"status": "undefined: ..."} when the
        pair cannot be compared, for the same reasons any contrast is undefined
    markdown: "Headline (analysis-plan 6.1): **<outcome>**"
"""

from __future__ import annotations

import campaign_summary as cs
import numpy as np
import pytest
from support import cell_rows, frame, manifests_for

ARM, CONTROL = "wjsq@cap157", "jsq_fastfirst@cap157"
BOOT = 300


def _noise(n: int, seed: int, loc: float = 1000.0, scale: float = 100.0) -> np.ndarray:
    return np.random.default_rng(seed).normal(loc, scale, n).clip(50.0)


def _point(tmp_path, arm_values=None, control_values=None, extra=()):
    runs = [*extra]
    for repeat in (1, 2, 3):
        base = _noise(40, repeat)
        if control_values is not None:
            runs.append(
                cell_rows("jsq_fastfirst", control_values(base), repeat=repeat, arm="cap157")
            )
        if arm_values is not None:
            runs.append(cell_rows("wjsq", arm_values(base), repeat=repeat, arm="cap157"))
    data = frame(*runs)
    return cs.summarise(data, BOOT, 5, manifests_for(tmp_path, data))["points"][0]


# ---------------------------------------------------------------------------- the rule


def test_the_margin_is_five_percent() -> None:
    assert cs.EQUIVALENCE_MARGIN == 0.05


@pytest.mark.parametrize(
    ("lo", "hi", "outcome"),
    [
        (0.80, 0.90, "A"),
        (0.80, 0.9499, "A"),
        (0.97, 0.99, "B"),  # separated, but inside the margin: B, not A
        (0.96, 1.04, "B"),
        (0.95, 1.05, "B"),  # both edges belong to B
        (1.01, 1.03, "B"),  # separated the other way, still inside
        (1.06, 1.20, "reversed"),
        (0.93, 0.97, "inconclusive"),  # crosses the lower margin
        (1.02, 1.08, "inconclusive"),  # crosses the upper margin
        (0.90, 1.10, "inconclusive"),  # too wide to say anything
        (0.94, 0.95, "inconclusive"),  # touches 1 - d from below: not shown either way
    ],
)
def test_the_verdict_on_an_interval(lo, hi, outcome) -> None:
    assert cs.headline_verdict(lo, hi) == outcome


def test_the_margin_is_a_parameter_of_the_verdict() -> None:
    assert cs.headline_verdict(0.95, 0.98, margin=0.01) == "A"
    assert cs.headline_verdict(0.97, 0.99, margin=0.01) == "inconclusive"
    assert cs.headline_verdict(0.93, 0.97, margin=0.10) == "B"


def test_an_interval_that_is_not_an_interval_is_refused() -> None:
    with pytest.raises(ValueError):
        cs.headline_verdict(float("nan"), 0.9)
    with pytest.raises(ValueError):
        cs.headline_verdict(1.1, 0.9)


# ------------------------------------------------------------------- in the summary


def test_a_clear_gain_from_magnitude_is_headline_a(tmp_path) -> None:
    h = _point(tmp_path, arm_values=lambda b: 0.9 * b, control_values=lambda b: b)["headline_6_1"]
    assert (h["arm"], h["control"]) == (ARM, CONTROL)
    assert h["ratio"] == 0.9
    assert h["ratio_ci95"] == [0.9, 0.9]
    assert h["margin"] == 0.05
    assert h["outcome"] == "A"


def test_a_small_separated_gain_is_headline_b(tmp_path) -> None:
    h = _point(tmp_path, arm_values=lambda b: 0.98 * b, control_values=lambda b: b)["headline_6_1"]
    assert h["ratio_ci95"] == [0.98, 0.98]
    assert h["outcome"] == "B"


def test_magnitude_that_costs_latency_is_reported_as_reversed(tmp_path) -> None:
    h = _point(tmp_path, arm_values=lambda b: 1.2 * b, control_values=lambda b: b)["headline_6_1"]
    assert h["outcome"] == "reversed"


def test_a_noisy_effect_on_the_margin_is_inconclusive_and_agrees_with_the_rule(tmp_path) -> None:
    """The ratio sits on 1 - d exactly, with noise around it that averages to zero inside
    each repeat, so the interval has to cross the margin whichever stream draws it."""
    noise = {}
    for r in (1, 2, 3):
        z = np.random.default_rng(100 + r).normal(0.0, 60.0, 40)
        noise[r] = z - z.mean()
    repeats = iter([1, 2, 3])

    h = _point(
        tmp_path, arm_values=lambda b: 0.95 * b + noise[next(repeats)], control_values=lambda b: b
    )["headline_6_1"]
    lo, hi = h["ratio_ci95"]
    assert h["ratio"] == 0.95
    assert lo < 0.95 < hi, "the fixture has to cross the lower margin"
    assert h["outcome"] == cs.headline_verdict(lo, hi) == "inconclusive"


def test_a_transient_control_leaves_the_headline_undefined(tmp_path) -> None:
    h = _point(
        tmp_path,
        arm_values=lambda b: b,
        control_values=lambda b: np.linspace(800.0, 2400.0, b.size),
    )["headline_6_1"]
    assert h["status"].startswith("undefined")
    assert CONTROL in h["status"]
    assert "outcome" not in h


def test_a_missing_half_of_the_pair_is_undefined_rather_than_absent(tmp_path) -> None:
    """In the ablation, a lost wjsq run must show up as a headline that could not be
    decided, not as a point that never had one."""
    h = _point(tmp_path, control_values=lambda b: b)["headline_6_1"]
    assert h["status"] == f"undefined: no valid run for {ARM}"


def test_a_campaign_without_the_pair_has_no_headline(tmp_path) -> None:
    data = frame(cell_rows("jsq", _noise(40, 1)), cell_rows("wjsq", _noise(40, 2)))
    point = cs.summarise(data, 50, 1, manifests_for(tmp_path, data))["points"][0]
    assert "headline_6_1" not in point


def test_other_arms_do_not_stand_in_for_the_baseline(tmp_path) -> None:
    """wjsq told a ratio of 2.5 is a point on the curve, not the calibrated router."""
    extra = [cell_rows("wjsq", 0.5 * _noise(40, r), repeat=r, arm="cap250") for r in (1, 2, 3)]
    h = _point(tmp_path, control_values=lambda b: b, extra=extra)["headline_6_1"]
    assert h["status"] == f"undefined: no valid run for {ARM}"


def test_the_markdown_states_the_headline(tmp_path) -> None:
    runs = []
    for repeat in (1, 2, 3):
        base = _noise(40, repeat)
        runs.append(cell_rows("jsq_fastfirst", base, repeat=repeat, arm="cap157"))
        runs.append(cell_rows("wjsq", 0.98 * base, repeat=repeat, arm="cap157"))
    data = frame(*runs)
    summary = cs.summarise(data, BOOT, 5, manifests_for(tmp_path, data))
    assert "Headline (analysis-plan 6.1): **B**" in cs.markdown(summary)


def test_the_markdown_says_when_the_headline_could_not_be_decided(tmp_path) -> None:
    runs = [cell_rows("jsq_fastfirst", _noise(40, r), repeat=r, arm="cap157") for r in (1, 2, 3)]
    data = frame(*runs)
    summary = cs.summarise(data, BOOT, 5, manifests_for(tmp_path, data))
    md = cs.markdown(summary)
    assert f"Headline (analysis-plan 6.1): not decided, undefined: no valid run for {ARM}." in md
    assert "Headline (analysis-plan 6.1): **" not in md


# ------------------------------------------------------------------ what the set describes


def test_r_headline_comes_from_the_rows(tmp_path) -> None:
    """The R a summary quotes is the R of the runs it summarises, not of whichever row
    happened to come first. With two R values in one set there is no one R to quote."""
    one = frame(cell_rows("jsq", _noise(30, 1), R=3.0), cell_rows("wjsq", _noise(30, 2), R=3.0))
    s = cs.summarise(one, 50, 1, manifests_for(tmp_path / "one", one))
    assert s["R_headline"] == 3.0
    assert "R_values" not in s

    two = frame(cell_rows("jsq", _noise(30, 1), R=3.0), cell_rows("wjsq", _noise(30, 2), R=1.5))
    s = cs.summarise(two, 50, 1, manifests_for(tmp_path / "two", two))
    assert s["R_headline"] is None
    assert s["R_values"] == [1.5, 3.0]


def test_two_traces_from_one_seed_are_not_independent_arrivals(tmp_path) -> None:
    """A trace's hash also covers the generator commit, so one seed regenerated at a later
    commit hashes differently while drawing the same arrivals."""
    from support import run_id, write_manifest

    data = frame(
        cell_rows("jsq", _noise(30, 1), repeat=1), cell_rows("jsq", _noise(30, 2), repeat=2)
    )
    assert data["trace_sha256"].nunique() == 2
    for r in (1, 2):
        write_manifest(tmp_path, run_id("jsq", repeat=r), policy="jsq", gen_seed=11)
    s = cs.summarise(data, 50, 1, tmp_path / "runset.parquet")
    assert s["arrivals_independent"] is False
    assert s["gen_seeds"] == [11]
