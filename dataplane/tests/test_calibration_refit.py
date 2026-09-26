"""Re-fitting an old calibration run from its own observations, by today's rule (M1).

The occupancy filter fits a cell only from samples served at the concurrency it claims. It is
computed while a campaign runs, so a calibration recorded before it existed was fitted from
every sample, drained ones included. The 3050 will be recalibrated with the filter; unless
the 1650 Ti's run is re-fitted by the same rule, the two nodes of one pool are described by
two different fitting procedures, and R between them carries the difference.

Written before the code, so the tests fix the interface:

    dataplane.calibration.refit.refit_run(run_dir, out_root) -> Path
        Reads `campaign.json` and `observations.jsonl` from `run_dir`, recomputes every
        sample's `occupancy_mean` from its engine span, per segment and per grid cell,
        exactly as the campaign does online, fits through the campaign's own `_finish`, and
        writes a new run directory under `out_root` with `campaign.write_result`. Returns it.

    tools/refit_calibration.py RUN_DIR [--out-root DIR]
        The same from the command line. `--out-root` defaults to RUN_DIR's parent.

Decisions these tests pin, and why:

* The refit is a new run, stamped when it was fitted. `_finish` already stamps a fit with
  the time it was made and names the run `cal_<class>_<time>`, so going through it gives
  the refit a newer `measured_at_unix` than the run it replaces, and `promote_calibration`
  promotes it by its existing rule. Keeping the original time instead would put two series
  with one timestamp in the contracts, and the scheduler's "newest in class" would be a tie.
  The original series stays where it is, because manifests of runs already done name it.
* `campaign.json` names the run it was re-fitted from as `refit_of`. That file is not a
  contract, so it can carry the link; the C-3 snapshot cannot, since the Java reader
  refuses a field it does not know.
* Occupancy is always recomputed, never trusted from the file. It is a pure function of the
  spans, so recomputing is safe, and a run whose stored value is wrong gets the right one.
* Occupancy is computed within one segment and one grid cell. Offline, the cell is the only
  record of which requests shared the engine, since cells ran one after another.
* Occupancy counts a cell's warmups, online as well as offline. The warmups share the
  engine with a cell's first samples, so leaving them out records those samples as drained
  from a full batch. The campaign logs them with `"segment": "warmup"` and the refit counts
  them by cell from that log, so the two give the same number. A log written before
  warmups were logged has none to count, and its refit says so with
  `occupancy_counts_warmups: false`. `test_calibration_occupancy.py` pins the campaign side.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import json
import time
from pathlib import Path

import pytest
from conftest import REPO_ROOT, assert_conforms
from test_calibration_campaign import FakeEngine, _config

from dataplane.calibration import campaign as camp
from dataplane.calibration import cost_model as cm

T0 = 1_789_000_000
T1 = T0 + 86_400

G2_1650TI = (
    REPO_ROOT
    / "runs"
    / "calibration"
    / "llama32-1b-anchorgrid"
    / "cal_gtx1650ti_ngl99_p4_q4km_llama32_1b_1789619501"
)


@pytest.fixture
def refit():
    return importlib.import_module("dataplane.calibration.refit")


@pytest.fixture
def cli():
    import sys

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    return importlib.import_module("refit_calibration")


def _rows(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "observations.jsonl").read_text().splitlines()]


def _files(run_dir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(run_dir)): p.read_bytes()
        for p in sorted(run_dir.rglob("*"))
        if p.is_file()
    }


def _online(tmp_path, monkeypatch, **config) -> tuple[Path, list[dict], dict]:
    """A campaign run as it is recorded today, then stripped back to how an old run looks."""
    monkeypatch.setattr(time, "time", lambda: float(T0))
    result = asyncio.run(camp.run_campaign(FakeEngine(), _config(**config)))
    run_dir = tmp_path / "calibration" / result.report["run_id"]
    camp.write_result(run_dir, result)
    recorded = _rows(run_dir)
    assert all(r["occupancy_mean"] is not None for r in recorded)
    old = [{k: v for k, v in r.items() if k != "occupancy_mean"} for r in recorded]
    (run_dir / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))
    return run_dir, recorded, result.report


def _snapshots(run_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((run_dir / "snapshots").glob("*.json"))]


# ----------------------------------------------------------- the same rule as the campaign


def test_a_refit_recovers_the_occupancy_the_campaign_recorded(refit, tmp_path, monkeypatch) -> None:
    original, recorded, _ = _online(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(original, tmp_path / "calibration")

    rows = _rows(out)
    assert [r["occupancy_mean"] for r in rows] == [r["occupancy_mean"] for r in recorded]
    strip = lambda r: {k: v for k, v in r.items() if k != "occupancy_mean"}
    assert [strip(r) for r in rows] == [strip(r) for r in recorded]


def test_a_refit_fits_the_same_table_the_campaign_fitted(refit, tmp_path, monkeypatch) -> None:
    """The only thing a refit may change is which samples a cell keeps. On a run that was
    recorded with the filter, that is nothing, so the table is the one the campaign wrote."""
    original, _, _ = _online(tmp_path, monkeypatch)
    before = _snapshots(original)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    after = _snapshots(refit.refit_run(original, tmp_path / "calibration"))

    assert len(after) == len(before) > 1
    for old, new in zip(before, after, strict=True):
        assert new["entries"] == old["entries"]
        assert new["stochastic"] == old["stochastic"]


def test_a_refit_reports_what_each_cell_kept(refit, tmp_path, monkeypatch) -> None:
    original, _, report = _online(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(original, tmp_path / "calibration")
    new = json.loads((out / "campaign.json").read_text())
    assert new["grid_samples_at_stated_concurrency"] == report["grid_samples_at_stated_concurrency"]
    assert new["config"] == report["config"]
    assert new["failures"] == report["failures"]


def test_occupancy_is_recomputed_even_when_the_file_carries_a_wrong_one(
    refit, tmp_path, monkeypatch
) -> None:
    original, recorded, _ = _online(tmp_path, monkeypatch)
    wrong = [r | {"occupancy_mean": 99.0} for r in _rows(original)]
    (original / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in wrong))
    monkeypatch.setattr(time, "time", lambda: float(T1))
    rows = _rows(refit.refit_run(original, tmp_path / "calibration"))
    assert [r["occupancy_mean"] for r in rows] == [r["occupancy_mean"] for r in recorded]


def _bare(row: dict) -> cm.Observation:
    """A logged row as the observation it was, before any occupancy was put on it."""
    fields = set(cm.Observation.__dataclass_fields__) - {"occupancy_mean"}
    return cm.Observation(**{k: row[k] for k in fields})


def test_a_refit_counts_the_logged_warmups_and_reproduces_the_campaign_exactly(
    refit, tmp_path, monkeypatch
) -> None:
    """Four slots, two warmups, eight samples: the shape of the real grid. The refit reads
    the warmup lines back and recounts every occupancy to the number the campaign wrote."""
    original, recorded, report = _online(
        tmp_path, monkeypatch, concurrencies=[4], warmup_per_cell=2, samples_per_cell=8
    )
    assert report["occupancy_counts_warmups"] is True
    assert [r["segment"] for r in recorded].count("warmup") == 2

    # The warmups move the first samples' number, so reproducing it needs them.
    grid = [r for r in recorded if r["segment"] == "grid"]
    alone = camp._with_occupancy([_bare(r) for r in grid])
    assert [o.occupancy_mean for o in alone] != [r["occupancy_mean"] for r in grid]

    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(original, tmp_path / "calibration")
    assert [r["occupancy_mean"] for r in _rows(out)] == [r["occupancy_mean"] for r in recorded]
    new = json.loads((out / "campaign.json").read_text())
    assert new["occupancy_counts_warmups"] is True
    assert new["grid_samples_at_stated_concurrency"] == report["grid_samples_at_stated_concurrency"]


def test_a_log_without_warmup_lines_says_its_occupancy_does_not_count_them(
    refit, tmp_path, monkeypatch
) -> None:
    """A run recorded before warmups were logged. Its first sample shared the engine with
    a warmup the log does not hold, so the refit cannot count it and has to say so."""
    original, recorded, _ = _online(tmp_path, monkeypatch)
    old = [r for r in _rows(original) if r["segment"] != "warmup"]
    (original / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in old))

    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(original, tmp_path / "calibration")
    assert json.loads((out / "campaign.json").read_text())["occupancy_counts_warmups"] is False

    rows = _rows(out)
    assert all(r["segment"] != "warmup" for r in rows)
    grid = [r for r in rows if r["segment"] == "grid"]
    online = [r for r in recorded if r["segment"] == "grid"]
    assert [r["occupancy_mean"] for r in grid] == [
        o.occupancy_mean for o in refit.with_occupancy([_bare(r) for r in grid])
    ]
    # The sample that ran beside the warmup reads lower without it.
    assert any(g["occupancy_mean"] < o["occupancy_mean"] for g, o in zip(grid, online, strict=True))


def test_a_warmup_counts_only_for_its_own_grid_cell(refit, tmp_path, monkeypatch) -> None:
    """A warmup of the p64_o32_c2 cell spans the same interval as the first sample of that
    cell, a sample of another cell, and a sustained sample with the same key. Only the grid
    sample of its own cell counts it: the sustained segment ran minutes later, and a shared
    key does not mean it shared the engine."""
    rows = [
        _row("warmup", 64, 32, 2, 0, 100),
        _row("grid", 64, 32, 2, 0, 100),
        _row("grid", 64, 32, 2, 100, 200),
        _row("grid", 256, 32, 2, 0, 100),
        _row("sustained", 64, 32, 2, 0, 100),
    ]
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(_handmade_run(tmp_path, rows), tmp_path / "calibration")
    got = sorted((r["segment"], r["prompt_len"], r["occupancy_mean"]) for r in _rows(out))
    assert got == sorted(
        [
            ("warmup", 64, 2.0),
            ("grid", 64, 2.0),
            ("grid", 64, 1.0),
            ("grid", 256, 1.0),
            ("sustained", 64, 1.0),
        ]
    )
    assert json.loads((out / "campaign.json").read_text())["occupancy_counts_warmups"] is True


def _handmade_run(tmp_path, rows: list[dict]) -> Path:
    run_dir = tmp_path / "calibration" / f"cal_fake_ngl99_p2_q4km_{T0}"
    run_dir.mkdir(parents=True)
    (run_dir / "campaign.json").write_text(
        json.dumps({"run_id": run_dir.name, "config": _config().as_dict(), "f18_status": "full"})
    )
    (run_dir / "observations.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return run_dir


def _row(segment, p, o, c, start_ms, end_ms, status="ok") -> dict:
    return {
        "prompt_len": p,
        "output_len": o,
        "concurrency": c,
        "service_ns": int((end_ms - start_ms) * 1e6),
        "output_tokens": o if status == "ok" else 0,
        "t_end_ns": int(end_ms * 1e6),
        "status": status,
        "prefill_ns": int((end_ms - start_ms) * 0.2e6),
        "decode_ns": int((end_ms - start_ms) * 0.7e6),
        "error": "",
        "segment": segment,
    }


def test_occupancy_is_computed_within_one_segment_and_one_cell(
    refit, tmp_path, monkeypatch
) -> None:
    """Two cells whose spans overlap in time, and a sustained segment over the same interval.
    Each request shares the engine only with its own cell, so each records 2, not 4 or 6."""
    rows = [
        _row("grid", 64, 32, 2, 0, 100),
        _row("grid", 64, 32, 2, 0, 100),
        _row("grid", 256, 32, 2, 0, 100),
        _row("grid", 256, 32, 2, 0, 100),
        _row("sustained", 64, 32, 2, 0, 100),
        _row("sustained", 64, 32, 2, 0, 100),
    ]
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(_handmade_run(tmp_path, rows), tmp_path / "calibration")
    assert [r["occupancy_mean"] for r in _rows(out)] == [2.0] * 6
    assert [r["segment"] for r in _rows(out)] == [r["segment"] for r in rows]


def test_a_drained_sample_is_kept_in_the_record_and_left_out_of_the_fit(
    refit, tmp_path, monkeypatch
) -> None:
    """Three requests of a concurrency-2 cell: two ran together, the third ran alone after
    them. The third is recorded with occupancy 1 and is not fitted."""
    rows = [
        _row("grid", 64, 32, 2, 0, 100),
        _row("grid", 64, 32, 2, 0, 100),
        _row("grid", 64, 32, 2, 100, 160),
        # A timeout with no span of its own: it shared the engine with nobody.
        _row("grid", 64, 32, 2, 300, 300, status="timeout"),
    ]
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(_handmade_run(tmp_path, rows), tmp_path / "calibration")
    recorded = _rows(out)
    assert len(recorded) == 4
    assert recorded[2]["occupancy_mean"] == pytest.approx(1.0)
    report = json.loads((out / "campaign.json").read_text())
    assert report["grid_samples_at_stated_concurrency"] == {"p64_o32_c2": "2/3"}
    assert report["failures"] == {"timeout": 1}


# ------------------------------------------------------------------ what the refit is called


def test_the_refit_is_a_new_run_stamped_when_it_was_fitted(refit, tmp_path, monkeypatch) -> None:
    original, _, report = _online(tmp_path, monkeypatch)
    before = _snapshots(original)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    out = refit.refit_run(original, tmp_path / "calibration")
    new_report = json.loads((out / "campaign.json").read_text())

    assert out == tmp_path / "calibration" / f"cal_{_config().node_class}_{T1}"
    assert new_report["run_id"] == out.name
    assert new_report["refit_of"] == report["run_id"]
    after = _snapshots(out)
    assert after[0]["measured_at_unix"] == T1
    assert max(s["measured_at_unix"] for s in after) > max(s["measured_at_unix"] for s in before)
    assert not {s["snapshot_id"] for s in after} & {s["snapshot_id"] for s in before}
    assert all(s["calibration_run_ids"] == [out.name] for s in after)
    # The spacing of the series is the campaign's own, so staleness lookups still work.
    gaps = lambda series: [
        b["measured_at_unix"] - a["measured_at_unix"] for a, b in itertools.pairwise(series)
    ]
    assert gaps(after) == gaps(before)


def test_the_original_run_is_left_exactly_as_it_was(refit, tmp_path, monkeypatch) -> None:
    original, _, _ = _online(tmp_path, monkeypatch)
    before = _files(original)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    refit.refit_run(original, tmp_path / "elsewhere")
    assert _files(original) == before


def test_the_refit_snapshots_satisfy_c3(refit, tmp_path, monkeypatch, schema) -> None:
    original, _, _ = _online(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    snapshots = _snapshots(refit.refit_run(original, tmp_path / "calibration"))
    assert_conforms(schema("cost_model"), snapshots, "snapshot")


# ------------------------------------------------------------------------ the command line


def test_the_command_writes_beside_the_original_by_default(
    cli, tmp_path, monkeypatch, capsys
) -> None:
    original, _, _ = _online(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    assert cli.main([str(original)]) == 0
    out = original.parent / f"cal_{_config().node_class}_{T1}"
    assert (out / "campaign.json").is_file()
    assert str(out) in capsys.readouterr().out


def test_the_command_takes_an_out_root(cli, tmp_path, monkeypatch) -> None:
    original, _, _ = _online(tmp_path, monkeypatch)
    monkeypatch.setattr(time, "time", lambda: float(T1))
    assert cli.main([str(original), "--out-root", str(tmp_path / "refits")]) == 0
    assert (tmp_path / "refits" / f"cal_{_config().node_class}_{T1}" / "snapshots").is_dir()


@pytest.mark.parametrize("missing", ["campaign.json", "observations.jsonl"])
def test_a_directory_that_is_not_a_calibration_run_is_refused(
    cli, tmp_path, monkeypatch, capsys, missing
) -> None:
    original, _, _ = _online(tmp_path, monkeypatch)
    (original / missing).unlink()
    assert cli.main([str(original)]) == 2
    assert missing in capsys.readouterr().out


# ----------------------------------------------------------------- the run that needs it


def test_refitting_changes_only_cells_above_one_slot(refit, tmp_path, monkeypatch) -> None:
    """The same claim as the G2 data check below, on a run built here so it runs anywhere.
    The old fit took every sample; the refit keeps only the steady ones. At one slot nobody
    shares the engine, so those cells, and the capability read from them, do not move. Above
    one slot the last sample of a cell ran beside fewer requests, and the cell loses it."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import pool_load

    run_dir, _, _ = _online(tmp_path, monkeypatch)
    promoted = _snapshots(run_dir)[0]
    grid, sustained, _ = refit.load_observations(run_dir / "observations.jsonl")
    config = camp.CampaignConfig.from_dict(
        json.loads((run_dir / "campaign.json").read_text())["config"]
    )
    # How a run recorded before the occupancy filter was fitted: every sample, drained ones too.
    old = cm.build_snapshot(
        grid + sustained,
        node_class=config.node_class,
        prompt_edges=config.prompt_edges,
        output_edges=config.output_edges,
        provenance=config.provenance,
        admissibility=config.admissibility,
        calibration_run_ids=promoted["calibration_run_ids"],
        stochastic=promoted["stochastic"],
        measured_at_unix=promoted["measured_at_unix"],
    )
    monkeypatch.setattr(time, "time", lambda: float(T1))
    new = _snapshots(refit.refit_run(run_dir, tmp_path / "refit"))[0]

    key = lambda e: (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"])
    old_cells = {key(e): e for e in old["entries"]}
    new_cells = {key(e): e for e in new["entries"]}
    assert set(new_cells) == set(old_cells)
    for k, e in new_cells.items():
        if k[2] == 1:
            assert e == old_cells[k], k
    assert any(e["n_samples"] < old_cells[k]["n_samples"] for k, e in new_cells.items() if k[2] > 1)
    assert pool_load.capability(new) == pool_load.capability(old)


@pytest.mark.skipif(
    not G2_1650TI.is_dir(),
    reason="lab-machine data check: the G2 1650 Ti calibration is gitignored and only on the "
    "lab machine; test_refitting_changes_only_cells_above_one_slot covers the rule anywhere",
)
def test_refitting_the_g2_1650ti_run_changes_only_cells_above_one_slot(
    refit, tmp_path, monkeypatch
) -> None:
    """At one slot no request shares the engine, so no sample drains and those cells, and the
    capability read from them, must come out exactly as promoted. Above one slot the last
    samples of a cell drained, and some cell must now keep fewer than it had."""
    import pool_load

    old = _snapshots(G2_1650TI)[0]
    monkeypatch.setattr(time, "time", lambda: float(T1 + 10**6))
    out = refit.refit_run(G2_1650TI, tmp_path)
    new = _snapshots(out)[0]

    key = lambda e: (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"])
    # The promoted snapshots carry the phase split `backfill_phase_split.py` added at
    # promotion. The fit now writes the same split itself, so at one slot the whole cell,
    # split included, comes out as promoted. The one key the fit adds is `thin` (D5), which
    # the promoted snapshots predate, and a cell at one slot is never thin.
    old_cells = {key(e): e for e in old["entries"]}
    new_cells = {key(e): e for e in new["entries"]}
    assert set(new_cells) == set(old_cells)
    for k, e in new_cells.items():
        if k[2] == 1:
            assert e["thin"] is False, k
            assert {f: v for f, v in e.items() if f != "thin"} == old_cells[k], k
        assert e["n_samples"] >= 1
    assert pool_load.capability(new) == pool_load.capability(old)
    kept = json.loads((out / "campaign.json").read_text())["grid_samples_at_stated_concurrency"]
    drained = {c: v for c, v in kept.items() if int(v.split("/")[0]) < int(v.split("/")[1])}
    assert drained, "no cell above one slot kept fewer samples than it had"
    assert all(not c.endswith("_c1") for c in drained)
    assert cm.OCCUPANCY_TOLERANCE == 0.05


def test_a_refit_that_dropped_nothing_prints_no_thin_cell_block(
    cli, tmp_path, monkeypatch, capsys
) -> None:
    """At one slot nobody shares the engine, so every sample is steady and every cell keeps
    all of them. The block that lists cells fitted from fewer samples has nothing to list,
    and an empty heading would read as though something had been dropped."""
    rows = [_row("grid", 64, 32, 1, 100 * i, 100 * i + 90) for i in range(4)]
    rows += [_row("grid", 256, 32, 1, 1000 + 100 * i, 1000 + 100 * i + 90) for i in range(3)]
    monkeypatch.setattr(time, "time", lambda: float(T1))
    assert cli.main([str(_handmade_run(tmp_path, rows))]) == 0
    out = capsys.readouterr().out
    assert "fewer samples than they hold" not in out
    assert "7 grid + 0 sustained samples" in out


def test_a_source_with_no_snapshots_is_refitted_and_says_there_is_nothing_to_compare(
    cli, tmp_path, monkeypatch, capsys
) -> None:
    """A campaign whose tau could not be fitted wrote no snapshot, but its samples are still
    worth refitting. There is no old table to set the new one against, so it says so."""
    original, _, _ = _online(tmp_path, monkeypatch)
    for path in (original / "snapshots").glob("*.json"):
        path.unlink()
    monkeypatch.setattr(time, "time", lambda: float(T1))
    assert cli.main([str(original)]) == 0
    out = capsys.readouterr().out
    assert "no snapshot to compare against" in out
    assert "capability and prefill are not compared" not in out
