"""Week-2 — the calibration campaign driver.

    grid pass          every (prompt bucket x output bucket x concurrency) cell,
                       warmed then sampled          -> C-3 entries
    sustained segment  one operating point, held under constant load for minutes,
                       binned into a throughput series  -> tau, the variance envelope
    snapshot series    the sustained cell re-fitted on a rolling window, every N seconds
                       -> the estimate history H3 replays

The campaign is open-loop in the sense that matters here but not in the replay client's
sense, and the difference is worth being explicit about. The replay client must never let
a slow pool throttle its arrival rate, because that would silently turn a queueing
experiment into a measurement of itself. Calibration is the opposite: I *want* exactly
`concurrency` requests in flight and no more, because the cell being measured is defined
by that number. So the grid pass holds concurrency fixed by construction and the
sustained segment replaces each completed request with a new one.

**Warmup is discarded, per cell, not once per campaign.** The first request into a fresh
`-ngl` configuration pays CUDA context setup and kernel JIT — the very stall the README
warns about when `CMAKE_CUDA_ARCHITECTURES` is wrong — and the first request at a new
concurrency pays slot allocation. Folding those into a cell mean would put a one-off cost
into a number the scheduler uses for every subsequent request.

**Prompts are materialized before the timing loop**, from the same
`harness.prompts.materialize` the trace generator and replay client use. A second
definition of "what a prompt of length N is" would drift from the trace's, and then the
cost model would be calibrated on a workload the study never replays.

Failures are counted, never fitted. A timeout is a censored observation, and a cell mean
that averaged in the timeout ceiling would report my own `--timeout` setting as the
node's speed. The counts go in the campaign index, where they are the raw material for
the Week-3 admissible-set determination (F-13) and the cliff characterization (F-15).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from dataplane.calibration import cost_model as cm
from dataplane.calibration import stationarity as st
from dataplane.harness.prompts import materialize
from dataplane.worker.adapter import ServiceResult, f18_status
from dataplane.worker.llamacpp import LlamaCppAdapter

__all__ = [
    "CampaignConfig",
    "CampaignResult",
    "GridPoint",
    "grid",
    "load_config",
    "run_campaign",
    "snapshot_series",
]


@dataclass(frozen=True)
class GridPoint:
    """One calibration cell: a length pair held at a fixed number of in-flight requests."""

    prompt_len: int
    output_len: int
    concurrency: int

    @property
    def label(self) -> str:
        return f"p{self.prompt_len}_o{self.output_len}_c{self.concurrency}"


@dataclass(frozen=True)
class CampaignConfig:
    """Everything a campaign needs, from one file plus a seed (F-20).

    `prompt_lens` are the representative points sampled inside `prompt_edges` buckets —
    one measured length per bucket, chosen by whoever writes the config, because a bucket
    sampled only at its lower edge would advertise a speed the bucket's longer requests
    never see.
    """

    node_class: str
    model: str
    host: str
    endpoint: str
    prompt_edges: list[int]
    output_edges: list[int]
    prompt_lens: list[int]
    output_lens: list[int]
    concurrencies: list[int]
    samples_per_cell: int
    warmup_per_cell: int
    sustained: dict[str, Any]
    provenance: dict[str, Any]
    admissibility: dict[str, Any]
    vocab_size: int
    seed: int
    snapshot_every_s: float
    snapshot_window_s: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CampaignConfig:
        return cls(
            node_class=d["node_class"],
            model=d["model"],
            host=d["host"],
            endpoint=d["endpoint"],
            prompt_edges=d["prompt_edges"],
            output_edges=d["output_edges"],
            prompt_lens=d["prompt_lens"],
            output_lens=d["output_lens"],
            concurrencies=d["concurrencies"],
            samples_per_cell=d["samples_per_cell"],
            warmup_per_cell=d.get("warmup_per_cell", 2),
            sustained=d["sustained"],
            provenance=d["provenance"],
            admissibility=d["admissibility"],
            vocab_size=d["vocab_size"],
            seed=d["seed"],
            snapshot_every_s=d.get("snapshot_every_s", 30.0),
            snapshot_window_s=d.get("snapshot_window_s", 60.0),
        )


@dataclass
class CampaignResult:
    """The whole Week-2 output for one node class."""

    observations: list[cm.Observation] = field(default_factory=list)
    sustained: list[cm.Observation] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    f18: str = "partial"


def grid(config: CampaignConfig) -> list[GridPoint]:
    """The full cross product, ordered so concurrency varies slowest.

    Ordering is not cosmetic. Changing `--parallel` occupancy is the expensive transition
    — slots have to drain — so sweeping lengths inside a fixed concurrency keeps the
    engine in one regime for a whole block, and any thermal drift shows up *within* a
    concurrency level rather than being aliased onto it.
    """
    return [
        GridPoint(p, o, c)
        for c in config.concurrencies
        for p in config.prompt_lens
        for o in config.output_lens
    ]


def _observe(point: GridPoint, completion: ServiceResult, t_end_ns: int) -> cm.Observation:
    return cm.Observation(
        prompt_len=point.prompt_len,
        output_len=point.output_len,
        concurrency=point.concurrency,
        service_ns=completion.service_ns,
        output_tokens=completion.output_tokens,
        t_end_ns=t_end_ns,
        status=completion.status,
        prefill_ns=completion.prefill_ns,
        decode_ns=completion.decode_ns,
        error=completion.error,
    )


def _prompt_pool(seed: int, point: GridPoint, count: int, vocab_size: int) -> list[list[int]]:
    """Distinct prompts, materialized up front, deterministic from the campaign seed.

    Distinct rather than one prompt reused: identical token ids would let any prefix
    reuse the engine still does show up as a speedup that the pool will never see on a
    real trace. `cache_prompt` is already false in the adapter; this is the second lock on
    the same door, because a cost model inflated by cache hits is the kind of error that
    makes everything downstream look fine and be wrong.
    """
    streams = np.random.SeedSequence(seed).spawn(count)
    return [
        materialize(int(s.generate_state(1, dtype=np.uint32)[0]), point.prompt_len, vocab_size)
        for s in streams
    ]


async def _run_cell(
    adapter: LlamaCppAdapter, point: GridPoint, prompts: list[list[int]]
) -> list[cm.Observation]:
    """Fire `len(prompts)` requests holding exactly `point.concurrency` in flight."""
    sem = asyncio.Semaphore(point.concurrency)

    async def one(prompt: list[int]) -> cm.Observation:
        async with sem:
            completion = await adapter.complete(prompt, point.output_len)
            return _observe(point, completion, time.monotonic_ns())

    return list(await asyncio.gather(*(one(p) for p in prompts)))


async def _run_sustained(
    adapter: LlamaCppAdapter, point: GridPoint, prompts: list[list[int]], duration_s: float
) -> list[cm.Observation]:
    """Hold `point.concurrency` in flight for `duration_s`, replacing each completion.

    This is the segment MPR-1 is measured on, and its only job is to be *long and
    unchanging*. tau is the timescale on which a node's throughput decorrelates from
    itself; anything I vary during the segment would be measured as drift that the
    hardware did not produce.
    """
    deadline = time.monotonic_ns() + int(duration_s * 1e9)
    out: list[cm.Observation] = []

    async def worker(slot: int) -> None:
        i = slot
        while time.monotonic_ns() < deadline:
            completion = await adapter.complete(prompts[i % len(prompts)], point.output_len)
            out.append(_observe(point, completion, time.monotonic_ns()))
            i += point.concurrency

    await asyncio.gather(*(worker(k) for k in range(point.concurrency)))
    return out


def snapshot_series(
    base: dict[str, Any],
    sustained: list[cm.Observation],
    *,
    every_s: float,
    window_s: float,
    prompt_edges: list[int],
    output_edges: list[int],
) -> list[dict[str, Any]]:
    """Re-fit the sustained cell on a rolling window, every `every_s`, in time order.

    This is the requirement in C-3's description that is easy to miss: Aditya's staleness
    injection (F-8) serves the scheduler a snapshot from `s` seconds ago, and if I hand
    over a single fitted model he has to *synthesize* age by perturbing parameters — which
    would make H3 a study of his perturbation model instead of a study of real drift.

    Only the sustained cell is re-fitted. The rest of the table is carried forward from
    the grid pass unchanged, and that is deliberate rather than lazy: a recalibration
    heartbeat on a live node only observes the traffic the node is actually serving, so a
    snapshot that refreshed every cell would describe a measurement nobody could take.
    Each snapshot's `measured_at_unix` advances, which is what makes the series orderable
    and therefore ageable.
    """
    ok = sorted((o for o in sustained if o.status == "ok"), key=lambda o: o.t_end_ns)
    if not ok:
        return []
    t0, t_end = ok[0].t_end_ns, ok[-1].t_end_ns
    base_unix = base["measured_at_unix"]

    out: list[dict[str, Any]] = []
    step_ns, window_ns = int(every_s * 1e9), int(window_s * 1e9)
    mark = t0 + window_ns
    while mark <= t_end:
        window = [o for o in ok if mark - window_ns <= o.t_end_ns <= mark]
        if window:
            measured_at = base_unix + int((mark - t0) / 1e9)
            snap = dict(base)
            snap["measured_at_unix"] = measured_at
            # Sequenced, not just stamped: the id is second-granular and the manifest
            # references snapshots by it, so a fast cadence must not produce two snapshots
            # that a staleness lookup cannot tell apart.
            snap["snapshot_id"] = cm.snapshot_id(base["node_class"], measured_at, seq=len(out) + 1)
            snap["entries"] = _merge_cell(base["entries"], window, base, prompt_edges, output_edges)
            out.append(snap)
        mark += step_ns
    return out


def _merge_cell(
    entries: list[dict[str, Any]],
    window: list[cm.Observation],
    base: dict[str, Any],
    prompt_edges: list[int],
    output_edges: list[int],
) -> list[dict[str, Any]]:
    """Replace the one cell the window covers; leave the rest of the table alone."""
    refit = cm.build_snapshot(
        window,
        node_class=base["node_class"],
        prompt_edges=prompt_edges,
        output_edges=output_edges,
        provenance=base["provenance"],
        admissibility=base["admissibility"],
        calibration_run_ids=base["calibration_run_ids"],
        stochastic=base["stochastic"],
        measured_at_unix=base["measured_at_unix"],
    )
    fresh = {
        (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"]): e
        for e in refit["entries"]
    }
    return [
        fresh.get(
            (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"]),
            e,
        )
        for e in entries
    ]


async def run_campaign(adapter: LlamaCppAdapter, config: CampaignConfig) -> CampaignResult:
    """Grid pass, then the sustained segment, then the fits. One node class per call."""
    result = CampaignResult()
    points = grid(config)

    for point in points:
        total = config.warmup_per_cell + config.samples_per_cell
        prompts = _prompt_pool(config.seed, point, total, config.vocab_size)
        obs = await _run_cell(adapter, point, prompts)
        # Warmup is dropped by count, not by timestamp: `asyncio.gather` preserves input
        # order, so the first `warmup_per_cell` results are the first `warmup_per_cell`
        # requests regardless of the order they completed in.
        result.observations.extend(obs[config.warmup_per_cell :])
        if result.f18 == "partial":
            ok = [o for o in obs if o.status == "ok"]
            if ok:
                result.f18 = f18_status(
                    ServiceResult(
                        status="ok",
                        service_ns=ok[0].service_ns,
                        prompt_tokens=ok[0].prompt_len,
                        output_tokens=ok[0].output_tokens,
                        prefill_ns=ok[0].prefill_ns,
                        decode_ns=ok[0].decode_ns,
                    )
                )

    sustained_point = GridPoint(
        config.sustained["prompt_len"],
        config.sustained["output_len"],
        config.sustained["concurrency"],
    )
    pool = _prompt_pool(
        config.seed + 1,
        sustained_point,
        max(sustained_point.concurrency * 8, 8),
        config.vocab_size,
    )
    result.sustained = await _run_sustained(
        adapter, sustained_point, pool, float(config.sustained["duration_s"])
    )

    result.failures = _failure_counts(result.observations + result.sustained)
    _finish(result, config)
    return result


def _median_decode_tok_s(observations: list[cm.Observation]) -> float:
    """Median per-request decode tok/s over the sustained segment — the node's speed.

    Median rather than mean, and per-request rather than windowed, so the number does not
    depend on the window size that the tau estimate is sensitive to. This is the value
    that enters an R ratio, so its definition has to be the least fragile one available.
    """
    rates = [r for r in (o.decode_tokens_per_s for o in observations) if r is not None]
    return round(float(np.median(rates)), 4) if rates else 0.0


def _failure_counts(observations: list[cm.Observation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in observations:
        if o.status != "ok":
            counts[o.status] = counts.get(o.status, 0) + 1
    return counts


def _finish(result: CampaignResult, config: CampaignConfig) -> None:
    """Fit the table, then tau, then the snapshot series. Order matters.

    sigma is a residual of the fitted table (F-22), so the table has to exist before the
    stochastic block can be filled in — which means the first snapshot is fitted twice:
    once with a placeholder to get predictions to residual against, then again with the
    real sigma. Refitting is cheap and the alternative is a sigma that describes the raw
    spread across the whole grid rather than the error around the scheduler's actual
    prediction.
    """
    measured_at = int(time.time())
    run_id = f"cal_{config.node_class}_{measured_at}"
    every = {
        "node_class": config.node_class,
        "prompt_edges": config.prompt_edges,
        "output_edges": config.output_edges,
        "provenance": config.provenance,
        "admissibility": config.admissibility,
        "calibration_run_ids": [run_id],
        "measured_at_unix": measured_at,
    }
    all_obs = result.observations + result.sustained
    provisional = cm.build_snapshot(
        all_obs,
        stochastic={
            "model": "lognormal_multiplier",
            "sigma": 0.0,
            "autocorr_time_s": 1.0,
            "fit_r2": 0.0,
        },
        **every,
    )

    obs_ms, pred_ms = cm.residuals(all_obs, provisional)
    sigma = st.lognormal_sigma(obs_ms, pred_ms)

    ok_sustained = [o for o in result.sustained if o.status == "ok" and o.decode_ns]
    base_report = {
        "run_id": run_id,
        "node_class": config.node_class,
        "model": config.model,
        "host": config.host,
        "f18_status": result.f18,
        "n_grid_samples": len(result.observations),
        "n_sustained_samples": len(result.sustained),
        "failures": result.failures,
        "engine_config": config.provenance["engine_config"],
        "sigma": round(sigma, 5),
        # Reported from the sustained segment whether or not tau could be fitted. R is a
        # ratio of throughputs and the R range is its own Week-2 deliverable (F-9a) — it
        # must not be held hostage to whether this node happened to drift.
        "headline_tokens_per_s": _median_decode_tok_s(ok_sustained),
    }
    try:
        stationarity = st.characterize(
            [o.t_end_ns for o in ok_sustained],
            [o.decode_ns for o in ok_sustained],
            [o.output_tokens for o in ok_sustained],
            node_class=config.node_class,
            window_s=float(config.sustained["window_s"]),
            sigma=sigma,
            batch_size=int(config.sustained["concurrency"]),
        )
    except ValueError as exc:
        # The samples cost minutes of GPU time; a fit that cannot be made is not a reason
        # to throw them away. No tau means no C-3 snapshot — the schema requires
        # `autocorr_time_s > 0` and inventing one to satisfy it is the only genuinely
        # unacceptable outcome here — so the campaign writes its observations, says why
        # MPR-1 did not land, and exits non-zero.
        result.snapshots = []
        result.report = base_report | {
            "n_snapshots": 0,
            "stationarity": None,
            "stationarity_error": str(exc),
        }
        return

    final = cm.build_snapshot(
        all_obs,
        stochastic={
            "model": "lognormal_multiplier",
            "sigma": round(sigma, 5),
            "autocorr_time_s": round(stationarity.fit.tau_s, 4),
            "fit_r2": round(stationarity.fit.fit_r2, 4),
        },
        **every,
    )
    result.snapshots = [final] + snapshot_series(
        final,
        result.sustained,
        every_s=config.snapshot_every_s,
        window_s=config.snapshot_window_s,
        prompt_edges=config.prompt_edges,
        output_edges=config.output_edges,
    )

    result.report = base_report | {
        "n_snapshots": len(result.snapshots),
        "stationarity": stationarity.to_dict(),
        "stationarity_error": None,
    }


def load_config(path: Path) -> CampaignConfig:
    return CampaignConfig.from_dict(json.loads(Path(path).read_text()))


def write_result(out_dir: Path, result: CampaignResult) -> None:
    """One directory per campaign: raw samples, the snapshot series, the MPR-1 report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "observations.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "prompt_len": o.prompt_len,
                    "output_len": o.output_len,
                    "concurrency": o.concurrency,
                    "service_ns": o.service_ns,
                    "output_tokens": o.output_tokens,
                    "t_end_ns": o.t_end_ns,
                    "status": o.status,
                    "prefill_ns": o.prefill_ns,
                    "decode_ns": o.decode_ns,
                    "error": o.error,
                    "segment": seg,
                },
                separators=(",", ":"),
            )
            + "\n"
            for seg, group in (("grid", result.observations), ("sustained", result.sustained))
            for o in group
        )
    )
    snap_dir = out_dir / "snapshots"
    snap_dir.mkdir(exist_ok=True)
    for i, snap in enumerate(result.snapshots):
        (snap_dir / f"{i:03d}_{snap['snapshot_id']}.json").write_text(
            json.dumps(snap, indent=2) + "\n"
        )
    (out_dir / "campaign.json").write_text(json.dumps(result.report, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Week-2 calibration campaign (F-6, F-9a, MPR-1)")
    ap.add_argument("--config", type=Path, required=True, help="campaign config (F-20)")
    ap.add_argument("--out", type=Path, default=Path("runs/calibration"))
    ap.add_argument("--endpoint", help="override the config's llama-server endpoint for this run")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    if args.endpoint:
        config = CampaignConfig.from_dict(
            {**json.loads(args.config.read_text()), "endpoint": args.endpoint}
        )

    async def _go() -> CampaignResult:
        async with LlamaCppAdapter(
            config.endpoint,
            node_id=config.node_class,
            timeout_ceiling_ms=config.admissibility["timeout_ceiling_ms"],
            engine_version=config.provenance["engine_version"],
        ) as adapter:
            if not await adapter.health():
                raise SystemExit(
                    f"no healthy llama-server at {config.endpoint} — start it with the "
                    "engine_config this campaign claims to be calibrating"
                )
            return await run_campaign(adapter, config)

    result = asyncio.run(_go())
    out_dir = args.out / result.report["run_id"]
    write_result(out_dir, result)

    print(
        f"{out_dir}  {result.report['n_grid_samples']} grid + "
        f"{result.report['n_sustained_samples']} sustained samples, "
        f"{result.report['n_snapshots']} snapshots, f18={result.f18}"
    )
    if result.failures:
        print(f"       failures (not fitted): {result.failures}")

    s = result.report["stationarity"]
    if s is None:
        print(f"MPR-1  NOT ESTABLISHED — {result.report['stationarity_error']}")
        print(f"       {len(result.sustained)} sustained samples kept in {out_dir}")
        return 1
    print(
        f"MPR-1  tau = {s['autocorr_time_s']:.2f}s (integrated {s['integrated_autocorr_time_s']:.2f}s, "
        f"1/e {s['e_folding_s']:.2f}s, r2 {s['fit_r2']:.3f})"
    )
    print(
        f"       envelope p05-p95 {s['envelope']['p05_tok_s']:.1f}-{s['envelope']['p95_tok_s']:.1f} tok/s "
        f"(band {s['envelope']['band_ratio']:.2f}x, CV {s['envelope']['cv']:.3f})"
    )
    print(
        f"       a single calibrated mean understates its own standard error by "
        f"{s['se_inflation']:.2f}x ({s['n_eff']:.1f} independent windows in {s['n_windows']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
