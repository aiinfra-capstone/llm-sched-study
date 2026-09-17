"""tools/pool_load.py: capacity and load from the C-3 cost models.

Every load target a campaign is run at, and every utilisation the summary reports, goes
through these functions. The first test is the one that matters most: `capability` has to
agree with com.sched.core.Capability, because three of the five policies route on it and a
Python copy that drifted would make the reported StaticWeighted share describe a policy
nobody ran.
"""

from __future__ import annotations

import json
import math

import pool_load
import pytest
from support import CONFIGS, FIRST_PAIR_SNAPSHOTS, current_snapshots, pool_nodes


@pytest.fixture
def snapshot_index():
    return pool_load.snapshot_index()


def _entry(p, o, c, service, **extra):
    return {
        "prompt_bucket": list(p),
        "output_bucket": list(o),
        "concurrency": c,
        "service_ms_mean": service,
        **extra,
    }


def test_capability_matches_what_the_java_scheduler_logged(snapshot_index) -> None:
    """On the first pair the scheduler logged 103.9472... and 163.6070...; the first four
    decimals must agree. Pinned to that pair's snapshots, which is the history being checked."""
    fast = pool_load.capability(snapshot_index[FIRST_PAIR_SNAPSHOTS["rtx3050"]])
    slow = pool_load.capability(snapshot_index[FIRST_PAIR_SNAPSHOTS["gtx1650ti"]])
    assert math.floor(slow * 1e4) == 1039472
    assert math.floor(fast * 1e4) == 1636070


def test_capability_falls_back_to_decode_rate_without_a_phase_split() -> None:
    snap = {
        "entries": [
            _entry((129, 256), (1, 64), 1, 900.0, tokens_per_s=40.0),
            _entry((1, 128), (1, 64), 1, 500.0, tokens_per_s=80.0),
            _entry((1, 128), (1, 64), 4, 700.0, tokens_per_s=60.0),
        ]
    }
    assert pool_load.capability(snap) == 80.0


@pytest.mark.parametrize(("prompt", "expected"), [(128, (1, 128)), (129, (129, 256))])
def test_locate_bucket_edges_are_inclusive(prompt, expected) -> None:
    entries = [
        _entry((1, 128), (1, 64), 1, 100.0),
        _entry((129, 256), (1, 64), 1, 200.0),
    ]
    assert tuple(pool_load.locate(entries, prompt, 64, 1)["prompt_bucket"]) == expected


def test_locate_returns_nothing_for_a_length_no_bucket_covers() -> None:
    entries = [_entry((1, 128), (1, 64), 1, 100.0)]
    assert pool_load.locate(entries, 300, 64, 1) is None
    assert pool_load.locate(entries, 64, 64, 4) is None


def test_bucket_mix_normalises_weights() -> None:
    mix = pool_load.bucket_mix({"buckets": ["p128_o64", "p512_o128"], "weights": [3, 1]})
    assert mix == [("p128_o64", 128, 64, 0.75), ("p512_o128", 512, 128, 0.25)]


def test_mean_service_is_the_bucket_weighted_mean() -> None:
    snap = {
        "snapshot_id": "s",
        "entries": [
            _entry((1, 128), (1, 64), 1, 100.0),
            _entry((129, 512), (65, 128), 1, 400.0),
        ],
    }
    dist = {"buckets": ["p128_o64", "p512_o128"], "weights": [0.75, 0.25]}
    assert pool_load.mean_service_ms(snap, dist, 1) == pytest.approx(175.0)


def test_mean_service_refuses_a_bucket_the_cost_model_does_not_cover() -> None:
    snap = {"snapshot_id": "s", "entries": [_entry((1, 128), (1, 64), 1, 100.0)]}
    with pytest.raises(ValueError, match="no cell for p512_o128 at c=1"):
        pool_load.mean_service_ms(snap, {"buckets": ["p512_o128"], "weights": [1]}, 1)


def test_pool_capacity_is_slots_over_full_slot_service_in_seconds() -> None:
    snap = {
        "snapshot_id": "s",
        "entries": [
            _entry((1, 128), (1, 64), 1, 600.0, tokens_per_s=100.0),
            _entry((1, 128), (1, 64), 4, 1987.4, tokens_per_s=90.0),
        ],
    }
    nodes = [
        {"node_id": "n1", "host": "a", "max_batch": 4, "engine_config": {"parallel": 4}},
        {"node_id": "probe", "host": "a", "role": "probe", "engine_config": {}},
    ]
    cap = pool_load.pool_capacity(
        nodes, {"n1": "s"}, {"buckets": ["p128_o64"], "weights": [1]}, {"s": snap}
    )
    assert list(cap) == ["n1"]
    assert cap["n1"]["slots"] == 4
    assert cap["n1"]["service_ms_c1"] == 600.0
    assert cap["n1"]["service_ms_full"] == 1987.4
    assert cap["n1"]["capacity_rps"] == pytest.approx(2.0127, abs=5e-5)


def test_pool_capacity_reads_slots_from_parallel_when_max_batch_is_absent() -> None:
    snap = {
        "snapshot_id": "s",
        "entries": [
            _entry((1, 128), (1, 64), 1, 500.0, tokens_per_s=1.0),
            _entry((1, 128), (1, 64), 2, 1000.0, tokens_per_s=1.0),
        ],
    }
    nodes = [{"node_id": "n1", "host": "a", "engine_config": {"parallel": 2}}]
    cap = pool_load.pool_capacity(
        nodes, {"n1": "s"}, {"buckets": ["p128_o64"], "weights": [1]}, {"s": snap}
    )
    assert cap["n1"]["slots"] == 2
    assert cap["n1"]["capacity_rps"] == pytest.approx(2.0)


def test_mean_rate_of_an_mmpp_is_dwell_weighted() -> None:
    arrival = {
        "process": "mmpp",
        "lambda_base": 1,
        "burst_lambda": 3,
        "quiet_mean_s": 2,
        "burst_mean_s": 1,
    }
    assert pool_load.mean_rate(arrival) == pytest.approx(5 / 3)


def test_mean_rate_of_poisson_is_its_base_rate_and_anything_else_is_refused() -> None:
    assert pool_load.mean_rate({"process": "poisson", "lambda_base": 0.9}) == 0.9
    with pytest.raises(ValueError, match="unknown arrival process 'gamma'"):
        pool_load.mean_rate({"process": "gamma", "lambda_base": 1})


_CAPS = {"fast": {"capacity_rps": 6.0}, "slow": {"capacity_rps": 2.0}}


def test_rate_for_a_fixed_rate_is_that_rate() -> None:
    assert pool_load.rate_for({"lambda_rps": 1.5}, _CAPS) == (1.5, "fixed rate")


def test_rate_for_pool_utilisation_is_that_share_of_summed_capacity() -> None:
    rate, note = pool_load.rate_for({"pool_utilisation": 0.5}, _CAPS)
    assert rate == pytest.approx(4.0)
    assert "pool utilisation 0.5" in note


def test_rate_for_slow_node_utilisation_assumes_an_even_split() -> None:
    """Pinned: the slow node gets 1/len(pool) of the traffic, RoundRobin's share. Under a
    policy that sends the slow node less, its real utilisation is lower than the target;
    the config that uses this target has to say so."""
    rate, note = pool_load.rate_for({"slow_node_utilisation": 0.7}, _CAPS)
    assert rate == pytest.approx(0.7 * 2.0 * 2)
    assert rate / len(_CAPS) / _CAPS["slow"]["capacity_rps"] == pytest.approx(0.7)
    assert "even split" in note
    comment = json.loads((CONFIGS / "hw_shapes_matched_3050.json").read_text())["_comment"]
    assert "slow_node_utilisation" in comment and "even split" in comment


def test_rate_for_refuses_an_unknown_target() -> None:
    with pytest.raises(ValueError, match="a load target needs"):
        pool_load.rate_for({"queue_depth": 3}, _CAPS)


def test_snapshot_index_skips_json_that_is_not_a_snapshot(tmp_path) -> None:
    (tmp_path / "cls").mkdir()
    (tmp_path / "cls" / "a.json").write_text(json.dumps({"snapshot_id": "a", "entries": []}))
    (tmp_path / "cls" / "notes.json").write_text(json.dumps({"comment": "not a snapshot"}))
    assert list(pool_load.snapshot_index(tmp_path)) == ["a"]


def test_the_committed_pool_has_a_capacity_on_every_committed_shape(snapshot_index) -> None:
    for name in ("trace_anchor_1b.json", "trace_heavytail_1b.json"):
        dist = json.loads((CONFIGS / name).read_text())["length_dist"]
        cap = pool_load.pool_capacity(pool_nodes(), current_snapshots(), dist, snapshot_index)
        assert set(cap) == {"gtx1650ti", "rtx3050"}
        assert all(c["capacity_rps"] > 0 for c in cap.values())
