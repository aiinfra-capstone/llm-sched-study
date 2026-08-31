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

matplotlib.use("Agg")  # headless: figures are produced by a script, never by a window

import matplotlib.pyplot as plt
import pandas as pd

__all__ = [
    "CELLS",
    "FIGURES",
    "achieved_rps",
    "analysable",
    "annotations",
    "by_offered_load",
    "caption",
    "eligible",
    "example_frame",
    "example_sweep",
    "h1_interaction",
    "h2_advantage_curve",
    "h3_axis",
    "percentile",
    "render",
    "render_many",
    "render_set",
    "stamp",
    "stamp_text",
    "vehicle_of",
]

# F-24's mark. Exactly one vehicle earns it, which is what keeps it worth reading.
SIMULATED_LABEL = "SIMULATED"
VEHICLES = ("hardware", "simulator")

# The 2x2 of §"What is being tested". Named here because H1's estimator is a statement
# about these four cells and nothing else, and a fifth policy appearing in a sweep must
# not quietly become part of the decomposition.
CELLS = ("round_robin", "static_weighted", "jsq", "wjsq")


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
            }
        )
    return pd.DataFrame(out).sort_values("offered_rps", ignore_index=True)


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


def validation(frame: pd.DataFrame, out_dir: Path) -> Path:
    """F-23 — the simulator against the machine, at matched operating points.

    The Week-4 joint gate. Both vehicles must be present and must have replayed the same
    trace, because F-23's whole content is that the comparison is like-for-like: a
    simulator validated against a different workload has been validated against nothing.
    Rather than draw the hardware half alone while the DES output is outstanding, this
    refuses and says which half is missing.
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

    points = by_offered_load(frame)
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    for vehicle, marker in (("hardware", "o"), ("simulator", "^")):
        side = points[points["vehicle"] == vehicle]
        ax.plot(side["offered_rps"], side["p95_ms"], marker=marker, label=vehicle)
    ax.set_xlabel("offered rate λ (req/s)")
    ax.set_ylabel("p95 end-to-end latency (ms)")
    ax.set_title("F-23 — simulator against hardware, identical trace")
    ax.legend()
    ax.grid(alpha=0.3)
    return _finish(fig, frame, out_dir, "validation")


FIGURES = {
    "latency-vs-load": latency_vs_load,
    "throughput-vs-load": throughput_vs_load,
    "validation": validation,
}


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
        aware = at_r[at_r["policy"].isin(["static_weighted", "wjsq"])]["mean_latency_ms"]
        blind = at_r[at_r["policy"].isin(["round_robin", "jsq"])]["mean_latency_ms"]
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


def render_set(
    source: str | Path | pd.DataFrame,
    out_dir: str | Path,
    which: list[str] | None = None,
) -> list[Path]:
    """Draw the named figures from a run set, and return what was written.

    `source` is the Parquet a run set was written to, or the frame itself. `which`
    defaults to the figures whose inputs are present: `validation` is skipped unless the
    set actually holds both vehicles, so the default run does not fail on a hardware-only
    set while still refusing loudly when asked for it by name.
    """
    frame = source if isinstance(source, pd.DataFrame) else pd.read_parquet(Path(source))
    if which is None:
        which = [
            name
            for name in FIGURES
            if name != "validation" or set(frame["vehicle"]) == {"hardware", "simulator"}
        ]

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
    args = ap.parse_args(argv)

    for path in render_set(args.parquet, args.out, args.only):
        print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
