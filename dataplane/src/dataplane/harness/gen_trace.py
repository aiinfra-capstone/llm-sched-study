"""F-16 — the seeded trace generator.

    config -> SeedSequence(gen_seed).spawn(3)
                +- rng_arrival  -> MMPP / Poisson offsets
                +- rng_length   -> bucket draws + priority draws
                +- rng_content  -> per-request content_seed
             -> sort by offset -> assign req_ids -> write header + records -> sha256

Three separate streams, so that changing the length distribution does not shift the
arrival process underneath you. That is not tidiness — an R-sweep that accidentally
re-rolls its own arrivals cannot attribute anything to R.

Single-threaded, no I/O in the sampling loop, **no wall-clock reads anywhere**. A trace
is a pure function of (config, seed), and its SHA-256 is its identity everywhere
downstream: the run manifest carries it (C-6) and the replay client verifies it before
t0. `tests/test_trace_determinism.py` is the guard, and it is a test rather than an
assumption because the usual way this breaks is float formatting, silently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["TRACE_SCHEMA_VERSION", "generate", "load"]

TRACE_SCHEMA_VERSION = 1

# Tokenizer facts per staged model. `vocab_size` is the sampling ceiling the materializer
# draws below; `reserved_ids_excluded` says whether that ceiling actually excludes the
# model's special tokens, which depends on where the model puts them:
#
#   Llama-3 / 3.2  specials at the TOP (128000-128255 of 128256) -> a ceiling excludes them
#   Mistral-v0.3   specials at the BOTTOM (<unk>=0, <s>=1, </s>=2) -> a ceiling cannot
#
# The false flag on Mistral is honest, not broken: with `ignore_eos` and a forced
# output_len, and no prompt ever decoded back to text, an id 2 inside a prompt changes
# nothing that is measured. A trace claiming `true` while sampling id 2 would be the
# actual problem. See harness/prompts.py for why excluding a low floor properly needs a
# C-2 field that does not exist yet.
# Every entry read out of the GGUF the node actually loads, never off a model card. The
# materializer samples ids below `vocab_size`, so a wrong number here fills prompts with ids
# the model does not have — and the trace stores seeds rather than tokens, so it would
# surface weeks later as strange prefill figures in someone else's plot.
#
# `reserved_ids_excluded` is True only where a *ceiling* can exclude the reserved block,
# which needs the specials to sit at the top of the vocabulary. Llama-3 and Granite keep
# theirs there (128000 of 128256; 100256 of 100352). Mistral, Gemma and LFM2 keep theirs at
# 0-7, and a ceiling cannot exclude a floor — LFM2 has reserved ids at *both* ends — so the
# honest value is False. Nothing measured changes either way: output length is forced with
# `ignore_eos` and no prompt is ever decoded back to text, so a reserved id inside a prompt
# is a token the model has and nothing more.
MODELS: dict[str, dict[str, Any]] = {
    "llama3-8b": {"vocab_size": 128000, "reserved_ids_excluded": True},
    "llama32-3b": {"vocab_size": 128000, "reserved_ids_excluded": True},
    "llama32-1b": {"vocab_size": 128000, "reserved_ids_excluded": True},
    "mistral-7b-v03": {"vocab_size": 32768, "reserved_ids_excluded": False},
    "gemma4-e4b": {"vocab_size": 262144, "reserved_ids_excluded": False},
    "granite4-h-tiny": {"vocab_size": 100256, "reserved_ids_excluded": True},
    "lfm2-2.6b": {"vocab_size": 65536, "reserved_ids_excluded": False},
}

# Header field order is fixed to match contracts/examples/trace.sample.jsonl. Byte-identical
# regeneration is a property of this module's output, so field order is part of the format.
_HEADER_FIELDS = (
    "record",
    "trace_schema",
    "gen_seed",
    "n_requests",
    "duration_s",
    "arrival",
    "length_dist",
    "priority_mix",
    "admissible",
    "vocab_size",
    "reserved_ids_excluded",
    "generator_git_sha",
)

# "p512_o128" -> prompt_len 512, output_len 128. The bucket id IS the length pair; there
# is no second table to fall out of sync with.
_BUCKET_RE = re.compile(r"^p(\d+)_o(\d+)$")

# The one line in the file whose formatting is load-bearing. 4 decimal places, fixed by
# the C-2 schema, because "%r" of a float is where byte-stability goes to die.
_REQ_LINE = (
    '{{"record":"req","req_id":"{req_id}","arrival_offset_s":{offset:.4f},'
    '"prompt_len":{prompt_len},"output_len":{output_len},"bucket_id":"{bucket_id}",'
    '"priority":{priority},"content_seed":{content_seed}}}'
)


def _generator_git_sha() -> str:
    """The generator's own commit, resolved from this file's repo — not the caller's cwd.

    Recorded in the header so a trace can be traced back to the code that produced it
    (F-20). Falls back to "unknown" rather than raising: an unversioned checkout should
    still be able to generate a trace, it just cannot claim provenance for it.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip() or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _check_model(config: dict[str, Any]) -> None:
    """If a config names a model, its tokenizer facts must match the table.

    A config saying `mistral-7b-v03` while carrying Llama-3's vocab_size would produce
    prompts full of ids the model does not have, and nothing downstream would notice: the
    trace stores seeds, not tokens, so the mistake surfaces only as strange prefill
    numbers, weeks later, in someone else's figure.
    """
    name = config.get("model")
    if name is None:
        return
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known: {sorted(MODELS)}")
    for key, expected in MODELS[name].items():
        actual = config.get(key)
        if actual is not None and actual != expected:
            raise ValueError(
                f"config says model={name!r} but {key}={actual!r}; the table says {expected!r}"
            )


def _parse_bucket(bucket_id: str) -> tuple[int, int]:
    m = _BUCKET_RE.match(bucket_id)
    if m is None:
        raise ValueError(f"bucket_id {bucket_id!r} is not of the form p<prompt>_o<output>")
    return int(m.group(1)), int(m.group(2))


def _arrival_offsets(
    rng: np.random.Generator, arrival: dict, n_cap: int, duration_s: float
) -> list[float]:
    """Arrival offsets from a Poisson or 2-state Markov-modulated Poisson process.

    Stops at whichever bound binds first: `n_requests` or `duration_s`. `n_requests` in
    the config is therefore a CAP; the header records how many were actually written, so
    a header is always self-consistent with the body beneath it.

    The MMPP switch is exact rather than approximate: when the next interarrival would
    cross a state boundary, time advances to the boundary and the draw is retaken under
    the new rate. The exponential is memoryless, so this is the same process — and `t`
    strictly increases on that path, so the loop terminates.
    """
    process = arrival["process"]
    offsets: list[float] = []
    t = 0.0

    if process == "poisson":
        rate = float(arrival["lambda_base"])
        while len(offsets) < n_cap:
            t += float(rng.exponential(1.0 / rate))
            if t > duration_s:
                break
            offsets.append(t)
        return offsets

    if process != "mmpp":
        raise ValueError(f"unknown arrival process {process!r} (expected 'poisson' or 'mmpp')")

    rates = {"quiet": float(arrival["lambda_base"]), "burst": float(arrival["burst_lambda"])}
    dwell = {"quiet": float(arrival["quiet_mean_s"]), "burst": float(arrival["burst_mean_s"])}
    state = "quiet"  # every trace starts quiet, by convention
    state_end = t + float(rng.exponential(dwell[state]))

    while len(offsets) < n_cap:
        dt = float(rng.exponential(1.0 / rates[state]))
        if t + dt > state_end:
            t = state_end
            state = "burst" if state == "quiet" else "quiet"
            state_end = t + float(rng.exponential(dwell[state]))
            if t > duration_s:
                break
            continue
        t += dt
        if t > duration_s:
            break
        offsets.append(t)
    return offsets


def generate(config: dict[str, Any], path: str | Path) -> str:
    """Write a C-2 trace file and return its SHA-256.

    The return value is the trace's identity: it goes into `manifest.trace_sha256`, and
    the replay client refuses to start against a file whose hash does not match.
    """
    path = Path(path)
    _check_model(config)
    seed = int(config["gen_seed"])
    duration_s = float(config["duration_s"])
    vocab_size = int(config["vocab_size"])
    admissible = config["admissible"]

    # Validated before a single arrival is drawn. F-13 is a property of the *config*, not
    # of the requests that happened to be sampled from it, and checking it after the
    # arrival process means a config with an inadmissible bucket passes silently whenever
    # Poisson happens to return zero arrivals — and reports the wrong reason when it does
    # not. This block draws from no RNG stream, so hoisting it cannot move a trace byte.
    buckets: list[str] = list(config["length_dist"]["buckets"])
    weights = np.asarray(config["length_dist"]["weights"], dtype=float)
    lengths = [_parse_bucket(b) for b in buckets]
    for bucket_id, (p_len, o_len) in zip(buckets, lengths, strict=True):
        if p_len < 1 or o_len < 1:
            # C-2 puts a minimum of 1 on both, so a zero-length bucket writes a trace that
            # hashes, replays, and then fails `contracts/check.py` — after the run. It is
            # also meaningless: a zero-token request measures nothing but RPC overhead.
            raise ValueError(f"bucket {bucket_id!r} has a zero length; both must be >= 1")
        if p_len > admissible["max_prompt"] or o_len > admissible["max_output"]:
            raise ValueError(f"bucket {bucket_id!r} exceeds the F-13 admissible envelope")

    rng_arrival, rng_length, rng_content = (
        np.random.default_rng(s) for s in np.random.SeedSequence(seed).spawn(3)
    )

    offsets = sorted(
        _arrival_offsets(rng_arrival, config["arrival"], int(config["n_requests"]), duration_s)
    )
    n = len(offsets)
    if n == 0:
        raise ValueError("arrival process produced no requests; check lambda and duration_s")

    # Bulk draws, in a fixed order, from the length stream only — never from the arrival
    # stream, which is what keeps offsets invariant under a change of length_dist.
    bucket_idx = rng_length.choice(len(buckets), size=n, p=weights / weights.sum())
    prio_keys = sorted(config["priority_mix"], key=int)
    prio_w = np.asarray([config["priority_mix"][k] for k in prio_keys], dtype=float)
    prio_idx = rng_length.choice(len(prio_keys), size=n, p=prio_w / prio_w.sum())
    content_seeds = rng_content.integers(0, 2**31 - 1, size=n, dtype=np.int64)

    header = {
        "record": "header",
        "trace_schema": TRACE_SCHEMA_VERSION,
        "gen_seed": seed,
        "n_requests": n,  # what was written, not what was asked for
        "duration_s": duration_s,
        "arrival": config["arrival"],
        "length_dist": config["length_dist"],
        "priority_mix": config["priority_mix"],
        "admissible": admissible,
        "vocab_size": vocab_size,
        # A claim about vocab_size, not a filter applied anywhere. Honest per model —
        # see MODELS above and the docstring in harness/prompts.py.
        "reserved_ids_excluded": bool(config.get("reserved_ids_excluded", True)),
        "generator_git_sha": _generator_git_sha(),
    }
    assert tuple(header) == _HEADER_FIELDS, "header field order drifted from the C-2 sample"

    lines = [json.dumps(header, separators=(",", ":"), sort_keys=False)]
    lines += [
        _REQ_LINE.format(
            req_id=f"r{i:06d}",
            offset=offset,
            prompt_len=lengths[b][0],
            output_len=lengths[b][1],
            bucket_id=buckets[b],
            priority=int(prio_keys[p]),
            content_seed=int(cs),
        )
        for i, (offset, b, p, cs) in enumerate(
            zip(offsets, bucket_idx, prio_idx, content_seeds, strict=True), start=1
        )
    ]

    blob = ("\n".join(lines) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def load(path: str | Path, expect_sha256: str | None = None) -> tuple[dict, list[dict]]:
    """Read a trace back, verifying the schema version and (optionally) the hash.

    §12.2: reject an unknown `trace_schema` loudly rather than defaulting. A loader that
    guesses is how a format change becomes a silently wrong figure six weeks later.
    """
    path = Path(path)
    blob = path.read_bytes()

    if expect_sha256 is not None:
        actual = hashlib.sha256(blob).hexdigest()
        if actual != expect_sha256:
            raise ValueError(
                f"trace sha256 mismatch: file is {actual}, manifest says {expect_sha256}"
            )

    records = [json.loads(line) for line in blob.decode().splitlines() if line.strip()]
    if not records or records[0].get("record") != "header":
        raise ValueError(f"{path}: first line is not a header record")

    header, body = records[0], records[1:]
    if header["trace_schema"] != TRACE_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: trace_schema {header['trace_schema']} is not supported "
            f"(this build reads {TRACE_SCHEMA_VERSION}); refusing to guess"
        )
    if len(body) != header["n_requests"]:
        raise ValueError(
            f"{path}: header claims {header['n_requests']} requests, file has {len(body)}"
        )
    return header, body


def main() -> int:
    ap = argparse.ArgumentParser(description="F-16 — generate a seeded, byte-reproducible trace")
    ap.add_argument("config", type=Path, help="JSON trace config")
    ap.add_argument("-o", "--out", type=Path, required=True, help="output .jsonl path")
    ap.add_argument("--seed", type=int, help="override gen_seed from the config")
    ap.add_argument(
        "--model",
        choices=sorted(MODELS),
        help="set vocab_size and reserved_ids_excluded from the tokenizer table. The "
        "model is held constant within a pool (F-9); this picks which run set the trace "
        "belongs to, not a per-node knob.",
    )
    args = ap.parse_args()

    config = json.loads(args.config.read_text())
    if args.seed is not None:
        config["gen_seed"] = args.seed
    if args.model is not None:
        config = {**config, "model": args.model, **MODELS[args.model]}

    sha = generate(config, args.out)
    header, body = load(args.out, expect_sha256=sha)
    model = config.get("model", "unspecified")
    print(f"{args.out}  {len(body)} requests over {header['duration_s']}s  model={model}")
    print(f"sha256  {sha}")
    print("        ^ this is the trace's identity: put it in manifest.trace_sha256")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
