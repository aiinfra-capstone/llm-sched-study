"""F-20 / C-6 — the run manifest.

The manifest is the reproducibility record, and it is the *only* thing about a run that
is committed: traces regenerate byte-for-byte from (config, seed), logs are large and
per-run, but the manifest carries the seed, the trace SHA-256, and the git shas needed to
reproduce any of it.

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

__all__ = ["SEND_LAG_THRESHOLD_MS", "Validity", "build", "config_hash", "git_shas"]

# The open-loop guard. F-17 suggests 50 ms; a run that exceeds it anywhere in the
# measurement window is marked invalid rather than analysed, because a load generator
# that fell behind was not generating the load the manifest claims it was.
SEND_LAG_THRESHOLD_MS = 50.0


@dataclass
class Validity:
    """The C-6 validity block. Every field is a count of something that should be zero.

    `valid` is computed, never set. The conditions that invalidate a run outright: the
    load generator drifted (`send_lag_violations`), requests never came back
    (`dropped_requests`), or the pool was not what the manifest says it was
    (`colocated_nodes`, `engine_restarts`). `heartbeat_gaps` is reported but not fatal —
    a missed heartbeat degrades the scheduler's estimate, which is a thing H3 is *about*,
    not a thing that ruins the measurement.
    """

    max_send_lag_ms: float = 0.0
    send_lag_violations: int = 0
    dropped_requests: int = 0
    heartbeat_gaps: int = 0
    engine_restarts: int = 0
    colocated_nodes: int = 0

    @property
    def valid(self) -> bool:
        return (
            self.send_lag_violations == 0
            and self.dropped_requests == 0
            and self.colocated_nodes == 0
            and self.engine_restarts == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_send_lag_ms": round(self.max_send_lag_ms, 3),
            "send_lag_violations": self.send_lag_violations,
            "dropped_requests": self.dropped_requests,
            "heartbeat_gaps": self.heartbeat_gaps,
            "engine_restarts": self.engine_restarts,
            "valid": self.valid,
            "colocated_nodes": self.colocated_nodes,
        }

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
        return out


def config_hash(config: dict[str, Any]) -> str:
    """Canonical hash of the run config. Same config, same hash, on any machine."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _sha(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
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
    return {"worker": head, "scheduler": head, "harness": head, "sim": head} | overrides


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
        "git_shas": git_shas(),
        **({"f18_status": f18_status} if f18_status else {}),
        "validity": validity.to_dict(),
    }
