#!/usr/bin/env python3
"""Promote a finished calibration into the contracts, and point every config at it.

After a node class is recalibrated, five things have to happen before any campaign can run,
and doing them by hand on 2026-09-17 took an hour and one near miss:

1. **Backfill the phase split.** `calibrate` writes snapshots whose entries carry service time
   only; the per-request prefill and decode timings are in `observations.jsonl`, and
   `tools/backfill_phase_split.py` summarises them onto the snapshots. Without it, phase R
   and the phase-aware parts of the simulator have nothing to read.
2. **Validate against C-3, and against the hardware it claims to describe.** A snapshot that
   fails the schema, for instance one whose provenance lacks `driver`, is refused rather than
   promoted. So is one that is inverted in concurrency, where a bucket's service time falls
   as more requests share the engine.
3. **Copy the series into `contracts/cost_models/<node_class>/`.**
4. **Repoint the configs.** `tools/hw_runs.py` refuses any campaign whose config names
   an older snapshot than the newest in its class, because that config missed a promotion.
   Every config in `dataplane/configs/` that names the class's previous newest snapshot is
   rewritten to name the new one. The edit replaces the id string only, so a config's layout
   and comments stay as they were. Manifests of runs already done keep the snapshot that
   served them.
5. **Dry-run every campaign**, so a config that still does not check is found now.

It also answers the question a recalibration is for: did the node change? It prints the
capability and every cell's service and prefill time against the snapshot it replaces.

Usage:
  uv run --project dataplane python tools/promote_calibration.py \\
      runs/calibration/llama32-1b-anchorgrid/cal_rtx3050_ngl99_p4_q4km_llama32_1b_<ts> [--dry-run]
"""

from __future__ import annotations

import argparse
import itertools
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import pool_load

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_ROOT = REPO_ROOT / "contracts" / "cost_models"
CONFIGS = REPO_ROOT / "dataplane" / "configs"
TOOLS = REPO_ROOT / "tools"


def newest_in_class(index: dict[str, dict[str, Any]], node_class: str) -> dict[str, Any] | None:
    snaps = [s for s in index.values() if s["node_class"] == node_class]
    return max(snaps, key=lambda s: s["measured_at_unix"]) if snaps else None


def has_phase_split(snap: dict[str, Any]) -> bool:
    return all(e.get("prefill_ms_mean") is not None for e in snap["entries"])


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=REPO_ROOT)


def compare(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Capability and every shared cell, old against new, as printable lines."""

    def key(e: dict[str, Any]) -> tuple:
        return (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]), e["concurrency"])

    out = [
        (
            f"capability  {pool_load.capability(old):.3f} -> {pool_load.capability(new):.3f} "
            "output tok/s of service"
        ),
        (
            f"{'prompt':>12} {'output':>10} {'c':>2} {'service':>17} {'change':>7} "
            f"{'prefill':>15} {'change':>7}"
        ),
    ]
    olds = {key(e): e for e in old["entries"]}
    for e in sorted(new["entries"], key=key):
        o = olds.get(key(e))
        if o is None:
            continue
        ds = 100 * (e["service_ms_mean"] - o["service_ms_mean"]) / o["service_ms_mean"]
        pre = ""
        if o.get("prefill_ms_mean") and e.get("prefill_ms_mean"):
            dp = 100 * (e["prefill_ms_mean"] - o["prefill_ms_mean"]) / o["prefill_ms_mean"]
            pre = f"{o['prefill_ms_mean']:6.0f} -> {e['prefill_ms_mean']:6.0f} {dp:+6.1f}%"
        out.append(
            f"{e['prompt_bucket']!s:>12} {e['output_bucket']!s:>10} {e['concurrency']:>2} "
            f"{o['service_ms_mean']:6.0f} -> {e['service_ms_mean']:6.0f} {ds:+6.1f}% {pre}"
        )
    return out


def inversions(snap: dict[str, Any], tolerance: float = 0.02) -> list[str]:
    """Cells whose service time falls as concurrency rises, which hardware does not do.

    Sharing an engine with more requests cannot make a request faster, so a bucket whose
    mean drops from one concurrency to the next is a measurement artefact rather than a
    property of the node. The 2026-09-14 RTX 3050 grid was inverted at concurrency 3 in
    all six of its buckets, by 15 to 40%, and nothing refused it: it reached the contracts,
    parameterised the simulator, and is why the simulator failed the contrast criterion on
    every held-out shape (`results.md` section 8). The tolerance absorbs sampling noise,
    not a step.
    """
    by_bucket: dict[tuple, dict[int, float]] = {}
    for e in snap["entries"]:
        key = (tuple(e["prompt_bucket"]), tuple(e["output_bucket"]))
        by_bucket.setdefault(key, {})[e["concurrency"]] = e["service_ms_mean"]
    out = []
    for (pb, ob), row in sorted(by_bucket.items()):
        for lo, hi in itertools.pairwise(sorted(row)):
            if row[hi] < row[lo] * (1 - tolerance):
                out.append(
                    f"prompt {list(pb)} output {list(ob)}: c={lo} {row[lo]:.0f} ms is slower "
                    f"than c={hi} {row[hi]:.0f} ms, by {100 * (row[lo] / row[hi] - 1):.0f}%"
                )
    return out


def rescale_overrides(
    config: dict[str, Any], capabilities: dict[str, float]
) -> tuple[dict[str, Any], list[str]]:
    """The ablation's believed-ratio arms, re-expressed against a recalibrated pool.

    Analysis-plan 6.1 sweeps what WJSQ is *told* the pool's capability ratio is: 1.0, 1.2,
    2.5, 3.35, 5.0 and 100. Those ratios are the axis of the value-of-calibration curve and
    they never move. What the config stores is not the ratio but the pair of tok/s numbers
    that expresses it, anchored on the slow node's measured capability, so a recalibration
    of that node leaves every arm claiming a ratio it no longer has. Nothing would fail:
    the campaign would run and the curve's x-axis would be quietly wrong by whatever the
    node moved by.

    So each override is rebuilt from the ratio it already encodes, with the slow node put
    back at its measurement. The slow node is the anchor because it is the one the arms
    hold fixed, and it is identified by measurement, not by name: whichever node in the
    override has the lowest measured capability.

    This is why promoting the fast node changes no override. Its measured capability is not
    what any arm is anchored on, and what the pair's own ratio is at four slots is the `c4`
    arm's job to read from the cost model rather than a number written into a belief.

    Returns the rewritten config and one line per value that moved, which is empty when
    nothing did.
    """
    out = json.loads(json.dumps(config))
    changed: list[str] = []
    for arm in out.get("capability_arms", []):
        override = arm.get("capability_override")
        if not override:
            continue
        name = str(arm.get("name", ""))
        if set(override) != set(capabilities):
            # An arm that names a node the pool does not have, or leaves one out, is not a
            # belief about this pool at all. Rescaling it would invent an anchor, so it is
            # refused here rather than quietly re-expressed against a pool it never meant.
            raise ValueError(
                f"{name}: capability_override covers {sorted(override)}, but the pool this "
                f"config names is {sorted(capabilities)}"
            )
        anchor = min(override, key=lambda n: capabilities[n])
        base = override[anchor]
        # The believed ratio is recovered from the pair and rounded before it is applied.
        # The config stores the product of a ratio and a measurement, both already rounded,
        # so the quotient comes back a few parts in a million off the ratio the plan states,
        # and re-anchoring would walk the axis a little further from it at every promotion.
        rebuilt = {
            node: round(capabilities[anchor] * round(value / base, 4), 3)
            for node, value in override.items()
        }
        # A value left exactly as written when the rescale lands on it, so a promotion that
        # moves nothing rewrites nothing.
        if rebuilt != override:
            arm["capability_override"] = rebuilt
            changed.extend(
                f"{name}: {node} {override[node]:g} -> {value:g}"
                for node, value in rebuilt.items()
                if value != override[node]
            )
    return out, changed


def without_comments(value: Any) -> Any:
    """The same structure with every `_`-prefixed key dropped, comments included."""
    if isinstance(value, dict):
        return {k: without_comments(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [without_comments(v) for v in value]
    return value


def rewrite_overrides(text: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    """`after` written back over the config text, by replacing the numbers that moved.

    As a text substitution rather than a JSON dump, because a config is written to be read:
    it carries a `_comment` that explains what each arm is, and that comment quotes the
    anchor. A dump would keep the stale sentence and reformat everything around it. The
    substitution catches the prose too, which is the only way the old number stops being
    in the file.

    It falls back to a dump if the edited text no longer parses as the config it should be,
    which is what happens if one of these numbers is also something else in the file. The
    check ignores the comments, since editing them is the point.
    """
    moves: dict[float, float] = {}
    for was, now in zip(
        before.get("capability_arms", []), after.get("capability_arms", []), strict=True
    ):
        for node, value in (now.get("capability_override") or {}).items():
            old_value = was["capability_override"][node]
            if value != old_value:
                moves[old_value] = value
    out = text
    for old_value, value in sorted(moves.items(), key=lambda kv: -len(json.dumps(kv[0]))):
        out = out.replace(json.dumps(old_value), json.dumps(value))
    try:
        if without_comments(json.loads(out)) != without_comments(after):
            return json.dumps(after, indent=2) + "\n"
    except json.JSONDecodeError:
        return json.dumps(after, indent=2) + "\n"
    return out


def capabilities_of(config: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, float]:
    """What each of a config's nodes is measured at, under the snapshots it names."""
    return {
        node_id: pool_load.capability(index[snapshot_id])
        for node_id, snapshot_id in (config.get("cost_model_snapshots") or {}).items()
        if snapshot_id in index
    }


def configs_naming(snapshot_id: str) -> list[Path]:
    """Configs whose cost_model_snapshots name this id, confirmed by parsing, not by grep."""
    hits = []
    for path in sorted(CONFIGS.glob("*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        # Some configs are a bare list of node blocks, and name no snapshot at all.
        if isinstance(d, dict) and snapshot_id in (d.get("cost_model_snapshots") or {}).values():
            hits.append(path)
    return hits


class Repoint(NamedTuple):
    """One config's rewrite: where it goes, its new text, and a line per override that moved."""

    path: Path
    text: str
    changed: list[str]


def plan_repoint(old_id: str, new_snap: dict[str, Any], index: dict[str, dict]) -> list[Repoint]:
    """Every config that names `old_id`, rewritten to name `new_snap`, without writing any.

    Planning is kept apart from writing so a refusal leaves nothing behind. When the check ran
    inside the write loop, a config that could not be re-anchored stopped the promotion after
    the snapshots were copied and the configs before it were rewritten, so the contracts and
    half the configs had moved and the rest had not.

    Raises ValueError naming the config when one of its believed-ratio arms cannot be
    re-anchored.
    """
    # The index the configs are read against has to hold the snapshot being promoted, or a
    # dry run would price the arms off the calibration this one replaces.
    fresh_index = {**index, new_snap["snapshot_id"]: new_snap}
    plans = []
    for path in configs_naming(old_id):
        text = path.read_text(encoding="utf-8")
        # The id is swapped as text so a config's layout and comments survive. Only a
        # config whose arms move is rewritten as JSON, and only then.
        moved = text.replace(old_id, new_snap["snapshot_id"])
        config = json.loads(moved)
        try:
            rescaled, changed = rescale_overrides(config, capabilities_of(config, fresh_index))
        except ValueError as exc:
            raise ValueError(f"{path.relative_to(REPO_ROOT)} cannot be re-anchored: {exc}") from exc
        if changed:
            moved = rewrite_overrides(moved, config, rescaled)
        plans.append(Repoint(path, moved, changed))
    return plans


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path, help="a finished calibration run directory")
    ap.add_argument("--dry-run", action="store_true", help="report, and change nothing")
    args = ap.parse_args(argv)

    campaign = json.loads((args.run_dir / "campaign.json").read_text(encoding="utf-8"))
    node_class = campaign["node_class"]
    series = sorted((args.run_dir / "snapshots").glob("*.json"))
    if not series:
        print(f"refusing: {args.run_dir} has no snapshots")
        return 2

    # 1. Phase split.
    newest_path = series[-1]
    if not has_phase_split(json.loads(newest_path.read_text(encoding="utf-8"))):
        print("backfilling the phase split from observations.jsonl")
        if not args.dry_run:
            r = run(
                [
                    sys.executable,
                    str(TOOLS / "backfill_phase_split.py"),
                    "--observations",
                    str(args.run_dir / "observations.jsonl"),
                    "--snapshots",
                    str(args.run_dir / "snapshots" / "*.json"),
                ]
            )
            if r.returncode != 0:
                print(f"refusing: backfill failed\n{r.stdout}{r.stderr}")
                return 2
            if not has_phase_split(json.loads(newest_path.read_text(encoding="utf-8"))):
                print("refusing: the backfill ran but the newest snapshot still has no split")
                return 2

    # 2. C-3.
    r = run(
        [
            sys.executable,
            str(REPO_ROOT / "contracts" / "check.py"),
            "--validate",
            *(str(p) for p in series),
            "--schema",
            "cost_model.schema.json",
        ]
    )
    if r.returncode != 0:
        print(f"refusing: the snapshots do not satisfy C-3\n{r.stdout[-2000:]}")
        return 2
    print(f"{len(series)} snapshot(s) satisfy C-3")

    new = json.loads(newest_path.read_text(encoding="utf-8"))

    # 2b. Is the grid self-consistent?
    bad = inversions(new)
    if bad:
        print(f"refusing: {newest_path.name} is inverted in concurrency")
        for line in bad:
            print(f"  {line}")
        print(
            "  A request cannot be served faster by sharing the engine with more requests. "
            "Recalibrate this class rather than promoting a grid that says it can."
        )
        return 2
    index = pool_load.snapshot_index(SNAPSHOT_ROOT)
    old = newest_in_class(index, node_class)
    if old is not None and old["snapshot_id"] == new["snapshot_id"]:
        print(f"{new['snapshot_id']} is already the newest in {node_class}; nothing to promote")
        return 0
    if old is not None and old["measured_at_unix"] >= new["measured_at_unix"]:
        print(
            f"refusing: {old['snapshot_id']} in the contracts is newer than this run's "
            f"{new['snapshot_id']}, so promoting would repoint every config at the older "
            "calibration, which hw_runs.py then refuses"
        )
        return 2

    # The answer to "did the node change".
    if old is not None:
        print(f"\n{node_class}: {old['snapshot_id']}\n  -> {new['snapshot_id']}")
        for line in compare(old, new):
            print("  " + line)
        print()

    # 3. Promote, once every check has passed. The configs are planned before anything is
    # copied, so a config that refuses leaves the contracts and every config untouched.
    dest = SNAPSHOT_ROOT / node_class
    for p in series:
        target = dest / p.name
        if target.exists() and target.read_bytes() != p.read_bytes():
            print(f"refusing: {target} exists with different content")
            return 2
    try:
        repoints = plan_repoint(old["snapshot_id"], new, index) if old is not None else []
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        for p in series:
            shutil.copy2(p, dest / p.name)
    print(f"{'would promote' if args.dry_run else 'promoted'} {len(series)} snapshot(s) to {dest}")

    # 4. Repoint, and re-anchor any believed-ratio arm the new measurement moves.
    for plan in repoints:
        if not args.dry_run:
            plan.path.write_text(plan.text, encoding="utf-8")
        print(f"  {'would repoint' if args.dry_run else 'repointed'} {plan.path.relative_to(REPO_ROOT)}")
        for line in plan.changed:
            print(f"    {'would rescale' if args.dry_run else 'rescaled'} {line}")
    if args.dry_run:
        return 0

    # 5. Every campaign still checks.
    failed = []
    for path in sorted(CONFIGS.glob("hw_*.json")):
        r = run(
            [
                "uv",
                "run",
                "--project",
                "dataplane",
                "python",
                str(TOOLS / "hw_runs.py"),
                str(path),
                "--dry-run",
            ]
        )
        if r.returncode != 0:
            failed.append((path.name, (r.stdout + r.stderr).strip().splitlines()[-1:]))
    if failed:
        print("campaigns that no longer dry-run:")
        for name, tail in failed:
            print(f"  {name}: {tail}")
        return 1
    print("every hw_*.json dry-runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
