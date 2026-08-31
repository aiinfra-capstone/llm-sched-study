"""Week 3 — the admissible set (F-13), and the validation anchors Week 4 depends on.

Skipped until `dataplane.calibration.admissible` lands.

The admissible set is the (prompt, output) envelope **every** pool node can serve inside
the timeout ceiling. It is determined once, from the Week-2 campaign, and then written
into every trace config. Two things make it worth guarding with tests written in advance:

  * It is an intersection, not a union. The slowest node binds it. Taking the union would
    put buckets into the trace that the CPU node cannot finish, and the resulting
    categorical failures would be read as a scheduling result rather than as a bad
    envelope.
  * It closes a loop with code that already exists: `gen_trace` refuses a bucket outside
    the envelope. So a determined envelope must round-trip into a config the Week-1
    generator accepts. That round trip is the actual test.
"""

from __future__ import annotations

import pytest
from conftest import pending

from dataplane.harness import gen_trace

pytestmark = pytest.mark.forward

admissible = pending(
    "dataplane.calibration.admissible",
    "determine",
    week="Week 3",
    deliverable="admissible-set determination",
)


def test_the_envelope_is_the_intersection_across_the_pool() -> None:
    """The slow node binds. A union would guarantee timeouts on it, and the study would
    then be measuring my envelope choice rather than the policies."""
    fast = {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000}
    slow = {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 60000}
    envelope = admissible.determine([fast, slow])
    assert envelope["max_prompt"] == 512
    assert envelope["max_output"] == 128


def test_a_pool_with_no_common_envelope_is_an_error_not_an_empty_set() -> None:
    """If no bucket is servable by every node, the pool cannot run a comparable workload
    and the right answer is to say so, not to emit an envelope of zero."""
    with pytest.raises(ValueError):
        admissible.determine([{"max_prompt": 0, "max_output": 0, "timeout_ceiling_ms": 1}])


def test_the_timeout_ceiling_is_the_tightest_one_in_the_pool() -> None:
    """The ceiling is what makes a slow node's failure categorical rather than a long
    tail. It has to be the one every node can actually meet."""
    envelope = admissible.determine(
        [
            {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
            {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 30000},
        ]
    )
    assert envelope["timeout_ceiling_ms"] == 30000


def test_a_determined_envelope_round_trips_into_a_generatable_trace(tmp_path) -> None:
    """The loop that closes Week 1 against Week 3. `gen_trace` refuses a bucket outside
    the envelope, so an envelope that cannot produce a trace is an envelope with no use."""
    envelope = admissible.determine(
        [{"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000}]
    )
    config = {
        "gen_seed": 1,
        "n_requests": 20,
        "duration_s": 60,
        "arrival": {"process": "poisson", "lambda_base": 5.0},
        "length_dist": {"buckets": admissible.buckets_within(envelope), "weights": None},
        "priority_mix": {"0": 1.0},
        "admissible": envelope,
        "vocab_size": 128000,
    }
    config["length_dist"]["weights"] = [1.0] * len(config["length_dist"]["buckets"])
    gen_trace.generate(config, tmp_path / "t.jsonl")


def test_every_proposed_bucket_fits_the_envelope_it_came_from() -> None:
    """Belt and braces on the same loop, without going through file I/O."""
    envelope = {"max_prompt": 1024, "max_output": 128, "timeout_ceiling_ms": 60000}
    for bucket in admissible.buckets_within(envelope):
        prompt, output = bucket[1:].split("_o")
        assert int(prompt) <= envelope["max_prompt"]
        assert int(output) <= envelope["max_output"]


def test_the_envelope_is_derived_from_measurement_not_from_a_constant() -> None:
    """F-13 says the envelope is what the pool can serve, which is a measured fact about
    Week-2 data. A hardcoded 2048/256 would survive a pool change silently."""
    source = admissible.__file__
    with open(source) as fh:
        text = fh.read()
    assert "cost_model" in text or "service_ms" in text


# --------------------------------------------------------------------------------------
# Validation anchors — my Week-3 deliverable, consumed by F-23 in Week 4
# --------------------------------------------------------------------------------------


def test_anchors_cover_at_least_three_operating_points() -> None:
    """F-23 compares p50 and p95 at three operating points. Two would not distinguish a
    simulator that is right from one that is calibrated to a single load."""
    anchors = admissible.load_anchors()
    assert len(anchors) >= 3


def test_every_anchor_run_is_a_valid_run() -> None:
    """An anchor is the ground truth the simulator is judged against. Anchoring to a run
    whose load generator drifted would move the target instead of failing the check."""
    for anchor in admissible.load_anchors():
        assert anchor["validity"]["valid"] is True


def test_anchor_runs_share_one_trace(schema) -> None:
    """F-23 compares vehicles on identical replayed traces. Different traces across
    operating points would confound the comparison with a workload difference."""
    anchors = admissible.load_anchors()
    for anchor in anchors:
        assert not list(schema("manifest").iter_errors(anchor))
    assert len({a["trace_sha256"] for a in anchors}) == 1


def test_anchor_runs_are_hardware_runs() -> None:
    """Validating a simulator against a simulator proves nothing."""
    assert {a["vehicle"] for a in admissible.load_anchors()} == {"hardware"}
