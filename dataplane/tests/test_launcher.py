"""F-9a — the launcher, which is the only thing that can catch co-location.

A run with two logical nodes on one host still produces plausible numbers: the slow node
looks slow, the fast node looks fast, and the ratio between them is partly an artifact of
them contending for PCIe and memory bandwidth. Nothing downstream can detect that, so it
has to be caught before the run.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from dataplane.harness import launch
from dataplane.harness.launch import build_nodes, colocated_count, main, validity_for

_EC = {"ngl": 20, "threads": 6, "parallel": 4}


def _node(node_id: str, host: str, **over):
    return {
        "node_id": node_id,
        "host": host,
        "engine": "llamacpp",
        "engine_version": "b10569+cuda13.2",
        "model": "Meta-Llama-3-8B-Instruct",
        "quant": "Q4_K_M",
        "gpu": "GTX 1650 Ti",
        "engine_config": dict(_EC),
        "prefix_caching": False,
        "max_batch": 4,
    } | over


def test_two_nodes_on_one_host_are_refused() -> None:
    with pytest.raises(ValueError, match="co-located"):
        build_nodes([_node("n1", "boxA"), _node("n2", "boxA")])


def test_co_location_can_be_forced_but_never_becomes_valid() -> None:
    """`allow_colocation` makes a smoke run runnable, not analysable — the validity block
    still counts the co-located nodes and the manifest still marks the run invalid."""
    nodes = build_nodes([_node("n1", "boxA"), _node("n2", "boxA")], allow_colocation=True)
    validity = validity_for(nodes)

    assert colocated_count(nodes) == 2
    assert validity.colocated_nodes == 2
    assert not validity.valid
    assert any("contention" in r for r in validity.reasons())


def test_distinct_hosts_pass() -> None:
    nodes = build_nodes([_node("n1", "boxA"), _node("n2", "boxB")])
    assert colocated_count(nodes) == 0
    assert validity_for(nodes).valid
    assert all(n["role"] == "pool" for n in nodes)


def test_the_engine_gap_probe_may_share_a_box() -> None:
    """F-9b's probe is a measured condition, not a pool member: it never runs at the same
    time as the pool, so it cannot contend with it."""
    nodes = build_nodes(
        [
            _node("n1", "boxA"),
            _node("probe", "boxA", engine="vllm", role="engine_gap_probe", quant="AWQ"),
        ]
    )
    assert colocated_count(nodes) == 0


def test_vllm_cannot_enter_the_pool() -> None:
    """An engine effect inside R is a confound no hypothesis in §2 can decompose."""
    with pytest.raises(ValueError, match="role='engine_gap_probe'"):
        build_nodes([_node("n1", "boxA", engine="vllm")])


def test_a_pool_running_two_models_is_refused() -> None:
    with pytest.raises(ValueError, match="not homogeneous"):
        build_nodes([_node("n1", "boxA"), _node("n2", "boxB", model="Llama-3.2-1B-Instruct")])


def test_engine_config_cannot_be_defaulted() -> None:
    """Under F-9a these three numbers ARE the experimental condition."""
    with pytest.raises(ValueError, match="engine_config is missing"):
        build_nodes([_node("n1", "boxA", engine_config={"ngl": 20})])


def test_a_node_missing_c6_fields_is_refused() -> None:
    with pytest.raises(ValueError, match="missing C-6 fields"):
        build_nodes([{"node_id": "n1", "host": "boxA"}])


def test_an_empty_pool_and_duplicate_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="at least one node"):
        build_nodes([])
    with pytest.raises(ValueError, match="duplicate node_id"):
        build_nodes([_node("n1", "boxA"), _node("n1", "boxB")])


def test_cli_writes_the_node_block_and_flags_colocation(tmp_path, capsys) -> None:
    pool = tmp_path / "pool.json"
    pool.write_text(json.dumps([_node("n1", "boxA"), _node("n2", "boxB")]))
    out = tmp_path / "nodes.json"

    assert main([str(pool), "--out", str(out)]) == 0
    assert len(json.loads(out.read_text())) == 2
    assert "2 node(s) across 2 host(s)" in capsys.readouterr().out

    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps([_node("n1", "boxA"), _node("n2", "boxA")]))
    assert main([str(shared), "--allow-colocation"]) == 1
    assert "cannot be analysed" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# Engine reads, shared by hw_runs and anchors
# --------------------------------------------------------------------------------------


def test_engine_verdict_covers_same_changed_died_unknown() -> None:
    line = "4242 Mon Sep 15 10:00:00 2026 /opt/llama/bin/llama-server -m /m/1b.gguf"
    other = "5151 Mon Sep 15 10:07:00 2026 /opt/llama/bin/llama-server -m /m/1b.gguf"
    assert launch.engine_verdict(line, line) == "same"
    assert launch.engine_verdict(line, other) == "changed"
    # ps ran after the run and found no llama-server: the engine that served it is gone.
    assert launch.engine_verdict(line, "") == "died"
    # A read that failed teaches nothing, before or after, and neither does a run whose
    # engine was already missing when it started.
    assert launch.engine_verdict(line, None) == "unknown"
    assert launch.engine_verdict(None, line) == "unknown"
    assert launch.engine_verdict("", line) == "unknown"


def test_engine_processes_reads_locally_or_over_ssh_and_retries_a_failed_read(
    monkeypatch,
) -> None:
    calls = []
    answers = {
        "local": [subprocess.CompletedProcess([], 0, stdout="1  a   b\n", stderr="")],
        "u@far": [
            subprocess.CompletedProcess([], 255, stdout="", stderr="refused"),
            subprocess.CompletedProcess([], 1, stdout="", stderr=""),
        ],
        "u@gone": [subprocess.CompletedProcess([], 255, stdout="", stderr="refused")] * 2,
        "u@slow": [],
    }

    def run(cmd, **kw):
        where = cmd[3] if cmd[0] == "ssh" else "local"
        calls.append((where, cmd))
        if not answers[where]:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return answers[where].pop(0)

    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    nodes = [
        {"node_id": "here"},
        {"node_id": "far", "ssh": "u@far"},
        {"node_id": "gone", "ssh": "u@gone"},
        {"node_id": "slow", "ssh": "u@slow"},
    ]
    got = launch.engine_processes(nodes, run=run)
    assert got == {"here": "1 a b", "far": "", "gone": None, "slow": None}
    local = next(cmd for where, cmd in calls if where == "local")
    assert local == ["sh", "-c", launch.ENGINE_PS]
    assert [w for w, _ in calls].count("u@far") == 2
    assert [w for w, _ in calls].count("u@gone") == 2
