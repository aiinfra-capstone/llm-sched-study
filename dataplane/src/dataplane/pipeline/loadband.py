"""§5.5 — the controlled load band: where dispatch policy can still change p99.

Policy differences vanish at both ends of the load axis, and for two different reasons.

**Too light**, and there is no queue: every request finds a free slot the instant it
arrives, so every policy makes the same placement and every policy gets the same latency.
Nothing is being decided.

**Too heavy**, and there is nothing but queue: offered load exceeds what the pool can
retire, the backlog grows for as long as the run lasts, and end-to-end latency is
dominated by a term that no placement decision can shrink. Every policy converges again,
this time on "bad".

Between them is a band, and the spec makes finding it a prerequisite step and a reportable
characterization in its own right — because a headline result quoted at a load outside the
band is a result about the load, not about the policy. This module locates the band from
the anchor runs, which is the same set of runs F-23 validates the simulator against; one
sweep, two deliverables.

Two operational definitions, both stated here rather than tuned later:

* **Onset** (`QUEUEING_ONSET`). The lightest usable run is the reference, and the
  comparison is **p99 against the reference's p99** — like against like. A point is inside
  the band once its tail is 20% worse than the reference's tail.

  Comparing the tail to the *reference's p50* is the version I wrote first, and it is wrong
  in a way worth recording, because it looked right. The trace mixes `p128_o64` with
  `p512_o128`, so p99 sits several times above p50 from the **length spread alone**, with no
  queue anywhere: on the first hardware sweep the lightest point had p50 1603 ms and p99
  4987 ms, a ratio of 3.1, and the rule fired at the floor of every sweep it would ever see.
  The fix comes free from the anchor design — every point replays the *same trace*, so the
  length composition is identical across points by construction and a tail-to-tail
  comparison isolates the one thing that changed, which is when the requests arrived.

  If no point clears the threshold, the sweep never reached onset and the band is reported
  as unidentified rather than pinned to its lowest rung.
* **Saturation**, read two ways, because one of them is not sensitive enough on its own.
  `SATURATION_DRIFT` is a trend test: a stable queue has a stationary latency distribution
  and an unstable one climbs for as long as you keep offering load, so fit end-to-end
  latency against arrival time and call the point saturated when the fitted rise across the
  window is at least as large as the run's own p50. `SATURATION_SHORTFALL` is the direct
  reading: in an open-loop replay the offered rate is fixed by the trace, so if the pool
  retires meaningfully **less** than that over the whole run, the backlog grew — by
  definition, not by inference.

  Both are here because the first sweep on real hardware showed the trend test missing a
  point that was genuinely over the knee. At 1.80 req/s against a pool that retires about
  1.65, the excess is small enough that the backlog builds slowly, and over a 111-second run
  the fitted rise reached only 0.30 of the run's p50 — under the trend threshold, while the
  pool was visibly retiring 1.63 req/s against 1.80 offered. A level test cannot tell "slow
  but stable" from "not keeping up"; the trend test can, but not quickly. The shortfall test
  sees it immediately, and reporting both is more honest than choosing one and hoping.

Both are why the anchors are replayed open-loop and why send lag is a validity condition. A
closed-loop client cannot produce an unstable queue at all — it throttles itself — so it
would report every load as stable and place the upper edge at infinity.

**What one node cannot tell you.** With a single-node pool there is no placement to get
wrong, so these runs bound the band from physics (queueing exists here; the queue clears
here) but cannot demonstrate the thing the band is defined by: that policies *differ*
inside it. `policy_separable` is False until the pool has at least two nodes, and it stays
in the output so that a figure drawn from a one-node sweep cannot silently claim more than
the runs support.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "QUEUEING_ONSET",
    "SATURATION_DRIFT",
    "SATURATION_SHORTFALL",
    "LoadBand",
    "LoadPoint",
    "characterize",
    "point_from_run",
    "read_runs",
]

# A point's p99 this far above the *reference point's p99* means requests are waiting behind
# other requests. Tail against tail, on an identical trace — see the module docstring for
# why comparing the tail to a median is not the same test.
QUEUEING_ONSET = 1.2

# Fitted latency rise across the measurement window, as a multiple of the run's own p50.
# At 1.0 the run ends with latency about double what it started at, which no stationary
# queue does.
SATURATION_DRIFT = 1.0

# Achieved throughput this far below the offered rate means the pool did not keep up. 5% is
# above the sampling noise in a completion count over a run of a few hundred requests, and
# below any shortfall a stable queue produces — a stable pool retires exactly what it is
# offered, because the trace, not the pool, decides when requests arrive.
SATURATION_SHORTFALL = 0.95

# Under this many completed requests a percentile is a story about three numbers. Points
# below it are carried (they are still evidence the run happened) but never used to place
# an edge of the band.
MIN_SAMPLES = 30


@dataclass(frozen=True)
class LoadPoint:
    """One offered load, summarized. Every duration is the client's own monotonic clock."""

    name: str
    lambda_rps: float
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    drift_ms: float
    achieved_rps: float
    n_nodes: int
    failures: int

    @property
    def usable(self) -> bool:
        """Enough completions for a p99 to mean anything."""
        return self.n >= MIN_SAMPLES

    @property
    def climbing(self) -> bool:
        """Latency still rising when the run ended — the queue never cleared."""
        return self.p50_ms > 0 and self.drift_ms >= SATURATION_DRIFT * self.p50_ms

    @property
    def short(self) -> bool:
        """The pool retired less than the trace offered, so the backlog grew."""
        return self.lambda_rps > 0 and self.achieved_rps < SATURATION_SHORTFALL * self.lambda_rps

    @property
    def saturated(self) -> bool:
        """Either reading is enough. They are two views of one fact, not two hypotheses."""
        return self.climbing or self.short

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "lambda_rps": round(self.lambda_rps, 4),
            "n": self.n,
            "failures": self.failures,
            "p50_ms": round(self.p50_ms, 2),
            "p95_ms": round(self.p95_ms, 2),
            "p99_ms": round(self.p99_ms, 2),
            "drift_ms": round(self.drift_ms, 2),
            "achieved_rps": round(self.achieved_rps, 4),
            "saturated": self.saturated,
            "climbing": self.climbing,
            "short": self.short,
            "usable": self.usable,
        }


@dataclass(frozen=True)
class LoadBand:
    """The answer, plus enough of the working to argue with it."""

    lo_rps: float | None
    hi_rps: float | None
    reference_p50_ms: float
    reference_p99_ms: float
    points: list[LoadPoint]
    policy_separable: bool

    @property
    def identified(self) -> bool:
        return self.lo_rps is not None and self.hi_rps is not None and self.hi_rps >= self.lo_rps

    def to_dict(self) -> dict[str, Any]:
        return {
            "lo_rps": None if self.lo_rps is None else round(self.lo_rps, 4),
            "hi_rps": None if self.hi_rps is None else round(self.hi_rps, 4),
            "identified": self.identified,
            "reference_p50_ms": round(self.reference_p50_ms, 2),
            "reference_p99_ms": round(self.reference_p99_ms, 2),
            "policy_separable": self.policy_separable,
            "onset_rule": f"p99 >= {QUEUEING_ONSET} x the reference point's p99",
            "saturation_rule": (
                f"fitted rise across the window >= {SATURATION_DRIFT} x own p50, "
                f"or achieved throughput < {SATURATION_SHORTFALL} x offered"
            ),
            "points": [p.to_dict() for p in self.points],
        }

    def summary(self) -> str:
        if not self.identified:
            return (
                "load band NOT identified from these runs "
                f"(onset {self.lo_rps}, saturation edge {self.hi_rps}) — "
                "sweep wider, or run longer at each point"
            )
        line = (
            f"load band: {self.lo_rps:.2f}–{self.hi_rps:.2f} req/s "
            f"(reference p50 {self.reference_p50_ms:.0f} ms, p99 {self.reference_p99_ms:.0f} ms)"
        )
        if not self.policy_separable:
            line += (
                "; one-node pool, so this is the band's physical bound only — "
                "policy separation is not demonstrated by these runs"
            )
        return line


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile. Explicit because a run of 40 requests has no 99th anything.

    Nearest-rank rather than an interpolating definition so that a reported p99 is a
    latency some request actually had. Interpolation invents a value between two
    measurements, which is fine for a plot and wrong for a number quoted in a table.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def _drift_ms(offsets_s: list[float], latencies_ms: list[float]) -> float:
    """Fitted rise in latency across the measurement window, in ms.

    An ordinary least-squares slope times the window's own span. Least squares because the
    alternative — comparing the first and last few requests — is a difference of two noisy
    numbers, and at these sample sizes the noise is the same size as the effect.
    """
    n = len(offsets_s)
    if n < 2:
        return 0.0
    span = max(offsets_s) - min(offsets_s)
    if span <= 0:
        return 0.0
    # `span > 0` already rules out every x being equal, so sxx cannot be zero here.
    mean_x = statistics.fmean(offsets_s)
    mean_y = statistics.fmean(latencies_ms)
    sxx = sum((x - mean_x) ** 2 for x in offsets_s)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(offsets_s, latencies_ms, strict=True))
    return (sxy / sxx) * span


def _achieved_rps(ok: list[dict[str, Any]]) -> float:
    """Completions per second, measured from first send to last delivery.

    Spans the drain as well as the trace window, on purpose: a saturated run finishes
    retiring its backlog after the last request was sent, and a rate computed over the
    trace window alone would credit the pool with throughput it only reached by running
    late. Every stamp here is this client's own monotonic clock.
    """
    if len(ok) < 2:
        return 0.0
    first_send = min(float(r["actual_send_offset_s"]) for r in ok)
    last_done = max(float(r["actual_send_offset_s"]) + r["e2e_duration_ns"] / 1e9 for r in ok)
    span = last_done - first_send
    return len(ok) / span if span > 0 else 0.0


def point_from_run(manifest: dict[str, Any], records: list[dict[str, Any]]) -> LoadPoint:
    """Summarize one anchor run into one point on the load axis.

    Warmup is excluded using `intended_offset_s` against the manifest's `warmup_s` — the
    trace's own timeline, never the run's wall clock. That is the §12.2 rule that keeps the
    hardware run and the simulator run discarding the *same requests*; a warmup window
    measured from process start would discard different ones in each vehicle and the F-23
    comparison would be off by however long the engine took to load.
    """
    warmup_s = float(manifest.get("warmup_s", 0.0))
    windowed = [r for r in records if r.get("intended_offset_s", 0.0) >= warmup_s]
    ok = [r for r in windowed if r.get("status") == "ok"]
    latencies = [r["e2e_duration_ns"] / 1e6 for r in ok]
    offsets = [float(r["intended_offset_s"]) for r in ok]
    return LoadPoint(
        name=manifest.get("config", {}).get("operating_point", manifest["run_id"]),
        lambda_rps=float(manifest["lambda"]),
        n=len(ok),
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        p99_ms=_percentile(latencies, 0.99),
        drift_ms=_drift_ms(offsets, latencies),
        achieved_rps=_achieved_rps(ok),
        n_nodes=len([n for n in manifest["nodes"] if n.get("role", "pool") == "pool"]),
        failures=len(windowed) - len(ok),
    )


def characterize(points: list[LoadPoint]) -> LoadBand:
    """Place both edges of the band, or say which one the sweep did not reach.

    The reference is the lightest **usable** point, and both its p50 and its p99 are kept:
    the p99 is what the onset rule compares against, and the p50 is the context a reader
    needs to see that the reference really was unloaded.

    Taking the reference from the lightest measured point rather than from the cost model is
    deliberate. The cost model is what the simulator will be built from, and using it here
    would make the band partly a property of the thing the band exists to help validate.
    """
    if not points:
        raise ValueError("a load band is read off a sweep; got 0 operating points")
    usable = sorted((p for p in points if p.usable), key=lambda p: p.lambda_rps)
    ordered = sorted(points, key=lambda p: p.lambda_rps)
    if not usable:
        return LoadBand(
            lo_rps=None,
            hi_rps=None,
            reference_p50_ms=0.0,
            reference_p99_ms=0.0,
            points=ordered,
            policy_separable=max(p.n_nodes for p in points) > 1,
        )

    reference = usable[0]
    lo = next(
        (p.lambda_rps for p in usable if p.p99_ms >= QUEUEING_ONSET * reference.p99_ms),
        None,
    )
    stable = [p for p in usable if not p.saturated]
    hi = max((p.lambda_rps for p in stable), default=None)
    return LoadBand(
        lo_rps=lo,
        hi_rps=hi,
        reference_p50_ms=reference.p50_ms,
        reference_p99_ms=reference.p99_ms,
        points=ordered,
        policy_separable=max(p.n_nodes for p in points) > 1,
    )


def read_runs(root: str | Path) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Every run directory under `root` that holds a manifest and a client log."""
    out = []
    for manifest_path in sorted(Path(root).glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        logs = sorted(manifest_path.parent.glob("client_*.jsonl"))
        if not logs:
            continue
        records = [json.loads(line) for line in logs[0].read_text().splitlines() if line.strip()]
        out.append((manifest, records))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="§5.5 — identify the controlled load band")
    ap.add_argument("root", type=Path, help="directory of run dirs (e.g. runs/anchors)")
    ap.add_argument("--out", type=Path, help="write the characterization as JSON here")
    args = ap.parse_args(argv)

    runs = read_runs(args.root)
    if not runs:
        raise SystemExit(f"no run directories with a manifest and a client log under {args.root}")

    band = characterize([point_from_run(m, r) for m, r in runs])
    print(band.summary())
    for p in sorted(band.points, key=lambda p: p.lambda_rps):
        flags = []
        if not p.usable:
            flags.append(f"only {p.n} completions")
        if p.climbing:
            flags.append("latency still climbing")
        if p.short:
            flags.append(f"retired only {p.achieved_rps:.2f}/s")
        if p.failures:
            flags.append(f"{p.failures} failed")
        print(
            f"  {p.name:>12}  lambda={p.lambda_rps:6.2f}/s  n={p.n:<5} "
            f"p50={p.p50_ms:8.1f}  p95={p.p95_ms:8.1f}  p99={p.p99_ms:8.1f}  "
            f"drift={p.drift_ms:+8.1f} ms" + (f"   [{', '.join(flags)}]" if flags else "")
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(band.to_dict(), indent=2) + "\n")
    return 0 if band.identified else 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
