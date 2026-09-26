"""F-20 / C-6 — the run manifest.

The manifest is the reproducibility record, and it is the *only* thing about a run that
is committed: traces regenerate byte-for-byte from (config, seed) at the recorded
generator sha, logs are large and per-run, but the manifest carries the seed, the trace
SHA-256, the generator sha, and the git shas needed to reproduce any of it.

It is also where a run is declared invalid. That declaration is made by the harness, from
measurements, at the end of the run — not by a human reading a plot later and deciding it
looks wrong.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "COMPONENTS",
    "SEND_LAG_THRESHOLD_MS",
    "Validity",
    "build",
    "config_hash",
    "git_dirty",
    "git_shas",
    "heartbeat_gaps_from",
]

# The four components C-6 records a sha for.
COMPONENTS = ("worker", "scheduler", "harness", "sim")

# The open-loop guard. F-17 suggests 50 ms; a run that exceeds it anywhere in the
# measurement window is marked invalid rather than analysed, because a load generator
# that fell behind was not generating the load the manifest claims it was.
SEND_LAG_THRESHOLD_MS = 50.0


# The console line for each validity field a run may leave unmeasured.
_UNMEASURED_NOTES = {
    "heartbeat_gaps": (
        "heartbeat_gaps not counted: no heartbeat_summary record from the scheduler, so "
        "missed heartbeats are unknown (written as null, not 0)"
    ),
}


@dataclass
class Validity:
    """The C-6 validity block. Every field is a count of something that should be zero.

    `valid` is computed, never set. The conditions that invalidate a run outright: the
    load generator drifted (`send_lag_violations`), requests never came back
    (`dropped_requests`), the pool was not what the manifest says it was
    (`colocated_nodes`, `engine_restarts`, `engine_unchecked`), or the worker logs do not
    account for every dispatch (`worker_log_incomplete`).

    `engine_unchecked` is fatal for the reason `engine_restarts: 0` was never evidence on
    its own. That field was written by a driver that could not observe an engine at all, so
    its zero meant "nobody looked" while reading as "nothing happened". A driver that reads
    each engine's process before and after a run can now say which of the two it is, and a
    run where it could not look is not a measurement of the pool the manifest names.
    `heartbeat_gaps` is reported but not fatal: a missed heartbeat degrades the scheduler's
    estimate of a node, and does not make the measurement of the run wrong. It is None when
    nothing counted it (no `heartbeat_summary` record reached the harness, or the vehicle has
    no heartbeats), and a None is written as null and named in `unmeasured`, never as 0.

    `clock_unsynced_hosts` is reported and not fatal either, and the arithmetic is why.
    The only clock term the pipeline acts on is the rate error, because a constant offset
    cancels in a difference of single-host durations and there is no cross-host subtraction
    anywhere in C-4 or C-5. `clocksync` bounds a disciplined host at 100 ppm, which on a
    900 ms request is 0.09 ms. Even an undisciplined RTC drifting ten times that costs under
    a millisecond. What an unsynchronised host actually costs is the *evidence*: the run can
    no longer show that its durations were comparable, only assert it. That belongs in the
    record, not in the reject rule.
    """

    max_send_lag_ms: float = 0.0
    send_lag_violations: int = 0
    dropped_requests: int = 0
    heartbeat_gaps: int | None = None
    engine_restarts: int = 0
    engine_unchecked: int = 0
    colocated_nodes: int = 0
    clock_unsynced_hosts: int = 0
    # Pool nodes whose worker log does not hold one record per dispatch. The join is then
    # short of exactly the requests still running at the end, which is not a random sample.
    worker_log_incomplete: int = 0
    # Validity fields this run could not measure, by name. Reported, never fatal.
    unmeasured: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return (
            self.send_lag_violations == 0
            and self.dropped_requests == 0
            and self.colocated_nodes == 0
            and self.engine_restarts == 0
            and self.engine_unchecked == 0
            and self.worker_log_incomplete == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_send_lag_ms": round(self.max_send_lag_ms, 3),
            "send_lag_violations": self.send_lag_violations,
            "dropped_requests": self.dropped_requests,
            "heartbeat_gaps": self.heartbeat_gaps,
            "engine_restarts": self.engine_restarts,
            "engine_unchecked": self.engine_unchecked,
            "valid": self.valid,
            "colocated_nodes": self.colocated_nodes,
            "clock_unsynced_hosts": self.clock_unsynced_hosts,
            "worker_log_incomplete": self.worker_log_incomplete,
            **({"unmeasured": self.unmeasured_fields()} if self.unmeasured_fields() else {}),
        }

    def unmeasured_fields(self) -> list[str]:
        """`unmeasured` as written: a null heartbeat count is always named in it."""
        out = list(self.unmeasured)
        if self.heartbeat_gaps is None and "heartbeat_gaps" not in out:
            out.append("heartbeat_gaps")
        return out

    def notes(self) -> list[str]:
        """What this run could not measure, in words, for the console. Never invalidates."""
        return [
            _UNMEASURED_NOTES.get(name, f"{name} not measured in this run")
            for name in self.unmeasured_fields()
        ]

    def reasons(self) -> list[str]:
        """Why a run was rejected, in words, for the console. Empty when valid."""
        out: list[str] = []
        if self.send_lag_violations:
            out.append(
                f"{self.send_lag_violations} request(s) exceeded the "
                f"{SEND_LAG_THRESHOLD_MS:g} ms send-lag threshold "
                f"(max {self.max_send_lag_ms:.1f} ms) — the client was not open-loop "
                "for the whole window"
            )
        if self.dropped_requests:
            out.append(f"{self.dropped_requests} request(s) never returned a response")
        if self.colocated_nodes:
            out.append(
                f"{self.colocated_nodes} colocated node(s) — two logical nodes on one host "
                "reintroduce the contention confound F-9a exists to remove"
            )
        if self.engine_restarts:
            out.append(f"{self.engine_restarts} engine restart(s) mid-run")
        if self.engine_unchecked:
            out.append(
                f"{self.engine_unchecked} node(s) whose engine process could not be read "
                "before and after the run, so a restart can be neither confirmed nor ruled "
                "out and the pool was not shown to be the one the manifest claims"
            )
        if self.worker_log_incomplete:
            out.append(
                f"{self.worker_log_incomplete} node(s) whose worker log does not hold one "
                "record per dispatch, so the join is short of the requests still running at "
                "the end"
            )
        return out


def heartbeat_gaps_from(scheduler_records: list[dict[str, Any]]) -> int | None:
    """Missed heartbeats over the pool, from the scheduler's `heartbeat_summary` records (C-4).

    The live scheduler writes one record per node when it shuts down. C-4 also allows an
    `end_run` record, and every record is a running total, so the last record per node is
    the count and any earlier one is not added to it. None when there is no summary record:
    nobody counted, which is not 0.
    """
    last: dict[str, dict[str, Any]] = {}
    for r in scheduler_records:
        if r.get("type") == "heartbeat_summary":
            last[r["node_id"]] = r
    if not last:
        return None
    return sum(int(r["missed_beats"]) for r in last.values())


def unsynced_hosts(clock_sync: dict[str, Any] | None) -> int:
    """How many hosts in a C-6 `clock_sync` block had no disciplined clock.

    A missing block returns 0 rather than a count, because absent means nobody measured
    rather than everybody failed. The distinction matters: a single-host run has no clock
    block by design and is not thereby suspect.
    """
    if not clock_sync:
        return 0
    hosts = clock_sync.get("hosts") or {}
    return sum(1 for c in hosts.values() if not c.get("synchronised", False))


def config_hash(config: dict[str, Any]) -> str:
    """Canonical hash of the run config. Same config, same hash, on any machine."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _sha(repo: Path) -> str:
    """The full 40-character sha: a short one becomes ambiguous as the history grows."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def git_shas(root: Path | None = None, **overrides: str) -> dict[str, str]:
    """The four component shas C-6 requires.

    In this monorepo all four resolve to the same commit; the field stays four-valued
    because `scheduler` and `sim` are Aditya's and may yet move to their own repo, and a
    manifest that already has the shape survives that without a contract change.
    """
    root = root or Path(__file__).resolve().parents[4]
    head = _sha(root)
    return dict.fromkeys(COMPONENTS, head) | overrides


def git_dirty(root: Path | None = None) -> dict[str, bool]:
    """Per component, whether the tree had changes on top of the sha `git_shas` names.

    A sha names the committed tree. A run from a tree with uncommitted edits, or with
    untracked files, ran code that sha does not name. In this monorepo the four components
    share one tree, so they share one answer. A directory that is not a checkout reports
    dirty, since nothing it holds is named by a commit.
    """
    root = root or Path(__file__).resolve().parents[4]
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        dirty = bool(out.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        dirty = True
    return dict.fromkeys(COMPONENTS, dirty)


def build(
    *,
    run_id: str,
    config: dict[str, Any],
    trace_path: str | Path,
    trace_sha256: str,
    validity: Validity,
    nodes: list[dict[str, Any]] | None = None,
    vehicle: str = "hardware",
    policy: str = "round_robin",
    f18_status: str | None = None,
    cost_model_snapshots: dict[str, Any] | None = None,
    clock_sync: dict[str, Any] | None = None,
    started_unix: int | None = None,
) -> dict[str, Any]:
    """Assemble a C-6 manifest. Validated against the schema by `contracts/check.py`.

    `started_unix` is the one wall-clock read in the whole harness, and it is deliberately
    not used for any duration: it exists so a run can be found in a lab notebook. Every
    duration in the analysis comes from a single machine's monotonic clock.

    `nodes` is required and must be non-empty. The harness cannot invent it — under F-9a
    the node block IS the experimental condition (`-ngl`, `--threads`, `--parallel`, and
    the engine version each node actually ran), and it is the launcher that knows those. A
    manifest with an empty pool describes no run, so this refuses to build one rather than
    emitting a file that fails `contracts/check.py` afterwards.

    `clock_sync` is omitted when it was not measured, and that absence is the honest
    record: a zeroed block would read as "the clocks agreed" when what happened is that
    nobody looked. Single-host runs leave it out for exactly that reason.
    """
    if not nodes:
        raise ValueError(
            "manifest.build() needs a non-empty `nodes` block: under F-9a the per-node "
            "engine_config is the experimental condition, and the launcher owns it. "
            "Pass the pool description, or write the validity block on its own."
        )
    arrival = config.get("arrival", {})
    return {
        "run_id": run_id,
        "started_unix": started_unix if started_unix is not None else int(time.time()),
        "vehicle": vehicle,
        "config_hash": config_hash(config),
        "config": config,
        "trace_path": str(trace_path),
        "trace_sha256": trace_sha256,
        "policy": policy,
        "lambda": float(config.get("lambda", arrival.get("lambda_base", 0.0))),
        "staleness_s": float(config.get("staleness_s", 0.0)),
        "warmup_s": float(config.get("warmup_s", 0.0)),
        "duration_s": float(config["duration_s"]),
        "cost_model_snapshots": cost_model_snapshots or {},
        "nodes": nodes,
        **({"clock_sync": clock_sync} if clock_sync else {}),
        "git_shas": git_shas(),
        "git_dirty": git_dirty(),
        **({"f18_status": f18_status} if f18_status else {}),
        "validity": validity.to_dict(),
    }
