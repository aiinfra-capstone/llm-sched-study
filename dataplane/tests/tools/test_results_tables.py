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
        "| Load | Queue-blind SW/RR | ms | Queue-aware WJSQ/JSQ | ms |",
        "|---|---|---|---|---|",
        "| 1.3 | 0.860 [0.771, 0.957] | 128 [37, 226] | 0.862 [0.815, 0.907] | 102 [64, 145] |",
        "| 2.4 | n/d | n/d | 0.872 [0.816, 0.924] | 112 [63, 166] |",
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
