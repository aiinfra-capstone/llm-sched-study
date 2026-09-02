#!/bin/bash
set -euo pipefail

# F-20: the dispatch sequence is a property of (trace, seed, policy) and of nothing else.
# Two SimApp runs into separate directories from identical inputs with --deterministic,
# then compare (decision_seq, req_id, chosen_node). `decide_duration_ns` is wall clock and
# is deliberately not compared.

echo "Running determinism test (F-20)..."

TRACE="runs/traces/anchor_1b.trace.jsonl"

# `runs/**` is gitignored, so a fresh checkout has no trace. Regenerate it from the
# committed C-2 config rather than falling back to contracts/examples: that sample manifest
# names cost model snapshot cm_rtx3090_ngl99_p4_q4km_20260428T1412Z, which is illustrative
# and has never existed under contracts/cost_models/, so SimApp refuses it and the run fails
# for a reason that has nothing to do with determinism.
uv run --project dataplane python tools/ensure_trace.py \
  --config dataplane/configs/trace_anchor_1b.json \
  --out "$TRACE" \
  --anchors runs/anchors

MANIFEST=$(ls -d runs/anchors/anchor1b_light_*/manifest.json 2>/dev/null | head -1 || true)
if [ -z "$MANIFEST" ]; then
  echo "FAIL: no light anchor manifest under runs/anchors"
  exit 1
fi
echo "Manifest: $MANIFEST"

OUT1=$(mktemp -d)
OUT2=$(mktemp -d)
LOG1=$(mktemp)
LOG2=$(mktemp)
trap 'rm -rf "$OUT1" "$OUT2" "$LOG1" "$LOG2"' EXIT

echo "Building control plane..."
pushd controlplane > /dev/null
mvn -q compile -DskipTests
popd > /dev/null

# `mvn exec:java` exits 0 even when the exec'd main catches its own exception, so the exit
# code alone is not evidence that a simulation ran. Two further checks: SimApp prints
# "Error during simulation:" on that path, and a run that reached the end always leaves a
# scheduler log. Without them a failed simulation surfaces later as "no scheduler log",
# which points at the comparison step instead of at the stack trace that caused it.
run_sim() {
  local out=$1 log=$2
  if ! mvn -q -f controlplane/pom.xml exec:java \
        -Dexec.mainClass=com.sched.sim.SimApp \
        -Dexec.args="$TRACE $MANIFEST $out --deterministic --cost-models contracts/cost_models" \
        > "$log" 2>&1; then
    echo "FAIL: SimApp exited non-zero"
    tail -n 20 "$log"
    return 1
  fi
  if grep -q "Error during simulation" "$log"; then
    echo "FAIL: SimApp reported an error"
    grep -A 4 "Error during simulation" "$log" | head -n 10
    return 1
  fi
  if ! compgen -G "$out/scheduler_*.jsonl" > /dev/null; then
    echo "FAIL: SimApp produced no scheduler log in $out"
    tail -n 20 "$log"
    return 1
  fi
}

echo "Run 1 -> $OUT1"
run_sim "$OUT1" "$LOG1"
echo "Run 2 -> $OUT2"
run_sim "$OUT2" "$LOG2"

echo "Comparing dispatch sequences (decision_seq, req_id, chosen_node)..."
python3 - "$OUT1" "$OUT2" << 'PY'
import glob
import json
import pathlib
import sys


def load_triples(outdir):
    files = sorted(glob.glob(str(pathlib.Path(outdir) / "scheduler_*.jsonl")))
    if not files:
        print(f"FAIL: no scheduler log in {outdir}")
        sys.exit(1)
    path = files[0]
    triples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") != "decision":
                continue
            triples.append((rec["decision_seq"], rec["req_id"], rec["chosen_node"]))
    triples.sort(key=lambda x: x[0])
    return triples, path


t1, p1 = load_triples(sys.argv[1])
t2, p2 = load_triples(sys.argv[2])
if len(t1) == 0:
    print("FAIL: no decision records found")
    sys.exit(1)
if t1 != t2:
    print(f"FAIL: dispatch sequences differ ({len(t1)} vs {len(t2)} records)")
    for a, b in zip(t1, t2):
        if a != b:
            print(f"  first mismatch: {a} vs {b}")
            break
    sys.exit(1)
print(f"OK: {len(t1)} decisions identical across runs")
print(f"  {p1} vs {p2}")
PY

echo "Determinism test passed."
