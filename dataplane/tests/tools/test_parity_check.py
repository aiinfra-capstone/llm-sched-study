"""tools/parity_check.py: do the live scheduler and the simulator decide alike?

The two vehicles share every policy class and nothing else, so a policy can be right in both
while the two still route differently. The sequences cannot be compared, because after the
first completion the pools hold different requests. What can be compared is the rule: where
both saw one state for one request and drew the same tie-break, they must have chosen the
same node.

The four cases the test plan names come first. Two of them are about what the tool must not
do: judge a pair that differs only in the draw, and report a pass when nothing was compared.
"""

from __future__ import annotations

import json

import parity_check as parity
import pytest

LIVE, SIM = "live", "sim"


def _candidate(node, depth, inflight=0, admissible=True, capability=100.0) -> dict:
    return {
        "node_id": node,
        "queue_depth": depth,
        "inflight": inflight,
        "admissible": admissible,
        "capability_tok_s": capability,
    }


def _decision(req_id, chosen, *, depths=(0, 1), draw=0.25, **extra) -> dict:
    return {
        "type": "decision",
        "req_id": req_id,
        "chosen_node": chosen,
        "tie_break_draw": draw,
        "candidates": [_candidate("n1", depths[0]), _candidate("n2", depths[1])],
        **extra,
    }


def _log(run_dir, records, name=None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / (name or f"scheduler_{run_dir.name}.jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "\n")


def _live_run(root, run_id, records):
    run = root / run_id
    _log(run, records)
    (run / "manifest.json").write_text(json.dumps({"run_id": run_id}))
    return run


def _check(tmp_path, live_records, sim_records, *extra) -> int:
    live_root, sim_root = tmp_path / LIVE, tmp_path / SIM
    _live_run(live_root, "t_jsq_s0_u30_r1", live_records)
    _log(sim_root / "t_jsq_s0_u30_r1_sim", sim_records)
    return parity.main(["--live", str(live_root), "--sim", str(sim_root), *extra])


# --------------------------------------------------------------- the four cases in 3.8


def test_two_logs_agreeing_on_state_and_draw_pass(tmp_path, capsys) -> None:
    records = [_decision("r1", "n1"), _decision("r2", "n2", depths=(3, 1))]
    assert _check(tmp_path, records, records) == 0
    out = capsys.readouterr().out
    assert "1 run pairs, 2 decisions made on the same state and draw" in out
    assert "agreed: 2" in out
    assert "PARITY PASSED: all 2 comparable decisions agree" in out


def test_a_disagreement_on_the_chosen_node_fails_and_names_the_request(tmp_path, capsys) -> None:
    live = [_decision("r1", "n1"), _decision("r2", "n1")]
    sim = [_decision("r1", "n1"), _decision("r2", "n2")]
    out_path = tmp_path / "report" / "parity"
    assert _check(tmp_path, live, sim, "--out", str(out_path)) == 2

    out = capsys.readouterr().out
    assert "DISAGREES r2: live n1 vs sim n2" in out
    assert "PARITY FAILED: 1 of 2 comparable decisions differ" in out
    report = json.loads(out_path.with_suffix(".json").read_text())
    assert report["verdict"] == "fail"
    (d,) = report["runs"][0]["disagreements"]
    assert (d["req_id"], d["live_chosen"], d["sim_chosen"], d["draw"]) == ("r2", "n1", "n2", 0.25)
    assert "`r2`: live chose n1, simulator chose n2" in out_path.with_suffix(".md").read_text()


def test_a_pair_agreeing_on_state_but_not_the_draw_is_counted_not_judged(tmp_path, capsys) -> None:
    """A tie is broken by the draw, so two vehicles that saw one state and drew differently
    may choose differently and both be right. The pair is reported and left out of the
    verdict, whichever node each chose."""
    live = [_decision("r1", "n1"), _decision("r2", "n1", draw=0.1)]
    sim = [_decision("r1", "n1"), _decision("r2", "n2", draw=0.9)]
    out_path = tmp_path / "parity"
    assert _check(tmp_path, live, sim, "--out", str(out_path)) == 0

    report = json.loads(out_path.with_suffix(".json").read_text())
    assert report["comparable"] == 1
    assert report["same_state_other_draw"] == 1
    assert report["runs"][0]["disagreements"] == []
    assert "same state, different draw" in capsys.readouterr().out


def test_no_decision_made_on_the_same_state_exits_one_rather_than_passing(tmp_path, capsys) -> None:
    """Every shared request was decided on a different view, as a staleness veil makes
    likely. Nothing was compared, and a pass over nothing would read as parity shown."""
    live = [_decision("r1", "n1", depths=(0, 1)), _decision("r2", "n1", depths=(2, 2))]
    sim = [_decision("r1", "n1", depths=(1, 1)), _decision("r2", "n1", depths=(2, 3))]
    out_path = tmp_path / "parity"
    assert _check(tmp_path, live, sim, "--out", str(out_path)) == 1

    assert "PARITY UNKNOWN: no decision was made on the same state" in capsys.readouterr().out
    report = json.loads(out_path.with_suffix(".json").read_text())
    assert report["verdict"] == "unknown"
    assert report["runs"][0]["shared_requests"] == 2
    assert report["runs"][0]["comparable"] == 0


# ------------------------------------------------------------------ what "same state" means


@pytest.mark.parametrize(
    "change",
    [
        {"queue_depth": 5},
        {"inflight": 3},
        {"admissible": False},
        {"capability_tok_s": 100.1},
    ],
)
def test_every_field_the_policy_reads_is_part_of_the_state(change) -> None:
    base = _decision("r1", "n1")
    moved = _decision("r1", "n1")
    moved["candidates"][1] |= change
    assert parity.state(base) != parity.state(moved)


def test_the_state_ignores_candidate_order_and_capability_rounding_dust() -> None:
    a = _decision("r1", "n1")
    b = _decision("r1", "n1")
    b["candidates"] = list(reversed(b["candidates"]))
    b["candidates"][0]["capability_tok_s"] += parity.CAPABILITY_TOL / 10
    assert parity.state(a) == parity.state(b)


def test_a_missing_field_reads_as_its_default() -> None:
    bare = {"candidates": [{"node_id": "n1"}]}
    assert parity.state(bare) == (("n1", None, None, True, 0),)
    assert parity.draw({"tie_break_draw": 1}) == 1.0
    assert parity.draw({}) is None


def test_only_decisions_are_read_and_a_req_id_keeps_its_first_decision(tmp_path) -> None:
    """A second decision for one req_id is a redispatch after a failure, which was made on
    a different state by definition. A restarted run can leave two logs; both are read."""
    run = tmp_path / "run"
    _log(
        run,
        [_decision("r1", "n1"), {"type": "completion", "req_id": "r1"}],
        name="scheduler_a.jsonl",
    )
    _log(run, [_decision("r1", "n2"), _decision("r2", "n2")], name="scheduler_b.jsonl")
    got = parity.decisions(run)
    assert sorted(got) == ["r1", "r2"]
    assert got["r1"]["chosen_node"] == "n1"


def test_only_requests_both_vehicles_decided_are_compared(tmp_path) -> None:
    _log(tmp_path / "l", [_decision("r1", "n1"), _decision("r2", "n1")])
    _log(tmp_path / "s", [_decision("r2", "n1"), _decision("r3", "n2")])
    result = parity.compare_run(tmp_path / "l", tmp_path / "s")
    assert (result["live_decisions"], result["sim_decisions"]) == (2, 2)
    assert result["shared_requests"] == 1
    assert result["comparable"] == result["agreed"] == 1


# --------------------------------------------------------------------- pairing the runs


def test_each_live_run_is_paired_with_its_own_replay(tmp_path) -> None:
    """p4_validate names a replay `<run_id>_sim`; a replay under the bare run id, or a sim
    root that is itself one run, also counts. A live run with no replay is left out."""
    live, sim = tmp_path / LIVE, tmp_path / SIM
    a = _live_run(live, "run_a", [_decision("r1", "n1")])
    b = _live_run(live, "run_b", [_decision("r1", "n1")])
    _live_run(live, "run_c", [_decision("r1", "n1")])
    (live / "not_a_run").mkdir()
    _log(sim / "run_a_sim", [_decision("r1", "n1")])
    _log(sim / "run_b", [_decision("r1", "n1")])
    (sim / "run_c_sim").mkdir()  # a replay directory with no scheduler log
    assert parity.pair_up(live, sim) == [(a, sim / "run_a_sim"), (b, sim / "run_b")]

    single = _live_run(tmp_path / "one", "run_d", [_decision("r1", "n1")])
    _log(tmp_path / "flat", [_decision("r1", "n1")])
    assert parity.pair_up(single, tmp_path / "flat") == [(single, tmp_path / "flat")]


def test_a_path_that_is_not_a_directory_is_refused(tmp_path, capsys) -> None:
    (tmp_path / "file").write_text("")
    assert parity.main(["--live", str(tmp_path / "file"), "--sim", str(tmp_path)]) == 1
    assert "need two directories" in capsys.readouterr().out


def test_a_run_set_with_no_replay_is_refused(tmp_path, capsys) -> None:
    _live_run(tmp_path / LIVE, "run_a", [_decision("r1", "n1")])
    (tmp_path / SIM).mkdir()
    assert parity.main(["--live", str(tmp_path / LIVE), "--sim", str(tmp_path / SIM)]) == 1
    assert "has a replay under" in capsys.readouterr().out


def test_many_disagreements_are_listed_up_to_a_limit(tmp_path, capsys) -> None:
    live = [_decision(f"r{i:02d}", "n1") for i in range(25)]
    sim = [_decision(f"r{i:02d}", "n2") for i in range(25)]
    out_path = tmp_path / "parity"
    assert _check(tmp_path, live, sim, "--out", str(out_path)) == 2
    out = capsys.readouterr().out
    assert out.count("DISAGREES") == 10
    assert "... and 15 more" in out
    md = out_path.with_suffix(".md").read_text()
    assert md.count("live chose n1") == 20
    assert "- and 5 more" in md
    assert "| t_jsq_s0_u30_r1 | 25 | 25 | 0 | 0 |" in md
