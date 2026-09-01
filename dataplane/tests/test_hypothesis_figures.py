"""The hypothesis figures — the estimators of H1, H2 and H3, drawn.

Every estimator in `plots` was already tested as arithmetic. What is new here is the
rendering path, and the tests that matter are not about pixels. They are about the three
ways a figure can be wrong while still producing a PNG:

**Drawing an incomplete decomposition.** Three policies can be ranked but not decomposed.
A figure that quietly plots what it has would put a ranking in the report under a title
that says "interaction".

**Reducing requests to runs incorrectly.** The estimators take one row per run because a
run's requests are correlated with each other. Pooling requests and grouping afterwards
lets a long run outvote a short one inside a cell that is meant to be one observation.

**Inventing tau.** H3's axis is estimate age divided by the measured autocorrelation
time. Defaulting it to anything, including the heartbeat interval, silently rescales the
only axis the hypothesis is about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from dataplane.figures import plots

TAU_S = 69.5  # the Week-2 measurement on the CPU class, used as a stated input


def _requests(
    *,
    run_id: str,
    policy: str,
    r_value: float,
    staleness_s: float,
    latency_ms: float,
    n: int = 12,
    vehicle: str = "hardware",
    decided: bool = True,
    warmup: bool = False,
) -> list[dict[str, Any]]:
    """One run's worth of C-5 rows, in the shape `runset` writes them."""
    return [
        {
            "run_id": run_id,
            "vehicle": vehicle,
            "policy": policy,
            "R": r_value,
            "staleness_s": staleness_s,
            "lambda": 1.2,
            "is_warmup": warmup,
            "status": "ok",
            "e2e_ms": latency_ms + i,
            "service_ms": 400.0,
            "queue_wait_ms": 5.0,
            "intended_offset_s": float(i),
            # Two nodes, so the utilization figure has work to attribute. C-5 carries node
            # identity only here, through the scheduler's choice.
            "chosen_node": "n1" if i % 3 else "n2",
            # Materiality is 10% of service time, so 60 ms against 400 ms is not an
            # error and 200 ms is. A quarter of requests are made errors, and the share
            # rises with staleness, which is the shape H3 predicts.
            "routing_error_ms": (
                (200.0 if i % 4 == 0 else 10.0) + staleness_s if decided else float("nan")
            ),
        }
        for i in range(n)
    ]


def _sweep_frame(
    *,
    r_values: tuple[float, ...] = (1.0, 2.0, 8.0),
    staleness: tuple[float, ...] = (0.0, 10.0, 60.0),
    policies: tuple[str, ...] = plots.CELLS,
    decided: bool = True,
) -> pd.DataFrame:
    """The 2x2 crossed with R and staleness, non-monotonic in R on purpose.

    A fixture that rose monotonically in R would let an H2 figure that reports the wrong
    shape pass anyway, which is the one thing the figure exists to show.
    """
    advantage = {1.0: 1.0, 2.0: 25.0, 8.0: 5.0}
    rows: list[dict[str, Any]] = []
    run = 0
    for r_value in r_values:
        for stale in staleness:
            blind = 100.0 + 10.0 * r_value
            aware = blind - advantage[r_value]
            for policy in policies:
                run += 1
                base = aware if policy in plots.HARDWARE_AWARE else blind
                if policy in ("jsq", "wjsq"):
                    base -= 4.0
                rows += _requests(
                    run_id=f"run{run:03d}",
                    policy=policy,
                    r_value=r_value,
                    staleness_s=stale,
                    latency_ms=base * (1.0 + stale / 500.0),
                    decided=decided,
                )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------------------
# sweep_from — requests to runs
# --------------------------------------------------------------------------------------


def test_the_unit_of_a_sweep_row_is_a_run_not_a_request() -> None:
    """36 conditions, 12 requests each, 36 rows out. A run is one observation."""
    frame = _sweep_frame()
    sweep = plots.sweep_from(frame)
    assert len(sweep) == 36
    assert len(frame) == 36 * 12


def test_a_long_run_does_not_outvote_a_short_one_in_its_cell() -> None:
    """The reason the unit is a run: pooling requests would weight by run length.

    Two runs of the same policy at the same R, one four times longer and much slower.
    Averaged per run they sit at the midpoint; pooled per request the long one dominates.
    """
    frame = pd.DataFrame(
        _requests(run_id="short", policy="jsq", r_value=1.0, staleness_s=0.0, latency_ms=100.0, n=4)
        + _requests(
            run_id="long", policy="jsq", r_value=1.0, staleness_s=0.0, latency_ms=300.0, n=16
        )
    )
    sweep = plots.sweep_from(frame)
    assert set(sweep["run_id"]) == {"short", "long"}
    per_run = float(sweep["mean_latency_ms"].mean())
    per_request = float(frame["e2e_ms"].mean())
    # Per run: the mean of 101.5 and 307.5. Per request: 4 slow-run requests for every
    # fast one, so the long run drags the pooled figure 62 ms above the cell's midpoint.
    assert per_run == pytest.approx(204.5)
    assert per_request == pytest.approx(266.3)
    assert per_request - per_run > 60.0


def test_warmup_and_failures_never_reach_a_sweep_row() -> None:
    rows = _requests(run_id="r1", policy="jsq", r_value=1.0, staleness_s=0.0, latency_ms=100.0, n=6)
    rows[0]["is_warmup"] = True
    rows[1]["status"] = "timeout"
    rows[1]["e2e_ms"] = 60000.0
    sweep = plots.sweep_from(pd.DataFrame(rows))
    assert int(sweep["n"].iloc[0]) == 4
    assert sweep["mean_latency_ms"].iloc[0] < 200.0


def test_a_set_with_nothing_analysable_is_refused_rather_than_averaged() -> None:
    rows = _requests(run_id="r1", policy="jsq", r_value=1.0, staleness_s=0.0, latency_ms=100.0, n=3)
    for row in rows:
        row["status"] = "timeout"
    with pytest.raises(ValueError, match="nothing to decompose"):
        plots.sweep_from(pd.DataFrame(rows))


def test_tau_rides_through_the_reduction_when_it_was_injected() -> None:
    frame = _sweep_frame().assign(tau_s=TAU_S)
    assert float(plots.sweep_from(frame)["tau_s"].iloc[0]) == TAU_S


def test_a_run_with_no_scheduler_decision_reports_no_routing_error_rate() -> None:
    """None, not zero. Unobserved routing and perfect routing are opposite claims."""
    frame = _sweep_frame(r_values=(1.0,), staleness=(0.0,), decided=False)
    assert plots.sweep_from(frame)["routing_error_rate"].isna().all()


# --------------------------------------------------------------------------------------
# H1
# --------------------------------------------------------------------------------------


def test_h1_draws_the_interaction_and_names_its_sign(tmp_path: Path) -> None:
    path = plots.h1_decomposition(_sweep_frame(), tmp_path)
    assert path.name == "h1_decomposition.png" and path.stat().st_size > 0


def test_h1_refuses_a_three_policy_ladder(tmp_path: Path) -> None:
    """The failure this figure exists to prevent: a ranking captioned as a factorial."""
    frame = _sweep_frame(policies=("round_robin", "jsq", "wjsq"))
    with pytest.raises(KeyError, match="static_weighted"):
        plots.h1_decomposition(frame, tmp_path)


def test_the_figure_and_the_estimator_agree_on_the_same_set() -> None:
    """The drawing must not recompute the claim its own estimator states."""
    sweep = plots.sweep_from(_sweep_frame())
    cells = plots._cells_at(sweep)
    assert set(cells) == set(plots.CELLS)
    assert plots.h1_interaction(cells) == pytest.approx(
        (cells["wjsq"] - cells["jsq"]) - (cells["static_weighted"] - cells["round_robin"])
    )


# --------------------------------------------------------------------------------------
# H2 and MPR-2
# --------------------------------------------------------------------------------------


def test_h2_draws_the_curve(tmp_path: Path) -> None:
    path = plots.h2_advantage(_sweep_frame(), tmp_path)
    assert path.name == "h2_advantage.png" and path.stat().st_size > 0


def test_h2_refuses_a_single_operating_point(tmp_path: Path) -> None:
    """One R cannot be non-monotonic, and a point estimate is not a shape."""
    with pytest.raises(ValueError, match="one point cannot be non-monotonic"):
        plots.h2_advantage(_sweep_frame(r_values=(2.0,)), tmp_path)


def test_the_curve_recovers_the_shape_the_fixture_was_built_with() -> None:
    """Rises then falls. If the estimator flattened it, the figure would still draw."""
    curve = plots.h2_advantage_curve(plots.sweep_from(_sweep_frame()))
    advantage = [point["advantage_ms"] for point in curve]
    assert advantage[1] > advantage[0] and advantage[1] > advantage[2]


def test_mpr2_draws_the_range(tmp_path: Path) -> None:
    path = plots.mpr2_range(_sweep_frame(), tmp_path)
    assert path.name == "mpr2_range.png" and path.stat().st_size > 0


def test_mpr2_reports_a_straddling_interval_as_such(tmp_path: Path) -> None:
    """An interval crossing zero is a different result from a mean near zero.

    Built by flipping which side calibration helps at the two ends of the range, so the
    interaction changes sign across R and `sign_consistent` has to say so.
    """
    rows: list[dict[str, Any]] = []
    run = 0
    for r_value, wjsq in ((1.0, 80.0), (8.0, 130.0)):
        for policy, latency in (
            ("round_robin", 120.0),
            ("static_weighted", 110.0),
            ("jsq", 100.0),
            ("wjsq", wjsq),
        ):
            run += 1
            rows += _requests(
                run_id=f"m{run}",
                policy=policy,
                r_value=r_value,
                staleness_s=0.0,
                latency_ms=latency,
            )
    result = plots.mpr2_interaction_range(plots.sweep_from(pd.DataFrame(rows)))
    assert not result["sign_consistent"]
    assert result["low"] < 0 < result["high"]
    assert plots.mpr2_range(pd.DataFrame(rows), tmp_path).stat().st_size > 0


# --------------------------------------------------------------------------------------
# H3
# --------------------------------------------------------------------------------------


def test_h3_draws_against_tau(tmp_path: Path) -> None:
    path = plots.h3_staleness(_sweep_frame().assign(tau_s=TAU_S), tmp_path)
    assert path.name == "h3_staleness.png" and path.stat().st_size > 0


def test_h3_refuses_to_invent_tau(tmp_path: Path) -> None:
    """Without the measurement the axis is a guess, and it is the only axis H3 is about."""
    with pytest.raises(ValueError, match="measured autocorrelation time"):
        plots.h3_staleness(_sweep_frame(), tmp_path)


def test_h3_refuses_a_run_set_that_observed_no_routing(tmp_path: Path) -> None:
    """The fixture scheduler writes no decision record, so it cannot answer H3."""
    frame = _sweep_frame(decided=False).assign(tau_s=TAU_S)
    with pytest.raises(ValueError, match="unobserved rather than zero"):
        plots.h3_staleness(frame, tmp_path)


def test_the_axis_is_age_over_tau_and_not_age(tmp_path: Path) -> None:
    sweep = plots.sweep_from(_sweep_frame().assign(tau_s=TAU_S))
    axis = plots.h3_axis(sweep, autocorr_time_s=TAU_S)
    assert axis.name == "estimate_age_over_tau"
    assert max(axis) == pytest.approx(60.0 / TAU_S)


# --------------------------------------------------------------------------------------
# Selection, and the CLI
# --------------------------------------------------------------------------------------


def test_a_hardware_only_set_at_one_r_draws_only_what_it_can() -> None:
    """Skipped by default, so a partial set renders rather than failing on its first gap."""
    frame = _sweep_frame(r_values=(1.0,), staleness=(0.0,))
    names = plots.drawable(frame)
    assert "h1-decomposition" in names
    assert "h2-advantage" not in names and "mpr2-range" not in names
    assert "h3-staleness" not in names and "validation" not in names


def test_a_full_set_draws_every_hypothesis_figure() -> None:
    names = plots.drawable(_sweep_frame().assign(tau_s=TAU_S))
    assert {"h1-decomposition", "h2-advantage", "mpr2-range", "h3-staleness"} <= set(names)


def test_a_three_policy_set_offers_no_decomposition() -> None:
    frame = _sweep_frame(policies=("round_robin", "jsq", "wjsq"))
    assert "h1-decomposition" not in plots.drawable(frame)


def test_asking_for_a_skipped_figure_by_name_still_refuses(tmp_path: Path) -> None:
    """Silence and refusal are both right; which one you get depends on whether you asked."""
    frame = _sweep_frame(r_values=(2.0,), staleness=(0.0,))
    with pytest.raises(ValueError, match="non-monotonic"):
        plots.render_set(frame, tmp_path, ["h2-advantage"])


def test_render_set_injects_tau_in_exactly_one_place(tmp_path: Path) -> None:
    written = plots.render_set(_sweep_frame(), tmp_path, autocorr_time_s=TAU_S)
    assert (tmp_path / "h3_staleness.png") in written


def test_the_cli_renders_a_run_set_from_parquet(tmp_path: Path) -> None:
    parquet = tmp_path / "runset.parquet"
    _sweep_frame().to_parquet(parquet, index=False)
    assert plots.main([str(parquet), "--out", str(tmp_path), "--tau-s", str(TAU_S)]) == 0
    for name in ("h1_decomposition", "h2_advantage", "mpr2_range", "h3_staleness"):
        assert (tmp_path / f"{name}.png").exists()


def test_the_cli_skips_h3_without_tau(tmp_path: Path) -> None:
    parquet = tmp_path / "runset.parquet"
    _sweep_frame().to_parquet(parquet, index=False)
    assert plots.main([str(parquet), "--out", str(tmp_path)]) == 0
    assert not (tmp_path / "h3_staleness.png").exists()
    assert (tmp_path / "h2_advantage.png").exists()


def test_every_registered_figure_name_resolves(tmp_path: Path) -> None:
    """A name in the registry that nothing can draw is a broken --only flag."""
    frame = _sweep_frame().assign(tau_s=TAU_S)
    for name in plots.FIGURES:
        if name == "validation":  # needs both vehicles, which this set does not hold
            continue
        assert plots.render_set(frame, tmp_path, [name])


# --------------------------------------------------------------------------------------
# §5.4's other two dependent variables
# --------------------------------------------------------------------------------------


def test_queue_wait_is_drawn_apart_from_end_to_end_latency(tmp_path: Path) -> None:
    """Two panels, because they answer different questions.

    End-to-end latency rising could be a slower engine or a longer queue. Queue wait
    rising can only be a queue, which is what makes it the panel that says which of the
    two §5.5's onset actually is.
    """
    path = plots.queue_wait_vs_load(_sweep_frame(), tmp_path)
    assert path.name == "queue_wait_vs_load.png" and path.stat().st_size > 0


def test_utilization_is_drawn_per_node(tmp_path: Path) -> None:
    path = plots.node_utilization(_sweep_frame(), tmp_path)
    assert path.name == "node_utilization.png" and path.stat().st_size > 0


def test_utilization_refuses_a_set_that_cannot_say_which_node_served_what(
    tmp_path: Path,
) -> None:
    """An empty utilization panel reads as an idle pool, which is the opposite claim.

    C-5 carries node identity only through the scheduler's `chosen_node`, so a run the
    fixture scheduler drove has none. That is unattributed work, not zero work.
    """
    frame = _sweep_frame().assign(chosen_node=None)
    with pytest.raises(ValueError, match="unattributed rather than zero"):
        plots.node_utilization(frame, tmp_path)


def test_a_frame_without_the_node_column_at_all_simply_offers_no_utilization() -> None:
    """A frame assembled by hand for one figure need not carry every C-5 column."""
    frame = _sweep_frame().drop(columns=["chosen_node"])
    assert "node-utilization" in plots.drawable(_sweep_frame())
    assert "node-utilization" not in plots.drawable(frame)


def test_all_four_of_the_specs_dependent_variables_are_drawable() -> None:
    """§5.4 names four. Latency, queue wait, utilization, routing-error rate."""
    names = plots.drawable(_sweep_frame().assign(tau_s=TAU_S))
    assert {"latency-vs-load", "queue-wait-vs-load", "node-utilization", "h3-staleness"} <= set(
        names
    )
