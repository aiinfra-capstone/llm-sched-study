"""F-9a — the synthesizable R range.

R is the study's main independent variable, so the thing worth testing is not the
division. It is the distinction between what configuration can synthesize and what a pool
obeying F-9a can actually be run at: F-9a forbids co-locating logical nodes on one host,
so if both extremes were measured on the same machine, the deployable ratio is not the
configured one. Reporting the configured figure as if it were deployable would claim a
heterogeneity the pool cannot reach.
"""

from __future__ import annotations

import pytest

from dataplane.calibration.r_range import (
    NodeClassThroughput,
    from_reports,
    synthesizable_range,
)


def _c(
    name: str, host: str, tok_s: float, ngl: int = 99, model: str = "Meta-Llama-3-8B-Instruct"
) -> NodeClassThroughput:
    return NodeClassThroughput(
        node_class=name,
        host=host,
        tokens_per_s=tok_s,
        engine_config={"ngl": ngl, "threads": 6, "parallel": 4},
        model=model,
    )


def test_range_across_distinct_hosts_is_deployable() -> None:
    rng = synthesizable_range([_c("fast", "a", 120.0), _c("slow", "b", 8.0)])

    assert rng.r_min == 1.0
    assert rng.r_max_configured == pytest.approx(15.0)
    assert rng.r_max_deployable == pytest.approx(15.0)
    assert not rng.colocation_limited
    assert rng.n_hosts == 2
    assert "R in [1.00, 15.00]" in rng.summary()


def test_a_single_host_cannot_deploy_the_ratio_its_configs_can_synthesize() -> None:
    """The fix for this is another machine, not another code path — so it is surfaced."""
    rng = synthesizable_range([_c("fast", "a", 120.0, ngl=99), _c("slow", "a", 8.0, ngl=0)])

    assert rng.r_max_configured == pytest.approx(15.0)
    assert rng.r_max_deployable == 1.0
    assert rng.colocation_limited
    assert rng.deployable_pair is None
    assert "F-9a forbids co-locating" in rng.summary()
    assert rng.to_dict()["deployable_pair"] is None


def test_deployable_pair_picks_the_widest_cross_host_ratio() -> None:
    classes = [
        _c("fastest", "a", 120.0),
        _c("mid", "a", 40.0),
        _c("slow", "b", 10.0),
        _c("slowest", "a", 5.0),
    ]
    rng = synthesizable_range(classes)

    # 120/5 = 24 is configured but same-host; 120/10 = 12 is the widest legal pair.
    assert rng.r_max_configured == pytest.approx(24.0)
    assert rng.r_max_deployable == pytest.approx(12.0)
    assert rng.deployable_pair is not None
    assert [c.node_class for c in rng.deployable_pair] == ["fastest", "slow"]

    d = rng.to_dict()
    assert d["fastest"]["node_class"] == "fastest" and d["slowest"]["node_class"] == "slowest"
    assert d["n_classes"] == 4 and d["n_hosts"] == 2


def test_a_ratio_needs_two_classes_and_a_positive_throughput() -> None:
    with pytest.raises(ValueError, match="R is a ratio between two node classes"):
        synthesizable_range([_c("only", "a", 100.0)])
    with pytest.raises(ValueError, match="must be > 0"):
        _c("dead", "a", 0.0)


def test_classes_are_read_from_the_campaigns_own_output() -> None:
    """Hand-copying a throughput figure is the easiest place to introduce an error."""
    reports = [
        {
            "node_class": "fast",
            "model": "Meta-Llama-3-8B-Instruct",
            "host": "a",
            "headline_tokens_per_s": 120.0,
            "engine_config": {"ngl": 99, "threads": 6, "parallel": 4},
        },
        {
            "node_class": "slow",
            "model": "Meta-Llama-3-8B-Instruct",
            "host": "b",
            "headline_tokens_per_s": 8.0,
            "engine_config": {"ngl": 0, "threads": 6, "parallel": 4},
        },
    ]
    classes = from_reports(reports)
    assert synthesizable_range(classes).r_max_deployable == pytest.approx(15.0)


def test_a_range_across_two_models_is_refused() -> None:
    """F-9 holds the model constant inside a pool so that R is a property of hardware and
    configuration alone. An R computed across models has a model effect baked in, and
    neither the H1 2x2 decomposition nor the R-sweep can pull those apart afterwards."""
    with pytest.raises(ValueError, match="confounds the heterogeneity ratio"):
        synthesizable_range(
            [
                _c("gpu_8b", "a", 6.4, model="Meta-Llama-3-8B-Instruct"),
                _c("gpu_1b", "b", 70.3, model="Llama-3.2-1B-Instruct"),
            ]
        )


def test_a_campaign_report_without_a_model_cannot_be_checked() -> None:
    """Silently defaulting the model would make the F-9 guard unable to fire."""
    with pytest.raises(ValueError, match="no `model` field"):
        from_reports(
            [{"node_class": "n", "host": "a", "headline_tokens_per_s": 1.0, "engine_config": {}}]
        )
