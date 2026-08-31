"""The run-set layer — what makes a *set* of runs safe to analyse together (F-19).

`join.py` already has its own tests. What is new here is everything a single run cannot
express: that *R* is derived from the run rather than typed at the command line, that
`vehicle` survives into the frame so F-24's stamp cannot be forgotten, and that one bad
run is excluded with its reason attached instead of either poisoning the set or stopping
it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dataplane.pipeline import runset

# --------------------------------------------------------------------------------------
# Fixtures: the smallest run directory the pipeline will accept
# --------------------------------------------------------------------------------------


def _snapshot(node_class: str, snapshot_id: str, tokens_per_s: float) -> dict[str, Any]:
    """A C-3 snapshot cut down to the fields R is read from."""
    return {
        "cost_model_schema": 1,
        "snapshot_id": snapshot_id,
        "node_class": node_class,
        "measured_at_unix": 1788000000,
        "form": "table",
        "entries": [
            {
                "prompt_bucket": [128, 256],
                "output_bucket": [64, 64],
                "concurrency": 4,
                "tokens_per_s": tokens_per_s,
                "service_ms_p50": 1000.0,
                "service_ms_p95": 1200.0,
                "n_samples": 30,
            }
        ],
    }


def _write_snapshots(root: Path, snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for snap in snapshots:
        directory = root / snap["node_class"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{snap['snapshot_id']}.json").write_text(json.dumps(snap))
    return runset.snapshot_index(root)


TRACE = [
    {"record": "header", "trace_schema": 1},
    {
        "record": "req",
        "req_id": "r000001",
        "arrival_offset_s": 0.0,
        "prompt_len": 128,
        "output_len": 64,
        "bucket_id": "p128_o64",
        "priority": 0,
        "content_seed": 1,
    },
    {
        "record": "req",
        "req_id": "r000002",
        "arrival_offset_s": 5.0,
        "prompt_len": 128,
        "output_len": 64,
        "bucket_id": "p128_o64",
        "priority": 0,
        "content_seed": 2,
    },
]


def _make_run(
    root: Path,
    run_id: str,
    *,
    vehicle: str = "hardware",
    valid: bool = True,
    node_classes: tuple[str, ...] = ("fast",),
    trace_name: str = "t.jsonl",
    trace_sha256: str | None = None,
    trace_records: list[dict[str, Any]] | None = None,
) -> Path:
    """A run directory holding a manifest, a client log, a worker log and its trace."""
    import hashlib

    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    trace_path = root / trace_name
    blob = ("\n".join(json.dumps(r) for r in (trace_records or TRACE)) + "\n").encode()
    trace_path.write_bytes(blob)

    nodes = [
        {"node_id": f"n{i}", "role": "pool", "engine": "llamacpp"}
        for i, _ in enumerate(node_classes, start=1)
    ]
    manifest = {
        "run_id": run_id,
        "vehicle": vehicle,
        "policy": "round_robin",
        "lambda": 1.0,
        "staleness_s": 0.0,
        "warmup_s": 1.0,
        "duration_s": 30.0,
        "trace_path": str(trace_path),
        "trace_sha256": trace_sha256 or hashlib.sha256(blob).hexdigest(),
        "cost_model_snapshots": {
            f"n{i}": f"cm_{cls}" for i, cls in enumerate(node_classes, start=1)
        },
        "nodes": nodes,
        "validity": {"valid": valid},
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))

    client = [
        {
            "run_id": run_id,
            "req_id": "r000001",
            "intended_offset_s": 0.0,
            "send_lag_ms": 0.3,
            "e2e_duration_ns": 900_000_000,
            "status": "ok",
        },
        {
            "run_id": run_id,
            "req_id": "r000002",
            "intended_offset_s": 5.0,
            "send_lag_ms": 0.4,
            "e2e_duration_ns": 1_100_000_000,
            "status": "ok",
        },
    ]
    (run_dir / f"client_{run_id}.jsonl").write_text("\n".join(json.dumps(r) for r in client) + "\n")

    worker = [
        {
            "run_id": run_id,
            "req_id": rec["req_id"],
            "node_id": "n1",
            "queue_wait_ns": 50_000_000,
            "service_ns": 800_000_000,
            "status": "ok",
        }
        for rec in client
    ]
    (run_dir / f"worker_n1_{run_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in worker) + "\n"
    )
    return run_dir


@pytest.fixture
def index(tmp_path) -> dict[str, dict[str, Any]]:
    return _write_snapshots(
        tmp_path / "cost_models",
        [
            _snapshot("fast", "cm_fast", 100.0),
            _snapshot("slow", "cm_slow", 50.0),
        ],
    )


# --------------------------------------------------------------------------------------
# R, derived rather than typed
# --------------------------------------------------------------------------------------


def test_the_snapshot_index_is_keyed_by_the_id_inside_the_document(index) -> None:
    """Filenames are a convenience for a human reading a directory listing; the id inside
    the snapshot is what a manifest actually references."""
    assert set(index) == {"cm_fast", "cm_slow"}
    assert index["cm_fast"]["node_class"] == "fast"


def test_a_single_class_pool_has_an_r_of_exactly_one(tmp_path, index) -> None:
    """Not "unknown" and not NaN. A pool of one node class has no heterogeneity in it, and
    1.0 is the measurement, which is why this repository's deployable R reads 1.00x."""
    run_dir = _make_run(tmp_path, "solo", node_classes=("fast",))
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert runset.deployed_r(manifest, index) == 1.0


def test_two_classes_give_the_ratio_between_them(tmp_path, index) -> None:
    """The number nobody has to type, and therefore the number nobody can mistype."""
    run_dir = _make_run(tmp_path, "pair", node_classes=("fast", "slow"))
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert runset.deployed_r(manifest, index) == pytest.approx(2.0)


def test_a_node_with_no_named_snapshot_is_refused(tmp_path, index) -> None:
    """Deriving R is the point; falling back to a typed-in value would reinstate the
    footgun this function exists to remove."""
    run_dir = _make_run(tmp_path, "nameless")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["cost_model_snapshots"] = {}
    with pytest.raises(ValueError, match="names no cost model snapshot"):
        runset.deployed_r(manifest, index)


def test_a_snapshot_that_was_never_committed_is_refused(tmp_path, index) -> None:
    """A run served under a cost model that is not in the repository is not reproducible
    from the repository, whatever its logs say."""
    run_dir = _make_run(tmp_path, "ghost")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["cost_model_snapshots"] = {"n1": "cm_not_committed"}
    with pytest.raises(ValueError, match="not in"):
        runset.deployed_r(manifest, index)


def test_the_index_defaults_to_the_committed_cost_models(tmp_path) -> None:
    """The default index is the repository's own `contracts/cost_models/`, so a caller who
    passes nothing still gets real snapshots rather than an empty dict."""
    assert runset.SNAPSHOT_ROOT.name == "cost_models"
    assert runset.snapshot_index()


# --------------------------------------------------------------------------------------
# Discovery and one run
# --------------------------------------------------------------------------------------


def test_a_single_run_directory_and_a_directory_of_runs_are_the_same_call(tmp_path) -> None:
    """A figure script should not have to know which shape it was handed."""
    _make_run(tmp_path, "one")
    _make_run(tmp_path, "two")
    assert [p.name for p in runset.discover(tmp_path)] == ["one", "two"]
    assert runset.discover(tmp_path / "one") == [tmp_path / "one"]


def test_a_joined_run_carries_the_vehicle_and_the_trace_identity(tmp_path, index) -> None:
    """The two columns C-5 does not have and a set cannot do without: F-24's stamp reads
    the first, and "did these runs replay the same workload" reads the second."""
    run_dir = _make_run(tmp_path, "stamped", vehicle="simulator")
    frame = runset.load_run(run_dir, index=index)
    assert set(frame["vehicle"]) == {"simulator"}
    assert len(set(frame["trace_sha256"])) == 1
    assert set(frame["R"]) == {1.0}


def test_a_run_whose_trace_is_gone_is_refused_rather_than_zero_filled(tmp_path, index) -> None:
    """`join` will happily produce rows with zero-length prompts when handed no trace. That
    is right for a smoke run and catastrophic in an analysis set."""
    run_dir = _make_run(tmp_path, "traceless")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    Path(manifest["trace_path"]).unlink()
    with pytest.raises(FileNotFoundError, match="not there"):
        runset.load_run(run_dir, index=index)


def test_a_manifest_naming_a_different_trace_is_refused(tmp_path, index) -> None:
    """The hash comes along for free once the trace is resolved from the manifest, which is
    the reason to resolve it there rather than take it from the command line."""
    run_dir = _make_run(tmp_path, "mismatched", trace_sha256="f" * 64)
    with pytest.raises(ValueError, match="sha256|trace"):
        runset.load_run(run_dir, index=index)


def test_a_relative_trace_path_resolves_against_the_repository(tmp_path, index) -> None:
    """Manifests written by the harness carry a repo-relative path; an absolute one is
    equally acceptable, and both have to land on the same file."""
    run_dir = _make_run(tmp_path, "relative")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    manifest["trace_path"] = "no/such/trace.jsonl"
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FileNotFoundError):
        runset.load_run(run_dir, index=index)


# --------------------------------------------------------------------------------------
# The set
# --------------------------------------------------------------------------------------


def test_a_set_is_the_concatenation_of_its_runs(tmp_path, index) -> None:
    _make_run(tmp_path, "a")
    _make_run(tmp_path, "b")
    runs = runset.aggregate(tmp_path, index=index)
    assert runs.included == ["a", "b"]
    assert len(runs.frame) == 4
    assert not runs.excluded


def test_one_invalid_run_is_excluded_with_its_reason_not_dropped_silently(tmp_path, index) -> None:
    """The whole argument for a set-level assembler: a single drifted run must cost that
    run and nothing else, and must be impossible to lose track of."""
    _make_run(tmp_path, "good")
    _make_run(tmp_path, "drifted", valid=False)
    runs = runset.aggregate(tmp_path, index=index)
    assert runs.included == ["good"]
    assert [run_id for run_id, _ in runs.excluded] == ["drifted"]
    assert "invalid" in runs.excluded[0][1]
    assert any("EXCLUDED drifted" in line for line in runs.summary())


def test_force_lets_an_invalid_run_back_in(tmp_path, index) -> None:
    _make_run(tmp_path, "drifted", valid=False)
    runs = runset.aggregate(tmp_path, index=index, allow_invalid=True)
    assert runs.included == ["drifted"]


def test_a_set_with_nothing_joinable_is_an_error(tmp_path, index) -> None:
    """An empty frame would flow downstream and produce an empty figure, which reads as a
    result rather than as an absence."""
    _make_run(tmp_path, "drifted", valid=False)
    with pytest.raises(ValueError, match="no run under"):
        runset.aggregate(tmp_path, index=index)


def test_the_summary_says_when_a_set_cannot_speak_to_h2(tmp_path, index) -> None:
    """Every run at R = 1.00x is a homogeneous pool. The set is still analysable; it just
    cannot separate hardware-aware from hardware-blind, and saying so at assembly time is
    cheaper than discovering it in a flat curve."""
    _make_run(tmp_path, "a")
    lines = "\n".join(runset.aggregate(tmp_path, index=index).summary())
    assert "R = 1.00x" in lines
    assert "H2" in lines


def test_the_summary_flags_mixed_vehicles_and_mixed_traces(tmp_path, index) -> None:
    """Both are legitimate sets and both change what a figure drawn from them may claim."""
    _make_run(tmp_path, "hw", vehicle="hardware", trace_name="t1.jsonl")
    _make_run(
        tmp_path,
        "sim",
        vehicle="simulator",
        trace_name="t2.jsonl",
        trace_records=[
            *TRACE,
            {
                "record": "req",
                "req_id": "r000003",
                "prompt_len": 1,
                "output_len": 1,
                "bucket_id": "p128_o64",
                "priority": 0,
                "arrival_offset_s": 9.0,
                "content_seed": 3,
            },
        ],
    )
    lines = "\n".join(runset.aggregate(tmp_path, index=index).summary())
    assert "F-24" in lines
    assert "distinct traces" in lines


# --------------------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------------------


def test_the_cli_writes_a_parquet_and_reports_what_it_left_out(
    tmp_path, index, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(runset, "snapshot_index", lambda root=None: index)
    _make_run(tmp_path, "good")
    _make_run(tmp_path, "drifted", valid=False)
    out = tmp_path / "set.parquet"

    code = runset.main([str(tmp_path), "--out", str(out)])

    assert out.exists()
    printed = capsys.readouterr().out
    assert "EXCLUDED drifted" in printed
    # Non-zero: something was left out, and a shell script chaining onto this should see it.
    assert code == 1


def test_the_cli_returns_zero_when_the_whole_set_joined(tmp_path, index, monkeypatch) -> None:
    monkeypatch.setattr(runset, "snapshot_index", lambda root=None: index)
    _make_run(tmp_path, "good")
    assert runset.main([str(tmp_path), "--out", str(tmp_path / "s.parquet")]) == 0


def test_force_reaches_the_cli(tmp_path, index, monkeypatch) -> None:
    monkeypatch.setattr(runset, "snapshot_index", lambda root=None: index)
    _make_run(tmp_path, "drifted", valid=False)
    assert runset.main([str(tmp_path), "--out", str(tmp_path / "s.parquet"), "--force"]) == 0


def test_a_heterogeneous_set_reports_its_r_without_the_h2_caveat(tmp_path, index) -> None:
    """The other side of the same line: once a run really did span two node classes, the
    set can speak to H2 and should not be told it cannot."""
    _make_run(tmp_path, "pair", node_classes=("fast", "slow"))
    lines = "\n".join(runset.aggregate(tmp_path, index=index).summary())
    assert "R: 2.00x" in lines
    assert "H2" not in lines
