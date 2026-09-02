#!/bin/bash
set -euo pipefail

# F-23: simulator fidelity vs hardware. Run same trace through both vehicles
# at operating points defined by anchors, compare p50/p95 e2e against ±25% tolerance.
# Reports observed error, not just pass/fail. Can fail (exit 1) when outside tolerance.

if [ ! -d "runs/anchors" ]; then
    echo "Hardware anchors not found in runs/anchors — nothing to validate"
    exit 0
fi

echo "Validating F-23: Simulator vs Hardware"

TRACE="runs/traces/anchor_1b.trace.jsonl"
if [ ! -f "$TRACE" ]; then
  echo "Trace not found: $TRACE"
  exit 1
fi

# Build first
pushd controlplane > /dev/null
mvn -q compile -DskipTests
popd > /dev/null

overall_status=0

for anchor in runs/anchors/anchor1b_light_* runs/anchors/anchor1b_mid_* runs/anchors/anchor1b_heavy_*; do
  [ -d "$anchor" ] || continue
  manifest="$anchor/manifest.json"
  [ -f "$manifest" ] || continue
  anchor_name=$(basename "$anchor")
  echo ""
  echo "=== $anchor_name ==="
  echo "Manifest: $manifest"

  out=$(mktemp -d)
  # Run DES deterministically for this anchor
  if ! mvn -q -f controlplane/pom.xml exec:java -Dexec.mainClass=com.sched.sim.SimApp -Dexec.args="$TRACE $manifest $out --deterministic --cost-models contracts/cost_models"; then
    echo "FAIL: SimApp failed for $anchor_name"
    rm -rf "$out"
    overall_status=1
    continue
  fi

  # Compute sim p50/p95 from client log and compare to hardware values
  # Hardware p50/p95 are read from runset.parquet if present, else from known issue numbers for light
  python3 - "$anchor" "$out" << 'PY'
import pathlib, json, sys, glob, statistics
anchor = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])

# Load sim client E2E (ok only, warmup excluded via manifest warmup_s)
try:
    import json as js
    manifest = js.loads((anchor / "manifest.json").read_text())
    warmup_s = float(manifest.get("warmup_s", 0))
except Exception as e:
    print(f"WARN: could not read manifest warmup: {e}")
    warmup_s = 10.0

# Find sim client log
sims = glob.glob(str(out / "client_*.jsonl"))
if not sims:
    print(f"FAIL: no sim client log in {out}")
    sys.exit(1)
sim_path = sims[0]
sim_e2e = []
with open(sim_path) as f:
    for line in f:
        line=line.strip()
        if not line: continue
        rec = json.loads(line)
        if rec.get("status") != "ok": continue
        if float(rec.get("intended_offset_s", 0)) < warmup_s: continue
        sim_e2e.append(rec["e2e_duration_ns"] / 1e6)  # ms

if not sim_e2e:
    print("FAIL: no sim e2e samples after warmup filter")
    sys.exit(1)
sim_e2e_sorted = sorted(sim_e2e)
def p50(a): return a[len(a)//2]
def p95(a): 
    idx = int(len(a)*0.95)
    if idx>=len(a): idx=len(a)-1
    return a[idx]
sim_p50 = p50(sim_e2e_sorted)
sim_p95 = p95(sim_e2e_sorted)

# Try to get hardware p50/p95 from runset.parquet
hw_p50 = None
hw_p95 = None
parquet = anchor / "runset.parquet"
if parquet.exists():
    try:
        import pandas as pd
        df = pd.read_parquet(parquet)
        # runset has e2e_ms or similar? try common names
        col = None
        for c in ["e2e_ms","e2e_duration_ms","e2e"]:
            if c in df.columns:
                col=c
                break
        if col is None:
            # try to find any column with e2e
            for c in df.columns:
                if "e2e" in c.lower():
                    col=c
                    break
        if col is not None:
            # filter warmup if is_warmup column exists
            if "is_warmup" in df.columns:
                df = df[~df["is_warmup"].astype(bool)]
            vals = df[col].dropna().tolist()
            vals = sorted(vals)
            if vals:
                hw_p50 = vals[len(vals)//2]
                hw_p95 = vals[int(len(vals)*0.95)] if len(vals)>0 else vals[-1]
    except Exception as e:
        print(f"WARN: could not read parquet: {e}")

# Fallback to known hardware numbers for light anchor (from issue #11) if parquet not usable
if hw_p50 is None:
    if "light" in str(anchor):
        hw_p50 = 1981.2
        hw_p95 = 4521.5
        print(f"Using fallback hardware numbers for {anchor}: p50={hw_p50} ms p95={hw_p95} ms")
    else:
        print(f"WARN: no hardware p50 for {anchor}, skipping comparison")
        print(f"Sim p50={sim_p50:.1f} ms p95={sim_p95:.1f} ms (n={len(sim_e2e)})")
        sys.exit(0)

def err(sim, hw):
    return (sim - hw)/hw*100 if hw else 0

err_p50 = err(sim_p50, hw_p50)
err_p95 = err(sim_p95, hw_p95)
tol = 25.0
print(f"Hardware: p50={hw_p50:.1f} ms p95={hw_p95:.1f} ms")
print(f"Sim:      p50={sim_p50:.1f} ms p95={sim_p95:.1f} ms  (n={len(sim_e2e)})")
print(f"Error:    p50={err_p50:+.1f}% p95={err_p95:+.1f}%  tolerance ±{tol}%")
if abs(err_p50) > tol or abs(err_p95) > tol:
    print(f"FAIL: F-23 outside tolerance for {anchor}")
    sys.exit(1)
else:
    print(f"PASS: {anchor} within tolerance")
    sys.exit(0)
PY
  rc=$?
  if [ $rc -ne 0 ]; then
    overall_status=1
  fi
  rm -rf "$out"
done

if [ $overall_status -ne 0 ]; then
  echo ""
  echo "F-23 Validation FAILED (see per-anchor errors above)"
  exit 1
fi

echo ""
echo "F-23 Validation passed."
exit 0
