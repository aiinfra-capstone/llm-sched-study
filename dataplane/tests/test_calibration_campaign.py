"""The Week-2 campaign driver, run against a fake engine.

The campaign's job is to produce measurements that are *about the node*, so the tests
here are mostly about the ways it could accidentally measure itself: warmup folded into a
cell mean, prompts reused so prefix caching flatters the numbers, a fit failure throwing
away samples that cost minutes of GPU time, or a snapshot series that is one snapshot
repeated and therefore useless to H3.

The fake engine sleeps for real, because the sustained segment is binned on wall-clock
completion stamps and an engine that returned instantly would give every request the same
timestamp — which is not a faster test, it is a different code path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Self

import pytest

from dataplane.calibration import campaign as camp
from dataplane.calibration import cost_model as cm
from dataplane.worker.adapter import ServiceResult

_BASE_CONFIG = {
    "node_class": "fake_ngl99_p2_q4km",
    "model": "Llama-3.2-1B-Instruct",
    "host": "testhost",
    "endpoint": "http://127.0.0.1:9",
    "prompt_edges": [1, 128, 512],
    "output_edges": [1, 64, 128],
    "prompt_lens": [64],
    "output_lens": [32],
    "concurrencies": [1, 2],
    "samples_per_cell": 2,
    "warmup_per_cell": 1,
    "sustained": {
        "prompt_len": 64,
        "output_len": 32,
        "concurrency": 2,
        "duration_s": 0.6,
        "window_s": 0.005,
    },
    "snapshot_every_s": 0.1,
    "snapshot_window_s": 0.2,
    "vocab_size": 1000,
    "seed": 7,
    "provenance": {
        "engine": "llamacpp",
        "engine_version": "b10569+cuda13.2",
        "quant": "Q4_K_M",
        "gpu": "fake",
        "driver": "0",
        "prefix_caching": False,
        "engine_config": {"ngl": 99, "threads": 2, "parallel": 2},
    },
    "admissibility": {"max_prompt": 512, "max_output": 128, "timeout_ceiling_ms": 5000},
}


class FakeEngine:
    """Records every prompt it is given, and takes real (small) time to answer."""

    def __init__(self, *, healthy: bool = True, fail_every: int | None = None) -> None:
        self.healthy = healthy
        # Intermittent rather than terminal: a node that fails partway and stays failed
        # would leave the sustained segment with no samples in its second half, which is a
        # different scenario (a dead node) from the one being tested here (a lossy one).
        self.fail_every = fail_every
        self.prompts: list[tuple[int, ...]] = []

    async def health(self) -> bool:
        return self.healthy

    async def complete(self, prompt: list[int], output_len: int) -> ServiceResult:
        self.prompts.append(tuple(prompt))
        await asyncio.sleep(0.003)
        if self.fail_every is not None and len(self.prompts) % self.fail_every == 0:
            return ServiceResult(
                status="timeout",
                service_ns=5_000_000_000,
                prompt_tokens=len(prompt),
                output_tokens=0,
            )
        return ServiceResult(
            status="ok",
            service_ns=3_000_000,
            prompt_tokens=len(prompt),
            output_tokens=output_len,
            prefill_ns=1_000_000,
            decode_ns=2_000_000,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _config(**over) -> camp.CampaignConfig:
    merged = dict(_BASE_CONFIG)
    for k, v in over.items():
        merged[k] = (
            {**merged[k], **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
        )
    return camp.CampaignConfig.from_dict(merged)


def test_grid_varies_concurrency_slowest() -> None:
    """Slot occupancy is the expensive transition; sweeping lengths inside it keeps the
    engine in one regime, so drift shows up within a concurrency level, not aliased onto it."""
    points = camp.grid(_config(prompt_lens=[64, 256], output_lens=[32, 100], concurrencies=[1, 4]))

    assert [p.concurrency for p in points] == [1, 1, 1, 1, 4, 4, 4, 4]
    assert points[0].label == "p64_o32_c1"


def test_warmup_is_dropped_per_cell_not_once_per_campaign() -> None:
    """The first request into a new concurrency pays slot allocation; the first into a new
    `-ngl` pays kernel JIT. Either one folded into a cell mean is a one-off cost the
    scheduler would then apply to every request."""
    engine = FakeEngine()
    result = asyncio.run(camp.run_campaign(engine, _config()))

    # 2 cells x (1 warmup + 2 samples) fired; 2 cells x 2 samples kept.
    assert len(engine.prompts) >= 6
    assert len(result.observations) == 4


def test_every_prompt_in_a_cell_is_distinct() -> None:
    """Reusing one prompt would let any prefix reuse show up as a speedup the pool will
    never see on a real trace."""
    point = camp.GridPoint(64, 32, 1)
    pool = camp._prompt_pool(seed=7, point=point, count=6, vocab_size=1000)

    assert len({tuple(p) for p in pool}) == 6
    assert all(len(p) == 64 for p in pool)
    # Deterministic from the campaign seed (F-20).
    assert camp._prompt_pool(seed=7, point=point, count=6, vocab_size=1000) == pool
    assert camp._prompt_pool(seed=8, point=point, count=6, vocab_size=1000) != pool


def test_a_campaign_produces_a_time_ordered_snapshot_series() -> None:
    """C-3's least obvious requirement: without a real history, Aditya has to synthesize
    age by perturbing parameters, and H3 becomes a study of his perturbation model."""
    result = asyncio.run(camp.run_campaign(FakeEngine(), _config()))

    assert result.report["stationarity_error"] is None
    assert len(result.snapshots) > 1
    stamps = [s["measured_at_unix"] for s in result.snapshots]
    assert stamps == sorted(stamps)
    # Ids must stay distinct even when the whole series lands inside one second: the
    # manifest references snapshots by id, so a collision makes a staleness lookup
    # ambiguous rather than wrong-looking.
    assert len({s["snapshot_id"] for s in result.snapshots}) == len(result.snapshots)
    assert result.f18 == "full"


def test_failures_are_counted_but_never_fitted() -> None:
    engine = FakeEngine(fail_every=7)
    result = asyncio.run(camp.run_campaign(engine, _config()))

    ok_count = sum(1 for o in result.observations + result.sustained if o.status == "ok")
    assert result.failures.get("timeout", 0) > 0
    assert result.report["failures"] == result.failures
    # Every fitted sample is an ok sample: the cells account for the successes and nothing
    # else, so no cell mean can contain the 5s timeout ceiling.
    assert sum(e["n_samples"] for e in result.snapshots[0]["entries"]) == ok_count


def test_a_fit_that_cannot_be_made_keeps_the_samples() -> None:
    """The samples cost minutes of GPU time. No tau means no C-3 snapshot — the schema
    requires a positive `autocorr_time_s` and inventing one is the only unacceptable
    outcome — but the observations survive and the campaign says why."""
    result = asyncio.run(camp.run_campaign(FakeEngine(), _config(sustained={"duration_s": 0.02})))

    assert result.snapshots == []
    assert result.report["stationarity"] is None
    assert "30-window floor" in result.report["stationarity_error"]
    assert result.report["n_sustained_samples"] > 0
    assert result.report["headline_tokens_per_s"] > 0  # R does not depend on tau


def test_snapshot_series_is_empty_when_nothing_succeeded() -> None:
    edges = {"prompt_edges": [1, 128], "output_edges": [1, 64]}
    assert camp.snapshot_series({"node_class": "n"}, [], every_s=1.0, window_s=1.0, **edges) == []
    failed = [cm.Observation(64, 32, 1, 10**9, 0, i, status="timeout") for i in range(5)]
    assert (
        camp.snapshot_series({"node_class": "n"}, failed, every_s=1.0, window_s=1.0, **edges) == []
    )


def test_headline_throughput_is_zero_when_no_split_was_reported() -> None:
    """A `partial` backend gives no decode time, so no tok/s — stated, not guessed."""
    assert camp._median_decode_tok_s([]) == 0.0
    no_split = [cm.Observation(64, 32, 1, 10**9, 32, i) for i in range(3)]
    assert camp._median_decode_tok_s(no_split) == 0.0


def test_config_round_trips_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(_BASE_CONFIG))
    config = camp.load_config(path)

    assert config.node_class == "fake_ngl99_p2_q4km"
    assert config.warmup_per_cell == 1
    # Defaults exist for the two fields a short config may omit.
    minimal = camp.CampaignConfig.from_dict(
        {
            k: v
            for k, v in _BASE_CONFIG.items()
            if k not in {"warmup_per_cell", "snapshot_every_s", "snapshot_window_s"}
        }
    )
    assert (minimal.warmup_per_cell, minimal.snapshot_every_s, minimal.snapshot_window_s) == (
        2,
        30.0,
        60.0,
    )


def test_write_result_lays_out_one_directory_per_campaign(tmp_path: Path) -> None:
    result = asyncio.run(camp.run_campaign(FakeEngine(), _config()))
    camp.write_result(tmp_path, result)

    observations = (tmp_path / "observations.jsonl").read_text().splitlines()
    assert len(observations) == len(result.observations) + len(result.sustained)
    assert {json.loads(o)["segment"] for o in observations} == {"grid", "sustained"}

    snapshots = sorted((tmp_path / "snapshots").glob("*.json"))
    assert len(snapshots) == len(result.snapshots)
    assert snapshots[0].name.startswith("000_cm_")
    assert (
        json.loads((tmp_path / "campaign.json").read_text())["node_class"] == "fake_ngl99_p2_q4km"
    )


def _write_config(tmp_path: Path, **over) -> Path:
    merged = dict(_BASE_CONFIG)
    for k, v in over.items():
        merged[k] = (
            {**merged[k], **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else v
        )
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(merged))
    return path


def test_cli_writes_the_campaign_and_reports_mpr1(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(camp, "LlamaCppAdapter", lambda *a, **k: FakeEngine())
    code = camp.main(["--config", str(_write_config(tmp_path)), "--out", str(tmp_path / "runs")])

    out = capsys.readouterr().out
    assert code == 0
    assert "MPR-1  tau =" in out
    assert "understates its own standard error" in out
    assert list((tmp_path / "runs").glob("*/campaign.json"))


def test_cli_exits_nonzero_when_mpr1_did_not_land(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(camp, "LlamaCppAdapter", lambda *a, **k: FakeEngine())
    config = _write_config(tmp_path, sustained={"duration_s": 0.02})
    code = camp.main(["--config", str(config), "--out", str(tmp_path / "runs")])

    assert code == 1
    assert "MPR-1  NOT ESTABLISHED" in capsys.readouterr().out
    assert list((tmp_path / "runs").glob("*/observations.jsonl"))  # samples kept anyway


def test_cli_refuses_to_calibrate_against_a_server_that_is_not_up(tmp_path, monkeypatch) -> None:
    """A campaign against a dead endpoint would produce a table of engine_errors."""
    monkeypatch.setattr(camp, "LlamaCppAdapter", lambda *a, **k: FakeEngine(healthy=False))
    with pytest.raises(SystemExit, match="no healthy llama-server"):
        camp.main(["--config", str(_write_config(tmp_path)), "--out", str(tmp_path / "runs")])


def test_cli_endpoint_override_wins_over_the_config(tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    def spy(endpoint, **kw):
        seen.append(endpoint)
        return FakeEngine()

    monkeypatch.setattr(camp, "LlamaCppAdapter", spy)
    camp.main(
        [
            "--config",
            str(_write_config(tmp_path)),
            "--out",
            str(tmp_path / "runs"),
            "--endpoint",
            "http://other:8080",
        ]
    )
    assert seen == ["http://other:8080"]


def test_cli_prints_failures_it_did_not_fit(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(camp, "LlamaCppAdapter", lambda *a, **k: FakeEngine(fail_every=7))
    camp.main(["--config", str(_write_config(tmp_path)), "--out", str(tmp_path / "runs")])
    assert "failures (not fitted)" in capsys.readouterr().out


def test_the_report_carries_the_config_it_ran_not_a_path_to_it(tmp_path, monkeypatch) -> None:
    """F-20 says a run is reproducible from one config plus a seed. A report that names a
    *file* leaves that promise resting on something anyone can edit afterwards — which is
    exactly how a sustained-cell change went unnoticed mid-campaign. C-6 manifests embed the
    config verbatim; this is the campaign report catching up."""
    monkeypatch.setattr(camp, "LlamaCppAdapter", lambda *a, **k: FakeEngine())
    config_path = _write_config(tmp_path)
    camp.main(["--config", str(config_path), "--out", str(tmp_path / "runs")])

    report = json.loads(next((tmp_path / "runs").rglob("campaign.json")).read_text())
    on_disk = json.loads(config_path.read_text())
    assert report["config"] == on_disk
    assert report["config_hash"] == camp.manifest_mod.config_hash(on_disk)

    # Editing the file afterwards must not change what the report says the run used.
    on_disk["sustained"]["window_s"] = 999.0
    config_path.write_text(json.dumps(on_disk))
    again = json.loads(next((tmp_path / "runs").rglob("campaign.json")).read_text())
    assert again["config"]["sustained"]["window_s"] != 999.0


def test_a_config_built_in_code_still_reports_what_it_ran() -> None:
    """`from_dict` keeps the file's bytes; a config assembled directly has none to keep, so
    `as_dict` reconstructs it from the fields rather than reporting nothing."""
    built = camp.CampaignConfig(
        node_class="n", model="m", host="h", endpoint="http://x",
        prompt_edges=[1, 128], output_edges=[1, 64], prompt_lens=[64], output_lens=[32],
        concurrencies=[1], samples_per_cell=1, warmup_per_cell=0,
        sustained={"prompt_len": 64, "output_len": 32, "concurrency": 1,
                   "duration_s": 1.0, "window_s": 0.5},
        provenance={}, admissibility={}, vocab_size=1000, seed=1,
        snapshot_every_s=1.0, snapshot_window_s=1.0,
    )
    assert built.raw is None
    d = built.as_dict()
    assert "raw" not in d
    assert d["node_class"] == "n" and d["sustained"]["window_s"] == 0.5
