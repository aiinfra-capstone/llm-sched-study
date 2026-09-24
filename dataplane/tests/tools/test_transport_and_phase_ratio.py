"""Two tools that turn measurements into a number other code relies on.

`measure_transport_overhead.py` writes the per-request cost of everything that is not the
engine into C-6, and the simulator adds it back to every request. A negative residual is a
join or clock problem, and averaging it in would make the network look smaller than it is.

`phase_ratio.py` reports R separately for prefill and for decode, per cell and as each
workload profile sees it. The profile numbers are the ones the phase-asymmetry result rests
on, so a bucket that cannot be priced has to be named rather than quietly counted as nothing.
"""

from __future__ import annotations

import json

import measure_transport_overhead as transport
import pandas as pd
import phase_ratio
import pytest
from support import REPO_ROOT

# --------------------------------------------------------------- measure_transport_overhead


def _runset(path, rows) -> None:
    pd.DataFrame(rows).to_parquet(path)


def _row(run_id, residual, *, warmup=False, status="ok") -> dict:
    return {
        "run_id": run_id,
        "transport_residual_ms": residual,
        "is_warmup": warmup,
        "status": status,
    }


def test_the_overhead_pools_warmed_up_ok_rows_across_runs(tmp_path) -> None:
    """Pooled across operating points on purpose: the quantity is the wiring, not the load."""
    path = tmp_path / "runset.parquet"
    _runset(
        path,
        [
            _row("r1", 4.0),
            _row("r1", 6.0),
            _row("r2", 5.0),
            _row("r2", 500.0, warmup=True),
            _row("r2", 900.0, status="timeout"),
        ],
    )
    block = transport.summarise(path)
    assert block["mean_ms"] == 5.0
    assert block["sd_ms"] == round((2 / 3) ** 0.5, 4)
    assert block["n_samples"] == 3
    assert block["measured_from"] == ["r1", "r2"]
    assert "2 run(s)" in block["source"]


def test_a_negative_residual_is_refused_rather_than_averaged(tmp_path) -> None:
    path = tmp_path / "runset.parquet"
    _runset(path, [_row("r1", 5.0), _row("r1", -2.0), _row("r1", -1.0)])
    with pytest.raises(SystemExit, match="2 rows have negative transport_residual_ms"):
        transport.summarise(path)


def test_a_frame_that_is_not_a_c5_runset_is_refused(tmp_path) -> None:
    path = tmp_path / "runset.parquet"
    _runset(path, [{"run_id": "r1", "status": "ok", "is_warmup": False}])
    with pytest.raises(SystemExit, match="no column 'transport_residual_ms'"):
        transport.summarise(path)


def test_a_runset_with_nothing_measured_is_refused(tmp_path) -> None:
    path = tmp_path / "runset.parquet"
    _runset(path, [_row("r1", 5.0, warmup=True), _row("r1", 5.0, status="timeout")])
    with pytest.raises(SystemExit, match="no warmed-up ok rows"):
        transport.summarise(path)


def test_the_block_conforms_to_c6_and_a_bad_one_does_not(monkeypatch) -> None:
    """`validate` reads the schema relative to the working directory, which is the repository
    root when the tool is run the documented way."""
    import jsonschema

    monkeypatch.chdir(REPO_ROOT)
    good = {"mean_ms": 5.0, "sd_ms": 0.5, "n_samples": 3, "source": "s", "measured_from": ["r1"]}
    transport.validate(good)
    with pytest.raises(jsonschema.ValidationError):
        transport.validate({**good, "n_samples": 0})


def _manifest(run_dir, **extra) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": run_dir.name, **extra}))


def test_main_writes_the_block_into_every_named_manifest(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO_ROOT)
    runset = tmp_path / "runset.parquet"
    _runset(runset, [_row("r1", 4.0), _row("r2", 6.0)])
    _manifest(tmp_path / "r1")
    _manifest(tmp_path / "r2", transport_overhead={"mean_ms": 9.0})

    argv = [
        "--runset",
        str(runset),
        "--apply",
        str(tmp_path / "r1"),
        str(tmp_path / "r2" / "manifest.json"),
    ]
    assert transport.main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "transport overhead: 5.0 +/- 1.0 ms over n=2 from 2 run(s)" in out
    assert "would set transport_overhead" in out and "(was 9.0 ms)" in out
    assert "transport_overhead" not in json.loads((tmp_path / "r1" / "manifest.json").read_text())

    assert transport.main(argv) == 0
    for run in ("r1", "r2"):
        block = json.loads((tmp_path / run / "manifest.json").read_text())["transport_overhead"]
        assert block["mean_ms"] == 5.0
        assert set(block) == set(transport.SCHEMA_KEYS)


def test_main_reports_a_run_with_no_manifest(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(REPO_ROOT)
    runset = tmp_path / "runset.parquet"
    _runset(runset, [_row("r1", 4.0)])
    assert transport.main(["--runset", str(runset), "--apply", str(tmp_path / "gone")]) == 1
    assert "no manifest at:" in capsys.readouterr().err


# ---------------------------------------------------------------------------- phase_ratio

FAST = "rtx3050_ngl99_p4_q4km_llama32_1b"
SLOW = "gtx1650ti_ngl99_p4_q4km_llama32_1b"


def _cell(pb, ob, c, service, prefill=None, decode=None) -> dict:
    e = {"prompt_bucket": pb, "output_bucket": ob, "concurrency": c, "service_ms_mean": service}
    if prefill is not None:
        e["prefill_ms_mean"] = prefill
        e["decode_ms_mean"] = decode
    return e


def _write_snap(root, node_class, when, entries, name=None) -> None:
    d = root / node_class
    d.mkdir(parents=True, exist_ok=True)
    snap = {
        "snapshot_id": f"cm_{node_class}_{when}",
        "node_class": node_class,
        "measured_at_unix": when,
        "entries": entries,
    }
    (d / (name or f"{when}.json")).write_text(json.dumps(snap))


@pytest.fixture
def models(tmp_path):
    root = tmp_path / "cost_models"
    small, large = ([1, 128], [1, 64]), ([129, 512], [1, 64])
    _write_snap(root, FAST, 1, [_cell(*small, 1, 999.0, 99.0, 900.0)])  # superseded
    _write_snap(
        root,
        FAST,
        2,
        [
            _cell(*small, 1, 400.0, 20.0, 360.0),
            _cell(*large, 1, 500.0, 60.0, 420.0),
            _cell(*small, 4, 600.0, 40.0, 540.0),
            _cell([513, 2048], [1, 64], 1, 900.0),  # no split: counted, not compared
        ],
    )
    _write_snap(
        root,
        SLOW,
        5,
        [
            _cell(*small, 1, 600.0, 180.0, 400.0),
            _cell(*large, 1, 1100.0, 600.0, 460.0),
            _cell(*small, 4, 900.0, 300.0, 580.0),
        ],
    )
    # Listed after the newest one, and older than it: must not replace it.
    _write_snap(root, FAST, 0, [_cell(*small, 1, 1.0, 0.5, 0.5)], name="z_older.json")
    (root / FAST / "notes.json").write_text(json.dumps({"comment": "not a snapshot"}))
    (root / FAST / "broken.json").write_text("{nope")
    return root


def test_newest_by_class_keeps_the_newest_snapshot_and_skips_everything_else(models) -> None:
    snaps = phase_ratio.newest_by_class(models)
    assert set(snaps) == {FAST, SLOW}
    assert snaps[FAST]["measured_at_unix"] == 2


def test_ratios_are_slow_over_fast_per_phase() -> None:
    r = phase_ratio.ratios(
        _cell([1, 2], [1, 2], 1, 400.0, 20.0, 360.0), _cell([1, 2], [1, 2], 1, 600.0, 180.0, 400.0)
    )
    assert r == {"R_service": 1.5, "R_prefill": 9.0, "R_decode": pytest.approx(400 / 360)}


def test_phase_cells_skip_a_cell_without_the_split(models) -> None:
    cells = phase_ratio.phase_cells(phase_ratio.newest_by_class(models)[FAST])
    assert len(cells) == 3
    assert (((513, 2048), (1, 64), 1)) not in cells


def test_model_of_reads_the_trailing_part_of_the_class() -> None:
    assert phase_ratio.model_of(FAST) == "llama32_1b"
    assert phase_ratio.model_of("cpu_ngl0_p4") is None


def test_locate_and_profile_buckets(tmp_path) -> None:
    cells = {((1, 128), (1, 64), 1): None, ((129, 512), (1, 64), 1): None}
    assert phase_ratio.locate(128, 64, 1, cells) == ((1, 128), (1, 64), 1)
    assert phase_ratio.locate(129, 64, 1, cells) == ((129, 512), (1, 64), 1)
    assert phase_ratio.locate(129, 64, 4, cells) is None
    cfg = tmp_path / "trace_mix.json"
    cfg.write_text(
        json.dumps({"length_dist": {"buckets": ["p128_o64", "p256_o32"], "weights": [3, 1]}})
    )
    assert phase_ratio.profile_buckets(cfg) == ([(128, 64), (256, 32)], [0.75, 0.25])


def _profile(tmp_path, name, buckets, weights):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"length_dist": {"buckets": buckets, "weights": weights}}))
    return path


def test_main_reports_every_shared_cell_and_each_profiles_weighted_r(
    models, tmp_path, capsys
) -> None:
    profile = _profile(tmp_path, "trace_mix", ["p128_o64", "p256_o32"], [1, 1])
    out = tmp_path / "report.json"
    argv = ["--fast", FAST, "--slow", SLOW, "--cost-models", str(models)]
    assert phase_ratio.main([*argv, "--profile", str(profile), "--out", str(out)]) == 0

    report = json.loads(out.read_text())
    assert report["fast"]["snapshot_id"] == f"cm_{FAST}_2"
    assert [c["prompt_bucket"] for c in report["cells"]] == [[1, 128], [129, 512]]
    assert report["cells"][0]["R_prefill"] == 9.0
    assert report["cells"][0]["decode_over_prefill"] == pytest.approx((400 / 360) / 9.0)
    (entry,) = report["profiles"]
    assert entry["profile"] == "trace_mix"
    assert entry["R_prefill"] == pytest.approx(0.5 * 9.0 + 0.5 * 10.0)
    assert entry["mean_rho"] == pytest.approx(0.5 * 2 + 0.5 * 8)
    assert entry["unpriced_buckets"] == []
    printed = capsys.readouterr().out
    assert "1 cell(s) with no phase split" in printed
    assert "what each workload profile sees" in printed


def test_a_profile_with_an_unpriced_bucket_names_it_and_weights_the_rest(
    models, tmp_path, capsys
) -> None:
    """Half the profile's mass lands where no shared cell exists. The profile's R is the
    weighted mean over the buckets that could be priced, and the other half is named.
    Counting the unpriced half as zero would report a pool half as heterogeneous as it is."""
    profile = _profile(tmp_path, "trace_long", ["p128_o64", "p1024_o64"], [1, 1])
    out = tmp_path / "report.json"
    argv = ["--fast", FAST, "--slow", SLOW, "--cost-models", str(models), "--profile", str(profile)]
    assert phase_ratio.main([*argv, "--out", str(out)]) == 0
    (entry,) = json.loads(out.read_text())["profiles"]
    assert entry["unpriced_buckets"] == ["p1024_o64"]
    assert "no shared cell for: p1024_o64" in capsys.readouterr().out
    assert entry["R_service"] == pytest.approx(1.5)
    assert entry["R_prefill"] == pytest.approx(9.0)
    assert entry["mean_rho"] == pytest.approx(2.0)


def test_a_higher_concurrency_is_reported_with_its_warning(models, capsys) -> None:
    argv = ["--fast", FAST, "--slow", SLOW, "--cost-models", str(models), "--concurrency", "4"]
    assert phase_ratio.main(argv) == 0
    assert "harness contention" in capsys.readouterr().out


def test_a_class_with_no_snapshot_or_no_shared_cell_is_refused(models, capsys) -> None:
    assert phase_ratio.main(["--fast", FAST, "--slow", "nobody", "--cost-models", str(models)]) == 1
    assert "no snapshot for nobody" in capsys.readouterr().err
    argv = ["--fast", FAST, "--slow", SLOW, "--cost-models", str(models), "--concurrency", "2"]
    assert phase_ratio.main(argv) == 1
    assert (
        "no shared cell at concurrency 2; shared concurrencies: [1, 4]" in capsys.readouterr().err
    )


def test_two_classes_on_different_models_are_warned_about(tmp_path, capsys) -> None:
    root = tmp_path / "cost_models"
    other = "cpu_ngl0_p4_q4km_llama3_8b"
    _write_snap(root, FAST, 1, [_cell([1, 128], [1, 64], 1, 400.0, 20.0, 360.0)])
    _write_snap(root, other, 1, [_cell([1, 128], [1, 64], 1, 4000.0, 900.0, 3000.0)])
    assert phase_ratio.main(["--fast", FAST, "--slow", other, "--cost-models", str(root)]) == 0
    assert "mixes the models with the machines" in capsys.readouterr().out
