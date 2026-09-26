"""tools/results_tables.py: results.md tables drawn from summary.json, and checked (M5).

analysis-plan 8.1 makes a run set's `summary.json` the source of truth, and a number in prose
that disagrees with it wrong. Copying tables by hand across five campaigns drifts; it already
did once. So the tables that come straight from a summary are generated into `results.md`
between markers, and `--check` fails CI when the committed text no longer matches.

A generator rather than a checker: a checker still leaves someone copying numbers by hand,
and M5 is about that copying.

Written before the code. The interface:

    <!-- generated: KIND PATH [key=value ...] -->
    ...table, rewritten by the tool...
    <!-- /generated -->

    results_tables.render(kind, summary, **options) -> str      the table, no trailing newline
    results_tables.update(text, load) -> str                    every block rewritten; `load`
                                                                  maps a PATH to its summary
    results_tables.main([FILE, "--check", "--root", DIR]) -> int
        writes FILE and returns 0; with --check writes nothing and returns 1 on any stale
        block, naming it; returns 2 when a block cannot be rendered. PATH is relative to
        --root, which defaults to the repository root.

Two kinds to start with, the two table shapes results.md sections 3 and 5 are built on:

    policy_means [stat=mean|p95]   one row per policy, one column per load, staleness 0
    calibration_gain               one row per load: SW/RR and WJSQ/JSQ with their ms gains

Formats follow what results.md already prints: `916 [814, 1023]` for latency, three places
for a ratio, `n/d` where a contrast is undefined, and a trailing `T` on a transient cell,
which analysis-plan 8.3 says is shown and marked rather than dropped.

6.2, every hand-copied table in results.md generated. Written before the code:

    <!-- generated: KIND PATH[+PATH...] [labels=a,b,...] [load=2.4] [key=value ...] -->

    parse_spec(text) -> (kind, [path, ...], options)          a source may join several with +
    points_of_sets(summaries, options) -> points
        one summary and no labels= is points_of. Several summaries need labels=, one per
        summary, and each set's freshest points take its label as their workload. A count
        that does not match, or several summaries with no labels, is a ValueError. Tables
        list workloads in the order the labels name them, then by load.
    h1_interaction(points, stat="mean")        section 4: pool utilisation, the slow node's
                                               offered/capacity under RoundRobin, the
                                               interaction on log and in ms, and the status
    calibration_gain(points, stat="mean")      gains a last column, the slow node's
                                               offered/capacity under JSQ
    load_trend([(label, summary), ...])        section 5's second table, from each summary's
                                               load_trend_queue_aware
    k1_ratios(cell_intervals, [(label, summary), ...], load=None)
                                               section 2: one row per profile in the
                                               cell-intervals file, by mean_rho; operating R
                                               from each labelled set's freshest point whose
                                               lambda rounds to load at one place

    k1_ratios as a block: the first source is the cell-intervals file, and labels= names the
    summaries after it. load_trend as a block: labels= names every source.
    `generated()` keeps a block's source as written, joined with +.
"""

from __future__ import annotations

import importlib
import json
import re

import pytest
from support import REPO_ROOT


@pytest.fixture
def rt():
    return importlib.import_module("results_tables")


def _stat(value, lo, hi) -> dict:
    return {"value": value, "ci95": [lo, hi]}


def _policy(mean, p95=None, transient=False) -> dict:
    p95 = p95 or tuple(x * 2 for x in mean)
    return {
        "mean": _stat(*mean),
        "p95": _stat(*p95),
        "steady_state": {"transient": transient},
    }


def _gain(ratio, ms) -> dict:
    return {
        "status": "defined",
        "mean": {
            "ratio": ratio[0],
            "ratio_ci95": list(ratio[1:]),
            "gain_ms": ms[0],
            "gain_ms_ci95": list(ms[1:]),
        },
    }


def _util(pool: float, **slow_under: float) -> dict:
    """A point's utilisation block: the slow node's offered/capacity under each policy."""
    return {
        "slow_node": "gtx1650ti",
        "pool_utilisation": pool,
        "per_cell": {
            policy: {
                "gtx1650ti": {"offered_over_capacity": v},
                "rtx3050": {"offered_over_capacity": 0.1},
            }
            for policy, v in slow_under.items()
        },
    }


def _summary() -> dict:
    return {
        "points": [
            {
                "lambda_rps": 2.4,
                "staleness_s": 0.0,
                "policies": {
                    "wjsq": _policy((761.2, 691.4, 834.0)),
                    "round_robin": _policy((1516.0, 1330.0, 1713.0), transient=True),
                    "jsq": _policy((873.0, 795.0, 959.0)),
                },
                "calibration_gain": {
                    "queue_blind": {"status": "undefined: transient cell(s) round_robin"},
                    "queue_aware": _gain((0.872, 0.816, 0.924), (112.4, 63.2, 166.0)),
                },
                "utilisation": _util(0.3375, jsq=0.5812, round_robin=0.7251),
            },
            {
                "lambda_rps": 1.3,
                "staleness_s": 0.0,
                "policies": {
                    "round_robin": _policy((916.0, 814.0, 1023.0)),
                    "static_weighted": _policy((788.0, 710.0, 874.0)),
                    "jsq": _policy((734.0, 678.0, 794.0)),
                    "wjsq": _policy((632.0, 594.0, 672.0)),
                },
                "calibration_gain": {
                    "queue_blind": _gain((0.86, 0.771, 0.957), (128.0, 37.0, 226.0)),
                    "queue_aware": _gain((0.862, 0.815, 0.907), (102.0, 64.0, 145.0)),
                },
                "utilisation": _util(0.1838, jsq=0.3571, round_robin=0.4102),
            },
            {
                "lambda_rps": 1.3,
                "staleness_s": 5.0,
                "policies": {"wjsq": _policy((9999.0, 9000.0, 11000.0))},
                "calibration_gain": {
                    "queue_blind": {"status": "undefined"},
                    "queue_aware": {"status": "undefined"},
                },
            },
        ]
    }


# ------------------------------------------------------------------------- rendering


def test_policy_means_is_policies_by_load_at_staleness_zero(rt) -> None:
    lines = [
        "| Policy | 1.3 req/s | 2.4 req/s |",
        "|---|---|---|",
        "| RoundRobin | 916 [814, 1023] | 1516 [1330, 1713] T |",
        "| StaticWeighted | 788 [710, 874] |  |",
        "| JSQ | 734 [678, 794] | 873 [795, 959] |",
        "| WJSQ | 632 [594, 672] | 761 [691, 834] |",
    ]
    assert rt.render("policy_means", _summary()) == "\n".join(lines)


def test_policy_means_can_draw_p95(rt) -> None:
    table = rt.render("policy_means", _summary(), stat="p95")
    assert "| WJSQ | 1264 [1188, 1344] | 1522 [1383, 1668] |" in table


def test_every_policy_has_its_display_name_in_a_fixed_order(rt) -> None:
    names = [
        "ect",
        "static_weighted_wrr",
        "jsq_fastfirst",
        "threshold",
        "wjsq",
        "jsq",
        "static_weighted",
        "round_robin",
    ]
    summary = {
        "points": [
            {
                "lambda_rps": 1.0,
                "staleness_s": 0.0,
                "policies": {n: _policy((1.0, 1.0, 1.0)) for n in names},
            }
        ]
    }
    rows = [
        line.split(" | ")[0][2:] for line in rt.render("policy_means", summary).splitlines()[2:]
    ]
    assert rows == [
        "RoundRobin",
        "StaticWeighted",
        "JSQ",
        "WJSQ",
        "Threshold",
        "JSQFastFirst",
        "StaticWeightedWRR",
        "ECT",
    ]


def test_a_summary_with_several_workloads_needs_to_say_which(rt) -> None:
    summary = _summary()
    for pt, wl in zip(
        summary["points"], ("generation", "summarisation", "generation"), strict=True
    ):
        pt["workload"] = wl
    with pytest.raises(ValueError, match="workload"):
        rt.render("policy_means", summary)
    table = rt.render("policy_means", summary, workload="summarisation")
    assert table.splitlines()[0] == "| Policy | 1.3 req/s |"


def test_calibration_gain_is_one_row_per_load_with_nd_where_undefined(rt) -> None:
    lines = [
        (
            "| Load | Queue-blind SW/RR | ms | Queue-aware WJSQ/JSQ | ms "
            "| Slow node under JSQ, offered / capacity |"
        ),
        "|---|---|---|---|---|---|",
        (
            "| 1.3 | 0.860 [0.771, 0.957] | 128 [37, 226] | 0.862 [0.815, 0.907] | 102 [64, 145] "
            "| 0.36 |"
        ),
        "| 2.4 | n/d | n/d | 0.872 [0.816, 0.924] | 112 [63, 166] | 0.58 |",
    ]
    assert rt.render("calibration_gain", _summary()) == "\n".join(lines)


def test_an_unknown_kind_or_option_is_refused(rt) -> None:
    with pytest.raises(ValueError, match="h7_fantasy"):
        rt.render("h7_fantasy", _summary())
    with pytest.raises(ValueError, match="colour"):
        rt.render("policy_means", _summary(), colour="red")


def test_the_statistics_a_table_can_draw_are_fixed(rt) -> None:
    assert rt.STATS == ("mean", "p50", "p95", "p99")
    for stat in rt.STATS:
        rt.render("policy_means", _summary(), stat=stat)


def test_an_unknown_statistic_is_refused_by_name(rt) -> None:
    """A typo in `stat=` used to render a table of n/d under a heading that claimed p59, or
    whatever else was typed. The table says which statistic it is, so it has to be one."""
    with pytest.raises(ValueError, match="p59"):
        rt.render("policy_means", _summary(), stat="p59")
    with pytest.raises(ValueError, match="p59"):
        rt.update(
            "<!-- generated: policy_means runs/a/summary.json stat=p59 -->\n"
            "old\n<!-- /generated -->\n",
            lambda path: _summary(),
        )


def test_an_option_with_no_equals_sign_is_refused_by_name(rt) -> None:
    """`workload` alone reads as an option someone meant to finish. Python's own complaint
    about it names neither the token nor the block."""
    with pytest.raises(ValueError, match=re.escape("option workload has no '='")):
        rt.parse_spec("policy_means runs/a/summary.json workload")
    with pytest.raises(ValueError, match=re.escape("option workload has no '='")):
        rt.update(_doc(path="runs/a/summary.json workload"), lambda path: _summary())


# --------------------------------------------------------------------------- updating

PROSE_BEFORE = "# Results\n\nThe anchor trace, mean end-to-end latency in ms.\n\n"
PROSE_AFTER = "\nRoundRobin climbs at 2.4 req/s.\n"


def _doc(
    body: str = "stale\n", kind: str = "policy_means", path: str = "runs/a/summary.json"
) -> str:
    return (
        f"{PROSE_BEFORE}<!-- generated: {kind} {path} -->\n{body}<!-- /generated -->\n{PROSE_AFTER}"
    )


def test_update_rewrites_a_block_and_leaves_the_prose_exactly_as_it_was(rt) -> None:
    out = rt.update(_doc(), lambda path: _summary())
    table = rt.render("policy_means", _summary())
    assert out == _doc(body=table + "\n")
    assert out.startswith(PROSE_BEFORE) and out.endswith(PROSE_AFTER)


def test_update_is_idempotent(rt) -> None:
    once = rt.update(_doc(), lambda path: _summary())
    assert rt.update(once, lambda path: _summary()) == once


def test_each_block_reads_its_own_summary_and_options(rt) -> None:
    doc = (
        "<!-- generated: policy_means runs/a/summary.json -->\n<!-- /generated -->\n\n"
        "<!-- generated: policy_means runs/b/summary.json stat=p95 -->\nold\n<!-- /generated -->\n"
    )
    b = _summary()
    b["points"] = b["points"][:1]
    loaded = []
    out = rt.update(doc, lambda path: loaded.append(path) or (_summary() if "/a/" in path else b))
    assert loaded == ["runs/a/summary.json", "runs/b/summary.json"]
    assert rt.render("policy_means", _summary()) in out
    assert rt.render("policy_means", b, stat="p95") in out


def test_text_without_blocks_is_returned_unchanged(rt) -> None:
    text = "# Results\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert rt.update(text, lambda path: pytest.fail("nothing to load")) == text


def test_a_block_that_is_never_closed_is_refused(rt) -> None:
    with pytest.raises(ValueError, match="never closed"):
        rt.update(
            "<!-- generated: policy_means runs/a/summary.json -->\n| x |\n", lambda p: _summary()
        )


def test_a_block_with_an_unknown_kind_is_refused_by_name(rt) -> None:
    with pytest.raises(ValueError, match="h7_fantasy"):
        rt.update(_doc(kind="h7_fantasy"), lambda path: _summary())


# ----------------------------------------------------------------------- the command


def _files(tmp_path, doc: str) -> tuple:
    root = tmp_path / "repo"
    (root / "runs" / "a").mkdir(parents=True)
    (root / "runs" / "a" / "summary.json").write_text(json.dumps(_summary()))
    results = root / "docs" / "results.md"
    results.parent.mkdir()
    results.write_text(doc)
    return root, results


def test_the_command_writes_the_tables(rt, tmp_path) -> None:
    root, results = _files(tmp_path, _doc())
    assert rt.main([str(results), "--root", str(root)]) == 0
    assert rt.render("policy_means", _summary()) in results.read_text()


def test_check_passes_on_an_up_to_date_file_and_writes_nothing(rt, tmp_path) -> None:
    root, results = _files(tmp_path, _doc())
    rt.main([str(results), "--root", str(root)])
    before = results.stat().st_mtime_ns, results.read_text()
    assert rt.main([str(results), "--check", "--root", str(root)]) == 0
    assert (results.stat().st_mtime_ns, results.read_text()) == before


def test_check_fails_on_a_drifted_number_and_names_the_block(rt, tmp_path, capsys) -> None:
    root, results = _files(tmp_path, _doc())
    rt.main([str(results), "--root", str(root)])
    drifted = results.read_text().replace("632 [594, 672]", "633 [594, 672]")
    results.write_text(drifted)
    assert rt.main([str(results), "--check", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "policy_means runs/a/summary.json" in out
    assert results.read_text() == drifted


def test_a_summary_that_is_not_there_is_an_error_not_a_pass(rt, tmp_path, capsys) -> None:
    root, results = _files(tmp_path, _doc(path="runs/gone/summary.json"))
    assert rt.main([str(results), "--check", "--root", str(root)]) == 2
    assert "runs/gone/summary.json" in capsys.readouterr().out


def test_the_committed_results_are_generated_and_up_to_date(rt, capsys) -> None:
    """The guard M5 asks for. results.md has to carry at least one generated table, drawn
    from a committed summary, and every one of them has to match it."""
    results = REPO_ROOT / "docs" / "results.md"
    assert "<!-- generated:" in results.read_text(), "results.md has no generated table yet"
    assert rt.main([str(results), "--check", "--root", str(REPO_ROOT)]) == 0, (
        capsys.readouterr().out
    )


def test_a_workload_that_matches_no_point_is_refused(rt, tmp_path, capsys) -> None:
    """A misspelt workload would otherwise render an empty table under a sentence that
    describes a real one."""
    with pytest.raises(ValueError, match="no point"):
        rt.points_of(_summary(), {"workload": "generaton"})
    root, results = _files(tmp_path, _doc())
    results.write_text(
        results.read_text().replace("summary.json -->", "summary.json workload=generaton -->")
    )
    assert rt.main([str(results), "--root", str(root)]) == 2
    assert "generaton" in capsys.readouterr().out


def test_generated_refuses_a_block_that_is_never_closed(rt) -> None:
    text = "<!-- generated: policy_means runs/a/summary.json -->\n| x |\n\nprose\n"
    with pytest.raises(ValueError, match="never closed"):
        rt.generated(text)


def test_generated_lists_each_block_with_its_current_body(rt) -> None:
    text = _doc(body="| old |\n")
    assert rt.generated(text) == [("policy_means", "runs/a/summary.json", "| old |")]


def test_a_statistic_the_summary_does_not_hold_is_nd(rt) -> None:
    assert rt._ci(None) == rt.UNDEFINED == "n/d"
    assert rt._ci({"value": None, "ci95": [None, None]}) == "n/d"
    summary = _summary()
    del summary["points"][1]["policies"]["jsq"]["p95"]
    table = rt.render("policy_means", summary, stat="p95")
    assert "| JSQ | n/d | 1746 [1590, 1918] |" in table


def test_writing_a_file_that_is_already_current_leaves_it_untouched(rt, tmp_path) -> None:
    """A document whose tables already match is not rewritten, so its timestamp and any
    editor holding it open see no change."""
    root, results = _files(tmp_path, _doc())
    assert rt.main([str(results), "--root", str(root)]) == 0
    before = results.stat().st_mtime_ns, results.read_text()
    assert rt.main([str(results), "--root", str(root)]) == 0
    assert (results.stat().st_mtime_ns, results.read_text()) == before


# ------------------------------------------------------- 6.2: tables across run sets


def _point(lam: float, staleness: float = 0.0, **over) -> dict:
    """A point with the four H1 cells, a defined H1, and its utilisation."""
    point = {
        "lambda_rps": lam,
        "staleness_s": staleness,
        "policies": {
            "round_robin": _policy((1400.0 + lam, 1300.0, 1500.0)),
            "static_weighted": _policy((1250.0 + lam, 1180.0, 1360.0)),
            "jsq": _policy((1268.0 + lam, 1198.0, 1346.0)),
            "wjsq": _policy((1128.0 + lam, 1068.0, 1189.0)),
        },
        "calibration_gain": {
            "queue_blind": _gain((0.896, 0.852, 0.945), (146.0, 72.0, 211.0)),
            "queue_aware": _gain((0.889, 0.850, 0.926), (141.0, 92.0, 195.0)),
        },
        "h1_status": "defined",
        "h1": {
            "mean": {
                "interaction_log": -0.0081,
                "interaction_log_ci95": [-0.0982, 0.0689],
                "interaction": 4.6,
                "ci95": [-109.2, 102.7],
            }
        },
        "utilisation": _util(0.4312, round_robin=0.7251, jsq=0.6207),
    }
    return point | over


def _set(*points: dict, trend: dict | None = None) -> dict:
    return {"points": list(points), "load_trend_queue_aware": trend}


def test_a_source_can_name_several_run_sets_and_labels_must_match_them(rt) -> None:
    kind, sources, options = rt.parse_spec(
        "h1_interaction runs/a/summary.json+runs/b/summary.json labels=gen,bal"
    )
    assert (kind, sources, options) == (
        "h1_interaction",
        ["runs/a/summary.json", "runs/b/summary.json"],
        {"labels": "gen,bal"},
    )
    two = [_set(_point(2.4)), _set(_point(2.4))]
    with pytest.raises(ValueError, match="labels"):
        rt.points_of_sets(two, {"labels": "gen"})
    with pytest.raises(ValueError, match="labels"):
        rt.points_of_sets(two, {"labels": "gen,bal,summ"})
    with pytest.raises(ValueError, match="labels"):
        rt.points_of_sets(two, {})
    doc = (
        "<!-- generated: policy_means runs/a/summary.json+runs/b/summary.json labels=gen -->\n"
        "old\n<!-- /generated -->\n"
    )
    with pytest.raises(ValueError, match="labels"):
        rt.update(doc, lambda path: _set(_point(2.4)))
    # One summary and no labels is the table it always was.
    assert rt.points_of_sets([_summary()], {}) == rt.points_of(_summary(), {})


def test_h1_interaction_renders_defined_points_and_the_status_of_the_rest(rt) -> None:
    """Section 4. A defined point shows the interaction on log and in ms with its interval;
    any other point keeps its row, leaves both blank and says why, so a reader sees which
    points were not compared rather than finding them missing."""
    generation = _set(
        _point(2.4),
        _point(3.2, h1={}, h1_status="undefined: transient cell(s) round_robin")
        | {"utilisation": _util(0.5701, round_robin=0.9934)},
        _point(2.4, staleness=5.0, h1_status="stale, not shown"),
    )
    summarisation = _set(
        _point(3.2, h1={}, h1_status="undefined: no valid run for round_robin")
        | {"utilisation": _util(0.4912, jsq=0.8)},
    )
    points = rt.points_of_sets([generation, summarisation], {"labels": "generation,summarisation"})
    assert rt.h1_interaction(points) == [
        (
            "| Workload | Load | Pool utilisation | Slow node under RoundRobin, offered / capacity "
            "| Interaction, log | Interaction, ms | Status |"
        ),
        "|---|---|---|---|---|---|---|",
        "| generation | 2.4 | 0.43 | 0.73 | -0.008 [-0.098, +0.069] | +5 [-109, +103] | defined |",
        "| generation | 3.2 | 0.57 | 0.99 |  |  | undefined: transient cell(s) round_robin |",
        "| summarisation | 3.2 | 0.49 |  |  |  | undefined: no valid run for round_robin |",
    ]


def test_policy_means_across_labelled_sets_is_one_column_per_workload_and_load(rt) -> None:
    """Section 6. Three run sets, one per shape, in the order the labels name them rather
    than alphabetically: gen, bal, summ."""
    sets = [_set(_point(2.4), _point(3.2)) for _ in range(3)]
    for k, s in enumerate(sets):
        for pt in s["points"]:
            pt["policies"]["wjsq"] = _policy((100.0 * (k + 1) + pt["lambda_rps"], 1.0, 2.0))
    points = rt.points_of_sets(sets, {"labels": "gen,bal,summ"})
    lines = rt.policy_means(points)
    assert lines[0] == (
        "| Policy | gen, 2.4 req/s | gen, 3.2 req/s | bal, 2.4 req/s | bal, 3.2 req/s "
        "| summ, 2.4 req/s | summ, 3.2 req/s |"
    )
    assert lines[1] == "|---|---|---|---|---|---|---|"
    assert (
        "| WJSQ | 102 [1, 2] | 103 [1, 2] | 202 [1, 2] | 203 [1, 2] | 302 [1, 2] | 303 [1, 2] |"
        in lines
    )


def test_load_trend_renders_each_workload_across_its_loads(rt) -> None:
    """Section 5, second table: the gain at each steady load, lightest first, then the
    change from the lightest to the heaviest with its interval. A set with fewer than two
    steady points has no trend, and says n/d."""
    anchor = _set(
        _point(1.3),
        trend={
            "queue_aware_gain_ms": [101.6, 112.1, 178.4],
            "wjsq_over_jsq": [0.8616, 0.8716, 0.8236],
            "change_ms": 76.7,
            "change_ms_ci95": [-3.9, 171.7],
            "change_in_ratio": 0.956,
            "change_in_ratio_ci95": [0.8667, 1.048],
        },
    )
    generation = _set(
        _point(2.4),
        trend={
            "queue_aware_gain_ms": [141.2, 142.3],
            "wjsq_over_jsq": [0.8891, 0.9002],
            "change_ms": 1.1,
            "change_ms_ci95": [-70.4, 70.2],
            "change_in_ratio": 1.0126,
            "change_in_ratio_ci95": [0.9591, 1.0732],
        },
    )
    assert rt.load_trend(
        [("anchor", anchor), ("generation", generation), ("balanced", _set(_point(2.4)))]
    ) == [
        "| Workload | JSQ - WJSQ, ms | Change | WJSQ/JSQ | Change in ratio |",
        "|---|---|---|---|---|",
        (
            "| anchor | 102 / 112 / 178 | +77 [-4, +172] | 0.862 / 0.872 / 0.824 "
            "| x0.956 [0.867, 1.048] |"
        ),
        "| generation | 141 / 142 | +1 [-70, +70] | 0.889 / 0.900 | x1.013 [0.959, 1.073] |",
        "| balanced | n/d | n/d | n/d | n/d |",
    ]


def _ratio(value: float, lo: float, hi: float) -> dict:
    return {"value": value, "ci95": [lo, hi]}


def _profile(name: str, rho: float, **by_c: tuple) -> dict:
    return {
        "profile": f"trace_{name}_1b",
        "mean_rho": rho,
        "by_concurrency": {
            c.removeprefix("c"): {
                "R_service": _ratio(*service),
                "R_prefill": _ratio(*prefill),
                "R_decode": _ratio(*decode),
            }
            for c, (service, prefill, decode) in by_c.items()
        },
    }


def _operating(service: float, decode: float) -> dict:
    return {"pooled": {"R_service": service, "R_prefill": 9.0, "R_decode": decode}}


def test_k1_ratios_is_one_row_per_workload_from_the_cell_intervals_file(rt) -> None:
    """Section 2. Rows follow the prompt-to-output ratio, whatever order the file lists them
    in. Operating R is read at the stated load from the freshest point of the set labelled
    with that workload; a workload with no such set, or a concurrency the grid lacks, is n/d."""
    cell_intervals = {
        "profiles": [
            _profile(
                "summarisation",
                13.76,
                c1=((2.563, 2.551, 2.577), (11.152, 11.1, 11.2), (1.191, 1.19, 1.192)),
                c4=((4.512, 3.751, 5.402), (7.291, 5.691, 9.432), (2.541, 2.061, 3.081)),
            ),
            _profile(
                "generation",
                0.5,
                c1=((1.379, 1.377, 1.381), (8.716, 8.684, 8.747), (1.186, 1.184, 1.188)),
                c2=((1.77, 1.766, 1.774), (5.05, 5.041, 5.059), (1.533, 1.528, 1.538)),
                c4=((2.164, 1.809, 2.568), (7.009, 5.59, 8.811), (1.773, 1.469, 2.117)),
            ),
            _profile(
                "anchor",
                3.0,
                c1=((1.776, 1.771, 1.781), (9.97, 9.91, 10.03), (1.192, 1.19, 1.194)),
            ),
        ]
    }
    anchor = _set(
        _point(1.305) | {"operating_R_steady_cells": _operating(1.9, 1.5)},
        _point(2.385) | {"operating_R_steady_cells": _operating(2.2412, 1.7928)},
    )
    generation = _set(
        _point(2.4) | {"operating_R_steady_cells": _operating(1.5063, 1.4419)},
        _point(2.4, staleness=5.0) | {"operating_R_steady_cells": _operating(9.0, 9.0)},
    )
    assert rt.k1_ratios(
        cell_intervals, [("generation", generation), ("anchor", anchor)], load=2.4
    ) == [
        (
            "| Workload | Prompt:output | R service, 1 slot | R service, 4 slots | R prefill, 1 slot "
            "| R prefill, 4 slots | R decode, 1 slot | R decode, 4 slots | Operating R service "
            "| Operating R decode |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
        (
            "| generation | 0.50 | 1.38 [1.38, 1.38] | 2.16 [1.81, 2.57] | 8.72 [8.68, 8.75] "
            "| 7.01 [5.59, 8.81] | 1.19 [1.18, 1.19] | 1.77 [1.47, 2.12] | 1.51 | 1.44 |"
        ),
        (
            "| anchor | 3.00 | 1.78 [1.77, 1.78] | n/d | 9.97 [9.91, 10.03] | n/d "
            "| 1.19 [1.19, 1.19] | n/d | 2.24 | 1.79 |"
        ),
        (
            "| summarisation | 13.76 | 2.56 [2.55, 2.58] | 4.51 [3.75, 5.40] | 11.15 [11.10, 11.20] "
            "| 7.29 [5.69, 9.43] | 1.19 [1.19, 1.19] | 2.54 [2.06, 3.08] | n/d | n/d |"
        ),
    ]


def test_every_new_kind_renders_from_a_block(rt) -> None:
    """How the new kinds read their sources inside results.md. k1_ratios takes the
    cell-intervals file first and labels the summaries after it."""
    sets = {
        "runs/g/summary.json": _set(
            _point(2.4) | {"operating_R_steady_cells": _operating(1.5, 1.4)},
            trend=None,
        ),
        "runs/b/summary.json": _set(
            _point(2.4) | {"operating_R_steady_cells": _operating(1.8, 1.5)},
            trend=None,
        ),
        "runs/ci.json": {"profiles": [_profile("g", 0.5, c1=((1.0, 1.0, 1.0),) * 3)]},
    }
    doc = (
        "<!-- generated: h1_interaction runs/g/summary.json+runs/b/summary.json labels=g,b -->\n"
        "<!-- /generated -->\n"
        "<!-- generated: calibration_gain runs/g/summary.json+runs/b/summary.json labels=g,b -->\n"
        "<!-- /generated -->\n"
        "<!-- generated: load_trend runs/g/summary.json+runs/b/summary.json labels=g,b -->\n"
        "<!-- /generated -->\n"
        "<!-- generated: k1_ratios runs/ci.json+runs/g/summary.json+runs/b/summary.json "
        "labels=g,b load=2.4 -->\n<!-- /generated -->\n"
    )
    out = rt.update(doc, sets.__getitem__)
    g, b = sets["runs/g/summary.json"], sets["runs/b/summary.json"]
    points = rt.points_of_sets([g, b], {"labels": "g,b"})
    for lines in (
        rt.h1_interaction(points),
        rt.calibration_gain(points),
        rt.load_trend([("g", g), ("b", b)]),
        rt.k1_ratios(sets["runs/ci.json"], [("g", g), ("b", b)], load=2.4),
    ):
        assert "\n".join(lines) in out
    assert [kind for kind, _, _ in rt.generated(out)] == [
        "h1_interaction",
        "calibration_gain",
        "load_trend",
        "k1_ratios",
    ]
    assert rt.generated(out)[0][1] == "runs/g/summary.json+runs/b/summary.json"


def test_check_flags_a_stale_multi_source_block(rt, tmp_path, capsys) -> None:
    root = tmp_path / "repo"
    for name, lam in (("a", 2.4), ("b", 3.2)):
        (root / "runs" / name).mkdir(parents=True)
        (root / "runs" / name / "summary.json").write_text(json.dumps(_set(_point(lam))))
    results = root / "docs" / "results.md"
    results.parent.mkdir()
    spec = "policy_means runs/a/summary.json+runs/b/summary.json labels=gen,bal"
    results.write_text(f"# R\n\n<!-- generated: {spec} -->\nold\n<!-- /generated -->\n")
    assert rt.main([str(results), "--root", str(root)]) == 0
    assert rt.main([str(results), "--check", "--root", str(root)]) == 0
    written = results.read_text()
    assert "| Policy | gen, 2.4 req/s | bal, 3.2 req/s |" in written

    drifted = written.replace("1131 [1068, 1189]", "1132 [1068, 1189]")
    assert drifted != written
    results.write_text(drifted)
    assert rt.main([str(results), "--check", "--root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "policy_means runs/a/summary.json+runs/b/summary.json" in out
    assert results.read_text() == drifted
