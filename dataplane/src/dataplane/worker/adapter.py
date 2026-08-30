"""Engine-agnostic record types for the worker wrapper (F-10, F-18).

Under F-9 the pool runs one engine, so there is one adapter — but these types are not that
adapter's private business. `ServiceResult` and `LiveState` cross the seam into the C-4
worker log and the C-1 heartbeat, and the F-9b engine-gap probe produces them from vLLM,
which is not a llama.cpp client at all. So the *shape of a measurement* lives here and the
code that talks HTTP to `llama-server` lives in `llamacpp.py`.

That split is not anticipatory generality. It is the difference between what the study
records about a request and how one particular engine is asked.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ENGINE_STATES", "LiveState", "ServiceResult", "f18_status"]

# The three values `Heartbeat.engine_state` may take (C-1). "warming" is not cosmetic: a
# node that has loaded its weights but not yet run a token still pays CUDA context setup
# and kernel JIT on its first request, and a cold node reporting "ready" gets routed to
# during exactly the window where its service time is unrepresentative. That contaminates
# the tail latencies MPR-1 is measuring, so the state has to be sayable.
#
# "degraded" is what a node reports when it is up but cannot describe itself — /slots
# unreachable, say. It is deliberately distinct from being absent: a scheduler should be
# able to tell "this node is gone" from "this node is serving but I am flying blind".
ENGINE_STATES = ("ready", "warming", "degraded")


@dataclass(frozen=True)
class ServiceResult:
    """One request's worth of engine-local truth.

    `prefill_ns` / `decode_ns` are `None` when the backend did not emit a `timings` block.
    Every consumer must branch on that rather than defaulting to zero: a zero prefill is a
    measurement, `None` is the absence of one.
    """

    status: str
    service_ns: int
    prompt_tokens: int
    output_tokens: int
    prefill_ns: int | None = None
    decode_ns: int | None = None
    cached_tokens: int = 0
    slot_id: int = -1
    error: str = ""
    """What went wrong, when something did. Empty on success.

    C-4's status enum has four values, which is the right granularity for the *analysis* —
    a policy comparison does not care which exception class fired. It is the wrong
    granularity for debugging an instrument: a 7% `engine_error` rate on a CPU node is
    either a real property of that node class or a bug in my client, and the four-value
    enum cannot tell those apart. So the detail is kept here and in the calibration
    observations, and is deliberately *not* written into the C-4 worker log, whose schema
    is closed and whose consumers do not need it.
    """

    @property
    def f18_status(self) -> str:
        """`"full"` when this result carries both halves of the F-18 split."""
        return "full" if self.prefill_ns is not None and self.decode_ns is not None else "partial"

    @property
    def residual_ns(self) -> int | None:
        """`service_ns` minus the two stages the engine accounted for.

        In-engine queueing plus HTTP framing. Reported as one number because that is all I
        can honestly say about it — decomposing it further would be inventing stages.
        """
        if self.prefill_ns is None or self.decode_ns is None:
            return None
        return self.service_ns - (self.prefill_ns + self.decode_ns)

    @property
    def decode_tokens_per_s(self) -> float | None:
        """Decode throughput — the quantity the cost model and MPR-1 are actually about.

        Deliberately *not* `output_tokens / service_ns`: that ratio moves with prompt
        length through prefill, so a node would appear to slow down when the workload got
        longer prompts. tok/s here means decode tok/s, and everything downstream — tau, the
        variance envelope, the heterogeneity ratio R — inherits that definition.
        """
        if self.decode_ns is None or self.decode_ns <= 0:
            return None
        return self.output_tokens / (self.decode_ns / 1e9)


@dataclass(frozen=True)
class LiveState:
    """F-10 heartbeat payload — the five values a policy's node view is built from.

    `queue_depth` and `inflight` are separate because they are what distinguishes the two
    queue-aware policies from the two queue-blind ones: JSQ reads depth, and F-4's
    batch-awareness reads the composition of what is already admitted. On llama.cpp
    `inflight` is the number of busy slots and `queue_depth` is what the wrapper is
    holding behind them, because the engine admits exactly `--parallel` at a time.

    `kv_frac` is **slot occupancy, not paged-KV occupancy** — llama.cpp does not expose the
    latter. C-4 names the column `kv_occupancy_at_admission` and asks for the fraction of
    `--parallel` slots in use, which is what this is. The name is the contract's; the
    meaning is written here so nobody reads that column as a KV-cache figure in Week 6.
    """

    inflight: int = 0
    slots_total: int = 0
    kv_frac: float = -1.0
    queue_depth: int = 0
    recent_tok_s: float = 0.0
    state: str = "ready"

    def __post_init__(self) -> None:
        if self.state not in ENGINE_STATES:
            raise ValueError(f"engine_state must be one of {ENGINE_STATES}, got {self.state!r}")

    @classmethod
    def unavailable(cls) -> LiveState:
        """A server that is up but cannot describe itself (`--no-slots`, or /slots down).

        `kv_frac` is -1.0 rather than 0.0 because 0.0 is a real reading that means "idle",
        and a scheduler told a saturated node is idle will pile onto it. `degraded` is the
        state that says the number is missing rather than good.
        """
        return cls(kv_frac=-1.0, state="degraded")


def f18_status(result: ServiceResult) -> str:
    """`"full"` when the backend gave both halves of the split, `"partial"` otherwise.

    Stamped into the manifest per run. It is a property of the *backend build*, so it is
    probed once at startup rather than recomputed per request — but it is derived from a
    real result, because the only trustworthy answer to "does this build emit timings" is
    a response from this build that did.
    """
    return result.f18_status
