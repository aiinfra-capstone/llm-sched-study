#!/usr/bin/env bash
set -euo pipefail

# Build the USB stick that carries Lubuntu plus the study environment to the CPU-only box.
#
# Two jobs: make the stick bootable with Ventoy (once), and put the repo, the engine and
# the model weights on it (every time the repo moves). The second job is the one that gets
# run repeatedly, so it is the one that must be safe to repeat.

USB_DEV="${1:-/dev/sdb}"
MNT_DIR="/tmp/usb_mount"
SRC_USER="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$SRC_USER" | cut -d: -f6)"
REPO_SRC="$USER_HOME/Documents/capstone"
ENGINE_SRC="$USER_HOME/opt/llama.cpp"
MODEL_SRC="$USER_HOME/models/gguf/Llama-3.2-1B-Instruct-Q4_K_M.gguf"
ISO_SRC="/tmp/lubuntu.iso"
VENTOY_SH="/tmp/ventoy-1.0.99/Ventoy2Disk.sh"

# ---------------------------------------------------------------------------------------
# Refuse anything that is not a removable USB block device.
#
# The only guard here before was the FSTYPE test below, which skips the format when
# partition 1 is exfat or ext4. That checks the wrong property. It happens to protect an
# ext4 disk and does nothing for btrfs, xfs, ntfs, LUKS, or a disk with no partition 1, and
# btrfs is the root filesystem on this machine. Combined with defaulting to /dev/sdb and
# piping "y\ny" past Ventoy's confirmation, running this with no argument on a box where
# /dev/sdb is an internal disk would wipe it. Device names are not stable across boots
# either, so "it was sdb last time" is not a check.
# ---------------------------------------------------------------------------------------
if [ ! -b "$USB_DEV" ]; then
    echo "ERROR: $USB_DEV is not a block device." >&2
    lsblk -d -o NAME,SIZE,TRAN,RM,MODEL >&2
    exit 1
fi
DEV_TRAN="$(lsblk -d -no TRAN "$USB_DEV" 2>/dev/null | tr -d '[:space:]')"
DEV_RM="$(lsblk -d -no RM "$USB_DEV" 2>/dev/null | tr -d '[:space:]')"
if [ "$DEV_TRAN" != "usb" ] || [ "$DEV_RM" != "1" ]; then
    echo "REFUSING: $USB_DEV is not a removable USB device (TRAN=${DEV_TRAN:-?} RM=${DEV_RM:-?})." >&2
    echo "This script can format the target. Pass the USB device explicitly, e.g." >&2
    echo "    sudo $0 /dev/sdX" >&2
    lsblk -d -o NAME,SIZE,TRAN,RM,MODEL >&2
    exit 1
fi
echo "Target $USB_DEV: $(lsblk -d -no SIZE "$USB_DEV" | tr -d ' ') removable USB"

# ---------------------------------------------------------------------------------------
# Check every source before starting, so a missing model is reported in a second rather
# than after several minutes of rsync.
# ---------------------------------------------------------------------------------------
missing=0
for src in "$REPO_SRC" "$ENGINE_SRC" "$MODEL_SRC"; do
    [ -e "$src" ] || { echo "ERROR: source missing: $src" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || exit 1

# ---------------------------------------------------------------------------------------
# Mount. Prefer a mount that already exists, then udisks, then sudo. Only the sudo path
# needs a password, and a refresh of an already-mounted stick needs no root at all.
# ---------------------------------------------------------------------------------------
PART="${USB_DEV}1"
WE_MOUNTED=""
MNT="$(findmnt -n -o TARGET -S "$PART" 2>/dev/null | head -1 || true)"

if [ -z "$MNT" ]; then
    if ! lsblk -no FSTYPE "$PART" 2>/dev/null | grep -qE "exfat|ext4"; then
        echo "No Ventoy filesystem on $PART. Installing Ventoy to $USB_DEV (this formats the drive)."
        [ -f "$VENTOY_SH" ] || { echo "ERROR: $VENTOY_SH not found; cannot install Ventoy." >&2; exit 1; }
        read -r -p "Type ERASE to confirm wiping $USB_DEV: " confirm
        [ "$confirm" = "ERASE" ] || { echo "Aborted."; exit 1; }
        umount "${USB_DEV}"* 2>/dev/null || true
        sh "$VENTOY_SH" -I -s -g "$USB_DEV"
        sleep 2
    fi
    if command -v udisksctl >/dev/null 2>&1 && udisksctl mount -b "$PART" >/dev/null 2>&1; then
        MNT="$(findmnt -n -o TARGET -S "$PART" | head -1)"
        WE_MOUNTED="udisks"
    else
        mkdir -p "$MNT_DIR"
        mount "$PART" "$MNT_DIR"
        MNT="$MNT_DIR"
        WE_MOUNTED="mount"
    fi
fi
echo "Mounted at $MNT"

# ---------------------------------------------------------------------------------------
# Space. The three payloads plus the ISO are around 6 GB; refuse rather than half-fill it.
# ---------------------------------------------------------------------------------------
need_kb=$(( $(du -sk "$REPO_SRC" --exclude=.venv | cut -f1) \
          + $(du -sk "$ENGINE_SRC" --exclude=build | cut -f1) \
          + $(du -sk "$MODEL_SRC" | cut -f1) ))
free_kb=$(df -Pk "$MNT" | awk 'NR==2 {print $4}')
echo "Payload $((need_kb/1024)) MiB, free $((free_kb/1024)) MiB"
if [ "$free_kb" -lt "$need_kb" ]; then
    echo "ERROR: not enough free space on $MNT." >&2
    exit 1
fi

ENV_DIR="$MNT/llm-sched-study-env"
mkdir -p "$ENV_DIR/models" "$ENV_DIR/opt/llama.cpp" "$ENV_DIR/repo"

if [ ! -f "$MNT/lubuntu.iso" ]; then
    if [ -f "$ISO_SRC" ]; then
        echo "Copying Lubuntu ISO..."
        cp "$ISO_SRC" "$MNT/"
    else
        echo "ERROR: no lubuntu.iso on the stick and none at $ISO_SRC." >&2
        echo "Download it to $ISO_SRC and re-run, or the stick will not boot." >&2
        exit 1
    fi
else
    echo "Lubuntu ISO already on the stick."
fi

# --delete so a file removed from the repo goes away here too. Without it the stick
# accumulates deleted files and the box runs code that no longer exists upstream.
echo "-> repo"
rsync -r -L --delete --no-perms --no-owner --no-group --info=progress2 \
    --exclude='.venv' --exclude='__pycache__' \
    "$REPO_SRC/" "$ENV_DIR/repo/"
echo "-> engine (llama.cpp)"
rsync -r -L --delete --no-perms --no-owner --no-group --info=progress2 \
    --exclude='build' --exclude='node_modules' \
    "$ENGINE_SRC/" "$ENV_DIR/opt/llama.cpp/"
echo "-> model weights"
rsync -r -L --no-perms --no-owner --no-group --info=progress2 \
    "$MODEL_SRC" "$ENV_DIR/models/"

# What went on the stick, so the box can say what it is running without guessing.
git -C "$REPO_SRC" rev-parse HEAD > "$ENV_DIR/REPO_GIT_SHA" 2>/dev/null || true
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$ENV_DIR/COPIED_AT_UTC"

echo "Flushing write cache..."
sync

# ---------------------------------------------------------------------------------------
# Verify rather than announcing success. rsync exits 0 having copied nothing if a source
# path is wrong, and the old script printed "completely ready" either way.
# ---------------------------------------------------------------------------------------
fail=0
[ -f "$MNT/lubuntu.iso" ] || { echo "MISSING on stick: lubuntu.iso" >&2; fail=1; }
[ -f "$ENV_DIR/models/$(basename "$MODEL_SRC")" ] || { echo "MISSING on stick: model weights" >&2; fail=1; }
[ -f "$ENV_DIR/repo/README.md" ] || { echo "MISSING on stick: repo" >&2; fail=1; }
[ -d "$ENV_DIR/opt/llama.cpp" ] || { echo "MISSING on stick: engine" >&2; fail=1; }

if [ -n "$WE_MOUNTED" ]; then
    echo "Unmounting..."
    if [ "$WE_MOUNTED" = "udisks" ]; then udisksctl unmount -b "$PART" >/dev/null; else umount "$MNT"; fi
else
    echo "Leaving $MNT mounted, since it was mounted before this ran."
fi

if [ "$fail" -ne 0 ]; then
    echo "FAILED: the stick is not complete. See the MISSING lines above." >&2
    exit 1
fi

echo
echo "========================================================="
echo " USB ready. repo at $(cat "$ENV_DIR/REPO_GIT_SHA" 2>/dev/null | cut -c1-7)"
echo " Boot the 4 GB box from it and pick lubuntu.iso in Ventoy."
echo "========================================================="
