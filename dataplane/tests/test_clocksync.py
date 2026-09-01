"""The clock measurement taken at LAN setup, and the one correction it licenses.

Three claims are worth pinning here, because all three are easy to get backwards.

**Offset corrects nothing, and the code must not pretend otherwise.** Every duration in
C-5 is single-host, so a constant offset cancels in `transport_residual_ms`. The tests
below therefore never assert that an offset moved a number; they assert that it is
recorded, and that what moves numbers is the *rate*.

**Absent is not zero.** A host with no time daemon, or a daemon tracking nothing, must
come back marked unsynchronised rather than offset 0.0. A zero would read as agreement,
and agreement is exactly the claim nobody made.

**The default path is untouched.** A manifest with no `clock_sync` block must join to
byte-identical numbers, because every run measured before this existed has no block and
those runs are still the ones the anchors rest on.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from dataplane.harness import clocksync
from dataplane.pipeline import join as join_mod

# A real `chronyc tracking` capture from the GTX 1650 Ti host, kept verbatim. Reformatting
# it would quietly test the parser against a shape chrony does not actually emit.
TRACKING = """\
Reference ID    : ACECB40F (172-236-180-15.ip.linodeusercontent.com)
Stratum         : 6
Ref time (UTC)  : Tue Sep 01 06:15:15 2026
System time     : 0.000582285 seconds slow of NTP time
Last offset     : -0.000421241 seconds
RMS offset      : 0.001486534 seconds
Frequency       : 35.892 ppm slow
Residual freq   : -0.031 ppm
Skew            : 1.101 ppm
Root delay      : 0.027803496 seconds
Root dispersion : 0.013388096 seconds
Update interval : 519.8 seconds
Leap status     : Normal
"""

UNSYNCHRONISED = """\
Reference ID    : 00000000 ()
Stratum         : 0
Ref time (UTC)  : Thu Jan 01 00:00:00 1970
System time     : 0.000000000 seconds fast of NTP time
Residual freq   : +0.000 ppm
Skew            : 0.000 ppm
Root delay      : 1.000000000 seconds
Root dispersion : 1.000000000 seconds
Leap status     : Not synchronised
"""


def _clock(host: str, **kw: Any) -> clocksync.HostClock:
    base: dict[str, Any] = {
        "method": "chrony",
        "synchronised": True,
        "offset_ms": 0.0,
        "rate_error_ppm": 0.0,
        "measured_unix": 1788243528,
    }
    return clocksync.HostClock(host=host, **(base | kw))


# --------------------------------------------------------------------------------------
# Parsing what the daemon actually says
# --------------------------------------------------------------------------------------


def test_slow_of_ntp_time_becomes_a_negative_offset() -> None:
    """Chrony says "slow" for behind. The signed field must not carry an English word."""
    clock = clocksync.parse_tracking(TRACKING, host="fedora")
    assert clock.synchronised
    assert clock.offset_ms == pytest.approx(-0.582285)
    assert clock.rate_error_ppm == pytest.approx(-0.031)
    assert clock.skew_ppm == pytest.approx(1.101)
    assert clock.dispersion_ms == pytest.approx(13.388096)
    assert clock.stratum == 6
    assert clock.source == "172-236-180-15.ip.linodeusercontent.com"


def test_fast_of_ntp_time_becomes_a_positive_offset() -> None:
    text = TRACKING.replace("0.000582285 seconds slow", "0.000582285 seconds fast")
    assert clocksync.parse_tracking(text, host="h").offset_ms == pytest.approx(0.582285)


def test_a_daemon_tracking_nothing_reports_unsynchronised_rather_than_zero() -> None:
    """The stale numbers it prints are plausible, which is what makes them dangerous."""
    clock = clocksync.parse_tracking(UNSYNCHRONISED, host="h")
    assert not clock.synchronised
    assert clock.offset_ms == 0.0 and "not synchronised" in clock.note
    assert "UNSYNC" in clock.line()


def test_a_leap_status_of_not_synchronised_is_enough_on_its_own() -> None:
    text = TRACKING.replace("Leap status     : Normal", "Leap status     : Not synchronised")
    assert not clocksync.parse_tracking(text, host="h").synchronised


def test_output_with_no_readable_system_time_is_refused() -> None:
    text = TRACKING.replace(
        "System time     : 0.000582285 seconds slow of NTP time", "System time     : ?"
    )
    clock = clocksync.parse_tracking(text, host="h")
    assert not clock.synchronised and "could not read" in clock.note


def test_a_bare_reference_id_is_kept_when_there_is_no_hostname() -> None:
    text = TRACKING.replace(" (172-236-180-15.ip.linodeusercontent.com)", "")
    assert clocksync.parse_tracking(text, host="h").source == "ACECB40F"


def test_a_non_numeric_stratum_does_not_stop_the_reading() -> None:
    text = TRACKING.replace("Stratum         : 6", "Stratum         : ?")
    clock = clocksync.parse_tracking(text, host="h")
    assert clock.synchronised and clock.stratum == 0


def test_lines_that_are_not_key_value_pairs_are_ignored() -> None:
    assert clocksync.parse_tracking("banner text\n" + TRACKING, host="h").synchronised


# --------------------------------------------------------------------------------------
# Reading the local host
# --------------------------------------------------------------------------------------


def test_measure_reads_the_local_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        clocksync.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, TRACKING, ""),
    )
    assert clocksync.measure("alpha").host == "alpha"


def test_measure_names_the_host_itself_when_not_told(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clocksync.socket, "gethostname", lambda: "guessed")
    monkeypatch.setattr(
        clocksync.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, TRACKING, ""),
    )
    assert clocksync.measure().host == "guessed"


def test_a_host_with_no_chronyc_is_unmeasured_not_synchronised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("chronyc")

    monkeypatch.setattr(clocksync.subprocess, "run", _missing)
    clock = clocksync.measure("alpha")
    assert not clock.synchronised and clock.method == "none"
    assert "not installed" in clock.note


def test_a_chronyc_that_fails_is_reported_rather_than_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fails(*_a: Any, **_k: Any) -> Any:
        raise subprocess.CalledProcessError(1, "chronyc")

    monkeypatch.setattr(clocksync.subprocess, "run", _fails)
    assert "chronyc failed" in clocksync.measure("alpha").note


# --------------------------------------------------------------------------------------
# Combining hosts into the C-6 block
# --------------------------------------------------------------------------------------


def test_the_worst_pair_is_between_hosts_not_against_an_upstream_source() -> None:
    """Two hosts 20 ms from the same pool server in the same direction agree perfectly.

    Reporting each host's distance from its upstream would call that a 20 ms problem. It
    is the machines that have to agree with each other, and they do.
    """
    sync = clocksync.combine(
        [_clock("alpha", offset_ms=20.0), _clock("beta", offset_ms=20.4)], reference="alpha"
    )
    assert sync.max_abs_offset_ms == pytest.approx(0.4)
    assert sync.ok and sync.problems() == []


def test_rate_difference_is_also_pairwise() -> None:
    sync = clocksync.combine(
        [_clock("alpha", rate_error_ppm=-2.0), _clock("beta", rate_error_ppm=3.0)],
        reference="alpha",
    )
    assert sync.max_rate_error_ppm == pytest.approx(5.0)


def test_one_host_compares_nothing_and_says_so() -> None:
    sync = clocksync.combine([_clock("alpha", offset_ms=9.0)])
    assert sync.max_abs_offset_ms == 0.0 and sync.max_rate_error_ppm == 0.0
    assert "nothing was compared" in sync.summary()
    assert sync.ok


def test_a_reference_that_was_never_measured_is_a_problem() -> None:
    sync = clocksync.combine([_clock("alpha")], reference="ghost")
    assert not sync.ok
    assert any("no timebase" in p for p in sync.problems())


def test_an_unsynchronised_member_fails_the_block() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), clocksync.HostClock(host="beta", note="no daemon")], reference="alpha"
    )
    assert sync.unsynchronised == ["beta"] and not sync.ok
    assert any("rate-disciplined" in p for p in sync.problems())


def test_hosts_far_enough_apart_mean_the_discipline_is_not_running() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), _clock("beta", offset_ms=clocksync.MAX_OFFSET_MS + 1)],
        reference="alpha",
    )
    assert not sync.ok
    assert any("discipline is not running" in p for p in sync.problems())


def test_a_rate_difference_over_the_bound_is_named_as_scaling_durations() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), _clock("beta", rate_error_ppm=clocksync.MAX_RATE_ERROR_PPM + 1)],
        reference="alpha",
    )
    assert not sync.ok
    assert any("differ in rate" in p for p in sync.problems())


def test_combine_refuses_an_empty_pool() -> None:
    with pytest.raises(ValueError, match="at least one"):
        clocksync.combine([])


def test_combine_refuses_two_readings_of_the_same_host() -> None:
    with pytest.raises(ValueError, match="each host is measured once"):
        clocksync.combine([_clock("alpha"), _clock("alpha", offset_ms=5.0)])


def test_the_block_carries_its_own_summary_and_a_two_host_headline() -> None:
    sync = clocksync.combine([_clock("alpha"), _clock("beta")], reference="alpha")
    assert "2 hosts on reference alpha" in sync.summary()
    block = sync.to_dict()
    assert block["reference"] == "alpha" and set(block["hosts"]) == {"alpha", "beta"}
    assert block["ok"] is True


def test_a_note_is_carried_into_the_block_only_when_there_is_one() -> None:
    assert "note" not in _clock("alpha").to_dict()
    assert clocksync.HostClock(host="a", note="why").to_dict()["note"] == "why"


# --------------------------------------------------------------------------------------
# The one correction: rate, not offset
# --------------------------------------------------------------------------------------


def test_no_block_means_no_factors_rather_than_a_factor_of_one() -> None:
    """An empty mapping, so the caller's default path performs no arithmetic at all."""
    assert clocksync.rate_factors(None) == {}
    assert clocksync.rate_factors({}) == {}


def test_a_host_ticking_fast_gets_a_factor_above_one() -> None:
    sync = clocksync.combine(
        [_clock("alpha", rate_error_ppm=-0.03), _clock("beta", rate_error_ppm=40.0)],
        reference="alpha",
    ).to_dict()
    factors = clocksync.rate_factors(sync)
    assert set(factors) == {"beta"}
    assert factors["beta"] == pytest.approx(1.00004003)


def test_the_reference_corrects_nothing_against_itself() -> None:
    sync = clocksync.combine(
        [_clock("alpha", rate_error_ppm=12.0), _clock("beta", rate_error_ppm=12.0)],
        reference="alpha",
    ).to_dict()
    assert clocksync.rate_factors(sync) == {}


def test_an_unsynchronised_host_is_left_alone_rather_than_guessed_at() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), clocksync.HostClock(host="beta", note="no daemon")], reference="alpha"
    ).to_dict()
    assert clocksync.rate_factors(sync) == {}


def test_an_unsynchronised_reference_disables_the_correction_entirely() -> None:
    """Nothing to express the others against, so nothing is corrected, loudly rather than by
    silently falling back to whichever host was listed first."""
    sync = clocksync.combine(
        [clocksync.HostClock(host="alpha", note="no daemon"), _clock("beta", rate_error_ppm=40.0)],
        reference="alpha",
    ).to_dict()
    assert clocksync.rate_factors(sync) == {}


def test_a_reference_missing_from_hosts_disables_the_correction() -> None:
    assert clocksync.rate_factors({"reference": "ghost", "hosts": {}}) == {}


# --------------------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------------------


def _run(clock_sync: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest: dict[str, Any] = {
        "run_id": "r1",
        "policy": "jsq",
        "lambda": 1.0,
        "staleness_s": 0.0,
        "warmup_s": 0.0,
        "nodes": [
            {"node_id": "n1", "host": "alpha", "role": "pool"},
            {"node_id": "n2", "host": "beta", "role": "pool"},
        ],
        "validity": {"valid": True},
    }
    if clock_sync:
        manifest["clock_sync"] = clock_sync
    client = [
        {
            "run_id": "r1",
            "req_id": q,
            "intended_offset_s": 1.0,
            "send_lag_ms": 0.1,
            "e2e_duration_ns": 2_500_000_000,
            "status": "ok",
        }
        for q in ("q1", "q2")
    ]
    worker = [
        {
            "run_id": "r1",
            "req_id": q,
            "node_id": n,
            "queue_wait_ns": 100_000_000,
            "service_ns": 2_000_000_000,
            "prefill_ns": 200_000_000,
            "decode_ns": 1_800_000_000,
        }
        for q, n in (("q1", "n1"), ("q2", "n2"))
    ]
    scheduler = [
        {
            "type": "decision",
            "run_id": "r1",
            "req_id": q,
            "chosen_node": n,
            "decide_duration_ns": 120_000,
            "candidates": [],
        }
        for q, n in (("q1", "n1"), ("q2", "n2"))
    ]
    rows = join_mod.join(manifest=manifest, client=client, scheduler=scheduler, worker=worker)
    return rows, manifest


def test_a_run_with_no_block_joins_to_exactly_the_numbers_it_always_did() -> None:
    rows, _ = _run(None)
    assert [r["service_ms"] for r in rows] == [2000.0, 2000.0]
    assert [r["queue_wait_ms"] for r in rows] == [100.0, 100.0]


def test_the_fast_host_is_scaled_and_the_reference_is_not() -> None:
    sync = clocksync.combine(
        [_clock("alpha", rate_error_ppm=0.0), _clock("beta", rate_error_ppm=40.0)],
        reference="alpha",
    ).to_dict()
    rows, _ = _run(sync)
    on_alpha, on_beta = rows
    assert on_alpha["service_ms"] == 2000.0
    assert on_beta["service_ms"] == pytest.approx(2000.0 / 1.00004, rel=1e-12)
    assert on_beta["prefill_ms"] == pytest.approx(200.0 / 1.00004, rel=1e-12)
    assert on_beta["decode_ms"] == pytest.approx(1800.0 / 1.00004, rel=1e-12)
    assert on_beta["queue_wait_ms"] == pytest.approx(100.0 / 1.00004, rel=1e-12)


def test_the_correction_lands_in_the_residual_because_that_is_where_it_belongs() -> None:
    """The residual is the term that absorbs a disagreement between two hosts' clocks.

    Correcting the worker's durations moves the residual by exactly the correction, and
    nothing else in the row moves at all: `e2e_ms` is the client's own measurement.
    """
    sync = clocksync.combine(
        [_clock("alpha"), _clock("beta", rate_error_ppm=40.0)], reference="alpha"
    ).to_dict()
    corrected, _ = _run(sync)
    plain, _ = _run(None)
    delta_service = plain[1]["service_ms"] - corrected[1]["service_ms"]
    delta_queue = plain[1]["queue_wait_ms"] - corrected[1]["queue_wait_ms"]
    assert corrected[1]["e2e_ms"] == plain[1]["e2e_ms"]
    assert corrected[1]["transport_residual_ms"] - plain[1]["transport_residual_ms"] == (
        pytest.approx(delta_service + delta_queue)
    )


def test_the_summary_quotes_the_correction_against_the_longest_service_time() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), _clock("beta", offset_ms=3.3, rate_error_ppm=40.0)], reference="alpha"
    ).to_dict()
    rows, manifest = _run(sync)
    lines = " | ".join(join_mod.summarize(rows, manifest))
    assert "worst pair offset 3.300 ms" in lines
    assert "subtracted from nothing" in lines
    assert "rate correction applied to 1 node(s)" in lines


def test_an_unsynchronised_host_is_named_in_the_summary() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), clocksync.HostClock(host="beta", note="no daemon")], reference="alpha"
    ).to_dict()
    rows, manifest = _run(sync)
    assert any("were not synchronised" in line for line in join_mod.summarize(rows, manifest))


def test_a_multi_host_run_with_no_block_is_told_the_claim_is_unevidenced() -> None:
    rows, manifest = _run(None)
    assert any(
        "unevidenced rather than measured" in line for line in join_mod.summarize(rows, manifest)
    )


def test_a_single_host_run_with_no_block_says_nothing_about_clocks() -> None:
    rows, manifest = _run(None)
    manifest["nodes"] = manifest["nodes"][:1]
    assert not any("clock" in line for line in join_mod.summarize(rows, manifest))


def test_a_block_naming_a_host_no_node_runs_on_corrects_nothing() -> None:
    sync = clocksync.combine(
        [_clock("alpha"), _clock("gamma", rate_error_ppm=40.0)], reference="alpha"
    ).to_dict()
    rows, manifest = _run(sync)
    assert [r["service_ms"] for r in rows] == [2000.0, 2000.0]
    assert not any("rate correction applied" in line for line in join_mod.summarize(rows, manifest))


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_measure_writes_a_file_the_combiner_can_read_back(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        clocksync.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, TRACKING, ""),
    )
    out = tmp_path / "alpha.json"
    assert clocksync.main(["--measure", "--host", "alpha", "--out", str(out)]) == 0
    assert "ok" in capsys.readouterr().out
    assert clocksync.main(["--combine", str(out), "--reference", "alpha"]) == 0


def test_measure_exits_non_zero_on_an_undisciplined_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        clocksync.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, UNSYNCHRONISED, ""),
    )
    assert clocksync.main(["--measure"]) == 1


def test_a_declared_host_stays_distinguishable_from_a_measured_one(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A number a human typed must never be indistinguishable from one a daemon reported."""
    out = tmp_path / "beta.json"
    assert clocksync.main(["--declare", "beta", "2.5", "12.0", "--out", str(out)]) == 0
    written = json.loads(out.read_text())
    assert written["method"] == "declared" and written["offset_ms"] == 2.5
    assert "declared by hand" in written["note"]
    assert "ok" in capsys.readouterr().out


def test_combine_writes_the_block_and_reports_its_problems(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    files = []
    for host, ppm in (("alpha", "0"), ("beta", "500")):
        path = tmp_path / f"{host}.json"
        clocksync.main(["--declare", host, "0", ppm, "--out", str(path)])
        files.append(str(path))
    out = tmp_path / "clock_sync.json"
    rc = clocksync.main(["--combine", *files, "--reference", "alpha", "--out", str(out)])
    assert rc == 1
    assert "PROBLEM" in capsys.readouterr().out
    block = json.loads(out.read_text())
    assert block["ok"] is False and block["max_rate_error_ppm"] == pytest.approx(500.0)


def test_the_cli_refuses_to_guess_what_was_wanted() -> None:
    with pytest.raises(SystemExit):
        clocksync.main([])


def test_a_declared_host_can_be_printed_without_being_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert clocksync.main(["--declare", "beta", "1.0", "0.0"]) == 0
    assert "beta" in capsys.readouterr().out


def test_a_tracking_block_missing_a_field_keeps_the_fields_it_has() -> None:
    """Chrony's output has varied across versions. A missing line leaves its field at its
    default rather than aborting the reading, because the offset is still worth having."""
    text = "\n".join(l for l in TRACKING.splitlines() if not l.startswith("Skew"))
    clock = clocksync.parse_tracking(text + "\n", host="h")
    assert clock.synchronised and clock.skew_ppm == 0.0
    assert clock.rate_error_ppm == pytest.approx(-0.031)


# --------------------------------------------------------------------------------------
# What the LAN preflight says about it
# --------------------------------------------------------------------------------------


def _preflight_report(hosts: list[str], clock: clocksync.HostClock) -> Any:
    from dataplane.harness import preflight

    return preflight.PreflightReport(
        peers=[], pool_problems=[], hosts=hosts, colocated=0, clock=clock
    )


def test_one_host_with_an_undisciplined_clock_is_not_a_warning() -> None:
    """Nothing to be comparable with, so there is nothing to warn about."""
    report = _preflight_report(["alpha"], clocksync.HostClock(host="alpha", note="no daemon"))
    assert report.clock_warning == "" and report.ok


def test_two_hosts_with_an_undisciplined_clock_are_warned_but_not_failed() -> None:
    """A preflight sees one machine. Failing here would call a missing chronyd a broken LAN.

    The condition still has teeth, in `clocksync --combine` and again in the join summary,
    both of which see every host rather than the one this happened to run on.
    """
    report = _preflight_report(
        ["alpha", "beta"], clocksync.HostClock(host="alpha", note="no daemon")
    )
    assert "not disciplined" in report.clock_warning
    assert report.ok


def test_a_disciplined_clock_is_carried_into_the_preflight_json() -> None:
    report = _preflight_report(["alpha", "beta"], _clock("alpha", offset_ms=-0.5))
    assert report.clock_warning == ""
    assert report.to_dict()["clock"]["host"] == "alpha"
    assert "ok" in report.clock_line()


def test_a_preflight_run_without_the_clock_reading_carries_no_clock_at_all() -> None:
    report = _preflight_report(["alpha"], clock=None)
    assert report.to_dict().get("clock") is None
    assert report.clock_line() == "" and report.clock_warning == ""


def test_the_preflight_cli_prints_the_clock_and_its_warning(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dataplane.harness import preflight

    monkeypatch.setattr(
        preflight, "measure", lambda: clocksync.HostClock(host="alpha", note="no daemon")
    )
    monkeypatch.setattr(
        preflight,
        "run_preflight",
        lambda config, samples=0, clock=True: _preflight_report(
            ["alpha", "beta"], preflight.measure() if clock else None
        ),
    )
    cfg = tmp_path / "pre.json"
    cfg.write_text("{}")
    assert preflight.main([str(cfg), "--samples", "1"]) == 0
    printed = capsys.readouterr().out
    assert "UNSYNC" in printed and "CLOCK" in printed


def test_no_clock_skips_the_reading_entirely(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--no-clock` exists because the reading shells out, and a preflight that cannot run
    on a locked-down box is a preflight people stop running."""
    from dataplane.harness import preflight

    monkeypatch.setattr(
        preflight,
        "run_preflight",
        lambda config, samples=0, clock=True: _preflight_report(
            ["alpha", "beta"], _clock("alpha") if clock else None
        ),
    )
    cfg = tmp_path / "pre.json"
    cfg.write_text("{}")
    assert preflight.main([str(cfg), "--samples", "1", "--no-clock"]) == 0
    assert "chrony" not in capsys.readouterr().out
