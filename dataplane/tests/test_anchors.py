"""F-23 — the anchor campaign: three operating points, one trace, one hash.

The property this file exists to protect is that the three runs differ **only** in when
requests arrive. If the anchor set ever spans two traces, a Week-4 disagreement between the
simulator and the hardware becomes uninterpretable: it could be the simulator, or it could
be that the two vehicles were asked different questions. So the trace hash is asserted to
be one value across the set, and the rate is asserted to be the thing that changed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dataplane.harness import anchors, gen_trace
from dataplane.harness import manifest as manifest_mod

NODES = [
    {
        "node_id": "n1",
        "host": "fedora",
        "role": "pool",
        "engine": "llamacpp",
        "engine_version": "b10569+cuda13.2",
        "model": "Llama-3.2-1B-Instruct",
        "quant": "Q4_K_M",
        "gpu": "NVIDIA GeForce GTX 1650 Ti",
        "engine_config": {"ngl": 99, "threads": 6, "parallel": 4},
        "prefix_caching": False,
        "max_batch": 4,
    }
]


@pytest.fixture
def trace(tmp_path, trace_config):
    path = tmp_path / "anchor.trace.jsonl"
    sha = gen_trace.generate(trace_config(n_requests=12, duration_s=30), path)
    return path, sha


def _config(trace_path, sha, tmp_path, **over):
    return anchors.AnchorConfig.from_dict(
        {
            "trace": str(trace_path),
            "trace_sha256": sha,
            "scheduler": "127.0.0.1:50051",
            "nodes": NODES,
            "points": [
                {"name": "light", "rate_scale": 1.0},
                {"name": "mid", "rate_scale": 1.6},
                {"name": "heavy", "rate_scale": 2.4},
            ],
            "out_root": str(tmp_path / "anchors"),
            "settle_s": 0.0,
            **over,
        }
    )


def _fake_replay(header, *, lag_ms=1.0, violations=0, dropped=0, n=12):
    """Stand in for a real replay: the campaign's job is orchestration, not gRPC."""
    calls = []

    async def _replay(**kwargs):
        calls.append(kwargs)
        return anchors.replay_mod.ReplayResult(
            records=[
                {
                    "req_id": f"r{i:04d}",
                    "intended_offset_s": i * 0.5,
                    "status": "ok",
                    "e2e_duration_ns": 100_000_000,
                }
                for i in range(n)
            ],
            validity=manifest_mod.Validity(
                max_send_lag_ms=lag_ms,
                send_lag_violations=violations,
                dropped_requests=dropped,
            ),
            header=header,
        )

    return _replay, calls


def test_the_three_points_share_one_trace_and_differ_only_in_rate(
    tmp_path, trace, monkeypatch
) -> None:
    path, sha = trace
    header, _ = gen_trace.load(path)
    replay, calls = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", replay)

    results = asyncio.run(anchors.run_anchors(_config(path, sha, tmp_path)))

    assert [r.point.name for r in results] == ["light", "mid", "heavy"]
    assert [c["rate_scale"] for c in calls] == [1.0, 1.6, 2.4]
    assert len({r.manifest["trace_sha256"] for r in results}) == 1
    lambdas = [r.manifest["lambda"] for r in results]
    assert lambdas == sorted(lambdas) and lambdas[0] < lambdas[-1]


def test_every_anchor_is_a_hardware_run_that_load_anchors_will_accept(
    tmp_path, trace, monkeypatch, schema
) -> None:
    """Validating a simulator against a simulator proves nothing, and an anchor that fails
    C-6 is not an anchor at all."""
    from dataplane.calibration import admissible

    path, sha = trace
    header, _ = gen_trace.load(path)
    replay, _ = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", replay)

    root = tmp_path / "anchors"
    asyncio.run(anchors.run_anchors(_config(path, sha, tmp_path)))

    loaded = admissible.load_anchors(root)
    assert len(loaded) == 3
    assert {a["vehicle"] for a in loaded} == {"hardware"}
    assert len({a["trace_sha256"] for a in loaded}) == 1
    validator = schema("manifest")
    for a in loaded:
        assert not list(validator.iter_errors(a))


def test_the_client_log_lands_next_to_the_manifest(tmp_path, trace, monkeypatch) -> None:
    path, sha = trace
    header, _ = gen_trace.load(path)
    replay, _ = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", replay)

    results = asyncio.run(anchors.run_anchors(_config(path, sha, tmp_path)))
    for r in results:
        log = r.run_dir / f"client_{r.run_id}.jsonl"
        assert len(log.read_text().splitlines()) == r.n_records == 12
        assert r.n_ok == 12


def test_a_run_that_lost_its_open_loop_is_written_but_is_not_an_anchor(
    tmp_path, trace, monkeypatch, capsys
) -> None:
    """A run that failed to generate its stated load is not a data point about scheduling.
    It stays on disk to be looked at; it does not anchor anything."""
    path, sha = trace
    header, _ = gen_trace.load(path)
    replay, _ = _fake_replay(header, lag_ms=140.0, violations=7)
    monkeypatch.setattr(anchors.replay_mod, "replay", replay)

    results = asyncio.run(anchors.run_anchors(_config(path, sha, tmp_path)))
    assert not any(r.valid for r in results)
    assert (results[0].run_dir / "manifest.json").exists()
    assert "7 send-lag violation(s)" in capsys.readouterr().out


def test_reasons_names_every_way_a_run_can_be_disqualified() -> None:
    assert anchors._reasons(
        {
            "send_lag_violations": 2,
            "dropped_requests": 3,
            "colocated_nodes": 1,
            "engine_restarts": 4,
        }
    ) == [
        "2 send-lag violation(s)",
        "3 request(s) never returned",
        "1 co-located node(s)",
        "4 engine restart(s)",
    ]


def test_two_logical_nodes_on_one_host_are_counted_into_the_manifest() -> None:
    """F-9a. The launcher refuses to start such a pool; this is the record that the pool
    which actually ran was not one."""
    same_host = [dict(NODES[0]), {**NODES[0], "node_id": "n2"}]
    probe = [dict(NODES[0]), {**NODES[0], "node_id": "p1", "role": "engine_gap_probe"}]
    assert anchors._colocated(NODES) == 0
    assert anchors._colocated(same_host) == 2
    assert anchors._colocated(probe) == 0


def test_two_operating_points_are_not_an_anchor_set(tmp_path, trace) -> None:
    path, sha = trace
    with pytest.raises(ValueError, match="at least 3 operating points"):
        anchors.AnchorConfig.from_dict(
            {
                "trace": str(path),
                "trace_sha256": sha,
                "scheduler": "x:1",
                "nodes": NODES,
                "points": [
                    {"name": "a", "rate_scale": 1.0},
                    {"name": "b", "rate_scale": 2.0},
                ],
            }
        )


def test_operating_point_names_have_to_be_unique_because_they_name_run_dirs(
    tmp_path, trace
) -> None:
    path, sha = trace
    with pytest.raises(ValueError, match="must be unique"):
        anchors.AnchorConfig.from_dict(
            {
                "trace": str(path),
                "trace_sha256": sha,
                "scheduler": "x:1",
                "nodes": NODES,
                "points": [{"name": "a", "rate_scale": r} for r in (1.0, 2.0, 3.0)],
            }
        )


def test_a_rate_of_zero_is_not_an_operating_point() -> None:
    with pytest.raises(ValueError, match="rate_scale must be > 0"):
        anchors.AnchorPoint("still", 0.0)


def test_the_hash_is_checked_once_before_the_first_replay(tmp_path, trace, monkeypatch) -> None:
    """Three runs is tens of minutes. Discovering the trace is not the trace the manifest
    will claim is worth ten seconds at the start."""
    path, _sha = trace
    replay, calls = _fake_replay(gen_trace.load(path)[0])
    monkeypatch.setattr(anchors.replay_mod, "replay", replay)
    config = _config(path, "0" * 64, tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(anchors.run_anchors(config))
    assert calls == []


def test_cli_reports_the_anchor_count_and_fails_below_the_floor(
    tmp_path, trace, monkeypatch, capsys
) -> None:
    path, sha = trace
    header, _ = gen_trace.load(path)
    config_path = tmp_path / "anchors.json"
    config_path.write_text(
        json.dumps(
            {
                "trace": str(path),
                "trace_sha256": sha,
                "scheduler": "127.0.0.1:50051",
                "nodes": NODES,
                "settle_s": 0.0,
                "out_root": str(tmp_path / "from_config"),
                "points": [
                    {"name": "light", "rate_scale": 1.0},
                    {"name": "mid", "rate_scale": 1.6},
                    {"name": "heavy", "rate_scale": 2.4},
                ],
            }
        )
    )
    good, _ = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", good)
    assert anchors.main([str(config_path), "--out-root", str(tmp_path / "ok")]) == 0
    assert "3/3 anchors valid" in capsys.readouterr().out

    # No --out-root this time: the config names its own destination, and that is the
    # path a campaign is actually launched with (F-20 — one config file plus a seed).
    bad, _ = _fake_replay(header, dropped=4)
    monkeypatch.setattr(anchors.replay_mod, "replay", bad)
    assert anchors.main([str(config_path)]) == 1
    out = capsys.readouterr().out
    assert "F-23 needs 3 valid anchors" in out
    assert str(tmp_path / "from_config") in out


def test_a_rate_scale_of_zero_would_replay_a_trace_that_never_arrives(tmp_path, trace) -> None:
    """Guarded in the replay client itself, not only in the campaign, because `replay` is
    also driven directly from the command line."""
    path, sha = trace
    with pytest.raises(ValueError, match="must be > 0"):
        asyncio.run(
            anchors.replay_mod.replay(
                trace_path=path,
                scheduler_endpoint="127.0.0.1:1",
                run_id="r",
                expect_sha256=sha,
                rate_scale=0.0,
            )
        )


def test_cli_records_the_measured_clock_on_every_anchor(
    tmp_path, trace, monkeypatch, capsys
) -> None:
    """A multi-host anchor set has to carry the proof its clocks were disciplined.

    `manifest.build` has always accepted the block; nothing ever passed it, so every
    two-host run would have recorded an unevidenced claim that the hosts ticked together.
    The count of undisciplined hosts rides along in `validity`, where it is reported and
    deliberately not fatal.
    """
    path, sha = trace
    header, _ = gen_trace.load(path)
    out_root = tmp_path / "clocked"
    config_path = tmp_path / "anchors.json"
    config_path.write_text(
        json.dumps(
            {
                "trace": str(path),
                "trace_sha256": sha,
                "scheduler": "127.0.0.1:50051",
                "nodes": NODES,
                "settle_s": 0.0,
                "out_root": str(out_root),
                "points": [
                    {"name": "light", "rate_scale": 1.0},
                    {"name": "mid", "rate_scale": 1.6},
                    {"name": "heavy", "rate_scale": 2.4},
                ],
            }
        )
    )
    clock_path = tmp_path / "clock_sync.json"
    clock_path.write_text(
        json.dumps(
            {
                "reference": "fedora",
                "measured_unix": 1788376140,
                "hosts": {
                    "fedora": {"method": "chrony", "synchronised": True, "rate_error_ppm": 0.4},
                    "cpu1": {"method": "none", "synchronised": False},
                },
                "max_abs_offset_ms": 2.1,
                "max_rate_error_ppm": 0.4,
                "ok": False,
            }
        )
    )

    good, _ = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", good)
    assert anchors.main([str(config_path), "--clock-sync", str(clock_path)]) == 0
    assert "3/3 anchors valid" in capsys.readouterr().out

    manifests = sorted(out_root.glob("*/manifest.json"))
    assert len(manifests) == 3
    for m in manifests:
        man = json.loads(m.read_text())
        assert man["clock_sync"]["reference"] == "fedora"
        assert man["validity"]["clock_unsynced_hosts"] == 1
        # One host with no discipline does not sink the run: the only clock term the
        # pipeline acts on is the rate error, and it is sub-millisecond on a 900 ms request.
        assert man["validity"]["valid"] is True


def test_without_the_flag_an_anchor_manifest_carries_no_clock_claim(
    tmp_path, trace, monkeypatch
) -> None:
    """Single-host runs are the common case, and silence is the correct record for them."""
    path, sha = trace
    header, _ = gen_trace.load(path)
    out_root = tmp_path / "unclocked"
    config_path = tmp_path / "anchors.json"
    config_path.write_text(
        json.dumps(
            {
                "trace": str(path),
                "trace_sha256": sha,
                "scheduler": "127.0.0.1:50051",
                "nodes": NODES,
                "settle_s": 0.0,
                "out_root": str(out_root),
                "points": [
                    {"name": "light", "rate_scale": 1.0},
                    {"name": "mid", "rate_scale": 1.6},
                    {"name": "heavy", "rate_scale": 2.4},
                ],
            }
        )
    )
    good, _ = _fake_replay(header)
    monkeypatch.setattr(anchors.replay_mod, "replay", good)
    assert anchors.main([str(config_path)]) == 0
    for m in sorted(out_root.glob("*/manifest.json")):
        man = json.loads(m.read_text())
        assert "clock_sync" not in man
        assert man["validity"]["clock_unsynced_hosts"] == 0
