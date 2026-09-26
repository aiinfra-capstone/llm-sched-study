"""tools/paper_figures.py: smoke tests. The figures render, and they say what they left out.

A figure is checked by eye, so these tests do not compare pixels. They check the two things
an eye misses: a set of summaries where nothing is drawable still produces a figure, and a
point or bar that was left out is named on the figure rather than silently missing.
"""

from __future__ import annotations

import json

import matplotlib.figure
import paper_figures
import pytest


def _cell(value: float) -> dict:
    return {"value": value, "ci95": [value - 10, value + 10]}


def _policy(value: float) -> dict:
    return {
        "p50": _cell(value),
        "p95": _cell(value * 1.5),
        "p99": _cell(value * 2),
        "share_to_fast_node": 0.6,
    }


def _gain(defined: bool) -> dict:
    if not defined:
        return {"status": "undefined: transient cell(s) round_robin"}
    stat = {
        "ratio": 0.8,
        "ratio_ci95": [0.75, 0.85],
        "gain_ms": 100.0,
        "gain_ms_ci95": [80.0, 120.0],
    }
    return {"status": "defined", "mean": stat}


def _h1() -> dict:
    return {
        "mean": {
            "interaction_log": 0.05,
            "interaction_log_ci95": [0.01, 0.09],
            "interaction": 40.0,
            "ci95": [10.0, 70.0],
        }
    }


def _summary(steady: bool = True, lambdas=(1.0, 2.0), staleness: float = 0.0) -> dict:
    points = []
    for lam in lambdas:
        points.append(
            {
                "lambda_rps": lam,
                "staleness_s": 0.0,
                "policies": {
                    p: _policy(500 * lam + k * 20)
                    for k, p in enumerate(paper_figures.POLICY_ORDER[:4])
                },
                "h1": _h1() if steady else {},
                "calibration_gain": {"queue_blind": _gain(steady), "queue_aware": _gain(True)},
            }
        )
    if staleness:
        points.append({**points[0], "staleness_s": staleness, "h1": {}})
    return {"vehicle": ["hardware"], "fast_node": "rtx3050", "points": points}


@pytest.fixture
def saved(monkeypatch):
    figures: list[matplotlib.figure.Figure] = []
    original = matplotlib.figure.Figure.savefig

    def savefig(self, path, *args, **kwargs):
        figures.append(self)
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(matplotlib.figure.Figure, "savefig", savefig)
    return figures


RHO = {"summarisation": 13.76, "anchor": 3.0, "balanced": 2.0, "generation": 0.5}


def _report(rho: dict[str, float]) -> dict:
    """A phase_ratio.py report with one profile per shape, named as the tool names them."""
    return {
        "fast": {"node_class": "rtx3050_ngl99", "snapshot_id": "cm_fast"},
        "slow": {"node_class": "gtx1650ti_ngl99", "snapshot_id": "cm_slow"},
        "concurrency": 1,
        "profiles": [
            {
                "profile": f"trace_{name}_1b",
                "mean_rho": value,
                "R_service": 2.0,
                "R_prefill": 9.0,
                "R_decode": 1.2,
            }
            for name, value in rho.items()
        ],
    }


def _texts(fig) -> str:
    parts = [t.get_text() for t in fig.texts]
    if fig._suptitle is not None:
        parts.append(fig._suptitle.get_text())
    for ax in fig.axes:
        parts += [t.get_text() for t in ax.texts] + [ax.get_title()]
    return "\n".join(parts)


def test_h1_with_every_point_transient_still_draws_and_names_the_skipped_points(
    tmp_path, saved
) -> None:
    path = paper_figures.h1_interaction({"anchor": _summary(steady=False, staleness=5.0)}, tmp_path)
    assert path.is_file()
    text = _texts(saved[-1])
    assert "Not drawn (a 2x2 cell transient or saturated): anchor 1, anchor 2" in text
    assert "anchor 5" not in text  # a staleness point is not a skipped load point
    assert "vehicle: hardware | run sets: anchor" in text


def test_h1_with_every_point_drawable_lists_nothing_as_skipped(tmp_path, saved) -> None:
    paper_figures.h1_interaction({"anchor": _summary()}, tmp_path)
    assert "Not drawn" not in _texts(saved[-1])


def test_an_undefined_calibration_gain_is_marked_nd(tmp_path, saved) -> None:
    campaigns = {
        "generation": _summary(steady=False),
        "balanced": _summary(),
        "summarisation": _summary(lambdas=(1.0, 3.0)),
        "not_a_shape": _summary(),
    }
    path = paper_figures.calibration_gain_by_shape(campaigns, tmp_path, RHO)
    assert path.name == "calibration_gain_by_shape.png"
    text = _texts(saved[-1])
    assert "n/d" in text
    assert "not_a_shape" not in text
    # 3 req/s is in one shape only, so it gets no row; 2 req/s is in two of the three.
    assert "3 req/s" not in text and "1 req/s" in text and "2 req/s" in text


def test_calibration_gain_needs_two_shapes(tmp_path) -> None:
    assert paper_figures.calibration_gain_by_shape({"anchor": _summary()}, tmp_path, RHO) is None


def test_main_draws_every_figure_and_prints_their_paths(tmp_path, capsys) -> None:
    summaries = {name: _summary() for name in ("anchor", "generation")}
    summaries["generation"]["points"][0]["policies"].pop("wjsq")
    args = []
    for name, s in summaries.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(s))
        args += ["--campaign", f"{name}={p}"]
    ratio = tmp_path / "ratio.json"
    ratio.write_text(json.dumps(_report(RHO)))
    out = tmp_path / "figs"
    assert paper_figures.main([*args, "--phase-ratio", str(ratio), "--out", str(out)]) == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(
        [
            "latency_by_policy_anchor.png",
            "latency_by_policy_generation.png",
            "routing_share_anchor.png",
            "routing_share_generation.png",
            "h1_interaction.png",
            "calibration_gain_by_shape.png",
            "phase_ratio.png",
        ]
    )
    assert len(capsys.readouterr().out.splitlines()) == 7


def test_main_with_one_campaign_and_no_phase_report(tmp_path, capsys) -> None:
    p = tmp_path / "anchor.json"
    p.write_text(json.dumps(_summary()))
    assert paper_figures.main(["--campaign", f"anchor={p}", "--out", str(tmp_path / "f")]) == 0
    assert "calibration_gain_by_shape" not in capsys.readouterr().out
    assert paper_figures.main(["--out", str(tmp_path / "empty")]) == 0


def test_the_phase_ratio_axis_says_slow_over_fast(tmp_path, saved) -> None:
    """phase_ratio.py divides the slow node's time by the fast node's, so a bar above 1 is
    how many times slower the slow node is."""
    paper_figures.phase_ratio(_report(RHO), tmp_path)
    (ax,) = saved[-1].axes
    assert ax.get_ylabel() == "slow node time over fast node time"
    assert "gtx1650ti over rtx3050" in ax.get_title()


def test_rho_is_read_from_the_phase_ratio_report(tmp_path, saved) -> None:
    """rho is a property of the trace, measured by phase_ratio.py. A constant copied into
    this file goes stale the day a profile changes."""
    assert paper_figures.shape_rho(_report(RHO)) == RHO
    campaigns = {"generation": _summary(), "balanced": _summary()}
    for rho in (RHO, {**RHO, "balanced": 4.5}):
        paper_figures.calibration_gain_by_shape(
            campaigns, tmp_path, paper_figures.shape_rho(_report(rho))
        )
        ticks = [t.get_text() for t in saved[-1].axes[0].get_xticklabels()]
        assert ticks == ["generation\nrho 0.5", f"balanced\nrho {rho['balanced']:g}"]
    assert not hasattr(paper_figures, "SHAPE_RHO")


def test_the_h1_figure_raises_no_legend_warning(tmp_path) -> None:
    """With every point transient nothing is drawn, and a legend over no artists is a
    warning that says nothing a reader can use."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        paper_figures.h1_interaction({"anchor": _summary(steady=False)}, tmp_path)
        paper_figures.latency_by_policy("empty", {"vehicle": ["hardware"], "points": []}, tmp_path)
