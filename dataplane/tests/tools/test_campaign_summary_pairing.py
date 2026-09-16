"""tools/campaign_summary.py: what is paired with what.

Pairing is the reason the summary was rewritten, so these tests come first. A contrast
between two policies is only a contrast if both sides were measured on the same arrivals:
the same repeats (repeat k is arrival seed k) and the same positions in each repeat.

The rules pinned here:

* A request that failed is a real outcome under its policy. Latency contrasts use one
  shared set of positions for every cell in the contrast, and a position that failed under
  any of those cells makes the contrast undefined with the failure count. The cell's own
  numbers cover its own ok requests and report `not_ok`; SLO attainment counts the failure
  as a miss.
* A row missing for one policy while present for the others in the same repeat is lost log
  data, and `summarise` raises naming the run and the req_id.
* A position that is not measured in a repeat (inside that repeat's warmup) is excluded
  from every statistic in that repeat, the SLO denominator included.
* The shared set is chosen per contrast, so a bad arm only undefines the contrasts it is in.
* A repeat missing for one cell is intersected away, per contrast, for the point value and
  every draw alike. A repeat excluded because its manifest says `valid: false` is not: the
  cell is reported invalid at that point and contrasts that include it are undefined.

Names these tests introduce, collected here so they can be renamed in one place:

    policies[p]["not_ok"]                  failed measured requests in that cell
    policies[p]["runs_differ_at_point"]    run count differs from another cell at the point
    policies[p]["invalid_runs"]            {run_id: [reason, ...]} from manifests, valid: false
    <contrast>["repeats_used"]             sorted repeat numbers the contrast was computed on
    <contrast>["repeats_dropped"]          {policy: [repeat, ...]} left out by the intersection
    <contrast>["single_repeat"]            True when repeats_used has one entry
    point["h1_repeats_used"], point["h1_repeats_dropped"]   the same for the 2x2
    summarise raises ValueError for a missing row

<contrast> is `calibration_gain["queue_blind"]` or `calibration_gain["queue_aware"]`.
An undefined contrast carries a `status` starting with "undefined" (for H1, `h1_status`).
"""

from __future__ import annotations

import json

import campaign_summary as cs
import numpy as np
import pandas as pd
import pytest
from support import cell_rows, frame, manifests_for, run_id, write_manifest

H1 = ("round_robin", "static_weighted", "jsq", "wjsq")
BOOT = 300


def _summarise(data: pd.DataFrame, root=None, n_boot: int = BOOT, seed: int = 11) -> dict:
    path = manifests_for(root, data) if root is not None else None
    return cs.summarise(data, n_boot, seed, path)


def _noise(n: int, seed: int, loc: float = 1000.0, scale: float = 150.0) -> np.ndarray:
    return np.random.default_rng(seed).normal(loc, scale, n).clip(50.0)


# ------------------------------------------------------------------ paired resampling


def test_identical_arrays_give_zero_width_difference_and_interaction() -> None:
    a = _noise(60, 1)
    s = _summarise(frame(*(cell_rows(p, a) for p in H1)))
    pt = s["points"][0]
    assert pt["h1_status"] == "defined"
    for stat in cs.LATENCY_STATS:
        assert pt["h1"][stat]["ci95"] == [0.0, 0.0]
        assert pt["h1"][stat]["interaction_log_ci95"] == [0.0, 0.0]
    for kind in ("queue_blind", "queue_aware"):
        assert pt["calibration_gain"][kind]["mean"]["gain_ms_ci95"] == [0.0, 0.0]
        assert pt["calibration_gain"][kind]["mean"]["ratio_ci95"] == [1.0, 1.0]


def test_a_constant_shift_gives_an_interval_of_exactly_that_shift() -> None:
    """Independent resampling of the two arms fails this: the interval would be as wide as
    the noise in either arm, not zero."""
    a = _noise(60, 2)
    s = _summarise(frame(cell_rows("round_robin", a + 25.0), cell_rows("static_weighted", a)))
    gain = s["points"][0]["calibration_gain"]["queue_blind"]
    assert gain["status"] == "defined"
    assert gain["mean"]["gain_ms"] == 25.0
    assert gain["mean"]["gain_ms_ci95"] == [25.0, 25.0]
    assert gain["p95"]["gain_ms_ci95"] == [25.0, 25.0]


def test_repeats_are_resampled_jointly_across_policies() -> None:
    """Repeat 1 is slow for both policies, repeat 2 fast for both. Drawing repeat numbers
    separately per policy would pair a slow repeat with a fast one."""
    high, low = _noise(60, 3, loc=3000.0), _noise(60, 4, loc=300.0)
    data = frame(
        cell_rows("round_robin", high + 40.0, repeat=1),
        cell_rows("round_robin", low + 40.0, repeat=2),
        cell_rows("static_weighted", high, repeat=1),
        cell_rows("static_weighted", low, repeat=2),
    )
    gain = _summarise(data)["points"][0]["calibration_gain"]["queue_blind"]
    assert gain["mean"]["gain_ms_ci95"] == [40.0, 40.0]


# ------------------------------------------------------ requests that failed or went missing


def _five(root=None, *, rr=None, sw=None, **kw):
    # Level series: the first and last positions are equal, so neither cell is transient.
    rr_rows = cell_rows("round_robin", [120, 100, 130, 110, 120], **(rr or {}))
    sw_rows = cell_rows("static_weighted", [100, 95, 99_999, 105, 100], **(sw or {}))
    return frame(rr_rows, sw_rows)


def test_a_failed_position_makes_the_contrast_undefined_with_its_count(tmp_path) -> None:
    s = _summarise(_five(sw={"status": {3: "timeout"}}), tmp_path)
    pt = s["points"][0]
    gain = pt["calibration_gain"]["queue_blind"]
    assert gain["status"].startswith("undefined")
    assert "static_weighted" in gain["status"]
    assert "1" in gain["status"]
    assert "mean" not in gain
    sw = pt["policies"]["static_weighted"]
    assert sw["not_ok"] == 1
    assert pt["policies"]["round_robin"]["not_ok"] == 0
    # The cell's own numbers are over its own ok requests.
    assert sw["mean"]["value"] == 100.0
    assert sw["requests"] == 4
    # SLO attainment counts the failure as a miss: 4 of 5 within 2x of 391 ms.
    assert sw["slo_e2e_2x"]["value"] == 0.8


def test_a_row_missing_for_one_policy_is_refused_by_name() -> None:
    data = _five(sw={"drop": [4]})
    with pytest.raises(ValueError) as exc:
        _summarise(data)
    assert run_id("static_weighted") in str(exc.value)
    assert "r000004" in str(exc.value)


def test_a_position_outside_a_repeats_window_is_left_out_of_that_repeat(tmp_path) -> None:
    """Position 5 falls in warmup in repeat 2, for every policy. Every request that is
    measured is a hit, so attainment is 1.0 with one repeat or two. Scoring the unmeasured
    position as a miss would give 0.9 on the two-repeat set."""
    values = [120, 100, 130, 110, 120]
    one = frame(cell_rows("round_robin", values), cell_rows("static_weighted", values))
    two = frame(
        cell_rows("round_robin", values),
        cell_rows("round_robin", values, repeat=2, warmup=[5]),
        cell_rows("static_weighted", values),
        cell_rows("static_weighted", values, repeat=2, warmup=[5]),
    )
    s1 = _summarise(one, tmp_path / "one")
    s2 = _summarise(two, tmp_path / "two")
    for p in ("round_robin", "static_weighted"):
        c1, c2 = s1["points"][0]["policies"][p], s2["points"][0]["policies"][p]
        assert c2["requests"] == 9
        for stat in ("slo_e2e_2x", "slo_e2e_5x", "slo_ttft_2x", "slo_ttft_5x"):
            assert c2[stat]["value"] == c1[stat]["value"], (p, stat)
    assert s2["points"][0]["calibration_gain"]["queue_blind"]["status"] == "defined"


def _five_policies(fail: dict[str, int] | None = None) -> pd.DataFrame:
    fail = fail or {}
    runs = []
    for k, p in enumerate((*H1, "threshold")):
        status = {fail[p]: "timeout"} if p in fail else None
        runs.append(cell_rows(p, _noise(40, 20 + k), status=status))
    return frame(*runs)


def test_a_failure_under_threshold_leaves_the_h1_interaction_alone() -> None:
    base = _summarise(_five_policies())["points"][0]
    hit = _summarise(_five_policies({"threshold": 3}))["points"][0]
    assert hit["h1_status"] == "defined"
    assert hit["h1"] == base["h1"]
    assert hit["calibration_gain"] == base["calibration_gain"]


def test_a_failure_under_wjsq_leaves_the_queue_blind_gain_alone() -> None:
    base = _summarise(_five_policies())["points"][0]
    hit = _summarise(_five_policies({"wjsq": 3}))["points"][0]
    assert hit["calibration_gain"]["queue_blind"] == base["calibration_gain"]["queue_blind"]
    assert hit["calibration_gain"]["queue_aware"]["status"].startswith("undefined")
    assert hit["h1_status"].startswith("undefined")
    assert "wjsq" in hit["h1_status"]
    assert hit["h1"] == {}


# --------------------------------------------------------------- repeats missing for a cell


def _repeats(root, spec: dict[str, dict[int, np.ndarray]]) -> tuple[pd.DataFrame, object]:
    data = frame(*(cell_rows(p, v, repeat=r) for p, reps in spec.items() for r, v in reps.items()))
    return data, manifests_for(root, data)


def test_a_missing_repeat_is_intersected_away_for_value_and_draws(tmp_path) -> None:
    """RoundRobin has repeats 1-3, StaticWeighted 1-2 (its third run died, no directory).
    RoundRobin's repeat 3 is absurdly slow, so any draw that picked it would show."""
    rr = {1: _noise(30, 31), 2: _noise(30, 32), 3: np.full(30, 1e6)}
    sw = {1: _noise(30, 33, loc=900.0), 2: _noise(30, 34, loc=900.0)}
    data, path = _repeats(tmp_path, {"round_robin": rr, "static_weighted": sw})
    pt = cs.summarise(data, BOOT, 5, path)["points"][0]
    gain = pt["calibration_gain"]["queue_blind"]
    expected = np.concatenate([rr[1], rr[2]]).mean() - np.concatenate([sw[1], sw[2]]).mean()
    assert gain["status"] == "defined"
    assert gain["mean"]["gain_ms"] == round(float(expected), 1)
    assert gain["mean"]["gain_ms_ci95"][1] < 1000.0
    assert gain["repeats_used"] == [1, 2]
    assert gain["repeats_dropped"] == {"round_robin": [3]}
    assert gain.get("single_repeat") is not True
    # The per-cell table keeps all of a cell's repeats and flags the mismatch.
    assert pt["policies"]["round_robin"]["runs"] == 3
    assert pt["policies"]["static_weighted"]["runs"] == 2
    assert pt["policies"]["static_weighted"]["runs_differ_at_point"] is True


def test_a_run_that_left_only_a_pre_run_manifest_counts_as_missing(tmp_path) -> None:
    rr = {1: _noise(30, 41), 2: _noise(30, 42), 3: _noise(30, 43)}
    sw = {1: _noise(30, 44), 2: _noise(30, 45)}
    data, path = _repeats(tmp_path, {"round_robin": rr, "static_weighted": sw})
    pre = tmp_path / run_id("static_weighted", repeat=3) / "manifest.pre.json"
    pre.parent.mkdir()
    pre.write_text(json.dumps({"run_id": run_id("static_weighted", repeat=3)}))
    gain = cs.summarise(data, BOOT, 5, path)["points"][0]["calibration_gain"]["queue_blind"]
    assert gain["status"] == "defined"
    assert gain["repeats_used"] == [1, 2]


def test_a_contrast_without_the_short_cell_uses_every_repeat(tmp_path) -> None:
    full = {r: _noise(30, 50 + r) for r in (1, 2, 3)}
    spec = {p: {r: v + 10 * k for r, v in full.items()} for k, p in enumerate(H1)}
    complete_data, complete_path = _repeats(tmp_path / "complete", spec)
    short = {
        **spec,
        "static_weighted": {1: spec["static_weighted"][1], 2: spec["static_weighted"][2]},
    }
    short_data, short_path = _repeats(tmp_path / "short", short)

    complete = cs.summarise(complete_data, BOOT, 5, complete_path)["points"][0]
    missing = cs.summarise(short_data, BOOT, 5, short_path)["points"][0]

    a, b = complete["calibration_gain"]["queue_aware"], missing["calibration_gain"]["queue_aware"]
    assert b["repeats_used"] == [1, 2, 3]
    assert b["mean"]["gain_ms"] == a["mean"]["gain_ms"]
    assert b["mean"]["ratio"] == a["mean"]["ratio"]
    for lo_hi_a, lo_hi_b in zip(a["mean"]["gain_ms_ci95"], b["mean"]["gain_ms_ci95"], strict=True):
        assert lo_hi_b == pytest.approx(lo_hi_a, rel=0.05, abs=1.0)
    assert missing["h1_status"] == "defined"
    assert missing["h1_repeats_used"] == [1, 2]
    assert missing["h1_repeats_dropped"] == {p: [3] for p in H1 if p != "static_weighted"}


def test_an_invalid_run_undefines_its_cells_contrasts(tmp_path) -> None:
    """StaticWeighted's repeat 3 was run and its manifest says valid: false, so the run set
    left it out. That repeat is probably the policy's worst, so it is not averaged away."""
    spec = {p: {r: _noise(30, 60 + r + k) for r in (1, 2, 3)} for k, p in enumerate(H1)}
    spec["static_weighted"].pop(3)
    data, path = _repeats(tmp_path, spec)
    bad = run_id("static_weighted", repeat=3)
    write_manifest(
        tmp_path, bad, policy="static_weighted", validity={"dropped_requests": 4, "valid": False}
    )

    pt = cs.summarise(data, BOOT, 5, path)["points"][0]
    reasons = pt["policies"]["static_weighted"]["invalid_runs"][bad]
    assert any("never returned a response" in r for r in reasons)
    gain = pt["calibration_gain"]["queue_blind"]
    assert gain["status"].startswith("undefined")
    assert "invalid" in gain["status"]
    assert "mean" not in gain
    assert pt["h1_status"].startswith("undefined")
    assert pt["h1"] == {}
    assert pt["calibration_gain"]["queue_aware"]["status"] == "defined"


def test_no_repeat_in_common_is_undefined_not_an_error(tmp_path) -> None:
    data, path = _repeats(
        tmp_path, {"round_robin": {1: _noise(30, 70)}, "static_weighted": {2: _noise(30, 71)}}
    )
    gain = cs.summarise(data, BOOT, 5, path)["points"][0]["calibration_gain"]["queue_blind"]
    assert gain["status"].startswith("undefined")


def test_one_repeat_in_common_is_computed_and_said_to_be_one_arrival_path(tmp_path) -> None:
    data, path = _repeats(
        tmp_path,
        {
            "round_robin": {1: _noise(30, 72), 2: _noise(30, 73)},
            "static_weighted": {2: _noise(30, 74)},
        },
    )
    gain = cs.summarise(data, BOOT, 5, path)["points"][0]["calibration_gain"]["queue_blind"]
    assert gain["status"] == "defined"
    assert gain["repeats_used"] == [2]
    assert gain["single_repeat"] is True


def test_cell_draws_refuses_cells_whose_repeats_differ() -> None:
    """The independent-resampling fallback is gone: handed cells that cannot be paired, the
    draw function fails loudly instead of unpairing them."""
    rows = {r: pd.DataFrame(cell_rows("a", _noise(10, r), repeat=r)) for r in (1, 2)}
    positions = pd.Index(sorted(rows[1]["req_id"]))
    for r in rows.values():
        r["ref_service_ms"] = np.nan
        r["ref_prefill_ms"] = np.nan
    cells = {
        "a": cs.Cell("a", {1: rows[1], 2: rows[2]}, positions),
        "b": cs.Cell("b", {1: rows[1]}, positions),
    }
    with pytest.raises(ValueError):
        cs.cell_draws(cells, 10, 1, np.random.default_rng(0), len(positions))


def test_a_repeat_with_no_row_at_any_position_is_absent_rather_than_a_hole() -> None:
    """A repeat that shares no arrival with the point is a run that measured nothing here,
    not a record with a hole in it. Every position is absent for it, so it contributes
    nothing and refuses nothing."""
    here = pd.DataFrame(cell_rows("jsq", _noise(6, 80)))
    elsewhere = pd.DataFrame(cell_rows("jsq", _noise(6, 81), repeat=2))
    elsewhere["req_id"] = [f"r{i:06d}" for i in range(100, 106)]
    for g in (here, elsewhere):
        g["ref_service_ms"] = 400.0
        g["ref_prefill_ms"] = 20.0
    positions = pd.Index(sorted(here["req_id"]))

    cell = cs.Cell("jsq", {1: here, 2: elsewhere}, positions)

    assert cell.repeats == [1, 2]
    assert cell.present[0].all()
    assert not cell.present[1].any()
    assert not cell.failed[1].any()
    assert np.isnan(cell.e2e[1]).all()
    assert cs.point_values(cell)["mean"] == pytest.approx(here["e2e_ms"].mean())
