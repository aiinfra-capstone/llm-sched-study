"""F-7 self-validation — the diagnostic that sits upstream of F-23.

`costcheck` answers one question: would the cost model a run was served by have predicted
that run? It exists because an F-23 failure has two suspects on opposite sides of the seam,
and this is the one that can be settled without a simulator.

What the tests below pin is mostly about honesty rather than arithmetic. A cell the grid
never measured must be *reported* as extrapolated rather than silently priced at the
nearest measured level; a request-weighted verdict must not let a rarely-visited cell
outvote a common one; and a node whose manifest names no snapshot must raise rather than
be scored against someone else's model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dataplane.figures import plots
from dataplane.pipeline import costcheck

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


def _snapshot(
    snapshot_id: str = "cm_test",
    *,
    concurrencies: tuple[int, ...] = (1, 4),
    mean_ms: float = 1000.0,
) -> dict[str, Any]:
    """A C-3 snapshot over one bucket pair, measured at the given concurrency levels."""
    return {
        "cost_model_schema": 1,
        "snapshot_id": snapshot_id,
        "node_class": "testclass",
        "measured_at_unix": 1788000000,
        "calibration_run_ids": [],
        "form": "lookup_table",
        "entries": [
            {
                "prompt_bucket": [1, 128],
                "output_bucket": [1, 64],
                "concurrency": c,
                "service_ms_mean": mean_ms * c,
                "service_ms_p50": mean_ms * c,
                "service_ms_p95": mean_ms * c * 1.2,
                "tokens_per_s": 100.0,
                "n_samples": 8,
            }
            for c in concurrencies
        ],
        "stochastic": {
            "model": "lognormal_multiplier",
            "sigma": 0.02,
            "autocorr_time_s": 3.5,
            "fit_r2": 0.0,
        },
        "admissibility": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
        "provenance": {
            "engine": "llamacpp",
            "engine_version": "b10569+p1",
            "quant": "Q4_K_M",
            "gpu": "GTX 1650 Ti",
            "driver": "580.173.02",
            "prefix_caching": False,
            "engine_config": {"ngl": 99, "threads": 6, "parallel": 4},
        },
    }


def _make_run(
    root: Path,
    run_id: str = "r1",
    *,
    requests: list[tuple[float, int, str]] | None = None,
    snapshot_id: str | None = "cm_test",
    warmup_s: float = 1.0,
) -> Path:
    """A run directory. `requests` is (service_ms, inflight_at_admission, status) per request.

    The first request always lands inside warmup, so every test that counts rows is also
    asserting that warmup was dropped.
    """
    requests = requests or [(1000.0, 0, "ok"), (4000.0, 3, "ok")]
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "vehicle": "hardware",
        "policy": "round_robin",
        "lambda": 1.0,
        "staleness_s": 0.0,
        "warmup_s": warmup_s,
        "duration_s": 30.0,
        "nodes": [{"node_id": "n1", "role": "pool", "engine": "llamacpp"}],
        "cost_model_snapshots": {} if snapshot_id is None else {"n1": snapshot_id},
        "validity": {"valid": True},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))

    client = [
        {
            "run_id": run_id,
            "req_id": f"r{i:06d}",
            # request 1 is inside warmup, everything after it is not
            "intended_offset_s": 0.0 if i == 1 else 5.0 + i,
            "send_lag_ms": 0.2,
            "e2e_duration_ns": int(service_ms * 1e6),
            "status": status,
        }
        for i, (service_ms, _, status) in enumerate(requests, start=1)
    ]
    (run_dir / f"client_{run_id}.jsonl").write_text("\n".join(json.dumps(r) for r in client) + "\n")

    worker = [
        {
            "run_id": run_id,
            "req_id": f"r{i:06d}",
            "node_id": "n1",
            "engine": "llamacpp",
            "queue_wait_ns": 0,
            "service_ns": int(service_ms * 1e6),
            "prompt_tokens": 100,
            "output_tokens": 32,
            "batch_size_at_admission": inflight,
            "inflight_at_admission": inflight,
            "status": status,
        }
        for i, (service_ms, inflight, status) in enumerate(requests, start=1)
    ]
    (run_dir / f"worker_n1_{run_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in worker) + "\n"
    )
    return run_dir


@pytest.fixture
def index() -> dict[str, dict[str, Any]]:
    return {"cm_test": _snapshot()}


# --------------------------------------------------------------------------------------
# Reading a run
# --------------------------------------------------------------------------------------


def test_warmup_requests_are_not_observations(tmp_path):
    """Warmup is dropped from `intended_offset_s`, the same rule the join uses.

    The first request into a fresh engine pays kernel JIT. Scoring the cost model against
    it would report a one-off startup cost as a prediction error.
    """
    _make_run(tmp_path, requests=[(9999.0, 0, "ok"), (1000.0, 0, "ok"), (1000.0, 0, "ok")])
    rows = costcheck.observations(tmp_path / "r1")
    assert len(rows) == 2
    assert all(row["service_ms"] == pytest.approx(1000.0) for row in rows)


def test_failed_requests_are_not_observations(tmp_path):
    """A timeout is a censored observation, never a service time (F-13)."""
    _make_run(
        tmp_path,
        requests=[(10.0, 0, "ok"), (60000.0, 0, "timeout"), (1000.0, 0, "ok")],
    )
    rows = costcheck.observations(tmp_path / "r1")
    assert [row["service_ms"] for row in rows] == [1000.0]


def test_concurrency_is_inflight_at_admission_plus_one(tmp_path):
    """The request itself counts. A node with three others busy serves this one at four."""
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (2000.0, 3, "ok")])
    rows = costcheck.observations(tmp_path / "r1")
    assert [row["concurrency"] for row in rows] == [4]


# --------------------------------------------------------------------------------------
# Pricing the cells
# --------------------------------------------------------------------------------------


def test_error_is_signed_against_the_prediction(index):
    """A hardware time of twice the prediction is +100%, not -50%."""
    rows = [
        {
            "node_id": "n1",
            "prompt_len": 100,
            "output_len": 32,
            "concurrency": 1,
            "service_ms": 2000.0,
        }
    ]
    errors, _ = costcheck.cells(rows, {"n1": index["cm_test"]})
    assert errors[0].predicted_ms == pytest.approx(1000.0)
    assert errors[0].relative_error == pytest.approx(1.0)
    assert not errors[0].within


def test_a_cell_inside_tolerance_passes(index):
    rows = [
        {
            "node_id": "n1",
            "prompt_len": 100,
            "output_len": 32,
            "concurrency": 1,
            "service_ms": 1100.0,
        }
    ]
    errors, uncalibrated = costcheck.cells(rows, {"n1": index["cm_test"]})
    assert errors[0].within
    assert uncalibrated == []


def test_an_unmeasured_concurrency_is_reported_not_hidden(index):
    """The grid measured 1 and 4. A request served at 2 is priced by extrapolation.

    `predict_service_ms` will happily answer — it snaps to the nearest measured level —
    and that answer is the one the scheduler and the DES both use, so it is the right
    thing to score. What would be wrong is scoring it without saying it was never
    measured.
    """
    rows = [
        {
            "node_id": "n1",
            "prompt_len": 100,
            "output_len": 32,
            "concurrency": 2,
            "service_ms": 1500.0,
        }
    ]
    errors, uncalibrated = costcheck.cells(rows, {"n1": index["cm_test"]})
    assert len(uncalibrated) == 1
    assert uncalibrated[0][0] == "n1"
    assert uncalibrated[0][3] == 1
    # snapped to c=1, which is why the reported error is large
    assert errors[0].predicted_ms == pytest.approx(1000.0)


def test_percentiles_are_reported_alongside_the_mean(index):
    rows = [
        {"node_id": "n1", "prompt_len": 100, "output_len": 32, "concurrency": 1, "service_ms": ms}
        for ms in (900.0, 1000.0, 1100.0, 5000.0)
    ]
    errors, _ = costcheck.cells(rows, {"n1": index["cm_test"]})
    cell = errors[0]
    assert cell.n == 4
    assert cell.observed_p50_ms == pytest.approx(1000.0)
    assert cell.observed_p95_ms == pytest.approx(5000.0)
    assert cell.observed_mean_ms == pytest.approx(2000.0)


def test_each_node_is_scored_against_its_own_model(index):
    """A heterogeneous pool has one cost model per node class (F-9a).

    Averaging a fast node's error against a slow node's model would report a number that
    describes neither, which is exactly the confound R exists to make visible.
    """
    slow = _snapshot("cm_slow", mean_ms=2000.0)
    rows = [
        {
            "node_id": "n1",
            "prompt_len": 100,
            "output_len": 32,
            "concurrency": 1,
            "service_ms": 1000.0,
        },
        {
            "node_id": "n2",
            "prompt_len": 100,
            "output_len": 32,
            "concurrency": 1,
            "service_ms": 2000.0,
        },
    ]
    errors, _ = costcheck.cells(rows, {"n1": index["cm_test"], "n2": slow})
    assert {e.node_id: round(e.relative_error, 6) for e in errors} == {"n1": 0.0, "n2": 0.0}


# --------------------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------------------


def test_the_verdict_is_weighted_by_requests_not_by_cells(tmp_path, index):
    """One rare, badly-priced cell must not outvote the cell the trace actually lives in."""
    requests = [(1.0, 0, "ok")] + [(1000.0, 0, "ok")] * 99 + [(5000.0, 3, "ok")]
    _make_run(tmp_path, requests=requests)
    result = costcheck.check(tmp_path, index=index)
    per_cell = sum(abs(e.relative_error) for e in result.errors) / len(result.errors)
    assert result.weighted_error < per_cell
    assert result.weighted_error < 0.25


def test_an_empty_check_reports_zero_rather_than_dividing(tmp_path, index):
    """No measured requests is not a 0% error and not a crash; it is nothing to report."""
    _make_run(tmp_path, requests=[(1000.0, 0, "ok")])  # the only request is warmup
    result = costcheck.check(tmp_path, index=index)
    assert result.errors == []
    assert result.weighted_error == 0.0
    assert result.weighted_median_error == 0.0
    assert result.covered == 0.0


def test_the_median_verdict_separates_a_bad_model_from_a_skewed_label(tmp_path, index):
    """A cell whose mean is far out and whose median is not is the admission-time proxy.

    Nine requests at the predicted time and one at ten times it: the mean lands 90% above
    prediction and the median lands on it. The mean is still the verdict — a mean is what
    the model predicts — but the two numbers together say which of the two stories it is.
    """
    requests = [(1.0, 0, "ok")] + [(1000.0, 0, "ok")] * 9 + [(10000.0, 0, "ok")]
    _make_run(tmp_path, requests=requests)
    result = costcheck.check(tmp_path, index=index)
    assert result.weighted_error > 0.8
    assert result.weighted_median_error == pytest.approx(0.0)


def test_coverage_counts_extrapolated_requests_against_the_model(tmp_path, index):
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (1000.0, 0, "ok"), (1500.0, 1, "ok")])
    result = costcheck.check(tmp_path, index=index)
    assert result.covered == pytest.approx(0.5)


def test_summary_says_when_requests_were_priced_by_extrapolation(tmp_path, index):
    """The grid measured 1 and 4; a run that lives at 2 got answers nobody measured.

    Saying so is the point. A cost model that silently answers for a concurrency it never
    visited looks exactly like one that did, and the difference is the whole reason F-23
    has a tolerance rather than a checkmark.
    """
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (1000.0, 0, "ok"), (1500.0, 1, "ok")])
    lines = "\n".join(costcheck.check(tmp_path, index=index).summary())
    assert "concurrency level(s) the grid never measured" in lines
    assert "1 request(s) fell on" in lines


def test_summary_names_the_cells_outside_tolerance(tmp_path, index):
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (9000.0, 0, "ok")])
    lines = "\n".join(costcheck.check(tmp_path, index=index).summary())
    assert "outside tolerance" in lines
    assert "cannot be closer to the hardware" in lines


def test_summary_is_quiet_when_the_model_predicts_its_own_hardware(tmp_path, index):
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (1000.0, 0, "ok")])
    lines = costcheck.check(tmp_path, index=index).summary()
    assert len(lines) == 1
    assert "weighted |error| 0.0%" in lines[0]


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_a_node_with_no_named_snapshot_raises(tmp_path, index):
    """There is no default cost model. A node that served requests under an unrecorded
    model cannot be scored, and guessing which one it was is how a wrong verdict gets
    published."""
    _make_run(tmp_path, snapshot_id=None)
    with pytest.raises(ValueError, match="names no cost-model snapshot"):
        costcheck.check(tmp_path, index=index)


def test_a_snapshot_that_was_never_committed_raises(tmp_path, index):
    _make_run(tmp_path, snapshot_id="cm_not_committed")
    with pytest.raises(ValueError, match="not committed"):
        costcheck.check(tmp_path, index=index)


def test_a_candidate_snapshot_overrides_every_node(tmp_path, index):
    """Scoring a newly measured table against runs that predate it.

    The runs cannot be replayed, so the only way to ask whether recalibrating would have
    helped is to price the old run's requests with the new model.
    """
    _make_run(
        tmp_path, snapshot_id="cm_not_committed", requests=[(1.0, 0, "ok"), (1000.0, 0, "ok")]
    )
    result = costcheck.check(tmp_path, snapshot=_snapshot("cm_new"), index=index)
    assert result.errors[0].relative_error == pytest.approx(0.0)


# --------------------------------------------------------------------------------------
# The tolerance is one number, held in two places
# --------------------------------------------------------------------------------------


def test_the_tolerance_matches_the_figure_that_draws_it():
    """`figures` deliberately imports nothing from `dataplane`, so the F-23 tolerance is
    written twice. This is the pin that keeps the diagnostic and the figure from quietly
    disagreeing about what "within tolerance" means — the same arrangement, and the same
    reason, as the two nearest-rank percentile functions."""
    assert costcheck.F23_TOLERANCE == plots.F23_TOLERANCE


def test_percentiles_match_the_load_band(tmp_path, index):
    """And the percentile convention is the load band's, not a third one."""
    from dataplane.pipeline import loadband

    values = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    rows = [
        {"node_id": "n1", "prompt_len": 100, "output_len": 32, "concurrency": 1, "service_ms": v}
        for v in values
    ]
    errors, _ = costcheck.cells(rows, {"n1": index["cm_test"]})
    assert errors[0].observed_p50_ms == loadband._percentile(values, 0.50)
    assert errors[0].observed_p95_ms == loadband._percentile(values, 0.95)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_cli_exits_nonzero_when_the_model_does_not_predict_its_hardware(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr(
        costcheck.runset, "snapshot_index", lambda *a, **k: {"cm_test": _snapshot()}
    )
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (9000.0, 0, "ok")])
    assert costcheck.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "outside" in out


def test_cli_exits_zero_when_it_does(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        costcheck.runset, "snapshot_index", lambda *a, **k: {"cm_test": _snapshot()}
    )
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (1000.0, 0, "ok")])
    assert costcheck.main([str(tmp_path)]) == 0
    assert "weighted |error|" in capsys.readouterr().out


def test_cli_scores_a_candidate_snapshot_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(costcheck.runset, "snapshot_index", lambda *a, **k: {})
    _make_run(tmp_path / "runs", requests=[(1.0, 0, "ok"), (1000.0, 0, "ok")])
    candidate = tmp_path / "cm_new.json"
    candidate.write_text(json.dumps(_snapshot("cm_new")))
    assert costcheck.main([str(tmp_path / "runs"), "--snapshot", str(candidate)]) == 0
    assert "cm" not in capsys.readouterr().err


def test_cli_tolerance_is_configurable(tmp_path, monkeypatch):
    """The default is F-23's, but a tighter one is how a candidate model gets compared."""
    monkeypatch.setattr(
        costcheck.runset, "snapshot_index", lambda *a, **k: {"cm_test": _snapshot()}
    )
    _make_run(tmp_path, requests=[(1.0, 0, "ok"), (1100.0, 0, "ok")])
    assert costcheck.main([str(tmp_path)]) == 0
    assert costcheck.main([str(tmp_path), "--tolerance", "0.05"]) == 1
