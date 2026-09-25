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


# ------------------------------------------------------------ a grid inverted in concurrency

# The RTX 3050 grid of 2026-09-14, as it was committed. It reached the contracts inverted in
# all six buckets, parameterised the simulator, and is why the simulator failed the contrast
# criterion on every held-out shape. Pinned by id because this is that history.
INVERTED_3050 = "cm_rtx3050_ngl99_p4_q4km_llama32_1b_20260914T200053Z_008"


def _committed(snapshot_id: str) -> dict:
    import pool_load

    return pool_load.snapshot_index()[snapshot_id]


def test_the_2026_09_14_rtx3050_grid_is_inverted_in_every_bucket() -> None:
    lines = promote.inversions(_committed(INVERTED_3050))
    assert len(lines) == 6
    for bucket in ("[1, 128]", "[129, 256]", "[257, 512]"):
        assert sum(f"prompt {bucket}" in line for line in lines) == 2
    assert all("c=3" in line and "c=4" in line for line in lines)
    assert lines[0] == (
        "prompt [1, 128] output [1, 64]: c=3 679 ms is slower than c=4 593 ms, by 14%"
    )


def test_promotion_refuses_that_grid_and_names_its_buckets(repo, commands, capsys) -> None:
    inverted = {**_committed(INVERTED_3050), "snapshot_id": NEW, "measured_at_unix": 2_000}
    run_dir = _calibration(repo, inverted)
    assert promote.main([str(run_dir)]) == 2

    out = capsys.readouterr().out
    assert f"refusing: 000_{NEW}.json is inverted in concurrency" in out
    assert out.count("is slower than c=4") == 6
    assert "Recalibrate this class" in out
    assert not list((repo / "contracts" / "cost_models" / CLASS).iterdir())


def _grid(services: dict[int, float]) -> dict:
    snap = _snapshot(NEW, 2_000)
    snap["entries"] = [_entry(c, ms, 20.0) for c, ms in sorted(services.items())]
    return snap


def test_a_monotone_grid_has_no_inversion_and_is_promoted(repo, commands) -> None:
    grid = _grid({1: 400.0, 2: 440.0, 3: 520.0, 4: 610.0})
    assert promote.inversions(grid) == []
    assert promote.main([str(_calibration(repo, grid))]) == 0


def test_a_dip_inside_the_tolerance_is_noise_not_an_inversion() -> None:
    """Sampling noise can put one concurrency a hair under the one before it. Two percent is
    noise; a step is not."""
    assert promote.inversions(_grid({1: 400.0, 2: 500.0, 3: 491.0})) == []
    (line,) = promote.inversions(_grid({1: 400.0, 2: 500.0, 3: 489.0}))
    assert line.startswith("prompt [1, 128] output [1, 64]: c=2 500 ms is slower than c=3 489 ms")


def test_an_inversion_is_found_between_any_neighbouring_concurrencies() -> None:
    lines = promote.inversions(_grid({1: 600.0, 2: 400.0, 4: 300.0}))
    assert [line.split(": ")[1].split(" ms")[0] for line in lines] == ["c=1 600", "c=2 400"]


# ------------------------------------------------- the ablation's believed ratios (M2)
#
# Written before the code. The interface:
#
#     promote.rescale_overrides(config, capabilities) -> (new_config, changes)
#         `capabilities` maps each pool node to `pool_load.capability` of the snapshot the
#         config names for it. Returns the config with every arm's `capability_override`
#         recomputed, and one printable line per value that moved.
#
# and `main` applies it to every config it repoints, after the repoint and before the
# campaign dry runs, reporting instead of writing under --dry-run.
#
# The rule, and why. analysis-plan 6.1 freezes the believed ratios the curve is drawn at
# (1.0, 1.2, 2.5, 3.35, 5 and 100; 1.57 is the pool's own capability and has no override).
# The ratio is the x-axis, so it must not move when a node is recalibrated. What the ratio
# is anchored to is the slow node's measured capability, so that side of every arm's belief
# stays true. So:
#
#   * the anchor is the pool node with the lowest capability in the promoted snapshots;
#   * each arm's ratio is read off its current values, rounded to three places, which
#     recovers the frozen ratios exactly (125.848 / 104.873 is 1.2 at three places);
#   * the anchor's value becomes its new capability and every other node's becomes ratio
#     times that, both rounded to three places;
#   * promoting the fast node changes no override, since neither the anchor nor any ratio
#     moved. Which ratio the pair "actually has at four slots" is the `c4` arm's job,
#     which reads the live snapshot, not a frozen number.

SLOW_CLASS = "gtx1650ti_ngl99_p4_q4km_llama32_1b"
SLOW_OLD, SLOW_NEW = "cm_gtx1650ti_old", "cm_gtx1650ti_new"


def _class_snapshot(sid: str, node_class: str, when: int, tokens_per_s: float) -> dict:
    """A monotone two-cell grid whose capability is 0.9 x `tokens_per_s`."""
    cells = [
        {
            "prompt_bucket": [1, 128],
            "output_bucket": [1, 64],
            "concurrency": c,
            "service_ms_mean": 100.0 * c,
            "tokens_per_s": tokens_per_s,
            "n_samples": 8,
            "prefill_ms_mean": 10.0 * c,
            "decode_ms_mean": 90.0 * c,
        }
        for c in (1, 4)
    ]
    return {
        "snapshot_id": sid,
        "node_class": node_class,
        "measured_at_unix": when,
        "entries": cells,
    }


def _in_class(repo, snap: dict) -> None:
    d = repo / "contracts" / "cost_models" / snap["node_class"]
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{snap['snapshot_id']}.json").write_text(json.dumps(snap))


# Capabilities of 104.877 (old 1650 Ti), 116.649 (new) and 339.291 (3050): chosen so that no
# old value is a substring of a new one, which the text-level rewrite relies on.
SLOW_TOK_OLD, SLOW_TOK_NEW, FAST_TOK = 116.53, 129.61, 376.99


def _cap(tokens_per_s: float) -> float:
    import pool_load

    return round(pool_load.capability(_class_snapshot("x", "c", 1, tokens_per_s)), 3)


def _ablation(slow_id: str, fast_id: str, anchor: float) -> dict:
    arms = [{"name": "cap157", "policies": ["jsq", "jsq_fastfirst", "wjsq"]}]
    for name, ratio in (("cap100", 1.0), ("cap120", 1.2), ("cap335", 3.35), ("cap10000", 100.0)):
        arms.append(
            {
                "name": name,
                "policies": ["wjsq"],
                "capability_override": {
                    "gtx1650ti": anchor,
                    "rtx3050": round(ratio * anchor, 3),
                },
            }
        )
    arms.append({"name": "c4", "policies": ["wjsq"], "capability_concurrency": 4})
    return {
        "_comment": f"The ratio arms hold the 1650 Ti at its measured {anchor} output tok/s.",
        "tag": "ablation",
        "cost_model_snapshots": {"gtx1650ti": slow_id, "rtx3050": fast_id},
        "capability_arms": arms,
    }


def _overrides(config: dict) -> dict[str, dict[str, float]]:
    return {
        a["name"]: a["capability_override"]
        for a in config["capability_arms"]
        if "capability_override" in a
    }


def test_rescaling_keeps_every_ratio_and_moves_the_anchor() -> None:
    old, new = _cap(SLOW_TOK_OLD), _cap(SLOW_TOK_NEW)
    config = _ablation(SLOW_OLD, NEW, old)
    rescaled, changes = promote.rescale_overrides(
        config, {"gtx1650ti": new, "rtx3050": _cap(FAST_TOK)}
    )
    for name, ratio in (("cap100", 1.0), ("cap120", 1.2), ("cap335", 3.35), ("cap10000", 100.0)):
        values = _overrides(rescaled)[name]
        assert values["gtx1650ti"] == new
        assert values["rtx3050"] == round(ratio * new, 3)
    assert any(line.startswith("cap335: rtx3050") for line in changes)
    assert any(line.startswith("cap100: gtx1650ti") for line in changes)


def test_rescaling_touches_nothing_but_the_override_values() -> None:
    config = _ablation(SLOW_OLD, NEW, _cap(SLOW_TOK_OLD))
    rescaled, _ = promote.rescale_overrides(
        config, {"gtx1650ti": _cap(SLOW_TOK_NEW), "rtx3050": _cap(FAST_TOK)}
    )
    strip = lambda c: {
        **c,
        "capability_arms": [
            {k: v for k, v in a.items() if k != "capability_override"} for a in c["capability_arms"]
        ],
    }
    assert strip(rescaled) == strip(config)
    assert config == _ablation(SLOW_OLD, NEW, _cap(SLOW_TOK_OLD)), "the input must not be mutated"


def test_rescaling_at_the_same_anchor_changes_nothing() -> None:
    config = _ablation(SLOW_OLD, NEW, _cap(SLOW_TOK_OLD))
    same, changes = promote.rescale_overrides(
        config, {"gtx1650ti": _cap(SLOW_TOK_OLD), "rtx3050": _cap(FAST_TOK) * 2}
    )
    assert same == config
    assert changes == []


def test_the_anchor_is_the_slower_node_whatever_the_arm_says() -> None:
    """cap100 holds both nodes at one value, so the arm cannot say which node is the anchor.
    The pool can: it is the node with the lower measured capability."""
    config = _ablation(SLOW_OLD, NEW, 50.0)
    rescaled, _ = promote.rescale_overrides(config, {"gtx1650ti": 60.0, "rtx3050": 500.0})
    assert _overrides(rescaled)["cap100"] == {"gtx1650ti": 60.0, "rtx3050": 60.0}
    swapped, _ = promote.rescale_overrides(config, {"gtx1650ti": 500.0, "rtx3050": 60.0})
    assert _overrides(swapped)["cap120"]["rtx3050"] == 60.0


def test_a_config_without_overrides_is_returned_unchanged() -> None:
    config = _campaign_config(OLD)
    assert promote.rescale_overrides(config, {"gtx1650ti": 1.0, "rtx3050": 2.0}) == (config, [])


@pytest.mark.parametrize(
    "override",
    [{"gtx1650ti": 100.0}, {"gtx1650ti": 100.0, "rtx3050": 200.0, "cpu": 50.0}],
)
def test_an_override_that_does_not_cover_the_pool_is_refused(override) -> None:
    config = _ablation(SLOW_OLD, NEW, 100.0)
    config["capability_arms"][1]["capability_override"] = override
    with pytest.raises(ValueError, match="cap100"):
        promote.rescale_overrides(config, {"gtx1650ti": 110.0, "rtx3050": 300.0})


def _pool(repo) -> None:
    """The contracts hold the old 1650 Ti snapshot and the 3050's, both newest in class."""
    _in_class(repo, _class_snapshot(SLOW_OLD, SLOW_CLASS, 1_000, SLOW_TOK_OLD))
    _in_class(repo, _class_snapshot(OLD, CLASS, 1_000, FAST_TOK))


def test_promoting_the_slow_node_rescales_the_ablation_it_repoints(repo, commands, capsys) -> None:
    _pool(repo)
    old_anchor, new_anchor = _cap(SLOW_TOK_OLD), _cap(SLOW_TOK_NEW)
    path = _config(repo, "hw_calibration_ablation.json", _ablation(SLOW_OLD, OLD, old_anchor))
    assert str(old_anchor) in path.read_text()
    run_dir = _calibration(
        repo, _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW), node_class=SLOW_CLASS
    )

    assert promote.main([str(run_dir)]) == 0

    config = json.loads(path.read_text())
    assert config["cost_model_snapshots"]["gtx1650ti"] == SLOW_NEW
    assert _overrides(config)["cap335"] == {
        "gtx1650ti": new_anchor,
        "rtx3050": round(3.35 * new_anchor, 3),
    }
    # The old anchor survives nowhere, the comment that quotes it included.
    assert str(old_anchor) not in path.read_text()
    assert str(new_anchor) in config["_comment"]
    assert "rescaled cap335" in capsys.readouterr().out


def test_promoting_the_fast_node_leaves_every_override_alone(repo, commands) -> None:
    _pool(repo)
    anchor = _cap(SLOW_TOK_OLD)
    before = _ablation(SLOW_OLD, OLD, anchor)
    path = _config(repo, "hw_calibration_ablation.json", before)
    run_dir = _calibration(repo, _class_snapshot(NEW, CLASS, 2_000, FAST_TOK * 1.1))

    assert promote.main([str(run_dir)]) == 0

    config = json.loads(path.read_text())
    assert config["cost_model_snapshots"]["rtx3050"] == NEW
    assert _overrides(config) == _overrides(before)


def test_a_dry_run_reports_the_rescale_and_writes_nothing(repo, commands, capsys) -> None:
    _pool(repo)
    path = _config(
        repo, "hw_calibration_ablation.json", _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD))
    )
    before = path.read_text()
    run_dir = _calibration(
        repo, _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW), node_class=SLOW_CLASS
    )
    assert promote.main([str(run_dir), "--dry-run"]) == 0
    assert "would rescale cap335" in capsys.readouterr().out
    assert path.read_text() == before


def test_a_config_the_promotion_does_not_repoint_is_not_rescaled(repo, commands) -> None:
    _pool(repo)
    other = _ablation("cm_gtx1650ti_some_other", OLD, 42.0)
    path = _config(repo, "hw_other.json", other)
    run_dir = _calibration(
        repo, _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW), node_class=SLOW_CLASS
    )
    assert promote.main([str(run_dir)]) == 0
    assert json.loads(path.read_text()) == other


def test_the_committed_ablation_holds_the_frozen_ratios() -> None:
    """The rule reads each ratio off the committed values, so they must already be the ones
    analysis-plan 6.1 froze. Each arm's name is its ratio times 100, cap10000 included."""
    from support import CONFIGS

    config = json.loads((CONFIGS / "hw_calibration_ablation_3050.json").read_text())
    ratios = {
        name: round(v["rtx3050"] / v["gtx1650ti"], 3) for name, v in _overrides(config).items()
    }
    assert ratios == {
        "cap100": 1.0,
        "cap120": 1.2,
        "cap250": 2.5,
        "cap335": 3.35,
        "cap500": 5.0,
        "cap10000": 100.0,
    }


def test_a_moved_number_that_is_also_something_else_falls_back_to_a_clean_dump() -> None:
    """The rewrite swaps numbers as text so the comments keep up with them. If the old
    anchor is also some other value in the file, here a Threshold cutoff that happens to
    equal it, the swap would move that too. The parsed result no longer matches the config
    it should be, so the rewrite writes the config out whole instead: the cutoff keeps its
    value and every arm carries the rescaled override."""
    old, new = _cap(SLOW_TOK_OLD), _cap(SLOW_TOK_NEW)
    before = _ablation(SLOW_OLD, NEW, old)
    before["capability_arms"].append(
        {
            "name": "decode",
            "policies": ["threshold"],
            "capability_mode": "decode",
            "threshold_t": old,
        }
    )
    text = json.dumps(before, indent=2) + "\n"
    after, changed = promote.rescale_overrides(
        before, {"gtx1650ti": new, "rtx3050": _cap(FAST_TOK)}
    )
    assert changed

    out = promote.rewrite_overrides(text, before, after)

    parsed = json.loads(out)
    assert parsed == after
    assert parsed["capability_arms"][-1]["threshold_t"] == old
    assert _overrides(parsed)["cap335"] == {"gtx1650ti": new, "rtx3050": round(3.35 * new, 3)}


def test_the_rewrite_keeps_the_text_when_only_overrides_and_prose_moved() -> None:
    """The ordinary case, for contrast: nothing else holds the number, so the text edit is
    kept, the comment quotes the new anchor, and nothing but the numbers changed."""
    old, new = _cap(SLOW_TOK_OLD), _cap(SLOW_TOK_NEW)
    before = _ablation(SLOW_OLD, NEW, old)
    text = json.dumps(before, indent=2) + "\n"
    after, _ = promote.rescale_overrides(before, {"gtx1650ti": new, "rtx3050": _cap(FAST_TOK)})
    out = promote.rewrite_overrides(text, before, after)
    assert promote.without_comments(json.loads(out)) == promote.without_comments(after)
    assert str(new) in json.loads(out)["_comment"]
    assert out.count("\n") == text.count("\n")


def test_a_repointed_config_with_an_override_missing_a_pool_node_is_refused_by_name(
    repo, commands, capsys
) -> None:
    _pool(repo)
    config = _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD))
    config["capability_arms"][2]["capability_override"].pop("rtx3050")  # cap120
    path = _config(repo, "hw_calibration_ablation.json", config)
    before = path.read_text()
    run_dir = _calibration(
        repo, _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW), node_class=SLOW_CLASS
    )

    assert promote.main([str(run_dir)]) == 2

    out = capsys.readouterr().out
    assert "refusing: dataplane/configs/hw_calibration_ablation.json cannot be re-anchored" in out
    assert "cap120" in out
    assert path.read_text() == before


def _contracts(repo) -> dict[str, bytes]:
    root = repo / "contracts" / "cost_models"
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.mark.parametrize("broken", ["hw_a_ablation.json", "hw_b_ablation.json"])
def test_a_refusal_on_any_config_leaves_every_config_and_the_contracts_untouched(
    repo, commands, capsys, broken
) -> None:
    """Two configs name the snapshot being replaced. One can be re-anchored and the other
    is missing a pool node. The refusal has to come before anything is written, whichever
    of the two is read first: a promotion that copied the series and rewrote one config
    before refusing on the other leaves the contracts serving a snapshot that half the
    configs were never moved to."""
    _pool(repo)
    good = _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD))
    bad = _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD))
    bad["capability_arms"][2]["capability_override"].pop("rtx3050")  # cap120
    paths = {
        name: _config(repo, name, bad if name == broken else good)
        for name in ("hw_a_ablation.json", "hw_b_ablation.json")
    }
    configs_before = {name: p.read_bytes() for name, p in paths.items()}
    contracts_before = _contracts(repo)
    run_dir = _calibration(
        repo, _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW), node_class=SLOW_CLASS
    )

    assert promote.main([str(run_dir)]) == 2

    out = capsys.readouterr().out
    assert f"refusing: dataplane/configs/{broken} cannot be re-anchored" in out
    assert {name: p.read_bytes() for name, p in paths.items()} == configs_before
    assert _contracts(repo) == contracts_before, "no snapshot may be copied before the refusal"
    assert not any("hw_runs" in c for cmd in commands["calls"] for c in cmd)


def test_plan_repoint_plans_every_config_and_writes_none(repo) -> None:
    """The plan is what main writes afterwards: each config's path, its new text, and the
    arms it rescales. Nothing reaches the disk while planning."""
    _pool(repo)
    a = _config(repo, "hw_a_ablation.json", _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD)))
    b = _config(
        repo,
        "hw_b_seeded.json",
        {"tag": "seeded", "cost_model_snapshots": {"gtx1650ti": SLOW_OLD, "rtx3050": OLD}},
    )
    before = {p: p.read_bytes() for p in (a, b)}
    new = _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW)
    index = {
        s["snapshot_id"]: s
        for s in (
            _class_snapshot(SLOW_OLD, SLOW_CLASS, 1_000, SLOW_TOK_OLD),
            _class_snapshot(OLD, CLASS, 1_000, FAST_TOK),
            new,
        )
    }

    plan = promote.plan_repoint(SLOW_OLD, new, index)

    assert [r.path for r in plan] == [a, b]
    assert {p: p.read_bytes() for p in (a, b)} == before
    for r in plan:
        assert json.loads(r.text)["cost_model_snapshots"]["gtx1650ti"] == SLOW_NEW
        assert SLOW_OLD not in r.text
    by_path = {r.path: r for r in plan}
    assert any(line.startswith("cap335") for line in by_path[a].changed)
    assert by_path[b].changed == []
    # A NamedTuple, so main can unpack it and a reader can name its fields.
    path, text, changed = plan[0]
    assert (path, text, changed) == (plan[0].path, plan[0].text, plan[0].changed)


def test_plan_repoint_refuses_by_the_config_it_cannot_re_anchor(repo) -> None:
    _pool(repo)
    _config(repo, "hw_a_ablation.json", _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD)))
    bad = _ablation(SLOW_OLD, OLD, _cap(SLOW_TOK_OLD))
    bad["capability_arms"][2]["capability_override"].pop("rtx3050")
    _config(repo, "hw_b_ablation.json", bad)
    new = _class_snapshot(SLOW_NEW, SLOW_CLASS, 2_000, SLOW_TOK_NEW)
    index = {
        s["snapshot_id"]: s
        for s in (
            _class_snapshot(SLOW_OLD, SLOW_CLASS, 1_000, SLOW_TOK_OLD),
            _class_snapshot(OLD, CLASS, 1_000, FAST_TOK),
            new,
        )
    }

    with pytest.raises(ValueError, match="hw_b_ablation.json"):
        promote.plan_repoint(SLOW_OLD, new, index)


def _one_arm(override: dict[str, float]) -> dict:
    return {"capability_arms": [{"name": "cap250", "capability_override": override}]}


def test_a_value_that_did_not_move_is_not_substituted() -> None:
    """Only the numbers that moved are swapped. A node whose value came out the same stays
    as written, even when the other node in the same arm moved."""
    before = _one_arm({"gtx1650ti": 100.0, "rtx3050": 250.0})
    after = _one_arm({"gtx1650ti": 110.0, "rtx3050": 250.0})
    text = '{"capability_arms": [{"name": "cap250", "capability_override": {"gtx1650ti": 100.0, "rtx3050": 250.0}}]}\n'
    out = promote.rewrite_overrides(text, before, after)
    assert out == text.replace("100.0", "110.0")
    assert json.loads(out) == after


def test_an_edit_that_breaks_the_json_falls_back_to_a_clean_dump() -> None:
    """The old number sits inside a longer literal, so swapping it as text leaves something
    that no longer parses. The rewrite writes `after` out whole rather than a broken file."""
    before = _one_arm({"gtx1650ti": 100.0, "rtx3050": 250.0})
    after = _one_arm({"gtx1650ti": -5.0, "rtx3050": 250.0})
    before["_note"] = 1100.0
    after["_note"] = 1100.0
    text = json.dumps(before)
    assert "1100.0" in text
    out = promote.rewrite_overrides(text, before, after)
    assert out == json.dumps(after, indent=2) + "\n"
    assert json.loads(out) == after
