"""tools/sweep.py: the simulator swept over R, staleness, load and policy.

The part that matters most is the load axis. A pool holding a slower node has less capacity,
so one `rate_scale` across the R axis walks up the utilisation curve as R grows, and a policy
difference read off it is part R and part load. A `pool_utilisation` point exists to hold
the distance from saturation fixed across R, so it has to resolve its own rate against each
R's synthesised pool. Nothing but a test checks that: the resolution sits inside `main`, and
until now `sweep.py` was in the coverage omit list.

SimApp is faked at `run_one_des` for the grid tests and at `subprocess.run` for
`run_one_des`'s own tests. The synthesised snapshots, the manifests and the rate resolution
are the real code.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from types import SimpleNamespace

import pool_load
import pytest
import sweep

from dataplane.harness import gen_trace

BASE = "cm_base_ngl99_p4_q4km_llama32_1b_x"


def _entry(pb, ob, c, service, prefill=None, decode=None, tok=100.0) -> dict:
    e = {
        "prompt_bucket": pb,
        "output_bucket": ob,
        "concurrency": c,
        "service_ms_mean": service,
        "service_ms_p50": service,
        "service_ms_p95": service * 1.1,
        "tokens_per_s": tok,
        "n_samples": 8,
    }
    if prefill is not None:
        e["prefill_ms_mean"] = prefill
        e["decode_ms_mean"] = decode
    return e


def _base_snapshot(split: bool = True) -> dict:
    cells = []
    for c, service in ((1, 400.0), (4, 800.0)):
        pre, dec = (service * 0.1, service * 0.85) if split else (None, None)
        cells.append(_entry([1, 128], [1, 64], c, service, pre, dec))
    return {
        "snapshot_id": BASE,
        "node_class": "base_ngl99_p4_q4km_llama32_1b",
        "measured_at_unix": 1,
        "entries": cells,
        "provenance": {"engine": "llamacpp"},
    }


# ------------------------------------------------------------------- synthesis


def test_a_uniform_factor_scales_every_time_and_keeps_the_ids_it_always_had() -> None:
    base = _base_snapshot()
    synth = sweep.synthesize_snapshot(base, 4.0)
    assert synth["snapshot_id"] == f"synth_{BASE}__x4"
    assert synth["node_class"] == "base_ngl99_p4_q4km_llama32_1b__synth_x4"
    e = synth["entries"][0]
    assert e["service_ms_mean"] == 1600.0
    assert e["service_ms_p95"] == pytest.approx(1760.0)
    assert e["prefill_ms_mean"] == 160.0 and e["decode_ms_mean"] == 1360.0
    assert e["tokens_per_s"] == 25.0
    assert synth["provenance"] == base["provenance"]
    assert base["entries"][0]["service_ms_mean"] == 400.0, "the base must not be mutated"


def test_a_phase_skew_splits_the_factor_between_prefill_and_decode() -> None:
    """Prefill scaled by factor / sqrt(s), decode by factor * sqrt(s): their ratio is s and
    their geometric mean is still the factor. Each cell's service time scales by what its
    own split implies, and decode tok/s answers to the decode factor alone."""
    synth = sweep.synthesize_snapshot(_base_snapshot(), 4.0, phase_skew=4.0)
    assert synth["snapshot_id"] == f"synth_{BASE}__x4_skew4"
    e = synth["entries"][0]
    assert e["prefill_ms_mean"] == pytest.approx(40.0 * 2.0)
    assert e["decode_ms_mean"] == pytest.approx(340.0 * 8.0)
    assert e["service_ms_mean"] == pytest.approx(400.0 * (40 * 2 + 340 * 8) / 380)
    assert e["tokens_per_s"] == pytest.approx(100.0 / 8.0)
    assert (e["decode_ms_mean"] / 340.0) / (e["prefill_ms_mean"] / 40.0) == pytest.approx(4.0)


def test_a_skew_is_refused_without_a_split_and_below_zero() -> None:
    with pytest.raises(ValueError, match="no prefill/decode split"):
        sweep.synthesize_snapshot(_base_snapshot(split=False), 2.0, phase_skew=2.0)
    with pytest.raises(ValueError, match="must be > 0"):
        sweep.synthesize_snapshot(_base_snapshot(), 2.0, phase_skew=0.0)
    # A uniform factor needs no split, and a cell without one keeps none.
    e = sweep.synthesize_snapshot(_base_snapshot(split=False), 2.0)["entries"][0]
    assert "prefill_ms_mean" not in e and e["service_ms_mean"] == 800.0


def test_snapshots_are_indexed_by_id(tmp_path) -> None:
    (tmp_path / "cls").mkdir()
    (tmp_path / "cls" / "a.json").write_text(json.dumps(_base_snapshot()))
    assert list(sweep.load_snapshots_by_id(tmp_path)) == [BASE]


# ----------------------------------------------------------------- manifests and helpers


def test_a_sweep_manifest_names_its_point_and_the_trace_it_hashed(tmp_path) -> None:
    base = {"config": {"arrival": {"lambda_base": 0.9}, "gen_seed": 1}, "policy": "round_robin"}
    trace = tmp_path / "t.jsonl"
    man = sweep.build_sweep_manifest(
        base, "wjsq", 0.5, 2.0, 4.0, {"n1": "a", "slow": "b"}, "f" * 64, trace, phase_skew=2.0
    )
    assert man["policy"] == "wjsq" and man["staleness_s"] == 0.5
    assert man["lambda"] == 1.8
    assert man["cost_model_snapshots"] == {"n1": "a", "slow": "b"}
    assert man["trace_sha256"] == "f" * 64 and man["trace_path"] == str(trace)
    assert man["config"] | {"gen_seed": 1} == {
        "arrival": {"lambda_base": 0.9},
        "gen_seed": 1,
        "staleness_s": 0.5,
        "policy": "wjsq",
        "R_target": 4.0,
        "phase_skew": 2.0,
        "rate_scale": 2.0,
    }
    assert base["policy"] == "round_robin", "the base manifest must not be mutated"
    bare = sweep.build_sweep_manifest(
        {"config": {"arrival": "bursty"}}, "jsq", 0.0, 1.0, 1.0, trace_sha256="x"
    )
    assert bare["lambda"] == 0.9
    assert bare["trace_path"] == str(sweep.TRACE_DEFAULT)
    assert "cost_model_snapshots" not in bare
    unhashed = sweep.build_sweep_manifest({"trace_path": "kept"}, "jsq", 0.0, 1.0, 1.0)
    assert unhashed["trace_path"] == "kept" and "trace_sha256" not in unhashed


def test_no_maven_is_an_error(monkeypatch) -> None:
    monkeypatch.setattr(sweep.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="mvn not found"):
        sweep.find_mvn()


def test_an_overlay_holds_the_real_tree_and_the_synthesised_ones_and_is_built_once(
    tmp_path,
) -> None:
    real = tmp_path / "models"
    (real / "cls").mkdir(parents=True)
    (real / "cls" / "a.json").write_text("{}")
    synth = sweep.synthesize_snapshot(_base_snapshot(), 2.0)
    cache: dict = {}
    first = sweep.overlay_dir([synth], real, cache)
    assert (first / "cls" / "a.json").is_file()
    assert json.loads((first / f"{synth['snapshot_id']}.json").read_text()) == synth
    assert sweep.overlay_dir([synth], real, cache) == first
    uncached = sweep.overlay_dir([synth], real)
    assert uncached != first
    for path in (first, uncached):
        shutil.rmtree(path)


def test_the_sweeps_own_trace_helper_keeps_regenerates_or_matches(tmp_path, capsys) -> None:
    config = tmp_path / "trace.json"
    cfg = {
        "gen_seed": 3,
        "n_requests": 5,
        "duration_s": 10,
        "arrival": {"process": "poisson", "lambda_base": 1.0},
        "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
        "priority_mix": {"0": 1.0},
        "admissible": {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 60000},
        "vocab_size": 1000,
    }
    config.write_text(json.dumps(cfg))
    trace = tmp_path / "t.jsonl"
    sha = sweep.ensure_trace(trace, config)
    assert "regenerated" in capsys.readouterr().out
    assert sweep.ensure_trace(trace, config) == sha  # present, no anchors
    anchors = tmp_path / "anchors"
    (anchors / "a").mkdir(parents=True)
    (anchors / "a" / "manifest.json").write_text(json.dumps({"trace_sha256": sha}))
    assert sweep.ensure_trace(trace, config, anchors) == sha
    assert "matches anchors" in capsys.readouterr().out
    (anchors / "a" / "manifest.json").write_text(json.dumps({"trace_sha256": "0" * 64}))
    assert sweep.ensure_trace(trace, config, anchors) == sha
    (anchors / "a" / "manifest.json").unlink()
    assert sweep.ensure_trace(trace, config, anchors) == sha


# ------------------------------------------------------------------------- run_one_des


def _fake_simapp(monkeypatch, *, rc=0, stdout="", write_log=True):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        args = cmd[-1].split("=", 1)[1].split()
        if write_log:
            out = sweep.Path(args[2])
            (out / "scheduler_x.jsonl").write_text("{}\n")
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

    monkeypatch.setattr(sweep.shutil, "which", lambda name: "/usr/bin/mvn")
    monkeypatch.setattr(sweep.subprocess, "run", run)
    return calls


def test_run_one_des_runs_simapp_deterministically_and_cleans_up(tmp_path, monkeypatch) -> None:
    calls = _fake_simapp(monkeypatch)
    out = sweep.run_one_des(tmp_path / "t.jsonl", {"run_id": "r1"}, tmp_path / "out", tmp_path)
    assert out == tmp_path / "out"
    args = calls[0][-1]
    assert "--deterministic" in args and f"--cost-models {tmp_path}" in args
    manifest_path = sweep.Path(args.split()[1])
    assert not manifest_path.exists()


def test_run_one_des_with_synthesised_snapshots_points_simapp_at_an_overlay(
    tmp_path, monkeypatch
) -> None:
    calls = _fake_simapp(monkeypatch)
    (tmp_path / "models").mkdir()
    synth = sweep.synthesize_snapshot(_base_snapshot(), 2.0)
    sweep.run_one_des(
        tmp_path / "t", {"run_id": "r1"}, tmp_path / "out", tmp_path / "models", [synth]
    )
    cost_models = calls[0][-1].split("--cost-models ")[1]
    assert cost_models != str(tmp_path / "models")
    assert (sweep.Path(cost_models) / f"{synth['snapshot_id']}.json").is_file()
    shutil.rmtree(cost_models)


@pytest.mark.parametrize(
    ("rc", "stdout", "write_log", "match"),
    [
        (1, "", True, "SimApp failed for r1"),
        (0, "Error during simulation: boom", True, "SimApp failed for r1"),
        (0, "fine", False, "No scheduler log"),
    ],
)
def test_run_one_des_refuses_a_run_that_did_not_simulate(
    tmp_path, monkeypatch, rc, stdout, write_log, match
) -> None:
    _fake_simapp(monkeypatch, rc=rc, stdout=stdout, write_log=write_log)
    with pytest.raises(RuntimeError, match=match):
        sweep.run_one_des(tmp_path / "t", {"run_id": "r1"}, tmp_path / "out", tmp_path)


# ------------------------------------------------------------------------------ the grid


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A cost-model tree with one measured snapshot, a trace, a base manifest, and SimApp and
    the run-set aggregation faked. `world.runs` holds every manifest SimApp was handed."""
    models = tmp_path / "models"
    (models / "base").mkdir(parents=True)
    (models / "base" / "000.json").write_text(json.dumps(_base_snapshot()))

    trace = tmp_path / "trace.jsonl"
    gen_trace.generate(
        {
            "gen_seed": 3,
            "n_requests": 8,
            "duration_s": 20,
            "arrival": {"process": "poisson", "lambda_base": 0.5},
            "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
            "priority_mix": {"0": 1.0},
            "admissible": {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 60000},
            "vocab_size": 1000,
        },
        trace,
    )
    base = {
        "run_id": "base",
        "trace_sha256": "0" * 64,
        "policy": "round_robin",
        "cost_model_snapshots": {"n1": BASE},
        "nodes": [
            {
                "node_id": "n1",
                "host": "a",
                "role": "pool",
                "max_batch": 4,
                "engine_config": {"parallel": 4},
            }
        ],
        "config": {"arrival": {"lambda_base": 0.5}},
    }
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base))

    runs: list[dict] = []
    fail: set[str] = set()

    def fake_run(trace, manifest, out_dir, cost_models_dir, extra_snapshots, overlay_cache):
        runs.append(
            json.loads(json.dumps(manifest))
            | {"_extra": [s["snapshot_id"] for s in extra_snapshots]}
        )
        sweep.overlay_dir(extra_snapshots, cost_models_dir, overlay_cache)
        if any(tag in manifest["run_id"] for tag in fail):
            raise RuntimeError(f"SimApp failed for {manifest['run_id']}")
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir

    aggregated = SimpleNamespace(
        result=SimpleNamespace(frame=_Frame(), summary=lambda: ["2 runs"], excluded=[]),
        error=None,
        calls=[],
    )

    def fake_aggregate(root, *, index):
        aggregated.calls.append(index)
        if aggregated.error:
            raise aggregated.error
        return aggregated.result

    from dataplane.pipeline import runset

    monkeypatch.setattr(sweep, "run_one_des", fake_run)
    monkeypatch.setattr(runset, "aggregate", fake_aggregate)
    return SimpleNamespace(
        models=models,
        trace=trace,
        base=base,
        base_path=base_path,
        runs=runs,
        fail=fail,
        aggregated=aggregated,
        out=tmp_path / "out",
        tmp=tmp_path,
    )


class _Frame:
    def __len__(self) -> int:
        return 2

    def to_parquet(self, path, index=False) -> None:
        sweep.Path(path).write_text("parquet")


def _config(world, **grid) -> sweep.Path:
    cfg = {
        "base_manifest": str(world.base_path),
        "grid": {
            "policies": ["jsq"],
            "staleness_s": [0.0],
            "rate_scale": [],
            "pool_utilisation": [],
            "R": [1],
            "phase_skew": [1.0],
        }
        | grid,
    }
    path = world.tmp / "sweep.json"
    path.write_text(json.dumps(cfg))
    return path


def _sweep(world, config, *extra) -> int:
    argv = ["--config", str(config), "--out", str(world.out), "--trace", str(world.trace)]
    argv += ["--cost-models", str(world.models), "--anchors", str(world.tmp / "no_anchors"), *extra]
    return sweep.main(argv)


def _resolved(world, manifest) -> float:
    """The rate `pool_load.rate_for` gives this manifest's own pool, as a rate_scale."""
    index = sweep.load_snapshots_by_id(world.models)
    for sid in manifest["_extra"]:
        index[sid] = json.loads((world.out / "synthesised" / f"{sid}.json").read_text())
    header, _ = gen_trace.load(world.trace)
    capacity = pool_load.pool_capacity(
        manifest["nodes"], manifest["cost_model_snapshots"], header["length_dist"], index
    )
    rate, _ = pool_load.rate_for(manifest["config"]["load_target"], capacity)
    return rate / pool_load.mean_rate(header["arrival"])


def test_one_utilisation_point_offers_each_r_its_own_rate(world, capsys) -> None:
    """At fixed utilisation the offered rate falls as the slow node gets slower, because the
    pool it is a share of is smaller. One rate across R would be a different load at every R."""
    assert _sweep(world, _config(world, pool_utilisation=[0.5], R=[1, 4])) == 0

    by_r = {m["config"]["R_target"]: m for m in world.runs}
    assert set(by_r) == {1, 4}
    r1, r4 = by_r[1]["config"]["rate_scale"], by_r[4]["config"]["rate_scale"]
    assert r1 > r4
    for m in world.runs:
        assert m["config"]["rate_scale"] == pytest.approx(_resolved(world, m))
        assert m["config"]["load_target"] == {"pool_utilisation": 0.5}
        assert m["config"]["operating_point"] == "u0.5"
        assert "_u0.5_" in m["run_id"]
    assert "pool_utilisation 0.5 at R=4" in capsys.readouterr().out


def test_the_resolved_rate_is_the_pool_capacity_share_by_hand(world) -> None:
    """Two slots-of-four nodes on one bucket: capacity is 4 / service at c=4. At R=4 the slow
    node's c=4 cell is 3200 ms, so the pool retires 4/0.8 + 4/3.2 = 6.25 req/s."""
    assert _sweep(world, _config(world, pool_utilisation=[0.4], R=[4])) == 0
    (m,) = world.runs
    assert m["config"]["rate_scale"] == pytest.approx(0.4 * (4 / 0.8 + 4 / 3.2) / 0.5)


def test_a_grid_with_both_load_axes_runs_both(world) -> None:
    assert _sweep(world, _config(world, rate_scale=[1.2], pool_utilisation=[0.3], R=[1, 2])) == 0
    kinds = {(m["config"]["R_target"], next(iter(m["config"]["load_target"]))) for m in world.runs}
    assert kinds == {
        (1, "rate_scale"),
        (1, "pool_utilisation"),
        (2, "rate_scale"),
        (2, "pool_utilisation"),
    }
    fixed = [m for m in world.runs if "rate_scale" in m["config"]["load_target"]]
    assert {m["config"]["rate_scale"] for m in fixed} == {1.2}
    assert all("_r1.2_" in m["run_id"] for m in fixed)


def test_every_point_runs_a_two_node_pool_even_at_r_one(world) -> None:
    """A one-node R=1 point beside two-node R>1 points would read pool size as an effect of R."""
    assert _sweep(world, _config(world, rate_scale=[1.0], R=[1, 2], phase_skew=[1.0, 2.0])) == 0
    for m in world.runs:
        assert len(m["nodes"]) == 2
        slow = m["nodes"][1]
        assert slow["gpu"] == "synthesised"
        assert m["cost_model_snapshots"][slow["node_id"]] == m["_extra"][0]
    skewed = [m for m in world.runs if m["config"]["phase_skew"] == 2.0]
    assert skewed and all("_skew2" in m["run_id"] for m in skewed)
    assert sorted(p.name for p in (world.out / "synthesised").iterdir()) == sorted(
        {f"{m['_extra'][0]}.json" for m in world.runs}
    )


def test_k_slow_puts_that_many_slow_nodes_in_the_pool(world) -> None:
    cfg = _config(world, rate_scale=[1.0], R=[2])
    d = json.loads(cfg.read_text()) | {"k_slow": 3}
    cfg.write_text(json.dumps(d))
    assert _sweep(world, cfg) == 0
    (m,) = world.runs
    assert [n["node_id"] for n in m["nodes"]] == ["n1", "slow_2x_1", "slow_2x_2", "slow_2x_3"]
    assert len({n["host"] for n in m["nodes"]}) == 4


def test_the_aggregate_is_indexed_with_the_synthesised_snapshots(world, capsys) -> None:
    (world.out / "synthesised").mkdir(parents=True)
    (world.out / "synthesised" / "broken.json").write_text("{nope")
    assert _sweep(world, _config(world, rate_scale=[1.0], R=[2])) == 0
    (index,) = world.aggregated.calls
    assert f"synth_{BASE}__x2" in index
    out = capsys.readouterr().out
    assert "could not index synthesised broken.json" in out
    assert (world.out / "runset.parquet").read_text() == "parquet"


def test_an_aggregate_that_excludes_runs_or_fails_is_a_failed_sweep(world, capsys) -> None:
    world.aggregated.result.excluded = [("r1", "no trace")]
    assert _sweep(world, _config(world, rate_scale=[1.0])) == 1
    assert "1 runs excluded" in capsys.readouterr().out
    world.aggregated.error = ValueError("join refused")
    assert _sweep(world, _config(world, rate_scale=[1.0])) == 1
    assert "Aggregation failed: join refused" in capsys.readouterr().out


def test_a_failed_point_stops_the_sweep_unless_told_to_keep_going(world, capsys) -> None:
    world.fail.add("_R2_")
    cfg = _config(world, rate_scale=[1.0], R=[1, 2, 4])
    assert _sweep(world, cfg) == 1
    assert "Stopped at the first failure" in capsys.readouterr().out
    assert len(world.runs) == 2

    world.runs.clear()
    assert _sweep(world, cfg, "--keep-going") == 1
    assert "Only 2 of 3 points produced output" in capsys.readouterr().out
    assert len(world.runs) == 3


def test_a_skew_the_base_cannot_supply_is_a_failed_point(world, capsys) -> None:
    (world.models / "base" / "000.json").write_text(json.dumps(_base_snapshot(split=False)))
    cfg = _config(world, rate_scale=[1.0], phase_skew=[2.0, 3.0])
    assert _sweep(world, cfg) == 1
    assert capsys.readouterr().out.count("no prefill/decode split") == 1
    assert _sweep(world, cfg, "--keep-going") == 1
    assert capsys.readouterr().out.count("no prefill/decode split") == 2
    assert world.runs == []


def test_a_base_snapshot_that_is_not_there_is_a_failed_point(world, capsys) -> None:
    (world.models / "base" / "000.json").unlink()
    cfg = _config(world, rate_scale=[1.0], R=[1, 2])
    assert _sweep(world, cfg) == 1
    assert capsys.readouterr().out.count(f"failed: base snapshot {BASE} not in") == 1
    assert _sweep(world, cfg, "--keep-going") == 1
    assert capsys.readouterr().out.count(f"failed: base snapshot {BASE} not in") == 2
    assert world.runs == []


def test_settle_sleeps_between_loads_not_after_the_last(world, monkeypatch) -> None:
    slept = []
    monkeypatch.setattr(sweep.time, "sleep", slept.append)
    assert _sweep(world, _config(world, rate_scale=[1.0, 2.0]), "--settle-s", "0.25") == 0
    assert slept == [0.25]


def test_a_dry_run_counts_the_grid_and_runs_nothing(world, capsys) -> None:
    cfg = _config(
        world, rate_scale=[1.0], pool_utilisation=[0.3, 0.5], R=[1, 2], staleness_s=[0.0, 1.0]
    )
    assert _sweep(world, cfg, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Total points: 12" in out
    assert "pool_utilisation=[0.3, 0.5]" in out
    assert world.runs == []


def test_a_config_overriding_one_axis_keeps_the_defaults_for_the_rest(world, capsys) -> None:
    cfg = world.tmp / "partial.json"
    cfg.write_text(json.dumps({"base_manifest": str(world.base_path), "grid": {"R": [8]}}))
    assert _sweep(world, cfg, "--dry-run") == 0
    out = capsys.readouterr().out
    d = sweep.DEFAULT_GRID
    assert f"R=[8] phase_skew={d['phase_skew']}" in out
    total = len(d["policies"]) * len(d["phase_skew"]) * len(d["staleness_s"]) * len(d["rate_scale"])
    assert f"Total points: {total}" in out


def test_a_missing_trace_is_regenerated_from_its_config(world, capsys) -> None:
    trace_cfg = world.tmp / "trace_cfg.json"
    trace_cfg.write_text(
        json.dumps(
            {
                "gen_seed": 5,
                "n_requests": 4,
                "duration_s": 10,
                "arrival": {"process": "poisson", "lambda_base": 0.5},
                "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
                "priority_mix": {"0": 1.0},
                "admissible": {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 60000},
                "vocab_size": 1000,
            }
        )
    )
    world.trace.unlink()
    assert (
        _sweep(
            world, _config(world, rate_scale=[1.0]), "--trace-config", str(trace_cfg), "--dry-run"
        )
        == 0
    )
    assert "Trace missing" in capsys.readouterr().out
    assert world.trace.is_file()


def _anchor(world, sha: str) -> sweep.Path:
    anchors = world.tmp / "anchors"
    (anchors / "a1").mkdir(parents=True, exist_ok=True)
    man = world.base | {"trace_sha256": sha}
    (anchors / "a1" / "manifest.json").write_text(json.dumps(man))
    return anchors


def test_the_first_anchor_manifest_is_the_base_when_the_config_names_none(world, capsys) -> None:
    anchors = _anchor(world, "0" * 64)
    cfg = world.tmp / "no_base.json"
    cfg.write_text(
        json.dumps(
            {
                "grid": {"policies": ["jsq"], "staleness_s": [0.0], "rate_scale": [1.0], "R": [2]},
                "cost_model_snapshots": {"n1": BASE},
            }
        )
    )
    argv = [
        "--config",
        str(cfg),
        "--out",
        str(world.out),
        "--trace",
        str(world.trace),
        "--cost-models",
        str(world.models),
        "--anchors",
        str(anchors),
    ]
    assert sweep.main(argv) == 0
    out = capsys.readouterr().out
    assert "Trace hash check warning" in out
    (m,) = world.runs
    assert m["cost_model_snapshots"]["n1"] == BASE


def test_with_no_anchors_and_no_base_the_minimal_base_is_used(world, capsys) -> None:
    cfg = world.tmp / "minimal.json"
    cfg.write_text(json.dumps({"grid": {"R": [2]}}))
    argv = [
        "--config",
        str(cfg),
        "--out",
        str(world.out),
        "--trace",
        str(world.trace),
        "--anchors",
        str(world.tmp / "none"),
        "--dry-run",
    ]
    assert sweep.main(argv) == 0
    assert "using minimal base" in capsys.readouterr().out


def test_a_sweep_with_no_config_runs_the_default_grid(world, capsys) -> None:
    """The documented dry run takes no --config, and neither does a sweep run as it stands.
    Without a config it replays the default grid against the first anchor manifest."""
    anchors = _anchor(world, "0" * 64)
    argv = [
        "--out",
        str(world.out),
        "--trace",
        str(world.trace),
        "--cost-models",
        str(world.models),
        "--anchors",
        str(anchors),
    ]
    assert sweep.main([*argv, "--dry-run"]) == 0
    d = sweep.DEFAULT_GRID
    total = math.prod(
        len(d[k]) for k in ("policies", "R", "phase_skew", "staleness_s", "rate_scale")
    )
    assert f"Total points: {total}" in capsys.readouterr().out
    assert sweep.main(argv) == 0
    assert len(world.runs) == total


def test_with_no_config_and_no_anchors_the_minimal_base_is_used(world, capsys) -> None:
    argv = [
        "--out",
        str(world.out),
        "--trace",
        str(world.trace),
        "--anchors",
        str(world.tmp / "none"),
        "--dry-run",
    ]
    assert sweep.main(argv) == 0
    assert "Dry run, not executing" in capsys.readouterr().out


def test_the_overlays_are_removed_when_the_sweep_ends(world) -> None:
    """One overlay per R, reused across every policy and load at that R, and all of them
    deleted at the end: they are full copies of the cost-model tree."""
    made = []
    real = sweep.overlay_dir

    def tracking(snapshots, root, cache=None):
        path = real(snapshots, root, cache)
        made.append(path)
        return path

    sweep.overlay_dir, restore = tracking, real
    try:
        assert (
            _sweep(world, _config(world, policies=["jsq", "wjsq"], rate_scale=[1.0], R=[1, 2])) == 0
        )
    finally:
        sweep.overlay_dir = restore
    assert len(set(made)) == 2
    assert not any(p.exists() for p in made)


def test_a_base_with_no_node_blocks_still_names_the_slow_snapshot(world) -> None:
    base = json.loads(world.base_path.read_text()) | {"nodes": []}
    world.base_path.write_text(json.dumps(base))
    assert _sweep(world, _config(world, rate_scale=[1.0], R=[2])) == 0
    (m,) = world.runs
    assert m["nodes"] == []
    assert m["cost_model_snapshots"]["slow_2x"] == f"synth_{BASE}__x2"
