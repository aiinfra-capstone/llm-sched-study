#!/usr/bin/env bash
# llama-bench on one node, so the engine's raw prefill and decode speed can be checked against
# published numbers for the card before any more pairs are run.
#
# Why: the 1650 Ti's binary reports build 1, so it predates pool-install.sh's pinned build
# number and its CMake flags are unknown. It prefills a 1B Q4_K_M model at about 735 tok/s
# against 6000 to 8000 on the 3050. If part of that 10x is a build or kernel-path difference,
# it is an engine effect inside R, which F-9 exists to keep out. Run this on each node before
# and after rebuilding with tools/pool-install.sh; a large change in pp512 on the 1650 Ti
# means the first pair's R, and the runs on it, need redoing.
#
# Usage: tools/engine_bench.sh <build_dir> <model.gguf> <out.json>
#   e.g. tools/engine_bench.sh ~/opt/llama.cpp/b10569-cuda ~/models/gguf/Llama-3.2-1B-Instruct-Q4_K_M.gguf \
#        runs/bench/gtx1650ti_before_rebuild.json
# Stop llama-server first: the benchmark needs the GPU to itself.
set -euo pipefail

BUILD_DIR="${1:?build dir}"
MODEL="${2:?model path}"
OUT="${3:?output json}"
BENCH="$BUILD_DIR/bin/llama-bench"
[ -x "$BENCH" ] || { echo "no llama-bench at $BENCH; build it with tools/pool-install.sh" >&2; exit 1; }
if pgrep -x llama-server >/dev/null; then
  echo "llama-server is running; stop it so the benchmark has the GPU to itself" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp)"
"$BENCH" -m "$MODEL" -ngl 99 -t 6 -p 512 -n 128 -r 5 -o json > "$TMP"

python3 - "$TMP" "$OUT" "$BUILD_DIR" "$MODEL" <<'PY'
import hashlib, json, pathlib, platform, subprocess, sys
tmp, out, build, model = sys.argv[1:]
def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception:
        return ""
libs = sorted(pathlib.Path(build, "bin").glob("lib*.so"))
record = {
    "host": platform.node(),
    "gpu": sh("nvidia-smi --query-gpu=name,driver_version,power.limit --format=csv,noheader"),
    "llama_server_version": sh(f"{build}/bin/llama-server --version 2>&1 | head -2"),
    "cmake_cache": sh(f"grep -E '^(GGML_[A-Z_]+|CMAKE_CUDA_ARCHITECTURES|CMAKE_BUILD_TYPE|LLAMA_BUILD_NUMBER)[:=]' {build}/CMakeCache.txt"),
    "library_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in libs},
    "model_sha256": hashlib.sha256(pathlib.Path(model).read_bytes()).hexdigest(),
    "results": json.loads(pathlib.Path(tmp).read_text()),
}
pathlib.Path(out).write_text(json.dumps(record, indent=2) + "\n")
for r in record["results"]:
    kind = f"pp{r['n_prompt']}" if r.get("n_prompt") else f"tg{r['n_gen']}"
    print(f"{kind:8s} {r['avg_ts']:10.1f} tok/s  (sd {r['stddev_ts']:.1f})")
print(f"wrote {out}")
PY
rm -f "$TMP"
