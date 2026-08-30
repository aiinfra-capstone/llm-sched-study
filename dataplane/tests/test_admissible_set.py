"""F-13 / F-15 — determining the admissible set from real calibration output.

`determine` already knows how to intersect envelopes; what is tested here is the step in
front of it, which is where the mistakes that matter live. An envelope is read off a
bucket named by its **ceiling** and sampled in its **interior**, and it is drawn from
whichever snapshot the loader picked out of a time-ordered series. Both of those are
places where a number can look measured without being measured, so both are asserted.
"""

from __future__ import annotations

import json

import pytest

from dataplane.calibration import admissible, cost_model

PROVENANCE = {
    "engine": "llamacpp",
    "engine_version": "b10569+cuda13.2",
    "quant": "Q4_K_M",
    "gpu": "NVIDIA GeForce GTX 1650 Ti",
    "driver": "580.173.02",
    "prefix_caching": False,
    "engine_config": {"ngl": 99, "threads": 6, "parallel": 4},
}
STOCHASTIC = {
    "model": "lognormal_multiplier",
    "sigma": 0.03,
    "autocorr_time_s": 10.0,
    "fit_r2": 0.4,
}


def _obs(prompt_len, output_len, concurrency, service_ms, *, status="ok", t=0):
    return cost_model.Observation(
        prompt_len=prompt_len,
        output_len=output_len,
        concurrency=concurrency,
        service_ns=int(service_ms * 1e6),
        output_tokens=output_len if status == "ok" else 0,
        t_end_ns=1_000_000_000 + t,
        status=status,
        prefill_ns=int(service_ms * 1e6 * 0.2),
        decode_ns=int(service_ms * 1e6 * 0.8),
    )


def _write_run(
    root,
    node_class,
    observations,
    *,
    model="Llama-3.2-1B-Instruct",
    prompt_edges=(1, 128, 512),
    output_edges=(1, 64, 128),
    ceiling_ms=60_000,
    n_snapshots=2,
    host="fedora",
):
    """A calibration run directory in the shape the campaign writes one."""
    run_dir = root / f"cal_{node_class}_1788000000"
    (run_dir / "snapshots").mkdir(parents=True)
    (run_dir / "campaign.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "node_class": node_class,
                "model": model,
                "host": host,
                "engine_config": PROVENANCE["engine_config"],
                "headline_tokens_per_s": 70.0,
            }
        )
    )
    (run_dir / "observations.jsonl").write_text(
        "".join(
            json.dumps({**vars(o), "segment": "grid"}, separators=(",", ":")) + "\n"
            for o in observations
        )
    )
    admissibility = {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": ceiling_ms}
    for i in range(n_snapshots):
        measured_at = 1788000000 + i * 30
        snapshot = cost_model.build_snapshot(
            observations,
            node_class=node_class,
            prompt_edges=list(prompt_edges),
            output_edges=list(output_edges),
            provenance=PROVENANCE,
            admissibility=admissibility,
            calibration_run_ids=[run_dir.name],
            stochastic=STOCHASTIC,
            measured_at_unix=measured_at,
        )
        snapshot["snapshot_id"] = cost_model.snapshot_id(node_class, measured_at, seq=i)
        (run_dir / "snapshots" / f"{i:03d}.json").write_text(json.dumps(snapshot))
    return run_dir


FAST = [
    _obs(64, 32, 1, 400.0),
    _obs(64, 32, 4, 900.0),
    _obs(256, 96, 1, 1500.0),
    _obs(256, 96, 4, 3000.0),
]


def test_observations_round_trip_without_the_campaigns_own_bookkeeping(tmp_path) -> None:
    """`segment` is the campaign's note to itself, not part of a sample. Passing it through
    would break the moment the campaign learns to record something else about a segment."""
    run = _write_run(tmp_path, "gpu_a", FAST)
    loaded = admissible.load_observations(run)
    assert len(loaded) == len(FAST)
    assert loaded[0].prompt_len == 64
    assert not hasattr(loaded[0], "segment")


def test_the_envelope_reports_the_ceiling_and_the_evidence_separately(tmp_path) -> None:
    """A bucket is named by its ceiling and sampled in its interior. The `(129, 512)` bucket
    admitted on samples at 256 is a claim about 512 that nothing in the campaign tested, and
    the envelope has to be able to say so."""
    run = _write_run(tmp_path, "gpu_a", FAST)
    envelope = admissible.pool_envelope([run])

    assert envelope["max_prompt"] == 512
    assert envelope["max_output"] == 128
    assert envelope["evidence"][0]["max_prompt_measured"] == 256
    assert envelope["unmeasured_ceiling"] == [
        {"node_class": "gpu_a", "claimed_prompt": 512, "measured_prompt": 256}
    ]
    assert envelope["buckets"] == ["p128_o64", "p256_o64", "p512_o128"]


def test_a_bucket_whose_p95_blows_the_ceiling_is_outside_the_set(tmp_path) -> None:
    """p95, not the mean: a bucket that fits on average times out one request in twenty, and
    those timeouts land in exactly the tail statistics the study is about."""
    slow = [
        _obs(64, 32, 1, 400.0),
        _obs(64, 32, 4, 900.0),
        _obs(256, 96, 1, 9_000.0),
        _obs(256, 96, 4, 11_000.0),
    ]
    run = _write_run(tmp_path, "gpu_slow", slow, ceiling_ms=5_000)
    envelope = admissible.pool_envelope([run])
    assert envelope["max_prompt"] == 128
    assert envelope["max_output"] == 64
    assert envelope["per_node"][0]["worst_p95_ms"] > 5_000


def test_the_slowest_node_sets_the_pools_range(tmp_path) -> None:
    """F-13 is an intersection. One CPU node that cannot do long prompts shrinks the whole
    study's range, and the shrunk range is what gets reported."""
    fast = _write_run(tmp_path / "a", "gpu_a", FAST)
    slow = _write_run(
        tmp_path / "b",
        "cpu_b",
        [
            _obs(64, 32, 1, 3_000.0),
            _obs(64, 32, 4, 4_000.0),
            _obs(256, 96, 1, 40_000.0),
            _obs(256, 96, 4, 90_000.0),
        ],
    )
    envelope = admissible.pool_envelope([fast, slow])
    assert envelope["max_prompt"] == 128
    assert envelope["limiting_node"] == "cpu_b"
    assert len(envelope["per_node"]) == 2


def test_the_cliff_comes_from_the_samples_the_cost_model_threw_away(tmp_path) -> None:
    """A fitted table cannot say where the cliff is — it excludes failures by construction.
    This is what those discarded samples are for (F-15)."""
    run = _write_run(
        tmp_path,
        "cpu_b",
        [*FAST, _obs(256, 96, 4, 60_000.0, status="timeout"), _obs(256, 96, 4, 0.0, status="oom")],
    )
    cliff = admissible.pool_envelope([run])["cliffs"][0]
    assert cliff["n_failed"] == 2
    assert cliff["failures_by_status"] == {"oom": 1, "timeout": 1}
    assert cliff["first_failing_prompt"] == 256


def test_an_envelope_across_two_models_describes_no_pool_this_study_can_run(tmp_path) -> None:
    """F-9 holds the model constant across the pool, so intersecting two models' envelopes
    would produce a range for a pool that cannot exist."""
    a = _write_run(tmp_path / "a", "gpu_a", FAST, model="Llama-3.2-1B-Instruct")
    b = _write_run(tmp_path / "b", "gpu_b", FAST, model="Meta-Llama-3-8B-Instruct")
    with pytest.raises(ValueError, match="span 2 models"):
        admissible.pool_envelope([a, b])


def test_a_run_with_no_snapshot_states_no_service_times(tmp_path) -> None:
    run = _write_run(tmp_path, "gpu_a", FAST, n_snapshots=0)
    with pytest.raises(ValueError, match="no C-3 snapshots"):
        admissible.pool_envelope([run])


def test_an_empty_pool_is_not_an_intersection() -> None:
    with pytest.raises(ValueError, match="got 0 calibration runs"):
        admissible.pool_envelope([])


def test_cli_writes_the_envelope_and_names_what_it_could_not_measure(tmp_path, capsys) -> None:
    _write_run(tmp_path / "runs", "gpu_a", FAST)
    _write_run(
        tmp_path / "runs2",
        "cpu_b",
        [*FAST, _obs(256, 96, 4, 60_000.0, status="timeout")],
    )
    root = tmp_path / "runs"
    out = tmp_path / "admissible.json"
    assert admissible.main([str(root), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "admissible set: prompt <= 512" in printed
    assert "the bucket ceiling is claimed, not measured" in printed
    written = json.loads(out.read_text())
    assert written["model"] == "Llama-3.2-1B-Instruct"
    assert written["buckets"]

    assert admissible.main([str(tmp_path / "runs2")]) == 0
    assert "CLIFF: cpu_b failed 1/5" in capsys.readouterr().out


def test_cli_ceiling_override_tightens_the_set_without_recalibrating(tmp_path, capsys) -> None:
    """A pool can be run against a stricter timeout than it was calibrated at; the envelope
    has to follow, and the buckets line has to be able to say 'none'."""
    root = tmp_path / "runs"
    _write_run(root, "gpu_a", FAST)
    assert admissible.main([str(root), "--ceiling-ms", "1000"]) == 0
    assert "at a 1000 ms ceiling" in capsys.readouterr().out


def test_cli_refuses_to_assume_an_admissible_set(tmp_path) -> None:
    """The admissible set is measured. A directory with no campaign in it is not an answer."""
    with pytest.raises(SystemExit, match="measured, not"):
        admissible.main([str(tmp_path)])
