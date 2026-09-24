"""A cost-model cell is fitted only from samples served at the concurrency it claims.

A calibration cell fires more requests than it holds in flight, so its last few are served by
a draining batch. At concurrency 4 with 10 requests, two of them run alongside one other
request rather than three, and finish faster for it. Averaging them in reports a speed at
concurrency 4 that concurrency 4 never produced, and because the share of drained samples
depends on how the cell size divides by its concurrency, the bias lands unevenly across the
grid.

So every sample records how many requests it actually shared the engine with
(`occupancy_mean`), the fit keeps the ones at the stated concurrency, and the campaign says
how many each cell kept. These tests pin all four parts, including the two that must not
change: a cell with no steady sample still gets an entry, and a calibration recorded before
the field existed re-fits exactly as it did.

Coverage cannot see most of this. The filter is a conditional expression inside a
comprehension, and branch coverage does not measure the sides of one.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_calibration_campaign import FakeEngine, _config

from dataplane.calibration import admissible
from dataplane.calibration import campaign as camp
from dataplane.calibration import cost_model as cm


def _obs(
    concurrency: int,
    occupancy: float | None,
    *,
    service_ms: float = 100.0,
    status: str = "ok",
    prompt_len: int = 64,
    output_len: int = 32,
    t_end_ns: int = 0,
) -> cm.Observation:
    return cm.Observation(
        prompt_len=prompt_len,
        output_len=output_len,
        concurrency=concurrency,
        service_ns=int(service_ms * 1e6),
        output_tokens=output_len if status == "ok" else 0,
        t_end_ns=t_end_ns,
        status=status,
        prefill_ns=int(service_ms * 0.2e6) if status == "ok" else None,
        decode_ns=int(service_ms * 0.8e6) if status == "ok" else None,
        occupancy_mean=occupancy,
    )


def _snapshot(observations: list[cm.Observation]) -> dict:
    return cm.build_snapshot(
        observations,
        node_class="fake_ngl99_p4_q4km",
        prompt_edges=[1, 128, 512],
        output_edges=[1, 64, 128],
        provenance={"engine": "llamacpp"},
        admissibility={"max_prompt": 512, "max_output": 128},
        calibration_run_ids=["cal_x"],
        stochastic={"form": "lognormal"},
        measured_at_unix=1_789_000_000,
    )


# --------------------------------------------------------------- which samples are steady


def test_a_draining_cell_keeps_only_its_steady_samples() -> None:
    """Six requests shared the engine with three others; the last two ran beside one."""
    cell = [_obs(4, 4.0)] * 6 + [_obs(4, 2.0, service_ms=60.0)] * 2
    assert cm.steady_samples(cell) == cell[:6]
    assert cm.at_stated_concurrency(cell) == cell[:6]


def test_steady_allows_a_small_shortfall_and_no_more() -> None:
    """Occupancy is measured from overlapping spans, so a request that starts a hair after
    its neighbours reads a little under the stated concurrency without having drained."""
    edge = 4 - cm.OCCUPANCY_TOLERANCE
    kept = _obs(4, edge)
    dropped = _obs(4, edge - 0.01)
    assert cm.steady_samples([kept, dropped]) == [kept]


def test_a_cell_with_no_steady_sample_falls_back_rather_than_leaving_a_hole() -> None:
    """A cost model with a missing cell cannot price a request that lands there, which is
    worse than one with a known bias. The campaign reports the thin cell instead."""
    cell = [_obs(4, 1.5), _obs(4, 2.0), _obs(4, 3.0)]
    assert cm.steady_samples(cell) == []
    assert cm.at_stated_concurrency(cell) == cell


def test_a_sample_recorded_before_occupancy_existed_is_kept() -> None:
    old = [_obs(4, None), _obs(4, None)]
    assert cm.steady_samples(old) == old


def test_the_fit_uses_only_the_steady_samples_of_a_draining_cell() -> None:
    steady = [_obs(4, 4.0, service_ms=200.0)] * 6
    drained = [_obs(4, 1.8, service_ms=80.0)] * 2
    (entry,) = _snapshot(steady + drained)["entries"]
    assert entry["n_samples"] == 6
    assert entry["service_ms_mean"] == 200.0


def test_the_fit_keeps_a_cell_whose_every_sample_drained() -> None:
    (entry,) = _snapshot([_obs(4, 2.0, service_ms=90.0), _obs(4, 1.5, service_ms=70.0)])["entries"]
    assert entry["n_samples"] == 2
    assert entry["service_ms_mean"] == 80.0


def test_an_older_calibration_refits_exactly_as_it_did(tmp_path) -> None:
    """Observations written before `occupancy_mean` existed have no such key. Read back, they
    carry `None`, every one of them is fitted, and the snapshot is the one fitted then."""
    lines = [
        {
            "prompt_len": 64,
            "output_len": 32,
            "concurrency": 4,
            "service_ns": service * 1_000_000,
            "output_tokens": 32,
            "t_end_ns": i,
            "status": "ok",
            "prefill_ns": 20_000_000,
            "decode_ns": 80_000_000,
            "error": "",
            "segment": "grid",
        }
        for i, service in enumerate((100, 110, 60, 50))
    ]
    (tmp_path / "observations.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    obs = admissible.load_observations(tmp_path)
    assert all(o.occupancy_mean is None for o in obs)
    (entry,) = _snapshot(obs)["entries"]
    assert entry["n_samples"] == 4
    assert entry["service_ms_mean"] == 80.0


# ------------------------------------------------------------- how occupancy is measured


def _spanned(start_ms: float, end_ms: float, concurrency: int = 4) -> cm.Observation:
    """A sample whose engine span is [start, end] in milliseconds."""
    return _obs(concurrency, None, service_ms=end_ms - start_ms, t_end_ns=int(end_ms * 1e6))


def test_four_requests_in_flight_together_each_record_four() -> None:
    obs = camp._with_occupancy([_spanned(0, 100) for _ in range(4)])
    assert [o.occupancy_mean for o in obs] == [4.0, 4.0, 4.0, 4.0]


def test_a_request_the_batch_drained_under_records_less() -> None:
    """Two requests span [0, 100]; a third runs [50, 150], so half of it overlaps each of the
    first two and the second half runs alone."""
    obs = camp._with_occupancy([_spanned(0, 100), _spanned(0, 100), _spanned(50, 150)])
    assert [o.occupancy_mean for o in obs] == [2.5, 2.5, 2.0]


def test_a_request_alone_records_one() -> None:
    (only,) = camp._with_occupancy([_spanned(0, 100)])
    assert only.occupancy_mean == 1.0


def test_a_failed_request_with_no_span_gets_a_number_not_a_division_by_zero() -> None:
    failed = _obs(4, None, service_ms=0.0, status="timeout", t_end_ns=int(50e6))
    obs = camp._with_occupancy([_spanned(0, 100), failed])
    assert obs[0].occupancy_mean == 1.0
    assert obs[1].occupancy_mean >= 1.0


def test_occupancy_is_measured_over_the_engine_span_not_the_wall_clock() -> None:
    """The span is `[t_end - service, t_end]`, the interval service time is measured over, so
    a request that waited for a semaphore slot is not counted as sharing the engine then."""
    early = _spanned(0, 100)
    late = _spanned(100, 200)  # queued behind `early`, never in the engine with it
    assert [o.occupancy_mean for o in camp._with_occupancy([early, late])] == [1.0, 1.0]


def test_every_other_field_survives_the_occupancy_record() -> None:
    before = _spanned(0, 100)
    (after,) = camp._with_occupancy([before])
    assert after.service_ns == before.service_ns
    assert after.t_end_ns == before.t_end_ns
    assert after.status == before.status


# ------------------------------------------------------------------ what the campaign says


def test_steady_counts_are_per_cell_and_ignore_failed_samples() -> None:
    observations = [
        _obs(4, 4.0),
        _obs(4, 4.0),
        _obs(4, 2.0),
        _obs(4, None, status="timeout"),
        _obs(1, 1.0, prompt_len=256),
    ]
    assert camp._steady_counts(observations) == {"p64_o32_c4": "2/3", "p256_o32_c1": "1/1"}


def test_steady_counts_of_nothing_is_empty() -> None:
    assert camp._steady_counts([_obs(4, None, status="timeout")]) == {}


def test_the_counts_reach_campaign_json_and_the_samples_carry_their_occupancy(tmp_path) -> None:
    result = asyncio.run(camp.run_campaign(FakeEngine(), _config()))
    camp.write_result(tmp_path, result)

    report = json.loads((tmp_path / "campaign.json").read_text())
    counts = report["grid_samples_at_stated_concurrency"]
    assert counts == camp._steady_counts(result.observations)
    assert set(counts) == {"p64_o32_c1", "p64_o32_c2"}
    for kept_of_total in counts.values():
        kept, total = (int(x) for x in kept_of_total.split("/"))
        assert 0 < kept <= total

    rows = [json.loads(line) for line in (tmp_path / "observations.jsonl").read_text().splitlines()]
    assert rows and all(r["occupancy_mean"] is not None for r in rows)
    grid = [r for r in rows if r["segment"] == "grid" and r["concurrency"] == 1]
    assert all(r["occupancy_mean"] == pytest.approx(1.0) for r in grid)
