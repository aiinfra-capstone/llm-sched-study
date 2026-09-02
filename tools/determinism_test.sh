#!/bin/bash
set -euo pipefail

# F-20: dispatch sequence identical across vehicles / across repeated DES runs
# Two SimApp runs into different dirs, same trace+manifest+seed, --deterministic
# => compare (decision_seq, req_id, chosen_node) triples, ignoring decide_duration_ns

echo "Running determinism test (F-20)..."

TRACE="runs/traces/anchor_1b.trace.jsonl"
MANIFEST="runs/anchors/anchor1b_light_1788100627/manifest.json"
if [ ! -f "$TRACE" ]; then
  echo "Anchor trace not found, falling back to example trace"
  TRACE="contracts/examples/trace.sample.jsonl"
  MANIFEST="contracts/examples/manifest.sample.json"
fi
if [ ! -f "$MANIFEST" ]; then
  echo "FAIL: manifest not found: $MANIFEST"
  exit 1
fi

OUT1=$(mktemp -d)
OUT2=$(mktemp -d)
trap 'rm -rf "$OUT1" "$OUT2"' EXIT

echo "Building control plane..."
pushd controlplane > /dev/null
mvn -q compile -DskipTests
popd > /dev/null

run_sim() {
  local out=$1
  mvn -q -f controlplane/pom.xml exec:java -Dexec.mainClass=com.sched.sim.SimApp -Dexec.args="$TRACE $MANIFEST $out --deterministic --cost-models contracts/cost_models"
}

echo "Run 1 -> $OUT1"
run_sim "$OUT1"
echo "Run 2 -> $OUT2"
run_sim "$OUT2"

echo "Comparing dispatch sequences (decision_seq, req_id, chosen_node)..."
python3 - "$OUT1" "$OUT2" << 'PY'
import pathlib, json, sys, glob

def load_triples(outdir):
    files = glob.glob(str(pathlib.Path(outdir) / "scheduler_*.jsonl"))
    if not files:
        print(f"FAIL: no scheduler log in {outdir}")
        sys.exit(1)
    # take first scheduler file (run_id is manifest+_sim)
    path = files[0]
    triples = []
    with open(path) as f:
        for line in f:
            line=line.strip()
            if not line: continue
            rec=json.loads(line)
            if rec.get("type") != "decision":
                continue
            triples.append((rec["decision_seq"], rec["req_id"], rec["chosen_node"]))
    triples.sort(key=lambda x: x[0])
    return triples, path

t1, p1 = load_triples(sys.argv[1])
t2, p2 = load_triples(sys.argv[2])
if len(t1)==0:
    print("FAIL: no decision records found")
    sys.exit(1)
if t1 != t2:
    print(f"FAIL: dispatch sequences differ ({len(t1)} vs {len(t2)} records)")
    # show first mismatch
    for a,b in zip(t1,t2):
        if a!=b:
            print(f"  first mismatch: {a} vs {b}")
            break
    sys.exit(1)
print(f"OK: {len(t1)} decisions identical across runs")
print(f"  {p1} vs {p2}")
# Also ensure decide_duration_ns differs (it should, as it is wall-clock)
# but we ignore it for determinism, so no check needed
PY

echo "Determinism test passed."
