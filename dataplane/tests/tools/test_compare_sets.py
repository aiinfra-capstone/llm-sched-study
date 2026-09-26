"""tools/compare_sets.py: one contrast measured in several run sets, compared across them.

Decision rules 6.4 and 6.5 are answered here and nowhere else, so what matters is that the
number this prints for one set is the number that set's own summary reports, that two
workloads inside one campaign are two sets rather than one, and that a set it cannot measure
leaves the pair undefined instead of quietly dropping out of the comparison.
"""

from __future__ import annotations

import json

import campaign_summary as cs
import compare_sets
import numpy as np
import pandas as pd
import pytest
from support import cell_rows, frame, manifests_for, run_id

BOOT = 200
SEED = 6


def _noise(n: int, seed: int, loc: float, sd: float = 100.0) -> np.ndarray:
    return np.random.default_rng(seed).normal(loc, sd, n).clip(50.0)


def _runs(
    workload: str,
    *,
    point: str = "u30",
    lam: float = 2.4,
    jsq: float = 1000.0,
    wjsq: float = 900.0,
    seed: int = 1,
    staleness: float = 0.0,
    sd: float = 100.0,
    repeats: tuple[int, ...] = (1, 2),
) -> list[list[dict]]:
    out = []
    for k, (policy, base) in enumerate((("jsq", jsq), ("wjsq", wjsq))):
        for repeat in repeats:
            out.append(
                cell_rows(
                    policy,
                    _noise(40, seed + k + 10 * repeat, base, sd),
                    repeat=repeat,
                    lam=lam,
                    staleness=staleness,
                    workload=workload,
                    point=point,
                )
            )
    return out


def _runset(root, *runs: list[dict], load_target=None):
    """A run set on disk: the parquet the tool reads and the manifests beside it."""
    data = frame(*runs)
    path = manifests_for(root, data, **({"load_target": load_target} if load_target else {}))
    data.to_parquet(path)
    return path


def _measure(path, workload="", point="u30", label="set", staleness=0.0):
    return compare_sets.measure(label, path, workload, point, staleness, "jsq", "wjsq", BOOT, SEED)


# ------------------------------------------------------------------ one set's ratio


def test_the_ratio_for_a_set_is_the_ratio_its_own_summary_reports(tmp_path) -> None:
    """The cells, the steady-state gate and the pairing are `campaign_summary`'s own, so a
    reader comparing this report against a set's summary.json sees the same point estimate."""
    runs = _runs("", seed=3)
    path = _runset(tmp_path, *runs)
    out, draws = _measure(path)

    summary = cs.summarise(pd.read_parquet(path), BOOT, SEED, path)
    (point,) = summary["points"]
    assert out["status"] == "defined"
    assert out["mean_ratio"] == point["calibration_gain"]["queue_aware"]["mean"]["ratio"]
    assert out["repeats_used"] == [1, 2]
    assert out["lambda_rps"] == 2.4
    assert draws is not None and draws.size == BOOT
    assert out["mean_ratio_ci95"][0] <= out["mean_ratio"] <= out["mean_ratio_ci95"][1]
    assert set(out["secondary"]) == {"jsq", "wjsq"}


def test_a_point_that_holds_two_rates_is_refused_rather_than_averaged(tmp_path) -> None:
    """One operating point is one rate. Two rates under one name is a campaign that changed
    its load without changing the name, and the mean of the two answers nothing."""
    path = _runset(tmp_path, *_runs("", lam=2.4, seed=4), *_runs("", lam=3.6, seed=5))
    out, draws = _measure(path)
    assert out["status"].startswith("undefined: point 'u30' holds several rates")
    assert "2.4" in out["status"] and "3.6" in out["status"]
    assert draws is None
    assert "mean_ratio" not in out


def test_a_point_nobody_ran_is_undefined_and_says_which_one(tmp_path) -> None:
    path = _runset(tmp_path, *_runs("", point="u30", seed=6))
    out, draws = _measure(path, point="u40")
    assert out["status"] == "undefined: no run at point 'u40' and staleness 0"
    assert draws is None
    out, _ = _measure(path, workload="generation")
    assert out["status"].endswith("for workload 'generation'")


def test_a_hash_selects_one_workloads_runs_from_a_campaign_that_ran_several(tmp_path) -> None:
    """Two workloads in one run set are two sets here. Selecting on the path alone would put
    both shapes' runs in one cell and average conditions the campaign kept apart."""
    path = _runset(
        tmp_path,
        *_runs("generation", jsq=900.0, wjsq=810.0, seed=7, sd=10.0),
        *_runs("summarisation", jsq=1800.0, wjsq=1440.0, seed=8, sd=10.0),
    )
    gen, gen_draws = _measure(path, workload="generation", label="generation")
    summ, summ_draws = _measure(path, workload="summarisation", label="summarisation")

    assert gen["workload"] == "generation"
    assert gen["mean_ratio"] == pytest.approx(0.9, abs=0.02)
    assert summ["mean_ratio"] == pytest.approx(0.8, abs=0.02)
    assert gen["repeats_used"] == summ["repeats_used"] == [1, 2]
    assert not np.array_equal(gen_draws, summ_draws)


def test_a_transient_cell_leaves_the_set_undefined(tmp_path) -> None:
    climbing = [
        cell_rows("wjsq", list(np.linspace(900.0, 2000.0, 40)), repeat=r, lam=2.4, point="u30")
        for r in (1, 2)
    ]
    steady = [
        cell_rows("jsq", _noise(40, 9 + r, 1000.0), repeat=r, lam=2.4, point="u30") for r in (1, 2)
    ]
    path = _runset(tmp_path, *steady, *climbing)
    out, draws = _measure(path)
    assert out["status"].startswith("undefined: transient cell(s)")
    assert "wjsq" in out["status"]
    assert draws is None


# ----------------------------------------------------------------------- the tie rate


def _decisions(root, rid: str, records: list[list | None]) -> None:
    """A scheduler log beside a run. Each record is a list of admissible scores, or None for
    a line that is not a decision."""
    lines = []
    for rec in records:
        if rec is None:
            lines.append({"type": "completion", "req_id": "r1"})
            continue
        lines.append(
            {
                "type": "decision",
                "candidates": [
                    {"node_id": f"n{i}", "admissible": True, "score": s} for i, s in enumerate(rec)
                ],
            }
        )
    (root / rid).mkdir(parents=True, exist_ok=True)
    (root / rid / f"scheduler_{rid}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )


def test_a_shared_best_score_counts_once_per_decision(tmp_path) -> None:
    """The tie rate is the share of decisions whose best score was shared, so a three-way tie
    is one tied decision and not two. A decision with one admissible node is not a choice."""
    rid = "run_a"
    _decisions(
        tmp_path,
        rid,
        [
            [1.0, 1.0],  # tied
            [1.0, 1.0, 1.0],  # tied once, not twice
            [1.0, 2.0],  # not tied
            [5.0],  # one candidate: not a choice
            None,  # not a decision
            [1.0, 1.0 + compare_sets.TIE_EPS / 2],  # within the epsilon: tied
        ],
    )
    assert compare_sets.tie_rate(tmp_path, [rid]) == {"decisions": 4, "tie_rate": 0.75}


def test_a_run_with_no_scheduler_log_has_no_tie_rate(tmp_path) -> None:
    (tmp_path / "run_b").mkdir()
    assert compare_sets.tie_rate(tmp_path, ["run_b"]) == {"decisions": 0, "tie_rate": None}


def test_an_inadmissible_or_unscored_candidate_is_not_a_choice(tmp_path) -> None:
    rid = "run_c"
    (tmp_path / rid).mkdir(parents=True)
    (tmp_path / rid / f"scheduler_{rid}.jsonl").write_text(
        "\n".join(
            json.dumps(rec)
            for rec in (
                {
                    "type": "decision",
                    "candidates": [
                        {"node_id": "n0", "admissible": True, "score": 1.0},
                        {"node_id": "n1", "admissible": False, "score": 1.0},
                    ],
                },
                {
                    "type": "decision",
                    "candidates": [
                        {"node_id": "n0", "admissible": True, "score": 2.0},
                        {"node_id": "n1", "admissible": True, "score": None},
                    ],
                },
            )
        )
    )
    assert compare_sets.tie_rate(tmp_path, [rid])["decisions"] == 0


def test_the_tie_rate_travels_with_the_measurement(tmp_path) -> None:
    runs = _runs("", seed=11)
    path = _runset(tmp_path, *runs)
    for policy in ("jsq", "wjsq"):
        for repeat in (1, 2):
            _decisions(tmp_path, run_id(policy, "u30", repeat), [[1.0, 1.0], [1.0, 2.0]])
    out, _ = _measure(path)
    assert out["tie"]["jsq"] == {"decisions": 4, "tie_rate": 0.5}
    assert out["tie"]["wjsq"] == {"decisions": 4, "tie_rate": 0.5}


# ------------------------------------------------------------- comparing sets with main


def _write(tmp_path, name: str, *runs, **kw):
    return _runset(tmp_path / name, *runs, **kw)


def _main(argv: list[str]) -> int:
    return compare_sets.main([*argv, "--draws", str(BOOT), "--seed", str(SEED)])


def test_two_sets_are_compared_draw_by_draw_and_the_report_is_written(tmp_path, capsys) -> None:
    anchor = _write(
        tmp_path,
        "anchor",
        *_runs("", jsq=1000.0, wjsq=900.0, seed=12),
        load_target={"pool_utilisation": 0.3},
    )
    heavy = _write(tmp_path, "heavy", *_runs("", jsq=1000.0, wjsq=500.0, seed=13))
    out = tmp_path / "report" / "compare"

    assert (
        _main(
            [
                "--set",
                f"anchor={anchor}",
                "--set",
                f"heavy={heavy}",
                "--point",
                "u30",
                "--out",
                str(out),
            ]
        )
        == 0
    )

    report = json.loads(out.with_suffix(".json").read_text())
    assert report["contrast"] == "wjsq over jsq, mean end-to-end latency"
    assert [s["set"] for s in report["sets"]] == ["anchor", "heavy"]
    assert report["sets"][0]["load_target"] == [{"pool_utilisation": 0.3}]
    (pair,) = report["pairs"]
    assert (pair["from"], pair["to"]) == ("anchor", "heavy")
    assert pair["status"] == "defined"
    assert pair["difference"] == pytest.approx(
        report["sets"][1]["mean_ratio"] - report["sets"][0]["mean_ratio"]
    )
    assert pair["separated"] is True
    assert report["ordering"] is None

    text = capsys.readouterr().out
    assert "wjsq/jsq on mean latency at point `u30`, staleness 0 s." in text
    assert "| anchor | 2.4 | defined |" in text
    assert out.with_suffix(".md").read_text().strip() == text.strip()


def test_a_set_with_no_defined_ratio_leaves_the_pair_undefined(tmp_path, capsys) -> None:
    """The pair is reported as undefined rather than left out, so a reader sees that the
    comparison was attempted and why it could not be made."""
    good = _write(tmp_path, "good", *_runs("", seed=14))
    empty = _write(tmp_path, "empty", *_runs("", point="u40", seed=15))
    assert _main(["--set", f"a={good}", "--set", f"b={empty}", "--point", "u30"]) == 0
    text = capsys.readouterr().out
    assert "| a | b | undefined: a set has no defined ratio at this point | |" in text
    assert "no run at point 'u30'" in text


def test_ordered_reports_a_monotone_run_with_separated_extremes(tmp_path) -> None:
    """6.5 asks whether the ordering across shapes holds, not only whether each pair differs.
    The extremes are the first and last set named, in the order the hypothesis predicts."""
    sets = {
        "generation": _write(tmp_path, "gen", *_runs("", jsq=1000.0, wjsq=980.0, seed=16)),
        "balanced": _write(tmp_path, "bal", *_runs("", jsq=1000.0, wjsq=850.0, seed=17)),
        "summarisation": _write(tmp_path, "sum", *_runs("", jsq=1000.0, wjsq=700.0, seed=18)),
    }
    out = tmp_path / "ordered"
    argv = [arg for label, path in sets.items() for arg in ("--set", f"{label}={path}")]
    assert _main([*argv, "--point", "u30", "--ordered", "decreasing", "--out", str(out)]) == 0

    ordering = json.loads(out.with_suffix(".json").read_text())["ordering"]
    assert ordering["order"] == ["generation", "balanced", "summarisation"]
    assert ordering["values"] == sorted(ordering["values"], reverse=True)
    assert ordering["monotone"] is True
    assert ordering["extremes_separated"] is True
    assert (
        "Ordering generation -> balanced -> summarisation (decreasing): monotone yes, "
        "extremes separated yes." in out.with_suffix(".md").read_text()
    )


def test_ordered_says_no_when_the_middle_set_breaks_the_run(tmp_path, capsys) -> None:
    sets = {
        "generation": _write(tmp_path, "gen", *_runs("", jsq=1000.0, wjsq=980.0, seed=19)),
        "balanced": _write(tmp_path, "bal", *_runs("", jsq=1000.0, wjsq=600.0, seed=20)),
        "summarisation": _write(tmp_path, "sum", *_runs("", jsq=1000.0, wjsq=850.0, seed=21)),
    }
    argv = [arg for label, path in sets.items() for arg in ("--set", f"{label}={path}")]
    assert _main([*argv, "--point", "u30", "--ordered", "decreasing"]) == 0
    assert "monotone no, extremes separated yes" in capsys.readouterr().out


def test_ordered_with_an_undefined_set_is_neither_monotone_nor_separated(tmp_path, capsys) -> None:
    good = _write(tmp_path, "good", *_runs("", seed=22))
    empty = _write(tmp_path, "empty", *_runs("", point="u40", seed=23))
    argv = ["--set", f"a={good}", "--set", f"b={empty}", "--point", "u30"]
    assert _main([*argv, "--ordered", "increasing"]) == 0
    assert "monotone no, extremes separated no" in capsys.readouterr().out


def _shapes(tmp_path, wjsq: tuple[float, float, float]) -> list[str]:
    names = ("generation", "balanced", "summarisation")
    argv = []
    for k, (name, arm) in enumerate(zip(names, wjsq, strict=True)):
        path = _write(tmp_path, name, *_runs("", jsq=1000.0, wjsq=arm, seed=40 + k))
        argv += ["--set", f"{name}={path}"]
    return argv


def test_a_reversed_ordering_is_not_monotone(tmp_path, capsys) -> None:
    """The hypothesis states a direction before the data. Values that rise across sets the
    hypothesis says should fall are the opposite result, not a monotone one."""
    argv = _shapes(tmp_path, (700.0, 850.0, 980.0))  # the ratio rises
    out = tmp_path / "reversed"
    assert _main([*argv, "--point", "u30", "--ordered", "decreasing", "--out", str(out)]) == 0
    ordering = json.loads(out.with_suffix(".json").read_text())["ordering"]
    assert ordering["direction"] == "decreasing"
    assert ordering["monotone"] is False
    assert "monotone no" in capsys.readouterr().out


def test_the_ordering_direction_must_be_stated(tmp_path, capsys) -> None:
    argv = _shapes(tmp_path, (980.0, 850.0, 700.0))
    with pytest.raises(SystemExit) as exc:
        _main([*argv, "--point", "u30", "--ordered"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        _main([*argv, "--point", "u30", "--ordered", "sideways"])


def test_an_ordering_in_the_stated_direction_is_monotone(tmp_path) -> None:
    falling = _shapes(tmp_path / "f", (700.0, 850.0, 980.0))
    rising = _shapes(tmp_path / "r", (980.0, 850.0, 700.0))
    for argv, direction in ((falling, "increasing"), (rising, "decreasing")):
        out = tmp_path / direction
        assert _main([*argv, "--point", "u30", "--ordered", direction, "--out", str(out)]) == 0
        ordering = json.loads(out.with_suffix(".json").read_text())["ordering"]
        assert ordering["direction"] == direction
        assert ordering["monotone"] is True
    assert compare_sets.ordering(["a", "b"], [1.0, None], [], "increasing") == {
        "order": ["a", "b"],
        "direction": "increasing",
        "values": [1.0, None],
        "monotone": False,
        "extremes_separated": False,
    }


def test_the_contrast_and_staleness_can_be_named(tmp_path, capsys) -> None:
    path = _write(
        tmp_path,
        "stale",
        *_runs("", jsq=1000.0, wjsq=900.0, seed=24, staleness=5.0),
    )
    assert (
        _main(
            ["--set", f"s={path}", "--point", "u30", "--staleness", "5", "--contrast", "wjsq,jsq"]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "jsq/wjsq on mean latency at point `u30`, staleness 5 s." in text
    assert "| s | 2.4 | defined |" in text
