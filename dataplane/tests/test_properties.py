"""Property-based tests: the invariants I can state but cannot enumerate.

Everything else in this suite picks inputs. That is fine for the cases I thought of and
useless for the ones I did not — and the properties that matter most here are exactly the
ones stated as universals in the source. `gen_trace`'s own docstring says three separate
RNG streams exist so "changing the length distribution does not shift the arrival process
underneath you", and that an R-sweep which re-rolls its own arrivals "cannot attribute
anything to R". That is a claim about *every* pair of length distributions, and a
hand-written example checking two of them is not evidence for it.

Hypothesis is a dev dependency only; nothing in the harness imports it.

Deadlines are off throughout: `generate` calls `git rev-parse` for the header's
`generator_git_sha`, so the first example in a run is slower than the rest by a subprocess
and a flaky deadline would fail on that alone.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from conftest import BASE_TRACE_CONFIG, SCHEMAS, read_jsonl
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from dataplane.harness import gen_trace, manifest, prompts

# Trace generation writes a file and shells out to git, so the example budget is small on
# purpose. These are slow properties; the cheap ones below run at the default budget.
TRACE = settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
CHEAP = settings(deadline=None)


# --------------------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------------------

# Inside the F-13 envelope the base config declares, so a generated bucket is admissible
# by construction and a rejection means a real bug rather than a bad draw.
prompt_lens = st.integers(min_value=1, max_value=2048)
output_lens = st.integers(min_value=1, max_value=256)
bucket_ids = st.builds(lambda p, o: f"p{p}_o{o}", prompt_lens, output_lens)


@st.composite
def length_dists(draw: Any) -> dict[str, Any]:
    """A `length_dist` block: distinct buckets with positive, unnormalised weights."""
    ids = draw(st.lists(bucket_ids, min_size=1, max_size=4, unique=True))
    weights = draw(
        st.lists(
            st.floats(min_value=0.01, max_value=100, allow_nan=False, allow_infinity=False),
            min_size=len(ids),
            max_size=len(ids),
        )
    )
    return {"buckets": ids, "weights": weights}


@st.composite
def trace_configs(draw: Any) -> dict[str, Any]:
    """A valid config. Small: these get written to disk and hashed, once per example."""
    return {
        **json.loads(json.dumps(BASE_TRACE_CONFIG)),
        "gen_seed": draw(st.integers(min_value=0, max_value=2**31 - 1)),
        "n_requests": draw(st.integers(min_value=1, max_value=30)),
        # The floor is load-bearing, so do not lower it for faster tests.
        #
        # A Poisson process over a finite window can produce nothing, and `generate`
        # refuses that config. The chance of it is exp(-lambda * duration_s) — it does not
        # depend on `n_requests` at all, since only the first interarrival draw has to
        # clear the window. Hypothesis shrinks toward boundaries, so the number that
        # matters is the worst corner this strategy can reach, `lambda_base` at 1.0 and
        # `duration_s` at its floor:
        #
        #     floor  5 -> 0.674%   (1 in 148)      <- was here; hit in Week 4
        #     floor  6 -> 0.248%   (1 in 403)
        #     floor  7 -> 0.091%   (1 in 1,097)    <- here now
        #     floor 10 -> 0.0045%  (1 in 22,026)
        #
        # 7 rather than 5 because the floor is free to raise: a trace holds
        # min(n_requests, arrivals) records and `n_requests` caps at 30, so once
        # lambda * duration clears 30 the window stops affecting how much work `generate`
        # does. Measured `generate` time is flat across floors of 5, 6, 7 and 10. 7 rather
        # than 10 because a ten-second minimum window is a number that would need its own
        # justification, and 7 leaves room for `lambda_base`'s floor to drop to ~0.5 later
        # without the rate becoming interesting again.
        #
        # `_generate` discards the residual case with `assume`, and this floor is what
        # keeps that honest. The two are one decision: the floor holds the rejection rate
        # near 1e-5 — some 50,000x below `filter_too_dense` — so the `assume` never
        # distorts the distribution being sampled and never costs measurable time, while
        # the `assume` covers the tail the floor only makes rare rather than impossible.
        # Lower this floor and the `assume` quietly starts doing real work instead.
        "duration_s": draw(st.integers(min_value=7, max_value=120)),
        "arrival": {
            "process": "poisson",
            "lambda_base": draw(st.floats(min_value=1.0, max_value=20.0)),
        },
        "length_dist": draw(length_dists()),
    }


# JSON-ish values, for the config-hash properties. Floats are excluded deliberately: the
# hash canonicalises through `json.dumps`, and NaN would make equality reflexively false
# for reasons that have nothing to do with the property under test.
json_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=12))
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)
json_objects = st.dictionaries(st.text(min_size=1, max_size=8), json_values, max_size=6)


def _shuffled(value: Any, rotate: int) -> Any:
    """Rebuild a structure with every dict's keys in a different order. Lists keep theirs."""
    if isinstance(value, dict):
        items = [(k, _shuffled(v, rotate)) for k, v in value.items()]
        cut = rotate % len(items) if items else 0
        return dict(items[cut:] + items[:cut])
    if isinstance(value, list):
        return [_shuffled(v, rotate) for v in value]
    return value


def _generate(config: dict[str, Any], path: Path) -> str:
    """`gen_trace.generate`, with the one refusal that is out of scope here discarded.

    A Poisson process over a finite window can legitimately produce nothing, and
    `generate` refuses that config rather than writing an empty trace — correctly, since a
    zero-request trace is not a workload. None of the properties below are about arrivals,
    and every one of them needs a trace to exist before it can say anything, so that case
    is discarded instead of failed.

    **Only that one.** `generate` raises `ValueError` from eleven places — a malformed
    `bucket_id`, a zero-length bucket, a bucket outside the F-13 admissible envelope, an
    unknown model, an unknown arrival process. Catching `ValueError` wholesale would turn
    every one of those into a silently discarded example, and the rejection rate would stay
    far too low for Hypothesis to warn about it. Today the envelope check happens to be
    unreachable from `trace_configs` — `bucket_ids` draws `p` in [1, 2048] and `o` in
    [1, 256], which are exactly the `admissible` bounds inherited from `BASE_TRACE_CONFIG`
    — and that coincidence is not something to build on. So everything else re-raises.
    """
    try:
        return gen_trace.generate(config, path)
    except ValueError as exc:
        assume("produced no requests" not in str(exc))
        raise


def _write(config: dict[str, Any], name: str = "t.jsonl") -> tuple[str, list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / name
        sha = _generate(config, path)
        return sha, read_jsonl(path)


# --------------------------------------------------------------------------------------
# the materializer
# --------------------------------------------------------------------------------------


@CHEAP
@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    length=st.integers(min_value=1, max_value=2048),
    vocab=st.integers(min_value=1, max_value=200_000),
)
def test_every_materialized_id_is_in_range(seed: int, length: int, vocab: int) -> None:
    """The only thing the wire cares about: `repeated uint32` cannot carry a negative."""
    ids = prompts.materialize(seed, length, vocab)

    assert len(ids) == length
    assert all(type(i) is int for i in ids), "numpy scalars do not marshal to uint32"
    assert all(0 <= i < vocab for i in ids)


@CHEAP
@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    length=st.integers(min_value=1, max_value=512),
    vocab=st.integers(min_value=1, max_value=200_000),
)
def test_materializing_is_a_function_of_its_arguments(seed: int, length: int, vocab: int) -> None:
    """Called twice, same answer — the generator and the replay client both rely on this."""
    assert prompts.materialize(seed, length, vocab) == prompts.materialize(seed, length, vocab)


@CHEAP
@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    length=st.integers(min_value=1, max_value=256),
    vocab=st.integers(min_value=2, max_value=200_000),
    extra=st.integers(min_value=1, max_value=256),
)
def test_a_longer_prompt_extends_a_shorter_one(
    seed: int, length: int, vocab: int, extra: int
) -> None:
    """Same seed, more tokens: the shorter prompt is a prefix of the longer one.

    Not required by any contract, but it is what a single ordered draw off one PCG64
    stream gives you, and it is the cheapest possible check that nobody has quietly
    swapped in a per-length reseed — which would make prompt content covary with
    prompt_len and confound every length-versus-latency figure in the study.
    """
    short = prompts.materialize(seed, length, vocab)
    long = prompts.materialize(seed, length + extra, vocab)

    assert long[:length] == short


@CHEAP
@given(
    st.lists(
        st.fixed_dictionaries(
            {
                "req_id": st.text(min_size=1, max_size=8),
                "content_seed": st.integers(min_value=0, max_value=2**31 - 1),
                "prompt_len": st.integers(min_value=1, max_value=64),
            }
        ),
        min_size=1,
        max_size=8,
        unique_by=lambda r: r["req_id"],
    ),
    st.integers(min_value=1, max_value=200_000),
)
def test_the_batch_agrees_with_one_at_a_time(records: list[dict], vocab: int) -> None:
    """`materialize_all` is a pre-t0 optimisation; it must not be a second implementation."""
    batch = prompts.materialize_all(records, vocab)

    assert batch == {
        r["req_id"]: prompts.materialize(r["content_seed"], r["prompt_len"], vocab) for r in records
    }


@CHEAP
@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    length=st.integers(max_value=0),
    vocab=st.integers(min_value=1, max_value=200_000),
)
def test_no_prompt_length_below_one_is_ever_accepted(seed: int, length: int, vocab: int) -> None:
    with pytest.raises(ValueError, match="prompt_len must be >= 1"):
        prompts.materialize(seed, length, vocab)


# --------------------------------------------------------------------------------------
# the config hash
# --------------------------------------------------------------------------------------


@CHEAP
@given(config=json_objects, rotate=st.integers(min_value=1, max_value=6))
def test_the_config_hash_ignores_key_order_at_every_depth(config: dict, rotate: int) -> None:
    """Two people writing the same config by hand must get the same `config_hash`.

    `json.dumps(sort_keys=True)` sorts recursively, so this holds for nested objects too —
    which is the part worth asserting, since the run config nests `arrival`, `length_dist`
    and `admissible` blocks.
    """
    assert manifest.config_hash(config) == manifest.config_hash(_shuffled(config, rotate))


@CHEAP
@given(config=json_objects, values=st.lists(json_scalars, min_size=2, max_size=4, unique=True))
def test_list_order_still_changes_the_config_hash(config: dict, values: list) -> None:
    """Reordering `length_dist.buckets` changes the trace, so it must change the hash."""
    a = {**config, "buckets": list(values)}
    b = {**config, "buckets": list(reversed(values))}

    assert manifest.config_hash(a) != manifest.config_hash(b)


# --------------------------------------------------------------------------------------
# the validity block
# --------------------------------------------------------------------------------------

counts = st.integers(min_value=0, max_value=50)


@CHEAP
@given(
    max_lag=st.floats(min_value=0, max_value=1e4, allow_nan=False),
    violations=counts,
    dropped=counts,
    gaps=counts,
    restarts=counts,
    colocated=counts,
)
def test_a_run_is_valid_exactly_when_it_has_no_reasons_to_be_rejected(
    max_lag: float, violations: int, dropped: int, gaps: int, restarts: int, colocated: int
) -> None:
    """`valid` and `reasons()` are two renderings of one decision and must never disagree.

    They are computed by separate code paths — one a boolean conjunction, the other a list
    of `if`s — so this is the assertion that stops a fifth invalidating condition from
    being added to one and forgotten in the other.
    """
    v = manifest.Validity(max_lag, violations, dropped, gaps, restarts, colocated)

    assert v.valid == (v.reasons() == [])
    assert v.to_dict()["valid"] == v.valid


@CHEAP
@given(gaps=counts, max_lag=st.floats(min_value=0, max_value=1e4, allow_nan=False))
def test_heartbeat_gaps_alone_never_invalidate_a_run(gaps: int, max_lag: float) -> None:
    """A missed heartbeat degrades the scheduler's estimate — which is what H3 studies."""
    v = manifest.Validity(max_send_lag_ms=max_lag, heartbeat_gaps=gaps)

    assert v.valid
    assert v.reasons() == []


# --------------------------------------------------------------------------------------
# the generator
# --------------------------------------------------------------------------------------


@TRACE
@given(config=trace_configs())
def test_a_trace_is_a_pure_function_of_its_config(config: dict) -> None:
    """Byte-identical regeneration — the discipline the whole reproducibility story rests on."""
    sha_a, records_a = _write(config)
    sha_b, records_b = _write(config, name="again.jsonl")

    assert sha_a == sha_b
    assert records_a == records_b


@TRACE
@given(config=trace_configs())
def test_every_generated_trace_is_internally_consistent(config: dict) -> None:
    """The header describes the body: count, ordering, dense ids, and the bucket lengths."""
    _, (header, *body) = _write(config)

    assert header["n_requests"] == len(body)
    assert len(body) <= config["n_requests"], "n_requests is a cap, never a floor"
    offsets = [r["arrival_offset_s"] for r in body]
    assert offsets == sorted(offsets)
    assert all(0 <= o <= config["duration_s"] for o in offsets)
    assert [r["req_id"] for r in body] == [f"r{i:06d}" for i in range(1, len(body) + 1)]
    for r in body:
        assert r["bucket_id"] in config["length_dist"]["buckets"]
        assert r["bucket_id"] == f"p{r['prompt_len']}_o{r['output_len']}"
        assert str(r["priority"]) in config["priority_mix"]
        assert 0 <= r["content_seed"] < 2**31 - 1


@TRACE
@given(config=trace_configs(), other=length_dists())
def test_changing_the_length_mix_does_not_move_the_arrivals(config: dict, other: dict) -> None:
    """The reason `SeedSequence(seed).spawn(3)` exists, stated as a universal.

    An R-sweep varies the length mix and nothing else. If the arrival stream were shared,
    each point of that sweep would sit on a different realisation of the arrival process,
    and the difference between two points would no longer be attributable to R. Same for
    `content_seed`: prompt *content* must not covary with the length distribution either.
    """
    assume(other["buckets"] != config["length_dist"]["buckets"])

    _, (_, *a) = _write(config)
    _, (_, *b) = _write({**config, "length_dist": other}, name="other.jsonl")

    assert len(a) == len(b)
    assert [r["arrival_offset_s"] for r in a] == [r["arrival_offset_s"] for r in b]
    assert [r["content_seed"] for r in a] == [r["content_seed"] for r in b]


@TRACE
@given(config=trace_configs(), scale=st.floats(min_value=0.01, max_value=1000))
def test_weights_are_scale_invariant(config: dict, scale: float) -> None:
    """`weights` are normalised internally, so a config may state them any way it likes.

    The comparison is on the drawn records, not on the trace's SHA-256: the header embeds
    `length_dist` verbatim, so `[6, 4]` and `[0.6, 0.4]` are genuinely different files that
    describe the same experiment. Scale invariance is a claim about the draws.
    """
    original = config["length_dist"]["weights"]
    total = sum(original)
    # Float scaling is not exact. Where normalisation lands on a different double the
    # draws may legitimately differ, so those examples are skipped rather than asserted
    # away — the property is about normalisation, not about IEEE arithmetic.
    assume(all((w * scale) / (total * scale) == w / total for w in original))

    scaled = {**config["length_dist"], "weights": [w * scale for w in original]}
    _, (_, *a) = _write(config)
    _, (_, *b) = _write({**config, "length_dist": scaled}, name="scaled.jsonl")

    assert a == b


@TRACE
@given(config=trace_configs())
def test_a_generated_trace_round_trips_through_its_own_loader(config: dict) -> None:
    """`load` verifies the hash it is given; the hash `generate` returns must be that one."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "t.jsonl"
        sha = _generate(config, path)

        header, body = gen_trace.load(path, expect_sha256=sha)

        assert header["record"] == "header"
        assert len(body) == header["n_requests"]


@TRACE
@given(config=trace_configs())
def test_every_generated_record_conforms_to_c2(config: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMAS / "trace.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)

    _, records = _write(config)

    for record in records:
        validator.validate(record)


@TRACE
@given(
    config=trace_configs(),
    bad_prompt=st.integers(min_value=2049, max_value=100_000),
)
def test_no_bucket_outside_the_admissible_envelope_is_ever_written(
    config: dict, bad_prompt: int
) -> None:
    """F-13 is enforced at generation time, not discovered at replay time."""
    config["length_dist"] = {"buckets": [f"p{bad_prompt}_o64"], "weights": [1.0]}

    with pytest.raises(ValueError, match="admissible envelope"):
        _write(config)
