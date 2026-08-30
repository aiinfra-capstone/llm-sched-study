"""A pinned reference trace: the one test that would notice the generator changing.

Everything else about determinism compares this build against itself. Generate twice, get
the same bytes — true, and true of any deterministic generator, including a wrong one. It
stays true if the three RNG streams are reassigned, if numpy changes what
`Generator.choice` does with a probability vector, or if a refactor swaps two draws: the
trace changes, both sides change with it, and the assertion passes.

That matters because a trace's SHA-256 is its identity everywhere downstream. The manifest
carries it (C-6, F-20), the replay client refuses to start against a file whose hash does
not match, and the reproducibility claim is that (config, seed) regenerates a run's input
byte-for-byte *later* — on a different machine, from a later commit. A reference computed
once and written down is the only thing that can check a claim about later.

Targeted mutation testing is what surfaced this. Swapping `rng_length` and `rng_content`
in `generate` changed every byte of every trace the harness produces and the whole suite
still passed, because the streams stayed independent of each other and nothing anywhere
knew what the output was supposed to *be*.

The header is excluded on purpose: it carries `generator_git_sha`, which changes on every
commit by design. The body is the part the RNG determines, so the body is the part pinned.

**If this test fails**, the generator's output changed. That is either a bug or a
deliberate, breaking change — and if it is deliberate, the trace schema version in C-2
has to move with it, because every recorded `trace_sha256` in every existing manifest now
refers to bytes this build can no longer produce.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from dataplane.harness import gen_trace, prompts

# Written out here rather than imported from conftest: a shared default that someone
# tunes would silently invalidate the reference. Three buckets and three priorities so
# both `rng_length` draws are exercised, and unnormalised weights so the normalisation
# path is part of what is pinned.
CONFIG: dict[str, Any] = {
    "gen_seed": 20260421,
    "n_requests": 12,
    "duration_s": 30,
    "arrival": {"process": "poisson", "lambda_base": 4.0},
    "length_dist": {"buckets": ["p128_o64", "p512_o128", "p2048_o256"], "weights": [3, 2, 1]},
    "priority_mix": {"0": 0.5, "1": 0.3, "2": 0.2},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
    "vocab_size": 128000,
}

BODY_SHA256 = "58603b8ea9444caa3752e61f7ab81df73273bf2715ee3c3af80850e54700f7a2"

# The first three records, verbatim. The hash above says "something changed"; these say
# *what* changed, which is the difference between a two-minute diagnosis and an afternoon.
FIRST_THREE = [
    (
        '{"record":"req","req_id":"r000001","arrival_offset_s":0.0698,"prompt_len":128,'
        '"output_len":64,"bucket_id":"p128_o64","priority":0,"content_seed":407242708}'
    ),
    (
        '{"record":"req","req_id":"r000002","arrival_offset_s":0.0924,"prompt_len":128,'
        '"output_len":64,"bucket_id":"p128_o64","priority":0,"content_seed":979282819}'
    ),
    (
        '{"record":"req","req_id":"r000003","arrival_offset_s":0.2325,"prompt_len":512,'
        '"output_len":128,"bucket_id":"p512_o128","priority":0,"content_seed":558580823}'
    ),
]

# PCG64 seeded with the first record's content_seed. Pins the materializer to the same
# standard: the trace stores seeds, and this is what those seeds have to mean.
FIRST_PROMPT_HEAD = [5489, 126881, 60848, 67695, 30878, 98058, 99475, 62270]


def _body(tmp_path: Path) -> list[str]:
    path = tmp_path / "golden.jsonl"
    gen_trace.generate(CONFIG, path)
    return path.read_text().splitlines()[1:]


def test_the_reference_trace_still_hashes_to_the_recorded_value(tmp_path: Path) -> None:
    body = _body(tmp_path)

    assert len(body) == 12
    digest = hashlib.sha256(("\n".join(body) + "\n").encode()).hexdigest()
    assert digest == BODY_SHA256, (
        "the generator's output changed. If that was deliberate, bump TRACE_SCHEMA_VERSION: "
        "every trace_sha256 in every existing manifest now names bytes this build cannot "
        "produce."
    )


def test_the_first_records_are_exactly_what_was_recorded(tmp_path: Path) -> None:
    """Named fields, so a failure says which draw moved rather than only that one did."""
    assert _body(tmp_path)[:3] == FIRST_THREE


def test_a_content_seed_still_expands_to_the_same_prompt(tmp_path: Path) -> None:
    """The trace stores seeds, not tokens. This pins what a stored seed means."""
    ids = prompts.materialize(407242708, len(FIRST_PROMPT_HEAD), CONFIG["vocab_size"])

    assert ids == FIRST_PROMPT_HEAD
