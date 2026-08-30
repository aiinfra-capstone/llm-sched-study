"""F-16 — the arrival process, and every way a config can be wrong about it.

`test_trace_determinism.py` guards that a trace regenerates byte-for-byte. This file
guards that the trace was the right one to begin with: that the arrival process is the
one the config asked for, that both bounds on the request count actually bind, and that a
config which contradicts itself is refused at generation time rather than discovered as a
strange figure in Week 5.

The generator is the only place in the harness where a mistake is invisible. A trace with
a slightly wrong lambda still validates, still hashes, still replays, and still produces
plausible latency numbers — it just answers a different question than the one in the
manifest.
"""

from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest
from conftest import assert_conforms

from dataplane.harness import gen_trace

MMPP = {
    "process": "mmpp",
    "lambda_base": 3.5,
    "burst_lambda": 14.0,
    "burst_mean_s": 8.0,
    "quiet_mean_s": 45.0,
}


def _body(config: dict, path: Path) -> list[dict]:
    gen_trace.generate(config, path)
    _, body = gen_trace.load(path)
    return body


# --------------------------------------------------------------------------------------
# The two bounds
# --------------------------------------------------------------------------------------


def test_n_requests_is_a_cap_not_a_target(trace_config, tmp_path: Path) -> None:
    """`n_requests` binds when the duration is generous."""
    body = _body(trace_config(n_requests=9, duration_s=10_000), tmp_path / "t.jsonl")
    assert len(body) == 9


def test_duration_binds_when_it_is_the_tighter_bound(trace_config, tmp_path: Path) -> None:
    """And when it binds, no request may sit past the end of the window."""
    config = trace_config(n_requests=100_000, duration_s=3.0)
    body = _body(config, tmp_path / "t.jsonl")
    assert 0 < len(body) < 100_000
    assert max(r["arrival_offset_s"] for r in body) <= config["duration_s"]


def test_header_count_matches_the_body_it_describes(trace_config, tmp_path: Path) -> None:
    """The header records what was WRITTEN, not what was asked for.

    `load()` cross-checks the two, so a header that lied about its own body would make
    every trace unreadable rather than subtly wrong — which is the right failure.
    """
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(n_requests=100_000, duration_s=2.0), path)
    header, body = gen_trace.load(path)
    assert header["n_requests"] == len(body)


def test_offsets_are_sorted_and_non_negative(trace_config, tmp_path: Path) -> None:
    """The replay client sleeps forward through this list and never sorts it itself."""
    body = _body(trace_config(arrival=MMPP, n_requests=300, duration_s=600), tmp_path / "t.jsonl")
    offsets = [r["arrival_offset_s"] for r in body]
    assert offsets == sorted(offsets)
    assert offsets[0] >= 0.0


def test_req_ids_are_dense_and_ordered(trace_config, tmp_path: Path) -> None:
    """`r000001`.. with no gaps. The id is the join key in every downstream log."""
    body = _body(trace_config(n_requests=15, duration_s=10_000), tmp_path / "t.jsonl")
    assert [r["req_id"] for r in body] == [f"r{i:06d}" for i in range(1, len(body) + 1)]


# --------------------------------------------------------------------------------------
# The processes themselves
# --------------------------------------------------------------------------------------


def test_poisson_rate_is_roughly_the_configured_lambda(trace_config, tmp_path: Path) -> None:
    """Not a distributional test — a wiring test.

    It catches the class of mistake where lambda is read as a mean interarrival, or the
    rate is passed straight to `exponential` instead of its reciprocal. That inverts the
    load axis of the whole study, and nothing downstream would notice.
    """
    config = trace_config(
        arrival={"process": "poisson", "lambda_base": 20.0},
        n_requests=100_000,
        duration_s=100.0,
    )
    body = _body(config, tmp_path / "t.jsonl")
    observed = len(body) / config["duration_s"]
    assert 16.0 < observed < 24.0, f"observed rate {observed:.1f}/s for lambda=20"


def test_mmpp_is_burstier_than_poisson_at_the_same_base_rate(trace_config, tmp_path: Path) -> None:
    """The burst state is the point of MMPP. If the switch never fires, the process is
    just Poisson with extra config, and H1's load-band story loses its high-load end."""
    common = {"n_requests": 100_000, "duration_s": 1200.0}
    poisson = _body(
        trace_config(arrival={"process": "poisson", "lambda_base": 3.5}, **common),
        tmp_path / "p.jsonl",
    )
    mmpp = _body(trace_config(arrival=MMPP, **common), tmp_path / "m.jsonl")

    def gaps(body: list[dict]) -> list[float]:
        offs = [r["arrival_offset_s"] for r in body]
        return [b - a for a, b in pairwise(offs)]

    def cv(xs: list[float]) -> float:
        mean = sum(xs) / len(xs)
        var = sum((x - mean) ** 2 for x in xs) / len(xs)
        return (var**0.5) / mean

    assert cv(gaps(mmpp)) > cv(gaps(poisson)), "MMPP produced no burst structure"


def test_mmpp_respects_the_duration_bound(trace_config, tmp_path: Path) -> None:
    """The state-switch branch advances `t` to the boundary and re-draws. That path has
    its own `break`, and it is the one that can run past the window if it is forgotten."""
    config = trace_config(arrival=MMPP, n_requests=100_000, duration_s=30.0)
    body = _body(config, tmp_path / "t.jsonl")
    assert max(r["arrival_offset_s"] for r in body) <= 30.0


def test_unknown_arrival_process_is_refused(trace_config, tmp_path: Path) -> None:
    """§12.2: reject loudly rather than defaulting to Poisson and producing a trace whose
    header claims a process it did not use."""
    config = trace_config(arrival={"process": "weibull", "lambda_base": 3.0})
    with pytest.raises(ValueError, match="unknown arrival process"):
        gen_trace.generate(config, tmp_path / "t.jsonl")


def test_a_process_that_produces_nothing_is_an_error(trace_config, tmp_path: Path) -> None:
    """An empty trace hashes and validates. It just measures nothing, so it is refused."""
    config = trace_config(duration_s=1e-6, n_requests=10)
    with pytest.raises(ValueError, match="produced no requests"):
        gen_trace.generate(config, tmp_path / "t.jsonl")


# --------------------------------------------------------------------------------------
# Lengths, buckets, priorities
# --------------------------------------------------------------------------------------


def test_bucket_id_is_the_length_pair(trace_config, tmp_path: Path) -> None:
    """There is no second lookup table: `p512_o128` IS 512 and 128. This is what makes a
    bucket label safe to group by in the figures."""
    body = _body(
        trace_config(
            length_dist={"buckets": ["p128_o64", "p512_o128", "p2048_o256"], "weights": [1, 1, 1]},
            n_requests=200,
            duration_s=10_000,
        ),
        tmp_path / "t.jsonl",
    )
    for r in body:
        p_len, o_len = r["bucket_id"][1:].split("_o")
        assert r["prompt_len"] == int(p_len)
        assert r["output_len"] == int(o_len)


def test_a_bucket_outside_the_admissible_envelope_is_refused(trace_config, tmp_path: Path) -> None:
    """F-13: the envelope is what every pool node can serve inside the timeout ceiling.

    A trace carrying a bucket outside it guarantees categorical failures on the slow node,
    which would be read as a scheduling result rather than as a bad trace.
    """
    config = trace_config(length_dist={"buckets": ["p4096_o64"], "weights": [1.0]})
    with pytest.raises(ValueError, match="admissible envelope"):
        gen_trace.generate(config, tmp_path / "t.jsonl")


def test_output_length_over_the_envelope_is_refused_too(trace_config, tmp_path: Path) -> None:
    """Both halves of the envelope are checked, not just the prompt."""
    config = trace_config(length_dist={"buckets": ["p128_o512"], "weights": [1.0]})
    with pytest.raises(ValueError, match="admissible envelope"):
        gen_trace.generate(config, tmp_path / "t.jsonl")


def test_a_malformed_bucket_id_is_refused(trace_config, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not of the form"):
        gen_trace.generate(
            trace_config(length_dist={"buckets": ["short"], "weights": [1.0]}),
            tmp_path / "t.jsonl",
        )


def test_weights_need_not_be_normalised(trace_config, tmp_path: Path) -> None:
    """They are normalised on the way in, so a config written as counts still works —
    and, more importantly, a config that sums to 0.999 does not silently skew."""
    body = _body(
        trace_config(
            length_dist={"buckets": ["p128_o64", "p512_o128"], "weights": [3, 1]},
            n_requests=400,
            duration_s=10_000,
        ),
        tmp_path / "t.jsonl",
    )
    short = sum(1 for r in body if r["bucket_id"] == "p128_o64")
    assert 0.6 < short / len(body) < 0.9


def test_a_zero_weight_bucket_never_appears(trace_config, tmp_path: Path) -> None:
    """Setting a weight to zero is how a bucket is removed from a run set without editing
    the bucket list — so the bucket list stays comparable across run sets."""
    body = _body(
        trace_config(
            length_dist={"buckets": ["p128_o64", "p512_o128"], "weights": [1.0, 0.0]},
            n_requests=200,
            duration_s=10_000,
        ),
        tmp_path / "t.jsonl",
    )
    assert {r["bucket_id"] for r in body} == {"p128_o64"}


def test_priority_is_carried_but_only_as_a_label(trace_config, tmp_path: Path) -> None:
    """§5.4 resolved `priority` to a passthrough label: generated and carried everywhere,
    read by nothing. The test that matters is that it is still emitted, and that a
    multi-valued mix produces more than one value."""
    body = _body(
        trace_config(priority_mix={"0": 0.5, "1": 0.5}, n_requests=200, duration_s=10_000),
        tmp_path / "t.jsonl",
    )
    assert {r["priority"] for r in body} == {0, 1}


def test_priority_keys_are_read_as_integers_not_strings(trace_config, tmp_path: Path) -> None:
    """A mix with a two-digit class sorts as "10" < "2" under string ordering, which would
    silently reassign the weights. The generator sorts by int; this is the guard."""
    body = _body(
        trace_config(
            priority_mix={"0": 0.0, "2": 0.0, "10": 1.0}, n_requests=50, duration_s=10_000
        ),
        tmp_path / "t.jsonl",
    )
    assert {r["priority"] for r in body} == {10}


# --------------------------------------------------------------------------------------
# Conformance of what actually gets written
# --------------------------------------------------------------------------------------


def test_generated_trace_conforms_to_c2(trace_config, schema, tmp_path: Path) -> None:
    """Every line, header included, against the committed C-2 schema."""
    path = tmp_path / "t.jsonl"
    gen_trace.generate(trace_config(arrival=MMPP, n_requests=120, duration_s=600), path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert_conforms(schema("trace"), lines, "trace line")


def test_content_seeds_fit_a_positive_32_bit_range(trace_config, tmp_path: Path) -> None:
    """The seed is what the materializer expands. Keeping it inside a positive 32-bit
    range means it survives any JSON reader, including one in another language."""
    body = _body(trace_config(n_requests=200, duration_s=10_000), tmp_path / "t.jsonl")
    for r in body:
        assert 0 <= r["content_seed"] < 2**31 - 1


def test_the_committed_smoke_config_still_generates(schema, tmp_path: Path) -> None:
    """`configs/smoke.json` is the config I reach for first on a new node. If it has
    drifted out of conformance, I want to know here and not on the node."""
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "configs" / "smoke.json").read_text()
    )
    path = tmp_path / "smoke.jsonl"
    gen_trace.generate(config, path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert_conforms(schema("trace"), lines, "smoke line")


@pytest.mark.parametrize("bucket", ["p0_o64", "p128_o0", "p0_o0"])
def test_a_zero_length_bucket_is_refused(bucket: str, trace_config, tmp_path: Path) -> None:
    """C-2 puts a minimum of 1 on `prompt_len` and `output_len`.

    Before this check the regex accepted `p0_o0` happily: the trace was written, hashed,
    and recorded in a manifest, and only failed later — at `contracts/check.py`, or in the
    materializer, which refuses `prompt_len < 1`. Both of those happen after the run.
    """
    with pytest.raises(ValueError, match="zero length"):
        gen_trace.generate(
            trace_config(length_dist={"buckets": [bucket], "weights": [1.0]}),
            tmp_path / "t.jsonl",
        )


def test_the_envelope_boundary_itself_is_admissible(trace_config, tmp_path: Path) -> None:
    """The check is `>`, not `>=`. A bucket sitting exactly on the F-13 ceiling is the
    most interesting one in the sweep — it is where the slow node's timeout binds — so it
    must not be excluded by an off-by-one."""
    body = _body(
        trace_config(
            length_dist={"buckets": ["p2048_o256"], "weights": [1.0]},
            n_requests=5,
            duration_s=10_000,
        ),
        tmp_path / "t.jsonl",
    )
    assert all(r["prompt_len"] == 2048 and r["output_len"] == 256 for r in body)


def test_a_single_token_bucket_is_admissible(trace_config, tmp_path: Path) -> None:
    """The other boundary. `p1_o1` is degenerate but legal, and it is what a latency-floor
    measurement would use."""
    body = _body(
        trace_config(
            length_dist={"buckets": ["p1_o1"], "weights": [1.0]}, n_requests=3, duration_s=10_000
        ),
        tmp_path / "t.jsonl",
    )
    assert all(r["prompt_len"] == 1 for r in body)


def test_the_mmpp_window_can_end_inside_a_state_switch(tmp_path: Path) -> None:
    """The one MMPP branch a coarse test never reaches.

    When the next interarrival would cross a state boundary, time advances to the boundary
    and the draw is retaken under the new rate — and that boundary can itself fall past
    `duration_s`. Getting the bound wrong on *that* path (rather than on the ordinary
    arrival path) produces a trace with requests after the end of its own window, which
    the C-2 schema does not forbid and the replay client would happily fire.

    Short dwell times against a short window is what makes the switch land outside it.
    """
    config = {
        "gen_seed": 1,
        "n_requests": 100_000,
        "duration_s": 4.0,
        "arrival": {
            "process": "mmpp",
            "lambda_base": 0.5,
            "burst_lambda": 2.0,
            "burst_mean_s": 0.7,
            "quiet_mean_s": 0.7,
        },
        "length_dist": {"buckets": ["p128_o64"], "weights": [1.0]},
        "priority_mix": {"0": 1.0},
        "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
        "vocab_size": 128000,
    }
    path = tmp_path / "t.jsonl"
    gen_trace.generate(config, path)
    header, body = gen_trace.load(path)

    assert body, "the process produced nothing; the branch under test was never reached"
    assert max(r["arrival_offset_s"] for r in body) <= config["duration_s"]
    assert header["n_requests"] == len(body)
