"""F-23 and P4: the simulator measured against the hardware, per run and per policy.

`f23_compare.py` has a three-valued exit code on purpose. 0 is within tolerance, 2 is a real
comparison outside it, and 1 is a failure to compare at all. The caller counts 0 and 2 towards
the three-point minimum and 1 towards neither, so the tests pin that no broken input can come
back as 0 or 2.

`p4_validate.py` replays every hardware manifest through SimApp and compares each with
`f23_compare.py`. Maven is faked here. `f23_compare.py` itself runs for real in a
subprocess, because the parsing of its output is part of what P4 does.
"""

from __future__ import annotations

import json
import subprocess
import sys

import f23_compare as f23
import p4_validate as p4
import pytest

# --------------------------------------------------------------------------- f23_compare


def _client_log(run_dir, latencies_ms, *, warmup=(), failed=(), name="client_r.jsonl") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "req_id": f"r{i:06d}",
            "intended_offset_s": 0.5 if i in warmup else 20.0 + i,
            "status": "timeout" if i in failed else "ok",
            "e2e_duration_ns": int(ms * 1e6),
        }
        for i, ms in enumerate(latencies_ms)
    ]
    (run_dir / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n\n")


def test_percentile_is_nearest_rank() -> None:
    """A quoted p95 is a latency some request actually had."""
    values = [float(v) for v in range(10, 0, -1)]
    assert f23.percentile(values, 0.5) == 5.0
    assert f23.percentile(values, 0.95) == 10.0
    assert f23.percentile(values, 0.0) == 1.0
    assert f23.percentile([], 0.5) == 0.0


def test_both_vehicles_drop_warmup_and_failures_the_same_way(tmp_path) -> None:
    """Warmup is read off the trace's own timeline, so both vehicles discard the same
    requests whatever their wall clocks did."""
    _client_log(tmp_path, [100, 200, 300, 400], warmup={0}, failed={3})
    assert f23.client_latencies_ms(tmp_path, warmup_s=10.0) == [200.0, 300.0]
    assert f23.sim_latencies_ms(tmp_path, warmup_s=10.0) == [200.0, 300.0]
    with pytest.raises(FileNotFoundError, match="no client log"):
        f23.client_latencies_ms(tmp_path / "empty", 0.0)


def _f23(monkeypatch, argv) -> int:
    monkeypatch.setattr(sys, "argv", ["f23_compare", *argv])
    return f23.main()


def _hw_manifest(path, **over):
    man = {
        "run_id": "t_jsq_s0_u30_r1",
        "policy": "jsq",
        "lambda": 2.4,
        "warmup_s": 10.0,
        "config": {"operating_point": "u30"},
    } | over
    path.write_text(json.dumps(man))
    return path


@pytest.mark.parametrize(("scale", "rc", "verdict"), [(1.1, 0, "PASS"), (1.5, 2, "FAIL")])
def test_p4_mode_compares_against_the_runs_own_hardware_log(
    tmp_path, monkeypatch, capsys, scale, rc, verdict
) -> None:
    hw = [900.0 + 10 * i for i in range(20)]
    _client_log(tmp_path / "hw", hw)
    _client_log(tmp_path / "sim", [x * scale for x in hw])
    man = _hw_manifest(tmp_path / "manifest.json")
    argv = [
        "--manifest",
        str(man),
        "--sim-dir",
        str(tmp_path / "sim"),
        "--hardware-run-dir",
        str(tmp_path / "hw"),
    ]
    assert _f23(monkeypatch, argv) == rc
    out = capsys.readouterr().out
    assert "Run: t_jsq_s0_u30_r1  policy=jsq  point=u30  lambda=2.4 rps" in out
    assert f"{verdict}: t_jsq_s0_u30_r1" in out


def _band(path, **point) -> None:
    point = {"name": "u30", "lambda_rps": 2.4, "p50_ms": 1000.0, "p95_ms": 1200.0, "n": 150} | point
    path.write_text(json.dumps({"points": [point]}))


@pytest.mark.parametrize(("scale", "rc"), [(1.0, 0), (2.0, 2)])
def test_band_mode_compares_against_the_committed_load_band(
    tmp_path, monkeypatch, capsys, scale, rc
) -> None:
    _client_log(tmp_path / "sim", [1000.0 * scale] * 19 + [1200.0 * scale])
    _band(tmp_path / "band.json", saturated=True)
    man = _hw_manifest(tmp_path / "manifest.json")
    argv = [
        "--manifest",
        str(man),
        "--sim-dir",
        str(tmp_path / "sim"),
        "--load-band",
        str(tmp_path / "band.json"),
    ]
    assert _f23(monkeypatch, argv) == rc
    assert "Operating point: u30 (saturated)" in capsys.readouterr().out


def test_every_failure_to_compare_is_exit_one(tmp_path, monkeypatch, capsys) -> None:
    """Exit 1 is never counted towards F-23's minimum, so every way of not comparing lands
    there rather than on 0 or 2."""
    man = _hw_manifest(tmp_path / "manifest.json")
    sim, hw, band = tmp_path / "sim", tmp_path / "hw", tmp_path / "band.json"
    _band(band)
    base = ["--manifest", str(man), "--sim-dir", str(sim)]

    assert _f23(monkeypatch, base) == 1  # neither baseline
    assert _f23(monkeypatch, [*base, "--load-band", str(band), "--hardware-run-dir", str(hw)]) == 1
    assert _f23(monkeypatch, [*base, "--load-band", str(band)]) == 1  # no simulated log
    _client_log(sim, [100.0], warmup={0})
    assert _f23(monkeypatch, [*base, "--load-band", str(band)]) == 1  # nothing after warmup

    _client_log(sim, [1000.0] * 5)
    assert _f23(monkeypatch, [*base, "--hardware-run-dir", str(hw)]) == 1  # no hardware log
    _client_log(hw, [100.0], warmup={0})
    assert _f23(monkeypatch, [*base, "--hardware-run-dir", str(hw)]) == 1  # nothing after warmup

    no_point = _hw_manifest(tmp_path / "m2.json", config={})
    assert (
        _f23(
            monkeypatch,
            ["--manifest", str(no_point), "--sim-dir", str(sim), "--load-band", str(band)],
        )
        == 1
    )
    other = _hw_manifest(tmp_path / "m3.json", config={"operating_point": "u99"})
    assert (
        _f23(
            monkeypatch, ["--manifest", str(other), "--sim-dir", str(sim), "--load-band", str(band)]
        )
        == 1
    )

    out = capsys.readouterr().out
    assert out.count("FAIL:") == 8
    for reason in (
        "pass exactly one of",
        "could not read the simulated run",
        "no simulated requests survived",
        "could not read the hardware run",
        "no hardware requests survived",
        "has no config.operating_point",
        "no point named 'u99'",
    ):
        assert reason in out


def test_a_zero_hardware_latency_is_outside_any_tolerance(tmp_path, monkeypatch) -> None:
    _client_log(tmp_path / "hw", [0.0] * 5)
    _client_log(tmp_path / "sim", [1.0] * 5)
    man = _hw_manifest(tmp_path / "manifest.json")
    argv = [
        "--manifest",
        str(man),
        "--sim-dir",
        str(tmp_path / "sim"),
        "--hardware-run-dir",
        str(tmp_path / "hw"),
    ]
    assert _f23(monkeypatch, argv) == 2


# ---------------------------------------------------------------------------- p4_validate


class FakeTools:
    """Maven, `uv run runset`, `uv run campaign_summary` and `contrast_check.py`, faked.

    SimApp writes a client log whose latencies are the hardware's times `scale[run_id]`, so a
    test decides which runs pass. `f23_compare.py` is not faked.
    """

    def __init__(self, monkeypatch, real_run) -> None:
        self.real_run = real_run
        self.calls: list[list[str]] = []
        self.scale: dict[str, float] = {}
        self.sim_fails: set[str] = set()
        self.no_logs: dict[str, str] = {}
        self.step_rc: dict[str, int] = {}
        self.hw_latencies: dict[str, list[float]] = {}
        monkeypatch.setattr(p4.subprocess, "run", self.run)
        monkeypatch.setattr(p4.shutil, "which", lambda name: "/usr/bin/mvn")

    def run(self, cmd, **kw):
        joined = " ".join(str(c) for c in cmd)
        if "f23_compare.py" in joined:
            return self.real_run(cmd, **kw)
        self.calls.append([str(c) for c in cmd])
        if "exec:java" in joined:
            args = next(c for c in cmd if c.startswith("-Dexec.args=")).split("=", 1)[1].split()
            manifest, out_dir = json.loads(p4.Path(args[1]).read_text()), p4.Path(args[2])
            rid = manifest["run_id"]
            if rid in self.sim_fails:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="Error during simulation", stderr=""
                )
            missing = self.no_logs.get(rid)
            if missing != "scheduler":
                (out_dir / f"scheduler_{rid}.jsonl").write_text("{}\n")
            if missing != "client":
                _client_log(
                    out_dir,
                    [x * self.scale.get(rid, 1.0) for x in self.hw_latencies[rid]],
                    name=f"client_{rid}.jsonl",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        for step in ("runset", "campaign_summary", "contrast_check"):
            if step in joined:
                rc = self.step_rc.get(step, 0)
                return subprocess.CompletedProcess(cmd, rc, stdout=f"{step} said {rc}", stderr="")
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def tools(monkeypatch):
    return FakeTools(monkeypatch, subprocess.run)


def _hw_run(root, tools, run_id, policy, point, trace, latencies=None):
    run_dir = root / run_id
    lat = latencies or [900.0 + 10 * i for i in range(20)]
    _client_log(run_dir, lat, name=f"client_{run_id}.jsonl")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "policy": policy,
                "lambda": 2.4,
                "warmup_s": 10.0,
                "trace_path": str(trace),
                "config": {"operating_point": point},
            }
        )
    )
    tools.hw_latencies[run_id] = lat
    return run_dir


@pytest.fixture
def campaign(tmp_path, tools):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    root = tmp_path / "hw"
    for policy in ("jsq", "wjsq"):
        for repeat in (1, 2):
            _hw_run(root, tools, f"t_{policy}_s0_u30_r{repeat}", policy, "u30", trace)
    return root


def _p4(root, out, *extra) -> int:
    return p4.main(["--hardware-root", str(root), "--out", str(out), *extra])


def test_every_run_within_tolerance_passes(campaign, tools, tmp_path, capsys) -> None:
    assert _p4(campaign, tmp_path / "sims") == 0
    out = capsys.readouterr().out
    assert "P4: 4 hardware runs" in out
    assert "  jsq             u30       pass 2/2  outside 0  error 0" in out
    assert "4/4 runs compared: 4 pass, 0 outside tolerance, 0 errors" in out
    assert "P4 PASSED" in out
    sim_args = [c for c in tools.calls if "exec:java" in " ".join(c)]
    assert len(sim_args) == 4
    assert all("--deterministic" in c[-1] for c in sim_args)


def test_a_run_outside_tolerance_is_mixed_and_names_its_errors(
    campaign, tools, tmp_path, capsys
) -> None:
    tools.scale["t_wjsq_s0_u30_r2"] = 1.6
    assert _p4(campaign, tmp_path / "sims") == 2
    out = capsys.readouterr().out
    assert "wjsq            u30       pass 1/2  outside 1  error 0" in out
    assert "MISS t_wjsq_s0_u30_r2: p50=+60.0% p95=+60.0%" in out
    assert "P4 MIXED" in out


def test_a_simapp_failure_is_an_error_and_stops_unless_told_to_keep_going(
    campaign, tools, tmp_path, capsys
) -> None:
    tools.sim_fails.add("t_jsq_s0_u30_r1")
    assert _p4(campaign, tmp_path / "a") == 1
    out = capsys.readouterr().out
    assert "FAIL: SimApp failed" in out
    assert "1/4 runs compared" in out
    assert "Stopped early (1 failures)" in out

    assert _p4(campaign, tmp_path / "b", "--keep-going") == 1
    out = capsys.readouterr().out
    assert "4/4 runs compared: 3 pass, 0 outside tolerance, 1 errors" in out
    assert "P4 FAILED" in out


@pytest.mark.parametrize("missing", ["scheduler", "client"])
def test_a_simapp_run_that_wrote_no_log_is_a_failure(
    campaign, tools, tmp_path, capsys, missing
) -> None:
    tools.no_logs["t_jsq_s0_u30_r1"] = missing
    assert _p4(campaign, tmp_path / "sims") == 1
    assert f"no {missing} log in" in capsys.readouterr().out


def test_a_finished_sim_is_reused_rather_than_run_again(campaign, tools, tmp_path, capsys) -> None:
    out = tmp_path / "sims"
    sim = out / "t_jsq_s0_u30_r1_sim"
    _client_log(sim, tools.hw_latencies["t_jsq_s0_u30_r1"], name="client_t_jsq_s0_u30_r1.jsonl")
    (sim / "manifest.json").write_text("{}")
    assert _p4(campaign, out) == 0
    assert f"sim exists, reusing {sim}" in capsys.readouterr().out
    assert len([c for c in tools.calls if "exec:java" in " ".join(c)]) == 3


def test_a_bad_manifest_or_a_missing_trace_is_an_error(tmp_path, tools, capsys) -> None:
    root = tmp_path / "hw"
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    _hw_run(root, tools, "t_jsq_s0_u30_r1", "jsq", "u30", tmp_path / "gone.jsonl")
    (root / "t_wjsq_s0_u30_r1").mkdir()
    (root / "t_wjsq_s0_u30_r1" / "manifest.json").write_text("{not json")

    assert _p4(root, tmp_path / "sims", "--keep-going") == 0
    out = capsys.readouterr().out
    assert "FAIL: trace not found" in out
    assert "FAIL: could not read manifest" in out
    assert "0/2 runs compared" in out

    assert _p4(root, tmp_path / "sims2") == 0
    assert "Stopped early" in capsys.readouterr().out

    # A fixed --trace replaces the one each manifest names.
    assert _p4(root, tmp_path / "sims3", "--trace", str(trace), "--keep-going") == 0
    assert "1/2 runs compared: 1 pass" in capsys.readouterr().out


def test_a_single_run_directory_is_its_own_campaign(tmp_path, tools, capsys) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    run = _hw_run(tmp_path / "hw", tools, "t_jsq_s0_u30_r1", "jsq", "u30", trace)
    assert _p4(run, tmp_path / "sims") == 0
    assert "P4: 1 hardware runs" in capsys.readouterr().out


def test_a_dry_run_lists_the_first_five_and_replays_nothing(tmp_path, tools, capsys) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    for i in range(7):
        _hw_run(tmp_path / "hw", tools, f"t_jsq_s0_u30_r{i}", "jsq", "u30", trace)
    assert _p4(tmp_path / "hw", tmp_path / "sims", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("would replay") == 5
    assert "... and 2 more" in out
    assert tools.calls == []


def test_a_missing_or_empty_hardware_root_is_refused(tmp_path, tools, capsys) -> None:
    assert _p4(tmp_path / "nowhere", tmp_path / "sims") == 1
    assert "hardware root not found" in capsys.readouterr().out
    (tmp_path / "empty").mkdir()
    assert _p4(tmp_path / "empty", tmp_path / "sims") == 1
    assert "no runs with manifest.json" in capsys.readouterr().out


def test_contrasts_run_the_summary_and_return_the_checks_verdict(
    campaign, tools, tmp_path, capsys
) -> None:
    """The verdict that decides citability is contrast_check's, so its exit code is P4's."""
    (campaign / "summary.json").write_text("{}")
    tools.step_rc["contrast_check"] = 2
    assert _p4(campaign, tmp_path / "sims", "--contrasts") == 2
    steps = [c for c in tools.calls if "exec:java" not in " ".join(c)]
    assert [
        s
        for s in ("runset", "campaign_summary", "contrast_check")
        if any(s in " ".join(c) for c in steps)
    ] == [
        "runset",
        "campaign_summary",
        "contrast_check",
    ]
    assert "contrast_check said 2" in capsys.readouterr().out


def test_contrasts_need_the_hardware_summary_and_stop_at_a_failed_step(
    campaign, tools, tmp_path, capsys
) -> None:
    assert _p4(campaign, tmp_path / "a", "--contrasts") == 1
    assert "run tools/campaign_summary.py on the hardware run set first" in capsys.readouterr().out

    (campaign / "summary.json").write_text("{}")
    tools.step_rc["runset"] = 1
    assert _p4(campaign, tmp_path / "b", "--contrasts") == 1
    assert "P4 contrasts FAILED at runset" in capsys.readouterr().out


def test_contrasts_are_not_checked_over_comparison_errors(
    campaign, tools, tmp_path, capsys
) -> None:
    (campaign / "summary.json").write_text("{}")
    tools.sim_fails.add("t_jsq_s0_u30_r1")
    assert _p4(campaign, tmp_path / "sims", "--contrasts", "--keep-going") == 1
    assert not [c for c in tools.calls if "contrast_check" in " ".join(c)]


def test_a_manifest_that_cannot_be_staged_fails_that_run(tmp_path) -> None:
    ok, tail = p4.run_one_sim(
        tmp_path / "t.jsonl", tmp_path / "gone.json", tmp_path / "out", tmp_path
    )
    assert ok is False
    assert "could not stage" in tail


def test_no_maven_is_an_error(monkeypatch) -> None:
    monkeypatch.setattr(p4.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="mvn not found"):
        p4.find_mvn()


def test_a_relative_out_is_placed_under_the_repository(
    campaign, tools, tmp_path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tools").symlink_to(p4.REPO_ROOT / "tools")
    monkeypatch.setattr(p4, "REPO_ROOT", repo)
    assert (
        p4.main(["--hardware-root", str(campaign), "--out", "p4_out", "--cost-models", "models"])
        == 0
    )
    assert (tmp_path / "repo" / "p4_out" / "t_jsq_s0_u30_r1_sim").is_dir()
    sim = next(c for c in tools.calls if "exec:java" in " ".join(c))
    assert f"--cost-models {tmp_path / 'repo' / 'models'}" in sim[-1]


def test_an_f23_run_that_never_compared_is_an_error_not_a_miss(
    campaign, tools, tmp_path, monkeypatch, capsys
) -> None:
    """Python exits 2 when it cannot open a script, and argparse exits 2 on a usage error, and
    f23_compare also uses 2 for "outside tolerance". An exit of 2 with no comparison printed
    is a failure to compare, which F-23 counts towards neither pass nor miss."""
    monkeypatch.setattr(
        p4,
        "compare_one",
        lambda *a: (2, "python: can't open file '/x/tools/f23_compare.py': [Errno 2]"),
    )
    assert _p4(campaign, tmp_path / "sims", "--keep-going") == 1
    out = capsys.readouterr().out
    assert "4/4 runs compared: 0 pass, 0 outside tolerance, 4 errors" in out
    assert "P4 FAILED" in out


def test_an_error_line_that_does_not_parse_leaves_the_numbers_empty(
    campaign, tools, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(p4, "compare_one", lambda *a: (0, "  Error:    p50=  n/a%   p95=  n/a%"))
    assert _p4(campaign, tmp_path / "sims") == 0
    assert "4 pass" in capsys.readouterr().out


def test_a_comparison_that_fails_stops_the_campaign(campaign, tools, tmp_path, capsys) -> None:
    """The hardware run's own client log is gone, so f23_compare cannot compare, exits 1, and
    P4 stops there rather than report the rest."""
    (campaign / "t_jsq_s0_u30_r1" / "client_t_jsq_s0_u30_r1.jsonl").unlink()
    assert _p4(campaign, tmp_path / "sims") == 1
    out = capsys.readouterr().out
    assert "could not read the hardware run" in out
    assert "1/4 runs compared: 0 pass, 0 outside tolerance, 1 errors" in out


def test_with_no_out_the_sims_go_to_a_temporary_directory(
    campaign, tools, tmp_path, monkeypatch, capsys
) -> None:
    target = tmp_path / "p4_tmp"
    monkeypatch.setattr(p4.tempfile, "mkdtemp", lambda prefix: str(target))
    assert p4.main(["--hardware-root", str(campaign)]) == 0
    assert f"sim out: {target}" in capsys.readouterr().out
    assert (target / "t_jsq_s0_u30_r1_sim").is_dir()


def test_a_dry_run_of_a_few_runs_lists_them_all(campaign, tools, tmp_path, capsys) -> None:
    assert _p4(campaign, tmp_path / "sims", "--dry-run") == 0
    out = capsys.readouterr().out
    assert out.count("would replay") == 4
    assert "more" not in out


def test_a_manifest_already_staged_is_not_copied_onto_itself(tmp_path, tools) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("{}\n")
    out = tmp_path / "out"
    out.mkdir()
    staged = out / "hw_manifest.json"
    staged.write_text(json.dumps({"run_id": "t_jsq_s0_u30_r1"}))
    tools.hw_latencies["t_jsq_s0_u30_r1"] = [900.0] * 5
    ok, _ = p4.run_one_sim(trace, staged, out, tmp_path)
    assert ok is True


def test_a_bad_manifest_stops_the_campaign_without_keep_going(tmp_path, tools, capsys) -> None:
    (tmp_path / "hw" / "t_a").mkdir(parents=True)
    (tmp_path / "hw" / "t_a" / "manifest.json").write_text("{not json")
    (tmp_path / "hw" / "t_b").mkdir()
    (tmp_path / "hw" / "t_b" / "manifest.json").write_text("{not json either")
    assert _p4(tmp_path / "hw", tmp_path / "sims") == 0
    out = capsys.readouterr().out
    assert out.count("could not read manifest") == 1
    assert "Stopped early (1 failures)" in out


def test_with_keep_going_a_failed_comparison_does_not_stop_the_rest(
    campaign, tools, tmp_path, capsys
) -> None:
    (campaign / "t_jsq_s0_u30_r1" / "client_t_jsq_s0_u30_r1.jsonl").unlink()
    assert _p4(campaign, tmp_path / "sims", "--keep-going") == 1
    assert "4/4 runs compared: 3 pass, 0 outside tolerance, 1 errors" in capsys.readouterr().out
