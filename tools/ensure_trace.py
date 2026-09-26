#!/usr/bin/env python3
"""Put the anchor trace back on disk so the cross-seam jobs have something to replay.

`runs/**` is gitignored, so a fresh checkout carries no trace and both cross-seam
scripts have nothing to hand SimApp. Committing 32 KB of JSONL would be the wrong fix.
`dataplane/configs/trace_anchor_1b.json` is committed, and the anchors record the
generator commit that wrote their trace, so the file comes back byte for byte.

A trace's SHA-256 identifies (config, seed, generator commit): `gen_trace` stamps
`generator_git_sha` into the header, and the header sits inside the hashed blob. So I
regenerate at the commit the anchors recorded (`config.generator_git_sha`), and the hash
check then proves the generator still writes the same stream. The checks, in order:

  1. the trace config still agrees with the anchor manifests that were replayed against
     it, on the fields that decide the request stream;
  2. the regenerated trace hashes to one of the anchors' `trace_sha256`. On a mismatch
     nothing is written and the exit code is 1.

When a trace is already on disk it is never overwritten. Its hash is checked against the
anchors, and a trace they did not replay is a stop, not a warning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

# The fields that decide which requests exist and when they arrive. `duration_s` is
# deliberately absent: a manifest records the replayed duration after `rate_scale` has
# compressed the timeline (193.04 s for the light anchor), while the trace config records
# the 222 s the generator actually drew over. Comparing the two would fail every time.
STREAM_FIELDS = ("gen_seed", "arrival", "length_dist")


def stream_mismatches(config: dict, manifest: dict) -> list[str]:
    """Fields where a manifest's recorded config disagrees with the committed trace config.

    This is the guard that makes regeneration safe. Editing `trace_anchor_1b.json` would
    otherwise silently validate the simulator against a different arrival process from the
    one the hardware anchors were collected under, and F-23 would compare two unrelated
    runs while reporting a percentage.
    """
    recorded = manifest.get("config", {})
    out = []
    for field in STREAM_FIELDS:
        if field not in recorded:
            continue
        if recorded[field] != config.get(field):
            out.append(
                f"{field}: config has {config.get(field)!r}, manifest has {recorded[field]!r}"
            )
    return out


class TraceMismatch(ValueError):
    """The trace on disk, or the one regenerated, is not the one the manifests replayed."""


def recorded_generator_sha(manifests: list[dict]) -> str:
    """The generator commit every manifest recorded for its trace. They must agree."""
    shas = {m.get("config", {}).get("generator_git_sha") for m in manifests}
    if None in shas:
        raise ValueError("a manifest records no config.generator_git_sha")
    if len(shas) != 1:
        raise ValueError(f"the manifests record different generator_git_sha: {sorted(shas)}")
    return shas.pop()


def verify_present(out: Path, manifests: list[dict]) -> str:
    """Hash a trace already on disk and require it to be one the manifests name."""
    actual = hashlib.sha256(out.read_bytes()).hexdigest()
    expected = {m["trace_sha256"] for m in manifests}
    if actual not in expected:
        raise TraceMismatch(
            f"{out} has sha256 {actual[:12]}, the manifests name "
            f"{sorted(e[:12] for e in expected)}; left as it is"
        )
    return actual


def ensure(config: dict, out: Path, manifests: list[dict]) -> str:
    """Return the sha256 of a trace at `out` that the manifests replayed.

    A trace already there is only checked. A missing one is generated at the recorded
    generator commit into a temporary file, and moved into place only when its hash is one
    the manifests name, so a mismatch leaves nothing behind.
    """
    if out.exists():
        return verify_present(out, manifests)
    generator_git_sha = recorded_generator_sha(manifests)
    expected = {m["trace_sha256"] for m in manifests}

    # Imported here rather than at module scope so that the checks in main still run under
    # a bare `python3`, and only the generation step needs the dataplane environment.
    from dataplane.harness.gen_trace import generate

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        staged = Path(tmp) / out.name
        sha = generate(config, staged, generator_git_sha=generator_git_sha)
        if sha not in expected:
            raise TraceMismatch(
                f"regenerated at {generator_git_sha}, the trace hashes to {sha[:12]}, the "
                f"manifests name {sorted(e[:12] for e in expected)}; nothing written"
            )
        os.replace(staged, out)
    return sha


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the anchor trace for cross-seam CI")
    ap.add_argument("--config", type=Path, required=True, help="committed C-2 trace config")
    ap.add_argument("--out", type=Path, required=True, help="where the trace should live")
    ap.add_argument("--anchors", type=Path, required=True, help="directory of anchor run dirs")
    args = ap.parse_args()

    manifests = sorted(args.anchors.glob("*/manifest.json"))
    if not manifests:
        print(f"no anchor manifests under {args.anchors}", file=sys.stderr)
        return 1
    loaded = [json.loads(p.read_text()) for p in manifests]

    if args.out.exists():
        try:
            sha = verify_present(args.out, loaded)
        except TraceMismatch as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 1
        print(f"trace present and matches the anchors: {args.out} {sha[:12]}")
        return 0

    config = json.loads(args.config.read_text())
    for path, manifest in zip(manifests, loaded, strict=True):
        bad = stream_mismatches(config, manifest)
        if bad:
            print(
                f"{args.config} no longer describes the stream {path} was replayed against:",
                file=sys.stderr,
            )
            for line in bad:
                print(f"  {line}", file=sys.stderr)
            return 1

    from dataplane.harness.gen_trace import load

    try:
        sha = ensure(config, args.out, loaded)
    except (TraceMismatch, ValueError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    header, body = load(args.out)
    print(f"regenerated {args.out} from {args.config}")
    print(f"  {len(body)} requests over {header['duration_s']}s, gen_seed {header['gen_seed']}")
    print(f"  sha256 {sha[:12]} at generator {header['generator_git_sha']}, as the anchors name")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
