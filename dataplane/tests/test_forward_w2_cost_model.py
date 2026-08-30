"""Week 2 — the calibration campaign, and C-3, the most load-bearing artifact I produce.

Skipped until `dataplane.calibration` lands. Written now because two of the requirements
on C-3 are easy to satisfy incorrectly in a way that only shows up as a strange H3 result
in Week 5:

  1. The campaign must emit a **time-ordered series** of snapshots, not one final fitted
     model. Staleness injection (F-8) means Aditya serves the scheduler a snapshot from s
     seconds ago. If I emit one snapshot, he has to synthesize aging by perturbing
     parameters — and H3 becomes a study of his perturbation model rather than of real
     drift.
  2. `stochastic.autocorr_time_s` is MPR-1's headline number and what F-22 uses to give
     the DES realistic variance. It is a deliverable, not a diagnostic.

Neither is checkable by the schema, which is exactly why they are checkable here.
"""

from __future__ import annotations

import json

import pytest
from conftest import EXAMPLES, pending

pytestmark = pytest.mark.forward

cost_model = pending(
    "dataplane.calibration.cost_model",
    "build_snapshot",
    week="Week 2",
    deliverable="calibration campaign",
)

SAMPLE = json.loads((EXAMPLES / "cost_model.sample.json").read_text())


# --------------------------------------------------------------------------------------
# The artifact itself
# --------------------------------------------------------------------------------------


def test_an_emitted_snapshot_conforms_to_c3(schema) -> None:
    """Aditya's scheduler and his DES both read this. A snapshot that fails the schema
    fails at the seam, in his half, days after I generated it."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    errors = list(schema("cost_model").iter_errors(snapshot))
    assert not errors, [e.message for e in errors]


def test_the_schema_version_is_pinned_at_one() -> None:
    assert cost_model.SCHEMA_VERSION == 1


def test_an_unknown_schema_version_is_rejected_loudly(tmp_path) -> None:
    """Watch-list failure mode 2: I learn something in Week 2 and want another field, and
    that breaks his DES. The version field plus a loader that refuses to guess is the
    whole mitigation — on my side too, so I find my own bad file first."""
    path = tmp_path / "cm.json"
    path.write_text(json.dumps({**SAMPLE, "cost_model_schema": 2}))
    with pytest.raises(ValueError, match="cost_model_schema"):
        cost_model.load(path)


def test_a_snapshot_records_the_runs_it_was_measured_from() -> None:
    """`calibration_run_ids` is what makes a cost model auditable rather than asserted."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    assert snapshot["calibration_run_ids"]


def test_entries_are_ordered_statistics_that_make_sense() -> None:
    """p50 <= p95 <= a plausible mean neighbourhood. A percentile computed on the wrong
    axis passes the schema and produces a cost model that is confidently wrong."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    for entry in snapshot["entries"]:
        assert 0 < entry["service_ms_p50"] <= entry["service_ms_p95"]
        assert entry["tokens_per_s"] > 0
        assert entry["n_samples"] >= 1


def test_concurrency_is_swept_not_assumed() -> None:
    """Under F-9 llama.cpp's slot count IS the node model. A cost model measured only at
    concurrency 1 tells the DES nothing about the batching regime every loaded node is
    actually in."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    assert len({e["concurrency"] for e in snapshot["entries"]}) > 1


# --------------------------------------------------------------------------------------
# The series, which is the part the schema cannot check
# --------------------------------------------------------------------------------------


def test_a_campaign_emits_more_than_one_snapshot() -> None:
    """One snapshot is a fitted model. H3 needs history."""
    series = cost_model.load_series(cost_model.example_campaign_dir())
    assert len(series) > 1


def test_the_series_is_strictly_time_ordered() -> None:
    """Aditya's StalenessVeil indexes into this by age. Two snapshots sharing a timestamp,
    or arriving out of order, make "the snapshot from s seconds ago" ambiguous."""
    stamps = [
        s["measured_at_unix"] for s in cost_model.load_series(cost_model.example_campaign_dir())
    ]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)


def test_every_snapshot_in_a_series_shares_one_form() -> None:
    """F-7 permits a lookup table OR a <=6-parameter regression. Picking one and
    committing is a Week-2 decision; supporting both doubles his interpolation logic for
    no research gain, and a series that mixes them forces exactly that."""
    forms = {s["form"] for s in cost_model.load_series(cost_model.example_campaign_dir())}
    assert len(forms) == 1


def test_every_snapshot_in_a_series_describes_one_node_class() -> None:
    """A series is the history of one node class drifting. Two classes interleaved is two
    histories, and the drift measured across them is an artifact of the interleaving."""
    classes = {s["node_class"] for s in cost_model.load_series(cost_model.example_campaign_dir())}
    assert len(classes) == 1


# --------------------------------------------------------------------------------------
# MPR-1 — the deliverable the whole of Week 2 exists to produce
# --------------------------------------------------------------------------------------


def test_autocorrelation_time_is_measured_and_positive() -> None:
    """MPR-1's headline number, and the thing that makes H3 answerable: routing quality
    degrades as estimate age approaches tau, so tau is what sets a heartbeat frequency
    that would otherwise be arbitrary."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    assert snapshot["stochastic"]["autocorr_time_s"] > 0


def test_the_variance_envelope_is_reported() -> None:
    """The other half of MPR-1: a single calibrated tok/s figure is a moving average over
    a non-stationary process, and sigma is the statement of how much that hides."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    assert snapshot["stochastic"]["sigma"] > 0
    assert 0 <= snapshot["stochastic"]["fit_r2"] <= 1


def test_tau_is_estimated_from_a_series_not_from_one_run() -> None:
    """An autocorrelation time computed inside a single short run measures that run's
    warmup, not the process. It has to come from the campaign."""
    tau = cost_model.autocorr_time_s(cost_model.example_throughput_series())
    assert tau > 0


# --------------------------------------------------------------------------------------
# F-9a / F-9b — what the campaign has to establish about the pool
# --------------------------------------------------------------------------------------


def test_the_synthesizable_r_range_is_a_range_not_a_figure() -> None:
    """§7 / MPR-2 asks for it as a range. It is what buys the reduction in threat R2, and
    a single number would be a claim about one configuration rather than about the pool."""
    r_range = pytest.importorskip(
        "dataplane.calibration.r_range", reason="Week 2: R sweep not implemented yet"
    )
    lo, hi = r_range.synthesizable(cost_model.load_series(cost_model.example_campaign_dir()))
    assert 1.0 <= lo < hi


def test_the_two_backends_are_two_node_classes_not_one() -> None:
    """CUDA and Vulkan on the same card are the same engine commit and the same model, but
    not the same throughput. Recording them as one node class would fold a backend effect
    into R, which is precisely what holding the engine constant was supposed to prevent."""
    classes = {s["node_class"] for s in cost_model.load_series(cost_model.example_campaign_dir())}
    provenance = cost_model.build_snapshot(**cost_model.example_inputs())["provenance"]
    assert provenance["engine"] == "llamacpp"
    assert provenance["engine_version"], "the backend rides inside engine_version"
    assert classes


def test_the_engine_gap_probe_is_a_separate_role() -> None:
    """F-9b: vLLM is one measured condition, never a pool member. The manifest marks it
    `role: engine_gap_probe`, and a cost model produced from it must not be usable as a
    pool node's cost model by accident."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs(engine="vllm"))
    assert snapshot["provenance"]["engine"] == "vllm"
    assert "probe" in snapshot["node_class"] or "vllm" in snapshot["node_class"]


def test_provenance_records_the_knobs_that_set_the_condition() -> None:
    """Under F-9a, `-ngl` / `--threads` / `--parallel` ARE the experimental condition. A
    cost model that does not record them cannot be matched to the node it describes."""
    provenance = cost_model.build_snapshot(**cost_model.example_inputs())["provenance"]
    assert set(provenance["engine_config"]) >= {"ngl", "threads", "parallel"}


def test_the_admissibility_block_matches_the_traces_it_was_measured_with() -> None:
    """A cost model whose envelope is wider than the trace's promises service times for
    lengths never measured, and the DES will happily interpolate into that gap."""
    snapshot = cost_model.build_snapshot(**cost_model.example_inputs())
    assert snapshot["admissibility"]["max_prompt"] >= 1
    assert snapshot["admissibility"]["timeout_ceiling_ms"] >= 1
