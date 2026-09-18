"""tools/campaign_summary.py when a run set holds more than one workload.

Two shapes at one slow-node utilisation, or Poisson beside MMPP at one pool utilisation, put
two different conditions at the same rate. They are not one point with twice the runs, so the
workload is part of what a point is: it splits the points, the block-length keys, the load
trend, and the random stream every interval at that point is drawn from.

Coverage cannot see any of this. The workload enters through conditional expressions
(`(workload, stale, lam) if workload else (stale, lam)`, `{"workload": wl} if wl else {}`),
and branch coverage does not measure the two sides of a conditional expression, so the file
reads as fully covered while no test has ever put two workloads in one frame.

The other half of the requirement is that nothing moved for the campaigns already run. A
single-workload campaign names its runs with an empty workload, so its points are keyed by
staleness and rate exactly as they were before workloads existed, and the committed
summaries stay reproducible.
"""

from __future__ import annotations

import campaign_summary as cs
import numpy as np
import pandas as pd
from support import cell_rows, frame, manifests_for, run_id, write_manifest

BOOT = 200
PAIR = ("jsq", "wjsq")


def _noise(n: int, seed: int, loc: float) -> np.ndarray:
    return np.random.default_rng(seed).normal(loc, 120.0, n).clip(50.0)


def _runs(workload: str, lam: float, base: float, seed: int, **kw) -> list[list[dict]]:
    """One point of one workload: JSQ and WJSQ, WJSQ faster, two repeats each."""
    out = []
    for k, policy in enumerate(PAIR):
        for repeat in (1, 2):
            values = _noise(40, seed + k + 10 * repeat, base - 80.0 * k)
            out.append(
                cell_rows(
                    policy,
                    values,
                    repeat=repeat,
                    lam=lam,
                    workload=workload,
                    point=f"l{lam:g}",
                    **kw,
                )
            )
    return out


def _summarise(data: pd.DataFrame, root, seed: int = 4) -> dict:
    return cs.summarise(data, BOOT, seed, manifests_for(root, data))


# ------------------------------------------------------------------ what names a point


def test_where_names_a_point_by_workload_only_when_there_is_one() -> None:
    assert cs.where("", 0.0, 2.4) == (0.0, 2.4)
    assert cs.where("generation", 0.0, 2.4) == ("generation", 0.0, 2.4)


def test_two_workloads_at_one_rate_are_two_points(tmp_path) -> None:
    data = frame(*_runs("generation", 2.4, 900.0, 1), *_runs("summarisation", 2.4, 1700.0, 50))
    s = _summarise(data, tmp_path)

    assert [(pt["workload"], pt["lambda_rps"]) for pt in s["points"]] == [
        ("generation", 2.4),
        ("summarisation", 2.4),
    ]
    gen, summ = s["points"]
    assert gen["policies"]["jsq"]["mean"]["value"] < summ["policies"]["jsq"]["mean"]["value"]
    # Each point is built from its own workload's runs only: 2 repeats x 40 arrivals.
    for pt in s["points"]:
        for policy in PAIR:
            assert pt["policies"][policy]["runs"] == 2
            assert pt["policies"][policy]["requests"] == 80


def test_the_block_length_key_names_the_workload(tmp_path) -> None:
    data = frame(*_runs("generation", 2.4, 900.0, 2), *_runs("summarisation", 2.4, 1700.0, 60))
    blocks = _summarise(data, tmp_path)["bootstrap"]["block_length_requests"]
    assert sorted(blocks) == ["generation_s0_l2.4", "summarisation_s0_l2.4"]


def test_each_workload_gets_its_own_load_trend(tmp_path) -> None:
    """A rate that rises from one workload's point to another's is not load, so the trend is
    per workload and the single-trend field is left empty."""
    data = frame(
        *_runs("generation", 1.2, 900.0, 3),
        *_runs("generation", 2.4, 1100.0, 4),
        *_runs("summarisation", 1.2, 1700.0, 70),
        *_runs("summarisation", 2.4, 2100.0, 80),
    )
    s = _summarise(data, tmp_path)

    assert s["load_trend_queue_aware"] is None
    trends = s["load_trend_queue_aware_by_workload"]
    assert sorted(trends) == ["generation", "summarisation"]
    for trend in trends.values():
        assert trend["lambdas"] == [1.2, 2.4]
        assert len(trend["queue_aware_gain_ms"]) == 2


def test_one_workload_keeps_the_single_trend_and_no_label(tmp_path) -> None:
    data = frame(*_runs("", 1.2, 900.0, 5), *_runs("", 2.4, 1100.0, 6))
    s = _summarise(data, tmp_path)

    assert "load_trend_queue_aware_by_workload" not in s
    assert s["load_trend_queue_aware"]["lambdas"] == [1.2, 2.4]
    assert all("workload" not in pt for pt in s["points"])
    assert sorted(s["bootstrap"]["block_length_requests"]) == ["s0_l1.2", "s0_l2.4"]


def test_a_campaign_with_no_workload_names_reads_as_it_did_before_workloads(tmp_path) -> None:
    """The committed summaries have to stay reproducible. A manifest that names no workload
    and one that names the empty string are the same campaign, and both take the streams the
    driver used before workloads were an axis."""
    data = frame(*_runs("", 2.4, 900.0, 7))
    unnamed = cs.summarise(data, BOOT, 9, manifests_for(tmp_path / "unnamed", data))
    named = cs.summarise(data, BOOT, 9, manifests_for(tmp_path / "named", data.assign(workload="")))
    assert named == unnamed


# --------------------------------------------------------------- one stream per workload


def test_the_same_numbers_under_two_workloads_share_values_and_not_streams(tmp_path) -> None:
    """The point value is arithmetic, so identical inputs give identical values. The interval
    is drawn from a stream keyed by the point, and the workload is part of that key, so the
    two points do not reuse one set of draws."""
    values = {policy: _noise(40, 30 + k, 900.0 - 80.0 * k) for k, policy in enumerate(PAIR)}
    runs = []
    for workload in ("generation", "summarisation"):
        for policy, series in values.items():
            for repeat in (1, 2):
                runs.append(
                    cell_rows(
                        policy, series, repeat=repeat, lam=2.4, workload=workload, point="l2.4"
                    )
                )
    s = _summarise(frame(*runs), tmp_path)
    gen, summ = s["points"]

    for policy in PAIR:
        assert gen["policies"][policy]["mean"]["value"] == summ["policies"][policy]["mean"]["value"]
        assert gen["policies"][policy]["mean"]["ci95"] != summ["policies"][policy]["mean"]["ci95"]
    gain = "calibration_gain"
    assert (
        gen[gain]["queue_aware"]["mean"]["gain_ms"] == summ[gain]["queue_aware"]["mean"]["gain_ms"]
    )
    assert (
        gen[gain]["queue_aware"]["mean"]["gain_ms_ci95"]
        != summ[gain]["queue_aware"]["mean"]["gain_ms_ci95"]
    )


def test_an_invalid_run_undefines_its_own_workload_only(tmp_path) -> None:
    """An invalid run is excluded from the run set and named in the summary, and its cell's
    contrasts are undefined rather than averaged over the survivors. The workload it belongs
    to is part of which cell that is."""
    good = _runs("generation", 2.4, 900.0, 8)
    hurt = [
        rows for rows in _runs("summarisation", 2.4, 1700.0, 90) if "_r2" not in rows[0]["run_id"]
    ]
    data = frame(*good, *hurt)
    path = manifests_for(tmp_path, data)
    bad = run_id("wjsq", "l2.4", 2, workload="summarisation")
    write_manifest(
        tmp_path,
        bad,
        policy="wjsq",
        lam=2.4,
        workload="summarisation",
        validity={"dropped_requests": 3, "valid": False},
    )

    s = cs.summarise(data, BOOT, 4, path)
    gen, summ = s["points"]
    assert bad in s["invalid_runs"]
    assert summ["policies"]["wjsq"]["invalid_runs"][bad]
    assert summ["calibration_gain"]["queue_aware"]["status"].startswith("undefined")
    assert "invalid" in summ["calibration_gain"]["queue_aware"]["status"]
    assert gen["calibration_gain"]["queue_aware"]["status"] == "defined"
    assert "invalid_runs" not in gen["policies"]["wjsq"]


def test_the_markdown_names_the_workload_on_every_point_and_trend(tmp_path) -> None:
    data = frame(
        *_runs("generation", 1.2, 900.0, 11),
        *_runs("generation", 2.4, 1100.0, 12),
        *_runs("summarisation", 1.2, 1700.0, 13),
        *_runs("summarisation", 2.4, 2100.0, 14),
    )
    md = cs.markdown(_summarise(data, tmp_path))
    for workload in ("generation", "summarisation"):
        assert f"### {workload}, 1.2 req/s" in md
        assert f"### {workload}, 2.4 req/s" in md
        assert f"### Queue-aware calibration gain against load, {workload}" in md
    assert md.count("JSQ - WJSQ on the mean at [1.2, 2.4]") == 2


def test_the_markdown_of_one_workload_names_no_workload(tmp_path) -> None:
    data = frame(*_runs("", 1.2, 900.0, 16), *_runs("", 2.4, 1100.0, 17))
    md = cs.markdown(_summarise(data, tmp_path))
    assert "### 1.2 req/s" in md
    assert "### Queue-aware calibration gain against load\n" in md


def test_a_workload_column_on_the_frame_never_decides_a_point(tmp_path) -> None:
    """The workload comes from each run's manifest, which is the record of what ran. A column
    on the joined frame is overwritten, so a mislabelled row cannot split a point."""
    data = frame(*_runs("generation", 2.4, 900.0, 15))
    path = manifests_for(tmp_path, data)
    s = cs.summarise(data.assign(workload="mislabelled"), BOOT, 4, path)
    assert [pt["workload"] for pt in s["points"]] == ["generation"]
