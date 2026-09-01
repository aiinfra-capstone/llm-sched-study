#!/usr/bin/env bash
# Survey one machine before it joins the pool.
#
# Run this on every machine, before any LAN work. It answers three questions that
# decide what the machine is allowed to be:
#
#   1. Which models fit here, at the slot count the study uses?
#   2. What range of node speeds can this machine produce? (that is where R comes from)
#   3. Can it host the scheduler and the replay client instead of an engine?
#
# It reads. It changes nothing, needs no root, and installs nothing.
#
#   ./survey.sh                          # inventory only
#   ./survey.sh --bench /path/model.gguf # also measure the -ngl sweep (needs llama-bench)
#   ./survey.sh --json survey.json       # machine-readable copy alongside the report
#
# The inventory works on a bare install. The benchmark needs llama.cpp already built,
# so in practice you run this twice: once when the machine first boots Linux, and again
# after llama.cpp and the GGUF are in place.

set -uo pipefail

BENCH_MODEL=""
JSON_OUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bench) BENCH_MODEL="${2:-}"; shift 2 ;;
    --json)  JSON_OUT="${2:-}";    shift 2 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- helpers

have() { command -v "$1" >/dev/null 2>&1; }
rule() { printf '%s\n' "------------------------------------------------------------------"; }
head2() { printf '\n%s\n' "$1"; rule; }
kv()  { printf '  %-24s %s\n' "$1" "$2"; }

# Bytes -> GiB with one decimal, without needing bc.
gib() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1073741824 }'; }
mib() { awk -v b="$1" 'BEGIN { printf "%.0f", b / 1048576 }'; }

BLOCKERS=()
NOTES=()

# ---------------------------------------------------------------- identity

head2 "MACHINE"

HOSTNAME_="$(hostname)"
kv "hostname" "$HOSTNAME_"

OS="unknown"
[ -r /etc/os-release ] && OS="$(. /etc/os-release; echo "$PRETTY_NAME")"
kv "os" "$OS"
kv "kernel" "$(uname -r)"
kv "arch" "$(uname -m)"

# systemd-detect-virt exits non-zero when it finds nothing, and still prints "none",
# so the exit status is not the signal here and the output is.
VIRT="none"
if have systemd-detect-virt; then
  VIRT="$(systemd-detect-virt 2>/dev/null | head -1)"
  [ -z "$VIRT" ] && VIRT="none"
fi
kv "virtualization" "$VIRT"
if [ "$VIRT" != "none" ]; then
  NOTES+=("Running under '$VIRT'. A pool node must be on bare metal: a virtualization layer lands inside R and is indistinguishable from a hardware difference.")
fi

# ---------------------------------------------------------------- cpu + ram

head2 "CPU AND MEMORY"

CORES_PHYS="?" ; CORES_LOG="$(nproc 2>/dev/null || echo '?')"
CPU_MODEL="unknown"
if have lscpu; then
  CPU_MODEL="$(lscpu | sed -n 's/^Model name: *//p' | head -1)"
  SOCKETS="$(lscpu | sed -n 's/^Socket(s): *//p' | head -1)"
  PERCORE="$(lscpu | sed -n 's/^Core(s) per socket: *//p' | head -1)"
  if [ -n "${SOCKETS:-}" ] && [ -n "${PERCORE:-}" ]; then
    CORES_PHYS=$(( SOCKETS * PERCORE ))
  fi
fi
kv "cpu" "$CPU_MODEL"
kv "cores (physical)" "$CORES_PHYS"
kv "threads (logical)" "$CORES_LOG"

MEM_TOTAL_KB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
MEM_AVAIL_KB="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)"
MEM_TOTAL=$(( MEM_TOTAL_KB * 1024 ))
MEM_AVAIL=$(( MEM_AVAIL_KB * 1024 ))
kv "ram total" "$(gib $MEM_TOTAL) GiB"
kv "ram available now" "$(gib $MEM_AVAIL) GiB"

SWAP_KB="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)"
kv "swap" "$(gib $(( SWAP_KB * 1024 ))) GiB"

# llama.cpp --threads should track physical cores, not hyperthreads: two threads on one
# core contend for the same vector units and the second one buys nothing but jitter.
SUGGEST_THREADS="$CORES_PHYS"
[ "$SUGGEST_THREADS" = "?" ] && SUGGEST_THREADS="$CORES_LOG"

# ---------------------------------------------------------------- gpu

head2 "GPU"

HAS_GPU=0
GPU_NAME="none"
VRAM_TOTAL=0
VRAM_FREE=0
DRIVER="none"
CUDA="none"

if have nvidia-smi; then
  if GPUQ="$(nvidia-smi --query-gpu=name,memory.total,memory.free,driver_version --format=csv,noheader,nounits 2>/dev/null)" && [ -n "$GPUQ" ]; then
    HAS_GPU=1
    GPU_NAME="$(echo "$GPUQ" | head -1 | cut -d, -f1 | sed 's/^ *//')"
    VRAM_TOTAL=$(( $(echo "$GPUQ" | head -1 | cut -d, -f2 | tr -d ' ') * 1048576 ))
    VRAM_FREE=$(( $(echo "$GPUQ" | head -1 | cut -d, -f3 | tr -d ' ') * 1048576 ))
    DRIVER="$(echo "$GPUQ" | head -1 | cut -d, -f4 | sed 's/^ *//')"
    CUDA="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | sed 's/^ *//')"
    kv "gpu" "$GPU_NAME"
    kv "vram total" "$(gib $VRAM_TOTAL) GiB"
    kv "vram free" "$(gib $VRAM_FREE) GiB"
    kv "driver" "$DRIVER"
    kv "compute capability" "$CUDA"
    GPU_COUNT="$(echo "$GPUQ" | wc -l)"
    [ "$GPU_COUNT" -gt 1 ] && NOTES+=("$GPU_COUNT GPUs present. The study runs one engine per machine; pin it with CUDA_VISIBLE_DEVICES and record which card in the node block.")
  fi
fi

if [ "$HAS_GPU" -eq 0 ]; then
  kv "gpu" "none detected"
  if have lspci && lspci 2>/dev/null | grep -qi 'vga\|3d controller'; then
    GPUHW="$(lspci 2>/dev/null | grep -i 'vga\|3d controller' | head -1 | cut -d: -f3- | sed 's/^ *//')"
    kv "display adapter" "$GPUHW"
    echo "$GPUHW" | grep -qi nvidia && BLOCKERS+=("An NVIDIA adapter is present but nvidia-smi does not answer. The driver is not installed or not loaded, so this machine cannot serve as a GPU node yet.")
  fi
fi

# ---------------------------------------------------------------- model budget

head2 "WHICH MODELS FIT"

# Weights are the published Q4_K_M file sizes. KV is computed for the study's shape:
# --parallel 4 slots at 640 tokens each (prompt 512 + output 128, the admissible
# envelope), f16 cache. Overhead is llama.cpp's compute buffers, measured generously.
#
#   name  weights_bytes  kv_bytes_per_token  layers
MODELS=(
  "llama-3.2-1b-q4km   847249408    32768   16"
  "llama-3.2-3b-q4km  2168745984   114688   28"
  "llama-3.1-8b-q4km  5284823040   131072   32"
)
SLOTS=4
CTX_PER_SLOT=640
OVERHEAD=402653184   # 384 MiB

if [ "$HAS_GPU" -eq 1 ]; then
  BUDGET=$VRAM_FREE; BUDGET_NAME="free VRAM"
else
  # Leave headroom on a CPU node: the OS, the worker process and the page cache all
  # need room, and a node that swaps mid-run produces a service time that measures
  # the disk rather than the engine.
  BUDGET=$(( MEM_AVAIL - 1073741824 )); BUDGET_NAME="available RAM minus 1 GiB headroom"
fi
[ "$BUDGET" -lt 0 ] && BUDGET=0
kv "budget" "$(gib $BUDGET) GiB  ($BUDGET_NAME)"
printf '\n  %-20s %9s %9s %9s   %s\n' "model" "weights" "kv" "total" "verdict"

FITS_1B=0
for row in "${MODELS[@]}"; do
  set -- $row
  NAME=$1; W=$2; KVT=$3; LAYERS=$4
  KV=$(( KVT * SLOTS * CTX_PER_SLOT ))
  TOTAL=$(( W + KV + OVERHEAD ))
  if [ "$TOTAL" -le "$BUDGET" ]; then
    VERDICT="fits"
    [ "$NAME" = "llama-3.2-1b-q4km" ] && FITS_1B=1
  else
    VERDICT="does not fit"
  fi
  printf '  %-20s %7s M %7s M %7s M   %s\n' "$NAME" "$(mib $W)" "$(mib $KV)" "$(mib $TOTAL)" "$VERDICT"
done

echo
echo "  The pool runs whatever the SMALLEST member can hold, so this table only"
echo "  constrains the pool downward. The study is pinned to llama-3.2-1b-q4km."

if [ "$FITS_1B" -eq 0 ]; then
  BLOCKERS+=("The study's model (llama-3.2-1b-q4km) does not fit in this machine's budget, so it cannot be a pool node.")
fi

# ---------------------------------------------------------------- R config space

head2 "SPEED SETTINGS THIS MACHINE CAN PRODUCE  (this is where R comes from)"

LAYERS_1B=16
if [ "$HAS_GPU" -eq 1 ]; then
  echo "  -ngl  0 .. $LAYERS_1B     layers on the GPU. 0 is CPU-only, $LAYERS_1B is fully offloaded."
  echo "                  Lowering it makes this node genuinely slower, which is how a"
  echo "                  second R point is produced without a second machine."
else
  echo "  -ngl  0 only    no GPU, so there is no offload axis here."
fi
echo "  --threads 1 .. $SUGGEST_THREADS   physical cores. Past that, threads fight over the same units."
echo "  --parallel 1, 2, 4  concurrent slots. The study calibrated at 4."
echo
echo "  A sensible sweep to measure:"
if [ "$HAS_GPU" -eq 1 ]; then
  echo "    -ngl 16, 12, 8, 4, 0  with --threads $SUGGEST_THREADS --parallel 4"
else
  echo "    --threads $SUGGEST_THREADS, $(( SUGGEST_THREADS / 2 > 0 ? SUGGEST_THREADS / 2 : 1 )), 2, 1  with -ngl 0 --parallel 4"
fi
echo
echo "  Feasible is not the same as measured. Re-run with --bench to get tokens/s"
echo "  per setting, which is the number R is actually computed from."

# ---------------------------------------------------------------- roles

head2 "WHAT THIS MACHINE CAN BE"

# Scheduler + replay client, measured on a lean Linux: JVM ~300 MiB, Python+grpc
# ~200 MiB, OS ~400 MiB. 1.5 GiB is that with room to breathe.
HARNESS_NEED=1610612736
WORKER_NEED=1610612736   # engine process with the 1B model resident

HARNESS_OK=0; POOL_OK=0; BOTH_OK=0
[ "$MEM_TOTAL" -ge "$HARNESS_NEED" ] && HARNESS_OK=1
[ "$FITS_1B" -eq 1 ] && [ "${#BLOCKERS[@]}" -eq 0 ] && POOL_OK=1
[ "$MEM_TOTAL" -ge $(( HARNESS_NEED + WORKER_NEED + 1073741824 )) ] && [ "$POOL_OK" -eq 1 ] && BOTH_OK=1

if [ "$HARNESS_OK" -eq 1 ]; then
  kv "harness host" "yes  (scheduler + replay client, needs ~1.5 GiB)"
else
  kv "harness host" "no   (under 1.5 GiB of RAM)"
fi

if [ "$POOL_OK" -eq 1 ]; then
  if [ "$HAS_GPU" -eq 1 ]; then
    kv "pool node" "yes  (GPU class)"
  else
    kv "pool node" "yes  (CPU class, -ngl 0)"
  fi
else
  kv "pool node" "no   (see blockers)"
fi

if [ "$BOTH_OK" -eq 1 ]; then
  kv "both at once" "yes, but do not. The client has to fire on schedule and"
  printf '  %-24s %s\n' "" "the engine will take the CPU it needs. A late send marks"
  printf '  %-24s %s\n' "" "the whole run invalid."
else
  kv "both at once" "no   (not enough RAM for an engine and the harness together)"
fi

if ! have java; then
  NOTES+=("No java on PATH. The scheduler is Java 17; install it if this machine is to be the harness host.")
else
  kv "java" "$(java -version 2>&1 | head -1)"
fi

# ---------------------------------------------------------------- clock

head2 "CLOCK"

if have chronyc; then
  if TRACK="$(chronyc tracking 2>/dev/null)"; then
    LEAP="$(echo "$TRACK" | sed -n 's/^Leap status *: *//p')"
    SYSTIME="$(echo "$TRACK" | sed -n 's/^System time *: *//p')"
    SKEW="$(echo "$TRACK" | sed -n 's/^Skew *: *//p')"
    REFID="$(echo "$TRACK" | sed -n 's/^Reference ID *: *//p')"
    kv "chrony" "running"
    kv "reference" "$REFID"
    kv "system time" "$SYSTIME"
    kv "skew" "$SKEW"
    kv "leap status" "$LEAP"
    case "$LEAP" in
      Normal) : ;;
      *) NOTES+=("chronyd is running but not synchronised. It must be tracking a source before the pool runs, so that every host's monotonic clock ticks at the same rate.") ;;
    esac
  else
    NOTES+=("chronyc is installed but chronyd is not answering. Start and enable it.")
  fi
else
  NOTES+=("chrony is not installed. Every machine in the pool needs it, so their clocks tick at a common rate.")
fi

# ---------------------------------------------------------------- network

head2 "NETWORK"

if have ip; then
  ip -4 -o addr show scope global 2>/dev/null | while read -r _ IFACE _ CIDR _; do
    LINKTYPE="wired"
    [ -d "/sys/class/net/$IFACE/wireless" ] && LINKTYPE="wireless"
    printf '  %-24s %s  (%s)\n' "$IFACE" "$CIDR" "$LINKTYPE"
  done
fi

FW="none detected"
if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then
  FW="firewalld active, default zone $(firewall-cmd --get-default-zone 2>/dev/null)"
elif have ufw && ufw status 2>/dev/null | grep -qi '^Status: active'; then
  FW="ufw active"
fi
kv "firewall" "$FW"
if [ "$FW" != "none detected" ]; then
  NOTES+=("A firewall is active. The worker answers the client directly, so the client's port must be reachable inbound from every worker host. Blocked, it looks exactly like a saturated pool rather than a firewall.")
fi

# ---------------------------------------------------------------- optional bench

BENCH_RAN=0
if [ -n "$BENCH_MODEL" ]; then
  head2 "MEASURED SPEED SWEEP"
  if ! have llama-bench; then
    echo "  llama-bench is not on PATH. Build llama.cpp first, then re-run with --bench."
  elif [ ! -r "$BENCH_MODEL" ]; then
    echo "  cannot read model: $BENCH_MODEL"
  else
    if [ "$HAS_GPU" -eq 1 ]; then NGL_SWEEP="0,4,8,12,16"; else NGL_SWEEP="0"; fi
    echo "  llama-bench -m $BENCH_MODEL -ngl $NGL_SWEEP -p 512 -n 128 -t $SUGGEST_THREADS"
    echo "  pp512 is prefill tokens/s, tg128 is decode tokens/s. Decode is what sets R."
    echo
    llama-bench -m "$BENCH_MODEL" -ngl "$NGL_SWEEP" -p 512 -n 128 -t "$SUGGEST_THREADS" 2>&1 | sed 's/^/  /'
    BENCH_RAN=1
  fi
fi

# ---------------------------------------------------------------- verdict

head2 "VERDICT"

if [ "${#BLOCKERS[@]}" -gt 0 ]; then
  echo "  Blocking:"
  for b in "${BLOCKERS[@]}"; do printf '    - %s\n' "$b"; done
  echo
fi

if [ "${#NOTES[@]}" -gt 0 ]; then
  echo "  To fix before the pool runs:"
  for n in "${NOTES[@]}"; do printf '    - %s\n' "$n"; done
  echo
fi

if [ "$POOL_OK" -eq 1 ] && [ "$HAS_GPU" -eq 1 ]; then
  echo "  Suggested role: POOL NODE (GPU). Sweep -ngl to produce R points."
elif [ "$POOL_OK" -eq 1 ] && [ "$HARNESS_OK" -eq 1 ]; then
  echo "  Suggested role: HARNESS HOST if a faster machine is available, otherwise a"
  echo "  CPU-class pool node. It cannot be both."
elif [ "$HARNESS_OK" -eq 1 ]; then
  echo "  Suggested role: HARNESS HOST (scheduler + replay client)."
else
  echo "  Suggested role: none. See blockers."
fi

if [ "$BENCH_RAN" -eq 0 ]; then
  echo
  echo "  Not measured yet: actual tokens/s. Re-run with --bench <model.gguf> once"
  echo "  llama.cpp is built here. R cannot be chosen from a spec sheet."
fi
echo

# ---------------------------------------------------------------- json

if [ -n "$JSON_OUT" ]; then
  {
    printf '{\n'
    printf '  "hostname": "%s",\n' "$HOSTNAME_"
    printf '  "os": "%s",\n' "$OS"
    printf '  "kernel": "%s",\n' "$(uname -r)"
    printf '  "virtualization": "%s",\n' "$VIRT"
    printf '  "cpu_model": "%s",\n' "$CPU_MODEL"
    printf '  "cores_physical": "%s",\n' "$CORES_PHYS"
    printf '  "threads_logical": "%s",\n' "$CORES_LOG"
    printf '  "ram_total_bytes": %s,\n' "$MEM_TOTAL"
    printf '  "ram_available_bytes": %s,\n' "$MEM_AVAIL"
    printf '  "has_gpu": %s,\n' "$([ $HAS_GPU -eq 1 ] && echo true || echo false)"
    printf '  "gpu": "%s",\n' "$GPU_NAME"
    printf '  "vram_total_bytes": %s,\n' "$VRAM_TOTAL"
    printf '  "vram_free_bytes": %s,\n' "$VRAM_FREE"
    printf '  "driver": "%s",\n' "$DRIVER"
    printf '  "suggested_threads": "%s",\n' "$SUGGEST_THREADS"
    printf '  "fits_study_model": %s,\n' "$([ $FITS_1B -eq 1 ] && echo true || echo false)"
    printf '  "can_be_pool_node": %s,\n' "$([ $POOL_OK -eq 1 ] && echo true || echo false)"
    printf '  "can_be_harness_host": %s,\n' "$([ $HARNESS_OK -eq 1 ] && echo true || echo false)"
    printf '  "firewall": "%s",\n' "$FW"
    printf '  "surveyed_unix": %s\n' "$(date +%s)"
    printf '}\n'
  } > "$JSON_OUT"
  echo "  wrote $JSON_OUT"
  echo
fi
