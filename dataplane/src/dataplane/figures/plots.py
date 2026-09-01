"""Figure scripts — F-19's last stage, and F-24's enforcement point.

Reads the Parquet a run set was assembled into, never a raw log. That is not tidiness:
every exclusion the analysis depends on — warmup discarded from the trace offset rather
than wall-clock, failed requests kept as rows but out of the latency statistics, the
engine-gap probe dropped as a non-member — is applied once in the pipeline. A figure
script that reached back to the logs would be a second place those rules could be
implemented, which over six weeks means a second place they can be implemented
differently.

**Only simulated figures carry the stamp.** I first wrote this the other way — stamp
everything, so that a missing stamp is visibly a bug rather than a claim of hardware
provenance. The Week-1 forward test says otherwise, and it is right: a label that appears
on every figure is a label a reader stops reading, and F-24 asks for a mark that means
something. So the stamp goes on when a simulator was involved and stays off when one was
not, and `render` refuses a manifest with no `vehicle` at all rather than defaulting —
defaulting to "hardware" is the one failure that puts a simulated number into the report
wearing a measurement's clothes.

Mixed provenance stamps as simulated. A figure that overlays an anchor run on simulated
sweeps is a simulated figure; the conservative direction is the only safe one, because
being over-cautious costs a caption and being under-cautious costs the result.

The vehicle is read from the run *manifest*, never from a caption written by hand and
never guessed from the frame. `eligible` applies the other half of the same discipline:
a run whose own open-loop guard failed was not generating the load its manifest claims,
so it is a diagnostic and not a data point, and it is dropped before anything is plotted.

The three hypothesis estimators are here rather than in the plotting code because each one
encodes a sign convention or an axis choice that is the actual claim being made, and those
are worth testing without a figure attached. `validation` is the Week-4 joint gate —
hardware against simulator on identical traces (F-23) — and refuses rather than drawing
half of itself while the DES output is still to come.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")  # headless: figures are produced by a script, never by a window

import matplotlib.pyplot as plt
import pandas as pd

__all__ = [
    "CELLS",
    "F23_TOLERANCE",
    "FIGURES",
    "HARDWARE_AWARE",
    "HARDWARE_BLIND",
    "MIN_VALIDATION_POINTS",
    "ROUTING_ERROR_MATERIALITY",
    "achieved_rps",
    "analysable",
    "annotations",
    "bootstrap_halfwidth",
    "by_offered_load",
    "caption",
    "drawable",
    "eligible",
    "example_frame",
    "example_sweep",
    "h1_decomposition",
    "h1_interaction",
    "h2_advantage",
    "h2_advantage_curve",
    "h3_axis",
    "h3_staleness",
    "mpr2_interaction_range",
    "mpr2_range",
    "per_node_utilization",
    "percentile",
    "render",
    "render_many",
    "render_set",
    "routing_error_rate",
    "stamp",
    "stamp_text",
    "sweep_from",
    "validation_error",
    "vehicle_of",
]

# F-24's mark. Exactly one vehicle earns it, which is what keeps it worth reading.
SIMULATED_LABEL = "SIMULATED"
VEHICLES = ("hardware", "simulator")

# The 2x2 of §"What is being tested". Named here because H1's estimator is a statement
# about these four cells and nothing else, and a fifth policy appearing in a sweep must
# not quietly become part of the decomposition.
CELLS = ("round_robin", "static_weighted", "jsq", "wjsq")

HARDWARE_AWARE = ("static_weighted", "wjsq")
HARDWARE_BLIND = ("round_robin", "jsq")

# F-23 requires a *stated* tolerance and the observed error against it. This is that
# number, and it is set by the anchors rather than chosen for roundness.
#
# Bootstrapping the four committed anchor runs (4000 resamples each) gives 95% intervals
# on their own percentiles of +-25.9% for p50 and +-24.7% for p95, at n = 180-196 measured
# requests per run. That is the resolution of the instrument the simulator is being
# compared against. A tolerance tighter than it would not be a stricter test, it would be
# an unfalsifiable one: the hardware side of the comparison does not pin its own p50 to
# better than a quarter of its value, so a simulator landing inside that interval cannot
# be distinguished from one landing exactly on it.
#
# Raising the precision is a matter of run length, not of analysis -- halving the interval
# takes roughly four times the requests per anchor. Until those runs exist, 25% is the
# honest floor, and `validation_error` reports each anchor's own interval alongside the
# error so a reader can see which side of the comparison is the limiting one.
F23_TOLERANCE = 0.25

# "Materially sooner" (§5.4) needs a threshold, or every floating-point difference counts
# as a routing error. A dispatch is counted as an error when the best admissible
# alternative was estimated to save at least this fraction of the request's actual service
# time -- relative rather than absolute, because 200 ms is decisive for a 1 s request and
# noise for a 20 s one.
ROUTING_ERROR_MATERIALITY = 0.10

# Fixed so a reported interval is reproducible from the same Parquet (F-20).
BOOTSTRAP_DRAWS = 4000
BOOTSTRAP_SEED = 20260831

# F-23 says "at least 3 operating points", and says it for a reason worth keeping visible.
MIN_VALIDATION_POINTS = 3


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. `q` is a **fraction**: 0.95, not 95.

    Deliberately the same estimator as `pipeline.loadband`, which is what places the edges
    of the §5.5 band. A figure that annotated that band while computing its percentiles by
    linear interpolation would disagree with it by a few milliseconds at every point, and
    the disagreement would be invisible and permanent. A test pins the two equal rather
    than one module importing the other's privates.

    I first wrote this taking a percent, and the pinning test caught it immediately: two
    functions of the same name in one codebase disagreeing about their unit is a trap that
    fails silently in the direction of returning the minimum. The unit here follows the
    older function rather than the other way round.

    The one intended difference is the empty case. `loadband` returns 0.0, because there a
    point with no completions is still a point on the load axis and is flagged separately.
    Here an empty list means a figure is about to plot a latency for a run that retired
    nothing, and 0.0 would draw as a suspiciously fast operating point.
    """
    if not values:
        raise ValueError("no values to take a percentile of")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def analysable(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows a latency statistic may be computed from, and only those.

    Three exclusions, each of which produces a plausible wrong number if skipped:

    **Warmup.** `is_warmup` came from the trace's `intended_offset_s`, not from wall-clock,
    so it discards the same requests in a hardware run and a simulated one. Averaging it
    in reports the cold cache as if it were steady state.

    **Failures.** A timeout's `e2e_ms` is the timeout, not a service time. Failed requests
    stay as rows — the failure rate is a result — but never enter a latency percentile.

    **Rows with no worker record.** `join` writes 0.0 rather than null for the worker-local
    durations there, because C-5 types them as numbers. A zero service time averaged into
    a service-time figure is the single most convincing wrong number this pipeline can
    produce, so those rows are dropped from anything worker-local.
    """
    ok = frame[(~frame["is_warmup"]) & (frame["status"] == "ok")]
    return ok[~((ok["service_ms"] == 0.0) & (ok["queue_wait_ms"] == 0.0))]


def achieved_rps(rows: pd.DataFrame) -> float:
    """Completions per second across the window these rows span.

    Client-local start to finish and nothing else: the span runs from the first intended
    offset to the last delivery, both of which are the client's own numbers. No worker or
    scheduler timestamp appears, so this survives unsynchronised clocks.
    """
    if len(rows) < 2:
        return 0.0
    start = float(rows["intended_offset_s"].min())
    end = float((rows["intended_offset_s"] + rows["e2e_ms"] / 1e3).max())
    span = end - start
    return len(rows) / span if span > 0 else 0.0


def by_offered_load(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per run: offered rate in, latency percentiles and achieved rate out.

    The unit of the load axis is a *run*, not a request. Every run in the set replayed one
    trace at one rate scale, so its requests are not independent samples of different
    loads — pooling them and binning by arrival rate would smear the operating points
    together, which is precisely the structure §5.5 needs kept apart.
    """
    out = []
    for run_id, rows in analysable(frame).groupby("run_id", sort=False):
        e2e = rows["e2e_ms"].astype(float).tolist()
        queue = rows["queue_wait_ms"].astype(float).tolist()
        out.append(
            {
                "run_id": run_id,
                "policy": rows["policy"].iloc[0],
                "vehicle": rows["vehicle"].iloc[0],
                "R": float(rows["R"].iloc[0]),
                "offered_rps": float(rows["lambda"].iloc[0]),
                "achieved_rps": achieved_rps(rows),
                "n": len(rows),
                "p50_ms": percentile(e2e, 0.50),
                "p95_ms": percentile(e2e, 0.95),
                "p99_ms": percentile(e2e, 0.99),
                "queue_wait_p50_ms": percentile(queue, 0.50),
                "queue_wait_p95_ms": percentile(queue, 0.95),
                "routing_error_rate": routing_error_rate(rows),
            }
        )
    return pd.DataFrame(out).sort_values("offered_rps", ignore_index=True)


def routing_error_rate(rows: pd.DataFrame) -> float | None:
    """§5.4 — the fraction of dispatches an alternative node would have finished sooner.

    `None`, not `0.0`, when no request in the set carries a scheduler decision. A run
    driven by the fixture scheduler writes no decision record, and reporting zero there
    would state that routing was perfect when in fact nothing about routing was observed.
    The two are opposite claims and they must not share a value.
    """
    decided = rows[rows["routing_error_ms"].notna()]
    if decided.empty:
        return None
    material = decided["routing_error_ms"].astype(float) >= (
        ROUTING_ERROR_MATERIALITY * decided["service_ms"].astype(float)
    )
    return float(material.sum()) / len(decided)


def per_node_utilization(frame: pd.DataFrame, *, slots_per_node: int | None = None) -> pd.DataFrame:
    """§5.4 — how busy each node was, from worker-local service time only.

    `busy_ratio` is the node's total service time divided by the wall span it served
    across: the mean number of requests **in service concurrently**. On a node running
    `--parallel 4` it can legitimately reach 4.0, so it is not a fraction and is not named
    like one. Passing `slots_per_node` divides by the slot count and yields the fraction
    of engine capacity used, which is the number §5.4 means by utilization; without it the
    ratio is reported honestly as a ratio rather than silently rescaled by a slot count
    guessed from the frame.

    Every term is worker-local, so nothing here subtracts one host's clock from another's.

    **It groups on `chosen_node`, which is the only node identity C-5 carries**, and that
    column is null for a run driven by the fixture scheduler. So this returns an empty
    table for the four committed anchor runs, and will populate the moment a real
    scheduler writes decision records. That is a property of the frozen contract rather
    than of this function: the client log knows the responding node and the worker log
    knows its own, but C-5 keeps neither, so a run with no scheduler log cannot say which
    node did the work. Adding `served_by` to C-5 would close it and is raised as a
    proposed amendment rather than made here, because the six artifacts froze at the end
    of Week 1 and changing one is a joint decision.
    """
    rows = analysable(frame)
    out = []
    for node_id, at_node in rows.groupby("chosen_node", sort=True):
        start = float(at_node["intended_offset_s"].min())
        end = float((at_node["intended_offset_s"] + at_node["e2e_ms"] / 1e3).max())
        span_s = end - start
        service_s = float(at_node["service_ms"].sum()) / 1e3
        busy_ratio = service_s / span_s if span_s > 0 else 0.0
        record = {
            "node_id": node_id,
            "n": len(at_node),
            "service_s": service_s,
            "span_s": span_s,
            "busy_ratio": busy_ratio,
        }
        if slots_per_node:
            record["utilization"] = busy_ratio / slots_per_node
        out.append(record)

    # Named columns even when nothing survived the exclusions. An empty frame with no
    # columns raises `KeyError` on the first thing a caller asks it for, which reads as a
    # bug in the caller rather than as "this run retired nothing".
    columns = ["node_id", "n", "service_s", "span_s", "busy_ratio"]
    if slots_per_node:
        columns.append("utilization")
    return pd.DataFrame(out, columns=columns)


def vehicle_of(manifest: dict[str, Any]) -> str:
    """The vehicle a run was produced by, refusing to guess.

    A manifest with no `vehicle` is an error rather than a default. Defaulting either way
    is unsafe, but the two are not symmetric: defaulting to "hardware" publishes a
    simulated number as a measurement, which is the exact failure F-24 exists to prevent.
    """
    vehicle = manifest.get("vehicle")
    if vehicle is None:
        raise ValueError(
            "manifest carries no 'vehicle', so the figure cannot say whether it is a "
            "measurement or a simulation — F-24 turns on this field and defaulting it "
            "would label a simulated figure as real"
        )
    if vehicle not in VEHICLES:
        raise ValueError(f"vehicle {vehicle!r} is not one of {list(VEHICLES)}")
    return vehicle


def stamp_text(vehicles: Iterable[str]) -> str:
    """The label a figure drawn from these vehicles must carry; empty for pure hardware.

    Mixed provenance stamps as simulated. An anchor run overlaid on simulated sweeps is a
    simulated figure, and the conservative direction is the only safe one: being
    over-cautious costs a caption, being under-cautious costs the claim.
    """
    seen = {vehicle_of({"vehicle": v}) for v in vehicles}
    if not seen:
        raise ValueError("no vehicle to stamp from")
    return "" if seen == {"hardware"} else SIMULATED_LABEL


def stamp(fig: Any, vehicles: Iterable[str]) -> str:
    """Write F-24's mark onto the figure if it is owed one, and return the text written.

    Returned as well as drawn so a caller — or a test — can assert what a figure claims
    about its own provenance without reading pixels back out of a PNG.
    """
    text = stamp_text(vehicles)
    if text:
        fig.text(0.99, 0.01, text, ha="right", va="bottom", fontsize=9, color="#b00020")
    return text


def eligible(manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The runs a figure may be drawn from: the ones that passed their own validity guard.

    A run whose open-loop guard tripped — send lag drifted, requests were dropped — was
    not offering the load its manifest says it was offering. Its numbers are real, but
    they describe a different experiment from the one labelled on the axis. That makes it
    a diagnostic and not a data point, and dropping it here means every figure drops it
    the same way rather than each script remembering to.
    """
    return [m for m in manifests if m.get("validity", {}).get("valid", True)]


def _finish(fig: Any, frame: pd.DataFrame, out_dir: Path, name: str) -> Path:
    stamp(fig, set(frame["vehicle"]))
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def latency_vs_load(frame: pd.DataFrame, out_dir: Path) -> Path:
    """e2e percentiles against offered rate — the queueing onset, drawn (§5.5).

    p99 is on the same axes as p50 rather than in its own panel because the onset is
    defined as a tail rising away from the tails below it, and a reader who has to switch
    panels to see that is being asked to do the comparison the figure exists to make.
    """
    points = by_offered_load(frame)
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    for column, label, marker in (
        ("p50_ms", "p50", "o"),
        ("p95_ms", "p95", "s"),
        ("p99_ms", "p99", "^"),
    ):
        ax.plot(points["offered_rps"], points[column], marker=marker, label=label)
    ax.set_xlabel("offered rate λ (req/s)")
    ax.set_ylabel("end-to-end latency (ms)")
    ax.set_title("Latency against offered load")
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "latency_vs_load")


def throughput_vs_load(frame: pd.DataFrame, out_dir: Path) -> Path:
    """Achieved against offered rate, with the line the pool would follow if it kept up.

    Saturation is read here as the point where the measured curve leaves y = x. It is the
    second of §5.5's two readings of the same edge — the other being latency drift within
    a run — and they are kept as two because agreeing is evidence and disagreeing is a
    finding.
    """
    points = by_offered_load(frame)
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    ceiling = float(points["offered_rps"].max())
    ax.plot([0, ceiling], [0, ceiling], linestyle="--", color="#999999", label="keeping up")
    ax.plot(points["offered_rps"], points["achieved_rps"], marker="o", label="achieved")
    ax.set_xlabel("offered rate λ (req/s)")
    ax.set_ylabel("achieved rate (completions/s)")
    ax.set_title("Throughput against offered load")
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "throughput_vs_load")


def bootstrap_halfwidth(
    values: list[float], q: float, *, draws: int = BOOTSTRAP_DRAWS, seed: int = BOOTSTRAP_SEED
) -> float:
    """The 95% interval on a percentile of these samples, as a fraction of the percentile.

    How well a run of a few hundred requests pins its own p50 and p95. It is reported
    beside every validation error because the comparison has two uncertain sides, and
    quoting the simulator's deviation without the anchor's own resolution invites reading
    a difference the hardware run cannot itself resolve as a simulator defect.
    """
    if len(values) < 2:
        raise ValueError("a bootstrap interval needs at least two samples")
    rng = np.random.default_rng(seed)
    sample = np.asarray(values, dtype=float)
    draws_matrix = rng.choice(sample, size=(draws, len(sample)), replace=True)
    point = percentile(values, q)
    low, high = np.percentile(np.percentile(draws_matrix, q * 100, axis=1), [2.5, 97.5])
    return float(max(high - point, point - low) / point)


def _matched_points(frame: pd.DataFrame) -> list[tuple[float, pd.DataFrame, pd.DataFrame]]:
    """Operating points where both vehicles ran, paired by offered rate.

    Paired on offered load rather than on run id, because the two vehicles name their runs
    independently. A point present on only one side is dropped and counted by the caller:
    F-23 compares like with like, and a simulator point with no hardware twin is not a
    validation point.
    """
    rows = analysable(frame)
    hardware = rows[rows["vehicle"] == "hardware"]
    simulator = rows[rows["vehicle"] == "simulator"]
    paired = []
    for offered in sorted({float(v) for v in hardware["lambda"]}):
        left = hardware[hardware["lambda"] == offered]
        right = simulator[simulator["lambda"] == offered]
        if not right.empty:
            paired.append((offered, left, right))
    return paired


def validation_error(
    frame: pd.DataFrame, *, tolerance: float = F23_TOLERANCE
) -> list[dict[str, Any]]:
    """F-23's actual criterion: p50 **and** p95 agreement, per operating point.

    The requirement is specific and easy to under-implement. It asks for agreement in p50
    *and* p95 end-to-end latency, within a stated tolerance, across at least three
    operating points, and it requires the tolerance and the observed error to be reported.
    A figure showing two curves satisfies none of that on its own, so the numbers are
    computed here and the figure annotates them.

    Fewer than three matched points is refused rather than reported. Three is not a
    stylistic minimum: with two points a simulator can be tuned to pass by construction,
    which is what the requirement is guarding against.
    """
    paired = _matched_points(frame)
    if len(paired) < MIN_VALIDATION_POINTS:
        raise ValueError(
            f"F-23 needs at least {MIN_VALIDATION_POINTS} operating points where both "
            f"vehicles ran; this set matches {len(paired)}. Fewer than three can be fitted "
            "exactly by a simulator with two free parameters, which is the failure the "
            "requirement exists to prevent"
        )

    out = []
    for offered, hardware, simulator in paired:
        record: dict[str, Any] = {"offered_rps": offered, "n_hardware": len(hardware)}
        worst = 0.0
        for q, name in ((0.50, "p50"), (0.95, "p95")):
            hw = percentile(hardware["e2e_ms"].astype(float).tolist(), q)
            sim = percentile(simulator["e2e_ms"].astype(float).tolist(), q)
            error = abs(sim - hw) / hw
            worst = max(worst, error)
            record[f"hardware_{name}_ms"] = hw
            record[f"simulator_{name}_ms"] = sim
            record[f"{name}_rel_error"] = error
            record[f"{name}_anchor_halfwidth"] = bootstrap_halfwidth(
                hardware["e2e_ms"].astype(float).tolist(), q
            )
        record["worst_rel_error"] = worst
        record["within_tolerance"] = worst <= tolerance
        out.append(record)
    return out


def validation(frame: pd.DataFrame, out_dir: Path, *, tolerance: float = F23_TOLERANCE) -> Path:
    """F-23 — the simulator against the machine, at matched operating points.

    The Week-4 joint gate. Both vehicles must be present and must have replayed the same
    trace, because F-23's whole content is that the comparison is like-for-like: a
    simulator validated against a different workload has been validated against nothing.
    Rather than draw the hardware half alone while the DES output is outstanding, this
    refuses and says which half is missing.

    Both percentiles the requirement names are drawn, the tolerance is shown as a band
    around the hardware curve rather than left in the caption, and the worst observed
    error is written onto the figure. A reader should not have to consult a separate table
    to see whether the gate passed.
    """
    vehicles = set(frame["vehicle"])
    if vehicles != {"hardware", "simulator"}:
        raise ValueError(
            f"F-23 compares two vehicles; this set has {sorted(vehicles)}. Join the "
            "simulator run directory into the same set and re-render — a validation "
            "figure drawn from one vehicle validates nothing"
        )
    traces = sorted(set(frame["trace_sha256"]))
    if len(traces) > 1:
        raise ValueError(
            f"the two vehicles replayed {len(traces)} different traces {traces}; F-23 "
            "requires an identical trace on both sides, so this comparison would be part "
            "simulator error and part workload difference"
        )

    errors = validation_error(frame, tolerance=tolerance)
    offered = [record["offered_rps"] for record in errors]

    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.2), sharex=True)
    for ax, name in zip(axes, ("p50", "p95"), strict=True):
        hardware = [record[f"hardware_{name}_ms"] for record in errors]
        simulator = [record[f"simulator_{name}_ms"] for record in errors]
        ax.fill_between(
            offered,
            [value * (1 - tolerance) for value in hardware],
            [value * (1 + tolerance) for value in hardware],
            color="#cccccc",
            alpha=0.5,
            label=f"±{tolerance:.0%} tolerance",
        )
        ax.plot(offered, hardware, marker="o", label="hardware")
        ax.plot(offered, simulator, marker="^", label="simulator")
        ax.set_xlabel("offered rate λ (req/s)")
        ax.set_ylabel(f"{name} end-to-end latency (ms)")
        ax.set_title(name)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)

    worst = max(record["worst_rel_error"] for record in errors)
    verdict = "within tolerance" if all(r["within_tolerance"] for r in errors) else "OUTSIDE"
    fig.suptitle(
        f"F-23 — simulator against hardware, identical trace: "
        f"worst error {worst:.1%} against ±{tolerance:.0%} ({verdict})"
    )
    return _finish(fig, frame, out_dir, "validation")


# --------------------------------------------------------------------------------------
# The hypothesis estimators. Each is the claim, stated as arithmetic.
# --------------------------------------------------------------------------------------


def h1_interaction(mean_latency_ms: dict[str, float]) -> float:
    """H1's interaction term, over the 2x2 and nothing else.

    `(WJSQ - JSQ) - (StaticWeighted - RoundRobin)`: what calibration buys once the policy
    is already queue-aware, minus what it buys when the policy is queue-blind. The order
    matters more than it looks. Both bracketed terms are negative when calibration helps,
    so getting the subtraction backwards produces a number of the same magnitude and the
    opposite sign — and the headline result reverses while still looking like a result.

    A missing cell is refused rather than dropped. Three policies cannot separate
    hardware-knowledge from queue-knowledge; they can only be ranked. Silently working
    with what is present is exactly how a ladder ends up reported as a factorial.
    """
    missing = [cell for cell in CELLS if cell not in mean_latency_ms]
    if missing:
        raise KeyError(
            f"the H1 decomposition is a 2x2 over {list(CELLS)} and is missing {missing}; "
            "without every cell this is a ranking of policies, not a decomposition of "
            "where the gain comes from"
        )
    queue_aware = mean_latency_ms["wjsq"] - mean_latency_ms["jsq"]
    queue_blind = mean_latency_ms["static_weighted"] - mean_latency_ms["round_robin"]
    return queue_aware - queue_blind


def h2_advantage_curve(sweep: pd.DataFrame) -> list[dict[str, float]]:
    """The advantage of hardware-aware routing as a function of *R* — H2's whole claim.

    H2 says that advantage is non-monotonic in *R*: it rises, peaks, then falls back
    toward zero as the best policy converges on thresholding. That is a claim about a
    curve, so a sweep holding one value of *R* is refused: it cannot be evidence for or
    against non-monotonicity, and reporting it as a point estimate is how MPR-2's
    *range* silently becomes a figure.
    """
    for column in ("R", "policy", "mean_latency_ms"):
        if column not in sweep.columns:
            raise ValueError(f"an H2 sweep needs a {column!r} column; got {list(sweep.columns)}")
    r_values = sorted({float(v) for v in sweep["R"]})
    if len(r_values) < 2:
        raise ValueError(
            f"H2 is a claim about the shape of a curve in R and this sweep holds "
            f"R = {r_values}; one point cannot be non-monotonic"
        )

    rows = []
    for r_value in r_values:
        at_r = sweep[sweep["R"] == r_value]
        aware = at_r[at_r["policy"].isin(HARDWARE_AWARE)]["mean_latency_ms"]
        blind = at_r[at_r["policy"].isin(HARDWARE_BLIND)]["mean_latency_ms"]
        if aware.empty or blind.empty:
            raise ValueError(
                f"R = {r_value} has no hardware-aware or no hardware-blind policy, so the "
                "advantage at that point is undefined rather than zero"
            )
        # Positive means hardware-awareness helped: the best aware policy finished sooner
        # than the best blind one. Best-against-best, because H2 is about what awareness
        # is worth at its best, not about the average of a policy set I chose.
        rows.append({"R": r_value, "advantage_ms": float(blind.min() - aware.min())})
    # A list of points rather than a frame: the curve is short, it is read point by point
    # when the shape is argued about, and it goes into the report as a table of numbers.
    return rows


def mpr2_interaction_range(sweep: pd.DataFrame) -> dict[str, Any]:
    """MPR-2, stated in the form MPR-2 is defined in: H1's 2x2 **across** the *R* range.

    §7 is precise about the shape of this result — "the 2x2 decomposition (H1) across the
    synthesized heterogeneity range of F-9a, reported as a range rather than a single
    figure". `h1_interaction` answers the 2x2 at one operating point, which is the
    ingredient rather than the deliverable. This evaluates it at every *R* the sweep holds
    and reports the interval, so what reaches the report is a range with the *R* values
    that produced its ends attached.

    The interval matters more than its midpoint. An interval that straddles zero says the
    redundancy H1 claims is not established across the range — a publishable negative
    result, and a different statement from a mean interaction that happens to sit near
    zero. `sign_consistent` names that case rather than leaving a reader to infer it from
    two numbers of opposite sign.
    """
    for column in ("R", "policy", "mean_latency_ms"):
        if column not in sweep.columns:
            raise ValueError(f"MPR-2 needs a {column!r} column; got {list(sweep.columns)}")

    by_r: dict[float, float] = {}
    for r_value in sorted({float(v) for v in sweep["R"]}):
        at_r = sweep[sweep["R"] == r_value]
        cells = {
            policy: float(at_r[at_r["policy"] == policy]["mean_latency_ms"].mean())
            for policy in CELLS
            if not at_r[at_r["policy"] == policy].empty
        }
        by_r[r_value] = h1_interaction(cells)

    low_r = min(by_r, key=lambda r: by_r[r])
    high_r = max(by_r, key=lambda r: by_r[r])
    return {
        "interaction_by_r": by_r,
        "low": by_r[low_r],
        "high": by_r[high_r],
        "low_at_r": low_r,
        "high_at_r": high_r,
        "sign_consistent": (by_r[low_r] < 0) == (by_r[high_r] < 0),
    }


def h3_axis(sweep: pd.DataFrame, *, autocorr_time_s: float) -> pd.Series:
    """H3's x-axis: estimate age in units of the node's own autocorrelation time.

    Raw seconds would make the result a property of the heartbeat interval I happened to
    configure. Divided by tau it is a property of the process, which is what makes the
    finding transferable to a pool with different hardware — and it is the reason tau was
    measured in Week 2 rather than assumed.
    """
    if "staleness_s" not in sweep.columns:
        raise ValueError(f"an H3 sweep needs a 'staleness_s' column; got {list(sweep.columns)}")
    if autocorr_time_s <= 0:
        raise ValueError(
            f"autocorr_time_s = {autocorr_time_s}; tau is the unit of this axis and a "
            "non-positive one means it was never measured. Take it from the C-3 snapshot"
        )
    # The axis, and only the axis. Returning the whole sweep with a column bolted on
    # would let a caller plot `staleness_s` by habit and still believe they had divided
    # by tau; a named Series can only be one thing.
    axis = sweep["staleness_s"].astype(float) / autocorr_time_s
    axis.name = "estimate_age_over_tau"
    return axis


# --------------------------------------------------------------------------------------
# The hypothesis figures. The estimators above, drawn.
# --------------------------------------------------------------------------------------


def sweep_from(frame: pd.DataFrame) -> pd.DataFrame:
    """A run set reduced to the shape the hypothesis estimators read: one row per run.

    The estimators take `R`, `policy`, `staleness_s` and `mean_latency_ms`, and they take
    them per *run* rather than per request. That unit is not a convenience: every run
    replayed one trace under one policy at one R and one staleness, so its requests are
    correlated with each other and are not independent samples of the condition. Pooling
    requests across runs and grouping afterwards would let a long run outvote a short one
    inside a cell that is supposed to be one observation.

    `mean_latency_ms` rather than a percentile because H1's interaction is a difference of
    differences, and differences of percentiles do not decompose: the p95 of a mixture is
    not a function of the p95s of its parts. The percentile is carried alongside for the
    figures that report distribution shape rather than decompose it.
    """
    out = []
    for run_id, rows in analysable(frame).groupby("run_id", sort=False):
        e2e = rows["e2e_ms"].astype(float)
        out.append(
            {
                # tau is carried through when the caller injected it, because it belongs
                # to the node class rather than the run and H3 has nowhere else to read
                # it from once the frame has been reduced to one row per run.
                **({"tau_s": float(rows["tau_s"].iloc[0])} if "tau_s" in rows else {}),
                "run_id": run_id,
                "vehicle": rows["vehicle"].iloc[0],
                "policy": rows["policy"].iloc[0],
                "R": float(rows["R"].iloc[0]),
                "staleness_s": float(rows["staleness_s"].iloc[0]),
                "lambda": float(rows["lambda"].iloc[0]),
                "n": len(rows),
                "mean_latency_ms": float(e2e.mean()),
                "p95_ms": percentile(e2e.tolist(), 0.95),
                "routing_error_rate": routing_error_rate(rows),
            }
        )
    if not out:
        raise ValueError(
            "no analysable rows in this run set: every request was warmup, failed, or "
            "carried no worker record, so there is nothing to decompose"
        )
    return pd.DataFrame(out)


def _cells_at(sweep: pd.DataFrame) -> dict[str, float]:
    """The 2x2's four cells, averaged over whatever else the sweep holds."""
    return {
        policy: float(sweep[sweep["policy"] == policy]["mean_latency_ms"].mean())
        for policy in CELLS
        if not sweep[sweep["policy"] == policy].empty
    }


def _r_axis(ax: Any, r_values: list[float]) -> None:
    """Label the R axis at the values actually measured, as plain ratios.

    Log scale because the claim is about a range that spans multiples rather than
    increments, and because consumer heterogeneity is quoted as a ratio. Matplotlib's
    default log ticks render that as "2 x 10^0", which is a worse way of writing 2 and
    puts minor ticks where no run exists. The measured values are the only meaningful
    positions on this axis, so they are the only ones labelled.
    """
    ax.set_xscale("log")
    ax.set_xticks(r_values)
    ax.set_xticklabels([f"{r:g}" for r in r_values])
    ax.minorticks_off()
    ax.set_xlabel("heterogeneity ratio R")


def h1_decomposition(frame: pd.DataFrame, out_dir: Path) -> Path:
    """H1 as an interaction plot: two lines, and whether they are parallel.

    This is the standard drawing of a 2x2 and it is the right one here, because H1 is a
    claim about *non-parallelism* and nothing else. Each line is what calibration buys:
    the upper one with a queue-blind policy (round_robin to static_weighted), the lower
    one with a queue-aware policy (jsq to wjsq). Parallel lines say calibration buys the
    same amount either way. A flatter queue-aware line is H1 confirmed, and it is legible
    without reading the number off the axis.

    Grouped bars would show the same four values and hide the only comparison that
    matters, because the eye compares heights within a group rather than slopes across
    one.
    """
    sweep = sweep_from(frame)
    cells = _cells_at(sweep)
    interaction = h1_interaction(cells)  # raises if the 2x2 is incomplete

    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    x = [0, 1]
    ax.plot(
        x,
        [cells["round_robin"], cells["static_weighted"]],
        marker="o",
        label="queue-blind (round_robin \u2192 static_weighted)",
    )
    ax.plot(
        x,
        [cells["jsq"], cells["wjsq"]],
        marker="s",
        label="queue-aware (jsq \u2192 wjsq)",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(["hardware-blind", "hardware-aware"])
    ax.set_ylabel("mean end-to-end latency (ms)")
    ax.set_title("H1: what calibration buys, with and without queue-awareness")
    # The sign is the finding, so it is spelled out rather than left to the reader to
    # infer from two line slopes they have to eyeball.
    verdict = "redundant" if interaction > 0 else "independent signal"
    ax.annotate(
        f"interaction = {interaction:+.1f} ms  ({verdict})",
        xy=(0.5, 0.02),
        xycoords="axes fraction",
        ha="center",
        fontsize=9,
    )
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "h1_decomposition")


def h2_advantage(frame: pd.DataFrame, out_dir: Path) -> Path:
    """H2: the advantage of hardware-awareness against R, and whether it turns over.

    R is on a log axis because the claim is about a shape across a range that spans
    multiples, not increments, and because consumer heterogeneity is quoted as a ratio.
    The peak is marked rather than left to be read off, since "it rises, peaks, then
    falls" is the entire hypothesis and the peak's R is the number that goes in the
    abstract.
    """
    curve = h2_advantage_curve(sweep_from(frame))  # raises on a single-R sweep
    r_values = [point["R"] for point in curve]
    advantage = [point["advantage_ms"] for point in curve]

    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.plot(r_values, advantage, marker="o")
    ax.axhline(0.0, linestyle="--", color="#999999", linewidth=1)
    peak = max(range(len(advantage)), key=lambda i: advantage[i])
    # Below and to the right of the marker: above it collides with the title, which is
    # exactly where a peak near the top of the range puts it.
    ax.annotate(
        f"peak {advantage[peak]:.0f} ms at R = {r_values[peak]:g}",
        xy=(r_values[peak], advantage[peak]),
        xytext=(8, -14),
        textcoords="offset points",
        fontsize=9,
    )
    _r_axis(ax, r_values)
    ax.set_ylabel("best aware minus best blind (ms)")
    ax.set_title("H2: the advantage of hardware-aware routing against R")
    ax.margins(y=0.15)
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "h2_advantage")


def mpr2_range(frame: pd.DataFrame, out_dir: Path) -> Path:
    """MPR-2: H1's interaction at every R, reported as a range rather than a figure.

    The deliverable is the interval, so the interval is what the figure draws: a shaded
    band between the extremes with the R values that produced them labelled. An interval
    straddling zero is a different result from a mean interaction near zero, and the two
    are easy to confuse in a table of numbers, so the zero line is drawn and the band's
    relationship to it is stated in the title.
    """
    result = mpr2_interaction_range(sweep_from(frame))
    by_r = result["interaction_by_r"]
    r_values = sorted(by_r)
    values = [by_r[r] for r in r_values]

    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.fill_between(
        r_values, min(values), max(values), alpha=0.12, color="#4477aa", label="reported range"
    )
    ax.plot(r_values, values, marker="o", color="#4477aa")
    ax.axhline(0.0, linestyle="--", color="#999999", linewidth=1)
    for label, key in (("low", "low"), ("high", "high")):
        ax.annotate(
            f"{result[key]:+.1f} at R = {result[f'{key}_at_r']:g}",
            xy=(result[f"{key}_at_r"], result[key]),
            xytext=(6, -12 if label == "low" else 6),
            textcoords="offset points",
            fontsize=9,
        )
    _r_axis(ax, r_values)
    ax.set_ylabel("H1 interaction (ms)")
    held = "one sign throughout" if result["sign_consistent"] else "straddles zero"
    ax.set_title(f"MPR-2: the H1 interaction across the R range ({held})")
    ax.margins(y=0.2)
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "mpr2_range")


def h3_staleness(frame: pd.DataFrame, out_dir: Path) -> Path:
    """H3: routing quality against estimate age, in units of the node's own tau.

    The x-axis is age over tau rather than age in seconds, which is what makes the result
    a property of the process instead of a property of the heartbeat interval configured
    on the day. `x = 1` is drawn because that is where the hypothesis says degradation
    should already be underway: an estimate as old as the autocorrelation time carries
    little information about the node's present state.

    Routing error rate is the dependent variable rather than latency, because H3 is a
    claim about decision quality given the information available, and latency also moves
    with load. Runs whose scheduler wrote no decision record report `None` and are absent
    here rather than plotted as perfect routing.
    """
    sweep = sweep_from(frame)
    if "tau_s" not in sweep.columns:
        raise ValueError(
            "an H3 figure needs the measured autocorrelation time: pass "
            "`autocorr_time_s` to render_set, or --tau-s on the command line. Take it "
            "from the C-3 snapshot for the node class this set ran on, and do not "
            "substitute the heartbeat interval, which is a setting rather than a "
            "measurement"
        )
    tau = float(sweep["tau_s"].iloc[0])
    sweep = sweep.assign(estimate_age_over_tau=h3_axis(sweep, autocorr_time_s=tau))

    plotted = sweep[sweep["routing_error_rate"].notna()]
    if plotted.empty:
        raise ValueError(
            "no run in this set carries a scheduler decision record, so routing error "
            "rate is unobserved rather than zero. A run driven by the fixture scheduler "
            "cannot answer H3"
        )

    # One point per (policy, age), averaged over whatever else the sweep crossed. A set
    # that also swept R holds several runs at each age, and drawing a line through them
    # in x-order would connect points that differ in R while implying they differ in
    # staleness. H3 is a claim about the age axis alone, so the other axes are collapsed
    # here rather than smuggled into the line.
    points = (
        plotted.groupby(["policy", "estimate_age_over_tau"], as_index=False)["routing_error_rate"]
        .mean()
        .sort_values("estimate_age_over_tau")
    )

    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    for policy, rows in points.groupby("policy", sort=True):
        ax.plot(
            rows["estimate_age_over_tau"],
            rows["routing_error_rate"],
            marker="o",
            label=str(policy),
        )
    ax.axvline(1.0, linestyle="--", color="#999999", linewidth=1)
    ax.annotate(
        "age = \u03c4",
        xy=(1.0, 0.98),
        xycoords=("data", "axes fraction"),
        ha="left",
        va="top",
        fontsize=9,
    )
    ax.set_xlabel(f"estimate age / \u03c4    (\u03c4 = {tau:g} s, measured)")
    ax.set_ylabel("routing error rate")
    ax.set_title("H3: routing quality against the age of the estimate")
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "h3_staleness")


# Every figure the pipeline can draw, keyed by the name `--only` takes. Defined here
# rather than beside the first one because the hypothesis figures below it are the
# study's actual output, and a registry that lists three characterisation plots reads
# like the whole set.
FIGURES = {
    "latency-vs-load": latency_vs_load,
    "throughput-vs-load": throughput_vs_load,
    "validation": validation,
    "h1-decomposition": h1_decomposition,
    "h2-advantage": h2_advantage,
    "mpr2-range": mpr2_range,
    "h3-staleness": h3_staleness,
}


# --------------------------------------------------------------------------------------
# Rendering a frame that arrived with its manifests
# --------------------------------------------------------------------------------------


CAPTION_PREFIX = "run set: "


def caption(fig: Any) -> str:
    """What this figure is a figure *of*, read back off the figure itself.

    Read back rather than remembered by the caller, because the point of the caption is
    that it travels with the image. A PNG dropped into the report carries its own answer
    to "which run set is this?", and a figure that lost its caption fails here rather
    than in a comparison six weeks later between two plots of different models.
    """
    for text in fig.texts:
        body = text.get_text()
        if body.startswith(CAPTION_PREFIX):
            return body
    return ""


def annotations(fig: Any) -> str:
    """Every piece of text the figure carries, as one string.

    The F-24 stamp is checked through this rather than by inspecting the artist that drew
    it: what matters is that a reader of the finished image sees the word, not that a
    particular call was made.
    """
    parts = [text.get_text() for text in fig.texts]
    for ax in fig.get_axes():
        parts.extend([ax.get_title(), ax.get_xlabel(), ax.get_ylabel()])
    return "\n".join(part for part in parts if part)


def _run_set_name(frame: pd.DataFrame) -> str:
    """What this figure is a figure *of*, so two replications can be told apart.

    The model set is a replication axis: the same sweep run under a different model is a
    different result, not more samples of the same one. A figure that does not name its
    run set cannot be placed beside the one that replicates it, so the name is drawn from
    the frame rather than left to a filename.
    """
    if "model" in frame.columns:
        names = sorted({str(v) for v in frame["model"]})
    else:
        names = sorted({str(v) for v in frame["run_id"]})
    return ", ".join(names)


def render(frame: pd.DataFrame, *, manifest: dict[str, Any]) -> Any:
    """One figure from one run's records, stamped from that run's manifest.

    The vehicle comes from the manifest and never from a caption typed by hand, which is
    the whole of F-24's mechanism: labelling by hand fails exactly once, in the final
    report, and by then nobody can tell which figures were checked.
    """
    return render_many(frame, manifests=[manifest])


def render_many(frame: pd.DataFrame, *, manifests: list[dict[str, Any]]) -> Any:
    """One figure from several runs, stamped from all of their manifests together.

    Every manifest is read, not just the first: a figure that overlays an anchor run on
    simulated sweeps takes the simulated stamp, and it can only do that if the provenance
    of each contributing run reaches this point.
    """
    if not manifests:
        raise ValueError("a figure has to come from at least one run; no manifests were given")
    vehicles = [vehicle_of(m) for m in manifests]

    rows = analysable(frame)
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    if len(rows):
        ax.plot(
            rows["intended_offset_s"].astype(float),
            rows["e2e_ms"].astype(float),
            marker=".",
            linestyle="none",
        )
    ax.set_xlabel("intended offset into the run (s)")
    ax.set_ylabel("end-to-end latency (ms)")
    ax.grid(alpha=0.3)
    fig.text(
        0.01,
        0.01,
        f"{CAPTION_PREFIX}{_run_set_name(frame)}",
        ha="left",
        va="bottom",
        fontsize=8,
        color="#555555",
    )
    stamp(fig, vehicles)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------------------
# Fixtures. Small, hand-written, and shaped like the real thing.
# --------------------------------------------------------------------------------------


def example_frame() -> pd.DataFrame:
    """A C-5-shaped frame small enough to reason about, with the exclusions represented.

    Carries warmup rows, a failed request and a row with no worker record on purpose:
    those are the three things `measured` exists to drop, and a fixture that omits them
    cannot demonstrate that it does.
    """
    rows = [
        # run, offset, e2e, status, service, queue, warmup
        ("anchor_a", 1.0, 900.0, "ok", 800.0, 40.0, True),
        ("anchor_a", 2.0, 850.0, "ok", 780.0, 30.0, True),
        ("anchor_a", 12.0, 1200.0, "ok", 1000.0, 120.0, False),
        ("anchor_a", 13.0, 1350.0, "ok", 1100.0, 160.0, False),
        ("anchor_a", 14.0, 30000.0, "timeout", 0.0, 0.0, False),
        ("anchor_a", 15.0, 1400.0, "ok", 0.0, 0.0, False),
        ("anchor_a", 16.0, 1500.0, "ok", 1180.0, 210.0, False),
    ]
    return pd.DataFrame(
        [
            {
                "run_id": run_id,
                "policy": "round_robin",
                "lambda": 1.0,
                "staleness_s": 0.0,
                "R": 1.0,
                "node_count": 1,
                "req_id": f"r{i:06d}",
                "bucket_id": "p128_o64",
                "prompt_len": 128,
                "output_len": 64,
                "priority": 0,
                "intended_offset_s": offset,
                "send_lag_ms": 0.4,
                "e2e_ms": e2e,
                "status": status,
                "chosen_node": "n1",
                "decide_us": None,
                "chosen_queue_depth": None,
                "chosen_est_age_ms": None,
                "best_alt_node": None,
                "best_alt_est_service_ms": None,
                "routing_error_ms": None,
                "queue_wait_ms": queue,
                "prefill_ms": None,
                "decode_ms": None,
                "service_ms": service,
                "transport_residual_ms": e2e - service - queue,
                "is_warmup": warmup,
                "vehicle": "hardware",
                "trace_sha256": "0" * 64,
            }
            for i, (run_id, offset, e2e, status, service, queue, warmup) in enumerate(rows)
        ]
    )


def example_sweep() -> pd.DataFrame:
    """A sweep shaped the way H2 and H3 need it: the 2x2 crossed with R and staleness.

    Deliberately non-monotonic in R — the advantage rises to a peak and falls away again —
    because a fixture that was monotonic would let an estimator that reports the wrong
    shape pass anyway.
    """
    rows = []
    for r_value, blind, aware in ((1.0, 100.0, 99.0), (2.0, 120.0, 95.0), (8.0, 160.0, 155.0)):
        for staleness_s in (0.0, 10.0, 60.0):
            decay = 1.0 + staleness_s / 200.0
            rows.extend(
                [
                    ("round_robin", blind),
                    ("jsq", blind - 5.0),
                    ("static_weighted", aware * decay),
                    ("wjsq", (aware - 3.0) * decay),
                ]
            )
            rows[-4:] = [
                {
                    "R": r_value,
                    "staleness_s": staleness_s,
                    "policy": policy,
                    "mean_latency_ms": value,
                }
                for policy, value in rows[-4:]
            ]
    return pd.DataFrame(rows)


def drawable(frame: pd.DataFrame) -> list[str]:
    """The figures whose inputs this run set actually holds.

    Each rule is the condition under which the figure's estimator would refuse anyway.
    Naming them here means the default run draws what it can instead of failing on the
    first thing it cannot, while asking for one of them BY NAME still raises and says
    why. Silence and refusal are both correct; which one you get should depend on
    whether you asked.
    """
    names = ["latency-vs-load", "throughput-vs-load"]
    if set(frame["vehicle"]) == {"hardware", "simulator"}:
        names.append("validation")
    policies = set(frame["policy"])
    if set(CELLS) <= policies:
        names.append("h1-decomposition")
        if frame["R"].nunique() > 1:
            names += ["h2-advantage", "mpr2-range"]
    if "tau_s" in frame.columns and frame["staleness_s"].nunique() > 1:
        names.append("h3-staleness")
    return names


def render_set(
    source: str | Path | pd.DataFrame,
    out_dir: str | Path,
    which: list[str] | None = None,
    *,
    autocorr_time_s: float | None = None,
) -> list[Path]:
    """Draw the named figures from a run set, and return what was written.

    `source` is the Parquet a run set was written to, or the frame itself. `which`
    defaults to `drawable(frame)`, so a hardware-only set at one R draws the
    characterisation plots and skips the hypothesis figures rather than failing on them.

    `autocorr_time_s` is tau, and it is a parameter rather than a column because it is
    measured once per node class in Week 2 and belongs to the hardware, not to the run.
    It is injected here so that exactly one place knows where it came from, and H3 refuses
    without it rather than defaulting to a number that would silently rescale its axis.
    """
    frame = source if isinstance(source, pd.DataFrame) else pd.read_parquet(Path(source))
    if autocorr_time_s is not None:
        frame = frame.assign(tau_s=float(autocorr_time_s))
    if which is None:
        which = drawable(frame)

    unknown = [name for name in which if name not in FIGURES]
    if unknown:
        raise ValueError(f"no such figure(s): {unknown}; known: {sorted(FIGURES)}")

    out_dir = Path(out_dir)
    return [FIGURES[name](frame, out_dir) for name in which]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="F-19/F-24 — render figures from a run set")
    ap.add_argument("parquet", type=Path, help="a run set written by `runset`")
    ap.add_argument("--out", type=Path, default=Path("figures"), help="output directory")
    ap.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help=f"render just this figure; repeatable. One of {sorted(FIGURES)}",
    )
    ap.add_argument(
        "--tau-s",
        type=float,
        metavar="SECONDS",
        help="the measured autocorrelation time for this set's node class, from its C-3 "
        "snapshot. H3's axis is estimate age divided by it, and without it the H3 figure "
        "is skipped rather than drawn against a guess",
    )
    args = ap.parse_args(argv)

    for path in render_set(args.parquet, args.out, args.only, autocorr_time_s=args.tau_s):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
