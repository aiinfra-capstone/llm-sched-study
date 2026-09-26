"""tools/hw_runs.py at its process boundaries: the scheduler, ssh, rsync, and main().

These are the branches a silent failure has come from, so none is excluded from coverage.
The scheduler is a shell script standing in for `mvn` on PATH, so starting, waiting for
the ready line, and the SIGTERM/SIGKILL shutdown are the real code. ssh and rsync are
replaced at `subprocess.run`, which is where the script hands them off.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import hw_runs
import pytest
from hw_support import LINE, install, remote_campaign
from support import CONFIGS, REPO_ROOT, campaign_dict

# ------------------------------------------------------------------------ the scheduler


def _fake_mvn(tmp_path: Path, body: str, monkeypatch) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    mvn = bindir / "mvn"
    mvn.write_text("#!/bin/sh\n" + body)
    mvn.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _scheduler(tmp_path: Path) -> hw_runs.Scheduler:
    c = hw_runs.Campaign.from_dict(campaign_dict(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    return hw_runs.Scheduler(c, run_dir / "manifest.pre.json", run_dir)


def test_the_scheduler_command_names_the_manifest_the_log_dir_and_every_worker(
    tmp_path, monkeypatch
) -> None:
    _fake_mvn(tmp_path, "exit 0\n", monkeypatch)
    s = _scheduler(tmp_path)
    args = s.cmd[-1]
    assert s.cmd[0].endswith("/mvn")
    assert "-Dexec.mainClass=com.sched.live.LiveSchedulerApp" in s.cmd
    assert args.startswith(f"-Dexec.args={tmp_path / 'run' / 'manifest.pre.json'} --port 50051")
    assert f"--log-dir {tmp_path / 'run'}" in args
    assert "--worker gtx1650ti=10.42.0.1:50061" in args
    assert "--worker rtx3050=10.42.0.12:50061" in args


def test_a_scheduler_that_says_it_is_ready_starts_and_stops_on_sigterm(
    tmp_path, monkeypatch
) -> None:
    _fake_mvn(
        tmp_path,
        "trap 'echo stopping; exit 0' TERM\necho 'INFO Live Control Plane active'\n"
        "while true; do sleep 0.05; done\n",
        monkeypatch,
    )
    s = _scheduler(tmp_path)
    s.start(timeout_s=10)
    assert s.proc.poll() is None
    s.stop(timeout_s=5)
    assert s.proc.poll() is not None
    assert "Live Control Plane active" in s.console.read_text()
    s.stop()  # a second stop is a no-op


def test_a_scheduler_that_ignores_sigterm_is_killed(tmp_path, monkeypatch) -> None:
    _fake_mvn(
        tmp_path,
        "trap '' TERM\necho 'Live Control Plane active'\nwhile true; do sleep 0.05; done\n",
        monkeypatch,
    )
    s = _scheduler(tmp_path)
    s.start(timeout_s=10)
    s.stop(timeout_s=0.3)
    assert s.proc.returncode is not None


def test_a_scheduler_that_exits_before_it_is_ready_is_an_error(tmp_path, monkeypatch) -> None:
    _fake_mvn(tmp_path, "echo 'BUILD FAILURE'\nexit 1\n", monkeypatch)
    s = _scheduler(tmp_path)
    with pytest.raises(RuntimeError, match="exited before it was ready"):
        s.start(timeout_s=10)


def test_a_scheduler_that_never_becomes_ready_is_stopped(tmp_path, monkeypatch) -> None:
    _fake_mvn(tmp_path, "while true; do sleep 0.05; done\n", monkeypatch)
    s = _scheduler(tmp_path)
    with pytest.raises(RuntimeError, match="was not ready in 0.6 s"):
        s.start(timeout_s=0.6)
    assert s.proc.poll() is not None


def test_a_scheduler_that_resolved_another_snapshot_is_refused(tmp_path, monkeypatch) -> None:
    _fake_mvn(
        tmp_path,
        "echo 'Resolving snapshot cm_x to cm_y'\necho 'Live Control Plane active'\n"
        "while true; do sleep 0.05; done\n",
        monkeypatch,
    )
    s = _scheduler(tmp_path)
    with pytest.raises(RuntimeError, match="served a different snapshot"):
        s.start(timeout_s=10)
    assert s.proc.poll() is not None


def test_no_maven_on_path_is_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hw_runs.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="mvn not found"):
        _scheduler(tmp_path)


# ----------------------------------------------------------------------- worker logs


def test_a_remote_worker_log_is_pulled_with_rsync(tmp_path, monkeypatch) -> None:
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(hw_runs.subprocess, "run", run)
    got = hw_runs.pull_worker_log("rtx3050", "u@10.0.0.2:runs/logs/", "r1", tmp_path)
    assert got == tmp_path / "worker_rtx3050_r1.jsonl"
    assert calls == [
        ["rsync", "-a", "--timeout=30", "u@10.0.0.2:runs/logs/worker_rtx3050_r1.jsonl", str(got)]
    ]


def test_a_failed_rsync_returns_nothing_and_says_why(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        hw_runs.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 23, stdout="", stderr="no such file"),
    )
    assert hw_runs.pull_worker_log("rtx3050", "u@h:logs", "r1", tmp_path) is None
    assert (
        "could not pull worker_rtx3050_r1.jsonl from u@h: no such file" in capsys.readouterr().out
    )


def test_a_local_worker_log_is_copied_and_a_missing_one_is_reported(tmp_path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert hw_runs.pull_worker_log("n1", str(logs), "r1", run_dir) is None
    assert "no " in capsys.readouterr().out
    (logs / "worker_n1_r1.jsonl").write_text("{}\n")
    assert hw_runs.pull_worker_log("n1", str(logs), "r1", run_dir).read_text() == "{}\n"
    # A worker that already logs into the run directory is not copied onto itself.
    assert (
        hw_runs.pull_worker_log("n1", str(run_dir), "r1", run_dir) == run_dir / "worker_n1_r1.jsonl"
    )


# ------------------------------------------------------------------------- node reads


def _campaign_with_logs(tmp_path, logs: dict[str, str]) -> hw_runs.Campaign:
    d = campaign_dict(tmp_path)
    d["workers"] = {n: {"endpoint": "e", "logs": v} for n, v in logs.items()}
    return hw_runs.Campaign.from_dict(d)


def test_on_node_uses_sh_locally_and_ssh_remotely_and_keeps_the_command_whole(
    tmp_path, monkeypatch
) -> None:
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="out\n", stderr="")

    monkeypatch.setattr(hw_runs.subprocess, "run", run)
    c = _campaign_with_logs(
        tmp_path,
        {"local": "runs/worker_logs", "abs": "/data/a:b/logs", "remote": "me@10.0.0.9:runs/logs"},
    )
    command = "sha256sum '/opt/my models/1b.gguf' && echo 'a b'"
    for node in ("local", "abs", "remote"):
        assert hw_runs._on_node(c, node, command) == "out\n"
    assert calls[0] == ["sh", "-c", command]
    assert calls[1] == ["sh", "-c", command]
    assert calls[2] == ["ssh", "-o", "BatchMode=yes", "me@10.0.0.9", command]


def _provenance(tmp_path, monkeypatch, ps_line: str) -> dict:
    def on_node(c, node_id, command, timeout_s=20, **kw):
        if command == hw_runs.ENGINE_PS:
            return ps_line
        if command.startswith("sha256sum"):
            return "aa11  /opt/llama/bin/libllama.so\nbb22  /opt/llama/bin/libggml-cuda.so\n\ncc33  /m/1b.gguf\n"
        if "--version" in command:
            return "version: 10569 (1a2b3c4)\nbuilt with cc 14 for x86_64\n"
        raise AssertionError(command)

    monkeypatch.setattr(hw_runs, "_on_node", on_node)
    c = _campaign_with_logs(tmp_path, {"n1": "runs/worker_logs"})
    return hw_runs.engine_provenance(c)["n1"]


@pytest.mark.parametrize(
    ("flag", "expected"),
    [(" --cache-ram 0", True), (" --cache-ram=0", True), ("", False), (" --cache-ram 8192", False)],
)
def test_engine_provenance_reads_the_serving_process(tmp_path, monkeypatch, flag, expected) -> None:
    line = f"4242 Mon Sep 15 10:00:00 2026 /opt/llama/bin/llama-server -m /m/1b.gguf -ngl 99{flag}"
    prov = _provenance(tmp_path, monkeypatch, line)
    assert prov["command"] == f"/opt/llama/bin/llama-server -m /m/1b.gguf -ngl 99{flag}"
    assert prov["version"] == "version: 10569 (1a2b3c4) built with cc 14 for x86_64"
    assert prov["sha256"] == {"libllama.so": "aa11", "libggml-cuda.so": "bb22", "1b.gguf": "cc33"}
    assert prov["cache_ram_0"] is expected


def test_engine_provenance_without_a_process_records_the_error(tmp_path, monkeypatch) -> None:
    assert _provenance(tmp_path, monkeypatch, "") == {"error": "no llama-server process found"}


def test_engine_provenance_without_a_model_flag_hashes_the_libraries_only(
    tmp_path, monkeypatch
) -> None:
    prov = _provenance(
        tmp_path, monkeypatch, "1 Mon Sep 15 10:00:00 2026 /opt/llama/bin/llama-server"
    )
    assert prov["command"] == "/opt/llama/bin/llama-server"


# ------------------------------------------------------------------------------ main


def _write(tmp_path, d) -> str:
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(d))
    return str(path)


def _tiny(tmp_path, **over) -> dict:
    base = {
        "repeats": 1,
        "repeat_seeds": [3],
        "policies": ["jsq", "wjsq"],
        "points": [{"name": "u20", "pool_utilisation": 0.2}],
    }
    return remote_campaign(tmp_path, **(base | over))


def test_a_dry_run_prints_the_plan_and_starts_nothing(tmp_path, monkeypatch, capsys) -> None:
    pool = install(monkeypatch, tmp_path)
    rc = hw_runs.main([_write(tmp_path, _tiny(tmp_path)), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 runs, about" in out
    assert "no --clock-sync" in out
    assert "gen 3 sched 3" in out
    assert pool.schedulers == [] and pool.ssh_calls == {}


def test_repeats_that_replay_one_trace_are_warned_about(tmp_path, monkeypatch, capsys) -> None:
    install(monkeypatch, tmp_path)
    from dataplane.harness import gen_trace

    trace = tmp_path / "t.jsonl"
    sha = gen_trace.generate(json.loads((CONFIGS / "trace_anchor_1b.json").read_text()), trace)
    d = _tiny(tmp_path, repeats=2, trace=str(trace), trace_sha256=sha)
    d.pop("trace_config")
    d.pop("repeat_seeds")
    clock = tmp_path / "clock.json"
    clock.write_text(json.dumps({"reference": "gtx1650ti", "hosts": {}}))
    rc = hw_runs.main([_write(tmp_path, d), "--dry-run", "--clock-sync", str(clock)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "every repeat replays one trace with one scheduler seed" in out
    assert "no --clock-sync" not in out


def test_a_config_that_fails_its_checks_is_refused_with_exit_2(
    tmp_path, monkeypatch, capsys
) -> None:
    install(monkeypatch, tmp_path)
    rc = hw_runs.main([_write(tmp_path, _tiny(tmp_path, advertise=None)), "--dry-run"])
    assert rc == 2
    assert "refusing: advertise is required" in capsys.readouterr().out


def test_a_full_campaign_runs_every_run_and_reports_them_valid(
    tmp_path, monkeypatch, capsys
) -> None:
    pool = install(monkeypatch, tmp_path)
    pool.engines(("line", LINE))
    rc = hw_runs.main([_write(tmp_path, _tiny(tmp_path))])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(pool.schedulers) == 2
    assert "all runs valid" in out
    assert "next: uv run --project dataplane runset" in out


def _finished(run: hw_runs.Run, *, valid: bool) -> Path:
    run_dir = run.workload.out_root / run.run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"validity": {"valid": valid}}))
    (run_dir / f"client_{run.run_id}.jsonl").write_text("")
    return run_dir


def test_finished_runs_are_skipped_on_resume(tmp_path, monkeypatch, capsys) -> None:
    pool = install(monkeypatch, tmp_path)
    d = _tiny(tmp_path, check_engine_restarts=False)
    runs = hw_runs.plan(hw_runs.Campaign.from_dict(d))
    _finished(runs[0], valid=True)
    assert hw_runs.main([_write(tmp_path, d)]) == 0
    assert "already done, skipped" in capsys.readouterr().out
    assert len(pool.schedulers) == 1


def test_resume_state_reads_the_manifest_validity(tmp_path) -> None:
    run_dir = tmp_path / "r1"
    assert hw_runs.resume_state(run_dir, "r1") == "todo"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"validity": {"valid": True}}))
    # A manifest with no client log beside it is a run that never finished writing.
    assert hw_runs.resume_state(run_dir, "r1") == "todo"
    (run_dir / "client_r1.jsonl").write_text("")
    assert hw_runs.resume_state(run_dir, "r1") == "done"
    (run_dir / "manifest.json").write_text(json.dumps({"validity": {"valid": False}}))
    assert hw_runs.resume_state(run_dir, "r1") == "invalid"
    (run_dir / "manifest.json").write_text("{}")
    assert hw_runs.resume_state(run_dir, "r1") == "invalid"


def test_an_invalid_run_is_reported_not_skipped_by_default(tmp_path, monkeypatch, capsys) -> None:
    pool = install(monkeypatch, tmp_path)
    d = _tiny(tmp_path, check_engine_restarts=False)
    runs = hw_runs.plan(hw_runs.Campaign.from_dict(d))
    run_dir = _finished(runs[0], valid=False)
    before = sorted((p.name, p.read_bytes()) for p in run_dir.iterdir())

    assert hw_runs.main([_write(tmp_path, d)]) == 1
    out = capsys.readouterr().out
    assert f"{runs[0].run_id}  recorded as invalid" in out
    assert "--rerun-invalid" in out
    assert f"1 run(s) not usable: ['{runs[0].run_id}']" in out
    assert sorted((p.name, p.read_bytes()) for p in run_dir.iterdir()) == before
    assert len(pool.schedulers) == 1


def test_rerun_invalid_moves_the_old_run_aside_and_runs_it_again(
    tmp_path, monkeypatch, capsys
) -> None:
    pool = install(monkeypatch, tmp_path)
    d = _tiny(tmp_path, check_engine_restarts=False)
    runs = hw_runs.plan(hw_runs.Campaign.from_dict(d))
    run_dir = _finished(runs[0], valid=False)

    assert hw_runs.main([_write(tmp_path, d), "--rerun-invalid"]) == 0
    aside = run_dir.with_name(f"{runs[0].run_id}.invalid")
    assert json.loads((aside / "manifest.json").read_text()) == {"validity": {"valid": False}}
    assert json.loads((run_dir / "manifest.json").read_text())["validity"]["valid"] is True
    assert len(pool.schedulers) == 2
    assert f"moved to {aside.name}" in capsys.readouterr().out

    # A second invalid attempt never overwrites the first one set aside.
    (run_dir / "manifest.json").write_text(json.dumps({"validity": {"valid": False}}))
    assert hw_runs.main([_write(tmp_path, d), "--rerun-invalid"]) == 0
    assert aside.is_dir() and run_dir.with_name(f"{runs[0].run_id}.invalid2").is_dir()


def test_the_scheduler_is_compiled_before_the_first_run(tmp_path, monkeypatch, capsys) -> None:
    pool = install(monkeypatch, tmp_path)
    d = _tiny(tmp_path, check_engine_restarts=False)
    assert hw_runs.main([_write(tmp_path, d)]) == 0
    assert pool.maven == ["compile", "exec:java", "exec:java"]

    pool.maven.clear()
    pool.compile_fails = True
    assert hw_runs.main([_write(tmp_path, d)]) == 2
    assert "refusing: mvn -q compile failed" in capsys.readouterr().out
    assert pool.maven == ["compile"]


def test_compile_scheduler_runs_maven_compile_and_refuses_a_failure(tmp_path, monkeypatch) -> None:
    _fake_mvn(tmp_path, "exit 0\n", monkeypatch)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    hw_runs.compile_scheduler(run=run)
    (cmd,) = calls
    assert cmd[0].endswith("/mvn")
    assert cmd[1:] == ["-q", "-f", str(hw_runs.REPO_ROOT / "controlplane" / "pom.xml"), "compile"]

    def fails(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="[ERROR] COMPILATION ERROR", stderr="")

    with pytest.raises(RuntimeError, match="COMPILATION ERROR"):
        hw_runs.compile_scheduler(run=fails)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="mvn not found"):
        hw_runs.compile_scheduler(run=run)


@pytest.mark.parametrize("keep_going", [False, True])
def test_a_run_that_fails_to_complete_stops_the_campaign_unless_told_to_keep_going(
    tmp_path, monkeypatch, capsys, keep_going
) -> None:
    install(monkeypatch, tmp_path)
    attempted = []

    def run_one(c, run, header, trace):
        attempted.append(run.run_id)
        raise RuntimeError("the scheduler exited before it was ready")

    monkeypatch.setattr(hw_runs, "run_one", run_one)
    d = _tiny(tmp_path, check_engine_restarts=False)
    rc = hw_runs.main([_write(tmp_path, d)] + (["--keep-going"] if keep_going else []))
    out = capsys.readouterr().out
    assert rc == 1
    assert len(attempted) == (2 if keep_going else 1)
    assert "failed: the scheduler exited before it was ready" in out
    assert "run(s) not usable" in out


def test_engine_provenance_is_printed_and_a_missing_cache_ram_flag_warned(
    tmp_path, monkeypatch, capsys
) -> None:
    install(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hw_runs,
        "engine_provenance",
        lambda c: {
            "gtx1650ti": {"version": "b10569", "sha256": {}, "cache_ram_0": False},
            "rtx3050": {"error": "no llama-server process found"},
        },
    )
    monkeypatch.setattr(hw_runs, "run_one", lambda c, run, header, trace: True)
    assert hw_runs.main([_write(tmp_path, _tiny(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "gtx1650ti: b10569" in out
    assert "rtx3050: no llama-server process found" in out
    assert "gtx1650ti: llama-server is not running with --cache-ram 0" in out


# ---------------------------------------------------------------- the committed configs


def _repo_with_real_configs(tmp_path) -> Path:
    """A repository root whose configs are the committed ones and whose traces directory
    is a scratch copy, so a dry run can generate traces without writing into runs/."""
    root = tmp_path / "repo"
    (root / "runs" / "traces").mkdir(parents=True)
    (root / "dataplane").symlink_to(REPO_ROOT / "dataplane")
    for trace in (REPO_ROOT / "runs" / "traces").glob("*.trace.jsonl"):
        (root / "runs" / "traces" / trace.name).symlink_to(trace)
    return root


HW_CONFIGS = sorted(p.name for p in CONFIGS.glob("hw_*.json"))


def _stand_in_trace(workload: dict, tmp_path) -> None:
    """Generate a gitignored trace from its committed config, and point the workload at it.

    `runs/traces/<shape>_1b.trace.jsonl` comes from `dataplane/configs/trace_<shape>_1b.json`.
    No manifest records the generator commit the committed hash was taken at, so the file
    cannot come back byte for byte; the dry run is checked against the stream the committed
    config describes today, under that file's own hash.
    """
    from dataplane.harness import gen_trace

    shape = Path(workload["trace"]).name.removesuffix(".trace.jsonl")
    config = json.loads((CONFIGS / f"trace_{shape}.json").read_text())
    path = tmp_path / "stand_in" / Path(workload["trace"]).name
    workload["trace_sha256"] = gen_trace.generate(config, path)
    workload["trace"] = str(path)


@pytest.mark.parametrize("name", HW_CONFIGS)
def test_every_committed_campaign_passes_its_checks_and_dry_runs(
    tmp_path, monkeypatch, capsys, name
) -> None:
    d = json.loads((CONFIGS / name).read_text())
    for w in d.get("workloads", [d]):
        if "trace" in w and not (REPO_ROOT / w["trace"]).is_file():
            _stand_in_trace(w, tmp_path)
    config = tmp_path / name
    config.write_text(json.dumps(d))
    monkeypatch.setattr(hw_runs, "REPO_ROOT", _repo_with_real_configs(tmp_path))
    assert hw_runs.main([str(config), "--dry-run"]) == 0, capsys.readouterr().out


@pytest.mark.parametrize(
    "name", sorted(n for n in HW_CONFIGS if "trace" in json.loads((CONFIGS / n).read_text()))
)
def test_a_committed_campaign_dry_runs_on_a_stand_in_trace(
    tmp_path, monkeypatch, capsys, name
) -> None:
    """The path a machine without the gitignored traces takes, run on every machine."""
    d = json.loads((CONFIGS / name).read_text())
    _stand_in_trace(d, tmp_path)
    config = tmp_path / name
    config.write_text(json.dumps(d))
    monkeypatch.setattr(hw_runs, "REPO_ROOT", _repo_with_real_configs(tmp_path))
    assert hw_runs.main([str(config), "--dry-run"]) == 0, capsys.readouterr().out


def test_the_seeded_anchor_campaign_has_distinct_seeds() -> None:
    d = json.loads((CONFIGS / "hw_seeded_anchor_3050.json").read_text())
    assert len(d["repeat_seeds"]) == d["repeats"] == len(set(d["repeat_seeds"]))


@pytest.mark.parametrize("name", ["trace_heavytail_1b.json", "trace_heavytail_mmpp_1b.json"])
def test_the_heavy_tail_trace_configs_generate_traces_that_pass_contracts_check(
    tmp_path, capsys, name
) -> None:
    import runpy

    from dataplane.harness import gen_trace

    path = tmp_path / f"{name.removesuffix('.json')}.trace.jsonl"
    gen_trace.generate(json.loads((CONFIGS / name).read_text()), path)
    check = runpy.run_path(str(REPO_ROOT / "contracts" / "check.py"))
    rc = check["main"](["--validate", str(path), "--schema", "trace.schema.json"])
    assert rc == 0, capsys.readouterr().out


def test_a_dirty_tree_is_refused_without_allow_dirty(tmp_path, monkeypatch, capsys) -> None:
    """A git sha names committed code. A campaign from a tree with edits on top would record
    shas that do not name what ran."""
    from dataplane.harness import manifest as manifest_mod

    pool = install(monkeypatch, tmp_path)
    dirty = dict.fromkeys(manifest_mod.COMPONENTS, True)
    monkeypatch.setattr(manifest_mod, "git_dirty", lambda root=None: dirty)
    d = _tiny(tmp_path, check_engine_restarts=False)
    assert hw_runs.main([_write(tmp_path, d)]) == 2
    assert "refusing: the working tree has uncommitted changes" in capsys.readouterr().out
    assert pool.schedulers == [] and pool.maven == []

    assert hw_runs.main([_write(tmp_path, d), "--allow-dirty"]) == 0
    runs = hw_runs.plan(hw_runs.Campaign.from_dict(d))
    man = json.loads((runs[0].workload.out_root / runs[0].run_id / "manifest.json").read_text())
    assert man["config"]["allow_dirty"] is True
    assert man["git_dirty"] == dirty
