"""tools/contrast_check.py: analysis-plan 6.6, the gate G4 holds the simulator to.

Nothing from the simulator is citable until this passes, so the tests are about the verdict
rather than the report: what counts as a miss, what a miss is allowed to be forgiven for
(nothing), and that the exit code a script reads agrees with the verdict printed.
"""

from __future__ import annotations

import json

import contrast_check
import pytest

POLICIES = ("round_robin", "static_weighted", "jsq", "wjsq")


def _policy(mean: float, *, ci: tuple[float, float] | None = None, transient: bool = False) -> dict:
    lo, hi = ci if ci else (mean - 5.0, mean + 5.0)
    return {
        "steady_state": {"transient": transient},
        "mean": {"value": mean, "ci95": [lo, hi]},
        "p50": {"value": mean * 0.9},
        "p95": {"value": mean * 1.4},
    }


def _point(
    means: dict[str, float],
    *,
    lam: float = 2.4,
    stale: float = 0.0,
    workload: str = "",
    ratio: float | None = 0.9,
    ratio_ci: tuple[float, float] = (0.85, 0.95),
    interaction: float | None = 0.05,
    interaction_ci: tuple[float, float] = (0.01, 0.09),
    cis: dict[str, tuple[float, float]] | None = None,
    transient: tuple[str, ...] = (),
) -> dict:
    cis = cis or {}
    point: dict = {
        **({"workload": workload} if workload else {}),
        "lambda_rps": lam,
        "staleness_s": stale,
        "policies": {
            p: _policy(m, ci=cis.get(p), transient=p in transient) for p, m in means.items()
        },
        "h1_status": "defined" if interaction is not None else "undefined: transient cell(s) jsq",
    }
    if ratio is None:
        point["calibration_gain"] = {"queue_aware": {"status": "undefined: transient cell(s) jsq"}}
    else:
        point["calibration_gain"] = {
            "queue_aware": {
                "status": "defined",
                "mean": {"ratio": ratio, "ratio_ci95": list(ratio_ci)},
            }
        }
    point["h1"] = (
        {}
        if interaction is None
        else {
            "mean": {
                "interaction_log": interaction,
                "interaction_log_ci95": list(interaction_ci),
            }
        }
    )
    return point


def _summary(*points: dict) -> dict:
    return {"points": list(points)}


MEANS = {"round_robin": 1200.0, "static_weighted": 1100.0, "jsq": 1000.0, "wjsq": 900.0}


def _agreeing() -> tuple[dict, dict]:
    hw = _summary(_point(MEANS))
    sim = _summary(_point({p: m * 1.05 for p, m in MEANS.items()}, ratio=0.9, interaction=0.05))
    return hw, sim


# ------------------------------------------------------------------------ the verdict


def test_a_simulator_that_reproduces_every_contrast_passes() -> None:
    result = contrast_check.compare(*_agreeing())
    assert result["passes"] is True
    (point,) = result["points"]
    assert point["misses"] == []
    assert point["ranking"]["hardware"] == point["ranking"]["simulator"]
    assert point["wjsq_over_jsq"]["simulator"] == 0.9
    assert point["h1_interaction_log"]["simulator"] == 0.05
    assert point["absolute_error_percent"]["jsq"] == {"p50": 5.0, "p95": 5.0}


def test_a_ranking_swap_is_a_miss_and_says_whether_the_hardware_could_tell_them_apart() -> None:
    """A swap between two policies the hardware cannot separate is still a miss: the rule was
    written that way before any result existed. It is listed apart so a reader can see whether
    the simulator disagreed with the hardware or only with its noise."""
    hw = _summary(_point(MEANS, cis={"jsq": (995.0, 1005.0), "static_weighted": (1000.0, 1200.0)}))
    swapped = {**MEANS, "jsq": 1100.0, "static_weighted": 1000.0}
    sim = _summary(_point(swapped))

    result = contrast_check.compare(hw, sim)
    (point,) = result["points"]
    assert result["passes"] is False
    assert "ranking on mean latency differs" in point["misses"]
    assert point["ranking"]["hardware"] == ["wjsq", "jsq", "static_weighted", "round_robin"]
    assert point["ranking"]["simulator"] == ["wjsq", "static_weighted", "jsq", "round_robin"]
    assert point["ranking"]["swapped_pairs_indistinguishable_on_hardware"] == [
        ["jsq", "static_weighted"]
    ]


def test_a_swap_the_hardware_can_separate_is_not_listed_as_overlapping() -> None:
    # The ratio interval is wide here so the ranking is the only thing that can miss.
    hw = _summary(
        _point(
            MEANS,
            cis={"jsq": (990.0, 1010.0), "static_weighted": (1090.0, 1110.0)},
            ratio_ci=(0.7, 0.99),
        )
    )
    sim = _summary(_point({**MEANS, "jsq": 1100.0, "static_weighted": 1000.0}))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["misses"] == ["ranking on mean latency differs"]
    assert point["ranking"]["swapped_pairs_indistinguishable_on_hardware"] == []


def test_a_transient_hardware_cell_is_left_out_of_the_ranking() -> None:
    """A mean taken off a filling queue is not comparable with anything, so the simulator is
    not asked to reproduce its place in the order."""
    hw = _summary(_point(MEANS, transient=("round_robin",)))
    sim = _summary(_point({**MEANS, "round_robin": 10.0}))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert "round_robin" not in point["ranking"]["hardware"]
    assert point["misses"] == []
    # It is still reported: only the ranking excludes it.
    assert "round_robin" in point["absolute_error_percent"]


def test_a_policy_the_simulator_did_not_run_is_left_out_of_the_ranking() -> None:
    hw = _summary(_point(MEANS))
    sim_means = {p: m for p, m in MEANS.items() if p != "round_robin"}
    (point,) = contrast_check.compare(hw, _summary(_point(sim_means)))["points"]
    assert point["ranking"]["hardware"] == ["wjsq", "jsq", "static_weighted"]
    assert "round_robin" not in point["absolute_error_percent"]


@pytest.mark.parametrize("ratio", [0.85, 0.95, 0.9])
def test_a_ratio_on_the_boundary_of_the_hardware_interval_is_inside_it(ratio) -> None:
    hw = _summary(_point(MEANS, ratio=0.9, ratio_ci=(0.85, 0.95)))
    sim = _summary(_point({**MEANS, "wjsq": 1000.0 * ratio}))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["wjsq_over_jsq"]["simulator"] == ratio
    assert point["misses"] == []


def test_a_ratio_outside_the_hardware_interval_is_a_miss() -> None:
    hw = _summary(_point(MEANS, ratio=0.9, ratio_ci=(0.85, 0.95)))
    sim = _summary(_point({**MEANS, "wjsq": 700.0}))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["misses"] == ["WJSQ over JSQ outside the hardware interval"]


def test_a_simulator_without_both_queue_aware_cells_cannot_answer_the_ratio() -> None:
    hw = _summary(_point(MEANS))
    sim = _summary(_point({p: m for p, m in MEANS.items() if p != "wjsq"}))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["wjsq_over_jsq"]["simulator"] is None
    assert "WJSQ over JSQ outside the hardware interval" in point["misses"]


def test_an_undefined_hardware_ratio_is_not_asked_of_the_simulator() -> None:
    hw = _summary(_point(MEANS, ratio=None, interaction=None))
    sim = _summary(_point(MEANS))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert "wjsq_over_jsq" not in point
    assert "h1_interaction_log" not in point
    assert point["misses"] == []


def test_a_simulator_that_cannot_define_h1_where_the_hardware_can_is_a_miss() -> None:
    hw = _summary(_point(MEANS, interaction=0.05, interaction_ci=(0.01, 0.09)))
    sim = _summary(_point(MEANS, interaction=None))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["h1_interaction_log"]["simulator"] is None
    assert point["misses"] == [
        "the simulator cannot define the H1 interaction (undefined: transient cell(s) jsq)"
    ]


def test_an_h1_interaction_outside_the_hardware_interval_is_a_miss() -> None:
    hw = _summary(_point(MEANS, interaction=0.05, interaction_ci=(0.01, 0.09)))
    sim = _summary(_point(MEANS, interaction=0.5))
    (point,) = contrast_check.compare(hw, sim)["points"]
    assert point["misses"] == ["H1 interaction outside the hardware interval"]


def test_a_point_the_simulator_never_ran_is_a_miss_and_nothing_else() -> None:
    hw = _summary(_point(MEANS, lam=1.2), _point(MEANS, lam=2.4))
    sim = _summary(_point(MEANS, lam=2.4))
    result = contrast_check.compare(hw, sim)
    missing, ran = result["points"]
    assert missing["misses"] == ["the simulator has no run at this point"]
    assert "ranking" not in missing
    assert ran["misses"] == []
    assert result["passes"] is False


def test_points_are_matched_on_workload_staleness_and_rate() -> None:
    """Two shapes at one rate are two points, and a staleness arm is another. Matching on the
    rate alone would compare the simulator's generation run against the hardware's
    summarisation one."""
    hw = _summary(
        _point(MEANS, workload="generation"),
        _point(MEANS, workload="summarisation"),
        _point(MEANS, stale=5.0),
    )
    sim = _summary(
        _point({p: m * 1.01 for p, m in MEANS.items()}, workload="summarisation"),
        _point({p: m * 1.01 for p, m in MEANS.items()}, stale=5.0),
        _point({p: m * 1.01 for p, m in MEANS.items()}, workload="generation", lam=2.4004),
    )
    result = contrast_check.compare(hw, sim)
    assert [p.get("workload", "") for p in result["points"]] == [
        "generation",
        "summarisation",
        "",
    ]
    assert result["passes"] is True


# --------------------------------------------------------------------------- the report


def test_the_exit_code_is_two_on_any_miss_and_zero_on_none(tmp_path, capsys) -> None:
    hw, sim = _agreeing()
    paths = {}
    for name, summary in (("hw", hw), ("sim", sim)):
        paths[name] = tmp_path / f"{name}.json"
        paths[name].write_text(json.dumps(summary))
    out = tmp_path / "report" / "contrast_check"
    argv = ["--hardware", str(paths["hw"]), "--simulator", str(paths["sim"])]

    assert contrast_check.main([*argv, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("**PASS**")
    assert json.loads(out.with_suffix(".json").read_text())["passes"] is True
    assert out.with_suffix(".md").read_text().strip() == printed.strip()

    paths["sim"].write_text(json.dumps(_summary(_point({**MEANS, "wjsq": 5000.0}))))
    assert contrast_check.main(argv) == 2
    assert capsys.readouterr().out.startswith("**FAIL**")


def test_the_report_names_every_contrast_it_checked(tmp_path) -> None:
    hw = _summary(
        _point(
            MEANS,
            workload="generation",
            cis={"jsq": (995.0, 1005.0), "static_weighted": (1000.0, 1200.0)},
        )
    )
    sim = _summary(
        _point({**MEANS, "jsq": 1100.0, "static_weighted": 1000.0}, workload="generation")
    )
    text = contrast_check.markdown(contrast_check.compare(hw, sim))
    assert "**FAIL**" in text
    assert "### generation, 2.4 req/s, staleness 0 s" in text
    assert "- Ranking, hardware: wjsq < jsq < static_weighted < round_robin" in text
    assert "- Swapped pairs whose hardware intervals overlap: jsq/static_weighted" in text
    assert "- WJSQ/JSQ: hardware 0.9 [0.85, 0.95], simulator" in text
    assert "- H1 interaction, log: hardware 0.05 [0.01, 0.09], simulator 0.05" in text
    assert "- Misses: ranking on mean latency differs" in text


def test_the_report_of_a_point_the_simulator_never_ran_has_nothing_to_compare() -> None:
    hw = _summary(_point(MEANS))
    text = contrast_check.markdown(contrast_check.compare(hw, _summary()))
    assert "### 2.4 req/s, staleness 0 s" in text
    assert "- Ranking" not in text
    assert "- WJSQ/JSQ" not in text
    assert "- H1 interaction" not in text
    assert "- Misses: the simulator has no run at this point" in text


def test_a_clean_report_says_none_missed(tmp_path) -> None:
    text = contrast_check.markdown(contrast_check.compare(*_agreeing()))
    assert "**PASS**" in text
    assert "### 2.4 req/s, staleness 0 s" in text
    assert "- Misses: none" in text
