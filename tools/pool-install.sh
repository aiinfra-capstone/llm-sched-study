#!/usr/bin/env bash
# Put the same software on a machine, and prove it is the same.
#
# F-9 holds the engine constant across the pool so that speed differences are hardware
# differences. That only means anything if every node can PROVE what it is running, so
# this ends by printing the three things the manifest asserts: the build number and
# commit the server reports about itself, the SHA-256 of the shared libraries the engine
# actually lives in, and the hash of the weights file.
#
#   Harness host (scheduler + replay client, no engine):
#     ./pool-install.sh --role harness --reference 10.42.0.1
#
#   Pool node:
#     ./pool-install.sh --role node --backend cuda   --reference 10.42.0.1
#     ./pool-install.sh --role node --backend vulkan --reference 10.42.0.1
#
#   The machine that other machines take their time from:
#     ./pool-install.sh --role node --backend cuda --reference self
#
# Prints a plan and stops for confirmation unless --yes. Two of these machines belong to
# other people, so nothing here is silent and nothing is destructive.

set -uo pipefail

ROLE=""; BACKEND=""; REFERENCE=""; ASSUME_YES=0; SKIP_ENGINE=0

# The pin. Every one of these is asserted in the manifest, so none of them is a default.
LLAMA_TAG="b10569"
LLAMA_COMMIT="5a32f7b66ef6cfb3e60deea26e3454cc6ad3438c"
LLAMA_COMMIT_SHORT="5a32f7b"
LLAMA_BUILD_NUMBER="10569"
PATCH_NAME="llamacpp-b10569-skip-chat-parse-on-completion.patch"

STUDY_MODEL="Llama-3.2-1B-Instruct-Q4_K_M.gguf"
STUDY_MODEL_SHA="6f85a640a97cf2bf5b8e764087b1e83da0fdb51d7c9fab7d0fece9385611df83"
STUDY_MODEL_URL="https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/$STUDY_MODEL"

OPT_ROOT="$HOME/opt/llama.cpp"
SRC_DIR="$OPT_ROOT/src"
MODEL_DIR="$HOME/models/gguf"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --role)      ROLE="${2:-}"; shift 2 ;;
    --backend)   BACKEND="${2:-}"; shift 2 ;;
    --reference) REFERENCE="${2:-}"; shift 2 ;;
    --skip-engine) SKIP_ENGINE=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    -h|--help)   sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '\nProceed? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) echo "stopped."; exit 1 ;; esac
}

case "$ROLE" in
  harness) : ;;
  node) case "$BACKEND" in cuda|vulkan|cpu) : ;; *) die "--role node needs --backend cuda|vulkan|cpu" ;; esac ;;
  *) die "--role takes 'harness' or 'node'" ;;
esac
[ -n "$REFERENCE" ] || die "--reference needs the time reference's address, or 'self'"

# ------------------------------------------------------------------ distro

PKG=""
have dnf && PKG="dnf"
have apt-get && PKG="apt"
[ -n "$PKG" ] || die "neither dnf nor apt-get found; install the prerequisites by hand"

pkg_install() {
  case "$PKG" in
    dnf) sudo dnf install -y "$@" ;;
    apt) sudo apt-get install -y "$@" ;;
  esac
}

OSNAME="unknown"
[ -r /etc/os-release ] && OSNAME="$(. /etc/os-release; echo "$PRETTY_NAME")"

# ------------------------------------------------------------------ plan

CORES="$(nproc 2>/dev/null || echo 4)"
# nvcc is memory-hungry, and a build that gets OOM-killed halfway leaves a tree that
# looks finished. Cap the CUDA build the way the README's recipe does.
JOBS_ENGINE="$CORES"
[ "$BACKEND" = "cuda" ] && [ "$JOBS_ENGINE" -gt 6 ] && JOBS_ENGINE=6

CUDA_ARCH=""
if [ "$BACKEND" = "cuda" ]; then
  if have nvidia-smi; then
    CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' .')"
    [ -n "$CC" ] && CUDA_ARCH="$CC"
  fi
  [ -n "$CUDA_ARCH" ] || die "cannot read compute capability from nvidia-smi; is the driver installed?"
fi

echo "Set this machine up as: $ROLE${BACKEND:+ / $BACKEND}"
say "host        $(hostname)  ($OSNAME, $PKG)"
say "time ref    $REFERENCE"
say "repo        $REPO_ROOT"
if [ "$ROLE" = "node" ]; then
  say "engine      llama.cpp $LLAMA_TAG @ $LLAMA_COMMIT_SHORT, plus $PATCH_NAME"
  say "build dir   $OPT_ROOT/$LLAMA_TAG-$BACKEND   (-j$JOBS_ENGINE)"
  [ -n "$CUDA_ARCH" ] && say "cuda arch   $CUDA_ARCH  (detected)"
  say "model       $MODEL_DIR/$STUDY_MODEL"
fi
say ""
say "This installs packages with sudo and writes under ~/opt and ~/models."
say "It touches nothing else and removes nothing."
confirm

# ------------------------------------------------------------------ base

step "Base packages"
case "$PKG" in
  dnf) pkg_install git curl chrony ;;
  apt) sudo apt-get update -qq && pkg_install git curl chrony ;;
esac || die "could not install base packages"

# ------------------------------------------------------------------ clock

step "Clock discipline"
# Linux slews CLOCK_MONOTONIC along with the system clock, so pointing every machine at
# one reference is what makes their durations comparable. The offset is recorded in the
# manifest and subtracted from nothing; the rate is what actually matters.
CHRONY_CONF="/etc/chrony.conf"
[ -r /etc/chrony/chrony.conf ] && CHRONY_CONF="/etc/chrony/chrony.conf"
DROPIN="/etc/chrony/conf.d/pool.conf"
[ -d /etc/chrony/conf.d ] || DROPIN=""

if [ "$REFERENCE" = "self" ]; then
  say "this machine is the reference; allowing the pool subnet to take time from it"
  LINE_A="allow 10.42.0.0/24"
  LINE_B="local stratum 10"
  if [ -n "$DROPIN" ]; then
    printf '%s\n%s\n' "$LINE_A" "$LINE_B" | sudo tee "$DROPIN" >/dev/null
  else
    grep -q "^allow 10.42.0.0/24" "$CHRONY_CONF" 2>/dev/null || \
      printf '\n# pool nodes take their time from here\n%s\n%s\n' "$LINE_A" "$LINE_B" | sudo tee -a "$CHRONY_CONF" >/dev/null
  fi
else
  say "taking time from $REFERENCE"
  LINE="server $REFERENCE iburst minpoll 4 maxpoll 6"
  if [ -n "$DROPIN" ]; then
    printf '%s\n' "$LINE" | sudo tee "$DROPIN" >/dev/null
  else
    grep -q "^server $REFERENCE " "$CHRONY_CONF" 2>/dev/null || \
      printf '\n# the pool time reference\n%s\n' "$LINE" | sudo tee -a "$CHRONY_CONF" >/dev/null
  fi
fi
sudo systemctl enable --now chronyd 2>/dev/null || sudo systemctl enable --now chrony 2>/dev/null
sudo systemctl restart chronyd 2>/dev/null || sudo systemctl restart chrony 2>/dev/null
sleep 3
chronyc tracking 2>/dev/null | sed -n '1,6p' | sed 's/^/  /'
say "(a fresh daemon needs a minute or two to settle before 'clocksync --measure' is meaningful)"

# ------------------------------------------------------------------ uv + repo

step "Python environment"
if ! have uv; then
  say "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv install failed"
  export PATH="$HOME/.local/bin:$PATH"
fi
have uv || die "uv installed but not on PATH; open a new shell and re-run"
( cd "$REPO_ROOT/dataplane" && uv sync --all-groups ) || die "uv sync failed"
say "dataplane environment ready"

# ------------------------------------------------------------------ harness role

if [ "$ROLE" = "harness" ]; then
  step "Harness host checks"
  if have java; then
    say "java     $(java -version 2>&1 | head -1)"
  else
    say "WARNING: no java. The scheduler is Java 17+; install a JDK before the first run."
  fi
  step "Done"
  say "This machine runs the scheduler and the replay client. It runs no engine, which"
  say "is the point: the client has to fire on schedule, and a late send marks the whole"
  say "run invalid."
  say ""
  say "Pin the client's delivery port, because the default is ephemeral and a firewall"
  say "cannot open a port that is chosen at startup:"
  say "  uv run replay --bind 0.0.0.0:50071 ..."
  exit 0
fi

# ------------------------------------------------------------------ engine

if [ "$SKIP_ENGINE" -eq 1 ]; then
  step "Skipping the engine build as asked"
else
  step "Engine prerequisites"
  case "$PKG:$BACKEND" in
    dnf:cuda)   pkg_install cmake gcc-c++ make ;;
    apt:cuda)   pkg_install cmake build-essential ;;
    dnf:vulkan) pkg_install cmake gcc-c++ make vulkan-headers vulkan-loader-devel glslc spirv-headers-devel ;;
    apt:vulkan) pkg_install cmake build-essential libvulkan-dev glslc spirv-headers ;;
    dnf:cpu)    pkg_install cmake gcc-c++ make ;;
    apt:cpu)    pkg_install cmake build-essential ;;
  esac || die "could not install build prerequisites"

  if [ "$BACKEND" = "cuda" ]; then
    # The CUDA rpm/deb does not put nvcc on PATH, and cmake then reports no CUDA
    # compiler on a machine that plainly has one.
    for d in /usr/local/cuda/bin /usr/local/cuda-*/bin; do
      [ -x "$d/nvcc" ] && export PATH="$d:$PATH" && break
    done
    have nvcc || die "nvcc not found. Install the CUDA toolkit for this distribution, then re-run. On Ubuntu: the cuda-toolkit package from NVIDIA's apt repo, not the distro's nvidia-cuda-toolkit."
    say "nvcc     $(nvcc --version | tail -1)"
  fi

  step "Source at the pin"
  if [ -d "$SRC_DIR/.git" ]; then
    say "source tree already present at $SRC_DIR"
  else
    mkdir -p "$OPT_ROOT"
    git clone --branch "$LLAMA_TAG" --depth 1 https://github.com/ggml-org/llama.cpp.git "$SRC_DIR" \
      || die "clone failed"
  fi
  GOT="$(git -C "$SRC_DIR" rev-parse HEAD)"
  [ "$GOT" = "$LLAMA_COMMIT" ] || die "source is at $GOT but the pin is $LLAMA_COMMIT; remove $SRC_DIR and re-run"
  say "commit   $GOT"

  step "Patch"
  PATCH_PATH="$REPO_ROOT/patches/$PATCH_NAME"
  [ -r "$PATCH_PATH" ] || die "patch not found at $PATCH_PATH"
  if git -C "$SRC_DIR" apply --reverse --check "$PATCH_PATH" 2>/dev/null; then
    say "already applied"
  elif git -C "$SRC_DIR" apply "$PATCH_PATH"; then
    say "applied $PATCH_NAME"
  else
    die "the patch does not apply cleanly to $LLAMA_TAG; do not build an unpatched engine, the pool would not be homogeneous"
  fi
  say "this is the +p1 in engine_version. llama-server cannot report it, which is"
  say "exactly why the manifest carries it and the library hashes."

  step "Build (-j$JOBS_ENGINE)"
  BUILD_DIR="$OPT_ROOT/$LLAMA_TAG-$BACKEND"
  CMAKE_ARGS=(-S "$SRC_DIR" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_NUMBER="$LLAMA_BUILD_NUMBER")
  case "$BACKEND" in
    cuda)   CMAKE_ARGS+=(-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH") ;;
    vulkan) CMAKE_ARGS+=(-DGGML_VULKAN=ON) ;;
    cpu)    : ;;
  esac
  # LLAMA_BUILD_NUMBER is not cosmetic: llama.cpp counts commits to derive it, so a
  # shallow clone would stamp the binary "build 1" and the engine could no longer say
  # which pin it is.
  cmake "${CMAKE_ARGS[@]}" || die "cmake configure failed"
  cmake --build "$BUILD_DIR" -j"$JOBS_ENGINE" || die "build failed"
fi

# ------------------------------------------------------------------ weights

step "Weights"
mkdir -p "$MODEL_DIR"
if [ ! -r "$MODEL_DIR/$STUDY_MODEL" ]; then
  say "fetching $STUDY_MODEL"
  curl -L --fail -C - --output-dir "$MODEL_DIR" -o "$STUDY_MODEL" "$STUDY_MODEL_URL" \
    || die "download failed"
fi
GOT_SHA="$(sha256sum "$MODEL_DIR/$STUDY_MODEL" | cut -d' ' -f1)"
if [ "$GOT_SHA" = "$STUDY_MODEL_SHA" ]; then
  say "sha256   $GOT_SHA  (matches the pool)"
else
  die "weights hash is $GOT_SHA, expected $STUDY_MODEL_SHA. 'The same model' has to mean the same bytes, not the same name on a page whose contents can be replaced."
fi

# ------------------------------------------------------------------ what to record

step "What this node is, for manifest.nodes[]"
BUILD_DIR="$OPT_ROOT/$LLAMA_TAG-$BACKEND"
SERVER="$BUILD_DIR/bin/llama-server"
if [ -x "$SERVER" ]; then
  VER="$("$SERVER" --version 2>&1 | head -1)"
  say "reports  $VER"
  case "$VER" in
    *"build $LLAMA_BUILD_NUMBER"*"$LLAMA_COMMIT_SHORT"*) say "matches the pin" ;;
    *) say "WARNING: this does not match build $LLAMA_BUILD_NUMBER / $LLAMA_COMMIT_SHORT" ;;
  esac
  echo
  say "engine libraries (llama-server itself is a thin wrapper; the engine is here):"
  sha256sum "$BUILD_DIR"/bin/libllama.so "$BUILD_DIR"/bin/libggml-*.so 2>/dev/null | sed 's/^/    /'
fi

SUFFIX="$BACKEND"
if [ "$BACKEND" = "cuda" ] && have nvcc; then
  CUDA_VER="$(nvcc --version | sed -n 's/.*release \([0-9.]*\).*/\1/p' | head -1)"
  [ -n "$CUDA_VER" ] && SUFFIX="cuda$CUDA_VER"
fi

echo
say "engine_version   ${LLAMA_TAG}+p1+${SUFFIX}"
say "model            Llama-3.2-1B-Instruct"
say "quant            Q4_K_M"
[ -n "${CUDA_ARCH:-}" ] && say "gpu              $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
say ""
say "Start the engine on loopback only, then the worker wrapper in front of it:"
say "  $SERVER --host 127.0.0.1 --port 18080 \\"
say "    -m $MODEL_DIR/$STUDY_MODEL -ngl 99 --threads $CORES --parallel 4"
say "  uv run worker --node-id <id> --engine http://127.0.0.1:18080 \\"
say "    --bind 0.0.0.0:50061 --engine-version ${LLAMA_TAG}+p1+${SUFFIX}"
echo
