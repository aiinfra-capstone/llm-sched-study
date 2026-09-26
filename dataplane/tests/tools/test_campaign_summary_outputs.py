"""tools/campaign_summary.py: the H1 contrast, the calibration gain and the per-policy numbers.

Every figure in the writing brief is read off this output, so each number is checked on an
input small enough to compute by hand.
"""

from __future__ import annotations

import json
import math

import campaign_summary as cs
import numpy as np
import pandas as pd
import pool_load
import pytest
from support import FIRST_PAIR_SNAPSHOTS, REPO_ROOT, cell_rows, frame, manifests_for, run_id

H1 = ("round_robin", "static_weighted", "jsq", "wjsq")


def _flat(value: float, n: int = 30) -> list[float]:
    return [float(value)] * n


def _h1_frame(values: dict[str, float], **kw) -> pd.DataFrame:
    return frame(*(cell_rows(p, _flat(v), **kw) for p, v in values.items()))


def _point(data: pd.DataFrame, root=None, n_boot: int = 200) -> dict:
    path = manifests_for(root, data) if root is not None else None
    return cs.summarise(data, n_boot, 7, path)["points"][0]


def _ramp(n: int = 30) -> list[float]:
    return list(np.linspace(1000.0, 2000.0, n))


# ------------------------------------------------------------------------------ H1


def test_a_missing_2x2_cell_leaves_h1_empty_and_names_it() -> None:
    pt = _point(_h1_frame({"round_robin": 200, "static_weighted": 100, "jsq": 100}))
    assert pt["h1"] == {}
    assert pt["h1_status"] == "undefined: no valid run for wjsq"


def test_a_transient_2x2_cell_leaves_h1_empty_and_names_it() -> None:
    data = frame(
        cell_rows("round_robin", _flat(200)),
        cell_rows("static_weighted", _flat(100)),
        cell_rows("jsq", _ramp()),
        cell_rows("wjsq", _flat(50)),
    )
    pt = _point(data)
    assert pt["policies"]["jsq"]["steady_state"]["transient"] is True
    assert pt["h1"] == {}
    assert pt["h1_status"] == "undefined: transient cell(s) jsq"


def test_equal_ratios_give_zero_log_interaction_and_a_positive_ms_one() -> None:
    """The C2 scale case. Calibration halves latency with and without queue depth, so on the
    log scale nothing interacts. In ms the queue-blind gain is 100 and the queue-aware gain
    50, so the ms interaction reads +50 from scale alone."""
    pt = _point(_h1_frame({"round_robin": 200, "static_weighted": 100, "jsq": 100, "wjsq": 50}))
    h = pt["h1"]["mean"]
    assert h["interaction_log"] == 0.0
    assert h["interaction_log_ci95"] == [0.0, 0.0]
    assert h["interaction"] == 50.0
    assert h["ci95"] == [50.0, 50.0]
    assert h["excludes_zero"] is True
    assert h["calibration_gain_queue_blind"] == 100.0
    assert h["calibration_gain_queue_aware"] == 50.0
    assert h["ratio_static_weighted_over_round_robin"] == 0.5
    assert h["ratio_wjsq_over_jsq"] == 0.5


def test_calibration_buying_less_once_queue_aware_is_positive_on_both_scales() -> None:
    pt = _point(_h1_frame({"round_robin": 200, "static_weighted": 100, "jsq": 100, "wjsq": 90}))
    h = pt["h1"]["mean"]
    assert h["interaction"] == 90.0
    assert h["interaction_log"] == round(math.log(0.9) - math.log(0.5), 4)
    assert h["interaction"] > 0 and h["interaction_log"] > 0


def test_calibration_gain_reports_a_status_when_it_cannot_be_computed() -> None:
    missing = _point(_h1_frame({"round_robin": 200, "jsq": 100, "wjsq": 90}))
    assert missing["calibration_gain"]["queue_blind"] == {
        "status": "undefined: no valid run for static_weighted"
    }
    data = frame(
        cell_rows("round_robin", _flat(200)),
        cell_rows("static_weighted", _ramp()),
        cell_rows("jsq", _flat(100)),
        cell_rows("wjsq", _flat(90)),
    )
    moving = _point(data)
    assert moving["calibration_gain"]["queue_blind"] == {
        "status": "undefined: transient cell(s) static_weighted"
    }
    aware = moving["calibration_gain"]["queue_aware"]
    assert aware["status"] == "defined"
    assert aware["mean"]["gain_ms"] == 10.0
    assert aware["mean"]["ratio"] == 0.9
    assert aware["mean"]["gain_share"] == 0.1
    assert aware["p95"]["gain_ms"] == 10.0


def test_two_staleness_values_at_one_rate_are_two_points() -> None:
    data = frame(
        *(cell_rows(p, _flat(100), staleness=0.0) for p in ("jsq", "wjsq")),
        *(cell_rows(p, _flat(120), staleness=5.0) for p in ("jsq", "wjsq")),
    )
    points = cs.summarise(data, 50, 1)["points"]
    assert [(pt["lambda_rps"], pt["staleness_s"]) for pt in points] == [(2.0, 0.0), (2.0, 5.0)]
    assert points[1]["policies"]["jsq"]["mean"]["value"] == 120.0


def test_the_queue_aware_gain_trend_runs_from_lightest_to_heaviest_fresh_point() -> None:
    data = frame(
        cell_rows("jsq", _flat(300), lam=1.0),
        cell_rows("wjsq", _flat(200), lam=1.0),
        cell_rows("jsq", _flat(400), lam=3.0),
        cell_rows("wjsq", _flat(350), lam=3.0),
        cell_rows("jsq", _flat(900), lam=3.0, staleness=5.0),
        cell_rows("wjsq", _flat(100), lam=3.0, staleness=5.0),
    )
    t = cs.summarise(data, 100, 1)["load_trend_queue_aware"]
    assert t["lambdas"] == [1.0, 3.0]
    assert t["queue_aware_gain_ms"] == [100.0, 50.0]
    assert t["change_ms"] == -50.0
    assert t["change_ms_ci95"] == [-50.0, -50.0]
    assert t["wjsq_over_jsq"] == [round(200 / 300, 4), 0.875]
    assert t["change_in_ratio"] == round(0.875 / (200 / 300), 4)


def test_one_steady_point_has_no_trend() -> None:
    data = frame(cell_rows("jsq", _flat(300)), cell_rows("wjsq", _flat(200)))
    assert cs.summarise(data, 50, 1)["load_trend_queue_aware"] is None


# ----------------------------------------------------------------------- per policy


def test_ttft_is_queue_wait_plus_prefill_and_rows_without_prefill_are_left_out() -> None:
    rows = cell_rows(
        "jsq", [100, 100, 100, 100, 100], queue_wait_ms=10.0, prefill_ms=[20, 30, None, 40, 50]
    )
    cell = _point(frame(rows))["policies"]["jsq"]
    assert cell["ttft_mean"]["value"] == 45.0


def test_a_cell_with_no_phase_split_reports_ttft_as_not_available() -> None:
    """An engine that does not expose prefill (f18_status partial) leaves every TTFT
    undefined. The summary reports that cell's TTFT as missing rather than failing."""
    rows = cell_rows("jsq", _flat(100, 6), prefill_ms=None, decode_ms=None)
    cell = _point(frame(rows))["policies"]["jsq"]
    assert cell["ttft_mean"]["value"] is None
    assert cell["ttft_p95"]["value"] is None
    assert cell["mean"]["value"] == 100.0


def test_tpot_divides_decode_time_by_output_tokens_minus_one() -> None:
    rows = cell_rows("jsq", _flat(100, 4), decode_ms=[630, 630, 50, 50], output_len=[64, 64, 1, 1])
    # 630/63 = 10 on two rows; a one-token output divides by 1, not 0: 50 on two rows.
    assert _point(frame(rows))["policies"]["jsq"]["tpot_mean"]["value"] == 30.0


def test_slo_references_are_the_fastest_nodes_one_slot_cell(tmp_path) -> None:
    """For p128_o64 the 3050's one-slot cell is 391.059 ms service and 20.363 ms prefill; the
    1650 Ti's is 615.671 and 174.212. Every value below is on the side of a threshold that
    only the 3050's cell puts it on."""
    rows = cell_rows(
        "jsq",
        [1000, 700, 2000, 1500, 1000],
        queue_wait_ms=0.0,
        prefill_ms=[30, 45, 90, 110, 30],
    )
    cell = _point(frame(rows), tmp_path)["policies"]["jsq"]
    assert cell["slo_e2e_2x"]["value"] == 0.2
    assert cell["slo_e2e_5x"]["value"] == 0.8
    assert cell["slo_ttft_2x"]["value"] == 0.4
    assert cell["slo_ttft_5x"]["value"] == 0.8


def test_failures_count_warmup_losses_and_responses_served_but_never_delivered() -> None:
    rows = cell_rows(
        "jsq",
        _flat(100, 8),
        warmup=[1, 2],
        status={1: "timeout", 6: "engine_error"},
        service_ms=[651, 651, 651, 651, 651, None, 651, 651],
    )
    s = cs.summarise(frame(rows), 50, 1)
    assert s["failures"] == {
        run_id("jsq"): {
            "not_ok": 2,
            "in_warmup": 1,
            "served_by_worker_but_not_delivered": 1,
            "statuses": {"timeout": 1, "engine_error": 1},
        }
    }


def _c4_cell(snapshot_id: str) -> dict:
    snap = pool_load.snapshot_index()[snapshot_id]
    return next(
        e
        for e in snap["entries"]
        if e["concurrency"] == 4
        and e["prompt_bucket"][0] <= 128 <= e["prompt_bucket"][1]
        and e["output_bucket"][0] <= 64 <= e["output_bucket"][1]
    )


def test_utilisation_on_a_hand_computed_pool(tmp_path) -> None:
    """Eleven requests over positions 1..11, so the window spans 10 s. Odd positions go to
    the 3050 (6 requests), even ones to the 1650 Ti (5), each 651 ms of service."""
    data = frame(cell_rows("round_robin", _flat(700, 11), lam=0.5))
    u = _point(data, tmp_path)["utilisation"]

    cap = {
        n: 4 / (_c4_cell(FIRST_PAIR_SNAPSHOTS[n])["service_ms_mean"] / 1000)
        for n in FIRST_PAIR_SNAPSHOTS
    }
    assert u["nodes"]["gtx1650ti"]["capacity_rps"] == round(cap["gtx1650ti"], 4)
    assert u["nodes"]["rtx3050"]["capacity_rps"] == round(cap["rtx3050"], 4)
    assert u["slow_node"] == "gtx1650ti" and u["fast_node"] == "rtx3050"
    assert u["pool_capacity_rps"] == round(sum(round(c, 4) for c in cap.values()), 4)
    assert u["pool_utilisation"] == round(0.5 / sum(round(c, 4) for c in cap.values()), 4)
    assert u["slow_node_utilisation_at_half_load"] == round(0.25 / round(cap["gtx1650ti"], 4), 4)

    cell = u["per_cell"]["round_robin"]
    assert cell["rtx3050"]["arrival_rps"] == 0.6
    assert cell["gtx1650ti"]["arrival_rps"] == 0.5
    assert cell["rtx3050"]["busy_slots"] == round(6 * 0.651 / 10, 4)
    assert cell["rtx3050"]["measured_utilisation"] == round(round(6 * 0.651 / 10, 4) / 4, 4)
    assert cell["rtx3050"]["offered_over_capacity"] == round(0.6 / round(cap["rtx3050"], 4), 4)
    assert cell["rtx3050"]["service_ms_mean"] == 651.0
    assert cell["rtx3050"]["transport_residual_ms_mean"] == 5.0


def test_a_node_that_got_no_requests_has_no_service_mean(tmp_path) -> None:
    data = frame(cell_rows("round_robin", _flat(700, 11), chosen_node="rtx3050"))
    cell = _point(data, tmp_path)["utilisation"]["per_cell"]["round_robin"]
    assert cell["gtx1650ti"]["arrival_rps"] == 0.0
    assert cell["gtx1650ti"]["service_ms_mean"] is None
    assert cell["gtx1650ti"]["transport_residual_ms_mean"] is None


def test_operating_r_is_slow_over_fast_per_bucket_with_ten_of_each() -> None:
    n = 24
    rows = pd.DataFrame(
        {
            "bucket_id": ["p128_o64"] * n + ["p512_o128"] * 4,
            "chosen_node": ["fast", "slow"] * (n // 2) + ["fast", "slow"] * 2,
            "service_ms": [100.0, 250.0] * (n // 2) + [1.0, 1.0] * 2,
            "prefill_ms": [10.0, 50.0] * (n // 2) + [1.0, 1.0] * 2,
            "decode_ms": [80.0, 160.0] * (n // 2) + [1.0, 1.0] * 2,
        }
    )
    out = cs.operating_r(rows, "fast", "slow")
    assert out["buckets"] == {
        "p128_o64": {
            "n_fast": 12,
            "n_slow": 12,
            "R_service": 2.5,
            "R_prefill": 5.0,
            "R_decode": 2.0,
        }
    }
    assert set(out["pooled"]) == {"R_service", "R_prefill", "R_decode"}
    assert cs.operating_r(rows[rows["chosen_node"] == "fast"], "fast", "slow") == {"buckets": {}}


def test_routing_error_is_not_reported_anywhere(tmp_path) -> None:
    data = _h1_frame({"round_robin": 200, "static_weighted": 100, "jsq": 100, "wjsq": 90})
    data["routing_error_ms"] = 3.0
    text = json.dumps(cs.summarise(data, 50, 1, manifests_for(tmp_path, data)))
    assert "routing_error" not in text


def test_seeds_and_trace_hashes_say_whether_repeats_were_independent(tmp_path) -> None:
    data = frame(cell_rows("jsq", _flat(100), repeat=1), cell_rows("jsq", _flat(100), repeat=2))
    for r, g in ((1, 11), (2, 12)):
        from support import write_manifest

        write_manifest(tmp_path, run_id("jsq", repeat=r), policy="jsq", gen_seed=g, seed=100 + r)
    s = cs.summarise(data, 50, 1, tmp_path / "runset.parquet")
    assert s["arrivals_independent"] is True
    assert s["scheduler_seed_varied"] is True
    assert s["gen_seeds"] == [11, 12]
    assert s["scheduler_seeds"] == [101, 102]


def test_an_empty_cell_raises_no_runtime_warning(tmp_path) -> None:
    """A run with no phase times has no TTFT or TPOT, and a flat latency series has no lag-1
    autocorrelation. Those are NaN on purpose and must not arrive as "Mean of empty slice"."""
    import warnings

    data = frame(
        *(
            cell_rows(p, _flat(v), prefill_ms=math.nan, decode_ms=math.nan)
            for p, v in {"round_robin": 200, "static_weighted": 100, "jsq": 100, "wjsq": 90}.items()
        ),
        cell_rows("threshold", _flat(150, 2)),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        pt = cs.summarise(data, 50, 1, manifests_for(tmp_path, data))["points"][0]
    jsq = pt["policies"]["jsq"]
    assert jsq["lag1_autocorrelation"] is None
    assert jsq["ttft_mean"]["value"] is None


def test_one_arrival_path_and_one_scheduler_seed_are_flagged(tmp_path) -> None:
    """The first pair's shape, built here: every repeat replayed one trace with one scheduler
    seed, so repeats sample hardware jitter and nothing else, and the summary says so."""
    from support import write_manifest

    data = frame(cell_rows("jsq", _flat(100), repeat=1), cell_rows("jsq", _flat(100), repeat=2))
    data["trace_sha256"] = "a" * 64
    for r in (1, 2):
        write_manifest(tmp_path, run_id("jsq", repeat=r), policy="jsq", gen_seed=7, seed=42)
    s = cs.summarise(data, 50, 1, tmp_path / "runset.parquet")
    assert s["arrivals_independent"] is False
    assert s["scheduler_seed_varied"] is False
    assert s["gen_seeds"] == [7] and s["scheduler_seeds"] == [42]


# -------------------------------------------------------------- the committed campaign

MPR2 = REPO_ROOT / "runs" / "exp" / "mpr2_1650ti_3050"


@pytest.fixture(scope="module")
def mpr2_summary() -> dict:
    if not (MPR2 / "runset.parquet").is_file():
        pytest.skip(
            f"lab-machine data check: {MPR2}/runset.parquet is gitignored and not on this "
            "machine; test_one_arrival_path_and_one_scheduler_seed_are_flagged covers the rule"
        )
    return cs.summarise(
        pd.read_parquet(MPR2 / "runset.parquet"), 100, 20260915, MPR2 / "runset.parquet"
    )


def test_the_first_pair_replayed_one_arrival_path_with_one_scheduler_seed(mpr2_summary) -> None:
    assert mpr2_summary["arrivals_independent"] is False
    assert mpr2_summary["scheduler_seed_varied"] is False


def test_the_first_pairs_means_did_not_move_only_their_intervals(mpr2_summary) -> None:
    old_path = MPR2 / "summary_iid_v1.json"
    if not old_path.is_file():
        pytest.skip(f"{old_path} is gitignored and not on this machine")
    old = json.loads(old_path.read_text())
    new = {pt["lambda_rps"]: pt for pt in mpr2_summary["points"] if pt["staleness_s"] == 0.0}
    compared = 0
    for pt in old["points"]:
        for policy, cell in pt["policies"].items():
            assert new[pt["lambda_rps"]]["policies"][policy]["mean"]["value"] == pytest.approx(
                cell["mean"]["value"], abs=0.05
            ), (pt["lambda_rps"], policy)
            compared += 1
    assert compared == 15
