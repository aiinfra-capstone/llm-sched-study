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
