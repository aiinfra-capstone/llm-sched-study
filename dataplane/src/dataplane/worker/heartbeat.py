"""F-10 — the heartbeat emitter.

Every `interval_s`, each worker tells the scheduler what it looks like right now: queue
depth, in-flight count, recent tok/s, slot occupancy, engine state. The scheduler's whole
node view is built from these, so two properties matter more than the payload itself.

**`seq` is monotonic per node and never reused.** The scheduler detects a gap by finding a
hole in the sequence, not by timing — because the alternative is comparing a worker stamp
to a scheduler stamp, which is the cross-host clock subtraction the whole study refuses to
do. `worker_mono_ns` rides along for debugging and must never be subtracted by anyone.

**A heartbeat is never skipped because the engine is slow.** If `/slots` cannot be read the
node reports `degraded` with `kv_frac = -1.0` and keeps its cadence. A missed heartbeat
degrades the scheduler's estimate, which is a thing H3 is *about*; a heartbeat that lies
about occupancy is a thing that makes H3 unmeasurable.

`recent_tokens_per_s` is an EWMA over completed requests' **decode** throughput — the same
definition the cost model and MPR-1 use, so the scheduler's live view and the calibrated
estimate are the same quantity. Reporting end-to-end tok/s here would make a node look
slower whenever the workload's prompts got longer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from dataplane.worker.adapter import ENGINE_STATES, LiveState

__all__ = ["DEFAULT_INTERVAL_S", "Heartbeat", "HeartbeatEmitter", "heartbeat_payload"]

# F-10 says configurable, not what to configure. 1 s is a starting point, not a finding:
# the empirical basis for choosing it is MPR-1's tau, and until tau is resolved on
# hardware that drifts, any value here is an assumption rather than a result.
DEFAULT_INTERVAL_S = 1.0

# Smoothing for the tok/s EWMA. 0.3 keeps roughly the last handful of completions, which
# is short enough to track a node falling behind and long enough that one slow request
# does not make a healthy node look broken.
_EWMA_ALPHA = 0.3


@dataclass(frozen=True)
class Heartbeat:
    """One C-1 `Heartbeat` message, as a plain record."""

    run_id: str
    node_id: str
    seq: int
    queue_depth: int
    inflight_count: int
    recent_tokens_per_s: float
    kv_occupancy_frac: float
    worker_mono_ns: int
    engine_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "seq": self.seq,
            "queue_depth": self.queue_depth,
            "inflight_count": self.inflight_count,
            "recent_tokens_per_s": round(self.recent_tokens_per_s, 4),
            "kv_occupancy_frac": self.kv_occupancy_frac,
            "worker_mono_ns": self.worker_mono_ns,
            "engine_state": self.engine_state,
        }


def heartbeat_payload(
    *, run_id: str, node_id: str, seq: int, state: LiveState, recent_tokens_per_s: float
) -> Heartbeat:
    """Build one heartbeat from a `LiveState` snapshot. Pure — the clock is the only input."""
    if state.state not in ENGINE_STATES:  # pragma: no cover - LiveState already enforces it
        raise ValueError(f"engine_state {state.state!r} is not one of {ENGINE_STATES}")
    return Heartbeat(
        run_id=run_id,
        node_id=node_id,
        seq=seq,
        queue_depth=state.queue_depth,
        inflight_count=state.inflight,
        recent_tokens_per_s=recent_tokens_per_s,
        kv_occupancy_frac=state.kv_frac,
        worker_mono_ns=time.monotonic_ns(),
        engine_state=state.state,
    )


@dataclass
class HeartbeatEmitter:
    """Produces the heartbeat stream for one node, and owns the tok/s EWMA.

    Separated from whatever transports it (gRPC in the live worker, a list in a test) so
    that the cadence and the sequence numbering can be tested without a socket, and so the
    same source can feed the fixture worker.
    """

    run_id: str
    node_id: str
    interval_s: float = DEFAULT_INTERVAL_S
    _seq: int = 0
    _tok_s: float = 0.0
    _queue_depth: int = 0
    _emitted: list[Heartbeat] = field(default_factory=list)

    def start_run(self, run_id: str) -> None:
        """Point the stream at a new run **without restarting the sequence**.

        `seq` is monotonic per *node*, not per run (C-1): the scheduler finds a gap by
        finding a hole in it, and a counter that restarted between runs would look like the
        largest gap of the run.

        What does reset is the run-scoped state — the tok/s EWMA and the queue depth —
        because carrying the previous run's throughput into the first heartbeat of the next
        one reports a rate this run never ran at.

        This exists instead of building a fresh emitter per run, and that is the whole
        point: the beating task holds a reference to *this object*, so replacing it would
        leave the heartbeat stream reporting a run that had ended while the queue depth the
        request path updates went somewhere nobody was reading.
        """
        self.run_id = run_id
        self._tok_s = 0.0
        self._queue_depth = 0

    def observe_completion(self, result_tokens_per_s: float | None) -> None:
        """Fold one completed request's decode rate into the EWMA.

        `None` — a backend with no F-18 split — is ignored rather than counted as zero. A
        node that cannot report its rate is not a node running at zero tok/s, and telling
        the scheduler otherwise would make it look like the slowest machine in the pool.
        """
        if result_tokens_per_s is None:
            return
        self._tok_s = (
            result_tokens_per_s
            if self._tok_s == 0.0
            else _EWMA_ALPHA * result_tokens_per_s + (1 - _EWMA_ALPHA) * self._tok_s
        )

    def set_queue_depth(self, depth: int) -> None:
        """Requests admitted by the wrapper but not yet handed to a slot."""
        self._queue_depth = depth

    def next(self, state: LiveState | None = None) -> Heartbeat:
        """Advance the sequence and emit one heartbeat.

        `state` defaults to an idle-but-ready node so that the sequence can be exercised
        without an engine. That default is a *test* convenience and not a fallback for the
        live worker: a real beat always carries a freshly probed `LiveState`, because a
        heartbeat that repeats the last known occupancy is worse than a missing one — the
        scheduler cannot tell it is stale.
        """
        state = state if state is not None else LiveState(kv_frac=-1.0, state="ready")
        self._seq += 1
        hb = heartbeat_payload(
            run_id=self.run_id,
            node_id=self.node_id,
            seq=self._seq,
            state=LiveState(
                inflight=state.inflight,
                slots_total=state.slots_total,
                kv_frac=state.kv_frac,
                queue_depth=self._queue_depth,
                recent_tok_s=self._tok_s,
                state=state.state,
            ),
            recent_tokens_per_s=self._tok_s,
        )
        self._emitted.append(hb)
        return hb

    async def run(self, probe, sink, *, stop: asyncio.Event) -> None:
        """Beat every `interval_s` until `stop` is set.

        The cadence is driven off a deadline that advances by a fixed step rather than by
        `sleep(interval)` after the work, so a slow `/slots` read does not make the whole
        heartbeat stream drift late — which would show up in Aditya's logs as estimate age
        growing for no reason, and would be indistinguishable from the staleness he
        injects on purpose.
        """
        next_at = time.monotonic()
        while not stop.is_set():
            next_at += self.interval_s
            state = await probe()
            sink(self.next(state))
            delay = next_at - time.monotonic()
            if delay > 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    @property
    def emitted(self) -> list[Heartbeat]:
        return list(self._emitted)
