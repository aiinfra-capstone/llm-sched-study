"""The Week-2 command-line surface, and the branches only a real invocation reaches.

The campaign and the R-range report are the two things a person actually types during
Week 2, so their entry points are covered the same way the rest of the harness is. The
`__main__` guards are exercised through `runpy` rather than a subprocess: a subprocess
would not be measured, and an entry point nothing measures is an entry point that can rot
between the week it is written and the week it is used.
"""

from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path

import pytest

from dataplane.calibration import campaign as camp
from dataplane.calibration import cost_model as cm
from dataplane.calibration import rrange
from dataplane.worker.adapter import LiveState, ServiceResult


def _report(node_class: str, host: str, tok_s: float, ngl: int, model: str = "M") -> dict:
    return {
        "run_id": f"cal_{node_class}",
        "node_class": node_class,
        "model": model,
        "host": host,
        "headline_tokens_per_s": tok_s,
        "engine_config": {"ngl": ngl, "threads": 6, "parallel": 4},
    }


def _campaign_tree(root: Path) -> Path:
    for i, rep in enumerate(
        [
            _report("gtx1650ti_ngl99_p4_q4km_llama32_1b", "fedora", 107.8, 99),
            _report("cpu_ngl0_p4_q4km_llama3_8b", "thinkpad", 2.6, 0),
        ]
    ):
        d = root / f"run_{i}"
        d.mkdir(parents=True)
        (d / "campaign.json").write_text(json.dumps(rep))
    return root


def test_r_range_cli_reports_a_range_and_the_classes_behind_it(tmp_path, capsys) -> None:
    assert rrange.main([str(_campaign_tree(tmp_path))]) == 0

    out = capsys.readouterr().out
    assert "R in [1.00, 41.46]" in out
    assert "gtx1650ti_ngl99_p4_q4km_llama32_1b" in out
    assert "cpu_ngl0_p4_q4km_llama3_8b" in out


def test_r_range_cli_can_write_the_range_as_json(tmp_path) -> None:
    out_file = tmp_path / "out" / "r_range.json"
    rrange.main([str(_campaign_tree(tmp_path / "runs")), "--out", str(out_file)])

    written = json.loads(out_file.read_text())
    assert written["r_min"] == 1.0
    assert written["r_max_deployable"] == pytest.approx(41.4615, rel=1e-3)
    assert written["colocation_limited"] is False


def test_r_range_cli_refuses_to_invent_a_pool(tmp_path) -> None:
    with pytest.raises(SystemExit, match="run the calibration campaign first"):
        rrange.main([str(tmp_path)])


@pytest.mark.parametrize(
    "module", ["dataplane.calibration.campaign", "dataplane.calibration.rrange"]
)
def test_entry_points_are_runnable_as_modules(module: str, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", [module, "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module(module, run_name="__main__")
    assert exc.value.code == 0


def test_a_snapshot_mark_with_no_samples_behind_it_is_skipped() -> None:
    """A sustained segment with a gap must not emit a snapshot fitted to nothing.

    The gap is not hypothetical: a node that stalls under thermal throttling stops
    completing requests, and that is precisely when a recalibration heartbeat would fire
    on an empty window.
    """
    inputs = cm.example_inputs()
    base = cm.build_snapshot(**inputs)

    early = [o for o in inputs["observations"][:3]]
    late = [
        cm.Observation(
            prompt_len=o.prompt_len,
            output_len=o.output_len,
            concurrency=o.concurrency,
            service_ns=o.service_ns,
            output_tokens=o.output_tokens,
            t_end_ns=o.t_end_ns + 30_000_000_000,
            status="ok",
            prefill_ns=o.prefill_ns,
            decode_ns=o.decode_ns,
        )
        for o in inputs["observations"][:3]
    ]
    series = camp.snapshot_series(
        base,
        early + late,
        every_s=1.0,
        window_s=1.0,
        prompt_edges=inputs["prompt_edges"],
        output_edges=inputs["output_edges"],
    )

    # Marks land across a 30s gap; only the marks with samples behind them produce one.
    assert 0 < len(series) < 30


class _DeadEngine:
    """Answers every request with a timeout — the node that is up but serving nothing."""

    async def complete(self, prompt: list[int], output_len: int) -> ServiceResult:
        await asyncio.sleep(0)
        return ServiceResult(
            status="timeout", service_ns=5_000_000_000, prompt_tokens=len(prompt), output_tokens=0
        )


def test_a_campaign_where_nothing_succeeded_refuses_to_fit() -> None:
    """Every request failed. There is no cost model to build, and building one out of
    timeouts would report the timeout ceiling as the node's speed."""
    config = camp.CampaignConfig.from_dict(
        {
            "node_class": "dead",
            "model": "Meta-Llama-3-8B-Instruct",
            "host": "h",
            "endpoint": "http://127.0.0.1:9",
            "prompt_edges": [1, 128],
            "output_edges": [1, 64],
            "prompt_lens": [64],
            "output_lens": [32],
            # Two cells, so the "this cell produced nothing to probe F-18 with" branch is
            # taken and the loop still moves on to the next cell.
            "concurrencies": [1, 2],
            "samples_per_cell": 2,
            "warmup_per_cell": 0,
            "sustained": {
                "prompt_len": 64,
                "output_len": 32,
                "concurrency": 1,
                "duration_s": 0.02,
                "window_s": 0.005,
            },
            "vocab_size": 1000,
            "seed": 1,
            "provenance": {
                "engine": "llamacpp",
                "engine_version": "b10569",
                "quant": "Q4_K_M",
                "gpu": "none",
                "driver": "0",
                "prefix_caching": False,
                "engine_config": {"ngl": 0, "threads": 1, "parallel": 1},
            },
            "admissibility": {"max_prompt": 128, "max_output": 64, "timeout_ceiling_ms": 5000},
        }
    )
    with pytest.raises(ValueError, match="no successful samples"):
        asyncio.run(camp.run_campaign(_DeadEngine(), config))


def test_autocorr_time_can_be_fitted_from_a_raw_series() -> None:
    """The same estimator in both directions, so a snapshot's tau and a freshly fitted
    one are the same quantity — otherwise H3's x-axis is measured one way and modelled
    another."""
    series = cm.example_throughput_series(tau_s=30.0, dt_s=1.0, n=4000)
    fitted = cm.autocorr_time_s(series, dt_s=1.0)

    assert 30.0 / 1.6 <= fitted <= 30.0 * 1.6
    assert cm.autocorr_time_s(cm.build_snapshot(**cm.example_inputs())) == pytest.approx(42.0)


def test_an_engine_state_outside_the_contract_is_refused() -> None:
    """C-1 pins `engine_state` to three values; a fourth would reach the scheduler as a
    string it has no branch for."""
    with pytest.raises(ValueError, match="engine_state must be one of"):
        LiveState(state="on_fire")
