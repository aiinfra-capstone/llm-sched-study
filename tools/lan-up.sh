#!/usr/bin/env bash
# Bring up the pool's LAN, and prove it carries the experiment.
#
# The network this study needs is unusual in one way: under F-11 the worker returns the
# response DIRECTLY to the client, so traffic has to flow worker -> client, not just
# client -> scheduler -> worker. Most consumer wireless setups break exactly that
# direction and break it silently: the dispatch succeeds, the worker serves the request,
# the client records a timeout, and the run looks like a saturated pool.
#
# That is why this refuses to use a phone hotspot, and why --verify exists.
#
#   On the machine that will host the access point:
#     ./lan-up.sh --ap --ssid poolnet --pass '<secret>'
#
#   On every other machine:
#     ./lan-up.sh --join --ssid poolnet --pass '<secret>' --ip 10.42.0.11
#
#   Then, per machine, open only the ports its role listens on:
#     ./lan-up.sh --open harness      # scheduler 50051, client delivery 50071
#     ./lan-up.sh --open node         # worker 50061
#
#   Then prove it, from each machine, naming the others:
#     ./lan-up.sh --verify 10.42.0.1:50051 10.42.0.1:50071
#
#   And when you are done with somebody else's laptop:
#     ./lan-up.sh --down
#
# Everything except --verify changes system state and needs sudo. Each mode prints what
# it will do and stops for confirmation unless --yes is given.

set -uo pipefail

MODE=""
SSID=""; PSK=""; IFACE=""; STATIC_IP=""; ROLE=""; ASSUME_YES=0
VERIFY_TARGETS=()

# The three ports the study listens on, and the one it must never expose.
PORT_SCHEDULER=50051   # scheduler: Dispatch from the client, Heartbeat from workers
PORT_WORKER=50061      # worker: Execute from the scheduler
PORT_CLIENT=50071      # client: Deliver from the workers (F-11). Must be pinned, see below.
PORT_ENGINE=18080      # llama-server. Localhost only, always.

AP_GATEWAY="10.42.0.1" # what NetworkManager's shared mode hands the AP host

while [ $# -gt 0 ]; do
  case "$1" in
    --ap)     MODE="ap"; shift ;;
    --join)   MODE="join"; shift ;;
    --open)   MODE="open"; ROLE="${2:-}"; shift 2 ;;
    --verify) MODE="verify"; shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do VERIFY_TARGETS+=("$1"); shift; done ;;
    --down)   MODE="down"; shift ;;
    --ssid)   SSID="${2:-}"; shift 2 ;;
    --pass)   PSK="${2:-}"; shift 2 ;;
    --iface)  IFACE="${2:-}"; shift 2 ;;
    --ip)     STATIC_IP="${2:-}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }
say()  { printf '  %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  printf '\nProceed? [y/N] '
  read -r reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) echo "stopped."; exit 1 ;; esac
}

# Pick the wireless interface if not told. One wireless NIC is the normal case; two is
# worth stopping for, because guessing wrong here silently takes down somebody's internet.
pick_wifi_iface() {
  [ -n "$IFACE" ] && { echo "$IFACE"; return; }
  local found=()
  for d in /sys/class/net/*/wireless; do
    [ -e "$d" ] && found+=("$(basename "$(dirname "$d")")")
  done
  [ "${#found[@]}" -eq 0 ] && die "no wireless interface found; pass --iface, or use ethernet"
  [ "${#found[@]}" -gt 1 ] && die "several wireless interfaces (${found[*]}); pass --iface"
  echo "${found[0]}"
}

firewall_backend() {
  if have firewall-cmd && firewall-cmd --state >/dev/null 2>&1; then echo firewalld
  elif have ufw && ufw status >/dev/null 2>&1; then echo ufw
  else echo none; fi
}

# ------------------------------------------------------------------ ap

if [ "$MODE" = "ap" ]; then
  [ -n "$SSID" ] && [ -n "$PSK" ] || die "--ap needs --ssid and --pass"
  [ "${#PSK}" -ge 8 ] || die "WPA needs a passphrase of at least 8 characters"
  have nmcli || die "nmcli not found; this assumes NetworkManager"
  DEV="$(pick_wifi_iface)"

  if ! iw list 2>/dev/null | grep -qw AP; then
    say "WARNING: 'iw list' does not report AP mode on this machine's radio."
    say "If the hotspot fails to start, run the access point on another machine."
  fi

  echo "Bring up an access point on this machine:"
  say "interface   $DEV"
  say "ssid        $SSID"
  say "subnet      ${AP_GATEWAY%.*}.0/24, this host at $AP_GATEWAY"
  say ""
  say "This uses NetworkManager's shared mode, which routes and hands out DHCP and"
  say "does NOT isolate clients from each other. That last part is the whole reason"
  say "we are not using a phone: most phone hotspots block device-to-device traffic,"
  say "which kills the worker's direct reply to the client and looks like a timeout."
  say ""
  say "It will take over $DEV, so this machine loses its current wireless network."
  confirm

  sudo nmcli device wifi hotspot ifname "$DEV" ssid "$SSID" password "$PSK" \
    || die "could not start the hotspot on $DEV"
  sleep 2
  echo
  say "up. Other machines join with:"
  say "  ./lan-up.sh --join --ssid '$SSID' --pass '<the passphrase>' --ip ${AP_GATEWAY%.*}.11"
  say ""
  say "Connected stations will appear in:  sudo iw dev $DEV station dump"
  exit 0
fi

# ------------------------------------------------------------------ join

if [ "$MODE" = "join" ]; then
  [ -n "$SSID" ] && [ -n "$PSK" ] || die "--join needs --ssid and --pass"
  [ -n "$STATIC_IP" ] || die "--join needs --ip; a DHCP lease can change between runs and the manifest records host addresses"
  have nmcli || die "nmcli not found; this assumes NetworkManager"
  DEV="$(pick_wifi_iface)"
  case "$STATIC_IP" in */*) ADDR="$STATIC_IP" ;; *) ADDR="$STATIC_IP/24" ;; esac

  echo "Join the pool network:"
  say "interface   $DEV"
  say "ssid        $SSID"
  say "address     $ADDR  (static, gateway $AP_GATEWAY)"
  say ""
  say "Static rather than DHCP because manifest.nodes[].host records where each node"
  say "was, and an address that changes between runs makes two runs incomparable."
  confirm

  sudo nmcli device wifi connect "$SSID" password "$PSK" ifname "$DEV" \
    || die "could not associate with $SSID"
  CONN="$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$DEV" '$2==d{print $1; exit}')"
  [ -n "$CONN" ] || die "associated but cannot find the connection profile for $DEV"
  sudo nmcli connection modify "$CONN" ipv4.method manual ipv4.addresses "$ADDR" ipv4.gateway "$AP_GATEWAY"
  sudo nmcli connection up "$CONN" >/dev/null || die "could not reapply $CONN with the static address"
  echo
  say "joined as $ADDR"
  say "check the AP is reachable:  ping -c2 $AP_GATEWAY"
  exit 0
fi

# ------------------------------------------------------------------ open

if [ "$MODE" = "open" ]; then
  case "$ROLE" in
    harness) PORTS=("$PORT_SCHEDULER" "$PORT_CLIENT") ;;
    node)    PORTS=("$PORT_WORKER") ;;
    *) die "--open takes 'harness' or 'node'" ;;
  esac
  FW="$(firewall_backend)"

  echo "Open inbound ports for role '$ROLE':"
  for p in "${PORTS[@]}"; do say "tcp/$p"; done
  say ""
  say "firewall    $FW"
  say ""
  if [ "$ROLE" = "harness" ]; then
    say "tcp/$PORT_CLIENT is the one people forget. The worker replies to the client"
    say "directly, so this port is inbound from every worker host. The replay client"
    say "defaults to an ephemeral port, which cannot be opened in advance, so it must"
    say "be pinned:  replay --bind 0.0.0.0:$PORT_CLIENT"
  else
    say "tcp/$PORT_ENGINE (llama-server) is deliberately NOT opened. The worker wrapper"
    say "talks to it over loopback, and exposing an unauthenticated engine to the LAN"
    say "is not something this study needs."
  fi

  if [ "$FW" = "none" ]; then
    say ""
    say "No active firewall detected, so there is nothing to open. Re-run --verify"
    say "from the other machines anyway: an AP can block this by itself."
    exit 0
  fi
  confirm

  for p in "${PORTS[@]}"; do
    case "$FW" in
      firewalld) sudo firewall-cmd --permanent --add-port="$p/tcp" >/dev/null && say "opened tcp/$p" ;;
      ufw)       sudo ufw allow "$p/tcp" >/dev/null && say "opened tcp/$p" ;;
    esac
  done
  [ "$FW" = "firewalld" ] && sudo firewall-cmd --reload >/dev/null

  # An engine reachable from the LAN is a real finding, not a nitpick: it means the
  # pool node is serving requests that never went through the scheduler, and nothing
  # downstream would show it.
  if have ss && ss -ltn 2>/dev/null | grep -qE "0\.0\.0\.0:$PORT_ENGINE|\[::\]:$PORT_ENGINE"; then
    echo
    say "WARNING: llama-server is listening on all interfaces (port $PORT_ENGINE)."
    say "Start it with --host 127.0.0.1 so only the worker wrapper can reach it."
  fi
  exit 0
fi

# ------------------------------------------------------------------ verify

if [ "$MODE" = "verify" ]; then
  [ "${#VERIFY_TARGETS[@]}" -gt 0 ] || die "--verify needs one or more host:port"
  echo "Reachability from $(hostname):"
  FAILED=0
  for t in "${VERIFY_TARGETS[@]}"; do
    H="${t%:*}"; P="${t##*:}"
    if timeout 3 bash -c "exec 3<>/dev/tcp/$H/$P" 2>/dev/null; then
      printf '  ok     %s\n' "$t"
    else
      printf '  FAIL   %s\n' "$t"
      FAILED=1
    fi
  done
  echo
  if [ "$FAILED" -eq 1 ]; then
    say "A refused connection here is a firewall or an isolating access point. A"
    say "connection that hangs until the timeout is usually AP client isolation."
    say ""
    say "This only proves a port is open. Use 'uv run preflight' for the real check:"
    say "it speaks gRPC, and a port that accepts TCP but not HTTP/2 fails there"
    say "instead of failing at the first request of a run."
    exit 1
  fi
  say "All named ports accept TCP. Now run the gRPC-level check:"
  say "  uv run preflight --serve 0.0.0.0:$PORT_CLIENT      # on the harness host"
  say "  uv run preflight --probe <harness>:$PORT_CLIENT    # from each pool node"
  exit 0
fi

# ------------------------------------------------------------------ down

if [ "$MODE" = "down" ]; then
  have nmcli || die "nmcli not found"
  HOTSPOTS="$(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}' | grep -i hotspot || true)"
  if [ -z "$HOTSPOTS" ]; then
    say "no hotspot connection found; nothing to remove"
    exit 0
  fi
  echo "Remove these connection profiles and give the radio back:"
  echo "$HOTSPOTS" | sed 's/^/  /'
  confirm
  echo "$HOTSPOTS" | while read -r c; do sudo nmcli connection delete "$c" >/dev/null && say "removed $c"; done
  say "reconnect to the usual network from the desktop, or with: nmcli device wifi connect <ssid>"
  exit 0
fi

die "give one of --ap, --join, --open <role>, --verify <host:port>..., --down  (see --help)"
