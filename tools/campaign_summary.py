#!/usr/bin/env python3
"""Per-point policy tables and the H1 interaction for one run set, with paired intervals.

`figures` draws H1 pooled over every load point in a set and without an interval. A paper
needs the table underneath: each policy at each point, and the interaction at each point,
with an interval that says whether a difference is larger than the noise that matters.

Rules, fixed before the re-analysis of the first pair was run
-------------------------------------------------------------

**Rows.** A request is measured when its intended arrival is past warmup. Latency statistics
use the measured requests whose status is ok. Failures are counted over the whole run,
warmup included, because a response lost in warmup is still a lost response.

**Resampling.** Latencies inside a run are autocorrelated (a queue carries over from one
request to the next), so resampling requests independently understates the interval. Every
draw does two things:

1. It resamples repeats with replacement, using the same repeat numbers for every policy at
   the point. Repeat k of every policy shares an arrival sequence (and, from the seeded
   campaigns on, a scheduler seed), so the repeats are paired across policies.
2. It resamples positions in the arrival sequence with a circular block bootstrap and
   applies the same positions to every policy and repeat. That keeps the pairing by arrival
   index and keeps the short-range correlation inside each block.

The block length is `max(ceil(n^(1/3)), ceil(2 * tau_int))`, where `tau_int` is the largest
integrated autocorrelation time (Sokal window, in requests) of the detrended end-to-end
latency series over the cells at the point, capped at n/5. Detrended, so a cell that is
still climbing does not set the block length for the stable ones.

When every repeat replayed the same trace with the same scheduler seed (the first pair's
campaigns), step 1 only samples hardware jitter. The summary says so in
`arrivals_independent`, and every interval from such a set is conditional on one arrival
path and one routing random stream.

**Steady state.** A cell (one policy at one point) is transient when the mean latency of the
last third of its measured arrivals is at least 10% above the first third's and the 95%
interval of that ratio excludes 1. Transient cells are reported with their numbers and
excluded from every contrast: no H1 interaction and no calibration gain is computed at a
point where any of the four 2x2 cells is transient. evidence.md section 6 already rules
that transient percentiles are not quoted beside steady-state ones.

**Primary statistic.** For H1 the primary statistic is the interaction on the log of mean
end-to-end latency, `log(wjsq/jsq) - log(static_weighted/round_robin)`, at steady-state
points. Positive means calibration buys a smaller fraction once the policy sees queue depth.
The same contrast in milliseconds, and on p50, p95 and p99, is reported as secondary. The
millisecond version is not scale-free: a difference in ms grows with the latency it is
measured on, so a queue-blind policy near its stability limit inflates it without any
change in how calibration and queue depth interact.

**Serving metrics.** TTFT here is worker-side, queue wait plus prefill. TPOT is decode time
over output tokens minus one. Delivery is not streamed, so the client never sees a first
token and neither number includes the network. SLO attainment is the share of measured
requests (failures count as misses) whose end-to-end latency, or TTFT, is within 2x and 5x
of what the fastest node in the pool takes for that request's bucket at concurrency 1 in
its cost model.

**Not a dependent variable.** `routing_error_ms` scores each decision with WJSQ's own score
on the veiled view the policy saw, so WJSQ's rate is zero by construction. It is not
reported. `queue_wait_ms` is reported for reference only: with four slots, contention shows
up as longer service time rather than as queue wait.

Usage:
  uv run --project dataplane python tools/campaign_summary.py runs/exp/<tag>/runset.parquet \\
      --out runs/exp/<tag>/summary
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import re
from pathlib import Path

import numpy as np
import pandas as pd
from dataplane.harness.manifest import Validity
from pool_load import capability, locate, pool_capacity, snapshot_index

H1_POLICIES = ("round_robin", "static_weighted", "jsq", "wjsq")
LATENCY_STATS = ("mean", "p50", "p95", "p99")
SLO_SCALES = (2.0, 5.0)
CLIMB_RATIO = 1.10
BLOCK_CAP_FRACTION = 5
SOKAL_C = 5.0
CHUNK = 250

# A simulated replay of a hardware run keeps its run id with "_sim" on the end.
_REPEAT_RE = re.compile(r"_r(\d+)(?:_sim)?$")


def where(workload: str, stale: float, lam: float) -> tuple:
    """The labels that name a point in every random stream drawn at it.

    A campaign with one workload names its points by staleness and rate, as every campaign
    did before workloads existed, so its streams and therefore its intervals are unchanged.
    """
    return (workload, stale, lam) if workload else (stale, lam)


def stream(seed: int, *labels: object) -> np.random.Generator:
    """A generator for one named piece of work, independent of what else was drawn.

    Every contrast gets its own stream keyed by the cells it compares and the point it sits
    at. Drawing them all from one generator would make a cell's interval depend on how many
    other contrasts happened to be computed first, so adding a failure under WJSQ would move
    the queue-blind interval it has nothing to do with.
    """
    key = "|".join(str(x) for x in labels).encode()
    return np.random.default_rng(
        np.random.SeedSequence([seed, int.from_bytes(hashlib.sha256(key).digest()[:8], "big")])
    )


# ---------------------------------------------------------------------------- inputs


def measured(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(~frame["is_warmup"]) & (frame["status"] == "ok")]


def manifests(runset_path: Path) -> dict[str, dict]:
    """run_id -> manifest, read from the run directories beside the run set."""
    out = {}
    for p in sorted(runset_path.parent.glob("*/manifest.json")):
        m = json.loads(p.read_text(encoding="utf-8"))
        out[m["run_id"]] = m
    return out


def validity_reasons(block: dict) -> list[str]:
    """Why the harness rejected a run, in its own words.

    Rebuilt through `Validity` rather than restated here, so the summary and the console at
    the end of a run give a reader the same sentence for the same rejection.
    """
    fields = {f.name for f in dataclasses.fields(Validity)}
    reasons = Validity(**{k: v for k, v in block.items() if k in fields}).reasons()
    return reasons or ["the harness marked this run invalid"]


def repeat_of(run_id: str) -> int:
    m = _REPEAT_RE.search(run_id)
    return int(m.group(1)) if m else 1


# ------------------------------------------------------------------------ bootstrap


def integrated_tau(x: np.ndarray) -> float:
    """Sokal-windowed integrated autocorrelation time of a detrended series, in samples."""
    x = x[~np.isnan(x)]
    n = x.size
    if n < 8:
        return 1.0
    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, x, 1)
    r = x - (slope * t + intercept)
    denom = float(np.dot(r, r))
    # A flat series has no correlation to measure. The comparison is relative because a
    # least-squares fit to a constant leaves rounding dust rather than exact zeros, and
    # dividing by dust turns every lag into a perfect correlation.
    if denom <= 1e-12 * max(1.0, float(np.dot(x, x))):
        return 1.0
    total = 1.0
    for k in range(1, n // 2):
        total += 2.0 * float(np.dot(r[: n - k], r[k:]) / denom)
        if k >= SOKAL_C * max(total, 1.0):
            break
    return max(total, 1.0)


def lag1(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if x.size < 3:
        return float("nan")
    c = x - x.mean()
    denom = float(np.dot(c, c))
    return float(np.dot(c[:-1], c[1:]) / denom) if denom > 0 else float("nan")


def block_positions(rng: np.random.Generator, draws: int, n: int, block: int) -> np.ndarray:
    """(draws, n) indices into [0, n) from a circular block bootstrap."""
    n_blocks = math.ceil(n / block)
    starts = rng.integers(0, n, size=(draws, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(draws, -1)[:, :n]


def percentile_rows(a: np.ndarray, q: float) -> np.ndarray:
    return np.nanpercentile(a, q, axis=1)


def interval(draws: np.ndarray) -> list[float]:
    d = draws[~np.isnan(draws)]
    if d.size == 0:
        return [float("nan"), float("nan")]
    lo, hi = np.percentile(d, [2.5, 97.5])
    return [float(lo), float(hi)]


def r1(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 1)


def r4(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 4)


# ---------------------------------------------------------------------------- a point


class Cell:
    """One policy at one load point: arrays over (repeat, arrival position).

    Three things a position can be, and they are not the same finding:

    **Measured.** A row with status ok. It carries its latencies.

    **Failed.** A row that came back anything else. Its latencies are absent, and it counts
    as an SLO miss, because a response that never arrived did not meet a deadline. A failed
    position inside a contrast makes that contrast undefined rather than quietly comparing
    one policy's full window with another's partial one.

    **Absent.** No row at all, past the end of that repeat's own window, which is what a
    shorter run looks like. The position is left out of that repeat everywhere, including
    the SLO rates: a request the run never sent neither met nor missed a deadline.

    A position with no row *inside* a repeat's own window is none of the three. It is a hole
    in the record, so the constructor refuses rather than filling it in.
    """

    def __init__(self, policy: str, runs: dict[int, pd.DataFrame], positions: pd.Index) -> None:
        self.policy = policy
        self.repeats = sorted(runs)
        self.runs = {rep: str(g["run_id"].iloc[0]) for rep, g in runs.items()}
        n = len(positions)
        shape = (len(self.repeats), n)
        self.e2e = np.full(shape, np.nan)
        self.ttft = np.full(shape, np.nan)
        self.tpot = np.full(shape, np.nan)
        self.present = np.zeros(shape, dtype=bool)
        self.failed = np.zeros(shape, dtype=bool)
        self.slo_e2e = {k: np.full(shape, np.nan) for k in SLO_SCALES}
        self.slo_ttft = {k: np.full(shape, np.nan) for k in SLO_SCALES}
        for i, rep in enumerate(self.repeats):
            g = runs[rep].set_index("req_id").reindex(positions)
            present = g["status"].notna().to_numpy()
            ok = (g["status"] == "ok").to_numpy()
            where = np.flatnonzero(present)
            if where.size:
                holes = [
                    str(positions[j]) for j in range(where[0], where[-1] + 1) if not present[j]
                ]
                if holes:
                    raise ValueError(
                        f"run {self.runs[rep]!r} ({policy}, repeat {rep}) has no row for "
                        f"{len(holes)} request(s) inside its own window, first {holes[0]}. "
                        "That is a hole in the record rather than a short run, and it would "
                        "silently drop those arrivals from one side of every contrast"
                    )
            e2e = g["e2e_ms"].to_numpy(dtype=float)
            ttft = (g["queue_wait_ms"] + g["prefill_ms"]).to_numpy(dtype=float)
            tpot = (g["decode_ms"] / (g["output_len"] - 1).clip(lower=1)).to_numpy(dtype=float)
            self.present[i] = present
            self.failed[i] = present & ~ok
            self.e2e[i] = np.where(ok, e2e, np.nan)
            self.ttft[i] = np.where(ok, ttft, np.nan)
            self.tpot[i] = np.where(ok, tpot, np.nan)
            ref_e2e = g["ref_service_ms"].to_numpy(dtype=float)
            ref_pre = g["ref_prefill_ms"].to_numpy(dtype=float)
            for k in SLO_SCALES:
                self.slo_e2e[k][i] = np.where(
                    present, np.where(ok & (e2e <= k * ref_e2e), 1.0, 0.0), np.nan
                )
                self.slo_ttft[k][i] = np.where(
                    present, np.where(ok & (ttft <= k * ref_pre), 1.0, 0.0), np.nan
                )

    def restrict(self, repeats: list[int]) -> Cell:
        """A view of this cell holding only the named repeats, for a paired contrast."""
        keep = [self.repeats.index(r) for r in repeats]
        other = Cell.__new__(Cell)
        other.policy = self.policy
        other.repeats = list(repeats)
        other.runs = {r: self.runs[r] for r in repeats}
        other.e2e = self.e2e[keep]
        other.ttft = self.ttft[keep]
        other.tpot = self.tpot[keep]
        other.present = self.present[keep]
        other.failed = self.failed[keep]
        other.slo_e2e = {k: v[keep] for k, v in self.slo_e2e.items()}
        other.slo_ttft = {k: v[keep] for k, v in self.slo_ttft.items()}
        return other


def gather(arr: np.ndarray, reps: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """arr (R, n), reps (B, R') repeat rows, pos (B, n) positions -> (B, R'*n)."""
    picked = arr[reps]  # (B, R', n)
    out = np.take_along_axis(picked, np.broadcast_to(pos[:, None, :], picked.shape), axis=2)
    return out.reshape(pos.shape[0], -1)


def cell_draws(
    cells: dict[str, Cell], n_boot: int, block: int, rng: np.random.Generator, n: int
) -> dict[str, dict[str, np.ndarray]]:
    """Bootstrap draws of every statistic for every cell, paired across cells.

    Every cell must hold the same repeats. Pairing is the point of drawing them together:
    the same repeat numbers and the same arrival positions go to every cell in one draw, and
    that is only meaningful if repeat k means the same arrival sequence in each. Callers
    intersect the repeats first (`Cell.restrict`) rather than letting this pair whatever
    happens to be present.
    """
    repeat_sets = {tuple(c.repeats) for c in cells.values()}
    if len(repeat_sets) > 1:
        raise ValueError(
            "cells with different repeats cannot be drawn together: "
            + ", ".join(f"{p}={c.repeats}" for p, c in sorted(cells.items()))
            + ". Restrict them to the repeats they share first"
        )
    out = {
        p: {
            s: np.empty(n_boot)
            for s in (
                *LATENCY_STATS,
                "ttft_mean",
                "ttft_p95",
                "tpot_mean",
                *(f"slo_e2e_{k:g}x" for k in SLO_SCALES),
                *(f"slo_ttft_{k:g}x" for k in SLO_SCALES),
                "log_mean",
            )
        }
        for p in cells
    }
    done = 0
    while done < n_boot:
        b = min(CHUNK, n_boot - done)
        pos = block_positions(rng, b, n, block)
        # One draw of repeats and one of positions, used by every cell. That is the pairing.
        r_count = len(next(iter(repeat_sets)))
        reps = rng.integers(0, r_count, size=(b, r_count))
        for p, c in cells.items():
            e2e = gather(c.e2e, reps, pos)
            sl = slice(done, done + b)
            mean = np.nanmean(e2e, axis=1)
            out[p]["mean"][sl] = mean
            out[p]["log_mean"][sl] = np.log(mean)
            out[p]["p50"][sl] = percentile_rows(e2e, 50)
            out[p]["p95"][sl] = percentile_rows(e2e, 95)
            out[p]["p99"][sl] = percentile_rows(e2e, 99)
            ttft = gather(c.ttft, reps, pos)
            out[p]["ttft_mean"][sl] = np.nanmean(ttft, axis=1)
            out[p]["ttft_p95"][sl] = percentile_rows(ttft, 95)
            out[p]["tpot_mean"][sl] = np.nanmean(gather(c.tpot, reps, pos), axis=1)
            for k in SLO_SCALES:
                out[p][f"slo_e2e_{k:g}x"][sl] = np.nanmean(gather(c.slo_e2e[k], reps, pos), axis=1)
                out[p][f"slo_ttft_{k:g}x"][sl] = np.nanmean(
                    gather(c.slo_ttft[k], reps, pos), axis=1
                )
        done += b
    return out


def point_values(c: Cell) -> dict[str, float]:
    e2e = c.e2e[~np.isnan(c.e2e)]
    ttft = c.ttft[~np.isnan(c.ttft)]
    tpot = c.tpot[~np.isnan(c.tpot)]
    # A snapshot fitted before the prefill/decode split existed leaves a run with no phase
    # times at all. TTFT and TPOT are then not available, which is not the same as zero.
    v = {
        "mean": float(e2e.mean()),
        "p50": float(np.percentile(e2e, 50)),
        "p95": float(np.percentile(e2e, 95)),
        "p99": float(np.percentile(e2e, 99)),
        "ttft_mean": float(ttft.mean()) if ttft.size else float("nan"),
        "ttft_p95": float(np.percentile(ttft, 95)) if ttft.size else float("nan"),
        "tpot_mean": float(tpot.mean()) if tpot.size else float("nan"),
    }
    v["log_mean"] = math.log(v["mean"])
    for k in SLO_SCALES:
        v[f"slo_e2e_{k:g}x"] = float(np.nanmean(c.slo_e2e[k]))
        v[f"slo_ttft_{k:g}x"] = float(np.nanmean(c.slo_ttft[k]))
    return v


def third_ratio(c: Cell) -> float:
    n = c.e2e.shape[1]
    third = n // 3
    return float(np.nanmean(c.e2e[:, n - third :]) / np.nanmean(c.e2e[:, :third]))


def paired(
    cells: dict[str, Cell],
    steady: dict[str, bool],
    invalid: dict[str, dict[str, list[str]]],
    names: tuple[str, ...],
) -> tuple[dict[str, Cell] | None, dict]:
    """The named cells restricted to the repeats they share, or a reason they cannot be.

    A contrast is a difference between policies on the same arrivals, so it is computed on
    the repeats every cell in it has, and it says which those were. The reasons it can come
    back undefined, in the order they are checked: a cell has no run at all; a run of one is
    invalid; a cell was still climbing; the cells share no repeat; or a request failed at a
    position inside the shared window, which would compare one policy's full window against
    another's partial one.
    """
    missing = [p for p in names if p not in cells]
    if missing:
        return None, {"status": f"undefined: no valid run for {', '.join(missing)}"}
    broken = sorted(p for p in names if invalid.get(p))
    if broken:
        runs = sorted(r for p in broken for r in invalid[p])
        return None, {
            "status": f"undefined: invalid run(s) {', '.join(runs)}",
            "invalid_runs": {p: invalid[p] for p in broken},
        }
    moving = [p for p in names if not steady[p]]
    if moving:
        return None, {"status": f"undefined: transient cell(s) {', '.join(moving)}"}
    common = sorted(set.intersection(*(set(cells[p].repeats) for p in names)))
    if not common:
        return None, {
            "status": "undefined: no repeat is shared by "
            + ", ".join(f"{p} {cells[p].repeats}" for p in names)
        }
    restricted = {p: cells[p].restrict(common) for p in names}
    failed = {p: int(c.failed.sum()) for p, c in restricted.items() if c.failed.any()}
    dropped = {
        p: sorted(set(cells[p].repeats) - set(common))
        for p in names
        if set(cells[p].repeats) - set(common)
    }
    if failed:
        return None, {
            "status": "undefined: "
            + ", ".join(f"{n} request(s) failed under {p}" for p, n in sorted(failed.items()))
            + " inside the window these cells share",
            "repeats_used": common,
            "repeats_dropped": dropped,
            "failed_positions": sum(failed.values()),
        }
    return restricted, {
        "status": "defined",
        "repeats_used": common,
        "repeats_dropped": dropped,
        # One repeat is one arrival sequence and one routing stream, so the interval under
        # it is a statement about that path and not about the pair of policies.
        "single_repeat": len(common) == 1,
    }


def contrast(
    cells: dict[str, Cell],
    steady: dict[str, bool],
    invalid: dict[str, dict[str, list[str]]],
    blind: str,
    aware: str,
    n_boot: int,
    block: int,
    rng: np.random.Generator,
) -> dict:
    """What calibration buys one router: `blind` minus `aware`, on the mean and on p95.

    Reported in ms and as `aware / blind`, so a reader can see whether a gain that grows in
    ms is also growing as a fraction.
    """
    restricted, out = paired(cells, steady, invalid, (blind, aware))
    if restricted is None:
        return out
    draws = cell_draws(restricted, n_boot, block, rng, restricted[blind].e2e.shape[1])
    vb, va = point_values(restricted[blind]), point_values(restricted[aware])
    for s in ("mean", "p95"):
        db, da = draws[blind][s], draws[aware][s]
        out[s] = {
            "gain_ms": r1(vb[s] - va[s]),
            "gain_ms_ci95": [r1(x) for x in interval(db - da)],
            "ratio": r4(va[s] / vb[s]),
            "ratio_ci95": [r4(x) for x in interval(da / db)],
            "gain_share": r4(1 - va[s] / vb[s]),
        }
    return out


def comparisons(
    cells: dict[str, Cell],
    steady: dict[str, bool],
    invalid: dict[str, dict[str, list[str]]],
    n_boot: int,
    block: int,
    seed: int,
    stale: float,
    lam: float,
    workload: str = "",
) -> dict:
    """Every cell at a point as a ratio to one reference cell, paired by arrival.

    This is the value-of-calibration curve's primary statistic: mean latency per arm over the
    same statistic for the JSQ arm at the same point. It is also what decides whether a
    calibrated policy is separated from the ordinal-only control, and whether the
    deterministic weighted round-robin differs from the random-draw StaticWeighted.

    The reference is a JSQ cell when the point holds one, since JSQ is the policy that reads
    no capability at all, and otherwise the first cell by name. Ratios below 1 mean the arm
    finished sooner than the reference.
    """
    ref = min(cells, key=lambda p: (p.split("@")[0] != "jsq", p))
    out: dict = {"reference": ref, "policies": {}}
    for policy in sorted(cells):
        if policy == ref:
            continue
        restricted, entry = paired(cells, steady, invalid, (ref, policy))
        if restricted is not None:
            draws = cell_draws(
                restricted,
                n_boot,
                block,
                stream(seed, "arm", ref, policy, *where(workload, stale, lam)),
                restricted[ref].e2e.shape[1],
            )
            vr, va = point_values(restricted[ref]), point_values(restricted[policy])
            for stat in ("mean", "p95"):
                lo, hi = interval(draws[policy][stat] / draws[ref][stat])
                entry[f"{stat}_ratio"] = r4(va[stat] / vr[stat])
                entry[f"{stat}_ratio_ci95"] = [r4(lo), r4(hi)]
                entry[f"{stat}_separated"] = bool(lo > 1.0 or hi < 1.0)
            # SLO attainment is a share, so the arms are compared by difference in
            # percentage points. A ratio of shares divides by zero as soon as a reference
            # arm misses every deadline, which RoundRobin does at the heavier points.
            lo, hi = interval(draws[policy]["slo_e2e_2x"] - draws[ref]["slo_e2e_2x"])
            entry["slo_e2e_2x_delta"] = r4(va["slo_e2e_2x"] - vr["slo_e2e_2x"])
            entry["slo_e2e_2x_delta_ci95"] = [r4(lo), r4(hi)]
        out["policies"][policy] = entry
    return out


def climb(c: Cell, block: int, n_boot: int, rng: np.random.Generator) -> dict:
    """Last-third mean over first-third mean, with a block-bootstrap interval per third."""
    n = c.e2e.shape[1]
    third = n // 3
    first, last = c.e2e[:, :third], c.e2e[:, n - third :]
    value = float(np.nanmean(last) / np.nanmean(first))
    blk = max(1, min(block, third // 3))
    reps = rng.integers(0, len(c.repeats), size=(n_boot, len(c.repeats)))
    lo_draw = np.nanmean(gather(first, reps, block_positions(rng, n_boot, third, blk)), axis=1)
    hi_draw = np.nanmean(gather(last, reps, block_positions(rng, n_boot, third, blk)), axis=1)
    ci = interval(hi_draw / lo_draw)
    transient = value >= CLIMB_RATIO and ci[0] > 1.0
    return {
        "last_over_first_third": r4(value),
        "ci95": [r4(x) for x in ci],
        "rise_ms": r1(float(np.nanmean(last) - np.nanmean(first))),
        "transient": bool(transient),
    }


def utilisation(
    at_point: pd.DataFrame,
    man: dict,
    snaps: dict[str, dict],
    lam: float,
) -> dict:
    """Pool and per-node capacity from the cost models, and measured load per cell per node."""
    cap = pool_capacity(
        man["nodes"], man["cost_model_snapshots"], man["config"]["length_dist"], snaps
    )
    nodes = {
        nid: {
            "slots": c["slots"],
            "capability_tok_s": r4(c["capability_tok_s"]),
            "cost_model_service_ms_c1": r1(c["service_ms_c1"]),
            f"cost_model_service_ms_c{c['slots']}": r1(c["service_ms_full"]),
            "capacity_rps": r4(c["capacity_rps"]),
        }
        for nid, c in cap.items()
    }
    caps = {nid: v["capacity_rps"] for nid, v in nodes.items()}
    slow = min(caps, key=caps.get)
    fast = max(caps, key=caps.get)
    cap_total = sum(capability(snaps[man["cost_model_snapshots"][n]]) for n in nodes)
    nominal_fast_share = capability(snaps[man["cost_model_snapshots"][fast]]) / cap_total

    per_cell = {}
    for policy, g in at_point.groupby("policy"):
        runs = g.groupby("run_id")
        span = runs["intended_offset_s"].agg(lambda s: s.max() - s.min()).mean()
        n_runs = g["run_id"].nunique()
        entry = {}
        for nid in nodes:
            rows = g[g["chosen_node"] == nid]
            lam_node = len(rows) / n_runs / span if span > 0 else float("nan")
            busy = rows["service_ms"].sum() / 1000.0 / n_runs / span if span > 0 else float("nan")
            entry[nid] = {
                "transport_residual_ms_mean": (
                    r1(float(rows["transport_residual_ms"].mean())) if len(rows) else None
                ),
                "arrival_rps": r4(lam_node),
                "service_ms_mean": r1(float(rows["service_ms"].mean())) if len(rows) else None,
                "offered_over_capacity": r4(lam_node / caps[nid]),
                "busy_slots": r4(busy),
                "measured_utilisation": r4(busy / nodes[nid]["slots"]),
            }
        per_cell[policy] = entry
    achieved = {
        policy: sum(e["arrival_rps"] for e in entry.values()) for policy, entry in per_cell.items()
    }
    return {
        "nodes": nodes,
        "fast_node": fast,
        # What the pool actually saw, beside what the config asked for. They differ when a
        # run's window is shorter than its trace, and the plan reports the achieved figure.
        "achieved_rps_by_policy": {p: r4(v) for p, v in achieved.items()},
        "achieved_pool_utilisation": r4(
            (sum(achieved.values()) / len(achieved)) / sum(caps.values())
        ),
        "slow_node": slow,
        "pool_capacity_rps": r4(sum(caps.values())),
        "pool_utilisation": r4(lam / sum(caps.values())),
        "slow_node_utilisation_at_half_load": r4(lam / 2 / caps[slow]),
        "static_weighted_nominal_share_to_fast": r4(nominal_fast_share),
        "cost_model_R_service_c1": r4(
            nodes[slow]["cost_model_service_ms_c1"] / nodes[fast]["cost_model_service_ms_c1"]
        ),
        "cost_model_R_service_at_slots": r4(caps[fast] / caps[slow]),
        "per_cell": per_cell,
    }


def operating_r(rows: pd.DataFrame, fast: str, slow: str) -> dict:
    """Slow over fast on the phase times the runs actually saw, per bucket and request-weighted."""
    out = {}
    buckets = {}
    for bucket, g in rows.groupby("bucket_id"):
        f, s = g[g["chosen_node"] == fast], g[g["chosen_node"] == slow]
        if len(f) < 10 or len(s) < 10:
            continue
        buckets[bucket] = {
            "n_fast": len(f),
            "n_slow": len(s),
            "R_service": r4(s["service_ms"].mean() / f["service_ms"].mean()),
            "R_prefill": r4(s["prefill_ms"].mean() / f["prefill_ms"].mean()),
            "R_decode": r4(s["decode_ms"].mean() / f["decode_ms"].mean()),
        }
    out["buckets"] = buckets
    f, s = rows[rows["chosen_node"] == fast], rows[rows["chosen_node"] == slow]
    if len(f) and len(s):
        out["pooled"] = {
            "R_service": r4(s["service_ms"].mean() / f["service_ms"].mean()),
            "R_prefill": r4(s["prefill_ms"].mean() / f["prefill_ms"].mean()),
            "R_decode": r4(s["decode_ms"].mean() / f["decode_ms"].mean()),
        }
    return out


# ------------------------------------------------------------------------- summarise


def prepare(
    frame: pd.DataFrame, mans: dict[str, dict], snaps: dict[str, dict]
) -> tuple[pd.DataFrame, dict[str, str], dict[str, tuple], dict[str, dict]]:
    """A run set made ready for per-point work, the same way for every tool that reads one.

    Labels each policy with its capability arm, attaches the SLO reference times, names the
    runs the harness rejected with its reasons, counts failures over whole runs, and drops
    the rejected runs' rows. `tools/compare_sets.py` calls this so a cross-set contrast is
    computed on exactly the cells a summary reports.
    """
    frame = frame.copy()
    frame["repeat"] = frame["run_id"].map(repeat_of)
    # A capability arm changes what the calibrated policies are told about the nodes, so two
    # runs of one policy under two arms are two conditions and not two repeats. The label
    # carries the arm, which keeps them apart everywhere below.
    arms = {rid: (m["config"].get("capability_arm") or "") for rid, m in mans.items()}
    frame["policy"] = [
        f"{pol}@{arms[rid]}" if arms.get(rid) else pol
        for pol, rid in zip(frame["policy"], frame["run_id"], strict=True)
    ]

    # Reference times for SLO attainment: the fastest node's c=1 cell for each bucket.
    any_man = next(iter(mans.values()), None)
    ref = {}
    if any_man is not None:
        pool_snaps = [snaps[s] for s in any_man["cost_model_snapshots"].values()]
        for bucket in frame["bucket_id"].unique():
            p, o = (int(x) for x in bucket[1:].split("_o"))
            cells = [locate(s["entries"], p, o, 1) for s in pool_snaps]
            best = min((c for c in cells if c), key=lambda c: c["service_ms_mean"])
            ref[bucket] = (best["service_ms_mean"], best["prefill_ms_mean"])
    frame["ref_service_ms"] = frame["bucket_id"].map(lambda b: ref.get(b, (np.nan, np.nan))[0])
    frame["ref_prefill_ms"] = frame["bucket_id"].map(lambda b: ref.get(b, (np.nan, np.nan))[1])

    # Runs the harness itself marked invalid. They are in the run set because someone chose
    # to join them, and they are reported with the reasons rather than averaged in silence.
    invalid_runs = {
        rid: (
            f"{m['policy']}@{m['config']['capability_arm']}"
            if m["config"].get("capability_arm")
            else m["policy"],
            float(m["config"].get("staleness_s", m.get("staleness_s", 0.0))),
            round(float(m.get("lambda", m["config"].get("lambda", 0.0))), 6),
            validity_reasons(m["validity"]),
        )
        for rid, m in mans.items()
        if not m.get("validity", {}).get("valid", True)
    }

    # Failures over the whole run, warmup included.
    failures = {}
    for run_id, g in frame.groupby("run_id"):
        bad = g[g["status"] != "ok"]
        if len(bad):
            failures[run_id] = {
                "not_ok": len(bad),
                "in_warmup": int(bad["is_warmup"].sum()),
                "served_by_worker_but_not_delivered": int((bad["service_ms"] > 0).sum()),
                "statuses": bad["status"].value_counts().to_dict(),
            }

    # A run the harness rejected contributes no number. It is named in the summary and in
    # every cell it belonged to, and its rows stop here (analysis plan, section 2).
    frame = frame[~frame["run_id"].isin(set(invalid_runs))]

    return frame, arms, invalid_runs, failures


def point_setup(window: pd.DataFrame) -> tuple[dict[str, Cell], int, dict[str, float], int]:
    """Cells, arrival positions, autocorrelation times and block length for one point."""
    positions = pd.Index(sorted(window["req_id"].unique()))
    n = len(positions)
    cells = {
        p: Cell(p, {rep: r for rep, r in g.groupby("repeat")}, positions)
        for p, g in window.groupby("policy")
    }
    taus = {
        p: max(integrated_tau(c.e2e[i]) for i in range(len(c.repeats))) for p, c in cells.items()
    }
    # The block length comes from the cells that are not visibly climbing. A queue that
    # grows for the whole run is correlated over the whole run, and letting it set the
    # block would widen every stable cell's interval to match the one that is not.
    level = {p: third_ratio(c) < CLIMB_RATIO for p, c in cells.items()}
    tau_block = max((taus[p] for p in cells if level[p]), default=max(taus.values()))
    block = max(math.ceil(n ** (1 / 3)), math.ceil(2 * tau_block))
    block = min(block, max(1, n // BLOCK_CAP_FRACTION))
    return cells, n, taus, block


def invalid_at(
    invalid_runs: dict[str, tuple], cells: dict[str, Cell], stale: float, lam: float
) -> dict[str, dict[str, list[str]]]:
    """Rejected runs belonging to each cell at one point.

    Read from the manifests rather than from the rows, because a run the harness marked
    invalid is usually not in the run set at all: `runset` leaves it out. Its cell would
    otherwise look complete while missing the repeat most likely to be the policy's worst,
    which is the one that made the run invalid.
    """
    return {
        p: {
            rid: reasons
            for rid, (pol, st, lm, reasons) in invalid_runs.items()
            if pol == p and st == float(stale) and lm == round(float(lam), 6)
        }
        for p in {*cells, *(v[0] for v in invalid_runs.values())}
    }


def load_trend(qa: list[dict]) -> dict | None:
    """How the queue-aware gain moves with load, lightest to heaviest steady point."""
    if len(qa) < 2:
        return None
    lo_pt, hi_pt = qa[0], qa[-1]
    ms = hi_pt["_queue_aware"]["ms"] - lo_pt["_queue_aware"]["ms"]
    ratio = hi_pt["_queue_aware"]["ratio"] / lo_pt["_queue_aware"]["ratio"]
    return {
        "from_lambda": lo_pt["lambda_rps"],
        "to_lambda": hi_pt["lambda_rps"],
        "queue_aware_gain_ms": [r1(pt["_queue_aware"]["ms_value"]) for pt in qa],
        "wjsq_over_jsq": [r4(pt["_queue_aware"]["ratio_value"]) for pt in qa],
        "lambdas": [pt["lambda_rps"] for pt in qa],
        "change_ms": r1(hi_pt["_queue_aware"]["ms_value"] - lo_pt["_queue_aware"]["ms_value"]),
        "change_ms_ci95": [r1(x) for x in interval(ms)],
        "change_in_ratio": r4(
            hi_pt["_queue_aware"]["ratio_value"] / lo_pt["_queue_aware"]["ratio_value"]
        ),
        "change_in_ratio_ci95": [r4(x) for x in interval(ratio)],
        "reading": (
            "H1's load clause predicts the queue-aware gain shrinks with load: change_ms "
            "below zero, change_in_ratio above one"
        ),
    }


def summarise(frame: pd.DataFrame, n_boot: int, seed: int, runset_path: Path | None = None) -> dict:
    mans = manifests(runset_path) if runset_path is not None else {}
    snaps = snapshot_index()
    frame, arms, invalid_runs, failures = prepare(frame, mans, snaps)
    # A campaign with several workloads (Poisson and MMPP at one utilisation, or three
    # shapes at one slow-node load) can put two of them at the same rate. They are different
    # conditions, so the workload is part of what a point is.
    workloads = {rid: m["config"].get("workload") or "" for rid, m in mans.items()}
    frame["workload"] = frame["run_id"].map(lambda r: workloads.get(r, ""))

    ok_rows = measured(frame)
    fast = ok_rows.groupby("chosen_node")["service_ms"].mean().idxmin()
    gen_seeds = {m["config"].get("gen_seed") for m in mans.values()}
    sched_seeds = {m["config"].get("seed") for m in mans.values()}
    traces = set(frame["trace_sha256"].unique())

    points = []
    blocks = {}
    for (wl, stale, lam), at_point in sorted(frame.groupby(["workload", "staleness_s", "lambda"])):
        at = where(wl, stale, lam)
        window = at_point[~at_point["is_warmup"]]
        cells, n, taus, block = point_setup(window)
        blocks[f"{wl + '_' if wl else ''}s{float(stale):g}_l{round(float(lam), 3)}"] = block
        # One cell at a time for the per-policy intervals: a cell's own interval needs no
        # pairing, and cells here can hold different repeats. Every contrast below draws
        # its own cells together, on the repeats they share.
        draws = {
            p: cell_draws({p: c}, n_boot, block, stream(seed, "cell", p, *at), n)[p]
            for p, c in cells.items()
        }
        invalid = invalid_at(
            {r: v for r, v in invalid_runs.items() if workloads.get(r, "") == wl}, cells, stale, lam
        )

        policies = {}
        steady = {}
        all_repeats = sorted({r for c in cells.values() for r in c.repeats})
        for p, c in sorted(cells.items()):
            v = point_values(c)
            g = window[window["policy"] == p]
            gate = climb(c, block, n_boot, stream(seed, "climb", p, *at))
            steady[p] = not gate["transient"]
            policies[p] = {
                "runs": len(c.repeats),
                "not_ok": int(c.failed.sum()),
                # True when this policy did not run every repeat the point holds, so its
                # numbers rest on a different set of arrival sequences than its neighbours.
                "runs_differ_at_point": c.repeats != all_repeats,
                "requests": int((~np.isnan(c.e2e)).sum()),
                "share_to_fast_node": round(float((g["chosen_node"] == fast).mean()), 3),
                "queue_wait_ms_mean_reference_only": round(float(g["queue_wait_ms"].mean()), 1),
                "lag1_autocorrelation": r4(
                    float(np.nanmean([lag1(c.e2e[i]) for i in range(len(c.repeats))]))
                ),
                "tau_int_requests": r4(taus[p]),
                "steady_state": gate,
                "repeats": c.repeats,
                # A run the harness marked invalid is reported with the reasons it gave,
                # and it undefines every contrast this cell is part of.
                **({"invalid_runs": invalid.get(p) or {}} if invalid.get(p) else {}),
                **{
                    s: {"value": r1(v[s]), "ci95": [r1(x) for x in interval(draws[p][s])]}
                    for s in (*LATENCY_STATS, "ttft_mean", "ttft_p95", "tpot_mean")
                },
                **{
                    s: {"value": r4(v[s]), "ci95": [r4(x) for x in interval(draws[p][s])]}
                    for s in (
                        *(f"slo_e2e_{k:g}x" for k in SLO_SCALES),
                        *(f"slo_ttft_{k:g}x" for k in SLO_SCALES),
                    )
                },
            }

        h1: dict = {}
        restricted, h1_head = paired(cells, steady, invalid, H1_POLICIES)
        h1_status = h1_head["status"]
        if restricted is not None:
            vals = {p: point_values(restricted[p]) for p in H1_POLICIES}
            d = cell_draws(restricted, n_boot, block, stream(seed, "h1", *at), n)
            for s in LATENCY_STATS:
                v = {p: vals[p][s] for p in H1_POLICIES}
                boot = (d["wjsq"][s] - d["jsq"][s]) - (
                    d["static_weighted"][s] - d["round_robin"][s]
                )
                lo, hi = interval(boot)
                log_boot = (np.log(d["wjsq"][s]) - np.log(d["jsq"][s])) - (
                    np.log(d["static_weighted"][s]) - np.log(d["round_robin"][s])
                )
                log_val = math.log(v["wjsq"] / v["jsq"]) - math.log(
                    v["static_weighted"] / v["round_robin"]
                )
                llo, lhi = interval(log_boot)
                h1[s] = {
                    "calibration_gain_queue_blind": r1(v["round_robin"] - v["static_weighted"]),
                    "calibration_gain_queue_blind_ci95": [
                        r1(x) for x in interval(d["round_robin"][s] - d["static_weighted"][s])
                    ],
                    "calibration_gain_queue_aware": r1(v["jsq"] - v["wjsq"]),
                    "calibration_gain_queue_aware_ci95": [
                        r1(x) for x in interval(d["jsq"][s] - d["wjsq"][s])
                    ],
                    "ratio_static_weighted_over_round_robin": r4(
                        v["static_weighted"] / v["round_robin"]
                    ),
                    "ratio_static_weighted_over_round_robin_ci95": [
                        r4(x) for x in interval(d["static_weighted"][s] / d["round_robin"][s])
                    ],
                    "ratio_wjsq_over_jsq": r4(v["wjsq"] / v["jsq"]),
                    "ratio_wjsq_over_jsq_ci95": [
                        r4(x) for x in interval(d["wjsq"][s] / d["jsq"][s])
                    ],
                    "interaction": r1(
                        (v["wjsq"] - v["jsq"]) - (v["static_weighted"] - v["round_robin"])
                    ),
                    "ci95": [r1(lo), r1(hi)],
                    "excludes_zero": bool(lo > 0 or hi < 0),
                    "interaction_log": r4(log_val),
                    "interaction_log_ci95": [r4(llo), r4(lhi)],
                    "interaction_log_excludes_zero": bool(llo > 0 or lhi < 0),
                    "repeats_used": h1_head["repeats_used"],
                }
        # Kept for the load-trend contrast across points, which uses independent draws.
        queue_aware = None
        aware_cells, _ = paired(cells, steady, invalid, ("jsq", "wjsq"))
        if aware_cells is not None:
            aware_draws = cell_draws(aware_cells, n_boot, block, stream(seed, "trend", *at), n)
            queue_aware = {
                "ms": aware_draws["jsq"]["mean"] - aware_draws["wjsq"]["mean"],
                "ratio": aware_draws["wjsq"]["mean"] / aware_draws["jsq"]["mean"],
                "ms_value": point_values(aware_cells["jsq"])["mean"]
                - point_values(aware_cells["wjsq"])["mean"],
                "ratio_value": point_values(aware_cells["wjsq"])["mean"]
                / point_values(aware_cells["jsq"])["mean"],
            }

        against_reference = comparisons(
            cells, steady, invalid, n_boot, block, seed, float(stale), float(lam), wl
        )
        man = next((mans[r] for r in at_point["run_id"].unique() if r in mans), None)
        util = (
            utilisation(window[window["status"] == "ok"], man, snaps, float(lam)) if man else None
        )
        steady_rows = window[(window["status"] == "ok") & window["policy"].map(steady)]
        oper = operating_r(steady_rows, util["fast_node"], util["slow_node"]) if util else None

        points.append(
            {
                **({"workload": wl} if wl else {}),
                "lambda_rps": round(float(lam), 3),
                "staleness_s": float(stale),
                "block_length_requests": block,
                "measured_arrivals": n,
                "policies": policies,
                "h1_status": h1_status,
                "h1_repeats_used": h1_head.get("repeats_used", []),
                "h1_repeats_dropped": h1_head.get("repeats_dropped", {}),
                "h1": h1,
                "calibration_gain": {
                    "queue_blind": contrast(
                        cells,
                        steady,
                        invalid,
                        "round_robin",
                        "static_weighted",
                        n_boot,
                        block,
                        stream(seed, "queue_blind", *at),
                    ),
                    "queue_aware": contrast(
                        cells,
                        steady,
                        invalid,
                        "jsq",
                        "wjsq",
                        n_boot,
                        block,
                        stream(seed, "queue_aware", *at),
                    ),
                },
                "against_reference": against_reference,
                "utilisation": util,
                "operating_R_steady_cells": oper,
                "_queue_aware": queue_aware,
            }
        )

    # How the queue-aware gain moves with load, lightest to heaviest steady point.
    fresh = min(pt["staleness_s"] for pt in points)
    trends = {}
    for wl in sorted({pt.get("workload", "") for pt in points}):
        qa = [
            pt
            for pt in points
            if pt["_queue_aware"] is not None
            and pt["staleness_s"] == fresh
            and pt.get("workload", "") == wl
        ]
        trends[wl] = load_trend(qa)
    for pt in points:
        pt.pop("_queue_aware")

    first = frame.iloc[0]
    several = len(trends) > 1
    return {
        "run_ids": sorted(frame["run_id"].unique().tolist()),
        "vehicle": sorted(frame["vehicle"].unique().tolist()),
        "R_headline": round(float(first["R"]), 3),
        "trace_sha256": sorted(traces),
        "gen_seeds": sorted(s for s in gen_seeds if s is not None),
        "capability_arms": sorted({a for a in arms.values() if a}),
        "capability_modes": sorted(
            {m["config"].get("capability_mode", "service") for m in mans.values()}
        ),
        "scheduler_seeds": sorted(s for s in sched_seeds if s is not None),
        "arrivals_independent": len(traces) > 1,
        "scheduler_seed_varied": len(sched_seeds - {None}) > 1,
        "fast_node": fast,
        "primary_statistic": "H1 interaction on log mean end-to-end latency, steady-state points",
        "bootstrap": {
            "draws": n_boot,
            "seed": seed,
            "method": "repeats resampled jointly across policies, then a circular block "
            "bootstrap over arrival positions shared by every policy and repeat",
            "block_length_requests": blocks,
        },
        "steady_state_rule": (
            f"transient when last-third mean over first-third mean >= {CLIMB_RATIO} and its "
            "95% interval excludes 1"
        ),
        "slo_reference": "fastest pool node's concurrency-1 cost-model cell for the request's bucket",
        "failures": failures,
        "invalid_runs": {rid: v[3] for rid, v in invalid_runs.items()},
        "load_trend_queue_aware": None if several else next(iter(trends.values())),
        # One trend per workload when a campaign holds several, since a rate that rises from
        # one workload's point to another's is not load.
        **({"load_trend_queue_aware_by_workload": trends} if several else {}),
        "points": points,
    }


# -------------------------------------------------------------------------- markdown


def _cell(r: dict, s: str, pct: bool = False) -> str:
    v, (lo, hi) = r[s]["value"], r[s]["ci95"]
    if v is None:
        return "n/a"
    if pct:
        return f"{v:.0%} [{lo:.0%}, {hi:.0%}]"
    return f"{v:.0f} [{lo:.0f}, {hi:.0f}]"


def _r2(v: float | None) -> str:
    # The phase ratios are undefined on a simulated replay, which records no phase timings.
    return "n/a" if v is None else f"{v:.2f}"


def _tpot(r: dict) -> str:
    # A simulated replay records no per-token timings, so its TPOT is undefined.
    v = r["tpot_mean"]["value"]
    return "n/a" if v is None else f"{v:.1f}"


def markdown(summary: dict) -> str:
    b = summary["bootstrap"]
    out = [
        (
            f"Fast node: `{summary['fast_node']}`. Bootstrap: {b['draws']} draws; {b['method']}. "
            "Intervals are 95%."
        ),
        "",
        f"Arrivals independent across repeats: **{'yes' if summary['arrivals_independent'] else 'no'}**. "
        f"Scheduler seed varied across repeats: **{'yes' if summary['scheduler_seed_varied'] else 'no'}**."
        + (
            ""
            if summary["arrivals_independent"]
            else " Every interval below is conditional on one arrival sequence and one routing random stream."
        ),
        "",
        f"Primary statistic: {summary['primary_statistic']}. Steady-state rule: {summary['steady_state_rule']}.",
        "",
    ]
    if summary["failures"]:
        out += ["Failures over whole runs, warmup included:", ""]
        for run_id, f in summary["failures"].items():
            out.append(
                f"- `{run_id}`: {f['not_ok']} not ok ({f['in_warmup']} in warmup), "
                f"{f['served_by_worker_but_not_delivered']} served by a worker but never delivered"
            )
        out.append("")
    for pt in summary["points"]:
        out += [
            f"### {pt['workload'] + ', ' if pt.get('workload') else ''}{pt['lambda_rps']} req/s"
            + (f", staleness {pt['staleness_s']:g} s" if pt["staleness_s"] else ""),
            "",
            f"Block length {pt['block_length_requests']} requests over {pt['measured_arrivals']} measured arrivals.",
            "",
            "| Policy | Steady | Climb | Lag-1 | To fast | Mean ms | p50 ms | p95 ms | p99 ms |",
            "|---|---|---:|---:|---:|---|---|---|---|",
        ]
        for p, r in pt["policies"].items():
            ss = r["steady_state"]
            out.append(
                f"| {p} | {'yes' if not ss['transient'] else '**no**'} | {ss['last_over_first_third']:.2f} | "
                f"{r['lag1_autocorrelation']:.2f} | {r['share_to_fast_node']:.0%} | {_cell(r, 'mean')} | "
                f"{_cell(r, 'p50')} | {_cell(r, 'p95')} | {_cell(r, 'p99')} |"
            )
        out += [
            "",
            "| Policy | TTFT mean ms (worker) | TTFT p95 ms | TPOT ms | E2E SLO 2x | E2E SLO 5x | TTFT SLO 2x | TTFT SLO 5x |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for p, r in pt["policies"].items():
            out.append(
                f"| {p} | {_cell(r, 'ttft_mean')} | {_cell(r, 'ttft_p95')} | {_tpot(r)} | "
                f"{_cell(r, 'slo_e2e_2x', True)} | {_cell(r, 'slo_e2e_5x', True)} | "
                f"{_cell(r, 'slo_ttft_2x', True)} | {_cell(r, 'slo_ttft_5x', True)} |"
            )
        out.append("")
        ar = pt.get("against_reference", {"reference": "", "policies": {}})
        if ar["policies"]:
            out += [
                f"| Against `{ar['reference']}` | Mean ratio | p95 ratio | E2E SLO 2x, points | Separated on the mean |",
                "|---|---|---|---|---|",
            ]
            for policy, e in ar["policies"].items():
                if e["status"] != "defined":
                    out.append(f"| {policy} | {e['status']} | | | |")
                    continue
                out.append(
                    f"| {policy} | {e['mean_ratio']:.3f} [{e['mean_ratio_ci95'][0]:.3f}, {e['mean_ratio_ci95'][1]:.3f}] | "
                    f"{e['p95_ratio']:.3f} [{e['p95_ratio_ci95'][0]:.3f}, {e['p95_ratio_ci95'][1]:.3f}] | "
                    f"{e['slo_e2e_2x_delta']:+.3f} [{e['slo_e2e_2x_delta_ci95'][0]:+.3f}, {e['slo_e2e_2x_delta_ci95'][1]:+.3f}] | "
                    f"{'yes' if e['mean_separated'] else 'no'} |"
                )
            out.append("")
        if pt["h1"]:
            out += [
                "| H1 on | Gain, queue-blind ms | Gain, queue-aware ms | Interaction ms | SW/RR | WJSQ/JSQ | Interaction, log | Excludes 0 (log) |",
                "|---|---|---|---|---:|---:|---|---|",
            ]
            for s, h in pt["h1"].items():
                gb, ga = (
                    h["calibration_gain_queue_blind_ci95"],
                    h["calibration_gain_queue_aware_ci95"],
                )
                out.append(
                    f"| {s} | {h['calibration_gain_queue_blind']:.0f} [{gb[0]:.0f}, {gb[1]:.0f}] | "
                    f"{h['calibration_gain_queue_aware']:.0f} [{ga[0]:.0f}, {ga[1]:.0f}] | "
                    f"{h['interaction']:+.0f} [{h['ci95'][0]:+.0f}, {h['ci95'][1]:+.0f}] | "
                    f"{h['ratio_static_weighted_over_round_robin']:.3f} | {h['ratio_wjsq_over_jsq']:.3f} | "
                    f"{h['interaction_log']:+.3f} [{h['interaction_log_ci95'][0]:+.3f}, {h['interaction_log_ci95'][1]:+.3f}] | "
                    f"{'yes' if h['interaction_log_excludes_zero'] else 'no'} |"
                )
        else:
            out.append(f"H1 {pt['h1_status']}.")
        out += [
            "",
            "| Calibration gain on the mean | ms | Calibrated / uncalibrated |",
            "|---|---|---|",
        ]
        for kind, label in (("queue_blind", "RR - SW"), ("queue_aware", "JSQ - WJSQ")):
            g = pt["calibration_gain"][kind]
            if g["status"] != "defined":
                out.append(f"| {label} | {g['status']} | |")
                continue
            m = g["mean"]
            out.append(
                f"| {label} | {m['gain_ms']:.0f} [{m['gain_ms_ci95'][0]:.0f}, {m['gain_ms_ci95'][1]:.0f}] | "
                f"{m['ratio']:.3f} [{m['ratio_ci95'][0]:.3f}, {m['ratio_ci95'][1]:.3f}] |"
            )
        u = pt["utilisation"]
        if u:
            out += [
                "",
                (
                    f"Pool capacity {u['pool_capacity_rps']:.2f} req/s at full slots (cost model), pool utilisation "
                    f"{u['pool_utilisation']:.2f}; slow node `{u['slow_node']}` at half the load "
                    f"{u['slow_node_utilisation_at_half_load']:.2f}. Cost-model R on service: "
                    f"{u['cost_model_R_service_c1']:.2f} at one slot, {u['cost_model_R_service_at_slots']:.2f} at full slots. "
                    f"StaticWeighted nominal share to the fast node {u['static_weighted_nominal_share_to_fast']:.1%}."
                ),
                "",
                "| Policy | Node | Arrival req/s | Service ms | Offered / capacity | Busy slots | Utilisation | Transport residual ms |",
                "|---|---|---:|---:|---:|---:|---:|---:|",
            ]
            for p, entry in u["per_cell"].items():
                for nid, e in entry.items():
                    svc = "" if e["service_ms_mean"] is None else f"{e['service_ms_mean']:.0f}"
                    out.append(
                        f"| {p} | {nid} | {e['arrival_rps']:.2f} | {svc} | {e['offered_over_capacity']:.2f} | "
                        f"{e['busy_slots']:.2f} | {e['measured_utilisation']:.2f} | "
                        f"{'' if e['transport_residual_ms_mean'] is None else format(e['transport_residual_ms_mean'], '.1f')} |"
                    )
        o = pt["operating_R_steady_cells"]
        if o and o.get("pooled"):
            q = o["pooled"]
            out += [
                "",
                (
                    f"Operating R in steady cells, slow over fast (request-weighted): service {_r2(q['R_service'])}, "
                    f"prefill {_r2(q['R_prefill'])}, decode {_r2(q['R_decode'])}."
                ),
            ]
        out.append("")
    by_workload = summary.get("load_trend_queue_aware_by_workload") or {
        "": summary["load_trend_queue_aware"]
    }
    for wl, t in by_workload.items():
        if not t:
            continue
        out += [
            f"### Queue-aware calibration gain against load{', ' + wl if wl else ''}",
            "",
            f"JSQ - WJSQ on the mean at {t['lambdas']}: {t['queue_aware_gain_ms']} ms; WJSQ/JSQ {t['wjsq_over_jsq']}.",
            (
                f"Change from {t['from_lambda']} to {t['to_lambda']} req/s: {t['change_ms']:+.0f} ms "
                f"[{t['change_ms_ci95'][0]:+.0f}, {t['change_ms_ci95'][1]:+.0f}], ratio x{t['change_in_ratio']:.3f} "
                f"[{t['change_in_ratio_ci95'][0]:.3f}, {t['change_in_ratio_ci95'][1]:.3f}]. {t['reading']}."
            ),
            "",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runset", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="writes <out>.json and <out>.md")
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    args = ap.parse_args(argv)
    summary = summarise(pd.read_parquet(args.runset), args.draws, args.seed, args.runset)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".json").write_text(json.dumps(summary, indent=2) + "\n")
    args.out.with_suffix(".md").write_text(markdown(summary) + "\n")
    print(markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
