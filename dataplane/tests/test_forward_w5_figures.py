"""Weeks 5-6 — the figure scripts (F-19, F-24), the last stage on my side.

Skipped until `dataplane.figures` lands.

Figures are where a measurement study either states what it measured or quietly overstates
it, and two of the rules are mechanical enough to test:

  * **F-24: every simulated figure is labelled as simulated.** The manifest carries
    `vehicle`, so the label is derivable. Labelling by hand fails exactly once — in the
    final report — which is why the test is that the script reads the field rather than
    that I remembered.
  * **An invalid run never reaches a figure.** `validity.valid` is computed by the harness
    from measurements. A figure script that plots whatever Parquet it is handed undoes
    that entire discipline at the last step.

The hypothesis-shaped tests below are not tests of the hypotheses — those are answered by
data, not by assertions. They are tests that the *estimator* computes the quantity H1 and
H2 are stated in terms of, with the sign convention the README uses. Getting the sign
backwards would invert the headline result and still produce a plausible plot.
"""

from __future__ import annotations

import pytest
from conftest import pending

pytestmark = pytest.mark.forward

figures = pending("dataplane.figures", "render", week="Weeks 5-6", deliverable="figure scripts")


# --------------------------------------------------------------------------------------
# F-24 — labelling
# --------------------------------------------------------------------------------------


def test_a_simulated_figure_is_stamped_from_the_manifest() -> None:
    """Read `vehicle`, stamp the plot. Never a hand-added caption."""
    fig = figures.render(figures.example_frame(), manifest={"vehicle": "simulator"})
    assert "SIMULATED" in figures.annotations(fig)


def test_a_hardware_figure_is_not_stamped() -> None:
    """The label has to mean something, so it cannot be on everything."""
    fig = figures.render(figures.example_frame(), manifest={"vehicle": "hardware"})
    assert "SIMULATED" not in figures.annotations(fig)


def test_a_manifest_with_no_vehicle_is_refused() -> None:
    """Defaulting to "hardware" would label a simulated figure as real. Refuse instead."""
    with pytest.raises((KeyError, ValueError)):
        figures.render(figures.example_frame(), manifest={})


def test_a_mixed_vehicle_frame_is_stamped_as_simulated() -> None:
    """A figure combining an anchor run with simulated sweeps is a simulated figure. The
    conservative direction is the only safe one here."""
    fig = figures.render_many(
        figures.example_frame(), manifests=[{"vehicle": "hardware"}, {"vehicle": "simulator"}]
    )
    assert "SIMULATED" in figures.annotations(fig)


# --------------------------------------------------------------------------------------
# What is allowed into a figure at all
# --------------------------------------------------------------------------------------


def test_invalid_runs_are_excluded() -> None:
    """A run that failed its own open-loop guard was not generating the load its manifest
    claims. It is a diagnostic, not a data point."""
    kept = figures.eligible(
        [
            {"run_id": "a", "validity": {"valid": True}},
            {"run_id": "b", "validity": {"valid": False}},
        ]
    )
    assert [r["run_id"] for r in kept] == ["a"]


def test_warmup_rows_are_excluded() -> None:
    """`is_warmup` is computed identically in both vehicles precisely so this filter is
    the same filter on both sides of the F-23 comparison."""
    frame = figures.example_frame()
    assert not figures.analysable(frame)["is_warmup"].any()


def test_figures_read_parquet_and_nothing_else() -> None:
    """The stage boundary that lets Aditya hand me simulator logs: the pipeline produces
    Parquet, the figures consume it. A figure script that reaches back into a raw log has
    quietly merged two stages that were separated on purpose."""
    with open(figures.__file__) as fh:
        source = fh.read()
    for forbidden in ("import grpc", "import httpx", ".jsonl"):
        assert forbidden not in source


# --------------------------------------------------------------------------------------
# The estimators, and their signs
# --------------------------------------------------------------------------------------


def test_the_h1_interaction_term_uses_the_stated_sign_convention() -> None:
    """H1 predicts calibration buys less once the policy is already queue-aware, which
    means `(WJSQ - JSQ) > (StaticWeighted - RoundRobin)`: both brackets are negative when
    calibration helps, and the queue-aware one is the shallower of the two. The estimator
    has to compute that difference in that order, or the headline result reverses while
    still looking like a result.

    This docstring used to quote the inequality the other way round, copied from the
    "Formally." sentence of H1 in the spec, which is inverted against its own prose two
    lines above it and against the assertion below. The assertions here were always
    right; only the sentence describing them was wrong."""
    latencies = {
        "round_robin": 100.0,
        "static_weighted": 80.0,  # calibration alone: -20
        "jsq": 70.0,
        "wjsq": 65.0,  # calibration given queue-awareness: -5
    }
    interaction = figures.h1_interaction(latencies)
    assert interaction == pytest.approx((65.0 - 70.0) - (80.0 - 100.0))
    assert interaction > 0, "redundancy shows as a positive interaction under this convention"


def test_the_2x2_refuses_a_missing_cell() -> None:
    """The decomposition is a 2x2, not a ladder. Three policies cannot separate
    hardware-knowledge from queue-knowledge, and silently dropping a cell is how a ladder
    gets reported as a factorial."""
    with pytest.raises((KeyError, ValueError)):
        figures.h1_interaction({"round_robin": 100.0, "jsq": 70.0, "wjsq": 65.0})


def test_the_h2_curve_is_reported_over_a_range_of_r() -> None:
    """H2 is a claim about non-monotonicity in R. A single R is not evidence about a
    curve, and MPR-2 asks for the synthesizable range to be reported as a range."""
    curve = figures.h2_advantage_curve(figures.example_sweep())
    assert len({point["R"] for point in curve}) >= 3


def test_the_h3_axis_is_estimate_age_against_tau() -> None:
    """H3 is about staleness measured in units of the node's own autocorrelation time. An
    x-axis in raw seconds would make the result a property of my heartbeat interval rather
    than of the process."""
    axis = figures.h3_axis(figures.example_sweep(), autocorr_time_s=42.0)
    assert axis.name in ("estimate_age_over_tau", "age_tau_ratio")


def test_every_figure_names_the_run_set_it_came_from() -> None:
    """Four models are staged as a replication axis across run sets. A figure that does
    not say which model it is from cannot be compared with the one that replicates it."""
    fig = figures.render(figures.example_frame(), manifest={"vehicle": "hardware"})
    assert figures.caption(fig)
