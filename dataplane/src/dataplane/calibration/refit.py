"""Re-fit a finished calibration from the samples it already wrote.

A calibration is two things: minutes of engine time, and the rules used to turn what the
engine did into a cost model. The samples do not go stale, but the rules change. The
occupancy rule is the one that forced this: a cell is now fitted only from the samples
served at the concurrency it claims, and every campaign taken before that rule existed was
fitted from all of them, draining batch included. Re-running those campaigns costs GPU time
that the samples already paid for, and it would measure a different machine on a different
day, which is exactly what a rule change is not supposed to introduce.

So the refit reads `observations.jsonl` and `campaign.json` from a finished run, rebuilds
the `CampaignResult` those samples came from, and puts it through `campaign._finish`: the
same fit, the same sigma, the same tau, the same snapshot series. Nothing here re-derives
any of that, because a second implementation of the fit is a second thing to keep in step
with C-3.

The result is a new run, not an edit of the old one. It is stamped with the time it was
fitted, carries its own run id, and names the run its samples came from as `refit_of`. The
source run keeps its snapshots: a promoted snapshot is what some campaign was actually
served, and rewriting it in place would make the record of that campaign untrue.

What a refit cannot recover is anything the log does not hold. The campaign counts a cell's
warmups in the occupancy of the samples they overlapped, and logs them with
`"segment": "warmup"` so the refit can count them the same way: fitting the same samples
twice, once online and once offline, has to give the same table. A log written before
warmups were logged has none to count, and its refit says so with
`occupancy_counts_warmups: false`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import campaign
from . import cost_model as cm

__all__ = ["by_cell", "load_observations", "refit_run", "with_occupancy"]


def by_cell(obs: list[cm.Observation]) -> list[list[cm.Observation]]:
    """The samples grouped the way the cell they belong to was run, in log order."""
    cells: dict[tuple[int, int, int], list[cm.Observation]] = {}
    for o in obs:
        cells.setdefault((o.prompt_len, o.output_len, o.concurrency), []).append(o)
    return list(cells.values())


def with_occupancy(
    obs: list[cm.Observation], warmups: Sequence[cm.Observation] = ()
) -> list[cm.Observation]:
    """Occupancy recomputed from the spans in the log, one cell at a time.

    Recomputed and not read, even when the log carries the field. A campaign written before
    the field existed carries nothing at all, and one written while warmups were left out of
    the count carries a number the same samples no longer produce; both have to end up with
    today's number, or the refit would fit two runs of one node by two rules. A request's
    span is `[t_end_ns - service_ns, t_end_ns]`, which the log records for every sample.

    Grouped by cell because the campaign runs one cell at a time: two cells in the same
    log are minutes apart and never shared the engine, but nothing in a span says so. A
    cell's warmups are found by the same key and counted as its samples' neighbours, the
    way the campaign counted them.
    """
    warm: dict[tuple[int, int, int], list[cm.Observation]] = {}
    for w in warmups:
        warm.setdefault((w.prompt_len, w.output_len, w.concurrency), []).append(w)
    out: list[cm.Observation] = []
    for group in by_cell(obs):
        key = (group[0].prompt_len, group[0].output_len, group[0].concurrency)
        out.extend(campaign._with_occupancy(group, warm.get(key, ())))
    return out


def load_observations(
    path: Path,
) -> tuple[list[cm.Observation], list[cm.Observation], list[cm.Observation]]:
    """The grid samples, sustained samples and warmups a campaign wrote, as `Observation`s.

    A sample with no `segment` is read as a grid sample: the field was added with the
    sustained segment, and the only logs without it are grid-only. A log written before
    warmups were logged gives an empty third list.
    """
    grid: list[cm.Observation] = []
    sustained: list[cm.Observation] = []
    warmups: list[cm.Observation] = []
    by_segment = {"sustained": sustained, "warmup": warmups}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        obs = cm.Observation(
            prompt_len=d["prompt_len"],
            output_len=d["output_len"],
            concurrency=d["concurrency"],
            service_ns=d["service_ns"],
            output_tokens=d["output_tokens"],
            t_end_ns=d["t_end_ns"],
            status=d.get("status", "ok"),
            prefill_ns=d.get("prefill_ns"),
            decode_ns=d.get("decode_ns"),
            error=d.get("error", ""),
            occupancy_mean=d.get("occupancy_mean"),
        )
        by_segment.get(d.get("segment"), grid).append(obs)
    return grid, sustained, warmups


def refit_run(run_dir: Path, out_root: Path) -> Path:
    """Fit a finished run's samples again by today's rules, into a new run under `out_root`.

    Returns the new run directory. A directory that is not a finished run raises
    `FileNotFoundError` on the file it is missing: a refit without the campaign record
    cannot know the buckets, the admissibility bounds or the provenance the snapshot has to
    carry, and `tools/refit_calibration.py` says so before it gets here.
    """
    run_dir = Path(run_dir)
    report_path = run_dir / "campaign.json"
    obs_path = run_dir / "observations.jsonl"
    source: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    config = campaign.CampaignConfig.from_dict(source["config"])
    grid, sustained, warmups = load_observations(obs_path)

    result = campaign.CampaignResult(
        observations=with_occupancy(grid, warmups),
        # The sustained segment has no warmups, and its cell can share a key with a grid
        # cell, so the grid's warmups must not be matched to it.
        sustained=with_occupancy(sustained),
        # The warmups' own occupancy is recounted the same way the campaign counted it, with
        # the roles swapped: each cell's samples are the warmups' neighbours.
        warmups=with_occupancy(warmups, grid),
        # F-18 is a property of the engine that served the samples, not of the fit, so it
        # is carried over rather than recomputed from a log that has no timings to test.
        f18=source.get("f18_status", "partial"),
    )
    result.failures = campaign._failure_counts(result.observations + result.sustained)
    # `_finish` stamps the run id and `measured_at_unix` with the time of the fit, not the
    # time the samples were taken, and that is on purpose. Promotion refuses a snapshot that
    # is not newer than the newest in its class, so a refit stamped with the source run's
    # time would tie with the snapshot it replaces and could never be promoted over it.
    # `refit_of` keeps the link back to when the engine actually ran.
    campaign._finish(result, config)
    result.report["refit_of"] = source.get("run_id", run_dir.name)
    result.report["refit_source_dir"] = str(run_dir)

    out_dir = Path(out_root) / result.report["run_id"]
    campaign.write_result(out_dir, result)
    return out_dir
