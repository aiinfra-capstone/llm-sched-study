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


def test_regeneration_is_byte_identical_across_processes(tmp_path: Path) -> None:
    """Byte-stability inside one process can come from a warm cache or a lucky ordering.

    The claim that matters is stronger: the same (config, seed) on a fresh interpreter —
    and, by extension, on another node — writes the same bytes. That is what makes the
    trace's SHA-256 an identity rather than a per-process checksum.
    """
    import subprocess
    import sys

    script = (
        "import json,sys;"
        "from dataplane.harness import gen_trace;"
        "print(gen_trace.generate(json.loads(sys.argv[1]), sys.argv[2]))"
    )
    out = []
    for name in ("a.jsonl", "b.jsonl"):
        proc = subprocess.run(
            [sys.executable, "-c", script, json.dumps(CONFIG), str(tmp_path / name)],
            capture_output=True,
            text=True,
            check=True,
        )
        out.append(proc.stdout.strip())

    assert out[0] == out[1]
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()


def test_the_generator_never_reads_the_wall_clock(tmp_path: Path, monkeypatch) -> None:
    """A trace is a pure function of (config, seed). One `time.time()` anywhere in it —
    a timestamp in the header, a jitter term — and byte-identical regeneration is gone.

    `time.monotonic` is deliberately left alone: `subprocess.run(timeout=...)` uses it to
    resolve the generator's own git sha, and that value never reaches the sampling loop.
    """
    import time as time_mod

    def forbidden(*_args, **_kwargs):
        raise AssertionError("gen_trace read the wall clock")

    monkeypatch.setattr(time_mod, "time", forbidden)
    monkeypatch.setattr(time_mod, "time_ns", forbidden)

    gen_trace.generate(CONFIG, tmp_path / "t.jsonl")


def test_a_different_seed_produces_a_different_trace(tmp_path: Path) -> None:
    """The other half of determinism: same seed, same bytes; different seed, different
    bytes. Without this, a byte-stable generator that ignores its seed would pass."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    sha_a = gen_trace.generate(CONFIG, a)
    sha_b = gen_trace.generate({**CONFIG, "gen_seed": CONFIG["gen_seed"] + 1}, b)
    assert sha_a != sha_b


def test_shortening_a_trace_yields_a_prefix_of_the_longer_one(tmp_path: Path) -> None:
    """`n_requests` caps a stream that is otherwise identical, so a short trace is the
    head of the long one. This is what makes a quick 50-request smoke run a genuine
    rehearsal of the front of the real trace rather than a different workload."""
    long_path, short_path = tmp_path / "long.jsonl", tmp_path / "short.jsonl"
    gen_trace.generate({**CONFIG, "n_requests": 200}, long_path)
    gen_trace.generate({**CONFIG, "n_requests": 40}, short_path)

    def offsets(p: Path) -> list[float]:
        return [json.loads(line)["arrival_offset_s"] for line in p.read_text().splitlines()[1:]]

    short = offsets(short_path)
    assert offsets(long_path)[: len(short)] == short


def test_header_field_order_matches_the_committed_sample(tmp_path: Path) -> None:
    """Field order is part of the format here, because the format is bytes.

    `contracts/examples/trace.sample.jsonl` is the reference. If the generator reorders a
    key, every previously recorded SHA-256 stops matching a regenerated file — silently,
    since the JSON still parses to the same object.
    """
    path = tmp_path / "t.jsonl"
    gen_trace.generate(CONFIG, path)
    mine = list(json.loads(path.read_text().splitlines()[0]))

    sample = Path(__file__).resolve().parents[2] / "contracts/examples/trace.sample.jsonl"
    theirs = list(json.loads(sample.read_text().splitlines()[0]))
    assert mine == theirs


def test_request_field_order_matches_the_committed_sample(tmp_path: Path) -> None:
    """Same argument, for the line type there are thousands of."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(CONFIG, path)
    mine = list(json.loads(path.read_text().splitlines()[1]))

    sample = Path(__file__).resolve().parents[2] / "contracts/examples/trace.sample.jsonl"
    theirs = list(json.loads(sample.read_text().splitlines()[1]))
    assert mine == theirs


def test_the_file_has_no_incidental_whitespace(tmp_path: Path) -> None:
    """Compact separators, one trailing newline, no spaces after colons. Every one of
    those is a byte, and bytes are the identity."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(CONFIG, path)
    blob = path.read_bytes()
    assert blob.endswith(b"\n") and not blob.endswith(b"\n\n")
    assert b'", "' not in blob and b'": ' not in blob


def test_the_header_records_the_generator_commit(tmp_path: Path) -> None:
    """F-20: a trace has to be traceable back to the code that produced it. An
    unversioned checkout says "unknown" rather than refusing — it just cannot claim
    provenance."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(CONFIG, path)
    header = json.loads(path.read_text().splitlines()[0])
    assert header["generator_git_sha"]


def test_vocab_size_does_not_perturb_the_draws(tmp_path: Path) -> None:
    """`vocab_size` is consumed by the materializer, never by the generator's sampling.

    So switching a run set from Llama-3 to Mistral changes the prompt CONTENT and nothing
    about when requests arrive or how long they are — which is what makes the two run sets
    a replication rather than two different experiments.
    """
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    gen_trace.generate(CONFIG, a)
    gen_trace.generate({**CONFIG, "vocab_size": 32768}, b)

    def drop_vocab(p: Path) -> list[dict]:
        return [json.loads(line) for line in p.read_text().splitlines()[1:]]

    assert drop_vocab(a) == drop_vocab(b)
