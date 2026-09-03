"""H2's degenerate baseline, Threshold(T), and the curve it is supposed to appear on.

Skipped until `plots.THRESHOLD_BASELINE` lands. The figure code is deliberately not
written yet; this file is the contract it has to satisfy.

H2 is two claims, and only the first of them is implemented today:

  1. the advantage of hardware-aware routing over hardware-blind routing is non-monotonic
     in *R*, rising to a peak and falling away again, and
  2. at high *R* the best policy converges toward `Threshold(T)`, which is round-robin
     over the nodes above a calibrated cutoff and is a one-line static rule.

`h2_advantage_curve` answers the first. Nothing answers the second: `threshold` is in
neither `CELLS`, `HARDWARE_AWARE` nor `HARDWARE_BLIND`, so a sweep that ran the policy has
its rows silently dropped and the convergence half of H2 cannot be drawn from data we
already paid to collect.

The trap this file exists to close is the obvious fix. `Threshold(T)` reads a calibrated
cutoff, so adding it to `HARDWARE_AWARE` looks right and is wrong: `advantage_ms` is
`blind.min() - aware.min()`, so the baseline would start winning that minimum and H2's
headline number would quietly stop being about `StaticWeighted` and `WJSQ`. The baseline is
what the tuned policies are measured against, not one of them. Several tests below assert
nothing more than that adding threshold rows leaves the existing estimators where they were.

Sign convention throughout, matching `advantage_ms`: positive means the tuned policy
finished sooner than the thing it is being compared to. So `threshold_gap_ms` is positive
while calibration and queue-awareness are still buying something over the static rule, and
approaches zero as they converge. Convergence and "never ran the baseline" are different
statements, so the second is `None` rather than a zero that reads as the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from conftest import pending

pytestmark = pytest.mark.forward

plots = pending(
    "dataplane.figures.plots",
    "THRESHOLD_BASELINE",
    week="Elevation 1",
    deliverable="the Threshold(T) baseline on the H2 curve",
)


# --------------------------------------------------------------------------------------
# Fixtures. The estimators read one row per run, so these are built at that grain rather
# than at C-5's request grain: a request-level frame would only be reduced back to this.
# --------------------------------------------------------------------------------------


def _sweep(
    *,
    converging: bool = True,
    with_threshold: bool = True,
    threshold_at: tuple[float, ...] | None = None,
) -> pd.DataFrame:
    """The 2x2 across three values of *R*, optionally with the baseline alongside.

    Built so the two halves of H2 point in different directions and a test cannot pass by
    conflating them. `advantage_ms` is non-monotonic (1, 25, 5 ms) while the gap to the
    baseline shrinks monotonically (40, 20, 2 ms), so an estimator that returned the
    advantage under the baseline's name would fail on shape alone.
    """
    advantage = {1.0: 1.0, 2.0: 25.0, 8.0: 5.0}
    gap = {1.0: 40.0, 2.0: 20.0, 8.0: 2.0} if converging else {1.0: 2.0, 2.0: 20.0, 8.0: 40.0}
    rows: list[dict[str, Any]] = []
    for r_value in (1.0, 2.0, 8.0):
        blind = 100.0 + 10.0 * r_value
        aware = blind - advantage[r_value]
        for policy in plots.CELLS:
            base = aware if policy in plots.HARDWARE_AWARE else blind
            rows.append({"R": r_value, "policy": policy, "mean_latency_ms": base})
        if with_threshold and (threshold_at is None or r_value in threshold_at):
            rows.append(
                {
                    "R": r_value,
                    "policy": plots.THRESHOLD_BASELINE,
                    "mean_latency_ms": aware + gap[r_value],
                }
            )
    return pd.DataFrame(rows)


def _by_r(curve: list[dict[str, Any]], key: str) -> dict[float, Any]:
    return {point["R"]: point[key] for point in curve}


# --------------------------------------------------------------------------------------
# The baseline is a baseline, not a fifth cell
# --------------------------------------------------------------------------------------


def test_threshold_is_not_a_cell_of_the_two_by_two() -> None:
    """H1 decomposes hardware-knowledge against queue-knowledge over four cells.

    `Threshold(T)` reads a calibrated cutoff and ignores queue depth, so it is not a
    missing corner of that square; it is the degenerate rule H2 says the square collapses
    toward. Putting it in `CELLS` would make `h1_interaction` a difference of differences
    over five policies, which is not a decomposition of anything.
    """
    assert plots.THRESHOLD_BASELINE not in plots.CELLS


def test_threshold_is_in_neither_awareness_set() -> None:
    """The one that matters. `advantage_ms` is `blind.min() - aware.min()`, so a baseline
    inside `HARDWARE_AWARE` would compete for that minimum and H2's headline would stop
    being about the tuned policies without anything failing."""
    assert plots.THRESHOLD_BASELINE not in plots.HARDWARE_AWARE
    assert plots.THRESHOLD_BASELINE not in plots.HARDWARE_BLIND


def test_the_advantage_is_unchanged_by_the_presence_of_the_baseline() -> None:
    """The regression guard behind the two assertions above, stated on the output rather
    than on the constants, because this is the number that reaches the report."""
    without = plots.h2_advantage_curve(_sweep(with_threshold=False))
    with_it = plots.h2_advantage_curve(_sweep(with_threshold=True))
    assert _by_r(with_it, "advantage_ms") == _by_r(without, "advantage_ms")


def test_h1_ignores_the_baseline_rather_than_refusing_it() -> None:
    """A sweep that ran five policies still holds a complete 2x2. `h1_interaction` refuses
    a *missing* cell; an extra policy is not a missing one, and refusing it would mean H1
    and H2 could not be drawn from the same run set."""
    at_r = _sweep()[lambda f: f["R"] == 2.0]
    cells = {
        row["policy"]: row["mean_latency_ms"]
        for _, row in at_r.iterrows()
        if row["policy"] in plots.CELLS
    }
    with_baseline = {row["policy"]: row["mean_latency_ms"] for _, row in at_r.iterrows()}
    assert plots.h1_interaction(with_baseline) == plots.h1_interaction(cells)


def test_mpr2_range_is_unchanged_by_the_presence_of_the_baseline() -> None:
    """MPR-2 is H1 across the R range, so it inherits the same requirement."""
    without = plots.mpr2_interaction_range(_sweep(with_threshold=False))
    with_it = plots.mpr2_interaction_range(_sweep(with_threshold=True))
    assert with_it["interaction_by_r"] == without["interaction_by_r"]
    assert (with_it["low"], with_it["high"]) == (without["low"], without["high"])


# --------------------------------------------------------------------------------------
# What the curve gains
# --------------------------------------------------------------------------------------


def test_the_curve_reports_the_gap_to_the_baseline_at_every_point() -> None:
    """Best-against-best, the same rule `advantage_ms` uses: H2 is about what the tuned
    policies are worth at their best against a static rule at its best, not about the
    average of a policy set I chose."""
    curve = plots.h2_advantage_curve(_sweep())
    assert _by_r(curve, "threshold_gap_ms") == {1.0: 40.0, 2.0: 20.0, 8.0: 2.0}


def test_the_gap_is_positive_while_calibration_still_buys_something() -> None:
    """Sign convention, pinned. Positive means the tuned policy finished sooner than the
    degenerate baseline, which is the direction `advantage_ms` already uses."""
    curve = plots.h2_advantage_curve(_sweep())
    assert all(point["threshold_gap_ms"] > 0 for point in curve)


def test_the_gap_closes_as_R_grows_when_the_data_says_so() -> None:
    """H2's second claim, in the form the estimator has to be able to express. The fixture
    converges, so a correct estimator produces a decreasing sequence; the assertion is that
    the quantity is computed, not that the hypothesis is true."""
    gaps = [point["threshold_gap_ms"] for point in plots.h2_advantage_curve(_sweep())]
    assert gaps == sorted(gaps, reverse=True)


def test_a_widening_gap_is_reported_rather_than_clamped() -> None:
    """The negative result has to be as reportable as the positive one. If the tuned
    policies pull away from the baseline instead of collapsing into it, H2's second half
    is false, and that is a finding rather than something to floor at zero."""
    gaps = [
        point["threshold_gap_ms"] for point in plots.h2_advantage_curve(_sweep(converging=False))
    ]
    assert gaps == sorted(gaps)


def test_the_gap_is_none_when_the_baseline_never_ran() -> None:
    """Not zero. Zero is the value that means "converged", and reporting it for a sweep
    that never ran the policy would put the strongest form of H2's second claim into the
    report on the strength of missing data."""
    curve = plots.h2_advantage_curve(_sweep(with_threshold=False))
    assert all(point["threshold_gap_ms"] is None for point in curve)


def test_a_baseline_at_only_some_R_values_is_refused() -> None:
    """A convergence claim read off a curve with holes in it is worse than no curve. This
    is the same refusal `h2_advantage_curve` already makes when a whole awareness set is
    missing at a point: undefined rather than interpolated."""
    with pytest.raises(ValueError, match="threshold"):
        plots.h2_advantage_curve(_sweep(threshold_at=(1.0, 2.0)))


# --------------------------------------------------------------------------------------
# Where it appears
# --------------------------------------------------------------------------------------


def test_the_baseline_rides_on_the_h2_figure_rather_than_adding_one() -> None:
    """Two figures would put the two halves of one hypothesis on two axes and leave a
    reader to align them by eye. The convergence is only legible against the advantage it
    is converging from, so it belongs on that plot."""
    frame = pd.DataFrame(
        {
            "vehicle": ["simulator"] * 5,
            "policy": [*plots.CELLS, plots.THRESHOLD_BASELINE],
            "R": [1.0, 1.0, 1.0, 2.0, 2.0],
            "staleness_s": [0.0] * 5,
        }
    )
    names = plots.drawable(frame)
    assert "h2-advantage" in names
    assert not any("threshold" in name for name in names)


def test_the_figure_still_draws_with_the_baseline_present(tmp_path: Path) -> None:
    """The rendering path, shallowly. What the estimator computes is asserted above; this
    is only that a run set holding five policies reaches a PNG instead of raising."""
    rows: list[dict[str, Any]] = []
    for _, row in _sweep().iterrows():
        run_id = f"run_{row['policy']}_{row['R']:g}"
        rows += [
            {
                "run_id": run_id,
                "vehicle": "simulator",
                "policy": row["policy"],
                "R": row["R"],
                "staleness_s": 0.0,
                "lambda": 1.2,
                "e2e_ms": float(row["mean_latency_ms"]),
                "service_ms": float(row["mean_latency_ms"]) * 0.8,
                "queue_wait_ms": float(row["mean_latency_ms"]) * 0.2,
                "routing_error_ms": 0.0,
                "is_warmup": False,
                "status": "ok",
            }
            for _ in range(12)
        ]
    path = plots.h2_advantage(pd.DataFrame(rows), tmp_path)
    assert path.name == "h2_advantage.png" and path.stat().st_size > 0
