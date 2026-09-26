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

The kinds:

- `policy_means`: end-to-end latency per policy, one column per point.
- `calibration_gain`: what calibration buys the queue-blind and the queue-aware router, and
  the load JSQ puts on the slow node.
- `h1_interaction`: the H1 interaction at every point, with the pool's utilisation and the
  load RoundRobin puts on the slow node.
- `load_trend`: the queue-aware gain across each workload's loads.
- `k1_ratios`: R per workload from a `cell_intervals.py` report, with the operating R the
  live runs recorded.

A block may also say `workload=`, which a run set holding several requires, and `stat=` to
choose the statistic: the mean, p50, p95 or p99. A table takes the freshest points a run set
has, because one that mixed a fresh cell with a stale one in neighbouring columns would
compare two different experiments.

A source can name several run sets joined by `+`, one per workload, and `labels=` then names
each set's workload in the same order, so one table can hold every shape:

    <!-- generated: policy_means runs/exp/g/summary.json+runs/exp/b/summary.json labels=gen,bal -->

For `k1_ratios` the first source is the `cell_intervals.py` report, the labels name the run
sets after it, and `load=` names the rate the operating columns are read at.
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


def parse_spec(text: str) -> tuple[str, list[str], dict[str, str]]:
    """`policy_means runs/exp/x/summary.json stat=p95` as (kind, sources, options).

    The kind comes first and the source second, because that is the order a person reads
    them in: what this table is, and what it was drawn from. A source naming several run
    sets joins them with `+`, and comes back as one path per set.

    An option with no `=` is refused rather than dropped or read as a flag: `stat p95` is a
    typo for `stat=p95`, and a table that ignored it would report the mean under a heading
    that says otherwise. So is a `labels=` that does not name every run set exactly once,
    since a label that fell on the wrong set would put one workload's numbers under
    another's name.
    """
    kind, source, *rest = text.split()
    for token in rest:
        if "=" not in token:
            raise ValueError(f"option {token} has no '='")
    sources = source.split("+")
    options = dict(token.split("=", 1) for token in rest)
    if "labels" in options:
        # k1_ratios reads a cell_intervals report first, and that file has no label.
        sets = sources[1:] if kind == "k1_ratios" else sources
        labels_for(options, len(sets))
    return kind, sources, options


def labels_for(options: dict[str, Any], n: int) -> list[str]:
    """The workload name of each of `n` run sets, from `labels=`, which must name all of them."""
    if "labels" not in options:
        raise ValueError(f"{n} run set(s) need labels= to name their workloads, one per set")
    labels = options["labels"].split(",")
    if len(labels) != n or not all(labels):
        raise ValueError(
            f"labels={options['labels']} names {len(labels)} workload(s) for {n} run set(s); "
            "give one non-empty label per set, in source order"
        )
    return labels


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


def points_of_sets(
    summaries: list[dict[str, Any]], options: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every run set's freshest points, each stamped with the workload its label names.

    A point keeps its set's position as `set_index`, so tables list the workloads in the
    order the block names them rather than alphabetically: the order is the author's, and
    results.md orders shapes by their prompt-to-output ratio.
    """
    if "labels" not in options and len(summaries) == 1:
        # One run set is a table like any other, and its points keep their own workload.
        return points_of(summaries[0], options)
    labels = labels_for(options, len(summaries))
    out = []
    for index, (label, summary) in enumerate(zip(labels, summaries, strict=True)):
        own = {k: v for k, v in options.items() if k not in ("labels", "workload")}
        out += [
            {**point, "workload": label, "set_index": index} for point in points_of(summary, own)
        ]
    return out


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
    """Run set, workload, then staleness, then rate: the order a campaign is read in."""
    return (
        point.get("set_index", 0),
        point.get("workload", ""),
        point["staleness_s"],
        point["lambda_rps"],
    )


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


def _signed(entry: dict[str, Any] | None, key: str, ci_key: str, places: int) -> str:
    """A signed value with its signed interval, or empty where the entry has none."""
    if not entry or entry.get(key) is None:
        return ""
    lo, hi = entry[ci_key]
    return f"{entry[key]:+.{places}f} [{lo:+.{places}f}, {hi:+.{places}f}]"


def slow_node_load(point: dict[str, Any], policy: str) -> str:
    """Offered over capacity on the slow node under one policy, or empty where not recorded."""
    utilisation = point.get("utilisation") or {}
    slow = utilisation.get("slow_node")
    cell = (utilisation.get("per_cell") or {}).get(policy, {}).get(slow) or {}
    value = cell.get("offered_over_capacity")
    return "" if value is None else f"{value:.2f}"


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
    columns = [
        "Queue-blind SW/RR",
        "ms",
        "Queue-aware WJSQ/JSQ",
        "ms",
        "Slow node under JSQ, offered / capacity",
    ]
    rows = [
        "| " + " | ".join([*head, *columns]) + " |",
        "|" + "---|" * (len(head) + len(columns)),
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
        values.append(slow_node_load(point, "jsq"))
        rows.append("| " + " | ".join([*label, *values]) + " |")
    return rows


def h1_interaction(points: list[dict[str, Any]], stat: str = "mean") -> list[str]:
    """The H1 interaction at every point, one row per point, blank where it is undefined.

    A point whose 2x2 has a transient or saturated cell computes no contrast, so its
    interaction cells stay empty and the status says why, in the run set's own words. The
    two load columns are what the reading of the table rests on: how full the pool was, and
    how close the queue-blind router ran the slow node to its capacity.
    """
    points = sorted(points, key=point_order)
    workloads = _several_workloads(points)
    head = (["Workload"] if workloads else []) + ["Load"]
    columns = [
        "Pool utilisation",
        "Slow node under RoundRobin, offered / capacity",
        "Interaction, log",
        "Interaction, ms",
        "Status",
    ]
    rows = [
        "| " + " | ".join([*head, *columns]) + " |",
        "|" + "---|" * (len(head) + len(columns)),
    ]
    for point in points:
        label = [point.get("workload", "")] if workloads else []
        label.append(f"{point['lambda_rps']:g}")
        pool = (point.get("utilisation") or {}).get("pool_utilisation")
        status = point.get("h1_status", "")
        entry = (point.get("h1") or {}).get(stat) if status == "defined" else None
        values = [
            "" if pool is None else f"{pool:.2f}",
            slow_node_load(point, "round_robin"),
            _signed(entry, "interaction_log", "interaction_log_ci95", 3),
            _signed(entry, "interaction", "ci95", 0),
            status,
        ]
        rows.append("| " + " | ".join([*label, *values]) + " |")
    return rows


def load_trend(summaries: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """The queue-aware gain across each workload's loads, lightest to heaviest.

    One row per run set, from its `load_trend_queue_aware` record: the gain in ms and as a
    ratio at each load, and the change from the lightest to the heaviest with its interval.
    A set with no record, or a load whose gain is undefined, reads `n/d`.
    """
    rows = [
        "| Workload | JSQ - WJSQ, ms | Change | WJSQ/JSQ | Change in ratio |",
        "|---|---|---|---|---|",
    ]

    def series(values: list[float | None] | None, spec: str) -> str:
        if not values:
            return UNDEFINED
        return " / ".join(UNDEFINED if v is None else format(v, spec) for v in values)

    for label, summary in summaries:
        trend = summary.get("load_trend_queue_aware") or {}
        change = UNDEFINED
        if trend.get("change_ms") is not None:
            lo, hi = trend["change_ms_ci95"]
            change = f"{trend['change_ms']:+.0f} [{lo:+.0f}, {hi:+.0f}]"
        ratio = UNDEFINED
        if trend.get("change_in_ratio") is not None:
            lo, hi = trend["change_in_ratio_ci95"]
            ratio = f"x{trend['change_in_ratio']:.3f} [{lo:.3f}, {hi:.3f}]"
        gains = series(trend.get("queue_aware_gain_ms"), ".0f")
        ratios = series(trend.get("wjsq_over_jsq"), ".3f")
        rows.append(f"| {label} | {gains} | {change} | {ratios} | {ratio} |")
    return rows


def shape_name(profile: str) -> str:
    """`trace_summarisation_1b` as `summarisation`, the label a campaign is given."""
    return profile.replace("trace_", "").replace("_1b", "")


def k1_ratios(
    cell_intervals: dict[str, Any],
    summaries: list[tuple[str, dict[str, Any]]],
    load: float | str,
) -> list[str]:
    """R per workload shape: the cost model's at one and four slots, and the live runs'.

    One row per profile in the `cell_intervals.py` report, ordered by its mean
    prompt-to-output ratio, which is the axis K1 is read along. Every cost-model cell carries
    its bootstrap interval, and a concurrency the grid did not cover reads `n/d`. The
    operating columns are the pooled R the steady cells of the live runs recorded, from the
    run set whose label names the profile's shape, at its freshest point whose rate rounds
    to `load` at one decimal (2.385 req/s is the 2.4 of the prose). The load is stated rather
    than chosen, because operating R rises with load and a column read at each set's own
    lightest load would compare the anchor at 1.305 req/s with a shape at 2.385. A shape
    with no run set, or no point at that load, reads `n/d`.
    """
    rows = [
        (
            "| Workload | Prompt:output | R service, 1 slot | R service, 4 slots | R prefill, 1 slot "
            "| R prefill, 4 slots | R decode, 1 slot | R decode, 4 slots | Operating R service "
            "| Operating R decode |"
        ),
        "|" + "---|" * 10,
    ]
    by_label = dict(summaries)
    at = round(float(load), 1)
    profiles = sorted(
        cell_intervals["profiles"],
        key=lambda p: (p.get("mean_rho") is None, p.get("mean_rho") or 0.0),
    )
    for profile in profiles:
        name = shape_name(profile["profile"])
        rho = profile.get("mean_rho")
        values = [name, UNDEFINED if rho is None else f"{rho:.2f}"]
        for phase in ("service", "prefill", "decode"):
            for slots in ("1", "4"):
                entry = profile["by_concurrency"].get(slots, {}).get(f"R_{phase}")
                values.append(_ci(entry, 2))
        operating: dict[str, Any] = {}
        if name in by_label:
            point = next(
                (p for p in points_of(by_label[name], {}) if round(p["lambda_rps"], 1) == at),
                {},
            )
            operating = (point.get("operating_R_steady_cells") or {}).get("pooled") or {}
        for phase in ("service", "decode"):
            value = operating.get(f"R_{phase}")
            values.append(UNDEFINED if value is None else f"{value:.2f}")
        rows.append("| " + " | ".join(values) + " |")
    return rows


# Kinds that tabulate points take the points of one or several run sets.
KINDS = {
    "policy_means": policy_means,
    "calibration_gain": calibration_gain,
    "h1_interaction": h1_interaction,
}
# Kinds that tabulate whole run sets take (label, summary) pairs.
SET_KINDS = {"load_trend": load_trend, "k1_ratios": k1_ratios}


# What a block may say beyond its kind and its source: which workload, which statistic, and
# the workload each of several run sets holds, and, for k1_ratios only, the load it reads
# operating R at.
# Anything else is a typo, and a typo that was ignored would leave a table quietly
# reporting something other than what it says it does.
OPTIONS = ("workload", "stat", "labels", "load")
# The statistics `stat=` may name. A value outside these is refused for the same reason: an
# unknown one used to reach the table as a key no entry has, and every cell read `n/d`.
STATS = ("mean", "p50", "p95", "p99")


def render(kind: str, summary: dict[str, Any] | list[dict[str, Any]], **options: Any) -> str:
    """One table's markdown, exactly as it should sit between the markers.

    `summary` is one run set, or one per source when a block names several. For
    `k1_ratios` the first is the `cell_intervals.py` report.
    """
    if kind not in KINDS and kind not in SET_KINDS:
        raise ValueError(
            f"unknown table kind {kind!r}; the kinds are {sorted([*KINDS, *SET_KINDS])}"
        )
    # `load=` means something to k1_ratios only, so on any other kind it is a typo like any
    # other unknown option.
    allowed = OPTIONS if kind == "k1_ratios" else tuple(o for o in OPTIONS if o != "load")
    unknown = sorted(k for k in options if k not in allowed)
    if unknown:
        raise ValueError(
            f"{kind}: unknown option(s) {', '.join(unknown)}; the options are {', '.join(allowed)}"
        )
    if "stat" in options and options["stat"] not in STATS:
        raise ValueError(
            f"{kind}: unknown stat={options['stat']!r}; the stats are {', '.join(STATS)}"
        )
    docs = summary if isinstance(summary, list) else [summary]
    if kind == "k1_ratios":
        intervals, sets = docs[0], docs[1:]
        return "\n".join(k1_ratios(intervals, _labelled(sets, options), options["load"]))
    if kind in SET_KINDS:
        return "\n".join(SET_KINDS[kind](_labelled(docs, options)))
    if len(docs) == 1 and "labels" not in options:
        points = points_of(docs[0], options)
    else:
        points = points_of_sets(docs, options)
    return "\n".join(KINDS[kind](points, **{k: v for k, v in options.items() if k == "stat"}))


def _labelled(
    summaries: list[dict[str, Any]], options: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    """Run sets paired with their labels, which every set needs: a row is named by its label."""
    return list(zip(labels_for(options, len(summaries)), summaries, strict=True))


def generated(text: str) -> list[tuple[str, str, str]]:
    """Every generated block: its kind, its source, and the markdown it currently holds."""
    out = []
    for match in OPEN_RE.finditer(text):
        close = CLOSE_RE.search(text, match.end())
        if close is None:
            raise ValueError(f"a generated block is never closed:{match.group('spec')}")
        kind, sources, _ = parse_spec(match.group("spec"))
        out.append((kind, "+".join(sources), text[match.end() : close.start()].strip("\n")))
    return out


def update(text: str, load: Any) -> str:
    """Every generated block in a document, regenerated from what `load` returns.

    `load` takes the source path a block names and returns the summary it holds, which is
    how the caller decides where a relative path is relative to, and how a test puts a
    summary in without a file. It is called once per distinct path: a document with six
    tables from one campaign should read that campaign once, however many blocks name it
    alone or beside other sets.
    """
    cache: dict[str, dict[str, Any]] = {}
    out: list[str] = []
    at = 0
    for match in OPEN_RE.finditer(text):
        close = CLOSE_RE.search(text, match.end())
        if close is None:
            raise ValueError(f"a generated block is never closed:{match.group('spec')}")
        kind, sources, options = parse_spec(match.group("spec"))
        for source in sources:
            if source not in cache:
                cache[source] = load(source)
        docs = [cache[source] for source in sources]
        fresh = render(kind, docs[0] if len(docs) == 1 else docs, **options)
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
