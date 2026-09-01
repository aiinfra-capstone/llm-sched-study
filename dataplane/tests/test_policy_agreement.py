"""The join refuses a run whose scheduler did not run the policy the manifest claims.

This guard exists because of a real finding rather than a hypothetical. F-1 requires all
five policies to be selectable from a single configuration value, and while that is not
yet true on the control plane, the scheduler writes a policy name into its own decision
records that nothing was checking. Meanwhile every C-5 row takes its `policy` label from
the manifest.

Two labels, two authors, no comparison between them. A scheduler dispatching round-robin
while writing "jsq" into its log, under a manifest that also says "jsq", produces a run
set in which every row is labelled with a policy that never ran. Nothing downstream can
notice: the latencies are real and the label is plausible. A 2x2 decomposition built from
four such runs compares one policy against itself under four names, and reports an
interaction term for it.
"""

from __future__ import annotations

from typing import Any

import pytest

from dataplane.pipeline import join as join_mod


def _manifest(policy: str = "jsq") -> dict[str, Any]:
    return {
        "run_id": "run1",
        "policy": policy,
        "lambda": 1.0,
        "staleness_s": 0.0,
        "warmup_s": 0.0,
        "nodes": [{"node_id": "n1", "host": "box-a", "role": "pool"}],
        "validity": {"valid": True},
    }


def _client() -> list[dict[str, Any]]:
    return [
        {
            "run_id": "run1",
            "req_id": "q1",
            "intended_offset_s": 1.0,
            "send_lag_ms": 0.1,
            "e2e_duration_ns": 2_000_000_000,
            "status": "ok",
        }
    ]


def _decision(policy: str) -> dict[str, Any]:
    return {
        "type": "decision",
        "run_id": "run1",
        "req_id": "q1",
        "policy": policy,
        "chosen_node": "n1",
        "decide_duration_ns": 120_000,
        "candidates": [],
    }


def _join(manifest: dict[str, Any], scheduler: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return join_mod.join(manifest=manifest, client=_client(), scheduler=scheduler, worker=[])


def test_agreement_joins_normally() -> None:
    rows = _join(_manifest("jsq"), [_decision("jsq")])
    assert rows[0]["policy"] == "jsq"


def test_a_scheduler_that_ran_something_else_is_refused() -> None:
    """The failure the guard exists for: a label that describes a run nobody performed."""
    with pytest.raises(ValueError, match="comparison nobody ran"):
        _join(_manifest("jsq"), [_decision("round_robin")])


def test_the_error_names_both_labels_so_the_disagreement_is_readable() -> None:
    with pytest.raises(ValueError) as caught:
        _join(_manifest("wjsq"), [_decision("round_robin")])
    message = str(caught.value)
    assert "'wjsq'" in message and "round_robin" in message


def test_several_policies_in_one_log_are_all_reported() -> None:
    """A log holding two policies is one run's worth of records from two runs."""
    scheduler = [_decision("jsq"), _decision("round_robin") | {"req_id": "q2"}]
    with pytest.raises(ValueError, match=r"\['jsq', 'round_robin'\]"):
        _join(_manifest("static_weighted"), scheduler)


def test_a_run_with_no_decision_records_says_nothing_and_is_left_alone() -> None:
    """The fixture scheduler writes none, and is honest about it rather than silent."""
    assert _join(_manifest("jsq"), [])[0]["policy"] == "jsq"


def test_a_decision_record_with_no_policy_field_is_not_a_disagreement() -> None:
    """Absent is not a claim. C-4 requires the field, so a missing one fails there."""
    record = _decision("jsq")
    del record["policy"]
    assert _join(_manifest("jsq"), [record])[0]["policy"] == "jsq"


def test_completion_records_are_not_policy_claims() -> None:
    """Only decisions carry a policy. A completion record must not be read as one."""
    scheduler = [
        _decision("jsq"),
        {"type": "completion_observed", "run_id": "run1", "req_id": "q1", "policy": "round_robin"},
    ]
    assert _join(_manifest("jsq"), scheduler)[0]["policy"] == "jsq"
