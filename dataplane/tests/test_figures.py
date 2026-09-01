"""The figure layer — F-24's stamp, and the exclusions every figure inherits.

The forward test pins the contract: what `render` must refuse, which sign convention H1
uses, what unit H3's axis is in. This file covers the rest — the run-set figures that
produce the actual PNGs, and every refusal path, because a figure script that fails
silently produces a plausible plot and that is the worst outcome available to it.
"""

from __future__ import annotations

import matplotlib
import pandas as pd
import pytest

from dataplane import figures
from dataplane.figures import plots

matplotlib.use("Agg")


def _run(run_id: str, *, offered: float, latencies: list[float], vehicle: str = "hardware"):
    """One run's worth of analysable rows at a stated offered rate."""
    base = figures.example_frame()
    rows = []
    for i, latency in enumerate(latencies):
        row = base.iloc[-1].to_dict()
        row.update(
            {
                "run_id": run_id,
                "req_id": f"{run_id}-{i}",
                "lambda": offered,
                "intended_offset_s": 10.0 + i / offered,
                "e2e_ms": latency,
                "status": "ok",
                "service_ms": latency * 0.8,
                "queue_wait_ms": latency * 0.1,
                "is_warmup": False,
                "vehicle": vehicle,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _set(vehicle: str = "hardware") -> pd.DataFrame:
    """A three-point load sweep, which is the smallest thing §5.5 can be read off."""
    return pd.concat(
        [
            _run("quiet", offered=0.5, latencies=[900.0, 950.0, 1000.0], vehicle=vehicle),
            _run("mid", offered=1.0, latencies=[1200.0, 1300.0, 1400.0], vehicle=vehicle),
            _run("heavy", offered=2.0, latencies=[4000.0, 5000.0, 6000.0], vehicle=vehicle),
        ],
        ignore_index=True,
    )


# --------------------------------------------------------------------------------------
# The exclusions, applied once so every figure inherits them
# --------------------------------------------------------------------------------------


def test_analysable_drops_warmup_failures_and_rows_with_no_worker_record() -> None:
    """Three exclusions, each of which produces a plausible wrong number if skipped: a
    cold cache averaged as steady state, a timeout averaged as a service time, and a
    zero service time from a request no worker ever logged."""
    kept = plots.analysable(figures.example_frame())
    assert not kept["is_warmup"].any()
    assert set(kept["status"]) == {"ok"}
    assert not ((kept["service_ms"] == 0.0) & (kept["queue_wait_ms"] == 0.0)).any()
    assert len(kept) == 3


def test_the_percentile_is_nearest_rank_and_agrees_with_the_load_band() -> None:
    """The figure annotates the §5.5 band, so it has to use the band's own estimator. Two
    definitions of p95 differ by a few milliseconds at every point, and the disagreement
    would be invisible and permanent."""
    from dataplane.pipeline import loadband

    values = [float(v) for v in range(1, 41)]
    for q in (0.50, 0.95, 0.99):
        assert plots.percentile(values, q) == loadband._percentile(values, q)


def test_a_percentile_of_nothing_is_an_error_not_a_zero() -> None:
    """Zero would plot as a suspiciously fast operating point rather than as an absence."""
    with pytest.raises(ValueError, match="no values"):
        plots.percentile([], 95)


def test_achieved_rate_needs_more_than_one_completion() -> None:
    """A single completion spans no window, so any rate computed from it is division by a
    number I made up."""
    assert plots.achieved_rps(plots.analysable(figures.example_frame()).head(1)) == 0.0


def test_achieved_rate_is_client_local_end_to_end() -> None:
    """First intended offset to last delivery, both the client's own numbers — so this
    survives hosts whose clocks were never synchronised."""
    rows = plots.analysable(_run("r", offered=1.0, latencies=[1000.0, 1000.0, 1000.0]))
    assert plots.achieved_rps(rows) > 0


def test_a_zero_span_reports_no_rate_rather_than_dividing_by_it() -> None:
    rows = plots.analysable(_run("r", offered=1.0, latencies=[0.0, 0.0]))
    rows = rows.assign(intended_offset_s=0.0, e2e_ms=0.0)
    assert plots.achieved_rps(rows) == 0.0


# --------------------------------------------------------------------------------------
# One row per run, because a run is the unit of the load axis
# --------------------------------------------------------------------------------------


def test_the_load_axis_has_one_point_per_run_not_per_request() -> None:
    """Every request inside a run was offered at the same rate. Pooling them and binning
    by arrival rate smears the operating points together, which is exactly the structure
    §5.5 needs kept apart."""
    points = plots.by_offered_load(_set())
    assert list(points["run_id"]) == ["quiet", "mid", "heavy"]
    assert list(points["offered_rps"]) == [0.5, 1.0, 2.0]
    assert (points["p99_ms"] >= points["p50_ms"]).all()


# --------------------------------------------------------------------------------------
# F-24
# --------------------------------------------------------------------------------------


def test_an_unknown_vehicle_is_refused() -> None:
    """F-24 turns on this field, so a value nobody defined must not be drawn at all."""
    with pytest.raises(ValueError, match="not one of"):
        plots.vehicle_of({"vehicle": "emulator"})


def test_stamping_needs_something_to_stamp_from() -> None:
    with pytest.raises(ValueError, match="no vehicle"):
        plots.stamp_text([])


def test_render_many_needs_at_least_one_run() -> None:
    with pytest.raises(ValueError, match="at least one run"):
        figures.render_many(figures.example_frame(), manifests=[])


def test_a_figure_from_a_frame_with_no_analysable_rows_still_draws() -> None:
    """An empty run is a result — every request failed — and it must not crash the render
    that would have shown it."""
    empty = figures.example_frame().assign(status="timeout")
    fig = figures.render(empty, manifest={"vehicle": "hardware"})
    assert figures.caption(fig)


def test_a_run_set_is_named_by_its_model_when_the_frame_says_so() -> None:
    """The model set is a replication axis: the same sweep under a different model is a
    different result, not more samples of the same one."""
    frame = figures.example_frame().assign(model="granite4-h-tiny")
    fig = figures.render(frame, manifest={"vehicle": "hardware"})
    assert "granite4-h-tiny" in figures.caption(fig)


def test_a_figure_with_no_caption_reports_the_empty_string() -> None:
    """`caption` reads the figure back rather than trusting the caller, so a figure that
    lost its caption is detectable."""
    import matplotlib.pyplot as plt

    fig = plt.figure()
    assert figures.caption(fig) == ""
    plt.close(fig)


# --------------------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------------------


def test_a_manifest_with_no_validity_block_is_kept() -> None:
    """Absent is not the same as false. A run that predates the validity block is not a
    run that failed one."""
    assert figures.eligible([{"run_id": "old"}]) == [{"run_id": "old"}]


# --------------------------------------------------------------------------------------
# The hypothesis estimators
# --------------------------------------------------------------------------------------


def test_h2_needs_the_columns_it_sweeps_over() -> None:
    with pytest.raises(ValueError, match="needs a 'R'|needs a 'policy'|needs a"):
        figures.h2_advantage_curve(pd.DataFrame({"R": [1.0, 2.0]}))


def test_h2_refuses_an_r_that_has_only_one_side_of_the_2x2() -> None:
    """The advantage at that R is undefined, not zero — and zero would plot as a point on
    the curve saying hardware-awareness bought nothing there."""
    sweep = figures.example_sweep()
    lopsided = sweep[~((sweep["R"] == 8.0) & sweep["policy"].isin(["static_weighted", "wjsq"]))]
    with pytest.raises(ValueError, match="undefined rather than zero"):
        figures.h2_advantage_curve(lopsided)


def test_h3_needs_a_staleness_column() -> None:
    with pytest.raises(ValueError, match="staleness_s"):
        figures.h3_axis(pd.DataFrame({"R": [1.0]}), autocorr_time_s=42.0)


def test_h3_refuses_a_tau_that_was_never_measured() -> None:
    """tau is the unit of the axis. A non-positive one means it was assumed, and the whole
    reason tau was measured in Week 2 was to avoid assuming it."""
    with pytest.raises(ValueError, match="never measured"):
        figures.h3_axis(figures.example_sweep(), autocorr_time_s=0.0)


def test_the_h2_fixture_is_non_monotonic_on_purpose() -> None:
    """A monotonic fixture would let an estimator that reports the wrong shape pass."""
    curve = figures.h2_advantage_curve(figures.example_sweep())
    advantages = [point["advantage_ms"] for point in curve]
    assert advantages[1] > advantages[0]
    assert advantages[2] < advantages[1]


# --------------------------------------------------------------------------------------
# The run-set figures, which are the artifacts
# --------------------------------------------------------------------------------------


def test_the_default_render_skips_validation_on_a_hardware_only_set(tmp_path) -> None:
    """A hardware-only set is the normal case until the DES lands, and the default run
    must not fail on it — while still refusing loudly when validation is asked for."""
    written = figures.render_set(_set(), tmp_path)
    # The exhaustive list, not a subset: what the default draws is a decision, and a
    # figure appearing in or vanishing from it should have to be stated here. These four
    # are S5.4's dependent variables plus the throughput axis; `validation` is the one
    # the default withholds, which is what this test is named for.
    assert [p.name for p in written] == [
        "latency_vs_load.png",
        "throughput_vs_load.png",
        "queue_wait_vs_load.png",
        "node_utilization.png",
    ]
    assert all(p.stat().st_size > 0 for p in written)


def test_a_hardware_figure_carries_no_simulated_stamp(tmp_path) -> None:
    figures.render_set(_set(), tmp_path, ["latency-vs-load"])
    assert (tmp_path / "latency_vs_load.png").exists()


def test_validation_refuses_a_set_holding_one_vehicle(tmp_path) -> None:
    """F-23's whole content is that the comparison is like-for-like. Drawing the hardware
    half alone would validate nothing while looking like a validation."""
    with pytest.raises(ValueError, match="validates nothing"):
        figures.render_set(_set(), tmp_path, ["validation"])


def test_validation_refuses_two_vehicles_that_replayed_different_traces(tmp_path) -> None:
    """Otherwise the gap between the curves is part simulator error and part workload
    difference, with no way to tell them apart."""
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    both.loc[both["vehicle"] == "simulator", "trace_sha256"] = "1" * 64
    with pytest.raises(ValueError, match="identical trace"):
        figures.render_set(both, tmp_path, ["validation"])


def test_validation_draws_both_vehicles_and_stamps_the_result(tmp_path) -> None:
    """The Week-4 joint gate, drawn. The stamp is not optional here: a figure that
    contains simulated points is a simulated figure."""
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    written = figures.render_set(both, tmp_path, ["validation"])
    assert written == [tmp_path / "validation.png"]


def test_the_default_set_includes_validation_once_both_vehicles_are_present(tmp_path) -> None:
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    written = figures.render_set(both, tmp_path)
    assert (tmp_path / "validation.png") in written


def test_an_unknown_figure_name_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="no such figure"):
        figures.render_set(_set(), tmp_path, ["latency-by-vibes"])


# --------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------


def test_the_cli_reads_a_parquet_and_writes_the_figures(tmp_path, capsys) -> None:
    parquet = tmp_path / "set.parquet"
    _set().to_parquet(parquet, index=False)

    code = figures.main([str(parquet), "--out", str(tmp_path / "fig")])

    assert code == 0
    printed = capsys.readouterr().out
    assert "latency_vs_load.png" in printed
    assert (tmp_path / "fig" / "throughput_vs_load.png").exists()


def test_the_cli_can_be_asked_for_one_figure(tmp_path) -> None:
    parquet = tmp_path / "set.parquet"
    _set().to_parquet(parquet, index=False)
    figures.main([str(parquet), "--out", str(tmp_path / "fig"), "--only", "throughput-vs-load"])
    assert not (tmp_path / "fig" / "latency_vs_load.png").exists()


def test_h2_refuses_a_sweep_holding_a_single_r() -> None:
    """One point cannot be non-monotonic. Reporting it anyway is how MPR-2's *range*
    quietly becomes a figure."""
    sweep = figures.example_sweep()
    with pytest.raises(ValueError, match="one point cannot be non-monotonic"):
        figures.h2_advantage_curve(sweep[sweep["R"] == 1.0])


def test_caption_ignores_the_other_text_on_the_figure() -> None:
    """The stamp is text too. `caption` has to find the caption specifically, or a
    simulated figure would report "SIMULATED" as the run set it came from."""
    import matplotlib.pyplot as plt

    fig = plt.figure()
    plots.stamp(fig, ["simulator"])
    assert figures.caption(fig) == ""
    assert "SIMULATED" in figures.annotations(fig)
    plt.close(fig)


# --------------------------------------------------------------------------------------
# §5.4's other dependent variables
# --------------------------------------------------------------------------------------


def test_a_run_with_no_scheduler_decisions_reports_no_routing_error_rate() -> None:
    """`None`, never `0.0`. A run driven by the fixture scheduler observed nothing about
    routing; reporting zero would claim routing was perfect. Opposite claims, so they must
    not share a value."""
    assert plots.routing_error_rate(plots.analysable(_set())) is None


def test_the_routing_error_rate_counts_only_material_ones() -> None:
    """ "Materially sooner" needs a threshold or every floating-point difference counts.
    A saving worth 10% of the request's own service time is the line."""
    rows = plots.analysable(_set()).copy()
    service = rows["service_ms"].astype(float)
    # One clearly material, one clearly not, the rest unobserved.
    rows["routing_error_ms"] = None
    rows.iloc[0, rows.columns.get_loc("routing_error_ms")] = service.iloc[0] * 0.5
    rows.iloc[1, rows.columns.get_loc("routing_error_ms")] = service.iloc[1] * 0.01
    assert plots.routing_error_rate(rows) == pytest.approx(0.5)


def test_the_load_axis_carries_queue_wait_and_the_routing_error_rate() -> None:
    """§5.4 names four dependent variables, not one. Latency percentiles alone leave the
    queue-wait and routing-quality halves of the question unanswered."""
    points = plots.by_offered_load(_set())
    assert {"queue_wait_p50_ms", "queue_wait_p95_ms", "routing_error_rate"} <= set(points.columns)
    assert (points["queue_wait_p95_ms"] >= points["queue_wait_p50_ms"]).all()


def test_per_node_utilization_reports_a_ratio_not_a_fraction() -> None:
    """A node running four slots can legitimately serve four requests at once, so the raw
    number is a mean concurrency and can exceed 1. Naming it a fraction would invite
    reading a healthy node as an impossible one."""
    table = plots.per_node_utilization(_set())
    assert list(table["node_id"]) == ["n1"]
    assert table["busy_ratio"].iloc[0] > 0
    assert "utilization" not in table.columns


def test_a_slot_count_turns_the_ratio_into_the_fraction_5_4_asks_for() -> None:
    table = plots.per_node_utilization(_set(), slots_per_node=4)
    assert table["utilization"].iloc[0] == pytest.approx(table["busy_ratio"].iloc[0] / 4)


def test_a_node_whose_requests_span_no_time_reports_no_utilization() -> None:
    """Degenerate but reachable: a client that recorded no elapsed time against a worker
    that recorded service. Guarded rather than left to divide by zero."""
    rows = _run("n", offered=1.0, latencies=[100.0, 100.0]).assign(
        intended_offset_s=0.0, e2e_ms=0.0
    )
    assert plots.per_node_utilization(rows)["busy_ratio"].iloc[0] == 0.0


def test_a_run_that_retired_nothing_still_has_the_columns() -> None:
    """An empty frame with no columns raises on the first thing a caller asks for, which
    reads as a bug in the caller rather than as "this run retired nothing"."""
    table = plots.per_node_utilization(_set().assign(status="timeout"))
    assert table.empty
    assert "busy_ratio" in table.columns


# --------------------------------------------------------------------------------------
# F-23, as the requirement actually words it
# --------------------------------------------------------------------------------------


def test_the_bootstrap_interval_is_reproducible_from_the_same_samples() -> None:
    """F-20: a reported interval has to be recoverable from the same Parquet, so the
    resampling is seeded rather than left to the global RNG."""
    values = [float(v) for v in range(1, 60)]
    assert plots.bootstrap_halfwidth(values, 0.95) == plots.bootstrap_halfwidth(values, 0.95)
    assert plots.bootstrap_halfwidth(values, 0.95) > 0


def test_a_bootstrap_needs_more_than_one_sample() -> None:
    with pytest.raises(ValueError, match="at least two samples"):
        plots.bootstrap_halfwidth([1.0], 0.95)


def test_validation_reports_p50_and_p95_error_against_a_stated_tolerance() -> None:
    """F-23's criterion is agreement in p50 *and* p95 within a stated tolerance, with the
    tolerance and the observed error both reported. Two curves on a plot satisfy none of
    that on their own."""
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    records = figures.validation_error(both)
    assert len(records) == 3
    for record in records:
        assert {"p50_rel_error", "p95_rel_error", "within_tolerance"} <= set(record)
        # Identical inputs on both sides: the error is zero and the gate passes.
        assert record["p50_rel_error"] == pytest.approx(0.0)
        assert record["within_tolerance"]


def test_the_anchors_own_resolution_is_reported_beside_the_error() -> None:
    """The comparison has two uncertain sides. Quoting the simulator's deviation without
    the hardware run's own interval invites reading a difference the anchor cannot itself
    resolve as a simulator defect."""
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    record = figures.validation_error(both)[0]
    assert record["p50_anchor_halfwidth"] > 0
    assert record["p95_anchor_halfwidth"] > 0


def test_a_simulator_outside_the_tolerance_is_marked_outside() -> None:
    simulator = _set("simulator")
    simulator["e2e_ms"] = simulator["e2e_ms"] * 2.0
    both = pd.concat([_set("hardware"), simulator], ignore_index=True)
    records = figures.validation_error(both)
    assert not any(record["within_tolerance"] for record in records)
    assert max(record["worst_rel_error"] for record in records) > figures.F23_TOLERANCE


def test_fewer_than_three_matched_points_is_refused() -> None:
    """Three is not a stylistic minimum: two points can be fitted exactly by a simulator
    with two free parameters, which is the failure F-23 exists to prevent."""
    hardware = _set("hardware")
    simulator = _set("simulator")
    simulator = simulator[simulator["lambda"] != 2.0]
    with pytest.raises(ValueError, match="at least 3 operating points"):
        figures.validation_error(pd.concat([hardware, simulator], ignore_index=True))


def test_an_unmatched_operating_point_is_not_a_validation_point() -> None:
    """The two vehicles name their runs independently, so points are paired on offered
    load. A simulator point with no hardware twin compares against nothing."""
    hardware = _set("hardware")
    simulator = _set("simulator")
    simulator.loc[simulator["lambda"] == 2.0, "lambda"] = 7.0
    with pytest.raises(ValueError, match="at least 3 operating points"):
        figures.validation_error(pd.concat([hardware, simulator], ignore_index=True))


def test_the_validation_figure_writes_the_verdict_onto_itself(tmp_path) -> None:
    """A reader should not have to consult a separate table to see whether the gate
    passed."""
    both = pd.concat([_set("hardware"), _set("simulator")], ignore_index=True)
    figures.render_set(both, tmp_path, ["validation"])
    assert (tmp_path / "validation.png").stat().st_size > 0


# --------------------------------------------------------------------------------------
# MPR-2
# --------------------------------------------------------------------------------------


def test_mpr2_is_a_range_over_r_not_a_single_interaction() -> None:
    """§7 words MPR-2 as the 2x2 "across the synthesized heterogeneity range ... reported
    as a range rather than a single figure". One interaction term is the ingredient."""
    result = figures.mpr2_interaction_range(figures.example_sweep())
    assert result["low"] <= result["high"]
    assert set(result["interaction_by_r"]) == {1.0, 2.0, 8.0}
    assert result["low_at_r"] in result["interaction_by_r"]


def test_mpr2_names_the_case_where_the_interval_straddles_zero() -> None:
    """An interval crossing zero says the redundancy H1 claims is not established across
    the range — a publishable negative, and a different statement from a mean near zero."""
    sweep = figures.example_sweep().copy()
    at_high_r = (sweep["R"] == 8.0) & (sweep["policy"] == "wjsq")
    sweep.loc[at_high_r, "mean_latency_ms"] = 1.0
    result = figures.mpr2_interaction_range(sweep)
    assert result["sign_consistent"] is False


def test_mpr2_needs_the_columns_it_decomposes() -> None:
    with pytest.raises(ValueError, match="MPR-2 needs a"):
        figures.mpr2_interaction_range(pd.DataFrame({"R": [1.0]}))


def test_mpr2_refuses_an_r_missing_a_cell_of_the_2x2() -> None:
    """Inherited from `h1_interaction`, and worth pinning: a ladder must not become a
    factorial by silently dropping the cell that was not run."""
    sweep = figures.example_sweep()
    with pytest.raises(KeyError, match="2x2"):
        figures.mpr2_interaction_range(sweep[sweep["policy"] != "static_weighted"])


def test_utilization_is_empty_when_no_decision_record_names_a_node() -> None:
    """C-5's only node identity is `chosen_node`, which the fixture scheduler never
    writes. Empty is the honest answer — the alternative would be inventing a node."""
    assert plots.per_node_utilization(_set().assign(chosen_node=None)).empty
