"""C-5 join and the F-13 admissible set.

The join's failure mode is not a crash — it is a set of rows that look perfectly
well-formed and describe a run that never happened. So most of these tests are about the
refusals, and the rest are about which columns are allowed to be zero.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from dataplane.calibration import admissible
from dataplane.calibration.cost_model import Observation
from dataplane.pipeline import join as join_mod

_MANIFEST = {
    "run_id": "run_0142",
    "policy": "wjsq",
    "lambda": 3.5,
    "staleness_s": 0.0,
    "warmup_s": 30.0,
    "trace_sha256": "a" * 64,
    "validity": {"valid": True},
    "nodes": [
        {"node_id": "n1", "host": "boxA", "role": "pool"},
        {"node_id": "n2", "host": "boxB", "role": "pool"},
        {"node_id": "probe", "host": "boxA", "role": "engine_gap_probe"},
    ],
}
_TRACE = [
    {"record": "header"},
    {
        "record": "req",
        "req_id": "r1",
        "prompt_len": 512,
        "output_len": 128,
        "bucket_id": "p512_o128",
        "priority": 0,
    },
]
_CLIENT = [
    {
        "run_id": "run_0142",
        "req_id": "r1",
        "intended_offset_s": 40.0,
        "send_lag_ms": 1.2,
        "e2e_duration_ns": 2_500_000_000,
        "status": "ok",
        "responding_node": "n1",
    },
]
_SCHEDULER = [
    {
        "type": "decision",
        "run_id": "run_0142",
        "req_id": "r1",
        "decision_seq": 1,
        "policy": "wjsq",
        "staleness_param_s": 0.0,
        "decide_duration_ns": 120_000,
        "chosen_node": "n1",
        "tie_break_draw": 0.5,
        "candidates": [
            {
                "node_id": "n1",
                "queue_depth": 0,
                "inflight": 1,
                "capability_tok_s": 100.0,
                "estimate_age_ms": 40,
                "admissible": True,
                "score": 1.0,
            },
            {
                "node_id": "n2",
                "queue_depth": 3,
                "inflight": 4,
                "capability_tok_s": 20.0,
                "estimate_age_ms": 55,
                "admissible": True,
                "score": 0.2,
            },
        ],
    },
]
_WORKER = [
    {
        "run_id": "run_0142",
        "req_id": "r1",
        "node_id": "n1",
        "engine": "llamacpp",
        "queue_wait_ns": 88_000_000,
        "service_ns": 2_000_000_000,
        "prompt_tokens": 512,
        "output_tokens": 128,
        "batch_size_at_admission": 1,
        "inflight_at_admission": 1,
        "status": "ok",
        "prefill_ns": 210_000_000,
        "decode_ns": 1_790_000_000,
    },
]


def _join(**over):
    kwargs = {
        "manifest": _MANIFEST,
        "trace": _TRACE,
        "client": _CLIENT,
        "scheduler": _SCHEDULER,
        "worker": _WORKER,
    } | over
    return join_mod.join(**kwargs)


def test_the_residual_is_what_the_three_hosts_did_not_account_for() -> None:
    """One honest residual, never decomposed into invented stages."""
    row = _join()[0]
    assert row["e2e_ms"] == pytest.approx(2500.0)
    assert row["service_ms"] == pytest.approx(2000.0)
    assert row["queue_wait_ms"] == pytest.approx(88.0)
    assert row["transport_residual_ms"] == pytest.approx(2500.0 - (0.12 + 88.0 + 2000.0))
    assert row["is_warmup"] is False  # offset 40 > warmup 30


def test_the_best_alternative_is_the_schedulers_own_view() -> None:
    """`routing_error_ms` measures decision quality given the information available — which
    is the quantity H3 is about — not what the other node would really have done."""
    row = _join()[0]
    assert row["best_alt_node"] == "n2"
    # n2: (3 queued + 1) * 128 tokens / 20 tok/s = 25.6 s; n1: 1 * 128 / 100 = 1.28 s.
    assert row["best_alt_est_service_ms"] == pytest.approx(25600.0)
    assert row["routing_error_ms"] == 0.0  # it chose the better node
    assert row["chosen_queue_depth"] == 0 and row["chosen_est_age_ms"] == 40


def test_a_missing_scheduler_record_is_null_not_zero() -> None:
    """A zero `decide_us` is a measurement of an instantaneous decision and a zero
    `chosen_queue_depth` says the node was idle. Nobody made either claim."""
    row = _join(scheduler=[])[0]
    assert row["decide_us"] is None
    assert row["chosen_node"] is None
    assert row["chosen_queue_depth"] is None
    assert row["routing_error_ms"] is None


def test_probe_node_rows_never_reach_the_record_set() -> None:
    """F-9b: the probe is a measured condition and appears in no policy comparison."""
    worker = [{**_WORKER[0], "node_id": "probe"}]
    row = _join(worker=worker)[0]
    assert row["service_ms"] == 0.0  # its worker record was dropped before the join
    assert row["node_count"] == 2  # the probe is not counted as a pool member


def test_logs_from_two_different_runs_are_refused() -> None:
    with pytest.raises(ValueError, match="different runs"):
        _join(worker=[{**_WORKER[0], "run_id": "run_0143"}])


def test_a_manifest_naming_another_trace_is_refused() -> None:
    with pytest.raises(ValueError, match="not the one this run replayed"):
        _join(trace_sha256="b" * 64)


def test_an_invalid_run_is_refused_unless_asked_for() -> None:
    bad = {**_MANIFEST, "validity": {"valid": False}}
    with pytest.raises(ValueError, match="marked invalid"):
        _join(manifest=bad)
    assert len(_join(manifest=bad, allow_invalid=True)) == 1


def test_a_run_without_a_trace_still_joins() -> None:
    row = _join(trace=None)[0]
    assert row["prompt_len"] == 0 and row["bucket_id"] == ""


def test_nulls_survive_the_parquet_round_trip(tmp_path) -> None:
    """A `prefill_ms` that came back 0.0 would say the backend measured an instantaneous
    prefill rather than that it does not report one (F-18 partial)."""
    worker = [{k: v for k, v in _WORKER[0].items() if k not in {"prefill_ns", "decode_ns"}}]
    path = join_mod.write_parquet(_join(worker=worker), tmp_path / "joined.parquet")
    back = pd.read_parquet(path)

    assert back["prefill_ms"].isna().all()
    assert back["decode_ms"].isna().all()
    assert list(back.columns) == join_mod.COLUMNS


def test_the_summary_says_what_must_not_be_averaged() -> None:
    rows = _join(scheduler=[], worker=[])
    lines = join_mod.summarize(rows, _MANIFEST)
    assert any("no scheduler decision" in x for x in lines)
    assert any("must not be averaged" in x for x in lines)
    assert any("engine-gap-probe" in x for x in lines)


def test_a_negative_residual_is_surfaced() -> None:
    slow = [{**_WORKER[0], "service_ns": 9_000_000_000}]
    lines = join_mod.summarize(_join(worker=slow), _MANIFEST)
    assert any("negative transport residual" in x for x in lines)


def test_cli_joins_a_run_directory(tmp_path, capsys) -> None:
    run = tmp_path / "run_0142"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps(_MANIFEST))
    (run / "client_run_0142.jsonl").write_text("\n".join(json.dumps(r) for r in _CLIENT))
    (run / "scheduler_run_0142.jsonl").write_text("\n".join(json.dumps(r) for r in _SCHEDULER))
    (run / "worker_n1_run_0142.jsonl").write_text("\n".join(json.dumps(r) for r in _WORKER))
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(r) for r in _TRACE) + "\n\n")

    assert join_mod.main([str(run), "--trace", str(trace), "--r", "12.5"]) == 0
    assert (run / "joined.parquet").exists()
    assert "1 rows" in capsys.readouterr().out
    assert pd.read_parquet(run / "joined.parquet")["R"].iloc[0] == pytest.approx(12.5)


def test_cli_returns_nonzero_for_an_invalid_run(tmp_path, capsys) -> None:
    run = tmp_path / "run_bad"
    run.mkdir()
    (run / "manifest.json").write_text(json.dumps({**_MANIFEST, "validity": {"valid": False}}))
    (run / "client_run_0142.jsonl").write_text(json.dumps(_CLIENT[0]))
    trace = tmp_path / "t.jsonl"
    trace.write_text(json.dumps(_TRACE[1]))

    assert join_mod.main([str(run), "--trace", str(trace), "--force"]) == 1
    assert "do not analyse it" in capsys.readouterr().out


# ---------------------------------------------------------------- F-13 / F-15


def test_the_slow_node_binds_the_envelope() -> None:
    envelope = admissible.determine(
        [
            {
                "node_class": "fast",
                "max_prompt": 2048,
                "max_output": 256,
                "timeout_ceiling_ms": 60000,
            },
            {
                "node_class": "slow",
                "max_prompt": 512,
                "max_output": 128,
                "timeout_ceiling_ms": 30000,
            },
        ]
    )
    assert (envelope["max_prompt"], envelope["max_output"]) == (512, 128)
    # The tightest ceiling in the pool, not the loosest: nobody asked the fast node
    # whether it could do the work in 30 s.
    assert envelope["timeout_ceiling_ms"] == 30000
    assert envelope["limiting_node"] == "slow"
    assert "limited by slow" in admissible.summary(envelope)


def test_a_pool_with_no_common_envelope_raises() -> None:
    with pytest.raises(ValueError, match="no common admissible envelope"):
        admissible.determine([{"max_prompt": 0, "max_output": 0, "timeout_ceiling_ms": 1000}])
    with pytest.raises(ValueError, match="got 0 nodes"):
        admissible.determine([])


def test_proposed_buckets_all_fit_the_envelope_they_came_from() -> None:
    """The loop that closes Week 1 against Week 3: gen_trace refuses anything outside."""
    envelope = {"max_prompt": 1024, "max_output": 128, "timeout_ceiling_ms": 60000}
    buckets = admissible.buckets_within(envelope)

    assert buckets and "p2048_o256" not in buckets
    for b in buckets:
        p, o = b.removeprefix("p").split("_o")
        assert int(p) <= 1024 and int(o) <= 128
    assert admissible.buckets_within({"max_prompt": 1, "max_output": 1}) == []


def test_a_snapshot_becomes_an_envelope_on_p95_at_every_concurrency() -> None:
    """A bucket that fits at concurrency 1 and blows the ceiling at 4 is not admissible —
    the scheduler will absolutely put four requests on that node under load."""
    snapshot = {
        "node_class": "n1",
        "entries": [
            {
                "prompt_bucket": [1, 512],
                "output_bucket": [1, 128],
                "concurrency": 1,
                "service_ms_p95": 900.0,
            },
            {
                "prompt_bucket": [1, 512],
                "output_bucket": [1, 128],
                "concurrency": 4,
                "service_ms_p95": 2500.0,
            },
            {
                "prompt_bucket": [513, 2048],
                "output_bucket": [1, 128],
                "concurrency": 1,
                "service_ms_p95": 1500.0,
            },
            {
                "prompt_bucket": [513, 2048],
                "output_bucket": [1, 128],
                "concurrency": 4,
                "service_ms_p95": 9000.0,
            },
        ],
    }
    envelope = admissible.node_limit_from_snapshot(snapshot, ceiling_ms=3000)
    assert (envelope["max_prompt"], envelope["max_output"]) == (512, 128)
    assert envelope["limiting_concurrency"] == 4
    assert envelope["worst_p95_ms"] == pytest.approx(9000.0)


def test_the_cliff_is_read_from_the_samples_the_cost_model_discards() -> None:
    """A fitted table cannot tell you where the cliff is, by construction — it excludes
    exactly the failures that locate it."""
    obs = [
        Observation(64, 32, 1, 10**9, 32, 1, status="ok"),
        Observation(2048, 256, 4, 60 * 10**9, 0, 2, status="timeout"),
        Observation(1024, 256, 4, 10**9, 0, 3, status="oom"),
    ]
    cliff = admissible.cliff_from_observations(obs, node_class="cpu_ngl0")

    assert cliff.n_failed == 2 and cliff.failure_rate == pytest.approx(2 / 3)
    assert cliff.failures_by_status == {"oom": 1, "timeout": 1}
    assert cliff.first_failing_prompt == 1024  # the near edge, not where it becomes certain
    assert cliff.to_dict()["node_class"] == "cpu_ngl0"
    assert admissible.cliff_from_observations([], node_class="n").failure_rate == 0.0


def test_anchors_are_empty_until_the_week3_runs_exist(tmp_path) -> None:
    """F-23's anchors are live hardware runs at three operating points. Returning an empty
    list is the correct answer before they are collected — validating a simulator against
    nothing is worse than not validating it."""
    assert admissible.load_anchors(tmp_path / "nope") == []

    good = tmp_path / "anchors" / "run_a"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text(
        json.dumps(
            {"run_id": "a", "vehicle": "hardware", "trace_sha256": "x", "validity": {"valid": True}}
        )
    )
    assert len(admissible.load_anchors(tmp_path / "anchors")) == 1

    bad = tmp_path / "anchors" / "run_b"
    bad.mkdir()
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "b",
                "vehicle": "hardware",
                "trace_sha256": "x",
                "validity": {"valid": False},
            }
        )
    )
    with pytest.raises(ValueError, match="marked invalid"):
        admissible.load_anchors(tmp_path / "anchors")


def test_anchors_replaying_different_traces_are_refused(tmp_path) -> None:
    root = tmp_path / "anchors"
    for name, sha in (("a", "x"), ("b", "y")):
        d = root / f"run_{name}"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": name,
                    "vehicle": "hardware",
                    "trace_sha256": sha,
                    "validity": {"valid": True},
                }
            )
        )
    with pytest.raises(ValueError, match="identical replayed traces"):
        admissible.load_anchors(root)


def test_the_worst_offender_is_the_one_reported(tmp_path) -> None:
    """Two buckets miss the ceiling; `worst_p95_ms` must name the worse of them, not the
    last one seen."""
    snapshot = {
        "node_class": "n1",
        "entries": [
            {
                "prompt_bucket": [1, 512],
                "output_bucket": [1, 128],
                "concurrency": 4,
                "service_ms_p95": 9000.0,
            },
            {
                "prompt_bucket": [513, 2048],
                "output_bucket": [1, 128],
                "concurrency": 2,
                "service_ms_p95": 5000.0,
            },
        ],
    }
    envelope = admissible.node_limit_from_snapshot(snapshot, ceiling_ms=1000)
    assert envelope["worst_p95_ms"] == pytest.approx(9000.0)
    assert envelope["limiting_concurrency"] == 4
    assert (envelope["max_prompt"], envelope["max_output"]) == (0, 0)


def test_a_candidate_with_no_capability_cannot_be_an_alternative() -> None:
    """A node the scheduler has no throughput estimate for is not a counterfactual — it is
    an unknown, and pricing it at zero tok/s would make it look infinitely slow."""
    scheduler = [
        {
            **_SCHEDULER[0],
            "candidates": [
                _SCHEDULER[0]["candidates"][0],
                {**_SCHEDULER[0]["candidates"][1], "capability_tok_s": 0.0},
            ],
        }
    ]
    row = _join(scheduler=scheduler)[0]
    assert row["best_alt_node"] is None
    assert row["routing_error_ms"] is None


def test_a_pool_with_no_probe_says_nothing_about_probes() -> None:
    manifest = {**_MANIFEST, "nodes": _MANIFEST["nodes"][:2]}
    lines = join_mod.summarize(_join(manifest=manifest), manifest)
    assert not any("engine-gap-probe" in x for x in lines)


@pytest.mark.parametrize("module", ["dataplane.pipeline.join", "dataplane.harness.launch"])
def test_entry_points_are_runnable_as_modules(module: str, monkeypatch) -> None:
    import runpy

    monkeypatch.setattr("sys.argv", [module, "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module(module, run_name="__main__")
    assert exc.value.code == 0
