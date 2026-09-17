#!/usr/bin/env python3
"""A contrast measured in several run sets, compared across them, with intervals.

`campaign_summary.py` answers questions inside one run set. Three of the frozen decision rules
compare run sets with each other, and each needs a number this computes:

- **6.4, heavy-tailed and bursty.** `wjsq/jsq` on the heavy-tailed trace against the seeded
  anchor at the same pool utilisation, and MMPP against Poisson, with the decision tie rate
  beside each.
- **6.5, shapes at matched load.** `wjsq/jsq` on generation, balanced and summarisation, which
  are three run sets, and whether the ordering holds with the extremes separated.

Each set's cells are built by `campaign_summary.prepare` and `point_setup`, the steady-state
gate is the same `climb` on the same named random stream, and the pairing within a set is
the same `paired`. So a ratio this prints for one set is the one that set's `summary.json`
reports, and a cell a summary marks transient is transient here. The intervals come from a
stream of their own, so their endpoints differ from the summary's by bootstrap noise only.

Sets are different runs, so their draws are independent, and a difference between two sets'
ratios is taken draw by draw. Two workloads inside one campaign are two sets here as well,
named `path#workload`, and they share each repeat's arrival seed. Their draws are still taken
independently, which widens the difference's interval rather than narrowing it, because the
positions resampled in one workload's trace do not correspond to positions in another's. "Separated" means that difference's 95% interval excludes zero.

The decision tie rate is read from the scheduler logs beside the run set: the share of
decisions whose lowest score among admissible nodes was shared by two or more of them. It is
what turns a JSQ-family choice into a random or ordinal draw, which is why 6.4 reports it
beside burstiness.

Usage:
  uv run --project dataplane python tools/compare_sets.py \\
      --set anchor=runs/exp/seeded_anchor_1650ti_3050/runset.parquet \\
      --set poisson=runs/exp/heavytail_1650ti_3050/runset.parquet#poisson \\
      --set mmpp=runs/exp/heavytail_1650ti_3050/runset.parquet#mmpp \\
      --point u30 --out runs/exp/compare_heavytail_u30
  uv run --project dataplane python tools/compare_sets.py \\
      --set generation=runs/exp/matched_1650ti_3050/runset.parquet#generation \\
      --set balanced=runs/exp/matched_1650ti_3050/runset.parquet#balanced \\
      --set summarisation=runs/exp/matched_1650ti_3050/runset.parquet#summarisation \\
      --point slow70 --ordered --out runs/exp/compare_shapes_slow70
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
from campaign_summary import (
    cell_draws,
    climb,
    interval,
    manifests,
    paired,
    point_setup,
    point_values,
    prepare,
    r4,
    stream,
    where,
)
from campaign_summary import invalid_at as cells_invalid_at
from pool_load import snapshot_index

TIE_EPS = 1e-6


def tie_rate(run_dir_root: Path, run_ids: list[str]) -> dict[str, float | int | None]:
    """Share of decisions whose best admissible score was shared, over the named runs."""
    decisions = ties = 0
    for rid in run_ids:
        for log in (run_dir_root / rid).glob("scheduler_*.jsonl"):
            for line in log.read_text(encoding="utf-8").splitlines():
                rec = json.loads(line)
                if rec.get("type") != "decision":
                    continue
                scores = [
                    c["score"]
                    for c in rec.get("candidates", [])
                    if c.get("admissible") and c.get("score") is not None
                ]
                if len(scores) < 2:
                    continue
                decisions += 1
                best = min(scores)
                ties += sum(1 for sc in scores if sc <= best + TIE_EPS) >= 2
    return {
        "decisions": decisions,
        "tie_rate": r4(ties / decisions) if decisions else None,
    }


def measure(
    label: str,
    path: Path,
    workload: str,
    point: str,
    staleness: float,
    ref: str,
    arm: str,
    n_boot: int,
    seed: int,
) -> tuple[dict, np.ndarray | None]:
    """One set's contrast at one point, and the bootstrap draws of its ratio."""
    mans = manifests(path)
    frame, _, invalid_runs, _ = prepare(pd.read_parquet(path), mans, snapshot_index())
    at_point_ids = [
        rid
        for rid, m in mans.items()
        if m["config"].get("operating_point") == point
        and float(m["config"].get("staleness_s", m.get("staleness_s", 0.0))) == staleness
        and (m["config"].get("workload") or "") == workload
    ]
    at_point = frame[frame["run_id"].isin(at_point_ids)]
    out: dict = {
        "set": label,
        "runset": str(path),
        **({"workload": workload} if workload else {}),
        "point": point,
        "staleness_s": staleness,
    }
    if at_point.empty:
        named = f" for workload {workload!r}" if workload else ""
        out["status"] = f"undefined: no run at point {point!r} and staleness {staleness:g}{named}"
        return out, None
    lams = sorted(at_point["lambda"].unique())
    if len(lams) != 1:
        out["status"] = f"undefined: point {point!r} holds several rates {lams}"
        return out, None
    lam = float(lams[0])
    out["lambda_rps"] = round(lam, 3)
    load_targets = {
        json.dumps(mans[r]["config"].get("load_target"), sort_keys=True) for r in at_point_ids
    }
    out["load_target"] = [json.loads(t) for t in sorted(load_targets)]

    window = at_point[~at_point["is_warmup"]]
    cells, n, _, block = point_setup(window)
    invalid = cells_invalid_at(invalid_runs, cells, staleness, lam)
    steady = {
        p: not climb(c, block, n_boot, stream(seed, "climb", p, *where(workload, staleness, lam)))[
            "transient"
        ]
        for p, c in cells.items()
    }
    restricted, head = paired(cells, steady, invalid, (ref, arm))
    out.update(head)
    out["tie"] = {
        p: tie_rate(path.parent, sorted(window[window["policy"] == p]["run_id"].unique()))
        for p in (ref, arm)
        if p in cells
    }
    if restricted is None:
        return out, None

    draws = cell_draws(
        restricted, n_boot, block, stream(seed, "compare", label, workload, ref, arm, lam), n
    )
    vr, va = point_values(restricted[ref]), point_values(restricted[arm])
    ratio_draws = draws[arm]["mean"] / draws[ref]["mean"]
    out["mean_ratio"] = r4(va["mean"] / vr["mean"])
    out["mean_ratio_ci95"] = [r4(x) for x in interval(ratio_draws)]
    out["p95_ratio"] = r4(va["p95"] / vr["p95"])
    out["p95_ratio_ci95"] = [r4(x) for x in interval(draws[arm]["p95"] / draws[ref]["p95"])]
    out["secondary"] = {
        p: {
            "slo_e2e_2x": r4(v["slo_e2e_2x"]),
            "slo_e2e_5x": r4(v["slo_e2e_5x"]),
            "tpot_mean_ms": r4(v["tpot_mean"]),
        }
        for p, v in ((ref, vr), (arm, va))
    }
    return out, ratio_draws


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--set",
        action="append",
        required=True,
        help="label=path/to/runset.parquet, or label=path#workload for a campaign that "
        "ran several workloads into one run set",
    )
    ap.add_argument("--point", required=True, help="operating point name, e.g. u30 or slow70")
    ap.add_argument("--staleness", type=float, default=0.0)
    ap.add_argument(
        "--contrast", default="jsq,wjsq", help="reference,arm; ratio is arm over reference"
    )
    ap.add_argument(
        "--ordered",
        action="store_true",
        help="the sets are given in the order the hypothesis predicts; test that order",
    )
    ap.add_argument("--draws", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260915)
    ap.add_argument("--out", type=Path, help="writes <out>.json and <out>.md")
    args = ap.parse_args(argv)

    ref, arm = args.contrast.split(",")
    measured: list[tuple[dict, np.ndarray | None]] = []
    for spec in args.set:
        label, _, rest = spec.partition("=")
        path, _, workload = rest.partition("#")
        measured.append(
            measure(
                label,
                Path(path),
                workload,
                args.point,
                args.staleness,
                ref,
                arm,
                args.draws,
                args.seed,
            )
        )

    pairs = []
    for (a, da), (b, db) in itertools.combinations(measured, 2):
        entry = {"from": a["set"], "to": b["set"]}
        if da is None or db is None:
            entry["status"] = "undefined: a set has no defined ratio at this point"
        else:
            lo, hi = interval(db - da)
            entry.update(
                {
                    "status": "defined",
                    "difference": r4(b["mean_ratio"] - a["mean_ratio"]),
                    "difference_ci95": [r4(lo), r4(hi)],
                    "separated": bool(lo > 0 or hi < 0),
                }
            )
        pairs.append(entry)

    ordering = None
    if args.ordered and len(measured) >= 2:
        values = [m["mean_ratio"] if d is not None else None for m, d in measured]
        defined = all(v is not None for v in values)
        extremes = next(
            (
                p
                for p in pairs
                if p["from"] == measured[0][0]["set"] and p["to"] == measured[-1][0]["set"]
            ),
            None,
        )
        ordering = {
            "order": [m["set"] for m, _ in measured],
            "values": values,
            "monotone": defined
            and (
                all(x >= y for x, y in itertools.pairwise(values))
                or all(x <= y for x, y in itertools.pairwise(values))
            ),
            "extremes_separated": bool(extremes and extremes.get("separated")),
        }

    report = {
        "contrast": f"{arm} over {ref}, mean end-to-end latency",
        "point": args.point,
        "staleness_s": args.staleness,
        "bootstrap": {"draws": args.draws, "seed": args.seed},
        "sets": [m for m, _ in measured],
        "pairs": pairs,
        "ordering": ordering,
    }

    lines = [
        f"{arm}/{ref} on mean latency at point `{args.point}`, staleness {args.staleness:g} s.",
        "",
        "| Set | Rate | Status | Repeats | Ratio | p95 ratio | Tie rate, ref | Tie rate, arm |",
        "|---|---:|---|---|---|---|---:|---:|",
    ]
    for m, _ in measured:
        ties = m.get("tie", {})
        tr = [ties.get(p, {}).get("tie_rate") for p in (ref, arm)]
        ratio = (
            f"{m['mean_ratio']:.3f} [{m['mean_ratio_ci95'][0]:.3f}, {m['mean_ratio_ci95'][1]:.3f}]"
            if "mean_ratio" in m
            else ""
        )
        p95 = (
            f"{m['p95_ratio']:.3f} [{m['p95_ratio_ci95'][0]:.3f}, {m['p95_ratio_ci95'][1]:.3f}]"
            if "p95_ratio" in m
            else ""
        )
        lines.append(
            f"| {m['set']} | {m.get('lambda_rps', '')} | {m['status']} | {m.get('repeats_used', '')} | "
            f"{ratio} | {p95} | {'' if tr[0] is None else format(tr[0], '.1%')} | "
            f"{'' if tr[1] is None else format(tr[1], '.1%')} |"
        )
    lines += ["", "| From | To | Difference in ratio | Separated |", "|---|---|---|---|"]
    for p in pairs:
        if p["status"] != "defined":
            lines.append(f"| {p['from']} | {p['to']} | {p['status']} | |")
        else:
            lines.append(
                f"| {p['from']} | {p['to']} | {p['difference']:+.3f} "
                f"[{p['difference_ci95'][0]:+.3f}, {p['difference_ci95'][1]:+.3f}] | "
                f"{'yes' if p['separated'] else 'no'} |"
            )
    if ordering:
        lines += [
            "",
            (
                f"Ordering {' -> '.join(ordering['order'])}: monotone "
                f"{'yes' if ordering['monotone'] else 'no'}, extremes separated "
                f"{'yes' if ordering['extremes_separated'] else 'no'}."
            ),
        ]
    text = "\n".join(lines)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        args.out.with_suffix(".md").write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
