"""C-5 / F-19 — join the three logs into one tidy per-request record set.

    manifest.json ─┐
    trace.jsonl   ─┤
    client_*.jsonl─┼─> one row per request ─> Parquet ─> every figure
    scheduler_*   ─┤
    worker_*      ─┘

A pure function of files. No network, no engine, runnable on a laptop — which is the
point: Aditya can hand me a directory of **simulator** logs in exactly this shape and the
pipeline processes them with zero changes, so hardware and simulated runs are analysed by
the same code and F-23's comparison is like-for-like.

Two rules the join exists to enforce, both easy to violate by accident:

**No cross-host clock subtraction.** `e2e_ms` is entirely client-local. `queue_wait_ms`,
`service_ms` and the F-18 split are entirely worker-local. `decide_us` is entirely
scheduler-local. Whatever is left over is `transport_residual_ms` — one honest residual,
reported as a single number rather than decomposed into invented stages. It can come out
negative when a request is rejected before it ever reaches a worker, and that is
*information*: a systematically negative residual means the durations do not add up and
the run should be looked at, not averaged.

**One clock correction, and it is not the offset.** When the manifest carries a
`clock_sync` block, the worker-local durations are divided by that host's measured clock
*rate* error relative to the reference host, which puts them on the same timebase as
`e2e_ms` before the residual is taken. The offset is not subtracted from anything, because
there is nothing here to subtract it from: every term above is a single-host duration, and
a constant offset cancels in their difference exactly. What does not cancel is rate, since
it scales each duration by its own length. On disciplined hardware the correction is
microseconds on a duration quoted in milliseconds; `summarize` prints its size so that
"the clocks did not matter" is a measured statement rather than an assumed one. With no
`clock_sync` block nothing is scaled at all, so a run joined before this existed joins to
the same numbers.

**The engine-gap probe is not a pool member.** A node with `role: "engine_gap_probe"`
(F-9b) participates in no policy comparison, so its rows are dropped here rather than
filtered in each figure script — the schema comment on `R` says so, and doing it once is
the difference between a rule and a habit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

# Reaching into `harness` from the pipeline, on purpose. `clock_sync` is a C-6 field, C-6
# is written by the harness, and the code that decodes the field belongs beside the code
# that writes it: two copies of "what rate_error_ppm means" is one more than can stay in
# agreement. Nothing heavy comes with it, so the pipeline is still laptop-runnable.
from dataplane.harness.clocksync import rate_factors

__all__ = ["join", "load_jsonl", "summarize", "to_frame", "write_parquet"]

# C-5 column order. Fixed so two runs of the pipeline produce comparable Parquet and a
# diff of two record sets is readable.
COLUMNS = [
    "run_id",
    "policy",
    "lambda",
    "staleness_s",
    "R",
    "node_count",
    "req_id",
    "bucket_id",
    "prompt_len",
    "output_len",
    "priority",
    "intended_offset_s",
    "send_lag_ms",
    "e2e_ms",
    "status",
    "chosen_node",
    "decide_us",
    "chosen_queue_depth",
    "chosen_est_age_ms",
    "best_alt_node",
    "best_alt_est_service_ms",
    "routing_error_ms",
    "queue_wait_ms",
    "prefill_ms",
    "decode_ms",
    "service_ms",
    "transport_residual_ms",
    "is_warmup",
]


def to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """The rows as a DataFrame with C-5's column order fixed.

    Fixed order so two runs of the pipeline produce comparable Parquet and a diff of two
    record sets is readable by a human rather than by column name lookup.
    """
    return pd.DataFrame(rows, columns=COLUMNS)


def summarize(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    """What has to be said out loud about a joined record set before anyone averages it."""
    probes = sum(1 for n in manifest["nodes"] if n.get("role", "pool") != "pool")
    missing_decision = sum(1 for r in rows if r["decide_us"] is None)
    missing_worker = sum(1 for r in rows if r["service_ms"] == 0.0 and r["queue_wait_ms"] == 0.0)
    warmup = sum(1 for r in rows if r["is_warmup"])
    negative = sum(1 for r in rows if r["transport_residual_ms"] < 0)

    out = [f"{len(rows)} rows  ({warmup} warmup, {len(rows) - warmup} measured)"]
    if missing_decision:
        out.append(
            f"{missing_decision} request(s) have no scheduler decision — dispatch was "
            "rejected or the scheduler log is short; their scheduler columns are null"
        )
    if missing_worker:
        out.append(
            f"{missing_worker} request(s) have no worker record: their worker-local "
            "durations are 0.0 and must not be averaged as service times"
        )
    if negative:
        out.append(
            f"{negative} row(s) have a negative transport residual — the per-host durations "
            "do not add up; check clocks before believing e2e"
        )
    if probes:
        out.append(
            f"excluded {probes} engine-gap-probe node(s) (F-9b): they are a measured "
            "condition, not pool members"
        )
    out.extend(_clock_lines(rows, manifest))
    return out


def _clock_lines(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> list[str]:
    """What the clock measurement says about these rows, in words. Silent when absent.

    The size of the correction is quoted against the longest worker-local duration in the
    set, because that is where a rate error is largest: the error is a fraction of the
    duration, so the worst case is the slowest request, not the average one. Quoting it
    against the mean would understate the thing the line exists to bound.
    """
    sync = manifest.get("clock_sync")
    if not sync:
        hosts = {n.get("host") for n in manifest.get("nodes", [])}
        if len(hosts) > 1:
            return [
                (
                    f"no clock_sync block, and this run spans {len(hosts)} hosts: nothing "
                    "here is wrong, but the claim that the hosts' clocks ticked at the "
                    "same rate is now unevidenced rather than measured"
                )
            ]
        return []

    out = [
        (
            f"clock_sync on reference {sync.get('reference')!r}: worst pair offset "
            f"{float(sync.get('max_abs_offset_ms', 0.0)):.3f} ms (subtracted from nothing; "
            f"every duration here is single-host), worst pair rate difference "
            f"{float(sync.get('max_rate_error_ppm', 0.0)):.3f} ppm"
        )
    ]
    hosts_block = sync.get("hosts") or {}
    unsync = sorted(h for h, c in hosts_block.items() if not c.get("synchronised"))
    if unsync:
        out.append(
            f"host(s) {unsync} were not synchronised, so their worker-local durations are "
            "on an undisciplined clock and were left uncorrected"
        )
    factors = _node_rate_factors(manifest)
    if factors:
        worst_ms = max((r["service_ms"] for r in rows if r["chosen_node"] in factors), default=0.0)
        worst_factor = max(abs(1.0 - 1.0 / f) for f in factors.values())
        out.append(
            f"rate correction applied to {len(factors)} node(s): at most "
            f"{worst_ms * worst_factor:.4f} ms on the longest service time in this set "
            f"({worst_ms:.1f} ms), which is the whole of what the clocks could have cost"
        )
    return out


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping blank lines. Used for all four inputs."""
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _check_inputs(
    *,
    manifest: dict[str, Any],
    client: list[dict[str, Any]],
    scheduler: list[dict[str, Any]],
    worker: list[dict[str, Any]],
    trace_sha256: str | None,
    allow_invalid: bool,
) -> None:
    """Three refusals, all of them cheap here and unrecoverable later.

    **Mismatched run_ids.** Joining one run's client log against another's worker log
    produces rows that look perfectly well-formed and describe a run that never happened.
    Nothing downstream can detect it, because every field is individually plausible.

    **A manifest naming a different trace.** The trace SHA-256 is the workload's identity
    (C-2). A manifest pointing at another trace means the lengths in these rows are not
    the lengths that were served, so every per-bucket result is misattributed.

    **A run the harness already marked invalid.** The load generator drifted, or requests
    were dropped, or nodes were co-located. That run is not a data point about scheduling
    and joining it silently is how it ends up in a figure. `allow_invalid=True` exists for
    looking at a bad run deliberately.
    """
    run_id = manifest["run_id"]
    for name, records in (("client", client), ("scheduler", scheduler), ("worker", worker)):
        wrong = {r["run_id"] for r in records if r.get("run_id") != run_id}
        if wrong:
            raise ValueError(
                f"{name} log carries run_id(s) {sorted(wrong)} but the manifest says "
                f"{run_id!r} — these logs are from different runs and joining them would "
                "describe a run that never happened"
            )
    if trace_sha256 is not None and manifest.get("trace_sha256") != trace_sha256:
        raise ValueError(
            f"manifest names trace {manifest.get('trace_sha256')!r} but the trace supplied "
            f"hashes to {trace_sha256!r} — the workload is not the one this run replayed"
        )
    if not allow_invalid and not manifest.get("validity", {}).get("valid", True):
        raise ValueError(
            f"run {run_id!r} is marked invalid in its manifest and is not a data point "
            "about scheduling; pass allow_invalid=True to look at it anyway"
        )
    _check_policy_agreement(manifest, scheduler)


def _check_policy_agreement(manifest: dict[str, Any], scheduler: list[dict[str, Any]]) -> None:
    """The scheduler must have run the policy the manifest says the run was.

    Every C-5 row takes its `policy` label from the manifest, because that is the field
    the launcher wrote and the one F-1 makes the single configuration value. Nothing was
    checking that the scheduler agreed, and the failure that gap admits is the worst kind
    available here: a scheduler that dispatches round-robin while writing a different name
    into its own log produces a run set where every row is labelled with a policy that
    never ran. Nothing downstream can detect it, because the numbers are real and the
    label is plausible, and a policy comparison built from it compares one policy against
    itself under four names.

    Checked against the decision records rather than trusted, because the two labels have
    two authors: the launcher writes the manifest and the scheduler writes the log. Two
    sources for one fact is exactly where they diverge.

    A run with no decision records says nothing about its policy and is left alone. That is
    the fixture scheduler, which is honest about writing none.
    """
    declared = manifest.get("policy")
    claimed = {d.get("policy") for d in scheduler if d.get("type") == "decision"}
    claimed.discard(None)
    if not claimed or claimed == {declared}:
        return
    raise ValueError(
        f"manifest says this run used policy {declared!r} but its scheduler log records "
        f"{sorted(claimed)}. Every joined row would be labelled {declared!r} regardless, "
        "so this is a run set that describes a comparison nobody ran (F-1)"
    )


def _candidate_view(decision: dict[str, Any], output_len: int) -> dict[str, Any]:
    """Pull the chosen node's view and the best admissible alternative out of F-3's array.

    `best_alt_est_service_ms` is a *counterfactual estimate*, and it is worth being
    precise about whose estimate it is: it is built from what the scheduler believed at
    decision time — the candidate's `capability_tok_s` and `queue_depth` as the staleness
    veil served them — not from what that node would actually have done. So
    `routing_error_ms` measures **decision quality given the information available**,
    which is the quantity H3 is about. A cost-model counterfactual would measure something
    different and is a Week-5 refinement, not a drop-in replacement.

    The estimate is `(queue_depth + inflight + 1) * output_len / capability_tok_s`: the
    request has to wait out everything already on the node and then decode its own tokens.
    First-order, and stated rather than buried.

    `inflight` belongs in that sum and leaving it out was a real hole. A node holding four
    requests in the engine and none in the buffer reports `queue_depth = 0`, so a
    queue-depth-only estimate scored it identically to a node sitting idle, and routing a
    request into a saturated engine came out as zero routing error. It also disagreed with
    the policy it grades: `WJSQ` scores on `queue_depth + inflight`, so the estimator was
    marking decisions against a different notion of load than the one that made them.
    """
    candidates = decision.get("candidates") or []
    chosen_id = decision.get("chosen_node")

    def est_ms(c: dict[str, Any]) -> float | None:
        cap = c.get("capability_tok_s") or 0.0
        if cap <= 0:
            return None
        pending = c.get("queue_depth", 0) + c.get("inflight", 0)
        return (pending + 1) * output_len / cap * 1000.0

    chosen = next((c for c in candidates if c.get("node_id") == chosen_id), None)
    alts = [
        c
        for c in candidates
        if c.get("node_id") != chosen_id and c.get("admissible") and est_ms(c) is not None
    ]
    best_alt = min(alts, key=lambda c: est_ms(c)) if alts else None

    chosen_est = est_ms(chosen) if chosen is not None else None
    best_alt_est = est_ms(best_alt) if best_alt is not None else None
    routing_error = (
        max(chosen_est - best_alt_est, 0.0)
        if chosen_est is not None and best_alt_est is not None
        else None
    )
    return {
        "chosen_queue_depth": int(chosen["queue_depth"]) if chosen else 0,
        "chosen_est_age_ms": int(chosen["estimate_age_ms"]) if chosen else 0,
        "best_alt_node": best_alt["node_id"] if best_alt else None,
        "best_alt_est_service_ms": best_alt_est,
        "routing_error_ms": routing_error,
    }


def _node_rate_factors(manifest: dict[str, Any]) -> dict[str, float]:
    """node_id -> the factor its worker-local durations are divided by. Usually empty.

    Empty means "nothing to correct", which is the case for every single-host run and for
    every run measured before `clock_sync` existed, so the default path does no arithmetic
    at all rather than multiplying by a 1.0 that a reader then has to convince themselves
    is exactly 1.0.

    The scheduler is deliberately left out. C-6 does not record which host it ran on, and
    `decide_us` is tens of microseconds, so the largest rate error this study would accept
    (100 ppm) moves it by a few picoseconds. Correcting it would be arithmetic performed
    to look thorough.
    """
    factors = rate_factors(manifest.get("clock_sync"))
    if not factors:
        return {}
    return {n["node_id"]: factors[n["host"]] for n in manifest["nodes"] if n.get("host") in factors}


def join(
    *,
    manifest: dict[str, Any],
    client: list[dict[str, Any]],
    scheduler: list[dict[str, Any]],
    worker: list[dict[str, Any]],
    trace: list[dict[str, Any]] | None = None,
    trace_sha256: str | None = None,
    r_value: float | None = None,
    allow_invalid: bool = False,
) -> list[dict[str, Any]]:
    """The C-5 record set: one row per client record — the client saw every request.

    Returns plain rows rather than a DataFrame. The rows are the contract; the frame is
    one way to hold them, and keeping them separable means a caller can validate a single
    row against the C-5 schema without pandas in the loop.

    `trace` is optional because the per-request length fields are the only thing it
    supplies, and a run can be joined without it — a fake-scheduler smoke run, say. The
    length columns are then zero rather than absent, since C-5 types them as integers.
    """
    _check_inputs(
        manifest=manifest,
        client=client,
        scheduler=scheduler,
        worker=worker,
        trace_sha256=trace_sha256,
        allow_invalid=allow_invalid,
    )
    pool = [n for n in manifest["nodes"] if n.get("role", "pool") == "pool"]
    pool_ids = {n["node_id"] for n in pool}

    by_req = {r["req_id"]: r for r in (trace or []) if r.get("record") == "req"}
    decisions = {d["req_id"]: d for d in scheduler if d.get("type") == "decision"}
    # Worker records from probe nodes are dropped before the join, not filtered after.
    workers = {w["req_id"]: w for w in worker if w["node_id"] in pool_ids}

    warmup_s = float(manifest.get("warmup_s", 0.0))
    node_rate = _node_rate_factors(manifest)
    rows: list[dict[str, Any]] = []

    for c in client:
        req_id = c["req_id"]
        t = by_req.get(req_id, {})
        d = decisions.get(req_id)
        w = workers.get(req_id)
        output_len = int(t.get("output_len", 0))
        # No decision record means the scheduler never spoke for this request — it was
        # rejected at dispatch, or its log is short. Every scheduler-side column is then
        # null, not zero: a zero `decide_us` is a measurement of an instantaneous decision,
        # and a zero `chosen_queue_depth` says the chosen node was idle. Both are claims
        # nobody made.
        cand = (
            _candidate_view(d, output_len)
            if d
            else {
                "chosen_queue_depth": None,
                "chosen_est_age_ms": None,
                "best_alt_node": None,
                "best_alt_est_service_ms": None,
                "routing_error_ms": None,
            }
        )

        e2e_ms = c["e2e_duration_ns"] / 1e6
        decide_us = (d["decide_duration_ns"] / 1e3) if d else None
        # Worker-local durations onto the client's timebase. 1.0 unless the manifest
        # measured this node's host as ticking at a different rate from the reference.
        rate = node_rate.get(w["node_id"], 1.0) if w else 1.0
        queue_wait_ms = (w["queue_wait_ns"] / 1e6 / rate) if w else 0.0
        service_ms = (w["service_ns"] / 1e6 / rate) if w else 0.0

        rows.append(
            {
                "run_id": manifest["run_id"],
                "policy": manifest["policy"],
                "lambda": float(manifest["lambda"]),
                "staleness_s": float(manifest["staleness_s"]),
                "R": float(r_value) if r_value is not None else float("nan"),
                "node_count": len(pool),
                "req_id": req_id,
                "bucket_id": t.get("bucket_id", ""),
                "prompt_len": int(t.get("prompt_len", 0)),
                "output_len": output_len,
                "priority": int(t.get("priority", 0)),
                "intended_offset_s": float(c["intended_offset_s"]),
                "send_lag_ms": float(c["send_lag_ms"]),
                "e2e_ms": e2e_ms,
                "status": c["status"],
                "chosen_node": d.get("chosen_node") if d else None,
                "decide_us": decide_us,
                **cand,
                "queue_wait_ms": queue_wait_ms,
                "prefill_ms": (w["prefill_ns"] / 1e6 / rate) if w and "prefill_ns" in w else None,
                "decode_ms": (w["decode_ns"] / 1e6 / rate) if w and "decode_ns" in w else None,
                "service_ms": service_ms,
                # The one honest residual: everything the three hosts did not account for.
                "transport_residual_ms": e2e_ms
                - ((decide_us or 0.0) / 1e3 + queue_wait_ms + service_ms),
                "is_warmup": bool(c["intended_offset_s"] < warmup_s),
            }
        )

    return rows


def write_parquet(rows: list[dict[str, Any]] | pd.DataFrame, path: str | Path) -> Path:
    """Write the record set to Parquet. Accepts rows or an already-built frame.

    The nulls are load-bearing and survive the round trip on purpose: a `prefill_ms` that
    came back 0.0 instead of null would say the backend measured an instantaneous prefill
    rather than that it does not report one (F-18 partial), and every figure downstream
    would average that in.
    """
    frame = rows if isinstance(rows, pd.DataFrame) else to_frame(rows)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def _one(run_dir: Path, pattern: str) -> list[dict[str, Any]]:
    """Concatenate every file matching a glob; missing files are an empty list.

    Worker logs are per node, so there are several; client and scheduler logs are one
    each. Treating them all as globs means the caller does not have to know which is
    which, and a run with no scheduler log (a worker-only calibration) still joins.
    """
    out: list[dict[str, Any]] = []
    for p in sorted(run_dir.glob(pattern)):
        out.extend(load_jsonl(p))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C-5 — join the three logs into Parquet (F-19)")
    ap.add_argument("run_dir", type=Path, help="directory holding manifest.json and the logs")
    ap.add_argument("--trace", type=Path, required=True, help="the C-2 trace the run replayed")
    ap.add_argument("--out", type=Path, help="output Parquet (default: <run_dir>/joined.parquet)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="join a run the manifest marks invalid; the output must not be analysed",
    )
    ap.add_argument(
        "--r",
        type=float,
        help="heterogeneity ratio for this run; the manifest does not carry one, so it is "
        "passed explicitly rather than guessed (see calibration/r_range.py)",
    )
    args = ap.parse_args(argv)

    manifest = json.loads((args.run_dir / "manifest.json").read_text())
    rows = join(
        manifest=manifest,
        trace=load_jsonl(args.trace),
        client=_one(args.run_dir, "client_*.jsonl"),
        scheduler=_one(args.run_dir, "scheduler_*.jsonl"),
        worker=_one(args.run_dir, "worker_*.jsonl"),
        r_value=args.r,
        allow_invalid=args.force,
    )
    out = write_parquet(to_frame(rows), args.out or args.run_dir / "joined.parquet")
    print(out)
    for line in summarize(rows, manifest):
        print(f"  {line}")
    if not manifest["validity"]["valid"]:
        print("  run is marked INVALID in the manifest — joined under --force, do not analyse it")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
