"""tools/promote_calibration.py: a recalibration reaching the contracts and the configs.

Doing this by hand took an hour and a near miss, and the near miss is what the tests are
about. A promotion that skips the phase split leaves the phase-aware half of the study with
nothing to read; one that promotes an older snapshot changes what the scheduler serves for
the worse; one that misses a config leaves a campaign naming a superseded id, which
`hw_runs.py` then refuses hours later.

Everything the script shells out to is faked here: what is under test is the order of the
steps and the refusals between them, not `backfill_phase_split.py` or `check.py`.
"""

from __future__ import annotations

import json
import subprocess

import promote_calibration as promote
import pytest

CLASS = "rtx3050_ngl99_p4_q4km_llama32_1b"


def _entry(concurrency: int, service: float, prefill: float | None) -> dict:
    return {
        "prompt_bucket": [1, 128],
        "output_bucket": [1, 64],
        "concurrency": concurrency,
        "service_ms_mean": service,
        "tokens_per_s": 160.0,
        "n_samples": 8,
        **({"prefill_ms_mean": prefill, "decode_ms_mean": service - prefill} if prefill else {}),
    }


def _snapshot(sid: str, when: int, *, service: float = 400.0, prefill: float | None = 20.0) -> dict:
    return {
        "snapshot_id": sid,
        "node_class": CLASS,
        "measured_at_unix": when,
        "entries": [_entry(1, service, prefill), _entry(4, service * 1.5, prefill)],
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repository root with a contracts directory and a configs directory."""
    root = tmp_path / "repo"
    (root / "contracts" / "cost_models" / CLASS).mkdir(parents=True)
    (root / "dataplane" / "configs").mkdir(parents=True)
    (root / "tools").mkdir()
    monkeypatch.setattr(promote, "REPO_ROOT", root)
    monkeypatch.setattr(promote, "SNAPSHOT_ROOT", root / "contracts" / "cost_models")
    monkeypatch.setattr(promote, "CONFIGS", root / "dataplane" / "configs")
    monkeypatch.setattr(promote, "TOOLS", root / "tools")
    return root


@pytest.fixture
def commands(monkeypatch):
    """Every shelled-out command, with what it should return and what it should leave behind."""
    calls: list[list[str]] = []
    outcomes: dict[str, int] = {}
    effects: dict[str, object] = {}

    def fake_run(cmd):
        calls.append(cmd)
        name = next(
            (k for k in ("backfill", "check.py", "hw_runs") if any(k in c for c in cmd)), ""
        )
        effect = effects.get(name)
        if effect is not None:
            effect()
        code = outcomes.get(name, 0)
        return subprocess.CompletedProcess(cmd, code, stdout=f"{name} said so\n", stderr="")

    monkeypatch.setattr(promote, "run", fake_run)
    return {"calls": calls, "outcomes": outcomes, "effects": effects}


def _calibration(repo, *snapshots: dict, node_class: str = CLASS):
    run_dir = repo / "runs" / "cal"
    (run_dir / "snapshots").mkdir(parents=True)
    (run_dir / "campaign.json").write_text(json.dumps({"node_class": node_class, "run_id": "cal"}))
    (run_dir / "observations.jsonl").write_text("{}\n")
    for i, snap in enumerate(snapshots):
        (run_dir / "snapshots" / f"{i:03d}_{snap['snapshot_id']}.json").write_text(json.dumps(snap))
    return run_dir


def _in_contracts(repo, *snapshots: dict) -> None:
    for i, snap in enumerate(snapshots):
        path = repo / "contracts" / "cost_models" / CLASS / f"{i:03d}_{snap['snapshot_id']}.json"
        path.write_text(json.dumps(snap))


def _config(repo, name: str, payload) -> object:
    path = repo / "dataplane" / "configs" / name
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


OLD = "cm_rtx3050_old"
NEW = "cm_rtx3050_new"


def _campaign_config(snapshot_id: str) -> dict:
    return {
        "tag": "seeded",
        "cost_model_snapshots": {"gtx1650ti": "cm_gtx1650ti_x", "rtx3050": snapshot_id},
    }


# ------------------------------------------------------------------------- refusals


def test_a_run_with_no_snapshots_is_refused(repo, commands, capsys) -> None:
    run_dir = _calibration(repo)
    assert promote.main([str(run_dir)]) == 2
    assert "has no snapshots" in capsys.readouterr().out
    assert commands["calls"] == []


def test_a_snapshot_older_than_the_one_in_the_contracts_is_refused(repo, commands, capsys) -> None:
    """Promoting it would not change what the scheduler serves, and it would leave the
    configs naming a snapshot that is not the newest in its class."""
    _in_contracts(repo, _snapshot(NEW, 2_000))
    run_dir = _calibration(repo, _snapshot(OLD, 1_000))
    assert promote.main([str(run_dir)]) == 2
    out = capsys.readouterr().out
    assert f"refusing: {NEW} in the contracts is newer than this run's {OLD}" in out
    assert not list((repo / "contracts" / "cost_models" / CLASS).glob(f"*{OLD}*"))


def test_a_snapshot_already_promoted_is_left_alone(repo, commands, capsys) -> None:
    _in_contracts(repo, _snapshot(NEW, 2_000))
    run_dir = _calibration(repo, _snapshot(NEW, 2_000))
    assert promote.main([str(run_dir)]) == 0
    assert f"{NEW} is already the newest in {CLASS}" in capsys.readouterr().out


def test_snapshots_that_fail_c3_are_refused(repo, commands, capsys) -> None:
    commands["outcomes"]["check.py"] = 1
    run_dir = _calibration(repo, _snapshot(NEW, 2_000))
    assert promote.main([str(run_dir)]) == 2
    out = capsys.readouterr().out
    assert "refusing: the snapshots do not satisfy C-3" in out
    assert not list((repo / "contracts" / "cost_models" / CLASS).iterdir())


def test_a_snapshot_already_in_the_contracts_with_other_content_is_refused(
    repo, commands, capsys
) -> None:
    """Two different calibrations under one id is a name collision, and copying over it would
    silently rewrite the record a finished run was measured against."""
    _in_contracts(repo, _snapshot(OLD, 1_000))
    # Same file name, different calibration inside it.
    other = _snapshot("cm_rtx3050_other", 1_500, service=999.0)
    (repo / "contracts" / "cost_models" / CLASS / f"000_{NEW}.json").write_text(json.dumps(other))
    run_dir = _calibration(repo, _snapshot(NEW, 2_000))
    assert promote.main([str(run_dir)]) == 2
    assert "exists with different content" in capsys.readouterr().out


# -------------------------------------------------------------------- the phase split


def test_a_newest_snapshot_without_a_phase_split_is_backfilled_first(
    repo, commands, capsys
) -> None:
    run_dir = _calibration(repo, _snapshot(NEW, 2_000, prefill=None))
    path = run_dir / "snapshots" / f"000_{NEW}.json"

    def backfill():
        path.write_text(json.dumps(_snapshot(NEW, 2_000, prefill=20.0)))

    commands["effects"]["backfill"] = backfill
    assert promote.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "backfilling the phase split from observations.jsonl" in out
    assert [c for c in commands["calls"] if "backfill" in " ".join(c)]
    assert promote.has_phase_split(json.loads(path.read_text()))


def test_a_backfill_that_produces_no_split_is_refused(repo, commands, capsys) -> None:
    """The backfill can exit 0 having matched nothing, and a snapshot without prefill times
    is one the phase-aware half of the study cannot read."""
    run_dir = _calibration(repo, _snapshot(NEW, 2_000, prefill=None))
    assert promote.main([str(run_dir)]) == 2
    assert "the backfill ran but the newest snapshot still has no split" in capsys.readouterr().out


def test_a_backfill_that_fails_is_refused(repo, commands, capsys) -> None:
    commands["outcomes"]["backfill"] = 1
    run_dir = _calibration(repo, _snapshot(NEW, 2_000, prefill=None))
    assert promote.main([str(run_dir)]) == 2
    assert "refusing: backfill failed" in capsys.readouterr().out


def test_a_snapshot_that_already_has_its_split_is_not_backfilled(repo, commands) -> None:
    run_dir = _calibration(repo, _snapshot(NEW, 2_000))
    assert promote.main([str(run_dir)]) == 0
    assert not [c for c in commands["calls"] if "backfill" in " ".join(c)]


# ----------------------------------------------------------- promoting and repointing


def test_a_promotion_copies_the_series_and_repoints_only_the_configs_that_name_the_old_id(
    repo, commands, capsys
) -> None:
    _in_contracts(repo, _snapshot(OLD, 1_000, service=500.0))
    series = (_snapshot(NEW, 2_000, service=400.0), _snapshot(f"{NEW}_008", 2_100, service=400.0))
    run_dir = _calibration(repo, *series)
    named = _config(repo, "hw_seeded.json", _campaign_config(OLD))
    other = _config(repo, "hw_other.json", _campaign_config("cm_gtx1650ti_x"))
    listed = _config(repo, "nodes_only.json", [{"node_id": "rtx3050", "note": OLD}])
    mentions = _config(repo, "trace_note.json", {"_comment": f"calibrated against {OLD}"})

    assert promote.main([str(run_dir)]) == 0

    promoted = sorted(p.name for p in (repo / "contracts" / "cost_models" / CLASS).glob("*.json"))
    assert len(promoted) == 3
    assert json.loads(named.read_text())["cost_model_snapshots"]["rtx3050"] == f"{NEW}_008"
    assert json.loads(other.read_text()) == _campaign_config("cm_gtx1650ti_x")
    assert json.loads(listed.read_text())[0]["note"] == OLD
    assert json.loads(mentions.read_text())["_comment"] == f"calibrated against {OLD}"

    out = capsys.readouterr().out
    assert "promoted 2 snapshot(s)" in out
    assert "repointed dataplane/configs/hw_seeded.json" in out
    assert "every hw_*.json dry-runs" in out


def test_a_promotion_prints_what_changed_against_the_snapshot_it_replaces(
    repo, commands, capsys
) -> None:
    """A recalibration is run to answer whether the node changed, so the comparison is the
    output, not a side effect."""
    _in_contracts(repo, _snapshot(OLD, 1_000, service=500.0, prefill=25.0))
    run_dir = _calibration(repo, _snapshot(NEW, 2_000, service=400.0, prefill=20.0))
    assert promote.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert f"{CLASS}: {OLD}" in out
    assert f"-> {NEW}" in out
    assert "capability" in out
    assert "-20.0%" in out  # service 500 -> 400 at both concurrencies
    assert "500 ->    400" in out.replace("  ", "  ")


def test_a_cell_the_old_snapshot_did_not_hold_is_not_compared(repo, commands, capsys) -> None:
    old = _snapshot(OLD, 1_000)
    old["entries"] = [_entry(1, 500.0, 25.0)]
    _in_contracts(repo, old)
    new = _snapshot(NEW, 2_000, service=400.0, prefill=None)
    new["entries"] = [_entry(1, 400.0, 20.0), _entry(8, 900.0, 40.0)]
    run_dir = _calibration(repo, new)
    assert promote.main([str(run_dir)]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if "->" in line]
    assert not any("[1, 128]" in line and " 8 " in line for line in lines)


def test_a_class_with_nothing_to_replace_promotes_without_a_comparison(
    repo, commands, capsys
) -> None:
    run_dir = _calibration(repo, _snapshot(NEW, 2_000), node_class=CLASS)
    assert promote.main([str(run_dir)]) == 0
    out = capsys.readouterr().out
    assert "capability" not in out
    assert "promoted 1 snapshot(s)" in out


def test_a_dry_run_reports_and_writes_nothing(repo, commands, capsys) -> None:
    _in_contracts(repo, _snapshot(OLD, 1_000))
    run_dir = _calibration(repo, _snapshot(NEW, 2_000, prefill=None))
    named = _config(repo, "hw_seeded.json", _campaign_config(OLD))
    before = named.read_text()

    assert promote.main([str(run_dir), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would promote 1 snapshot(s)" in out
    assert "would repoint dataplane/configs/hw_seeded.json" in out
    assert named.read_text() == before
    assert sorted(p.name for p in (repo / "contracts" / "cost_models" / CLASS).glob("*")) == [
        f"000_{OLD}.json"
    ]
    # Nothing was shelled out that writes: no backfill, and no campaign dry runs.
    assert not [c for c in commands["calls"] if "backfill" in " ".join(c)]
    assert not [c for c in commands["calls"] if "hw_runs" in " ".join(c)]


def test_a_campaign_that_no_longer_dry_runs_is_reported(repo, commands, capsys) -> None:
    """Step 5 exists so a config that stopped checking is found now rather than at the start
    of a measurement night. The promotion already happened, so this is a report, not a refusal."""
    commands["outcomes"]["hw_runs"] = 2
    _in_contracts(repo, _snapshot(OLD, 1_000))
    run_dir = _calibration(repo, _snapshot(NEW, 2_000))
    _config(repo, "hw_seeded.json", _campaign_config(OLD))

    assert promote.main([str(run_dir)]) == 1
    out = capsys.readouterr().out
    assert "campaigns that no longer dry-run:" in out
    assert "hw_seeded.json" in out
    assert (
        json.loads((repo / "dataplane" / "configs" / "hw_seeded.json").read_text())[
            "cost_model_snapshots"
        ]["rtx3050"]
        == NEW
    )


# ------------------------------------------------------------------------- the helpers


def test_run_shells_out_from_the_repository_root(repo) -> None:
    """Every command it runs is relative to the repository, including the campaign dry runs."""
    import sys

    result = promote.run([sys.executable, "-c", "import pathlib; print(pathlib.Path.cwd())"])
    assert result.returncode == 0
    assert result.stdout.strip() == str(repo)


def test_newest_in_class_picks_by_measurement_time_and_knows_an_empty_class() -> None:
    index = {
        "a": {"snapshot_id": "a", "node_class": CLASS, "measured_at_unix": 1},
        "b": {"snapshot_id": "b", "node_class": CLASS, "measured_at_unix": 9},
        "c": {"snapshot_id": "c", "node_class": "other", "measured_at_unix": 99},
    }
    assert promote.newest_in_class(index, CLASS)["snapshot_id"] == "b"
    assert promote.newest_in_class(index, "nobody") is None


def test_a_phase_split_needs_every_cell() -> None:
    snap = _snapshot(NEW, 1)
    assert promote.has_phase_split(snap)
    snap["entries"][1].pop("prefill_ms_mean")
    assert not promote.has_phase_split(snap)


def test_configs_naming_parses_rather_than_greps(repo) -> None:
    _config(repo, "a.json", _campaign_config(OLD))
    _config(repo, "b.json", {"_comment": OLD})
    _config(repo, "c.json", [{"note": OLD}])
    (repo / "dataplane" / "configs" / "broken.json").write_text("{not json")
    assert [p.name for p in promote.configs_naming(OLD)] == ["a.json"]
