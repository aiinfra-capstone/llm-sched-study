#!/usr/bin/env python3
"""Write a C-2 trace config whose prompt and output lengths are heavy-tailed, not three buckets.

Every trace so far draws from three buckets within about 10% of each other, and output
length is forced exactly. That is the regime where a queue count tracks remaining work
best: every job is roughly the same size, so "how many are waiting" and "how much work is
waiting" say the same thing. Real conversational traffic has output lengths spread over
more than an order of magnitude with a long right tail, and that is where a count stops
tracking work.

This discretises independent lognormals for prompt and output length onto a grid of
buckets and writes them as a C-2 `length_dist`, so the trace generator, the replay client
and the join need no change. Output length is still forced per request, because the
measurement needs it; what changes is its spread. None of the five policies reads output
length, so to them the length is unknown at dispatch either way.

The parameters are chosen, not fitted to a public trace. They are clamped to the admissible
envelope (prompt <= 512, output <= 128 on the first pair), which cuts the far tail. A
calibration at a larger envelope is what lifts that clamp.

Usage:
  uv run --project dataplane python tools/make_length_mix.py --base dataplane/configs/trace_anchor_1b.json \\
      --prompt-median 160 --prompt-sigma 0.8 --output-median 32 --output-sigma 0.9 \\
      --out dataplane/configs/trace_heavytail_1b.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path

import numpy as np

PROMPT_GRID = (16, 32, 64, 128, 192, 256, 384, 512)
OUTPUT_GRID = (4, 8, 16, 24, 32, 48, 64, 96, 128)


def lognormal_weights(grid: tuple[int, ...], median: float, sigma: float) -> np.ndarray:
    """Probability mass of each grid value: the lognormal integrated between log-midpoints.

    The first and last cells take the tails below and above them, which is the clamp.
    """
    edges = [0.0]
    for a, b in itertools.pairwise(grid):
        edges.append(math.sqrt(a * b))
    edges.append(math.inf)
    mu = math.log(median)

    def cdf(x: float) -> float:
        if x <= 0:
            return 0.0
        if math.isinf(x):
            return 1.0
        return 0.5 * (1 + math.erf((math.log(x) - mu) / (sigma * math.sqrt(2))))

    return np.array([cdf(edges[i + 1]) - cdf(edges[i]) for i in range(len(grid))])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--base", type=Path, required=True, help="trace config to copy everything else from"
    )
    ap.add_argument("--prompt-median", type=float, required=True)
    ap.add_argument("--prompt-sigma", type=float, required=True)
    ap.add_argument("--output-median", type=float, required=True)
    ap.add_argument("--output-sigma", type=float, required=True)
    ap.add_argument(
        "--arrival", type=Path, help="JSON file with a C-2 arrival block to use instead"
    )
    ap.add_argument(
        "--min-weight", type=float, default=0.002, help="drop buckets lighter than this"
    )
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    base = json.loads(args.base.read_text())
    limit_p = base["admissible"]["max_prompt"]
    limit_o = base["admissible"]["max_output"]
    prompts = tuple(p for p in PROMPT_GRID if p <= limit_p)
    outputs = tuple(o for o in OUTPUT_GRID if o <= limit_o)
    wp = lognormal_weights(prompts, args.prompt_median, args.prompt_sigma)
    wo = lognormal_weights(outputs, args.output_median, args.output_sigma)
    joint = np.outer(wp, wo)
    buckets, weights = [], []
    for i, p in enumerate(prompts):
        for j, o in enumerate(outputs):
            if joint[i, j] >= args.min_weight:
                buckets.append(f"p{p}_o{o}")
                weights.append(round(float(joint[i, j]), 5))
    config = {
        **base,
        "_comment": (
            f"Written by tools/make_length_mix.py from {args.base.name}: prompt lognormal(median "
            f"{args.prompt_median:g}, sigma {args.prompt_sigma:g}), output lognormal(median "
            f"{args.output_median:g}, sigma {args.output_sigma:g}), clamped to {limit_p}/{limit_o}. "
            "Chosen parameters, not fitted to a public trace."
        ),
        "length_dist": {"buckets": buckets, "weights": weights},
    }
    if args.arrival:
        config["arrival"] = json.loads(args.arrival.read_text())
    total = sum(weights)
    mean_o = sum(w * int(b.split("_o")[1]) for b, w in zip(buckets, weights, strict=True)) / total
    tail = (
        sum(w for b, w in zip(buckets, weights, strict=True) if int(b.split("_o")[1]) >= 96) / total
    )
    args.out.write_text(json.dumps(config, indent=2) + "\n")
    print(
        f"{args.out}: {len(buckets)} buckets, mean output {mean_o:.1f} tokens, {tail:.1%} at 96 or more"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
