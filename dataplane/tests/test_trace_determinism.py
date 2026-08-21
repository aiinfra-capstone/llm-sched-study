"""The Week-1 determinism test: a trace must regenerate byte-for-byte.

This is a TEST, not an assumption. The trace's SHA-256 is the identity used
everywhere downstream — it is written into every run manifest (C-6) and verified
by the replay client before t0. If regeneration is not byte-stable, that identity
is meaningless and F-17 (an identical trace replayable across policies and across
the hardware/simulator boundary) does not hold.

Float formatting is the usual culprit. `arrival_offset_s` is fixed at 4 decimal
places in the schema for exactly this reason.

Currently skipped: unskip the moment `gen_trace` exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

CONFIG = {
    "gen_seed": 20260421,
    "n_requests": 400,
    "duration_s": 600,
    "arrival": {
        "process": "mmpp",
        "lambda_base": 3.5,
        "burst_lambda": 14.0,
        "burst_mean_s": 8.0,
        "quiet_mean_s": 45.0,
    },
    "length_dist": {
        "buckets": ["p128_o64", "p512_o128", "p2048_o256"],
        "weights": [0.5, 0.35, 0.15],
    },
    "priority_mix": {"0": 0.7, "1": 0.3},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
    "vocab_size": 128000,
}

gen_trace = pytest.importorskip(
    "dataplane.harness.gen_trace",
    reason="gen_trace not implemented yet — Week-1 deliverable",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    """Same seed, same parameters, same bytes. No wall-clock reads anywhere."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    gen_trace.generate(CONFIG, a)
    gen_trace.generate(CONFIG, b)
    assert _sha256(a) == _sha256(b), "trace regeneration is not byte-stable"


def test_arrival_offsets_have_exactly_four_decimals(tmp_path: Path) -> None:
    """The schema fixes 4 decimal places; drift here breaks byte-stability."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(CONFIG, path)

    for line in path.read_text().splitlines()[1:]:  # skip the header record
        raw = line.split('"arrival_offset_s":')[1].split(",")[0]
        assert len(raw.split(".")[1]) == 4, f"bad offset formatting: {raw}"


def test_length_distribution_does_not_perturb_arrivals(tmp_path: Path) -> None:
    """Separate RNG streams: changing lengths must not shift arrivals underneath.

    This is why the generator spawns three independent streams from one
    SeedSequence rather than drawing everything from a single stream.
    """
    baseline = tmp_path / "base.jsonl"
    gen_trace.generate(CONFIG, baseline)

    altered = {**CONFIG, "length_dist": {**CONFIG["length_dist"], "weights": [0.2, 0.2, 0.6]}}
    other = tmp_path / "other.jsonl"
    gen_trace.generate(altered, other)

    def offsets(p: Path) -> list[float]:
        return [json.loads(line)["arrival_offset_s"] for line in p.read_text().splitlines()[1:]]

    assert offsets(baseline) == offsets(other)
