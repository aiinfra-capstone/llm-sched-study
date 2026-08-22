"""Prompt materializer — `(content_seed, prompt_len, vocab_size)` -> exact token ids.

Token IDs are never stored in the trace (C-2); only `content_seed` is. This module is
the single definition of how that seed becomes a token sequence, and it is imported by
*both* the trace generator and the replay client on purpose. Two implementations that
agree today are two implementations that drift in Week 3.

Determinism here is `numpy.random.default_rng`, which is a documented, versioned bit
generator (PCG64) — not `random`, whose stream is a CPython implementation detail.

`reserved_ids_excluded` in the trace header is about `vocab_size`, not about a filter
applied here: Llama-3 keeps its special tokens at the TOP of the vocabulary
(128000-128255 of 128256), so a `vocab_size` set to 128000 already excludes them and
sampling the whole `[0, vocab_size)` range is safe. Set `vocab_size` below the special
block for whatever model is pinned, and this stays true.
"""

from __future__ import annotations

import numpy as np

__all__ = ["materialize", "materialize_all"]


def materialize(content_seed: int, prompt_len: int, vocab_size: int) -> list[int]:
    """Deterministically expand one trace record into its prompt token ids.

    Pure: no clock, no global RNG state, no I/O. Callable from the timing loop in
    principle — but don't, see `materialize_all`.
    """
    if prompt_len < 1:
        raise ValueError(f"prompt_len must be >= 1, got {prompt_len}")
    if vocab_size < 1:
        raise ValueError(f"vocab_size must be >= 1, got {vocab_size}")

    rng = np.random.default_rng(content_seed)
    return rng.integers(0, vocab_size, size=prompt_len, dtype=np.int64).tolist()


def materialize_all(records: list[dict], vocab_size: int) -> dict[str, list[int]]:
    """Materialize every prompt in a trace, up front, before t0.

    The replay client calls this once and never allocates a prompt inside the timing
    loop. Tokenising under load is the most common cause of send-lag drift at high
    lambda, and send-lag drift invalidates the run rather than degrading it.
    """
    return {
        r["req_id"]: materialize(r["content_seed"], r["prompt_len"], vocab_size) for r in records
    }
