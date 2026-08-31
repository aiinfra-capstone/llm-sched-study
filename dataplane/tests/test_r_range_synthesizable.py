"""`synthesizable` — R read off C-3 snapshots, and the two ways it refuses.

This is the entry point anyone on the far side of the seam has: the snapshots are committed
under `contracts/cost_models/`, so R can be recomputed there without a copy of my run
directories. The refusals are the point of the function existing rather than someone
dividing two headline throughputs by hand.

The second refusal is the one that has already cost this study something. Decode rate falls
as the KV context grows, so a class measured at a 256-token prompt looks slower than the
same class at 64 — and when the fast node happened to be calibrated at p256 while the slow
one sat at p64, R read 1.66x instead of 2.00x. Nothing downstream could have separated the
hardware difference from the workload difference afterwards, which is why the check belongs
here, before the number exists.
"""

from __future__ import annotations

import pytest

from dataplane.calibration.r_range import synthesizable


def _snapshot(node_class: str, cells: dict[tuple, float], *, at: int = 1788000000) -> dict:
    """The minimum of a C-3 snapshot that `synthesizable` reads."""
    return {
        "node_class": node_class,
        "measured_at_unix": at,
        "snapshot_id": f"cm_{node_class}_{at}",
        "entries": [
            {
                "prompt_bucket": list(prompt),
                "output_bucket": list(output),
                "concurrency": concurrency,
                "tokens_per_s": tok_s,
            }
            for (prompt, output, concurrency), tok_s in cells.items()
        ],
    }


CELLS_FAST = {
    ((1, 128), (1, 64), 1): 60.0,
    ((1, 128), (1, 64), 4): 20.0,
    ((129, 512), (1, 64), 4): 15.0,
}
CELLS_SLOW = {
    ((1, 128), (1, 64), 1): 30.0,
    ((1, 128), (1, 64), 4): 10.0,
    ((129, 512), (1, 64), 4): 5.0,
}


def test_r_is_reported_as_an_interval_with_both_ends() -> None:
    """1.0 is the low end by construction — any pool can be made homogeneous by running one
    class everywhere — and stating it is what makes this a range rather than a figure."""
    lo, hi = synthesizable([_snapshot("fast", CELLS_FAST), _snapshot("slow", CELLS_SLOW)])
    assert lo == 1.0
    # The widest shared cell: 15.0 against 5.0. Not 60/5, which would pair the fast node's
    # easiest cell against the slow node's hardest and call the difference hardware.
    assert hi == pytest.approx(3.0)


def test_only_the_latest_snapshot_of_each_class_is_used() -> None:
    """The series is a history of one node under drift (F-8). Averaging it would produce a
    ratio no snapshot ever claimed, so the newest reading per class is the one that counts."""
    stale = _snapshot("slow", {k: v * 4 for k, v in CELLS_SLOW.items()}, at=1788000000)
    fresh = _snapshot("slow", CELLS_SLOW, at=1788000600)
    lo, hi = synthesizable([fresh, stale, _snapshot("fast", CELLS_FAST)])
    assert (lo, hi) == (1.0, pytest.approx(3.0))


def test_one_node_class_is_not_a_ratio() -> None:
    with pytest.raises(ValueError, match="ratio between two node classes"):
        synthesizable([_snapshot("only", CELLS_FAST)])


def test_classes_with_no_shared_cell_are_refused_rather_than_guessed_at() -> None:
    """This is the 1.66x-versus-2.00x mistake, caught before it becomes a number."""
    apart = {((513, 2048), (65, 128), 4): 5.0}
    with pytest.raises(ValueError, match="share no calibrated cell"):
        synthesizable([_snapshot("fast", CELLS_FAST), _snapshot("slow", apart)])
