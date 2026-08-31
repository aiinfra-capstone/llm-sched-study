"""F-7 self-validation — does the cost model predict the hardware it was measured on?

F-23 asks whether the simulator reproduces hardware within a stated tolerance. When that
comparison fails there are two suspects, and they live on opposite sides of the seam: the
simulator's queueing and policy logic, or the cost model the simulator was parameterised
from. Nothing in the F-23 figure separates them, so a failure there starts an argument
rather than a diagnosis.

This is the upstream half of that question, and it needs no simulator at all. Take a run
that already happened, take the C-3 snapshot its nodes were deployed under, and ask what
`predict_service_ms` would have said about each request that ran. If the cost model does
not predict the hardware it was measured on, no DES parameterised from it can pass F-23,
and the DES is not where to look.

**The comparison is against the reference lookup, not against a better fit.** What matters
is the error the scheduler and the simulator would actually make — nearest-measured
concurrency, bucketed lengths, no interpolation — rather than the error a cleverer
estimator could achieve from the same samples. A residual this reports is a residual
something downstream is really carrying.

**Concurrency is taken at admission.** `inflight_at_admission + 1` is the number of
requests in the engine when this one entered it, which is the same quantity the
calibration grid pass holds fixed by construction. It is a proxy: occupancy can change
while a request is being served. It is the *right* proxy, because it is the number the
calibration was indexed by, and comparing against anything else would measure the gap
between two definitions rather than the gap between prediction and hardware.

**Warmup is excluded here too**, and from the client log's `intended_offset_s` rather than
from anything worker-local, so that a request is warmup in this tool if and only if it is
warmup in the join (§12 failure mode 5).
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataplane.calibration import cost_model as cm
from dataplane.pipeline import runset
from dataplane.pipeline.join import load_jsonl
from dataplane.pipeline.loadband import _percentile as percentile

__all__ = [
    "CellError",
    "CostCheck",
    "cells",
    "check",
    "observations",
]

# The same tolerance the F-23 figure draws, and duplicated here for the same reason
# `figures.plots.percentile` is duplicated: this module deliberately does not import the
# figure layer, and a test pins the two equal rather than one importing the other. ±25% is
# the anchors' own resolution at n≈190, derived in `figures.plots`, and a cost model that
# misses by more than the measurement can resolve has already lost F-23.
F23_TOLERANCE = 0.25


@dataclass(frozen=True)
class CellError:
    """One calibrated cell, as the run actually exercised it."""

    node_id: str
    prompt_bucket: tuple[int, int]
    output_bucket: tuple[int, int]
    concurrency: int
    n: int
    observed_mean_ms: float
    observed_p50_ms: float
    observed_p95_ms: float
    predicted_ms: float

    @property
    def relative_error(self) -> float:
        """Signed, and against the prediction — a +1.0 means the hardware took twice as long."""
        return (self.observed_mean_ms - self.predicted_ms) / self.predicted_ms

    @property
    def median_relative_error(self) -> float:
        """The same comparison against the observed median, and it is worth reading.

        The prediction is a mean measured with concurrency *held fixed*. A live run's cell
        is a mean over requests **labelled** by their concurrency at admission, and a
        request admitted alone on a busy node does not stay alone — occupancy climbs while
        it is being served. Those requests keep the low-concurrency label and carry a
        high-concurrency service time, which lands entirely in the upper tail and drags
        the cell mean up while barely moving its median.

        So a cell whose mean is far outside tolerance and whose median is inside is not a
        cost model that failed. It is the admission-time label doing what a proxy does.
        The mean stays the verdict, because it is what the prediction is; this is here so
        the difference can be seen rather than argued about.
        """
        return (self.observed_p50_ms - self.predicted_ms) / self.predicted_ms

    @property
    def within(self) -> bool:
        return abs(self.relative_error) <= F23_TOLERANCE


@dataclass(frozen=True)
class CostCheck:
    """What one run set says about the cost model it was served by."""

    errors: list[CellError]
    uncalibrated: list[tuple[str, int, int, int]]
    tolerance: float

    @property
    def weighted_error(self) -> float:
        """Request-weighted mean absolute error — the headline.

        Weighted by request count rather than by cell, because a cell the trace visits
        four times and a cell it visits four hundred times contribute equally to a figure
        and should not contribute equally to the verdict on the model behind it.
        """
        n = sum(e.n for e in self.errors)
        if not n:
            return 0.0
        return sum(abs(e.relative_error) * e.n for e in self.errors) / n

    @property
    def weighted_median_error(self) -> float:
        """The same figure computed on medians — see `CellError.median_relative_error`."""
        n = sum(e.n for e in self.errors)
        if not n:
            return 0.0
        return sum(abs(e.median_relative_error) * e.n for e in self.errors) / n

    @property
    def covered(self) -> float:
        """Fraction of served requests that landed on a cell the grid actually measured.

        Every exercised cell is in `errors`, extrapolated or not, because every one of
        them got a prediction and that prediction is what the run was served by.
        `uncalibrated` marks the subset the grid never visited, so coverage is what is
        left after taking those out.
        """
        served = sum(e.n for e in self.errors)
        if not served:
            return 0.0
        return (served - sum(n for *_, n in self.uncalibrated)) / served

    def summary(self) -> list[str]:
        out = [
            (
                f"{len(self.errors)} exercised cell(s), "
                f"{sum(e.n for e in self.errors)} request(s), "
                f"weighted |error| {self.weighted_error * 100:.1f}% "
                f"against a +/-{self.tolerance * 100:.0f}% tolerance "
                f"({self.weighted_median_error * 100:.1f}% on medians)"
            )
        ]
        outside = [e for e in self.errors if not e.within]
        if outside:
            out.append(
                f"{len(outside)} cell(s) outside tolerance, worst "
                f"{max(abs(e.relative_error) for e in outside) * 100:.0f}% — the simulator "
                "cannot be closer to the hardware than the model it is parameterised from"
            )
        if self.uncalibrated:
            n = sum(count for *_, count in self.uncalibrated)
            out.append(
                f"{n} request(s) fell on {len(self.uncalibrated)} concurrency level(s) the "
                "grid never measured; their service time is the nearest measured level's, "
                "which is an extrapolation the cost model does not admit to making"
            )
        return out


def observations(run_dir: str | Path) -> list[dict[str, Any]]:
    """Every measured, successful request in one run directory, with its node and cell."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    warmup_s = float(manifest.get("warmup_s", 0.0))

    warm = {
        c["req_id"]
        for path in sorted(run_dir.glob("client_*.jsonl"))
        for c in load_jsonl(path)
        if float(c["intended_offset_s"]) < warmup_s
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("worker_*.jsonl")):
        for w in load_jsonl(path):
            if w["status"] != "ok" or w["req_id"] in warm:
                continue
            rows.append(
                {
                    "node_id": w["node_id"],
                    "prompt_len": int(w["prompt_tokens"]),
                    "output_len": int(w["output_tokens"]),
                    "concurrency": int(w["inflight_at_admission"]) + 1,
                    "service_ms": w["service_ns"] / 1e6,
                }
            )
    return rows


def cells(
    rows: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> tuple[list[CellError], list[tuple[str, int, int, int]]]:
    """Group the observations into calibrated cells and price each one.

    `snapshots` maps node_id to the C-3 document that node was deployed under, because a
    heterogeneous pool has a different cost model per node class and averaging their
    errors together would report a number describing neither.
    """
    grouped: dict[tuple[str, tuple[int, int], tuple[int, int], int], list[float]] = {}
    for row in rows:
        snapshot = snapshots[row["node_id"]]
        p_buckets = [tuple(e["prompt_bucket"]) for e in snapshot["entries"]]
        o_buckets = [tuple(e["output_bucket"]) for e in snapshot["entries"]]
        key = (
            row["node_id"],
            cm.assign_bucket(row["prompt_len"], p_buckets),
            cm.assign_bucket(row["output_len"], o_buckets),
            row["concurrency"],
        )
        grouped.setdefault(key, []).append(row["service_ms"])

    errors: list[CellError] = []
    uncalibrated: list[tuple[str, int, int, int]] = []
    for (node_id, p_bucket, o_bucket, concurrency), values in sorted(grouped.items()):
        snapshot = snapshots[node_id]
        measured = {
            e["concurrency"]
            for e in snapshot["entries"]
            if tuple(e["prompt_bucket"]) == p_bucket and tuple(e["output_bucket"]) == o_bucket
        }
        if concurrency not in measured:
            uncalibrated.append((node_id, p_bucket[1], o_bucket[1], len(values)))
        errors.append(
            CellError(
                node_id=node_id,
                prompt_bucket=p_bucket,
                output_bucket=o_bucket,
                concurrency=concurrency,
                n=len(values),
                observed_mean_ms=statistics.fmean(values),
                observed_p50_ms=percentile(values, 0.50),
                observed_p95_ms=percentile(values, 0.95),
                predicted_ms=cm.predict_service_ms(snapshot, p_bucket[0], o_bucket[0], concurrency),
            )
        )
    return errors, uncalibrated


def check(
    root: str | Path,
    *,
    snapshot: dict[str, Any] | None = None,
    index: dict[str, dict[str, Any]] | None = None,
    tolerance: float = F23_TOLERANCE,
) -> CostCheck:
    """Price every run under `root` against the cost model its nodes were deployed under.

    `snapshot` overrides that lookup with one candidate document for every node, which is
    how a *newly measured* table is tested against runs that predate it. That is the only
    honest way to ask "would recalibrating have helped?" — the runs cannot be replayed, so
    the new model is scored on the old run's requests.
    """
    index = runset.snapshot_index() if index is None else index
    rows: list[dict[str, Any]] = []
    snapshots: dict[str, dict[str, Any]] = {}
    for run_dir in runset.discover(root):
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        run_rows = observations(run_dir)
        for node_id in {r["node_id"] for r in run_rows}:
            if snapshot is not None:
                snapshots[node_id] = snapshot
                continue
            named = manifest.get("cost_model_snapshots", {}).get(node_id)
            if named is None:
                raise ValueError(
                    f"{run_dir.name}: node {node_id!r} served requests but the manifest "
                    "names no cost-model snapshot for it, so there is no prediction to "
                    "check it against"
                )
            if named not in index:
                raise ValueError(
                    f"{run_dir.name}: node {node_id!r} names snapshot {named!r}, which is "
                    "not committed under contracts/cost_models/"
                )
            snapshots[node_id] = index[named]
        rows += run_rows

    errors, uncalibrated = cells(rows, snapshots)
    return CostCheck(errors=errors, uncalibrated=uncalibrated, tolerance=tolerance)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="F-7 — check a cost model against the runs it was deployed for"
    )
    ap.add_argument("root", type=Path, help="directory of run directories")
    ap.add_argument(
        "--snapshot",
        type=Path,
        help="score this candidate C-3 snapshot instead of the one each manifest names",
    )
    ap.add_argument(
        "--tolerance",
        type=float,
        default=F23_TOLERANCE,
        help=f"relative error a cell may carry and still pass (default {F23_TOLERANCE})",
    )
    args = ap.parse_args(argv)

    candidate = cm.load(args.snapshot) if args.snapshot else None
    result = check(args.root, snapshot=candidate, tolerance=args.tolerance)

    header = f"{'node':>12} {'prompt':>10} {'output':>10} {'c':>2} {'n':>5}"
    print(
        f"{header} {'obs mean':>9} {'obs p50':>9} {'obs p95':>9} {'pred':>9} "
        f"{'error':>8} {'on p50':>8}"
    )
    for e in result.errors:
        flag = "" if abs(e.relative_error) <= args.tolerance else "  <-- outside"
        print(
            f"{e.node_id:>12} {list(e.prompt_bucket)!s:>10} {list(e.output_bucket)!s:>10} "
            f"{e.concurrency:>2} {e.n:>5} {e.observed_mean_ms:>9.1f} {e.observed_p50_ms:>9.1f} "
            f"{e.observed_p95_ms:>9.1f} {e.predicted_ms:>9.1f} {e.relative_error * 100:>7.1f}% "
            f"{e.median_relative_error * 100:>7.1f}%{flag}"
        )
    print()
    for line in result.summary():
        print(f"  {line}")
    return 0 if result.weighted_error <= args.tolerance else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
