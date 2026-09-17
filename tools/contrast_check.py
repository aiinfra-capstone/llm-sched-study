#!/usr/bin/env python3
"""Analysis-plan 6.6: does the simulator reproduce the hardware's contrasts?

The simulator's old validation asked whether each policy's p50 and p95 landed within 25% of the
hardware's. A simulator can pass that on every policy and still get a 10 to 18% difference
between two policies wrong, and differences between policies are all the paper claims. So the
criterion is on the contrasts, and it compares two `summary.json` files written by
`campaign_summary.py`: one from a hardware run set, one from the simulator replaying exactly
those runs (`tools/p4_validate.py --contrasts` produces the second).

At every hardware point, on the cells the hardware marks steady, it passes when all three hold:

1. **Ranking.** The policies ordered by mean end-to-end latency come out in the same order.
2. **WJSQ over JSQ.** The simulator's ratio lies inside the hardware's 95% interval, wherever
   the hardware's is defined.
3. **The H1 interaction.** The simulator's interaction on log mean latency lies inside the
   hardware's 95% interval, wherever the hardware's is defined. A simulator that cannot define
   it where the hardware can fails this.

A ranking miss between two policies whose hardware intervals overlap is still a miss, because
the rule was written that way before any result existed. It is listed separately, so a reader
can see whether the simulator disagreed with the hardware or only with its noise.

Absolute p50 and p95 error per policy is reported beside the verdict, and decides nothing.

Usage:
  uv run --project dataplane python tools/contrast_check.py \\
      --hardware runs/exp/phase_balanced_1650ti_3050/summary.json \\
      --simulator /tmp/p4_balanced/summary.json --out /tmp/p4_balanced/contrast_check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

LAMBDA_TOL = 1e-3


def _point(
    summary: dict[str, Any], lam: float, stale: float, workload: str
) -> dict[str, Any] | None:
    return next(
        (
            p
            for p in summary["points"]
            if abs(p["lambda_rps"] - lam) <= LAMBDA_TOL
            and float(p.get("staleness_s", 0.0)) == float(stale)
            and (p.get("workload") or "") == workload
        ),
        None,
    )


def _overlap(a: list[float], b: list[float]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def compare(hw: dict[str, Any], sim: dict[str, Any]) -> dict[str, Any]:
    points = []
    for hp in hw["points"]:
        lam, stale = hp["lambda_rps"], float(hp.get("staleness_s", 0.0))
        wl = hp.get("workload") or ""
        sp = _point(sim, lam, stale, wl)
        entry: dict[str, Any] = {
            **({"workload": wl} if wl else {}),
            "lambda_rps": lam,
            "staleness_s": stale,
            "misses": [],
        }
        if sp is None:
            entry["misses"].append("the simulator has no run at this point")
            points.append(entry)
            continue

        steady = [
            p
            for p, r in hp["policies"].items()
            if not r["steady_state"]["transient"] and p in sp["policies"]
        ]
        hw_order = sorted(steady, key=lambda p: hp["policies"][p]["mean"]["value"])
        sim_order = sorted(steady, key=lambda p: sp["policies"][p]["mean"]["value"])
        entry["ranking"] = {"hardware": hw_order, "simulator": sim_order}
        if hw_order != sim_order:
            entry["misses"].append("ranking on mean latency differs")
            entry["ranking"]["swapped_pairs_indistinguishable_on_hardware"] = [
                [a, b]
                for i, a in enumerate(hw_order)
                for b in hw_order[i + 1 :]
                if sim_order.index(a) > sim_order.index(b)
                and _overlap(hp["policies"][a]["mean"]["ci95"], hp["policies"][b]["mean"]["ci95"])
            ]

        hq = hp.get("calibration_gain", {}).get("queue_aware", {})
        if hq.get("status") == "defined":
            lo, hi = hq["mean"]["ratio_ci95"]
            sim_ratio = None
            if "jsq" in sp["policies"] and "wjsq" in sp["policies"]:
                sim_ratio = (
                    sp["policies"]["wjsq"]["mean"]["value"] / sp["policies"]["jsq"]["mean"]["value"]
                )
            entry["wjsq_over_jsq"] = {
                "hardware": hq["mean"]["ratio"],
                "hardware_ci95": [lo, hi],
                "simulator": None if sim_ratio is None else round(sim_ratio, 4),
            }
            if sim_ratio is None or not lo <= sim_ratio <= hi:
                entry["misses"].append("WJSQ over JSQ outside the hardware interval")

        hh = hp.get("h1", {}).get("mean")
        if hh is not None:
            lo, hi = hh["interaction_log_ci95"]
            sh = sp.get("h1", {}).get("mean")
            entry["h1_interaction_log"] = {
                "hardware": hh["interaction_log"],
                "hardware_ci95": [lo, hi],
                "simulator": None if sh is None else sh["interaction_log"],
            }
            if sh is None:
                entry["misses"].append(
                    f"the simulator cannot define the H1 interaction ({sp['h1_status']})"
                )
            elif not lo <= sh["interaction_log"] <= hi:
                entry["misses"].append("H1 interaction outside the hardware interval")

        entry["absolute_error_percent"] = {
            p: {
                stat: round(
                    100
                    * (sp["policies"][p][stat]["value"] - hp["policies"][p][stat]["value"])
                    / hp["policies"][p][stat]["value"],
                    1,
                )
                for stat in ("p50", "p95")
            }
            for p in hp["policies"]
            if p in sp["policies"]
        }
        points.append(entry)

    return {
        "criterion": "analysis-plan 6.6: ranking, WJSQ/JSQ and the H1 interaction at every point",
        "passes": all(not p["misses"] for p in points),
        "points": points,
    }


def markdown(result: dict[str, Any]) -> str:
    out = [f"**{'PASS' if result['passes'] else 'FAIL'}** on {result['criterion']}.", ""]
    for p in result["points"]:
        wl = f"{p['workload']}, " if p.get("workload") else ""
        out.append(f"### {wl}{p['lambda_rps']} req/s, staleness {p['staleness_s']:g} s")
        if "ranking" in p:
            r = p["ranking"]
            out.append(f"- Ranking, hardware: {' < '.join(r['hardware'])}")
            out.append(f"- Ranking, simulator: {' < '.join(r['simulator'])}")
            if r.get("swapped_pairs_indistinguishable_on_hardware"):
                out.append(
                    "- Swapped pairs whose hardware intervals overlap: "
                    + ", ".join(
                        f"{a}/{b}" for a, b in r["swapped_pairs_indistinguishable_on_hardware"]
                    )
                )
        if "wjsq_over_jsq" in p:
            w = p["wjsq_over_jsq"]
            out.append(
                f"- WJSQ/JSQ: hardware {w['hardware']} {w['hardware_ci95']}, simulator {w['simulator']}"
            )
        if "h1_interaction_log" in p:
            h = p["h1_interaction_log"]
            out.append(
                f"- H1 interaction, log: hardware {h['hardware']} {h['hardware_ci95']}, "
                f"simulator {h['simulator']}"
            )
        out.append(f"- Misses: {'; '.join(p['misses']) if p['misses'] else 'none'}")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hardware", type=Path, required=True, help="hardware summary.json")
    ap.add_argument("--simulator", type=Path, required=True, help="simulator summary.json")
    ap.add_argument("--out", type=Path, help="writes <out>.json and <out>.md")
    args = ap.parse_args(argv)
    result = compare(
        json.loads(args.hardware.read_text(encoding="utf-8")),
        json.loads(args.simulator.read_text(encoding="utf-8")),
    )
    text = markdown(result)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        args.out.with_suffix(".md").write_text(text + "\n")
    return 0 if result["passes"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
