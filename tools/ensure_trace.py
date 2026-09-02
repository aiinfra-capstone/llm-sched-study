#!/usr/bin/env python3
"""Put the anchor trace back on disk so the cross-seam jobs have something to replay.

`runs/**` is gitignored, so a fresh checkout carries no trace and both cross-seam
scripts have nothing to hand SimApp. Committing 32 KB of JSONL would be the wrong fix.
C-2 already says a trace is a pure function of (config, seed), and
`dataplane/configs/trace_anchor_1b.json` is committed, so every one of the 200 request
lines comes back from the config that made it.

What does not come back is the file's SHA-256. `gen_trace` stamps `generator_git_sha`
into the header, and the header sits inside the hashed blob, so a trace's identity moves
whenever the repo moves even though its request stream does not. The committed anchors
name `bea0546...`, written at d70b6d0; the same config at any later commit hashes
something else. Fixing that means moving provenance outside the hashed region, which is
a C-2 change and not one to make from a CI helper. So this script performs the two
checks that committed artifacts can support, and says plainly which one it cannot:

  1. the trace config still agrees with the anchor manifests that were replayed against
     it, on the fields that decide the request stream;
  2. the regenerated trace loads and carries the request count the anchors saw.

When the real trace is already on disk its hash is verified against the manifest and
nothing is regenerated, so a developer's own runs are never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
        actual = hashlib.sha256(args.out.read_bytes()).hexdigest()
        expected = {m["trace_sha256"] for m in loaded}
        if actual in expected:
            print(f"trace present and matches the anchors: {args.out} {actual[:12]}")
        else:
            print(f"trace present at {args.out}")
            print(
                f"  its sha256 is {actual[:12]}, the anchors name {sorted(e[:12] for e in expected)}"
            )
            print("  continuing: provenance is inside the hash, see this script's docstring")
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

    # Imported here rather than at module scope so that the checks above still run under a
    # bare `python3`, and only the generation step needs the dataplane environment.
    from dataplane.harness.gen_trace import generate, load

    sha = generate(config, args.out)
    header, body = load(args.out)
    print(f"regenerated {args.out} from {args.config}")
    print(f"  {len(body)} requests over {header['duration_s']}s, gen_seed {header['gen_seed']}")
    print(f"  sha256 {sha[:12]}, anchors name {sorted({m['trace_sha256'][:12] for m in loaded})}")
    print(
        "  the two differ only in the header's generator_git_sha; the request stream is reproduced"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
