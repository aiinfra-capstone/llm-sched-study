#!/usr/bin/env python3
"""Test-plan 3.8, the last requirement: do the live scheduler and the simulator decide alike?

The two vehicles share every policy class, and share nothing else. The live scheduler reads a
state store fed by heartbeats over a LAN, behind a staleness veil, under real concurrency; the
simulator reads its own event queue. So a policy can be right in both and the two can still
route differently, and F-23 would report the difference as simulator error without either
system looking broken.

The sequences themselves cannot be compared. Service times on hardware are drawn from an
engine and in the simulator from a cost model, so after the first completion the two pools
hold different requests, and every later decision is made on a different state. What can be
compared is the decision rule: wherever the two vehicles saw the same state for the same
request, they must have chosen the same node.

A decision pair is comparable when both logs hold the same `req_id`, every node's
`queue_depth`, `inflight`, `admissible` and capability agree, and the tie-break draw agrees.
The draw has to agree because a tie is resolved by it: two vehicles that saw one state and
drew differently may choose differently and both be correct. Pairs that agree on state but
not on the draw are counted and reported separately rather than judged.

Staleness is not excluded. Under a veil the live scheduler acts on an older view, which is
what makes many pairs incomparable, and the ones that remain are still a fair test of the
rule: the veil changes which state the policy is handed, not what it does with it.

Exit 0 when every comparable pair agrees, 2 when any disagrees, 1 when there was nothing to
compare or an input could not be read.

Usage:
  uv run --project dataplane python tools/parity_check.py \\
      --live runs/exp/phase_generation_1650ti_3050 --sim /tmp/p4_generation \\
      --out /tmp/p4_generation/parity
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CAPABILITY_TOL = 1e-6


def decisions(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Every decision record in a run's scheduler log, by `req_id`.

    A run writes one scheduler log, but a stopped and restarted run can leave more than one,
    so all of them are read. A `req_id` decided twice keeps the first decision: the second is
    a redispatch after a failure, which is a different state by definition.
    """
    out: dict[str, dict[str, Any]] = {}
    for log in sorted(run_dir.glob("scheduler_*.jsonl")):
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") != "decision":
                continue
            out.setdefault(rec["req_id"], rec)
    return out


def state(rec: dict[str, Any]) -> tuple:
    """The part of a decision record the policy actually reads, in a comparable form."""
    return tuple(
        sorted(
            (
                c["node_id"],
                c.get("queue_depth"),
                c.get("inflight"),
                bool(c.get("admissible", True)),
                round(float(c.get("capability_tok_s") or 0.0) / CAPABILITY_TOL),
            )
            for c in rec.get("candidates", [])
        )
    )


def draw(rec: dict[str, Any]) -> float | None:
    d = rec.get("tie_break_draw")
    return None if d is None else float(d)


def compare_run(live_dir: Path, sim_dir: Path) -> dict[str, Any]:
    """One live run against its own replay."""
    live = decisions(live_dir)
    sim = decisions(sim_dir)
    shared = sorted(set(live) & set(sim))
    comparable = 0
    agreed = 0
    other_draw = 0
    disagreements: list[dict[str, Any]] = []
    for req_id in shared:
        lrec, srec = live[req_id], sim[req_id]
        if state(lrec) != state(srec):
            continue
        if draw(lrec) != draw(srec):
            other_draw += 1
            continue
        comparable += 1
        if lrec.get("chosen_node") == srec.get("chosen_node"):
            agreed += 1
        else:
            disagreements.append(
                {
                    "req_id": req_id,
                    "live_chosen": lrec.get("chosen_node"),
                    "sim_chosen": srec.get("chosen_node"),
                    "state": [list(s) for s in state(lrec)],
                    "draw": draw(lrec),
                }
            )
    return {
        "run": live_dir.name,
        "live_decisions": len(live),
        "sim_decisions": len(sim),
        "shared_requests": len(shared),
        "comparable": comparable,
        "agreed": agreed,
        "same_state_other_draw": other_draw,
        "disagreements": disagreements,
    }


def pair_up(live_root: Path, sim_root: Path) -> list[tuple[Path, Path]]:
    """Match each live run dir to the replay of it, which p4_validate names `<run_id>_sim`."""
    if (live_root / "manifest.json").exists():
        live_dirs = [live_root]
    else:
        live_dirs = sorted(
            p for p in live_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()
        )
    pairs = []
    for d in live_dirs:
        for candidate in (sim_root / f"{d.name}_sim", sim_root / d.name, sim_root):
            if candidate.is_dir() and list(candidate.glob("scheduler_*.jsonl")):
                pairs.append((d, candidate))
                break
    return pairs


def markdown(results: list[dict[str, Any]], verdict: str) -> str:
    lines = [f"## Live and simulated parity: **{verdict}**", ""]
    lines.append("| run | shared | comparable | agreed | same state, other draw |")
    lines.append("|---|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r['run']} | {r['shared_requests']} | {r['comparable']} | {r['agreed']} "
            f"| {r['same_state_other_draw']} |"
        )
    bad = [d for r in results for d in r["disagreements"]]
    if bad:
        lines += ["", "### Disagreements", ""]
        for d in bad[:20]:
            lines.append(
                f"- `{d['req_id']}`: live chose {d['live_chosen']}, "
                f"simulator chose {d['sim_chosen']}, on {d['state']}"
            )
        if len(bad) > 20:
            lines.append(f"- and {len(bad) - 20} more")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Live and simulated parity (test-plan 3.8)")
    ap.add_argument("--live", type=Path, required=True, help="a live run dir, or a run set")
    ap.add_argument("--sim", type=Path, required=True, help="where the replays of those runs are")
    ap.add_argument("--out", type=Path, default=None, help="write <out>.json and <out>.md")
    args = ap.parse_args(argv)

    if not args.live.is_dir() or not args.sim.is_dir():
        print(f"need two directories: {args.live} and {args.sim}")
        return 1
    pairs = pair_up(args.live, args.sim)
    if not pairs:
        print(f"no run under {args.live} has a replay under {args.sim}")
        return 1

    results = [compare_run(live, sim) for live, sim in pairs]
    comparable = sum(r["comparable"] for r in results)
    agreed = sum(r["agreed"] for r in results)
    other_draw = sum(r["same_state_other_draw"] for r in results)
    bad = [d for r in results for d in r["disagreements"]]

    print(f"{len(pairs)} run pairs, {comparable} decisions made on the same state and draw")
    print(f"  agreed: {agreed}")
    print(
        f"  same state, different draw (a tie either vehicle may break its own way): {other_draw}"
    )
    for d in bad[:10]:
        print(f"  DISAGREES {d['req_id']}: live {d['live_chosen']} vs sim {d['sim_chosen']}")
    if len(bad) > 10:
        print(f"  ... and {len(bad) - 10} more")

    if not comparable:
        print("PARITY UNKNOWN: no decision was made on the same state in both vehicles")
        verdict = "unknown"
        rc = 1
    elif bad:
        print(f"PARITY FAILED: {len(bad)} of {comparable} comparable decisions differ")
        verdict = "fail"
        rc = 2
    else:
        print(f"PARITY PASSED: all {comparable} comparable decisions agree")
        verdict = "pass"
        rc = 0

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "verdict": verdict,
            "comparable": comparable,
            "agreed": agreed,
            "same_state_other_draw": other_draw,
            "runs": results,
        }
        args.out.with_suffix(".json").write_text(json.dumps(payload, indent=2) + "\n")
        args.out.with_suffix(".md").write_text(markdown(results, verdict))
        print(f"wrote {args.out.with_suffix('.json')} and {args.out.with_suffix('.md')}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
