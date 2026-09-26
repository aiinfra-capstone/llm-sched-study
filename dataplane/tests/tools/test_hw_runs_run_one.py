"""tools/hw_runs.py during and after a run: what makes a run valid, and what the manifest says.

The validity block is the run's own verdict on itself, and a sweep keeps or drops the run
on it. Two counts are decided here rather than in the replay client: failures during warmup,
and engine restarts, which only a read of each node's llama-server can see.

Engine reads, pinned from the 2026-09-15 decision. A read that could not be made is unknown,
never "no restart" and never "restart":

    before                     after                         verdict
    process found              same process                  same     valid on this check
    process found              different pid or start time   changed  engine_restarts += 1
    process found              ssh fine, no process          died     engine_restarts += 1
    process found              ssh failed after retries      unknown  engine_unchecked += 1
    ssh fine, no process       -                             the run does not start
    ssh failed after retries   -                             the run does not start

Names these tests introduce:

    validity["engine_unchecked"]                    its own counter; non-zero is invalid
    config["engine_check"][node_id]                 {"before": str, "after": str | None,
                                                     "verdict": "same"|"changed"|"died"|"unknown"}
    config["engine_check"] == "disabled"            check_engine_restarts was false
"""

from __future__ import annotations

import contextlib
import json

import hw_runs
import pytest
from conftest import assert_conforms
from hw_support import HOSTS, LINE, OTHER_PID, first_run, install, manifest_of, remote_campaign

from dataplane.harness import manifest as manifest_mod

NODE = "rtx3050"
OTHER = "gtx1650ti"


@pytest.fixture
def pool(monkeypatch, tmp_path):
    return install(monkeypatch, tmp_path)


def _campaign(tmp_path, **over) -> hw_runs.Campaign:
    return hw_runs.Campaign.from_dict(remote_campaign(tmp_path, **over))


def _run_one(c: hw_runs.Campaign):
    run, trace = first_run(c)
    ok = hw_runs.run_one(c, run, trace.header, trace)
    return ok, manifest_of(c, run), run


# --------------------------------------------------------------- failures and warmup


def _with_records(pool, statuses: list[str]) -> None:
    pool.records = [
        {"req_id": f"r{i:06d}", "status": s, "chosen_node_from_ack": list(HOSTS)[i % 2]}
        for i, s in enumerate(statuses)
    ]


def test_a_request_lost_in_warmup_invalidates_the_run(pool, tmp_path) -> None:
    _with_records(pool, ["timeout", "ok", "ok", "ok"])
    ok, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert man["validity"]["dropped_requests"] == 1
    assert man["validity"]["valid"] is False
    assert ok is False


def test_a_failure_after_warmup_is_counted_once(pool, tmp_path) -> None:
    _with_records(pool, ["ok", "ok", "timeout", "ok"])
    pool.validity = manifest_mod.Validity(dropped_requests=1)
    _, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert man["validity"]["dropped_requests"] == 1


def test_dropped_requests_never_goes_down(pool, tmp_path) -> None:
    _with_records(pool, ["timeout", "ok", "engine_error", "ok"])
    pool.validity = manifest_mod.Validity(dropped_requests=5)
    _, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert man["validity"]["dropped_requests"] == 5


def test_a_clean_run_is_valid_and_its_manifest_conforms(pool, tmp_path, schema) -> None:
    ok, man, run = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert ok is True
    assert man["validity"]["valid"] is True
    assert pool.schedulers == [run.workload.out_root / run.run_id / "manifest.pre.json"]
    assert_conforms(schema("manifest"), [man], "manifest")


def test_a_worker_log_that_misses_a_dispatch_fails_the_run(pool, tmp_path, capsys, schema) -> None:
    """The counts are compared before the manifest is written, so the file on disk says
    what `run_one` returns. A manifest saying valid beside a partial worker log is a run
    that resume skips and the join then silently shortens."""
    pool.worker_lines = {NODE: 3, OTHER: 2}
    ok, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert ok is False
    assert man["validity"]["valid"] is False
    assert man["validity"]["worker_log_incomplete"] == 1
    assert man["config"]["worker_log_counts"] == {
        OTHER: {"dispatched": 3, "logged": 2},
        NODE: {"dispatched": 3, "logged": 3},
    }
    assert man["config_hash"] == manifest_mod.config_hash(man["config"])
    assert_conforms(schema("manifest"), [man], "manifest")
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "the join will be partial" in out


def test_the_manifest_records_the_generator_sha_from_the_trace_header(pool, tmp_path) -> None:
    """The trace hash covers the generator commit. Recording it is what lets a missing trace
    be regenerated byte for byte later (`tools/ensure_trace.py`)."""
    c = _campaign(tmp_path, check_engine_restarts=False)
    _, man, run = _run_one(c)
    trace = hw_runs.trace_for(run.workload, run.gen_seed)
    pre = json.loads((run.workload.out_root / run.run_id / "manifest.pre.json").read_text())
    recorded = trace.header["generator_git_sha"]
    assert pre["config"]["generator_git_sha"] == recorded
    assert man["config"]["generator_git_sha"] == recorded


def test_config_hash_changes_with_engine_provenance(pool, tmp_path) -> None:
    hashes = []
    for k, version in enumerate(("b10569", "b10570")):
        c = _campaign(tmp_path / str(k), check_engine_restarts=False)
        c.engine_provenance = {NODE: {"version": version}}
        _, man, run = _run_one(c)
        pre = json.loads((run.workload.out_root / run.run_id / "manifest.pre.json").read_text())
        assert pre["config"]["engine_provenance"] == {NODE: {"version": version}}
        assert pre["config_hash"] == manifest_mod.config_hash(pre["config"])
        hashes.append((pre["config_hash"], man["config_hash"]))
    assert hashes[0][0] != hashes[1][0]
    assert hashes[0][1] != hashes[1][1]


# ----------------------------------------------------------------- engine restarts


def _check(man: dict, node: str) -> dict:
    return man["config"]["engine_check"][node]


def test_the_same_process_before_and_after_is_valid(pool, tmp_path, schema) -> None:
    pool.engines(("line", LINE))
    ok, man, _ = _run_one(_campaign(tmp_path))
    v = man["validity"]
    assert (v["engine_restarts"], v["engine_unchecked"], v["valid"]) == (0, 0, True)
    assert ok is True
    for node in HOSTS:
        assert _check(man, node) == {"before": LINE, "after": LINE, "verdict": "same"}
    assert_conforms(schema("manifest"), [man], "manifest")


def test_a_different_process_after_is_a_restart(pool, tmp_path) -> None:
    pool.engines(("line", LINE))
    pool.engines(("line", LINE), ("line", OTHER_PID), node=NODE)
    ok, man, _ = _run_one(_campaign(tmp_path))
    v = man["validity"]
    assert (v["engine_restarts"], v["engine_unchecked"], v["valid"]) == (1, 0, False)
    assert _check(man, NODE)["verdict"] == "changed"
    assert _check(man, NODE)["after"] == OTHER_PID
    assert _check(man, OTHER)["verdict"] == "same"
    assert ok is False


def test_no_process_after_is_a_restart(pool, tmp_path) -> None:
    pool.engines(("line", LINE))
    pool.engines(("line", LINE), ("none",), node=NODE)
    _, man, _ = _run_one(_campaign(tmp_path))
    v = man["validity"]
    assert (v["engine_restarts"], v["engine_unchecked"], v["valid"]) == (1, 0, False)
    assert _check(man, NODE)["verdict"] == "died"


@pytest.mark.parametrize("failure", ["ssh", "timeout"])
def test_an_after_read_that_never_succeeds_is_unchecked_not_a_restart(
    pool, tmp_path, schema, failure
) -> None:
    pool.engines(("line", LINE))
    pool.engines(("line", LINE), (failure,), node=NODE)
    ok, man, _ = _run_one(_campaign(tmp_path))
    v = man["validity"]
    assert (v["engine_restarts"], v["engine_unchecked"], v["valid"]) == (0, 1, False)
    assert _check(man, NODE)["verdict"] == "unknown"
    assert _check(man, NODE)["after"] is None
    assert pool.ssh_calls[(NODE, "after")] > 1, "an ssh failure is retried before it counts"
    assert ok is False
    assert_conforms(schema("manifest"), [man], "manifest")
    assert any(
        "engine" in r
        for r in manifest_mod.Validity(**{k: x for k, x in v.items() if k != "valid"}).reasons()
    )


@pytest.mark.parametrize("phase", ["before", "after"])
def test_an_ssh_failure_that_clears_on_retry_is_a_normal_read(pool, tmp_path, phase) -> None:
    pool.engines(("line", LINE))
    flaky = [("ssh",), ("line", LINE)]
    if phase == "before":
        pool.engines(flaky, ("line", LINE), node=NODE)
    else:
        pool.engines(("line", LINE), flaky, node=NODE)
    ok, man, _ = _run_one(_campaign(tmp_path))
    assert man["validity"]["valid"] is True
    assert _check(man, NODE)["verdict"] == "same"
    assert ok is True


def test_a_disabled_check_says_so_rather_than_writing_zero(pool, tmp_path) -> None:
    _, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert man["config"]["engine_check"] == "disabled"
    assert pool.ssh_calls == {}


def test_an_engine_without_cache_ram_0_is_warned_about(pool, tmp_path, capsys) -> None:
    pool.engines(("line", LINE.replace(" --cache-ram 0", "")))
    _run_one(_campaign(tmp_path))
    assert f"{NODE}: llama-server runs without --cache-ram 0" in capsys.readouterr().out


BEFORE = {"found": ("line", LINE), "no process": ("none",), "ssh failed": ("ssh",)}
AFTER = {
    "same": ("line", LINE),
    "restarted": ("line", OTHER_PID),
    "no process": ("none",),
    "ssh failed": ("ssh",),
}


@pytest.mark.parametrize("before", list(BEFORE))
@pytest.mark.parametrize("after", list(AFTER))
def test_no_read_that_failed_on_either_side_yields_a_valid_run(
    pool, tmp_path, before, after
) -> None:
    pool.engines(("line", LINE))
    pool.engines(BEFORE[before], AFTER[after], node=NODE)
    c = _campaign(tmp_path)
    run, trace = first_run(c)
    # Refusing to start, by raising or by returning, is one acceptable outcome.
    with contextlib.suppress(Exception):
        hw_runs.run_one(c, run, trace.header, trace)
    man = manifest_of(c, run)
    if before == "found" and after == "same":
        assert man is not None and man["validity"]["valid"] is True
    else:
        assert man is None or man["validity"]["valid"] is False


# --------------------------------------------------- a before-read that fails stops the campaign


def _config_file(tmp_path, **over) -> str:
    d = remote_campaign(tmp_path, repeats=1, repeat_seeds=[3], **over)
    path = tmp_path / "campaign.json"
    path.write_text(json.dumps(d))
    return str(path)


@pytest.mark.parametrize("before", [("none",), ("ssh",), ("timeout",)])
@pytest.mark.parametrize("keep_going", [False, True])
def test_a_node_that_cannot_be_read_before_a_run_stops_the_campaign(
    pool, tmp_path, monkeypatch, capsys, before, keep_going
) -> None:
    monkeypatch.setattr(
        hw_runs, "engine_provenance", lambda c: {n: {"version": "v"} for n in HOSTS}
    )
    pool.engines(("line", LINE))
    pool.engines(before, ("line", LINE), node=NODE)
    argv = [_config_file(tmp_path)] + (["--keep-going"] if keep_going else [])

    rc = hw_runs.main(argv)

    out = capsys.readouterr().out
    assert rc != 0
    assert pool.schedulers == []
    assert pool.replays == []
    assert not list((tmp_path / "out").glob("*/manifest.json"))
    assert NODE in out
    assert "[2/" not in out, "the campaign went on to the next run"


def test_run_one_generates_its_own_trace_and_skips_undispatched_requests(pool, tmp_path) -> None:
    """Called without a trace, the run generates the repeat's trace. A request the scheduler
    never placed has no node to count against in the worker-log check, and is still lost."""
    pool.records = [
        {"req_id": "r000001", "status": "ok", "chosen_node_from_ack": NODE},
        {"req_id": "r000002", "status": "rejected", "chosen_node_from_ack": ""},
    ]
    c = _campaign(tmp_path, check_engine_restarts=False)
    run = hw_runs.plan(c)[0]
    ok = hw_runs.run_one(c, run, header={})
    man = manifest_of(c, run)
    assert man["trace_sha256"] == hw_runs.trace_for(run.workload, run.gen_seed).sha256
    assert man["validity"]["dropped_requests"] == 1
    assert ok is False


# ------------------------------------------------------------------- heartbeat gaps


def _summary(node: str, missed: int) -> dict:
    return {
        "type": "heartbeat_summary",
        "run_id": "r",
        "node_id": node,
        "last_seq": 400,
        "missed_beats": missed,
        "seq_regressions": 0,
        "at": "shutdown",
    }


def test_the_scheduler_summary_reaches_validity_heartbeat_gaps(pool, tmp_path, schema) -> None:
    """The live scheduler writes one summary per node at shutdown. The run's count is their
    sum, and a counted run has nothing unmeasured to report."""
    pool.scheduler_log = [
        {"type": "completion_observed", "req_id": "r000001", "node_id": NODE},
        _summary(OTHER, 2),
        _summary(NODE, 3),
    ]
    ok, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert ok is True
    assert man["validity"]["heartbeat_gaps"] == 5
    assert "heartbeat_gaps" not in man["validity"].get("unmeasured", [])
    assert_conforms(schema("manifest"), [man], "manifest")


@pytest.mark.parametrize(
    "log",
    [None, [{"type": "completion_observed", "req_id": "r000001", "node_id": NODE}]],
    ids=["no-log", "no-summary"],
)
def test_a_scheduler_log_without_a_summary_writes_null_and_an_unmeasured_entry(
    pool, tmp_path, schema, log
) -> None:
    """Null, not 0, and never fatal: the run is still a valid measurement of the pool, it
    just cannot say how stale the scheduler's view of it was."""
    pool.scheduler_log = log
    ok, man, _ = _run_one(_campaign(tmp_path, check_engine_restarts=False))
    assert ok is True
    assert man["validity"]["heartbeat_gaps"] is None
    assert "heartbeat_gaps" in man["validity"]["unmeasured"]
    assert_conforms(schema("manifest"), [man], "manifest")
