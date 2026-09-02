#!/bin/bash
set -euo pipefail

# F-23: the simulator reproduces hardware p50 and p95 end-to-end latency to within +/-25%
# at three or more operating points. Every anchor is replayed through the DES on the same
# trace the hardware saw, and the observed error is printed whether or not it passes,
# because the observed error is itself a reported number in the writeup and R-2 turns on it.
#
# The tolerance and the three-point minimum are not invented here. They come from
# dataplane/src/dataplane/figures/plots.py (F23_TOLERANCE, MIN_VALIDATION_POINTS), which is
# the gate every simulated figure passes through.

TOLERANCE=25.0
MIN_POINTS=3

if [ ! -d "runs/anchors" ]; then
    echo "Hardware anchors not found in runs/anchors, nothing to validate"
    exit 0
fi
if [ ! -f "runs/anchors/load_band.json" ]; then
    echo "runs/anchors/load_band.json not found, no hardware baseline to compare against"
    exit 1
fi

echo "Validating F-23: Simulator vs Hardware"

TRACE="runs/traces/anchor_1b.trace.jsonl"

# `runs/**` is gitignored, so a fresh checkout has no trace and this job used to exit 1
# before it built anything. C-2 makes the trace regenerable from its committed config.
uv run --project dataplane python tools/ensure_trace.py \
  --config dataplane/configs/trace_anchor_1b.json \
  --out "$TRACE" \
  --anchors runs/anchors

echo "Building control plane..."
pushd controlplane > /dev/null
mvn -q compile -DskipTests
popd > /dev/null

overall_status=0
compared=0

# `mvn exec:java` exits 0 even when the exec'd main catches its own exception, so a failed
# simulation has to be recognised from its output and from the absence of a client log.
run_sim() {
  local manifest=$1 out=$2 log=$3
  if ! mvn -q -f controlplane/pom.xml exec:java \
        -Dexec.mainClass=com.sched.sim.SimApp \
        -Dexec.args="$TRACE $manifest $out --deterministic --cost-models contracts/cost_models" \
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
  if ! compgen -G "$out/client_*.jsonl" > /dev/null; then
    echo "FAIL: SimApp produced no client log in $out"
    tail -n 20 "$log"
    return 1
  fi
}

for anchor in runs/anchors/anchor1b_*/; do
  anchor=${anchor%/}
  manifest="$anchor/manifest.json"
  [ -f "$manifest" ] || continue
  echo ""
  echo "=== $(basename "$anchor") ==="
  echo "Manifest: $manifest"

  out=$(mktemp -d)
  log=$(mktemp)

  if ! run_sim "$manifest" "$out" "$log"; then
    overall_status=1
    rm -rf "$out" "$log"
    continue
  fi

  rc=0
  python3 tools/f23_compare.py \
    --manifest "$manifest" \
    --sim-dir "$out" \
    --load-band runs/anchors/load_band.json \
    --tolerance "$TOLERANCE" || rc=$?
  case $rc in
    0) compared=$((compared + 1)) ;;
    2) compared=$((compared + 1)); overall_status=1 ;;
    *) overall_status=1 ;;
  esac

  rm -rf "$out" "$log"
done

echo ""
if [ "$compared" -lt "$MIN_POINTS" ]; then
  echo "F-23 needs at least $MIN_POINTS operating points; only $compared were compared"
  overall_status=1
fi

if [ $overall_status -ne 0 ]; then
  echo "F-23 validation FAILED (see per-anchor errors above)"
  exit 1
fi

echo "F-23 validation passed at $compared operating points."
exit 0
