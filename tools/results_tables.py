#!/usr/bin/env python3
"""Generate the tables in `results.md` from the summaries they report, and check them.

Every number in `results.md` was, at some point, copied out of a `summary.json` by hand. A
recalibration moves them, a rerun of a campaign moves them, and nothing in the repository
notices when the prose and the run set stop agreeing: the document does not know where its
numbers came from. This is the thing most likely to put a wrong number in the paper, because
it fails silently and looks exactly like a correct document.

So a table can be generated instead. A generated block says what it is and what it was drawn
from, holds the markdown between two markers, and is rewritten from the run set at any time:

    <!-- generated: policy_means runs/exp/x/summary.json stat=p95 -->
    | Policy | 2.385 req/s |
    <!-- /generated -->

Run it on a document to rewrite its tables. `--check` reports the ones that no longer match
what the run set says and writes nothing, which is what CI runs.

Two kinds to start:

- `policy_means`: end-to-end latency per policy, one column per point.
- `calibration_gain`: what calibration buys the queue-blind and the queue-aware router.

A block may also say `workload=`, which a run set holding several requires, and `stat=` to
choose the statistic: the mean, p50, p95 or p99. A table takes the freshest points a run set
has, because one that mixed a fresh cell with a stale one in neighbouring columns would
compare two different experiments.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "docs" / "results.md"

OPEN_RE = re.compile(r"<!-- generated:(?P<spec>[^>]*?)-->", re.MULTILINE)
CLOSE_RE = re.compile(r"<!-- /generated -->")
CLOSE = "<!-- /generated -->"


def parse_spec(text: str) -> tuple[str, str, dict[str, str]]:
    """`policy_means runs/exp/x/summary.json stat=p95` as (kind, source, options).

    The kind comes first and the source second, because that is the order a person reads
    them in: what this table is, and what it was drawn from.

    An option with no `=` is refused rather than dropped or read as a flag: `stat p95` is a
    typo for `stat=p95`, and a table that ignored it would report the mean under a heading
    that says otherwise.
    """
    kind, source, *rest = text.split()
    for token in rest:
        if "=" not in token:
            raise ValueError(f"option {token} has no '='")
    return kind, source, dict(token.split("=", 1) for token in rest)


def points_of(summary: dict[str, Any], options: dict[str, Any]) -> list[dict[str, Any]]:
    """The points a table is about: every one the options do not exclude, in run-set order."""
    points = summary["points"]
    if "workload" in options:
        points = [p for p in points if p.get("workload", "") == options["workload"]]
    if not points:
        raise ValueError(f"no point in the run set matches {options}")
    # A campaign that varies staleness holds the same load points several times over, and a
    # table that mixed them would compare a fresh cell with a stale one in neighbouring
    # columns. The freshest points are the table; what staleness costs is section 7's
    # question and has its own tables.
    fresh = min(p["staleness_s"] for p in points)
    points = [p for p in points if p["staleness_s"] == fresh]
    workloads = {p.get("workload", "") for p in points}
    if len(workloads) > 1:
        # Staleness has a natural default, the freshest; a workload does not. Two workloads
        # in one table are two different request mixes, and which one a table is about is a
        # decision for whoever writes the sentence above it.
        raise ValueError(
            f"this run set holds {len(workloads)} workloads ({', '.join(sorted(workloads))}); "
            "say which with workload="
        )
    return points


# The names results.md has always used for the policies, and the order it lists them in:
# the queue-blind routers, then the queue-aware ones, then the controls that were added to
# separate the ranking from the magnitude. A table that sorted alphabetically would put the
# ordinal control between JSQ and RoundRobin and read as though it belonged there.
DISPLAY = {
    "round_robin": "RoundRobin",
    "static_weighted": "StaticWeighted",
    "jsq": "JSQ",
    "wjsq": "WJSQ",
    "threshold": "Threshold",
    "jsq_fastfirst": "JSQFastFirst",
    "static_weighted_wrr": "StaticWeightedWRR",
    "ect": "ECT",
}
UNDEFINED = "n/d"


def policy_name(cell: str) -> str:
    """`wjsq@cap250` as `WJSQ@cap250`, and a policy nobody has named yet as itself."""
    base, _, arm = cell.partition("@")
    return DISPLAY.get(base, base) + (f"@{arm}" if arm else "")


def policy_order(cell: str) -> tuple:
    """Ladder order, with any unnamed policy after the named ones and arms after the base."""
    base, _, arm = cell.partition("@")
    known = list(DISPLAY)
    return (known.index(base) if base in known else len(known), base, arm)


def point_order(point: dict[str, Any]) -> tuple:
    """Workload, then staleness, then rate: the order a campaign is read in."""
    return (point.get("workload", ""), point["staleness_s"], point["lambda_rps"])


def point_label(point: dict[str, Any], workloads: bool) -> str:
    """How a point names a column, saying only what tells it apart from the others."""
    parts = [f"{point['workload']}"] if workloads and point.get("workload") else []
    parts.append(f"{point['lambda_rps']:g} req/s")
    return ", ".join(parts)


def _ci(entry: dict[str, Any] | None, places: int = 0) -> str:
    """A value with its interval, or `n/d` where the run set has none."""
    if not entry or entry.get("value") is None:
        return UNDEFINED
    lo, hi = entry["ci95"]
    return f"{entry['value']:.{places}f} [{lo:.{places}f}, {hi:.{places}f}]"


def _several_workloads(points: list[dict[str, Any]]) -> bool:
    """Whether a label has to name the workload, which it does only when one varies."""
    return len({p.get("workload", "") for p in points}) > 1


def policy_means(points: list[dict[str, Any]], stat: str = "mean") -> list[str]:
    """End-to-end latency per policy, one column per point. **T** marks a transient cell.

    A policy with no cell at a point leaves that cell empty rather than reading as a number
    someone can compare: the run either was not done or was thrown out, and both of those
    are states the campaign's own summary already names.
    """
    points = sorted(points, key=point_order)
    workloads = _several_workloads(points)
    labels = [point_label(p, workloads) for p in points]
    rows = [
        "| Policy | " + " | ".join(labels) + " |",
        "|" + "---|" * (len(points) + 1),
    ]
    cells = sorted({p for point in points for p in point["policies"]}, key=policy_order)
    for cell in cells:
        values = []
        for point in points:
            entry = point["policies"].get(cell)
            if entry is None:
                values.append("")
                continue
            transient = entry.get("steady_state", {}).get("transient")
            values.append(_ci(entry.get(stat)) + (" T" if transient else ""))
        rows.append(f"| {policy_name(cell)} | " + " | ".join(values) + " |")
    return rows


def calibration_gain(points: list[dict[str, Any]], stat: str = "mean") -> list[str]:
    """What calibration buys each router, as a ratio and in ms, one row per point."""
    points = sorted(points, key=point_order)
    workloads = _several_workloads(points)
    head = (["Workload"] if workloads else []) + ["Load"]
    rows = [
        "| " + " | ".join([*head, "Queue-blind SW/RR", "ms", "Queue-aware WJSQ/JSQ", "ms"]) + " |",
        "|" + "---|" * (len(head) + 4),
    ]
    for point in points:
        label = [point.get("workload", "")] if workloads else []
        label.append(f"{point['lambda_rps']:g}")
        values = []
        for key in ("queue_blind", "queue_aware"):
            entry = point["calibration_gain"].get(key) or {}
            e = entry.get(stat) if entry.get("status") == "defined" else None
            if e is None:
                values += [UNDEFINED, UNDEFINED]
                continue
            rlo, rhi = e["ratio_ci95"]
            lo, hi = e["gain_ms_ci95"]
            values.append(f"{e['ratio']:.3f} [{rlo:.3f}, {rhi:.3f}]")
            values.append(f"{e['gain_ms']:.0f} [{lo:.0f}, {hi:.0f}]")
        rows.append("| " + " | ".join([*label, *values]) + " |")
    return rows


KINDS = {"policy_means": policy_means, "calibration_gain": calibration_gain}


# What a block may say beyond its kind and its source: which workload, and which statistic.
# Anything else is a typo, and a typo that was ignored would leave a table quietly
# reporting something other than what it says it does.
OPTIONS = ("workload", "stat")
# The statistics `stat=` may name. A value outside these is refused for the same reason: an
# unknown one used to reach the table as a key no entry has, and every cell read `n/d`.
STATS = ("mean", "p50", "p95", "p99")


def render(kind: str, summary: dict[str, Any], **options: Any) -> str:
    """One table's markdown, exactly as it should sit between the markers."""
    if kind not in KINDS:
        raise ValueError(f"unknown table kind {kind!r}; the kinds are {sorted(KINDS)}")
    unknown = sorted(k for k in options if k not in OPTIONS)
    if unknown:
        raise ValueError(
            f"{kind}: unknown option(s) {', '.join(unknown)}; the options are {', '.join(OPTIONS)}"
        )
    if "stat" in options and options["stat"] not in STATS:
        raise ValueError(
            f"{kind}: unknown stat={options['stat']!r}; the stats are {', '.join(STATS)}"
        )
    points = points_of(summary, options)
    return "\n".join(KINDS[kind](points, **{k: v for k, v in options.items() if k == "stat"}))


def generated(text: str) -> list[tuple[str, str, str]]:
    """Every generated block: its kind, its source, and the markdown it currently holds."""
    out = []
    for match in OPEN_RE.finditer(text):
        close = CLOSE_RE.search(text, match.end())
        if close is None:
            raise ValueError(f"a generated block is never closed:{match.group('spec')}")
        kind, source, _ = parse_spec(match.group("spec"))
        out.append((kind, source, text[match.end() : close.start()].strip("\n")))
    return out


def update(text: str, load: Any) -> str:
    """Every generated block in a document, regenerated from what `load` returns.

    `load` takes the source path a block names and returns the summary it holds, which is
    how the caller decides where a relative path is relative to, and how a test puts a
    summary in without a file. It is called once per distinct source: a document with six
    tables from one campaign should read that campaign once.
    """
    cache: dict[str, dict[str, Any]] = {}
    out: list[str] = []
    at = 0
    for match in OPEN_RE.finditer(text):
        close = CLOSE_RE.search(text, match.end())
        if close is None:
            raise ValueError(f"a generated block is never closed:{match.group('spec')}")
        kind, source, options = parse_spec(match.group("spec"))
        if source not in cache:
            cache[source] = load(source)
        fresh = render(kind, cache[source], **options)
        out.append(text[at : match.end()])
        out.append("\n" + fresh + "\n")
        at = close.start()
    out.append(text[at:])
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "doc", type=Path, nargs="?", default=RESULTS, help="the document to rewrite or check"
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="report tables that no longer match their run set, and write nothing",
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="what a relative source path in a block is relative to",
    )
    args = ap.parse_args(argv)

    def load(source: str) -> dict[str, Any]:
        path = Path(source)
        return json.loads(
            (path if path.is_absolute() else args.root / path).read_text(encoding="utf-8")
        )

    text = args.doc.read_text(encoding="utf-8")
    try:
        fresh = update(text, load)
    except (ValueError, OSError, KeyError) as exc:
        print(f"{args.doc}: {exc}")
        return 2

    found = len(OPEN_RE.findall(text))
    if args.check:
        stale = [
            f"{kind} {source}"
            for (kind, source, was), (_, _, now) in zip(
                generated(text), generated(fresh), strict=True
            )
            if was != now
        ]
        if stale:
            print(f"{args.doc}: {len(stale)} of {found} generated table(s) no longer match:")
            for name in stale:
                print(f"  {name}")
            print("  regenerate them, and read what moved before committing")
            return 1
        print(f"{args.doc}: {found} generated table(s) match their run sets")
        return 0
    if fresh != text:
        args.doc.write_text(fresh, encoding="utf-8")
    print(f"{args.doc}: {found} generated table(s) written from their run sets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
