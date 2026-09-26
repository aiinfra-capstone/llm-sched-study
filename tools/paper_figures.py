#!/usr/bin/env python3
"""Draw the paper's figures from campaign summaries, with their intervals.

`figures` renders what one run set can support and pools policies on its load plots. The
paper needs the comparisons across run sets: every policy as its own line with its interval,
the H1 interaction at each load point, what calibration buys on each workload shape, and the
phase-dependent R that motivates the shapes. This reads the JSON `campaign_summary.py`
writes and the report `phase_ratio.py --out` writes, so it never touches a log.

Every figure carries a footer naming the vehicle and the run sets it came from, the same
discipline F-24 asks of `figures`.

Usage:
  uv run --project dataplane python tools/paper_figures.py \\
      --campaign anchor=runs/exp/mpr2_1650ti_3050/summary.json \\
      --campaign generation=runs/exp/phase_generation_1650ti_3050/summary.json \\
      --phase-ratio runs/exp/phase_ratio_3050_over_1650ti.json --out figures/paper
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

POLICY_ORDER = ["round_robin", "static_weighted", "jsq", "wjsq", "threshold"]
POLICY_STYLE = {
    "round_robin": ("#7f7f7f", "o", "--"),
    "static_weighted": ("#1f77b4", "s", "--"),
    "jsq": ("#ff7f0e", "o", "-"),
    "wjsq": ("#2ca02c", "s", "-"),
    "threshold": ("#9467bd", "^", ":"),
}


def shape_name(profile: str) -> str:
    """`trace_summarisation_1b` -> `summarisation`, the label a campaign is given."""
    return profile.replace("trace_", "").replace("_1b", "")


def shape_rho(report: dict) -> dict[str, float]:
    """Mean prompt-to-output ratio per workload shape, as phase_ratio.py measured it."""
    return {shape_name(p["profile"]): float(p["mean_rho"]) for p in report["profiles"]}


def legend(ax, **kwargs) -> None:
    """A legend when something was drawn. Over no artists it is a warning and an empty box."""
    if ax.get_legend_handles_labels()[0]:
        ax.legend(**kwargs)


def footer(fig, labels: list[str], vehicles: set[str]) -> None:
    fig.text(
        0.01,
        0.005,
        f"vehicle: {', '.join(sorted(vehicles))} | run sets: {', '.join(labels)}",
        fontsize=7,
        color="#555",
    )


def yerr(entry: dict) -> list[float]:
    lo, hi = entry["ci95"]
    return [entry["value"] - lo, hi - entry["value"]]


def latency_by_policy(label: str, summary: dict, out: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(12, 3.8), sharex=True)
    for ax, stat in zip(axes, ["p50", "p95", "p99"], strict=True):
        for policy in POLICY_ORDER:
            pts = [
                (pt["lambda_rps"], pt["policies"][policy])
                for pt in summary["points"]
                if policy in pt["policies"]
            ]
            if not pts:
                continue
            color, marker, ls = POLICY_STYLE[policy]
            xs = [x for x, _ in pts]
            ys = [r[stat]["value"] for _, r in pts]
            errs = list(zip(*[yerr(r[stat]) for _, r in pts], strict=True))
            ax.errorbar(
                xs, ys, yerr=errs, color=color, marker=marker, linestyle=ls, capsize=3, label=policy
            )
        ax.set_title(f"{stat} end-to-end latency")
        ax.set_xlabel("offered load (req/s)")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("ms")
    legend(axes[0], fontsize=8)
    fig.suptitle(f"Latency by policy, {label} trace (95% bootstrap intervals)")
    footer(fig, [label], set(summary["vehicle"]))
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    path = out / f"latency_by_policy_{label}.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def h1_interaction(campaigns: dict[str, dict], out: Path) -> Path:
    """The H1 interaction at every point where all four 2x2 cells are steady.

    Left: on log mean latency, the primary statistic, where 0 means calibration saves the
    same fraction with and without queue depth. Right: the same contrast in ms, which grows
    with the queue-blind router's latency whether or not anything interacts. Points where a
    2x2 cell is transient or saturated are not drawn; the caption counts them.
    """
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    skipped = []
    for label, summary in campaigns.items():
        pts = [pt for pt in summary["points"] if pt["h1"] and not pt.get("staleness_s")]
        skipped += [
            f"{label} {pt['lambda_rps']:g}"
            for pt in summary["points"]
            if not pt["h1"] and not pt.get("staleness_s")
        ]
        if not pts:
            continue
        xs = [pt["lambda_rps"] for pt in pts]
        for ax, key, ci_key in (
            (axes[0], "interaction_log", "interaction_log_ci95"),
            (axes[1], "interaction", "ci95"),
        ):
            ys = [pt["h1"]["mean"][key] for pt in pts]
            errs = [
                [y - pt["h1"]["mean"][ci_key][0] for y, pt in zip(ys, pts, strict=True)],
                [pt["h1"]["mean"][ci_key][1] - y for y, pt in zip(ys, pts, strict=True)],
            ]
            ax.errorbar(xs, ys, yerr=errs, marker="o", capsize=3, label=label)
    for ax, title, unit in (
        (axes[0], "on log mean latency (primary)", "log(WJSQ/JSQ) - log(SW/RR)"),
        (axes[1], "on mean latency in ms (secondary)", "ms"),
    ):
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_title(f"H1 interaction {title}", fontsize=10)
        ax.set_xlabel("offered load (req/s)")
        ax.set_ylabel(unit)
        ax.grid(alpha=0.3)
    legend(axes[0], fontsize=8)
    fig.suptitle(
        "H1 at steady-state points only. Above 0, calibration buys less once queue depth is known."
        + (
            f"\nNot drawn (a 2x2 cell transient or saturated): {', '.join(skipped)}"
            if skipped
            else ""
        ),
        fontsize=8,
    )
    vehicles = {v for s in campaigns.values() for v in s["vehicle"]}
    footer(fig, list(campaigns), vehicles)
    fig.tight_layout(rect=(0, 0.03, 1, 0.9))
    path = out / "h1_interaction.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def calibration_gain_by_shape(
    campaigns: dict[str, dict], out: Path, rho: dict[str, float]
) -> Path | None:
    """What calibration buys each router on each workload, as a ratio and in ms.

    A bar is drawn only where both of the router's cells are steady. The ratio panel is the
    one to read across workloads: calibrated latency over uncalibrated latency, so 0.8 means
    calibration removed 20% of the latency, whatever the latency was. `rho` orders the shapes
    and labels them, and comes from the phase-ratio report (`shape_rho`).
    """
    shapes = [lbl for lbl in campaigns if lbl in rho]
    if len(shapes) < 2:
        return None
    shapes.sort(key=lambda lbl: rho[lbl])
    lambdas = sorted(
        {
            pt["lambda_rps"]
            for lbl in shapes
            for pt in campaigns[lbl]["points"]
            if not pt.get("staleness_s")
        }
    )
    lambdas = [
        lam
        for lam in lambdas
        if sum(any(pt["lambda_rps"] == lam for pt in campaigns[lbl]["points"]) for lbl in shapes)
        >= 2
    ]
    fig, axes = plt.subplots(len(lambdas), 2, figsize=(10, 3.2 * len(lambdas)), squeeze=False)
    for row, lam in enumerate(lambdas):
        for col, (field, ci_field, ylabel) in enumerate(
            (
                ("ratio", "ratio_ci95", "calibrated / uncalibrated mean latency"),
                ("gain_ms", "gain_ms_ci95", "ms saved by calibration"),
            )
        ):
            ax = axes[row][col]
            for kind, color, offset in (
                ("queue_blind", "#1f77b4", -0.18),
                ("queue_aware", "#2ca02c", 0.18),
            ):
                xs, ys, errs = [], [], [[], []]
                for i, lbl in enumerate(shapes):
                    pt = next(
                        (
                            p
                            for p in campaigns[lbl]["points"]
                            if p["lambda_rps"] == lam and not p.get("staleness_s")
                        ),
                        None,
                    )
                    if pt is None:
                        continue
                    g = pt["calibration_gain"][kind]
                    if g["status"] != "defined":
                        ax.text(
                            i + offset,
                            0.02 if field == "ratio" else 0,
                            "n/d",
                            ha="center",
                            va="bottom",
                            fontsize=7,
                            color=color,
                        )
                        continue
                    v = g["mean"][field]
                    lo, hi = g["mean"][ci_field]
                    xs.append(i + offset)
                    ys.append(v)
                    errs[0].append(v - lo)
                    errs[1].append(hi - v)
                ax.bar(
                    xs,
                    ys,
                    width=0.36,
                    color=color,
                    yerr=errs,
                    capsize=3,
                    label=kind.replace("_", "-"),
                )
            if field == "ratio":
                ax.axhline(1, color="black", linewidth=0.8)
                ax.set_ylim(0, 1.1)
            else:
                ax.axhline(0, color="black", linewidth=0.8)
            ax.set_xticks(range(len(shapes)), [f"{s}\nrho {rho[s]:g}" for s in shapes])
            ax.set_title(f"{lam:g} req/s", fontsize=10)
            ax.set_ylabel(ylabel, fontsize=8)
            ax.grid(alpha=0.3, axis="y")
    legend(axes[0][0], fontsize=8)
    fig.suptitle(
        "What calibration buys, by workload shape. n/d: a cell of that router was transient or"
        " saturated. Load in req/s is not matched across shapes.",
        fontsize=9,
    )
    vehicles = {v for lbl in shapes for v in campaigns[lbl]["vehicle"]}
    footer(fig, shapes, vehicles)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    path = out / "calibration_gain_by_shape.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def routing_share(label: str, summary: dict, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(6, 3.6))
    lambdas = [pt["lambda_rps"] for pt in summary["points"]]
    width = 0.8 / len(POLICY_ORDER)
    for i, policy in enumerate(POLICY_ORDER):
        ys = [
            pt["policies"].get(policy, {}).get("share_to_fast_node", 0) for pt in summary["points"]
        ]
        xs = [j + (i - 2) * width for j in range(len(lambdas))]
        ax.bar(xs, ys, width=width, color=POLICY_STYLE[policy][0], label=policy)
    ax.set_xticks(range(len(lambdas)), [f"{x:g}" for x in lambdas])
    ax.set_xlabel("offered load (req/s)")
    ax.set_ylabel(f"share of requests to {summary['fast_node']}")
    ax.set_ylim(0, 1.3)
    legend(ax, fontsize=7, ncol=3, loc="upper center")
    ax.grid(alpha=0.3, axis="y")
    ax.set_title(f"Where each policy sends work, {label} trace")
    footer(fig, [label], set(summary["vehicle"]))
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    path = out / f"routing_share_{label}.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def phase_ratio(report: dict, out: Path) -> Path:
    profiles = sorted(report["profiles"], key=lambda p: p["mean_rho"])
    fig, ax = plt.subplots(figsize=(8, 4.2))
    width = 0.27
    for i, (key, color) in enumerate(
        (("R_service", "#333"), ("R_prefill", "#d62728"), ("R_decode", "#1f77b4"))
    ):
        xs = [j + (i - 1) * width for j in range(len(profiles))]
        ax.bar(
            xs,
            [p[key] for p in profiles],
            width=width,
            color=color,
            label=key.replace("R_", "R on "),
        )
    ax.set_xticks(
        range(len(profiles)),
        [f"{shape_name(p['profile'])}\nrho {p['mean_rho']:g}" for p in profiles],
    )
    ax.axhline(1, color="black", linewidth=0.8)
    ax.set_yscale("log")
    # phase_ratio.py divides the slow node's time by the fast node's.
    ax.set_ylabel("slow node time over fast node time")
    legend(ax, fontsize=8, loc="upper left")
    ax.set_ylim(0.9, 30)
    ax.grid(alpha=0.3, axis="y")
    fast = report["fast"]["node_class"].split("_")[0]
    slow = report["slow"]["node_class"].split("_")[0]
    ax.set_title(
        f"R seen by each workload: {slow} over {fast}, concurrency {report['concurrency']}"
    )
    fig.text(
        0.01,
        0.005,
        f"vehicle: cost model (C-3) | {report['fast']['snapshot_id']} / {report['slow']['snapshot_id']}",
        fontsize=6,
        color="#555",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    path = out / "phase_ratio.png"
    fig.savefig(path, dpi=160)
    plt.close(fig)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--campaign", action="append", default=[], help="label=path/to/summary.json, repeatable"
    )
    ap.add_argument("--phase-ratio", type=Path, help="JSON written by tools/phase_ratio.py --out")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    campaigns = {}
    for spec in args.campaign:
        label, _, path = spec.partition("=")
        campaigns[label] = json.loads(Path(path).read_text())
    report = json.loads(args.phase_ratio.read_text()) if args.phase_ratio else None
    written = []
    for label, summary in campaigns.items():
        written.append(latency_by_policy(label, summary, args.out))
        written.append(routing_share(label, summary, args.out))
    if campaigns:
        written.append(h1_interaction(campaigns, args.out))
        # The shapes are placed by their measured rho, so without the report there is no
        # axis to put them on and the figure is not drawn.
        gain = calibration_gain_by_shape(campaigns, args.out, shape_rho(report)) if report else None
        if gain:
            written.append(gain)
    if report:
        written.append(phase_ratio(report, args.out))
    for p in written:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
