"""F-13 / F-14 / F-15 — the admissible set, and the cliff outside it.

Extreme heterogeneity produces a *categorical* failure, not a latency tail. A long-context
request routed to a CPU node does not arrive late; it OOMs, or it exceeds any reasonable
timeout. Naive tail statistics over that are degenerate — a p99 computed across a mixture
of "slow" and "never" is not a latency, it is an artifact of where the timeout was set.

So the study splits the space in two, and both halves are reported:

**The admissible set (F-13)** is the `(prompt, output)` range that *every* pool node can
serve within the timeout ceiling. It is an intersection, not an average: one CPU node that
cannot do 2048-token prompts shrinks the whole study's range, and that shrunk range is
reported alongside the results rather than quietly applied.

**The cliff (F-15)** is what happens outside it, characterized separately as a standalone
observation. Its location is a property of the pool worth reporting on its own.

F-14 makes admissibility a hard constraint in every policy, baselines included, so that no
policy is penalized for producing infinite-latency outcomes it had no way to avoid. That
constraint is Aditya's to enforce at dispatch; what this module does is *tell him where the
boundary is*, from measurement rather than from a guess.

The boundary is drawn on **p95, not the mean**. A bucket whose mean fits under the ceiling
but whose p95 does not will time out one request in twenty, and those timeouts land in the
tail statistics the study is about.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dataplane.calibration import cost_model
from dataplane.calibration.cost_model import Observation

__all__ = [
    "ANCHOR_ROOT",
    "BUCKET_LADDER",
    "Cliff",
    "NodeLimit",
    "buckets_within",
    "cliff_from_observations",
    "determine",
    "load_anchors",
    "load_observations",
    "measured_extent",
    "node_limit_from_snapshot",
    "pool_envelope",
    "summary",
]

# The candidate (prompt, output) ladder a trace can be built from. Powers of two on both
# axes, because the cost model buckets on the same scale and a length that lands mid-bucket
# would be priced by a cell it is not representative of.
BUCKET_LADDER: tuple[tuple[int, int], ...] = (
    (128, 64),
    (256, 64),
    (512, 128),
    (1024, 128),
    (2048, 256),
)

# Where F-23's validation anchors live: one directory per run, each holding the manifest the
# harness wrote. Resolved against the repository root rather than the working directory,
# because a relative default fails in the one way that is hardest to notice — `load_anchors`
# would return an empty list from anywhere but the repo root, and an empty list reads as
# "no anchors have been collected" rather than "you are in the wrong directory". The test
# suite runs from `dataplane/`, which is exactly that case.
ANCHOR_ROOT = Path(__file__).resolve().parents[4] / "runs" / "anchors"


@dataclass(frozen=True)
class NodeLimit:
    """The widest bucket one node class can serve inside the ceiling."""

    node_class: str
    max_prompt: int
    max_output: int
    limiting_concurrency: int
    worst_p95_ms: float


def summary(envelope: dict[str, Any]) -> str:
    """One line for the console and for the caption that reports the restricted range.

    F-13 requires the restricted range to be reported *alongside* results, so it needs a
    form a human will actually paste.
    """
    limiting = envelope.get("limiting_node")
    return (
        f"admissible set: prompt <= {envelope['max_prompt']}, "
        f"output <= {envelope['max_output']} at a "
        f"{envelope['timeout_ceiling_ms']} ms ceiling"
        + (f" (limited by {limiting})" if limiting else "")
    )


def node_limit_from_snapshot(snapshot: dict[str, Any], ceiling_ms: int) -> dict[str, Any]:
    """Turn a C-3 snapshot into the flat envelope `determine` consumes.

    The widest bucket pair whose p95 fits under the ceiling **at every calibrated
    concurrency**. "At every concurrency" is the strict reading and the right one: a bucket
    that fits at concurrency 1 and blows the ceiling at concurrency 4 is not admissible,
    because the scheduler will absolutely put four requests on that node under load — that
    is what the load band is.

    p95 rather than the mean, because a bucket whose mean fits and whose p95 does not will
    time out one request in twenty, and those timeouts land in the tail statistics the
    study is about.
    """
    fits: list[tuple[int, int]] = []
    worst = 0.0
    limiting_conc = 0
    by_bucket: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for e in snapshot["entries"]:
        by_bucket.setdefault((e["prompt_bucket"][1], e["output_bucket"][1]), []).append(e)

    for (p_hi, o_hi), entries in by_bucket.items():
        worst_here = max(entries, key=lambda e: e["service_ms_p95"])
        if worst_here["service_ms_p95"] <= ceiling_ms:
            fits.append((p_hi, o_hi))
        elif worst_here["service_ms_p95"] > worst:
            worst = worst_here["service_ms_p95"]
            limiting_conc = worst_here["concurrency"]

    return {
        "node_class": snapshot["node_class"],
        "max_prompt": max((p for p, _ in fits), default=0),
        "max_output": max((o for _, o in fits), default=0),
        "timeout_ceiling_ms": ceiling_ms,
        "limiting_concurrency": limiting_conc,
        "worst_p95_ms": worst,
    }


def buckets_within(envelope: dict[str, Any]) -> list[str]:
    """The trace bucket ids that fit inside an envelope, as `p{prompt}_o{output}`.

    This is the loop that closes Week 1 against Week 3: `gen_trace` refuses any bucket
    outside the admissible envelope it is handed, and this is what proposes buckets that
    will not be refused. Generating a trace and discovering at replay time that a node
    cannot serve it is the F-13 failure this exists to prevent.
    """
    max_p = envelope["max_prompt"]
    max_o = envelope["max_output"]
    return [f"p{p}_o{o}" for p, o in BUCKET_LADDER if p <= max_p and o <= max_o]


def determine(
    envelopes: list[dict[str, Any]], *, timeout_ceiling_ms: int | None = None
) -> dict[str, Any]:
    """Intersect the per-node envelopes. The slowest node sets the study's range (F-13).

    Returns the envelope as a plain dict — the same shape it consumes, the same shape
    `buckets_within` takes, and the same shape C-2's `admissible` header block wants. One
    shape for one concept, so the Week-1 trace generator and the Week-3 pool description
    cannot drift apart through an adapter nobody updates.

    Takes flat `{node_class?, max_prompt, max_output, timeout_ceiling_ms}` dicts — one per
    pool node. `node_limit_from_snapshot` produces them from a C-3 snapshot; a node whose
    limits are known some other way can be passed directly.

    The ceiling defaults to the **tightest** one in the pool rather than the loosest or a
    mean. A node calibrated against a 60 s ceiling was never asked whether it could do the
    work in 30 s, so borrowing another node's looser ceiling would admit lengths nobody
    measured on it.

    An empty intersection raises rather than returning an empty set. A pool with no common
    envelope cannot run a comparable workload at all, and F-13 says the primary study
    operates over a range every node can serve — if that range is empty the honest options
    are to raise the ceiling or drop the node, both of which are decisions rather than
    defaults, and neither is one this function should make silently.
    """
    if not envelopes:
        raise ValueError("an admissible set is an intersection across the pool; got 0 nodes")
    ceiling = (
        timeout_ceiling_ms
        if timeout_ceiling_ms is not None
        else min(int(e["timeout_ceiling_ms"]) for e in envelopes)
    )
    limits = [
        NodeLimit(
            node_class=e.get("node_class", f"node_{i}"),
            max_prompt=int(e["max_prompt"]),
            max_output=int(e["max_output"]),
            limiting_concurrency=int(e.get("limiting_concurrency", 0)),
            worst_p95_ms=float(e.get("worst_p95_ms", 0.0)),
        )
        for i, e in enumerate(envelopes)
    ]
    max_prompt = min(limit.max_prompt for limit in limits)
    max_output = min(limit.max_output for limit in limits)
    binding = min(limits, key=lambda limit: (limit.max_prompt, limit.max_output))
    if max_prompt <= 0 or max_output <= 0:
        raise ValueError(
            f"no common admissible envelope at a {ceiling} ms ceiling — {binding.node_class} "
            "can serve no calibrated bucket in time, so the pool cannot run a comparable "
            "workload. Raise the ceiling or drop that node; both are decisions, not defaults"
        )
    return {
        "max_prompt": max_prompt,
        "max_output": max_output,
        "timeout_ceiling_ms": ceiling,
        "limiting_node": binding.node_class if len(limits) > 1 else None,
        "per_node": [
            {
                "node_class": limit.node_class,
                "max_prompt": limit.max_prompt,
                "max_output": limit.max_output,
                "limiting_concurrency": limit.limiting_concurrency,
                "worst_p95_ms": round(limit.worst_p95_ms, 3),
            }
            for limit in limits
        ],
    }


def load_anchors(root: str | Path | None = None) -> list[dict[str, Any]]:
    """F-23's validation anchors: manifests of the hardware runs the DES is judged against.

    An anchor is ground truth, so this refuses to hand back anything that cannot serve as
    ground truth. Every anchor must be marked valid, and all of them must replay the *same*
    trace — otherwise the simulator/hardware comparison is confounded with a workload
    difference, and F-23's "agreement at three operating points" is answering three
    different questions.

    The three points are a Week-3 deliverable: live runs at three offered loads on a real
    multi-node pool. Until those exist this returns an empty list and the checks that
    depend on it fail loudly, which is the correct outcome — validating a simulator against
    nothing is worse than not validating it.
    """
    root = Path(root) if root is not None else ANCHOR_ROOT
    if not root.exists():
        return []
    anchors = [json.loads(p.read_text()) for p in sorted(root.glob("*/manifest.json"))]
    bad = [a["run_id"] for a in anchors if not a.get("validity", {}).get("valid", False)]
    if bad:
        raise ValueError(
            f"anchor run(s) {bad} are marked invalid — anchoring the simulator to a run "
            "whose load generator drifted moves the target instead of failing the check"
        )
    traces = {a.get("trace_sha256") for a in anchors}
    if len(traces) > 1:
        raise ValueError(
            f"anchor runs replay {len(traces)} different traces — F-23 compares vehicles on "
            "identical replayed traces, so a workload difference would be indistinguishable "
            "from a simulator error"
        )
    return anchors


@dataclass(frozen=True)
class Cliff:
    """F-15 — where a node stops degrading and starts failing, as a standalone observation."""

    node_class: str
    n_total: int
    n_failed: int
    failures_by_status: dict[str, int]
    first_failing_prompt: int | None
    first_failing_output: int | None

    @property
    def failure_rate(self) -> float:
        return self.n_failed / self.n_total if self.n_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_class": self.node_class,
            "n_total": self.n_total,
            "n_failed": self.n_failed,
            "failure_rate": round(self.failure_rate, 5),
            "failures_by_status": dict(sorted(self.failures_by_status.items())),
            "first_failing_prompt": self.first_failing_prompt,
            "first_failing_output": self.first_failing_output,
        }


def cliff_from_observations(observations: list[Any], *, node_class: str) -> Cliff:
    """Characterize the cliff from a campaign's raw samples.

    Takes the campaign's `Observation` records rather than the fitted cost model, because
    the cost model deliberately excludes failures — a fitted table cannot tell you where
    the cliff is, by construction. This is what those discarded samples are *for*.

    "First failing" is the shortest length that ever failed, not the length at which
    failure becomes certain. The cliff is a region, not a line, and reporting the near
    edge is the conservative choice for an admissibility boundary.
    """
    failed = [o for o in observations if o.status != "ok"]
    by_status: dict[str, int] = {}
    for o in failed:
        by_status[o.status] = by_status.get(o.status, 0) + 1
    return Cliff(
        node_class=node_class,
        n_total=len(observations),
        n_failed=len(failed),
        failures_by_status=by_status,
        first_failing_prompt=min((o.prompt_len for o in failed), default=None),
        first_failing_output=min((o.output_len for o in failed), default=None),
    )


def load_observations(run_dir: str | Path) -> list[Observation]:
    """Read a calibration run's raw samples back, failures included.

    `observations.jsonl` carries a `segment` field the `Observation` dataclass has no slot
    for — it is the campaign's own bookkeeping, not part of a sample — so unknown keys are
    dropped rather than passed through. Dropping them here keeps the cliff computation
    working when the campaign learns to record something new about a segment.
    """
    fields = {f.name for f in dataclasses.fields(Observation)}
    path = Path(run_dir) / "observations.jsonl"
    return [
        Observation(**{k: v for k, v in json.loads(line).items() if k in fields})
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def measured_extent(observations: list[Observation]) -> dict[str, int]:
    """The longest prompt and output this node actually served, successfully.

    This is the honesty check on an envelope, and it exists because a bucket is named by
    its **ceiling** and sampled in its **interior**. The `(513, 2048)` bucket admitted on
    the strength of samples at `prompt_len=1024` is a claim about 2048 that nothing in the
    campaign tested. The envelope still reports the ceiling — that is what C-2's header
    and the trace generator consume — but `evidence` reports what was measured, and when
    the two differ the difference is printed rather than left for someone to notice.
    """
    ok = [o for o in observations if o.status == "ok"]
    return {
        "max_prompt_measured": max((o.prompt_len for o in ok), default=0),
        "max_output_measured": max((o.output_len for o in ok), default=0),
        "n_ok": len(ok),
        "n_failed": len(observations) - len(ok),
    }


def pool_envelope(run_dirs: list[Path], *, timeout_ceiling_ms: int | None = None) -> dict[str, Any]:
    """F-13 end to end: calibration runs in, the pool's admissible set out.

    Each run directory contributes its **latest** C-3 snapshot. Latest rather than a merge
    of the series, because the snapshots are a time-ordered history of one node under
    drift (F-8) and averaging them would produce an envelope no snapshot ever claimed —
    the point of the series is that the node is not the same node at t=0 and t=300.

    Refuses to intersect across models for the same reason `r_range` refuses to divide
    across them (F-9): the pool holds engine, quantization and model constant, so an
    envelope spanning two models describes no pool that this study can run.
    """
    if not run_dirs:
        raise ValueError(
            "the admissible set is an intersection over the pool; got 0 calibration runs"
        )
    reports = [json.loads((d / "campaign.json").read_text()) for d in run_dirs]
    models = {r.get("model", "unspecified") for r in reports}
    if len(models) > 1:
        raise ValueError(
            f"calibration runs span {len(models)} models ({sorted(models)}); F-9 holds the "
            "model constant across the pool, so intersecting their envelopes would describe "
            "a pool that cannot exist. Determine one admissible set per model"
        )

    envelopes: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    cliffs: list[dict[str, Any]] = []
    for run_dir, report in zip(run_dirs, reports, strict=True):
        series = cost_model.load_series(run_dir / "snapshots")
        if not series:
            raise ValueError(
                f"{run_dir} has no C-3 snapshots, so it states no service times and cannot "
                "contribute an envelope — re-run the campaign for that node class"
            )
        snapshot = series[-1]
        ceiling = int(snapshot["admissibility"]["timeout_ceiling_ms"])
        envelopes.append(node_limit_from_snapshot(snapshot, ceiling))

        observations = load_observations(run_dir)
        node_class = report["node_class"]
        evidence.append({"node_class": node_class, **measured_extent(observations)})
        cliffs.append(cliff_from_observations(observations, node_class=node_class).to_dict())

    envelope = determine(envelopes, timeout_ceiling_ms=timeout_ceiling_ms)
    envelope["model"] = min(models)
    envelope["buckets"] = buckets_within(envelope)
    envelope["evidence"] = evidence
    envelope["cliffs"] = cliffs
    envelope["unmeasured_ceiling"] = [
        {
            "node_class": e["node_class"],
            "claimed_prompt": envelope["max_prompt"],
            "measured_prompt": e["max_prompt_measured"],
        }
        for e in evidence
        if e["max_prompt_measured"] < envelope["max_prompt"]
    ]
    return envelope


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="F-13/F-15 — the pool's admissible set, and the cliff outside it"
    )
    ap.add_argument(
        "root",
        type=Path,
        help="directory of calibration runs; every campaign.json under it is one pool node",
    )
    ap.add_argument(
        "--ceiling-ms",
        type=int,
        help="override the timeout ceiling; default is the tightest one in the pool",
    )
    ap.add_argument("--out", type=Path, help="write the envelope as JSON here as well as printing")
    args = ap.parse_args(argv)

    run_dirs = sorted(p.parent for p in args.root.rglob("campaign.json"))
    if not run_dirs:
        raise SystemExit(
            f"no campaign.json under {args.root} — the admissible set is measured, not "
            "assumed; run the calibration campaign first"
        )

    envelope = pool_envelope(run_dirs, timeout_ceiling_ms=args.ceiling_ms)
    print(f"model: {envelope['model']}")
    print(summary(envelope))
    print(f"trace buckets inside it: {', '.join(envelope['buckets']) or '(none)'}")
    for node in envelope["per_node"]:
        print(
            f"  {node['node_class']:42} prompt<={node['max_prompt']:<5} "
            f"output<={node['max_output']:<5} worst p95 {node['worst_p95_ms']:.0f} ms "
            f"at concurrency {node['limiting_concurrency']}"
        )
    for gap in envelope["unmeasured_ceiling"]:
        print(
            f"  NOTE: {gap['node_class']} admits prompts to {gap['claimed_prompt']} on "
            f"samples that reach only {gap['measured_prompt']} — the bucket ceiling is "
            "claimed, not measured"
        )
    for cliff in envelope["cliffs"]:
        if cliff["n_failed"]:
            print(
                f"  CLIFF: {cliff['node_class']} failed {cliff['n_failed']}/"
                f"{cliff['n_total']} ({cliff['failure_rate']:.1%}) "
                f"{cliff['failures_by_status']}; shortest failing request "
                f"{cliff['first_failing_prompt']}p/{cliff['first_failing_output']}o"
            )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(envelope, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
