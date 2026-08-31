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
    assert [p.name for p in written] == ["latency_vs_load.png", "throughput_vs_load.png"]
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
