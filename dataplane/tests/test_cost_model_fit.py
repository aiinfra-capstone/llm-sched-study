"""C-3 fitting (F-6, F-7) — the artifact both halves of the project meet on.

A wrong number here is a wrong number in Aditya's scheduler and in his DES, so the
assertions are about the things that would be silently wrong rather than loudly broken:
failures averaged into a cell mean, a bucket edge that swallows a length it should have
refused, a `tokens_per_s` that moves when prompt length does.

The schema check is done against `contracts/schemas/cost_model.schema.json` itself rather
than against a copy, because the point of a contract is that there is one of it.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import jsonschema
import pytest

from dataplane.calibration import cost_model as cm

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/schemas/cost_model.schema.json").read_text()
)

_PROVENANCE = {
    "engine": "llamacpp",
    "engine_version": "b10569+cuda13.2",
    "quant": "Q4_K_M",
    "gpu": "NVIDIA GeForce GTX 1650 Ti",
    "driver": "580.173.02",
    "prefix_caching": False,
    "engine_config": {"ngl": 20, "threads": 6, "parallel": 4},
}
_ADMISSIBILITY = {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000}
_STOCHASTIC = {
    "model": "lognormal_multiplier",
    "sigma": 0.11,
    "autocorr_time_s": 42.0,
    "fit_r2": 0.87,
}


def _obs(
    prompt_len=256, output_len=48, concurrency=1, service_ms=1000.0, tokens=48, t=0, status="ok"
):
    return cm.Observation(
        prompt_len=prompt_len,
        output_len=output_len,
        concurrency=concurrency,
        service_ns=int(service_ms * 1e6),
        output_tokens=tokens,
        t_end_ns=t,
        status=status,
        prefill_ns=int(0.2 * service_ms * 1e6),
        decode_ns=int(0.8 * service_ms * 1e6),
    )


def _fit(observations, **over):
    kwargs = {
        "node_class": "gtx1650ti_ngl20_p4_q4km_llama3_8b",
        "prompt_edges": [1, 128, 512, 2048],
        "output_edges": [1, 64, 128, 256],
        "provenance": _PROVENANCE,
        "admissibility": _ADMISSIBILITY,
        "calibration_run_ids": ["cal_0001"],
        "stochastic": _STOCHASTIC,
        "measured_at_unix": 1788077068,
    } | over
    return cm.build_snapshot(observations, **kwargs)


def test_a_fitted_snapshot_validates_against_the_c3_schema() -> None:
    snapshot = _fit([_obs(service_ms=900 + 10 * i, t=i) for i in range(8)])
    jsonschema.validate(snapshot, _SCHEMA)

    assert snapshot["form"] == cm.FORM == "lookup_table"
    assert snapshot["cost_model_schema"] == 1
    assert snapshot["snapshot_id"].startswith("cm_gtx1650ti_ngl20_p4_q4km_llama3_8b_")


def test_the_form_decision_is_a_constant_not_an_option() -> None:
    """Week 2 commits to one form. An option is a thing that gets set differently twice."""
    assert cm.FORM == "lookup_table"
    # `fit` takes no `form` argument at all — there is no call site that could set it to
    # anything else, which is the difference between a decision and a default.
    assert "form" not in inspect.signature(cm.build_snapshot).parameters
    assert _fit([_obs(t=i) for i in range(4)])["form"] == "lookup_table"


def test_cells_are_keyed_on_bucket_pair_and_concurrency() -> None:
    observations = [
        _obs(prompt_len=64, concurrency=1, t=1),
        _obs(prompt_len=64, concurrency=1, t=2),
        _obs(prompt_len=64, concurrency=4, t=3),
        _obs(prompt_len=256, concurrency=4, t=4),
    ]
    entries = _fit(observations)["entries"]

    keys = {
        (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"]) for e in entries
    }
    assert keys == {
        ((1, 128), (1, 64), 1),
        ((1, 128), (1, 64), 4),
        ((129, 512), (1, 64), 4),
    }
    assert sum(e["n_samples"] for e in entries) == 4


def test_failures_are_excluded_rather_than_averaged_in() -> None:
    """A timeout is a censored observation. Averaging the ceiling in would report my own
    `--timeout` setting as the node's speed."""
    good = [_obs(service_ms=1000, t=i) for i in range(4)]
    bad = [_obs(service_ms=60000, tokens=0, t=9, status="timeout")]
    snapshot = _fit(good + bad)

    (entry,) = snapshot["entries"]
    assert entry["n_samples"] == 4
    assert entry["service_ms_mean"] == pytest.approx(1000.0)

    with pytest.raises(ValueError, match="would report the timeout ceiling"):
        _fit(bad)


def test_tokens_per_s_is_decode_throughput_not_end_to_end() -> None:
    """R is a ratio of these numbers, so it must not move when the workload's prompts do.

    Both cells decode 48 tokens in 800 ms. Only the prompt length differs, and a
    service-time-based tok/s would make the long-prompt cell look like a slower node.
    """
    short = [_obs(prompt_len=64, service_ms=1000, t=i) for i in range(3)]
    long = [_obs(prompt_len=256, service_ms=1000, t=i + 10) for i in range(3)]
    entries = {tuple(e["prompt_bucket"]): e for e in _fit(short + long)["entries"]}

    assert entries[(1, 128)]["tokens_per_s"] == pytest.approx(60.0)
    assert entries[(129, 512)]["tokens_per_s"] == pytest.approx(60.0)


def test_a_cell_with_no_measurable_decode_reports_zero_rather_than_crashing() -> None:
    """A `partial` backend gives no split; the table still fits, tok/s just goes unstated."""
    obs = [
        cm.Observation(
            prompt_len=64,
            output_len=48,
            concurrency=1,
            service_ns=10**9,
            output_tokens=48,
            t_end_ns=i,
            status="ok",
        )
        for i in range(3)
    ]
    (entry,) = _fit(obs)["entries"]
    assert entry["tokens_per_s"] == 0.0
    assert entry["service_ms_mean"] == pytest.approx(1000.0)


def test_buckets_are_inclusive_and_leave_no_gap() -> None:
    assert cm.buckets_from_edges([1, 128, 512]) == [(1, 128), (129, 512)]
    assert cm.assign_bucket(128, [(1, 128), (129, 512)]) == (1, 128)
    assert cm.assign_bucket(129, [(1, 128), (129, 512)]) == (129, 512)

    with pytest.raises(ValueError, match="at least 2 edges"):
        cm.buckets_from_edges([1])
    with pytest.raises(ValueError, match="strictly increasing"):
        cm.buckets_from_edges([1, 512, 128])


def test_a_length_outside_the_grid_is_an_admissibility_question_not_a_clamp() -> None:
    """Clamping is how a categorical cliff (F-15) gets averaged into a latency tail."""
    with pytest.raises(ValueError, match="admissibility question"):
        cm.assign_bucket(4096, [(1, 128), (129, 512)])


def test_snapshot_id_is_deterministic_in_its_inputs() -> None:
    assert cm.snapshot_id("n1", 1788077068) == cm.snapshot_id("n1", 1788077068)
    assert cm.snapshot_id("n1", 1788077068) != cm.snapshot_id("n1", 1788077069)
    assert cm.snapshot_id("n1", 1788077068).endswith("Z")


def test_lookup_returns_the_cell_and_the_nearest_measured_concurrency() -> None:
    snapshot = _fit(
        [_obs(concurrency=1, service_ms=1000, t=1), _obs(concurrency=4, service_ms=3000, t=2)]
    )
    assert cm.predict_service_ms(snapshot, 256, 48, 1) == pytest.approx(1000.0)
    assert cm.predict_service_ms(snapshot, 256, 48, 4) == pytest.approx(3000.0)
    # Off-grid concurrency snaps to the nearest measured one; lengths never interpolate.
    assert cm.predict_service_ms(snapshot, 256, 48, 3) == pytest.approx(3000.0)


def test_lookup_refuses_a_bucket_pair_the_grid_never_measured() -> None:
    snapshot = _fit(
        [_obs(prompt_len=64, output_len=48, t=1), _obs(prompt_len=256, output_len=100, t=2)]
    )
    with pytest.raises(ValueError, match="unmeasured"):
        cm.predict_service_ms(snapshot, 64, 100, 1)


def test_residuals_are_measured_against_the_prediction_the_scheduler_would_make() -> None:
    """F-22's sigma is the error around the cost model, not the raw spread of the grid."""
    observations = [_obs(service_ms=900, t=1), _obs(service_ms=1100, t=2)]
    snapshot = _fit(observations)
    obs_ms, pred_ms = cm.residuals(observations + [_obs(t=3, status="oom")], snapshot)

    assert obs_ms == pytest.approx([900.0, 1100.0])  # the oom is not a residual
    assert pred_ms == pytest.approx([1000.0, 1000.0])
