"""Two small repair tools: the phase-split backfill and the anchor-trace regenerator.

`backfill_phase_split.py` writes the prefill and decode times a snapshot series was fitted
without, from the per-request timings the calibration kept. `promote_calibration.py` runs it
before every promotion, so a wrong share here reaches every phase-aware number downstream.

`ensure_trace.py` puts the anchor trace back on a fresh checkout for the cross-seam jobs. Its
one refusal is the one that matters: a trace config that no longer describes the arrivals
the anchors were replayed against must not be regenerated, or F-23 compares two unrelated
runs and reports a percentage.
"""

from __future__ import annotations

import hashlib
import json
import sys

import backfill_phase_split as backfill
import ensure_trace
import pytest
from support import CONFIGS

from dataplane.harness import gen_trace

# ------------------------------------------------------------------- backfill_phase_split


def _obs(p, o, c, *, service, prefill, decode, status="ok") -> dict:
    return {
        "prompt_len": p,
        "output_len": o,
        "concurrency": c,
        "service_ns": service,
        "prefill_ns": prefill,
        "decode_ns": decode,
        "status": status,
    }


def _write_obs(path, rows) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows) + "\n")


def _snapshot(path, entries) -> None:
    path.write_text(json.dumps({"snapshot_id": path.stem, "entries": entries}))


def _entry(pb, ob, c, service) -> dict:
    return {"prompt_bucket": pb, "output_bucket": ob, "concurrency": c, "service_ms_mean": service}


def test_the_split_is_a_share_of_the_summed_service_time(tmp_path) -> None:
    """Shares are taken over the cell's summed service, not averaged per request, so a long
    request counts for its length. Prefill plus decode is left short of service: the engine
    never reported the residual, and spreading it across the phases would invent it."""
    obs = tmp_path / "observations.jsonl"
    _write_obs(
        obs,
        [
            _obs(64, 32, 1, service=100, prefill=20, decode=70),
            _obs(64, 32, 1, service=300, prefill=40, decode=250),
            _obs(64, 32, 1, service=999, prefill=None, decode=None),  # no timings
            _obs(64, 32, 1, service=999, prefill=1, decode=1, status="timeout"),
            _obs(64, 32, 4, service=0, prefill=0, decode=0),  # no service: no share
        ],
    )
    ratios = backfill.cell_ratios(obs)
    assert ratios == {(64, 32, 1): (60 / 400, 320 / 400)}
    pre, dec = ratios[(64, 32, 1)]
    assert pre + dec < 1


def test_backfill_writes_each_entry_from_its_own_service_time(tmp_path) -> None:
    """The sustained cell drifts across a series while its share does not, so the split is
    written against each snapshot's own service mean rather than a run-wide mean."""
    ratios = {(64, 32, 1): (0.2, 0.7), (100, 32, 1): (0.4, 0.5), (64, 32, 4): (0.1, 0.8)}
    path = tmp_path / "000_snap.json"
    _snapshot(
        path,
        [
            _entry([1, 128], [1, 64], 1, 200.0),  # two calibration cells land here
            _entry([1, 128], [1, 64], 4, 500.0),
            _entry([129, 512], [1, 64], 1, 800.0),  # no calibration cell: left alone
        ],
    )
    assert backfill.backfill(path, ratios, dry_run=False) == (2, 1)
    entries = json.loads(path.read_text())["entries"]
    assert entries[0]["prefill_ms_mean"] == pytest.approx(0.3 * 200.0)
    assert entries[0]["decode_ms_mean"] == pytest.approx(0.6 * 200.0)
    assert entries[1]["prefill_ms_mean"] == pytest.approx(50.0)
    assert "prefill_ms_mean" not in entries[2]


def test_bucket_edges_are_inclusive() -> None:
    assert backfill.in_bucket(128, [1, 128])
    assert backfill.in_bucket(1, [1, 128])
    assert not backfill.in_bucket(129, [1, 128])


def test_a_dry_run_and_a_snapshot_nothing_matched_are_not_written(tmp_path) -> None:
    path = tmp_path / "000_snap.json"
    _snapshot(path, [_entry([1, 128], [1, 64], 1, 200.0)])
    before = path.read_text()
    assert backfill.backfill(path, {(64, 32, 1): (0.2, 0.7)}, dry_run=True) == (1, 0)
    assert path.read_text() == before
    assert backfill.backfill(path, {(64, 32, 2): (0.2, 0.7)}, dry_run=False) == (0, 1)
    assert path.read_text() == before


def test_backfill_main_writes_every_snapshot_the_glob_names(tmp_path, capsys) -> None:
    obs = tmp_path / "observations.jsonl"
    _write_obs(obs, [_obs(64, 32, 1, service=100, prefill=20, decode=70)])
    for name in ("000_a.json", "001_b.json"):
        _snapshot(tmp_path / name, [_entry([1, 128], [1, 64], 1, 100.0)])
    argv = ["--observations", str(obs), "--snapshots", str(tmp_path / "0*.json")]

    assert backfill.main([*argv, "--dry-run"]) == 0
    assert "would write 1 entr(ies), 0 unmatched -> 000_a.json" in capsys.readouterr().out
    assert "prefill_ms_mean" not in (tmp_path / "000_a.json").read_text()

    assert backfill.main(argv) == 0
    assert "wrote 1 entr(ies)" in capsys.readouterr().out
    for name in ("000_a.json", "001_b.json"):
        assert json.loads((tmp_path / name).read_text())["entries"][0]["prefill_ms_mean"] == 20.0


def test_backfill_main_refuses_timings_it_does_not_have_and_a_glob_that_matches_nothing(
    tmp_path, capsys
) -> None:
    """A backfill that did nothing has to say so with a non-zero exit, because the promotion
    reads the exit code before it reads the snapshot."""
    bare = tmp_path / "bare.jsonl"
    _write_obs(bare, [_obs(64, 32, 1, service=100, prefill=None, decode=None)])
    assert backfill.main(["--observations", str(bare), "--snapshots", "x"]) == 1
    assert "carries no prefill/decode timings" in capsys.readouterr().out

    obs = tmp_path / "observations.jsonl"
    _write_obs(obs, [_obs(64, 32, 1, service=100, prefill=20, decode=70)])
    missing = str(tmp_path / "nothing" / "*.json")
    assert backfill.main(["--observations", str(obs), "--snapshots", missing]) == 1
    assert "no snapshots matched" in capsys.readouterr().out


# ---------------------------------------------------------------------------- ensure_trace


def _anchor_config() -> dict:
    return json.loads((CONFIGS / "trace_anchor_1b.json").read_text())


RECORDED_SHA = "abc1234"


def _anchors(
    root, config: dict, *, sha: str = "0" * 64, generator_git_sha=RECORDED_SHA, **config_over
) -> None:
    for name in ("anchor1b_light_1", "anchor1b_heavy_2"):
        (root / name).mkdir(parents=True)
        recorded = {k: config[k] for k in ensure_trace.STREAM_FIELDS} | config_over
        recorded["duration_s"] = 193.04  # compressed by rate_scale; never compared
        if generator_git_sha is not None:
            recorded["generator_git_sha"] = generator_git_sha
        (root / name / "manifest.json").write_text(
            json.dumps({"trace_sha256": sha, "config": recorded})
        )


def _sha_at(tmp_path, config: dict, generator_git_sha: str = RECORDED_SHA) -> str:
    """The hash the anchors would have recorded, had they been replayed at that commit."""
    return gen_trace.generate(
        config, tmp_path / "scratch" / "t.jsonl", generator_git_sha=generator_git_sha
    )


def _run(monkeypatch, config, out, anchors) -> int:
    monkeypatch.setattr(
        sys,
        "argv",
        ["ensure_trace", "--config", str(config), "--out", str(out), "--anchors", str(anchors)],
    )
    return ensure_trace.main()


def test_stream_mismatches_compares_only_the_fields_that_decide_the_stream() -> None:
    config = _anchor_config()
    manifest = {"config": {k: config[k] for k in ensure_trace.STREAM_FIELDS} | {"duration_s": 1}}
    assert ensure_trace.stream_mismatches(config, manifest) == []
    moved = {**config, "gen_seed": config["gen_seed"] + 1}
    (line,) = ensure_trace.stream_mismatches(moved, manifest)
    assert line.startswith("gen_seed: config has")
    # A manifest that never recorded a field has nothing to disagree with.
    assert ensure_trace.stream_mismatches(moved, {"config": {}}) == []


def test_a_missing_trace_is_regenerated_from_its_config(tmp_path, monkeypatch, capsys) -> None:
    config = _anchor_config()
    config_path = tmp_path / "trace_anchor_1b.json"
    config_path.write_text(json.dumps(config))
    _anchors(tmp_path / "anchors", config, sha=_sha_at(tmp_path, config))
    out = tmp_path / "runs" / "traces" / "anchor_1b.trace.jsonl"

    assert _run(monkeypatch, config_path, out, tmp_path / "anchors") == 0

    header, body = gen_trace.load(out)
    assert header["gen_seed"] == config["gen_seed"]
    assert len(body) == config["n_requests"]
    assert "regenerated" in capsys.readouterr().out


def test_regeneration_uses_the_sha_the_anchors_recorded(tmp_path, monkeypatch) -> None:
    config = _anchor_config()
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config))
    expected = _sha_at(tmp_path, config, "d70b6d0")
    _anchors(tmp_path / "anchors", config, sha=expected, generator_git_sha="d70b6d0")
    out = tmp_path / "anchor.trace.jsonl"

    assert _run(monkeypatch, config_path, out, tmp_path / "anchors") == 0

    header, _ = gen_trace.load(out, expect_sha256=expected)
    assert header["generator_git_sha"] == "d70b6d0"


def test_a_regenerated_trace_that_does_not_match_exits_nonzero_and_writes_nothing(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _anchor_config()
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config))
    _anchors(tmp_path / "anchors", config, sha="0" * 64)
    out = tmp_path / "anchor.trace.jsonl"

    assert _run(monkeypatch, config_path, out, tmp_path / "anchors") == 1
    assert not out.exists()
    assert list(tmp_path.glob("*.jsonl*")) == []
    assert "0000000000" in capsys.readouterr().err


def test_anchors_without_a_generator_sha_or_with_two_are_refused() -> None:
    with_sha = {"config": {"generator_git_sha": "abc1234"}}
    assert ensure_trace.recorded_generator_sha([with_sha, with_sha]) == "abc1234"
    with pytest.raises(ValueError, match="generator_git_sha"):
        ensure_trace.recorded_generator_sha([with_sha, {"config": {}}])
    with pytest.raises(ValueError, match="generator_git_sha"):
        ensure_trace.recorded_generator_sha(
            [with_sha, {"config": {"generator_git_sha": "fff0000"}}]
        )


def test_a_config_that_no_longer_describes_the_anchors_is_not_regenerated(
    tmp_path, monkeypatch, capsys
) -> None:
    config = _anchor_config()
    _anchors(tmp_path / "anchors", config, gen_seed=config["gen_seed"] + 1)
    config_path = tmp_path / "trace_anchor_1b.json"
    config_path.write_text(json.dumps(config))
    out = tmp_path / "anchor.trace.jsonl"

    assert _run(monkeypatch, config_path, out, tmp_path / "anchors") == 1
    assert not out.exists()
    assert "no longer describes the stream" in capsys.readouterr().err


def test_a_trace_already_on_disk_is_never_overwritten(tmp_path, monkeypatch, capsys) -> None:
    """A trace on disk is checked, never replaced. One the anchors did not replay is a stop:
    nothing downstream may run against it."""
    config = _anchor_config()
    out = tmp_path / "anchor.trace.jsonl"
    out.write_text("a developer's own trace\n")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    config_path = tmp_path / "c.json"
    config_path.write_text(json.dumps(config))

    _anchors(tmp_path / "match", config, sha=sha)
    assert _run(monkeypatch, config_path, out, tmp_path / "match") == 0
    assert "matches the anchors" in capsys.readouterr().out

    _anchors(tmp_path / "other", config)
    assert _run(monkeypatch, config_path, out, tmp_path / "other") == 1
    assert sha[:12] in capsys.readouterr().err
    assert out.read_text() == "a developer's own trace\n"


def test_no_anchors_is_refused(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "anchors").mkdir()
    assert _run(monkeypatch, tmp_path / "c.json", tmp_path / "t.jsonl", tmp_path / "anchors") == 1
    assert "no anchor manifests" in capsys.readouterr().err


def test_ensure_checks_a_trace_on_disk_and_never_regenerates_it(tmp_path) -> None:
    out = tmp_path / "t.jsonl"
    out.write_text("on disk\n")
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    assert ensure_trace.ensure({}, out, [{"trace_sha256": sha}]) == sha
    with pytest.raises(ensure_trace.TraceMismatch, match="left as it is"):
        ensure_trace.ensure({}, out, [{"trace_sha256": "0" * 64}])
    assert out.read_text() == "on disk\n"
