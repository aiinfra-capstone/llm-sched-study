"""§5.5 — the load band, and the ways a sweep can fail to identify one.

The band is a claim about where a *policy* comparison is meaningful, so most of these tests
are about refusing to make that claim: too few completions to have a p99, a sweep that
never reaches saturation, a one-node pool that cannot separate policies at all.

Two positive tests carry the design. A queue that is still growing when the run ends is
called saturated even though its p50 looks healthy — the case a level rule gets wrong. And
a pool that retires less than the trace offered it is called saturated even when the trend
has not had time to show — the case the trend rule gets wrong, which is the one the first
hardware sweep actually hit.
"""

from __future__ import annotations

import json

import pytest

from dataplane.pipeline import loadband


def _records(n, *, base_ms=100.0, growth_ms=0.0, span_s=60.0, status="ok", warm=0.0):
    """n requests sent evenly over `span_s`, latency rising by `growth_ms` across the run.

    `actual_send_offset_s` tracks `intended_offset_s` because a run whose client fell behind
    is invalid before it ever reaches this module.
    """
    out = []
    for i in range(n):
        frac = i / max(1, n - 1)
        sent = warm + frac * span_s
        out.append(
            {
                "req_id": f"r{i:04d}",
                "intended_offset_s": sent,
                "actual_send_offset_s": sent,
                "status": status,
                "e2e_duration_ns": int((base_ms + growth_ms * frac) * 1e6),
            }
        )
    return out


def _manifest(name, lam, *, n_nodes=1, warmup_s=0.0, run_id="run_x"):
    nodes = [{"node_id": f"n{i}", "host": f"h{i}", "role": "pool"} for i in range(n_nodes)]
    return {
        "run_id": run_id,
        "lambda": lam,
        "warmup_s": warmup_s,
        "nodes": nodes,
        "config": {"operating_point": name},
    }


def _keeping_up(name, lam, n=60, **kw):
    """A point whose pool retired exactly what the trace offered it."""
    span = (n - 1) / lam
    return loadband.point_from_run(_manifest(name, lam), _records(n, span_s=span, **kw))


def test_percentile_returns_a_latency_some_request_actually_had() -> None:
    """Interpolating between two measurements invents a number for a table to quote."""
    values = [10.0, 20.0, 30.0, 40.0]
    assert loadband._percentile(values, 0.5) in values
    assert loadband._percentile(values, 0.99) == 40.0
    assert loadband._percentile([], 0.5) == 0.0


def test_a_growing_queue_is_saturated_even_when_its_p50_looks_fine() -> None:
    """The trend reading: 'slow but stable' and 'not keeping up' are opposite sides of the
    band's upper edge, and a threshold on p50 cannot tell them apart."""
    slow_stable = _keeping_up("heavy", 1.0, base_ms=4000.0)
    fast_growing = _keeping_up("over", 1.0, base_ms=200.0, growth_ms=900.0)
    assert slow_stable.p50_ms > fast_growing.p50_ms
    assert not slow_stable.climbing
    assert fast_growing.climbing and fast_growing.saturated


def test_a_pool_that_retires_less_than_it_was_offered_is_saturated(caplog) -> None:
    """The direct reading, and the one the first hardware sweep needed. At 1.80 req/s against
    a pool that retires 1.65 the backlog builds slowly enough that the trend test misses it
    inside a two-minute run, while the shortfall is plain."""
    # 60 requests offered at 2.0/s but spread over 40 s of completions: 1.5/s retired.
    lagging = loadband.point_from_run(
        _manifest("mid", 2.0), _records(60, span_s=40.0, base_ms=200.0, growth_ms=120.0)
    )
    assert not lagging.climbing  # the trend alone would have called this stable
    assert lagging.short
    assert lagging.saturated
    assert lagging.achieved_rps == pytest.approx(1.5, rel=0.05)


def test_warmup_is_excluded_on_the_trace_timeline_not_the_wall_clock() -> None:
    """§12.2: both vehicles must discard the same requests, and only intended_offset_s is
    the same number in both."""
    records = _records(40, span_s=40.0)
    point = loadband.point_from_run(_manifest("mid", 4.0, warmup_s=20.0), records)
    assert point.n == len([r for r in records if r["intended_offset_s"] >= 20.0])
    assert point.n < 40


def test_failures_are_counted_and_kept_out_of_the_percentiles() -> None:
    ok = _records(30)
    bad = _records(5, status="timeout")
    point = loadband.point_from_run(_manifest("mid", 4.0), ok + bad)
    assert point.n == 30
    assert point.failures == 5


def test_a_point_with_too_few_completions_cannot_place_an_edge() -> None:
    """A p99 over 8 requests is a story about one request."""
    thin = _keeping_up("thin", 1.0, n=8)
    assert not thin.usable
    band = loadband.characterize([thin])
    assert band.lo_rps is None and band.hi_rps is None
    assert not band.identified
    assert "NOT identified" in band.summary()


def test_the_band_is_placed_between_onset_and_saturation() -> None:
    points = [
        _keeping_up("light", 1.0, base_ms=100.0),
        _keeping_up("mid", 2.0, base_ms=180.0),
        loadband.point_from_run(
            _manifest("over", 6.0), _records(60, span_s=30.0, base_ms=200.0, growth_ms=1200.0)
        ),
    ]
    band = loadband.characterize(points)
    assert band.identified
    assert band.reference_p50_ms == pytest.approx(100.0, abs=1.0)
    assert band.reference_p99_ms == pytest.approx(100.0, abs=1.0)
    assert band.lo_rps == 2.0  # the light point is the reference and cannot be its own onset
    assert band.hi_rps == 2.0  # the 6/s point never caught up
    assert not band.policy_separable
    assert "one-node pool" in band.summary()


def test_a_wide_length_mix_does_not_by_itself_open_the_band() -> None:
    """The rule this replaces compared each point's p99 to the reference *p50*, and a trace
    that mixes short and long requests puts p99 several times above p50 with no queue
    anywhere — so it fired at the floor of every sweep. Tail against tail on an identical
    trace isolates the arrival rate, which is the only thing that differs between points."""
    spread = _records(60, span_s=59.0, base_ms=100.0)
    for r in spread[::4]:  # a quarter of the trace is long requests, at every load
        r["e2e_duration_ns"] = int(500e6)
    quiet = loadband.point_from_run(_manifest("quiet", 1.0), spread)
    assert quiet.p99_ms >= 4 * quiet.p50_ms  # the spread alone, with no queueing

    same_again = loadband.point_from_run(_manifest("light", 1.2), list(spread))
    band = loadband.characterize([quiet, same_again])
    assert band.lo_rps is None  # neither point's tail is worse than the reference's
    assert not band.identified


def test_two_nodes_is_what_makes_the_band_a_statement_about_policy() -> None:
    points = [
        loadband.point_from_run(
            _manifest("light", 1.0, n_nodes=2), _records(60, span_s=59.0, base_ms=100.0)
        ),
        loadband.point_from_run(
            _manifest("mid", 1.0, n_nodes=2), _records(60, span_s=59.0, base_ms=400.0)
        ),
    ]
    band = loadband.characterize(points)
    assert band.policy_separable
    assert "one-node pool" not in band.summary()


def test_a_sweep_with_no_points_is_refused() -> None:
    with pytest.raises(ValueError, match="0 operating points"):
        loadband.characterize([])


def test_a_run_directory_without_a_client_log_is_skipped(tmp_path) -> None:
    """A manifest alone describes a run that produced no measurements."""
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "manifest.json").write_text(json.dumps(_manifest("x", 1.0)))
    full = tmp_path / "full"
    full.mkdir()
    (full / "manifest.json").write_text(json.dumps(_manifest("mid", 4.0, run_id="run_full")))
    (full / "client_run_full.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records(40)))
    runs = loadband.read_runs(tmp_path)
    assert [m["run_id"] for m, _ in runs] == ["run_full"]


def test_cli_writes_the_characterization_and_exits_non_zero_when_unidentified(tmp_path) -> None:
    for name, lam, span, kwargs in [
        ("light", 1.0, 59.0, {"base_ms": 100.0}),
        ("mid", 2.0, 29.5, {"base_ms": 180.0}),
        ("over", 6.0, 30.0, {"base_ms": 200.0, "growth_ms": 1200.0}),
    ]:
        d = tmp_path / f"anchor_{name}"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(_manifest(name, lam, run_id=f"anchor_{name}")))
        # The saturated point also times a few requests out, which is what saturation looks
        # like on a real node and what the console line has to say out loud.
        rows = _records(60, span_s=span, **kwargs) + (
            _records(4, status="timeout") if name == "over" else []
        )
        (d / f"client_anchor_{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = tmp_path / "band.json"
    assert loadband.main([str(tmp_path), "--out", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["identified"] is True
    assert [p["name"] for p in written["points"]] == ["light", "mid", "over"]
    assert written["points"][-1]["saturated"] is True
    assert written["points"][-1]["short"] is True
    assert written["reference_p99_ms"] > 0

    thin = tmp_path / "thin_only"
    thin.mkdir()
    d = thin / "anchor_thin"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps(_manifest("thin", 1.0, run_id="anchor_thin")))
    (d / "client_anchor_thin.jsonl").write_text("".join(json.dumps(r) + "\n" for r in _records(6)))
    assert loadband.main([str(thin)]) == 1


def test_cli_refuses_a_directory_with_no_runs(tmp_path) -> None:
    with pytest.raises(SystemExit, match="no run directories"):
        loadband.main([str(tmp_path)])


def test_the_degenerate_windows_have_no_trend_and_no_rate() -> None:
    """One completion, and many completions at one instant: both are guarded rather than
    divided by zero."""
    assert loadband._drift_ms([1.0], [100.0]) == 0.0
    assert loadband._drift_ms([5.0, 5.0, 5.0], [100.0, 200.0, 300.0]) == 0.0
    assert loadband._achieved_rps([]) == 0.0
    assert (
        loadband._achieved_rps(
            [{"actual_send_offset_s": 1.0, "e2e_duration_ns": 0} for _ in range(3)]
        )
        == 0.0
    )
