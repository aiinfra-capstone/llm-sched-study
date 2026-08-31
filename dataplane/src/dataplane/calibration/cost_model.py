"""F-6 / F-7 / C-3 — fitting the cost model snapshot.

C-3 is the most load-bearing artifact in the repository: I produce these, and both
Aditya's live scheduler and his DES consume them. It is the one place where a mistake in
my half becomes a wrong number in his.

**The `form` decision, due end of Week 2: `lookup_table`.** F-7 permits either a
calibrated lookup table over prompt-length buckets or a regression of at most six
parameters, and the checklist says pick one, because supporting both doubles Aditya's
interpolation logic for no research gain. Three reasons it is the table:

1. *Concurrency is the axis that matters and it is the one a small regression cannot
   fit.* Service time against concurrency is flat while slots are free, then bends
   sharply once `--parallel` saturates and requests start batching against each other.
   That knee is the shape F-4 is about. A <=6-parameter form either smooths it away or
   spends most of its parameters describing it.
2. *The table is the measurement.* Every cell is a mean over samples actually collected
   at that operating point. A regression interposes a functional form between what the
   hardware did and what the scheduler believes, and when the simulator later disagrees
   with hardware (F-23) I would not be able to tell a policy effect from a fit artifact.
3. *F-7 also requires interpretable and inspectable.* A reviewer can read a cell off the
   JSON and check it against a plot. That is a lower bar for a regression to clear than
   it looks, once the regressors are interactions.

The cost is real and worth stating: the table only knows the grid. Off-grid queries need
interpolation, which is Aditya's side of the seam, and `predict_service_ms` here is the
reference implementation both halves must agree with **at grid points** — it is what I
compute residuals against, so if his interpolation disagrees on a cell that is a seam bug
and not a matter of taste.

**Snapshots are a time-ordered series, not one final fitted model.** This is the
requirement in C-3's own description that is easiest to miss and most expensive to get
wrong. H3 asks what happens when the scheduler is served an estimate from `s` seconds
ago. If I emit a single snapshot, Aditya has to synthesize age by perturbing parameters,
and H3 stops being an empirical result about real drift and becomes a study of his
perturbation model. A real snapshot history makes staleness injection (F-8) a replay of
something that actually happened.

**`tokens_per_s` means decode tok/s**, the same definition the adapter uses. Not
`output_tokens / service_ms`: that falls when prompts get longer, so a node would appear
to slow down when the workload changed, and R — a ratio of these numbers — would move
with the trace instead of with the hardware.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "FORM",
    "SCHEMA_VERSION",
    "Observation",
    "assign_bucket",
    "autocorr_time_s",
    "buckets_from_edges",
    "build_snapshot",
    "example_campaign_dir",
    "example_inputs",
    "example_pool_dir",
    "example_throughput_series",
    "load",
    "load_series",
    "predict_service_ms",
    "residuals",
    "snapshot_id",
]

SCHEMA_VERSION = 1

# The Week-2 decision, committed here rather than left as a runtime option. Making it a
# constant instead of a parameter is the point: an option is a thing that gets set
# differently in two places.
FORM = "lookup_table"


@dataclass(frozen=True)
class Observation:
    """One calibration sample: what was asked for, and what the engine did.

    `t_end_ns` is this host's monotonic completion stamp. It is what orders the snapshot
    series and what the stationarity segment is binned on, and it is never subtracted
    from a stamp taken anywhere else.
    """

    prompt_len: int
    output_len: int
    concurrency: int
    service_ns: int
    output_tokens: int
    t_end_ns: int
    status: str = "ok"
    prefill_ns: int | None = None
    decode_ns: int | None = None
    error: str = ""

    @property
    def service_ms(self) -> float:
        return self.service_ns / 1e6

    @property
    def decode_tokens_per_s(self) -> float | None:
        if self.decode_ns is None or self.decode_ns <= 0 or self.output_tokens <= 0:
            return None
        return self.output_tokens / (self.decode_ns / 1e9)


def buckets_from_edges(edges: list[int]) -> list[tuple[int, int]]:
    """`[1, 128, 512, 2048]` -> `[(1, 128), (129, 512), (513, 2048)]`.

    Half-open in intent, inclusive in the JSON, because C-3 writes buckets as a `[lo, hi]`
    pair of integers and an inclusive pair is unambiguous about which side owns the edge.
    A token length is an integer, so `129` is genuinely the next value after `128` and
    nothing falls between two buckets.
    """
    if len(edges) < 2:
        raise ValueError(f"need at least 2 edges to form a bucket, got {edges}")
    if any(b <= a for a, b in pairwise(edges)):
        raise ValueError(f"bucket edges must be strictly increasing, got {edges}")
    return [(lo if i == 0 else lo + 1, hi) for i, (lo, hi) in enumerate(pairwise(edges))]


def assign_bucket(value: int, buckets: list[tuple[int, int]]) -> tuple[int, int]:
    """Which bucket a length falls in. Refuses to guess for a length outside the grid.

    Clamping would be the convenient choice and the wrong one: a request longer than the
    calibrated range is exactly the F-13 admissibility question, and silently pricing it
    at the top bucket's rate is how a cliff gets averaged into a latency tail.
    """
    for lo, hi in buckets:
        if lo <= value <= hi:
            return (lo, hi)
    raise ValueError(
        f"{value} falls outside the calibrated buckets {buckets} — this is an "
        "admissibility question (F-13), not a value to clamp"
    )


def snapshot_id(node_class: str, measured_at_unix: int, seq: int | None = None) -> str:
    """`cm_<node_class>_<UTC>`, optionally `_<seq>`. Deterministic in its inputs.

    `seq` exists because the id is second-granular and the run manifest references
    snapshots *by id* (C-6 `cost_model_snapshots`). Two snapshots taken inside the same
    second would otherwise share an id, and a scheduler served "the snapshot from 30s ago"
    would be pointed at an ambiguous one — which is precisely the kind of thing that makes
    an H3 staleness result quietly wrong rather than visibly broken. The series passes its
    index; a standalone fit does not need one.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(measured_at_unix))
    tail = "" if seq is None else f"_{seq:03d}"
    return f"cm_{node_class}_{stamp}{tail}"


def _cell_entry(
    obs: list[Observation], prompt_bucket: tuple[int, int], output_bucket: tuple[int, int]
) -> dict[str, Any]:
    """One C-3 `entries[]` row from the samples that landed in one grid cell."""
    service_ms = np.array([o.service_ms for o in obs], dtype=np.float64)
    tok_s = [o.decode_tokens_per_s for o in obs]
    measured = [t for t in tok_s if t is not None]
    return {
        "prompt_bucket": [prompt_bucket[0], prompt_bucket[1]],
        "output_bucket": [output_bucket[0], output_bucket[1]],
        "concurrency": obs[0].concurrency,
        "service_ms_mean": round(float(service_ms.mean()), 3),
        "service_ms_p50": round(float(np.percentile(service_ms, 50)), 3),
        "service_ms_p95": round(float(np.percentile(service_ms, 95)), 3),
        # Median, not mean: one thermal excursion inside a cell should move the summary by
        # one sample's worth, not drag the node's advertised speed down with it.
        "tokens_per_s": round(float(np.median(measured)), 4) if measured else 0.0,
        "n_samples": len(obs),
    }


def build_snapshot(
    observations: list[Observation],
    *,
    node_class: str,
    prompt_edges: list[int],
    output_edges: list[int],
    provenance: dict[str, Any],
    admissibility: dict[str, Any],
    calibration_run_ids: list[str],
    stochastic: dict[str, Any],
    measured_at_unix: int,
) -> dict[str, Any]:
    """Build one schema-valid C-3 snapshot from a set of calibration samples.

    Only `status == "ok"` samples are fitted. A timeout is not a slow service time — it is
    a censored observation, and averaging a 60s ceiling into a cell would report the
    timeout setting as if it were the hardware's speed. Failures are the admissible-set
    signal (F-13/F-15) and are counted by the campaign, not folded in here.
    """
    ok = [o for o in observations if o.status == "ok"]
    if not ok:
        raise ValueError(
            f"no successful samples for {node_class}: a cost model fitted from failures "
            "would report the timeout ceiling as the node's speed"
        )
    p_buckets = buckets_from_edges(prompt_edges)
    o_buckets = buckets_from_edges(output_edges)

    cells: dict[tuple[tuple[int, int], tuple[int, int], int], list[Observation]] = {}
    for o in ok:
        key = (
            assign_bucket(o.prompt_len, p_buckets),
            assign_bucket(o.output_len, o_buckets),
            o.concurrency,
        )
        cells.setdefault(key, []).append(o)

    entries = [
        _cell_entry(obs, p_bucket, o_bucket)
        for (p_bucket, o_bucket, _), obs in sorted(cells.items())
    ]
    return {
        "cost_model_schema": SCHEMA_VERSION,
        "snapshot_id": snapshot_id(node_class, measured_at_unix),
        "node_class": node_class,
        "measured_at_unix": measured_at_unix,
        "calibration_run_ids": list(calibration_run_ids),
        "form": FORM,
        "entries": entries,
        "stochastic": stochastic,
        "admissibility": admissibility,
        "provenance": provenance,
    }


def predict_service_ms(
    snapshot: dict[str, Any], prompt_len: int, output_len: int, concurrency: int
) -> float:
    """The reference lookup (F-6). Exact cell, else nearest measured concurrency in it.

    Nearest-concurrency is the only interpolation done here, and it is done because the
    concurrency axis is sparse by construction — the grid measures 1, 2, 4, 8 slots, and a
    live pool will present every value in between. Off-grid *lengths* are not interpolated:
    the bucket lookup already covers the calibrated range and anything outside it raises,
    for the reason in `assign_bucket`.

    Aditya's scheduler may interpolate more cleverly. What it may not do is disagree with
    this function **on a grid point** — that is the silent-drift failure mode the seam
    document lists, and the cross-environment determinism test is where it would surface.
    """
    p_bucket = assign_bucket(prompt_len, [tuple(e["prompt_bucket"]) for e in snapshot["entries"]])
    o_bucket = assign_bucket(output_len, [tuple(e["output_bucket"]) for e in snapshot["entries"]])
    candidates = [
        e
        for e in snapshot["entries"]
        if tuple(e["prompt_bucket"]) == p_bucket and tuple(e["output_bucket"]) == o_bucket
    ]
    if not candidates:
        raise ValueError(
            f"no calibrated cell for prompt {prompt_len}, output {output_len} — the grid "
            "has that bucket pair unmeasured"
        )
    nearest = min(candidates, key=lambda e: abs(e["concurrency"] - concurrency))
    return float(nearest["service_ms_mean"])


def residuals(
    observations: list[Observation], snapshot: dict[str, Any]
) -> tuple[list[float], list[float]]:
    """`(observed_ms, predicted_ms)` for every fittable sample, for `lognormal_sigma`.

    This is what makes C-3's `stochastic.sigma` a residual of *this* cost model rather
    than a generic spread: F-22 asks the simulator's noise term to reproduce the variance
    the hardware showed **around the prediction the scheduler would have made**, which is
    a different and smaller number than the raw spread of service times across the grid.
    """
    obs_ms: list[float] = []
    pred_ms: list[float] = []
    for o in observations:
        if o.status != "ok":
            continue
        obs_ms.append(o.service_ms)
        pred_ms.append(predict_service_ms(snapshot, o.prompt_len, o.output_len, o.concurrency))
    return obs_ms, pred_ms


def load(path: str | Path) -> dict[str, Any]:
    """Read one C-3 snapshot, refusing a version this code does not understand.

    §12.2's mitigation, and it points at the consumer rather than the producer: a loader
    that silently defaults on an unknown `cost_model_schema` will read a v2 snapshot as
    though it were v1 and hand the scheduler numbers that mean something else. Failing
    here is the whole value of versioning the artifact.
    """
    snapshot = json.loads(Path(path).read_text())
    version = snapshot.get("cost_model_schema")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: cost_model_schema is {version!r}, this code understands "
            f"{SCHEMA_VERSION} — refusing to guess what the fields mean"
        )
    return snapshot


def load_series(directory: str | Path) -> list[dict[str, Any]]:
    """Every snapshot under `directory`, in the order they were measured.

    Ordered by `measured_at_unix` and then by `snapshot_id`, so a cadence fast enough to
    put two snapshots in the same second still has a total order. This is the artifact
    F-8 ages: "serve the scheduler the snapshot from s seconds ago" is a lookup into this
    list, and it only means anything if the list is genuinely a history.
    """
    snapshots = [load(p) for p in sorted(Path(directory).rglob("*.json"))]
    return sorted(snapshots, key=lambda s: (s["measured_at_unix"], s["snapshot_id"]))


def autocorr_time_s(source: dict[str, Any] | Sequence[float], *, dt_s: float = 1.0) -> float:
    """tau — MPR-1's headline number, and F-22's input to the DES.

    Takes either a C-3 snapshot (read the number that was fitted) or a raw throughput
    series (fit it now). One name for the quantity in both directions, because the failure
    this prevents is the two halves of the project disagreeing about what "tau" means: a
    snapshot's `stochastic.autocorr_time_s` and a freshly fitted series must be the same
    estimator, or H3's x-axis is measured one way and modelled another.

    A named accessor rather than a dict path, for the same reason — reaching into a nested
    key by hand is how a rename becomes a silent zero.
    """
    if isinstance(source, dict):
        return float(source["stochastic"]["autocorr_time_s"])
    from dataplane.calibration.stationarity import acf, fit_autocorr_time

    return fit_autocorr_time(acf(list(source)), dt_s).tau_s


def example_throughput_series(
    *, tau_s: float = 42.0, dt_s: float = 1.0, n: int = 600, seed: int = 11
) -> list[float]:
    """A synthetic decode-tok/s series with a known autocorrelation time.

    AR(1), because that is the process whose ACF is exactly the exponential
    `fit_autocorr_time` fits — so a test using this is checking the estimator against an
    answer that is known rather than against another estimator.
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    phi = float(np.exp(-dt_s / tau_s))
    x, out = 0.0, []
    for _ in range(n):
        x = phi * x + float(np.sqrt(1 - phi**2)) * float(rng.normal())
        out.append(100.0 * (1 + 0.12 * x))
    return out


def example_campaign_dir(root: str | Path | None = None) -> Path:
    """Write a small, ordered snapshot series to disk and return the directory.

    A *series*, not one file, because that is the shape C-3 actually requires and the
    shape `load_series` has to be able to read back. If this wrote a single snapshot it
    would quietly bless the mistake the schema's own description warns about.
    """
    directory = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="cal_series_"))
    directory.mkdir(parents=True, exist_ok=True)
    inputs = example_inputs()
    base_unix = inputs["measured_at_unix"]
    for i in range(3):
        measured_at = base_unix + i * 30
        snapshot = build_snapshot(**{**inputs, "measured_at_unix": measured_at})
        snapshot["snapshot_id"] = snapshot_id(inputs["node_class"], measured_at, seq=i + 1)
        (directory / f"{i:03d}_{snapshot['snapshot_id']}.json").write_text(
            json.dumps(snapshot, indent=2) + "\n"
        )
    return directory


def example_pool_dir(root: str | Path | None = None) -> Path:
    """A snapshot series for a **pool** — two node classes — rather than for one node.

    Separate from `example_campaign_dir` on purpose. That one is a *series*: one node class
    measured repeatedly, which is the shape F-8's staleness lookup ages through, and things
    downstream depend on it being exactly that. This one is the other shape the same
    artifact has to support, and R needs it: a ratio between node classes cannot be taken
    from a fixture that only contains one.

    The two classes are the same card running CUDA and running Vulkan — the same engine
    commit, the same model, the same quantization, and not the same throughput. That is the
    honest minimum for a pool under F-9, and it means the ratio between them is a backend
    effect with nothing else folded in. Both are measured at **identical cells**, so a
    ratio taken at any shared cell is comparing like with like; measuring them at different
    prompt lengths is exactly the mistake that once made R read 20% low.
    """
    directory = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="cal_pool_"))
    directory.mkdir(parents=True, exist_ok=True)
    base = example_inputs()
    base_unix = base["measured_at_unix"]
    slower = {
        **base,
        "node_class": "gtx1650ti_vulkan_ngl20_p4_q4km_llama3_8b",
        "provenance": {**base["provenance"], "engine_version": "b10569+vulkan"},
        "observations": [
            replace(
                o,
                service_ns=int(o.service_ns * 1.6),
                prefill_ns=int(o.prefill_ns * 1.6) if o.prefill_ns else o.prefill_ns,
                decode_ns=int(o.decode_ns * 1.6) if o.decode_ns else o.decode_ns,
            )
            for o in base["observations"]
        ],
    }

    written = 0
    for inputs in (base, slower):
        for i in range(2):
            measured_at = base_unix + i * 30
            snapshot = build_snapshot(**{**inputs, "measured_at_unix": measured_at})
            snapshot["snapshot_id"] = snapshot_id(inputs["node_class"], measured_at, seq=i + 1)
            (directory / f"{written:03d}_{snapshot['snapshot_id']}.json").write_text(
                json.dumps(snapshot, indent=2) + "\n"
            )
            written += 1
    return directory


def example_inputs(*, engine: str = "llamacpp") -> dict[str, Any]:
    """A complete, schema-valid argument set for `build_snapshot`.

    Kept next to the fitter rather than in a test, because it is also what a reader wants
    when they ask "what does a calibration sample actually look like" — and because a
    fixture that lives beside the code it exercises cannot drift from it.

    `engine` exists for one reason: the F-9b engine-gap probe emits a snapshot too, and it
    is the only legal way `provenance.engine` is anything but `llamacpp`. Everything else
    about the probe — that it joins no pool and appears in no policy comparison — is
    enforced by `manifest.nodes[].role`, not here.
    """
    observations = [
        Observation(
            prompt_len=prompt_len,
            output_len=48,
            concurrency=concurrency,
            service_ns=int(service_ms * 1e6),
            output_tokens=48,
            t_end_ns=i * 1_000_000_000,
            status="ok",
            prefill_ns=int(0.2 * service_ms * 1e6),
            decode_ns=int(0.8 * service_ms * 1e6),
        )
        for i, (prompt_len, concurrency, service_ms) in enumerate(
            [
                (64, 1, 900.0),
                (64, 1, 950.0),
                (64, 1, 1000.0),
                (64, 4, 2600.0),
                (64, 4, 2800.0),
                (64, 4, 3000.0),
                (256, 1, 1300.0),
                (256, 1, 1400.0),
                (256, 1, 1500.0),
                (256, 4, 3600.0),
                (256, 4, 3900.0),
                (256, 4, 4200.0),
            ]
        )
    ]
    return {
        "observations": observations,
        "node_class": (
            "gtx1650ti_ngl20_p4_q4km_llama3_8b"
            if engine == "llamacpp"
            else "gtx1650ti_vllm_awq_llama3_8b_probe"
        ),
        "prompt_edges": [1, 128, 512, 2048],
        "output_edges": [1, 64, 128, 256],
        "provenance": {
            "engine": engine,
            "engine_version": "b10569+cuda13.2" if engine == "llamacpp" else "vllm-0.6.x-awq",
            "quant": "Q4_K_M",
            "gpu": "NVIDIA GeForce GTX 1650 Ti",
            "driver": "580.173.02",
            "prefix_caching": False,
            "engine_config": {"ngl": 20, "threads": 6, "parallel": 4},
        },
        "admissibility": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
        "calibration_run_ids": ["cal_0001"],
        "stochastic": {
            "model": "lognormal_multiplier",
            "sigma": 0.113,
            "autocorr_time_s": 42.0,
            "fit_r2": 0.87,
        },
        "measured_at_unix": 1788077068,
    }
