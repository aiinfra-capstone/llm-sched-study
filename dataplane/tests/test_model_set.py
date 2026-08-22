"""The staged model set — variety across run sets, never inside a pool.

A reviewer asking "is this a scheduling result or a Llama-3 result?" is asking for
replication at another model. What they must NOT be given is a pool running two models:
that would confound R with a model effect, and neither the H1 2x2 decomposition nor the
R-sweep can separate those afterwards. `manifest.nodes[].model` is what makes the
constant-within-a-pool claim auditable; these tests guard the trace side of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataplane.harness import gen_trace
from dataplane.harness.gen_trace import MODELS
from dataplane.harness.prompts import materialize

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"

BASE = {
    "gen_seed": 5,
    "n_requests": 20,
    "duration_s": 60,
    "arrival": {"process": "poisson", "lambda_base": 5.0},
    "length_dist": {"buckets": ["p128_o64", "p512_o128"], "weights": [0.5, 0.5]},
    "priority_mix": {"0": 0.7, "1": 0.3},
    "admissible": {"max_prompt": 2048, "max_output": 256, "timeout_ceiling_ms": 60000},
}


@pytest.mark.parametrize("model", sorted(MODELS))
def test_every_model_produces_a_conformant_trace(model: str, tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    config = {**BASE, "model": model, **MODELS[model]}
    path = tmp_path / f"{model}.jsonl"
    gen_trace.generate(config, path)

    validator = jsonschema.Draft202012Validator(
        json.loads((CONTRACTS / "schemas" / "trace.schema.json").read_text())
    )
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        errors = list(validator.iter_errors(json.loads(line)))
        assert not errors, f"{model}:{lineno}: {[e.message for e in errors]}"

    header, _ = gen_trace.load(path)
    assert header["vocab_size"] == MODELS[model]["vocab_size"]
    assert header["reserved_ids_excluded"] is MODELS[model]["reserved_ids_excluded"]


def test_mistral_does_not_claim_its_specials_are_excluded(tmp_path: Path) -> None:
    """Mistral keeps <unk>/<s>/</s> at 0-2, so a ceiling cannot exclude them.

    `false` here is the honest answer and costs nothing measured — the worker forces
    output_len with ignore_eos and no prompt is decoded back to text. A trace claiming
    `true` while sampling id 2 is the thing that would mislead the next reader.
    """
    model = "mistral-7b-v03"
    path = tmp_path / "m.jsonl"
    gen_trace.generate({**BASE, "model": model, **MODELS[model]}, path)

    header, _ = gen_trace.load(path)
    assert header["reserved_ids_excluded"] is False


@pytest.mark.parametrize("model", sorted(MODELS))
def test_prompts_stay_inside_each_model_vocabulary(model: str, tmp_path: Path) -> None:
    vocab = MODELS[model]["vocab_size"]
    path = tmp_path / f"{model}.jsonl"
    gen_trace.generate({**BASE, "model": model, **MODELS[model]}, path)

    _, body = gen_trace.load(path)
    for record in body[:5]:
        tokens = materialize(record["content_seed"], record["prompt_len"], vocab)
        assert max(tokens) < vocab and min(tokens) >= 0


def test_config_contradicting_the_tokenizer_table_is_refused(tmp_path: Path) -> None:
    """Mistral's vocab is 32768. A config claiming Llama-3's would be silently wrong."""
    config = {
        **BASE,
        "model": "mistral-7b-v03",
        "vocab_size": 128000,
        "reserved_ids_excluded": False,
    }
    with pytest.raises(ValueError, match="the table says"):
        gen_trace.generate(config, tmp_path / "bad.jsonl")


def test_unknown_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown model"):
        gen_trace.generate({**BASE, "model": "gpt-9", "vocab_size": 1000}, tmp_path / "x.jsonl")
