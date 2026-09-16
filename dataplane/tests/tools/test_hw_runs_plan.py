"""tools/hw_runs.py before any run starts: the plan, the seeds, the traces and the refusals.

What runs, in what order, under what name, on which arrivals. A resumed campaign finds its
finished runs by run id, so a plan that renames or reorders a run silently re-runs it or
skips it. A config mistake that reaches the pool costs hours, so every refusal that can be
made up front is tested one at a time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import hw_runs
import pool_load
import pytest
from support import CONFIGS, REPO_ROOT, SNAPSHOTS, campaign_dict, pool_nodes

from dataplane.harness import gen_trace

GOLDEN = Path(__file__).parent / "golden" / "single_trace_run_ids.json"


def _campaign(tmp_path, **over) -> hw_runs.Campaign:
    return hw_runs.Campaign.from_dict(campaign_dict(tmp_path, **over))


def _multi(tmp_path, names=("generation", "summarisation"), **over) -> hw_runs.Campaign:
    d = campaign_dict(tmp_path, **over)
    d.pop("trace_config")
    d.pop("out_root")
    d["workloads"] = [
        {
            "name": n,
            "trace_config": str(CONFIGS / f"trace_{n}_1b.json"),
            "out_root": str(tmp_path / n),
        }
        for n in names
    ]
    return hw_runs.Campaign.from_dict(d)


def _check(c: hw_runs.Campaign) -> None:
    hw_runs.check_campaign(c, hw_runs.snapshot_index(), allow_colocation=False)


# -------------------------------------------------------------- backward compatibility


@pytest.mark.parametrize("config", sorted(json.loads(GOLDEN.read_text())))
def test_a_single_trace_config_plans_the_run_ids_it_planned_before_workloads(config) -> None:
    """The golden lists were written by the hw_runs.py committed before workloads and seeds
    existed. On the first pair they are also the run directories on disk."""
    c = hw_runs.Campaign.from_dict(json.loads((CONFIGS / config).read_text()))
    assert [r.run_id for r in hw_runs.plan(c)] == json.loads(GOLDEN.read_text())[config]


def test_a_single_trace_config_keeps_its_trace_and_out_root() -> None:
    d = json.loads((CONFIGS / "hw_mpr2_lan_3050.json").read_text())
    c = hw_runs.Campaign.from_dict(d)
    assert c.trace == REPO_ROOT / d["trace"]
    assert c.out_root == REPO_ROOT / d["out_root"]
    assert c.workloads[0].trace_sha256 == d["trace_sha256"]
    assert c.workloads[0].name == ""


def test_a_multi_workload_campaign_has_no_single_trace(tmp_path) -> None:
    c = _multi(tmp_path)
    assert c.trace is None
    assert c.out_root == tmp_path / "generation"


# ---------------------------------------------------------------------------- seeds


def test_repeat_k_gets_seed_k_and_the_scheduler_seed_k(tmp_path) -> None:
    c = _campaign(
        tmp_path, repeats=3, repeat_seeds=[11, 12, 13], scheduler_seeds=[-5, 0, 2**31 - 1]
    )
    for r in hw_runs.plan(c):
        assert r.gen_seed == [11, 12, 13][r.repeat - 1]
        assert r.scheduler_seed == [-5, 0, 2**31 - 1][r.repeat - 1]


@pytest.mark.parametrize("seed", [20260921, 2**40 + 3, -7, 2**31])
def test_without_scheduler_seeds_the_seed_is_the_arrival_seed_as_a_java_int(tmp_path, seed) -> None:
    c = _campaign(tmp_path, repeats=1, repeat_seeds=[seed])
    sched = {r.scheduler_seed for r in hw_runs.plan(c)}
    assert sched == {seed % 2**31}
    assert all(isinstance(s, int) and 0 <= s < 2**31 for s in sched)


def test_a_single_trace_campaign_has_no_seeds(tmp_path) -> None:
    c = hw_runs.Campaign.from_dict(json.loads((CONFIGS / "hw_mpr2_lan_3050.json").read_text()))
    assert {(r.gen_seed, r.scheduler_seed) for r in hw_runs.plan(c)} == {(None, None)}


def test_the_same_config_plans_the_same_runs_twice(tmp_path) -> None:
    a = hw_runs.plan(_multi(tmp_path))
    b = hw_runs.plan(_multi(tmp_path))
    assert [(r.run_id, r.point.rate_scale, r.gen_seed, r.sequence_no) for r in a] == [
        (r.run_id, r.point.rate_scale, r.gen_seed, r.sequence_no) for r in b
    ]


# ----------------------------------------------------------------- several workloads


def test_every_workload_policy_pair_runs_once_per_point_and_repeat(tmp_path) -> None:
    c = _multi(tmp_path, repeats=2, repeat_seeds=[1, 2])
    runs = hw_runs.plan(c)
    cells: dict[tuple, list] = {}
    for r in runs:
        cells.setdefault((r.repeat, r.point.name, r.staleness_s), []).append(
            (r.workload.name, r.policy)
        )
    expected = sorted((w.name, p) for w in c.workloads for p in c.policies)
    assert len(cells) == 2 * len(c.points)
    for pairs in cells.values():
        assert sorted(pairs) == expected
    assert [r.sequence_no for r in runs] == list(range(len(runs)))


def test_the_workload_policy_order_is_shuffled_across_points_and_repeats(tmp_path) -> None:
    c = _multi(
        tmp_path,
        names=("generation", "balanced", "summarisation"),
        repeats=3,
        repeat_seeds=[1, 2, 3],
    )
    orders: dict[tuple, list] = {}
    for r in hw_runs.plan(c):
        orders.setdefault((r.repeat, r.point.name), []).append((r.workload.name, r.policy))
    by_point = [orders[(1, p.name)] for p in c.points]
    by_repeat = [orders[(k, c.points[0].name)] for k in (1, 2, 3)]
    assert len({tuple(o) for o in by_point}) > 1
    assert len({tuple(o) for o in by_repeat}) > 1


def test_multi_workload_run_ids_name_the_workload_and_are_unique(tmp_path) -> None:
    c = _multi(tmp_path, repeats=2, repeat_seeds=[1, 2], staleness_s=[0.0, 2.5])
    runs = hw_runs.plan(c)
    for r in runs:
        assert r.run_id == (
            f"{c.tag}_{r.workload.name}_{r.policy}_s{r.staleness_s:g}_{r.point.name}_r{r.repeat}"
        )
    assert len({r.run_id for r in runs}) == len(runs)


def test_points_run_lightest_first(tmp_path) -> None:
    points = [{"name": "u40", "pool_utilisation": 0.4}, {"name": "u20", "pool_utilisation": 0.2}]
    c = _campaign(tmp_path, points=points, repeats=1, repeat_seeds=[1])
    names = [r.point.name for r in hw_runs.plan(c)]
    assert names.index("u20") < names.index("u40")


# ------------------------------------------------------------------ pre-run manifest


def test_the_pre_run_manifest_carries_the_seed_the_java_scheduler_reads(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(hw_runs, "REPO_ROOT", tmp_path / "repo")
    c = _multi(tmp_path, repeats=1, repeat_seeds=[77], scheduler_seeds=[123])
    run = next(r for r in hw_runs.plan(c) if r.policy == "threshold")
    trace = hw_runs.trace_for(run.workload, run.gen_seed)
    man = hw_runs.pre_run_manifest(c, run, trace.header, trace)
    cfg = man["config"]
    assert cfg["seed"] == 123
    assert cfg["gen_seed"] == 77
    assert cfg["workload"] == run.workload.name
    assert cfg["load_target"] == run.point.target
    assert cfg["mean_lambda"] == round(
        pool_load.mean_rate(trace.header["arrival"]) * run.point.rate_scale, 6
    )
    assert cfg["threshold_t"] == c.threshold_t
    assert man["trace_path"] == str(trace.path.relative_to(tmp_path / "repo"))
    assert man["cost_model_snapshots"] == SNAPSHOTS
    assert man["config_hash"] == hw_runs.manifest_mod.config_hash(cfg)


def test_a_single_trace_pre_run_manifest_has_no_seed_workload_or_target(tmp_path) -> None:
    trace = tmp_path / "t.jsonl"
    cfg = json.loads((CONFIGS / "trace_anchor_1b.json").read_text())
    sha = gen_trace.generate(cfg, trace)
    d = json.loads((CONFIGS / "hw_mpr2_lan_3050.json").read_text())
    d.update(trace=str(trace), trace_sha256=sha, out_root=str(tmp_path / "out"))
    c = hw_runs.Campaign.from_dict(d)
    run = next(r for r in hw_runs.plan(c) if r.policy == "jsq")
    man = hw_runs.pre_run_manifest(c, run, gen_trace.load(trace)[0])
    assert man["trace_path"] == str(trace)
    for key in ("seed", "workload", "load_target", "threshold_t"):
        assert key not in man["config"]


# ------------------------------------------------------------------------ refusals


def test_the_committed_seeded_campaign_passes_every_check(tmp_path) -> None:
    _check(_campaign(tmp_path))


def _refused(c: hw_runs.Campaign, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _check(c)


def test_a_trace_config_without_repeat_seeds_is_refused(tmp_path) -> None:
    d = campaign_dict(tmp_path)
    d.pop("repeat_seeds")
    _refused(hw_runs.Campaign.from_dict(d), "repeat_seeds is required")


def test_a_seed_count_that_does_not_match_repeats_is_refused(tmp_path) -> None:
    _refused(_campaign(tmp_path, repeats=3, repeat_seeds=[1, 2]), "2 seeds for 3 repeats")
    _refused(
        _campaign(tmp_path, repeats=2, repeat_seeds=[1, 2], scheduler_seeds=[1]),
        "scheduler_seeds has 1 seeds for 2 repeats",
    )


def test_a_repeated_seed_is_refused(tmp_path) -> None:
    _refused(_campaign(tmp_path, repeats=2, repeat_seeds=[5, 5]), "repeats a seed")


@pytest.mark.parametrize("seed", [2**31 - 1, -(2**31)])
def test_scheduler_seeds_at_the_java_int_bounds_are_accepted(tmp_path, seed) -> None:
    _check(_campaign(tmp_path, repeats=1, repeat_seeds=[1], scheduler_seeds=[seed]))


@pytest.mark.parametrize("seed", [2**31, -(2**31) - 1])
def test_scheduler_seeds_past_the_java_int_bounds_are_refused(tmp_path, seed) -> None:
    _refused(_campaign(tmp_path, repeats=1, repeat_seeds=[1], scheduler_seeds=[seed]), "Java int")


def test_duplicate_workload_names_are_refused(tmp_path) -> None:
    _refused(_multi(tmp_path, names=("generation", "generation")), "must be distinct")


def test_a_blank_workload_name_is_refused_only_among_several(tmp_path) -> None:
    c = _multi(tmp_path)
    c.workloads[1].name = ""
    _refused(c, "needs a name")
    single = _campaign(tmp_path)
    assert single.workloads[0].name == ""
    _check(single)


def test_other_config_mistakes_are_refused(tmp_path) -> None:
    _refused(_campaign(tmp_path, policies=["jsq", "sita"]), "unknown policies")
    _refused(_campaign(tmp_path, repeats=0, repeat_seeds=[]), "at least 1")
    _refused(_campaign(tmp_path, advertise=None), "advertise is required")
    workers = campaign_dict(tmp_path)["workers"]
    _refused(_campaign(tmp_path, workers={"rtx3050": workers["rtx3050"]}), "no worker endpoint")
    no_logs = {n: {"endpoint": w["endpoint"]} for n, w in workers.items()}
    _refused(_campaign(tmp_path, workers=no_logs), "no worker log location")
    _refused(
        _campaign(tmp_path, cost_model_snapshots={"rtx3050": SNAPSHOTS["rtx3050"]}),
        "no C-3 snapshot",
    )
    _refused(
        _campaign(tmp_path, cost_model_snapshots={**SNAPSHOTS, "rtx3050": "cm_nope"}),
        "is not under",
    )
    nodes = pool_nodes()
    nodes[1]["engine_config"]["ngl"] = 40
    _refused(_campaign(tmp_path, nodes=nodes), "runs ngl 40")
    older = "cm_gtx1650ti_ngl99_p4_q4km_llama32_1b_20260830T134342Z_008"
    _refused(
        _campaign(tmp_path, cost_model_snapshots={**SNAPSHOTS, "gtx1650ti": older}),
        "name that one",
    )


@pytest.mark.parametrize(
    "point",
    [
        {"name": "two", "pool_utilisation": 0.3, "lambda_rps": 2.0},
        {"name": "both", "rate_scale": 2.0, "pool_utilisation": 0.3},
        {"name": "neither"},
    ],
)
def test_a_point_needs_exactly_one_load_setting(tmp_path, point) -> None:
    with pytest.raises(ValueError, match="needs exactly one of rate_scale"):
        hw_runs.Campaign.from_dict(campaign_dict(tmp_path, points=[point]))


@pytest.mark.parametrize(
    "workload",
    [
        {
            "name": "both",
            "trace_config": str(CONFIGS / "trace_anchor_1b.json"),
            "trace": "t.jsonl",
            "trace_sha256": "0" * 64,
        },
        {"name": "neither"},
    ],
)
def test_a_workload_needs_exactly_one_of_trace_config_and_trace(tmp_path, workload) -> None:
    d = campaign_dict(tmp_path)
    d.pop("trace_config")
    d.pop("out_root")
    d["workloads"] = [{**workload, "out_root": str(tmp_path / "w")}]
    with pytest.raises(ValueError, match="exactly one of trace_config and trace"):
        hw_runs.Campaign.from_dict(d)


# ------------------------------------------------------------------------ load points


def test_a_utilisation_target_is_turned_into_a_rate_scale_per_workload(tmp_path) -> None:
    c = _campaign(tmp_path)
    index = pool_load.snapshot_index()
    mmpp = {
        "process": "mmpp",
        "lambda_base": 0.5,
        "burst_lambda": 2.0,
        "quiet_mean_s": 30,
        "burst_mean_s": 10,
    }
    workload = hw_runs.Workload(
        name="bursty",
        out_root=tmp_path,
        trace_config={**c.workloads[0].trace_config, "arrival": mmpp},
    )
    point = hw_runs.Point("u30", target={"pool_utilisation": 0.3})
    resolved = hw_runs.resolve_point(c, point, workload)
    cap = pool_load.pool_capacity(
        c.nodes, c.cost_model_snapshots, workload.trace_config["length_dist"], index
    )
    target = 0.3 * sum(v["capacity_rps"] for v in cap.values())
    assert resolved.rate_scale == pytest.approx(target / ((0.5 * 30 + 2.0 * 10) / 40))
    assert resolved.target == point.target
    assert resolved.note == "pool utilisation 0.3"
    assert resolved.order == resolved.rate_scale


def test_a_fixed_rate_point_is_left_alone_and_a_fixed_trace_reads_its_header(tmp_path) -> None:
    c = _campaign(tmp_path)
    fixed = hw_runs.Point("x", rate_scale=2.5)
    assert hw_runs.resolve_point(c, fixed, c.workloads[0]) is fixed
    assert fixed.order == 2.5

    cfg = json.loads((CONFIGS / "trace_anchor_1b.json").read_text())
    path = tmp_path / "t.jsonl"
    sha = gen_trace.generate(cfg, path)
    workload = hw_runs.Workload(name="", out_root=tmp_path, trace=path, trace_sha256=sha)
    from_trace = hw_runs.resolve_point(c, hw_runs.Point("u", target={"lambda_rps": 1.8}), workload)
    assert from_trace.rate_scale == pytest.approx(1.8 / cfg["arrival"]["lambda_base"])


# ---------------------------------------------------------------------------- traces


def _seeded_workload(tmp_path) -> hw_runs.Workload:
    return _campaign(tmp_path).workloads[0]


def test_trace_for_writes_a_file_named_after_the_seed_and_reuses_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hw_runs, "REPO_ROOT", tmp_path / "repo")
    w = _seeded_workload(tmp_path)
    first = hw_runs.trace_for(w, 20260921)
    assert first.path == tmp_path / "repo" / "runs" / "traces" / "anchor_1b_g20260921.trace.jsonl"
    assert first.sha256 == hashlib.sha256(first.path.read_bytes()).hexdigest()
    assert first.header["gen_seed"] == 20260921
    mtime = first.path.stat().st_mtime_ns
    again = hw_runs.trace_for(w, 20260921)
    assert again.sha256 == first.sha256
    assert first.path.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in first.path.parent.iterdir()) == [first.path.name]


def test_trace_for_refuses_a_file_that_no_longer_matches_its_seed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hw_runs, "REPO_ROOT", tmp_path / "repo")
    w = _seeded_workload(tmp_path)
    path = hw_runs.trace_for(w, 5).path
    blob = bytearray(path.read_bytes())
    last_line = blob.rfind(b"\n", 0, len(blob) - 1) + 1
    digit = next(i for i in range(len(blob) - 2, last_line, -1) if chr(blob[i]).isdigit())
    blob[digit] = ord("1") if blob[digit] != ord("1") else ord("2")
    path.write_bytes(bytes(blob))
    with pytest.raises(ValueError, match="does not match what its config and seed generate"):
        hw_runs.trace_for(w, 5)


def test_two_seeds_give_two_traces(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hw_runs, "REPO_ROOT", tmp_path / "repo")
    w = _seeded_workload(tmp_path)
    assert hw_runs.trace_for(w, 1).sha256 != hw_runs.trace_for(w, 2).sha256


def test_trace_for_a_seeded_workload_needs_a_seed_and_a_fixed_one_does_not(tmp_path) -> None:
    with pytest.raises(ValueError, match="needs a repeat seed"):
        hw_runs.trace_for(_seeded_workload(tmp_path), None)
    cfg = json.loads((CONFIGS / "trace_anchor_1b.json").read_text())
    path = tmp_path / "t.jsonl"
    sha = gen_trace.generate(cfg, path)
    w = hw_runs.Workload(name="", out_root=tmp_path, trace=path, trace_sha256=sha)
    got = hw_runs.trace_for(w, None)
    assert (got.path, got.sha256, got.header["gen_seed"]) == (path, sha, cfg["gen_seed"])
