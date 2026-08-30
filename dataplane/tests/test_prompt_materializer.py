"""The materializer is a shared definition, not an implementation detail.

The generator, the replay client, and the worker must all agree on how a `content_seed`
becomes a token sequence — the trace stores the seed and nothing else. If they drift,
prompt lengths stop matching the bucket the record claims, and every prefill measurement
in the study is quietly wrong about what it measured.
"""

from __future__ import annotations

import pytest

from dataplane.harness.prompts import materialize, materialize_all


def test_same_seed_same_tokens() -> None:
    assert materialize(1234, 64, 128000) == materialize(1234, 64, 128000)


def test_different_seeds_differ() -> None:
    assert materialize(1, 64, 128000) != materialize(2, 64, 128000)


def test_length_is_exactly_prompt_len() -> None:
    """The bucket id claims the prompt length; the materializer has to honour it exactly."""
    for n in (1, 17, 128, 2048):
        assert len(materialize(99, n, 128000)) == n


def test_ids_stay_inside_the_vocabulary() -> None:
    tokens = materialize(7, 4096, 1000)
    assert min(tokens) >= 0 and max(tokens) < 1000


def test_prefix_is_stable_as_length_grows() -> None:
    """A longer prompt from the same seed extends the shorter one rather than re-rolling it.

    This is what makes a length sweep a length sweep: the p2048 prompt shares its prefix
    with the p512 prompt at the same seed, so prefill cost varies with length and not with
    content. It also means prefix caching, if ever enabled, sees what we think it sees.
    """
    assert materialize(42, 2048, 128000)[:512] == materialize(42, 512, 128000)


def test_materialize_all_keys_by_req_id() -> None:
    records = [
        {"req_id": "r000001", "content_seed": 11, "prompt_len": 8},
        {"req_id": "r000002", "content_seed": 22, "prompt_len": 16},
    ]
    out = materialize_all(records, vocab_size=1000)
    assert set(out) == {"r000001", "r000002"}
    assert out["r000002"] == materialize(22, 16, 1000)


@pytest.mark.parametrize(("prompt_len", "vocab"), [(0, 1000), (-1, 1000), (8, 0)])
def test_invalid_arguments_raise(prompt_len: int, vocab: int) -> None:
    with pytest.raises(ValueError):
        materialize(1, prompt_len, vocab)


def test_ids_are_plain_python_ints() -> None:
    """`prompt_token_ids` is a repeated uint32 on the wire. A numpy int64 does not marshal
    into that, and the failure would appear as a gRPC type error inside the timing loop —
    the worst possible place for it."""
    tokens = materialize(5, 8, 128000)
    assert all(type(t) is int for t in tokens)


def test_a_one_token_vocabulary_degenerates_cleanly() -> None:
    """Not a realistic model; a boundary. `vocab_size=1` means every id is 0, and the
    materializer should say so rather than raise."""
    assert materialize(1, 4, 1) == [0, 0, 0, 0]


def test_the_full_admissible_prompt_length_is_supported() -> None:
    """F-13's envelope tops out at 2048 prompt tokens for the staged run sets. The
    materializer has to reach it without special-casing."""
    assert len(materialize(3, 2048, 128000)) == 2048


def test_seeds_at_the_edges_of_the_generated_range_work() -> None:
    """`content_seed` is drawn from [0, 2**31 - 1). Both ends must expand."""
    for seed in (0, 2**31 - 2):
        assert len(materialize(seed, 16, 128000)) == 16


def test_materialization_agrees_across_processes() -> None:
    """The generator runs on my laptop, the replay client runs on the load host, and the
    worker sees whatever the client sent. All three have to agree on what a `content_seed`
    means, and "agree" has to survive a fresh interpreter — which is exactly why this uses
    `numpy.random.default_rng` (PCG64, documented and versioned) rather than `random`,
    whose stream is a CPython implementation detail."""
    import subprocess
    import sys

    expected = materialize(20260421, 32, 128000)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from dataplane.harness.prompts import materialize;"
                "print(materialize(20260421, 32, 128000))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert eval(proc.stdout.strip()) == expected


def test_materialize_all_on_an_empty_trace() -> None:
    """An empty body is refused by the generator, but the replay client should not be the
    thing that crashes if one ever reaches it."""
    assert materialize_all([], vocab_size=1000) == {}


def test_materialize_all_does_not_mutate_the_records() -> None:
    """The same record list is written to the log afterwards. Materialising must not
    stash token ids on it — the trace stores seeds, and only seeds."""
    records = [{"req_id": "r000001", "content_seed": 11, "prompt_len": 8}]
    before = [dict(r) for r in records]
    materialize_all(records, vocab_size=1000)
    assert records == before


def test_materialize_all_matches_one_at_a_time() -> None:
    """The batch path is a convenience, not a second definition."""
    records = [
        {"req_id": f"r{i:06d}", "content_seed": i * 7919, "prompt_len": 4 + i} for i in range(1, 6)
    ]
    batch = materialize_all(records, vocab_size=128000)
    for r in records:
        assert batch[r["req_id"]] == materialize(r["content_seed"], r["prompt_len"], 128000)


def test_a_different_vocabulary_changes_the_tokens() -> None:
    """Which is why `vocab_size` travels in the trace header rather than being a constant
    in the client: a Mistral run set and a Llama run set materialize differently from the
    same seed, and the header is what says which one this is."""
    assert materialize(9, 32, 128000) != materialize(9, 32, 32768)
