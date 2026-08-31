"""Assemble many joined runs into the one frame a figure actually reads — F-19.

`join.py` turns one run directory into one C-5 record set. Nothing this study asks is
answered by a single run: H1 compares four policies, H2 sweeps *R*, H3 sweeps staleness,
and the load band is read across operating points. So what a figure script opens is a
concatenation of joined runs, and assembling that concatenation is where three problems
live that a single-run join is structurally unable to see.

**R was typed in by hand.** `pipeline --r 2.0` is defensible for one run and is a loaded
gun across a twenty-run sweep: *R* is the independent variable of H2, and a mistyped value
does not fail loudly, it silently relabels a point on the x-axis. Here *R* is **derived**
from the run's own manifest. `cost_model_snapshots` already names exactly which C-3
snapshot each node was serving under, so the ratio is recomputed from those snapshots
rather than asserted by whoever typed the command. A pool whose nodes are all one node
class reads 1.0 by construction — which is the honest number for a single-host pool, and
is why this repository reports a deployable *R* of 1.00x while the synthesizable range
tops out at 2.00x.

**`vehicle` never reached the frame.** F-24 requires every simulated figure to be labelled
as simulated, and the plan was always that the figure script reads `manifest.vehicle` and
stamps the plot automatically, because labelling by hand fails exactly once — in the final
report. A figure that reads a *set* has no single manifest left to read, so the label has
to ride in the rows. It is attached here rather than in `join.py` because C-5's column
list is frozen and Aditya's simulator emits against it; `vehicle` is a property of how a
set was assembled, not a field of a joined record.

**One bad run must not cost the other nineteen.** `join.py` refuses an invalid run loudly,
which is right when a person asked for that one run. A set is assembled from a directory,
where the same strictness would mean a single drifted run stops the whole analysis. So a
refusal becomes an *exclusion with its reason attached*, reported before anything is
plotted. Excluding it silently would be the actual sin, and `RunSet.excluded` exists so
that cannot happen.

Still a pure function of files: no network, no engine, no transport imports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from dataplane.calibration import r_range
from dataplane.pipeline import join as join_mod

__all__ = [
    "RunSet",
    "aggregate",
    "deployed_r",
    "discover",
    "load_run",
    "snapshot_index",
]

REPO_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"

# What the set carries beyond C-5. Both are properties of the assembly rather than of a
# joined record, which is exactly why they are added here and the frozen column list is
# left alone.
SET_COLUMNS = ["vehicle", "trace_sha256"]


@dataclass(frozen=True)
class RunSet:
    """A set of runs, plus an account of what was left out of it and why.

    The excluded list is not a diagnostic. It is half the artifact: an analysis assembled
    from eighteen of twenty runs is a different claim from one assembled from twenty, and
    the difference has to be visible at the point of assembly rather than reconstructed
    later from shell history.
    """

    frame: pd.DataFrame
    included: list[str]
    excluded: list[tuple[str, str]]

    def summary(self) -> list[str]:
        """What has to be said out loud about a set before anything is plotted from it."""
        out = [f"{len(self.included)} run(s), {len(self.frame)} rows"]

        vehicles = sorted(set(self.frame["vehicle"]))
        out.append(f"vehicle(s): {', '.join(vehicles)}")
        if len(vehicles) > 1:
            out.append(
                "the set mixes hardware and simulator rows — every figure drawn from it "
                "must be labelled as simulated (F-24) or split by vehicle first"
            )

        traces = sorted(set(self.frame["trace_sha256"]))
        if len(traces) > 1:
            out.append(
                f"{len(traces)} distinct traces in one set — the workload is not held "
                "constant, so a difference between these runs is not attributable to the "
                "scheduler alone"
            )

        r_values = sorted({float(v) for v in self.frame["R"]})
        out.append("R: " + ", ".join(f"{v:.2f}x" for v in r_values))
        if r_values == [1.0]:
            out.append(
                "every run is R = 1.00x — a homogeneous pool, so this set cannot speak to "
                "H2 and cannot separate hardware-aware from hardware-blind policies"
            )

        policies = sorted(set(self.frame["policy"]))
        out.append(f"policies: {', '.join(policies)}")

        for run_id, why in self.excluded:
            out.append(f"EXCLUDED {run_id}: {why}")
        return out


def snapshot_index(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Every committed C-3 snapshot, indexed by its own `snapshot_id`.

    Indexed by the field rather than parsed out of the filename. The two agree today, and
    the filename is a convenience for humans reading a directory listing; the id inside
    the document is what a manifest actually references.
    """
    root = SNAPSHOT_ROOT if root is None else Path(root)
    index: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("*/*.json")):
        snap = json.loads(path.read_text(encoding="utf-8"))
        index[snap["snapshot_id"]] = snap
    return index


def deployed_r(manifest: dict[str, Any], index: dict[str, dict[str, Any]] | None = None) -> float:
    """The *R* this run actually ran at, read off the snapshots its nodes were under.

    A different question from `r_range.synthesizable`, and the distinction is worth being
    pedantic about because the two numbers are both called R. `synthesizable` asks what
    ratio a pool *could be configured to span* and answers with a range over node classes
    that have been calibrated anywhere. This asks what ratio was in the room during one
    run, and answers 1.0 for a homogeneous pool — not because heterogeneity is unmeasured
    but because there was none.

    The ratio itself is delegated to `r_range.synthesizable` rather than recomputed here.
    That function refuses to divide throughputs measured at different (prompt, output,
    concurrency) cells, which is the mistake that cost this study 20% of its ratio once
    already; a second implementation of the same division is a second chance to make it.
    """
    index = snapshot_index() if index is None else index
    pool = [n for n in manifest["nodes"] if n.get("role", "pool") == "pool"]
    named = manifest.get("cost_model_snapshots", {})

    snapshots = []
    for node in pool:
        node_id = node["node_id"]
        snapshot_id = named.get(node_id)
        if snapshot_id is None:
            raise ValueError(
                f"node {node_id!r} is a pool member but the manifest names no cost model "
                "snapshot for it, so the R this run ran at cannot be derived — and typing "
                "one in is the thing this function exists to stop"
            )
        if snapshot_id not in index:
            raise ValueError(
                f"node {node_id!r} names snapshot {snapshot_id!r}, which is not in "
                "contracts/cost_models/ — commit the snapshot the run was served under, "
                "or the run is not reproducible from the repository"
            )
        snapshots.append(index[snapshot_id])

    if len({s["node_class"] for s in snapshots}) < 2:
        return 1.0
    return r_range.synthesizable(snapshots)[1]


def discover(root: str | Path) -> list[Path]:
    """Every run directory at or under `root`, in a stable order.

    A run directory is one holding a `manifest.json`. `root` itself counts, so a single
    run and a directory of runs are the same call and a figure script never has to know
    which shape it was handed.
    """
    root = Path(root)
    found = [root] if (root / "manifest.json").is_file() else []
    found.extend(sorted(p.parent for p in root.glob("*/manifest.json")))
    return found


def _logs(run_dir: Path, pattern: str) -> list[dict[str, Any]]:
    """Concatenate every log file matching a glob; a missing file is an empty list."""
    out: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob(pattern)):
        out.extend(join_mod.load_jsonl(path))
    return out


def load_run(
    run_dir: str | Path,
    *,
    index: dict[str, dict[str, Any]] | None = None,
    allow_invalid: bool = False,
) -> pd.DataFrame:
    """Join one run into a frame carrying the two set-level columns.

    The trace is resolved from the manifest and its hash checked, rather than passed in.
    `join.py` takes `--trace` from the command line because a person joining one run has
    it to hand; across a set, asking the caller to keep twenty run directories and twenty
    trace paths in the right correspondence is asking for exactly the mismatch that
    `trace_sha256` exists to catch. The manifest already names both, so the correspondence
    is read rather than supplied, and the hash check comes along for free.

    A run whose trace is missing is refused rather than joined without one. `join` accepts
    `trace=None` and zeroes the length columns, which is right for a smoke run and wrong
    here: zero-length rows in an analysis set are silently wrong per-bucket results.
    """
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    trace_path = Path(manifest["trace_path"])
    if not trace_path.is_absolute():
        trace_path = REPO_ROOT / trace_path
    if not trace_path.is_file():
        raise FileNotFoundError(
            f"run {manifest['run_id']!r} names trace {manifest['trace_path']!r}, which is "
            "not there — the per-request lengths cannot be recovered and joining without "
            "them would put zero-length rows into the analysis"
        )

    blob = trace_path.read_bytes()
    trace_sha256 = hashlib.sha256(blob).hexdigest()
    trace = [json.loads(line) for line in blob.decode().splitlines() if line.strip()]

    rows = join_mod.join(
        manifest=manifest,
        trace=trace,
        trace_sha256=trace_sha256,
        client=_logs(run_dir, "client_*.jsonl"),
        scheduler=_logs(run_dir, "scheduler_*.jsonl"),
        worker=_logs(run_dir, "worker_*.jsonl"),
        r_value=deployed_r(manifest, index),
        allow_invalid=allow_invalid,
    )

    frame = join_mod.to_frame(rows)
    frame["vehicle"] = manifest["vehicle"]
    frame["trace_sha256"] = trace_sha256
    return frame


def aggregate(
    root: str | Path,
    *,
    index: dict[str, dict[str, Any]] | None = None,
    allow_invalid: bool = False,
) -> RunSet:
    """Every run under `root`, joined and concatenated, with the refusals recorded.

    Only the deliberate refusals become exclusions — an invalid manifest, a missing or
    mismatched trace, an uncommitted cost model. Anything else propagates, because a set
    that quietly drops a run on a `KeyError` is a set that quietly drops a run on my bug.
    """
    index = snapshot_index() if index is None else index

    frames: list[pd.DataFrame] = []
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    for run_dir in discover(root):
        try:
            frames.append(load_run(run_dir, index=index, allow_invalid=allow_invalid))
        except (ValueError, FileNotFoundError) as exc:
            excluded.append((run_dir.name, str(exc)))
            continue
        included.append(run_dir.name)

    if not frames:
        raise ValueError(
            f"no run under {root} could be joined into a set "
            f"({len(excluded)} excluded, {len(discover(root))} directories seen)"
        )
    return RunSet(
        frame=pd.concat(frames, ignore_index=True),
        included=included,
        excluded=excluded,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="F-19 — join a directory of runs into the one frame the figures read"
    )
    ap.add_argument("root", type=Path, help="a run directory, or a directory of them")
    ap.add_argument("--out", type=Path, help="output Parquet (default: <root>/runset.parquet)")
    ap.add_argument(
        "--force",
        action="store_true",
        help="include runs their manifests mark invalid; the output must not be analysed",
    )
    args = ap.parse_args(argv)

    runs = aggregate(args.root, allow_invalid=args.force)
    out = join_mod.write_parquet(runs.frame, args.out or args.root / "runset.parquet")
    print(out)
    for line in runs.summary():
        print(f"  {line}")
    return 1 if runs.excluded else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
