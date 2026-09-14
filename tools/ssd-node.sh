#!/usr/bin/env bash
# Turn the Crucial X9 into a boot disk for the RTX 4070 pool node, keeping every file
# Windows has on it.
#
#   sudo tools/ssd-node.sh check       read-only. Identity, layout, the plan. Changes nothing.
#   sudo tools/ssd-node.sh shrink      shrink the NTFS volume from its end, then prove the files survived
#   sudo tools/ssd-node.sh verify      re-run that proof on its own (read-only)
#   sudo tools/ssd-node.sh partition   create the ESP and the ext4 root in the freed space
#   sudo tools/ssd-node.sh install     headless Fedora 43, NVIDIA driver, CUDA 13.2, bootloader, user
#   sudo tools/ssd-node.sh stage       repo, pinned engine source, models, uv environment (repeatable)
#   sudo tools/ssd-node.sh unmount     flush and detach everything, safe to unplug
#
# The layout it produces:
#
#   p1  sector 34           16 MiB     Microsoft reserved       untouched
#   p2  sector 32768        830.9 GiB  NTFS "Crucial X9"        same start, same data, shorter end
#   p3  sector 1742579712   600 MiB    EFI system (node)        new
#   p4  sector 1743808512   100 GiB    ext4 root (node)         new
#
# Why it is this paranoid. Resizing NTFS rewrites its metadata and moves any clusters that
# sit past the new end, there is no backup of this disk, and a wrong device name formats the
# wrong disk. So the disk is found by serial number rather than /dev/sdX, every stage re-reads
# the partition table and refuses unless it is exactly one of the layouts below, nothing is
# ever passed --force, and the shrink hashes the irreplaceable files before and after.

set -euo pipefail

# ------------------------------------------------------------------ the disk, as measured

BYID="/dev/disk/by-id/usb-Micron_CT1000X9SSD9_2514E8D3C4F5-0:0"
SERIAL="2514E8D3C4F5"
DISK_BYTES=1000204886016
PTUUID="01a06a4d-0b04-4ff8-83d8-e9e881ef5a54"
MSR_PARTUUID="b3895502-079f-4b9b-be95-acf356d59266"
NTFS_PARTUUID="f5f24c9e-b066-4c96-a7f3-e0fadc980eeb"
NTFS_FS_UUID="7EBADD16BADCCBB1"

MSR_START=34
MSR_SECTORS=32734
NTFS_START=32768
NTFS_SECTORS_ORIG=1953490944

# Everything below is 512-byte sectors and aligned to 1 MiB (2048 sectors). The new
# partitions end exactly where NTFS ends today, so nothing moves past the old end.
ROOT_SECTORS=$(( 100 * 1024 * 1024 * 1024 / 512 ))
ESP_SECTORS=$(( 600 * 1024 * 1024 / 512 ))
OLD_END=$(( NTFS_START + NTFS_SECTORS_ORIG ))
ROOT_START=$(( OLD_END - ROOT_SECTORS ))
ESP_START=$(( ROOT_START - ESP_SECTORS ))
NTFS_SECTORS_NEW=$(( ESP_START - NTFS_START ))
NTFS_BYTES_NEW=$(( NTFS_SECTORS_NEW * 512 ))

ESP_TYPE="C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
LINUX_TYPE="0FC63DAF-8483-4772-8E79-3D69D8477DE4"
ESP_LABEL="NODE-ESP"
ROOT_LABEL="node-root"

# ------------------------------------------------------------------ the node

NODE_HOSTNAME="rtx4070"
RELEASEVER=43
CUDA_PKG="cuda-toolkit-13-2"
REPOS="fedora,updates,rpmfusion-free,rpmfusion-free-updates,rpmfusion-nonfree,rpmfusion-nonfree-updates,rpmfusion-nonfree-nvidia-driver,cuda-fedora43-x86_64"
KERNEL_ARGS="rd.driver.blacklist=nouveau modprobe.blacklist=nouveau nvidia-drm.modeset=1"

R="/mnt/ssd-node"
UDEV_RULE="/run/udev/rules.d/99-ssd-node-no-automount.rules"

# ------------------------------------------------------------------ helpers

say()  { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nREFUSING: %s\n' "$*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run this with sudo"
  [ "$USER_NAME" != "root" ] || die "run it as 'sudo tools/ssd-node.sh ...' from your own account, so it knows whose files to copy"
}

# The account whose repo, models and engine get copied, and who owns the state directory.
# Under sudo that is the caller, not root.
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
STATE="$USER_HOME/ssd-node-state"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

confirm_word() {
  local word="$1" reply
  printf '\nType %s to continue, anything else stops: ' "$word"
  read -r reply
  [ "$reply" = "$word" ] || { echo "stopped, nothing changed by this step."; exit 1; }
}

sysfs_part() { cat "/sys/block/$DISK_BASE/$DISK_BASE$1/$2" 2>/dev/null; }

# Find the disk by serial and prove it is the X9, not whatever happens to be sdb today.
resolve_disk() {
  [ -e "$BYID" ] || die "the Crucial X9 ($SERIAL) is not plugged in"
  DEV="$(readlink -f "$BYID")"
  [[ "$DEV" =~ ^/dev/sd[a-z]+$ ]] || die "$BYID resolves to $DEV, which is not a whole SCSI disk"
  DISK_BASE="$(basename "$DEV")"
  [ "$(lsblk -dno TRAN "$DEV" | tr -d ' ')" = "usb" ] || die "$DEV is not on USB"
  [ "$(lsblk -dno SERIAL "$DEV" | tr -d ' ')" = "$SERIAL" ] || die "$DEV serial does not match $SERIAL"
  [ "$(lsblk -bdno SIZE "$DEV" | tr -d ' ')" = "$DISK_BYTES" ] || die "$DEV size changed"
  [ "$(lsblk -dno PTUUID "$DEV" | tr -d ' ')" = "$PTUUID" ] || die "$DEV partition table UUID changed"
  local m
  for m in / /boot /boot/efi /home; do
    case "$(findmnt -no SOURCE "$m" 2>/dev/null)" in
      "$DEV"*) die "$DEV holds $m of this laptop" ;;
    esac
  done
  P1="${DEV}1"; P2="${DEV}2"; P3="${DEV}3"; P4="${DEV}4"
}

# Prints ORIGINAL, SHRUNK or PARTITIONED. Anything else stops the script.
layout() {
  udevadm settle 2>/dev/null || true
  local n=0 p
  for p in /sys/block/"$DISK_BASE"/"$DISK_BASE"[0-9]*; do [ -e "$p/partition" ] && n=$((n + 1)); done

  [ "$(sysfs_part 1 start)" = "$MSR_START" ] && [ "$(sysfs_part 1 size)" = "$MSR_SECTORS" ] \
    || die "partition 1 is not the 16 MiB Microsoft reserved partition it was"
  [ "$(lsblk -no PARTUUID "$P1" | tr -d ' ')" = "$MSR_PARTUUID" ] || die "partition 1 PARTUUID changed"
  [ "$(sysfs_part 2 start)" = "$NTFS_START" ] || die "the NTFS partition no longer starts at sector $NTFS_START"
  [ "$(lsblk -no PARTUUID "$P2" | tr -d ' ')" = "$NTFS_PARTUUID" ] || die "partition 2 PARTUUID changed"
  [ "$(lsblk -no UUID "$P2" | tr -d ' ')" = "$NTFS_FS_UUID" ] || die "partition 2 no longer holds the NTFS volume $NTFS_FS_UUID"

  local s2; s2="$(sysfs_part 2 size)"
  if [ "$n" -eq 2 ] && [ "$s2" = "$NTFS_SECTORS_ORIG" ]; then echo ORIGINAL; return; fi
  if [ "$n" -eq 2 ] && [ "$s2" = "$NTFS_SECTORS_NEW" ]; then echo SHRUNK; return; fi
  if [ "$n" -eq 4 ] && [ "$s2" = "$NTFS_SECTORS_NEW" ] \
     && [ "$(sysfs_part 3 start)" = "$ESP_START" ] && [ "$(sysfs_part 3 size)" = "$ESP_SECTORS" ] \
     && [ "$(sysfs_part 4 start)" = "$ROOT_START" ] && [ "$(sysfs_part 4 size)" = "$ROOT_SECTORS" ]; then
    echo PARTITIONED; return
  fi
  die "unexpected layout ($n partitions, partition 2 is $s2 sectors). Not guessing; nothing changed."
}

# udisks mounts partitions the moment they appear. During a resize that is the last thing we
# want, so the X9 is kept out of automount until reboot or 'unmount'. Manual mounts still work.
no_automount() {
  mkdir -p "$(dirname "$UDEV_RULE")"
  printf 'ENV{ID_SERIAL_SHORT}=="%s", ENV{UDISKS_AUTO}="0"\n' "$SERIAL" > "$UDEV_RULE"
  udevadm control --reload 2>/dev/null || true
}

unmount_all_of() {
  local dev="$1" t
  while t="$(findmnt -no TARGET -S "$dev" 2>/dev/null | head -1)" && [ -n "$t" ]; do
    say "unmounting $dev from $t"
    umount "$dev" 2>/dev/null || { fuser -vm "$dev" >&2 || true; die "$dev is busy. Close whatever uses it (Steam, a file manager, a terminal cd'd into it)."; }
  done
}

on_ac_power() {
  local f
  for f in /sys/class/power_supply/*/online; do
    [ -e "$f" ] || continue
    [ "$(cat "$(dirname "$f")/type" 2>/dev/null)" = "Mains" ] && [ "$(cat "$f")" = "1" ] && return 0
  done
  return 1
}

# A listing of every file with its size, and SHA-256 of the files that cannot be downloaded
# again (RL Agent, the top-level files) plus 300 random others as a sample of the rest.
snapshot() {
  local mnt="$1" out="$2"
  # find exits non-zero on one unreadable entry, and under pipefail that would abort the
  # whole script mid-shrink. An unreadable file shows up as a difference instead.
  ( cd "$mnt" && { find . -xdev -type f -printf '%s\t%p\n' 2>/dev/null || true; } | LC_ALL=C sort -t $'\t' -k2 ) > "$out.list"
  ( cd "$mnt" && {
      find "./RL Agent" -type f 2>/dev/null || true
      find . -maxdepth 1 -type f 2>/dev/null || true
      { find . -xdev -type f -size -256M ! -path './RL Agent/*' 2>/dev/null || true; } | shuf -n 300
    } | LC_ALL=C sort -u | tr '\n' '\0' | { xargs -0 sha256sum || true; } ) > "$out.sha256"
  chown "$USER_NAME:" "$out.list" "$out.sha256"
}

# Proves the shrunk volume is intact without ntfsresize. ntfsresize flags every volume it
# resizes for a Windows chkdsk, and then refuses to open a flagged volume, so it cannot be
# the verifier of its own work. Everything here only reads.
verify_ntfs() {
  [ -s "$STATE/before.list" ] && [ -s "$STATE/before.sha256" ] \
    || die "no pre-shrink snapshot in $STATE; nothing to verify against"

  step "Verify: boot sector against the partition"
  local oem total backup_ok
  oem="$(dd if="$P2" bs=1 skip=3 count=8 status=none)"
  [ "$oem" = "NTFS    " ] || die "partition 2 does not start with an NTFS boot sector (got '$oem')"
  total="$(dd if="$P2" bs=1 skip=40 count=8 status=none | od -An -tu8 | tr -d ' ')"
  say "volume     $total sectors ($(( total * 512 )) bytes)"
  say "partition  $(sysfs_part 2 size) sectors"
  [ "$total" -lt "$(sysfs_part 2 size)" ] \
    || die "the NTFS volume claims more sectors than partition 2 holds. Do not mount it, do not run 'partition'; send me this."
  backup_ok=0
  cmp -s <(dd if="$P2" bs=512 count=1 status=none) \
         <(dd if="$P2" bs=512 skip="$total" count=1 status=none) && backup_ok=1
  if [ "$backup_ok" -eq 1 ]; then
    say "backup boot sector present right after the volume, identical to the primary"
  else
    say "note: no identical backup boot sector at sector $total. Windows chkdsk rewrites it; not fatal."
  fi

  step "Verify: every file against the pre-shrink snapshot (read-only mount)"
  local tmpm ok=1
  tmpm="$(mktemp -d)"
  mount -t ntfs-3g -o ro "$P2" "$tmpm" || { rmdir "$tmpm"; die "ntfs-3g will not mount the shrunk volume read-only. Send me this."; }
  snapshot "$tmpm" "$STATE/after"
  if cmp -s "$STATE/before.list" "$STATE/after.list"; then
    say "file list identical: $(wc -l < "$STATE/after.list") files, same names, same sizes"
  else
    ok=0; say "FILE LIST DIFFERS:"; diff "$STATE/before.list" "$STATE/after.list" | head -20 | sed 's/^/    /'
  fi
  if ( cd "$tmpm" && sha256sum --quiet -c "$STATE/before.sha256" ); then
    say "all $(wc -l < "$STATE/before.sha256") hashed files read back byte-identical"
  else
    ok=0
  fi
  df -h "$tmpm" | sed 's/^/    /'
  umount "$tmpm"; rmdir "$tmpm"
  [ "$ok" -eq 1 ] || die "verification failed. Do not run 'partition'. Send me the output."
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/verified-ok"; chown "$USER_NAME:" "$STATE/verified-ok"
}

cmd_verify() {
  need_root
  resolve_disk
  no_automount
  local state; state="$(layout)"
  [ "$state" != "ORIGINAL" ] || die "the disk is not shrunk yet; nothing to verify"
  unmount_all_of "$P2"
  verify_ntfs
  step "Done"
  say "NTFS verified. Windows runs one quick chkdsk the next time it sees the X9; let it finish."
  say "Next: sudo tools/ssd-node.sh partition"
}

# ------------------------------------------------------------------ check

cmd_check() {
  resolve_disk
  local state; state="$(layout)"
  step "Disk"
  say "device      $DEV  (found by serial $SERIAL, not by name)"
  say "layout      $state"
  lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTTYPENAME,MOUNTPOINTS "$DEV" | sed 's/^/    /'

  step "Plan"
  say "NTFS        $NTFS_SECTORS_ORIG -> $NTFS_SECTORS_NEW sectors ($((NTFS_SECTORS_ORIG * 512 / 1073741824)) GiB -> $((NTFS_BYTES_NEW / 1073741824)) GiB), start stays $NTFS_START"
  say "ESP         start $ESP_START, $ESP_SECTORS sectors (600 MiB)"
  say "root        start $ROOT_START, $ROOT_SECTORS sectors (100 GiB), ends at $OLD_END, where NTFS ends today"

  step "NTFS health signals available without unmounting"
  local mnt opts
  mnt="$(findmnt -no TARGET -S "$P2" 2>/dev/null | head -1 || true)"
  if [ -n "$mnt" ]; then
    opts="$(findmnt -no OPTIONS -S "$P2" | head -1)"
    df -h "$mnt" | sed 's/^/    /'
    case ",$opts," in
      *,rw,*) say "mounted read-write by ntfs-3g, so Windows did not leave it hibernated (Fast Startup)" ;;
      *)      say "WARNING: mounted read-only. ntfs-3g does that to a hibernated volume. Fix in Windows first." ;;
    esac
  else
    say "not mounted; 'shrink' runs ntfsresize --info, which checks consistency"
  fi
  if on_ac_power; then say "power       on AC"; else say "power       ON BATTERY. Plug in before 'shrink'."; fi
  echo
  say "Nothing was changed."
}

# ------------------------------------------------------------------ shrink

cmd_shrink() {
  need_root
  resolve_disk
  local state; state="$(layout)"
  if [ "$state" != "ORIGINAL" ]; then say "already shrunk ($state), nothing to do"; return; fi
  on_ac_power || die "on battery. A power loss while clusters are being moved can corrupt the volume."
  command -v ntfsresize >/dev/null || die "ntfsresize missing (dnf install ntfsprogs)"
  no_automount
  mkdir -p "$STATE"; chown "$USER_NAME:" "$STATE"

  step "Record the partition table and the NTFS boot sectors on the internal disk"
  sfdisk --dump "$DEV" > "$STATE/x9-partition-table.sfdisk"
  dd if="$DEV" of="$STATE/x9-first-1MiB.bin" bs=512 count=2048 status=none
  dd if="$DEV" of="$STATE/x9-last-1MiB.bin" bs=512 skip=$(( $(blockdev --getsz "$DEV") - 2048 )) status=none
  dd if="$P2" of="$STATE/ntfs-boot-sector.bin" bs=512 count=1 status=none
  dd if="$P2" of="$STATE/ntfs-backup-boot-sector.bin" bs=512 skip=$(( NTFS_SECTORS_ORIG - 1 )) count=1 status=none
  chown "$USER_NAME:" "$STATE"/*
  say "saved to $STATE (restorable with: sfdisk $DEV < x9-partition-table.sfdisk)"

  step "Snapshot the files before touching anything"
  local mnt tmpm=""
  mnt="$(findmnt -no TARGET -S "$P2" 2>/dev/null | head -1 || true)"
  if [ -z "$mnt" ]; then
    tmpm="$(mktemp -d)"; mount -t ntfs-3g -o ro "$P2" "$tmpm"; mnt="$tmpm"
  fi
  if [ ! -s "$STATE/before.list" ]; then
    say "listing every file and hashing RL Agent plus a 300-file sample (about a minute)"
    snapshot "$mnt" "$STATE/before"
  fi
  say "$(wc -l < "$STATE/before.list") files, $(awk -F'\t' '{s+=$1} END {printf "%.1f GiB", s/1073741824}' "$STATE/before.list"), $(wc -l < "$STATE/before.sha256") hashed"
  [ -n "$tmpm" ] && { umount "$tmpm"; rmdir "$tmpm"; }

  step "Unmount"
  unmount_all_of "$P2"
  sync

  step "ntfsresize --info (consistency check, reads only)"
  local info min
  info="$(ntfsresize --info --no-progress-bar "$P2" 2>&1)" || { echo "$info" | sed 's/^/    /'; die "ntfsresize refused the volume as it is. If it mentions hibernation, a dirty flag or chkdsk: plug the X9 into Windows, run 'chkdsk X: /f', eject it safely, then re-run. Never add --force."; }
  echo "$info" | sed 's/^/    /'
  min="$(echo "$info" | sed -n 's/.*You might resize at \([0-9]*\) bytes.*/\1/p' | head -1)"
  [ -n "$min" ] || die "could not read the minimum size from ntfsresize"
  [ $(( min + 20 * 1073741824 )) -lt "$NTFS_BYTES_NEW" ] || die "the volume needs $min bytes and the plan leaves $NTFS_BYTES_NEW; under 20 GiB of margin"

  step "Dry run"
  if ! ntfsresize --no-action --no-progress-bar --size "$NTFS_BYTES_NEW" "$P2" < /dev/null 2>&1 | sed 's/^/    /'; then
    die "the dry run failed; nothing was written"
  fi

  echo
  say "Next: shrink the NTFS filesystem to $NTFS_BYTES_NEW bytes, then shorten partition 2 to match."
  say "Do not unplug the X9, suspend, or close this terminal until it says done."
  confirm_word SHRINK

  step "Shrink the filesystem"
  # ntfsresize asks its own y/n question as well. Answer it.
  systemd-inhibit --what=sleep:idle:handle-lid-switch --who=ssd-node --why="resizing NTFS on the Crucial X9" \
    ntfsresize --no-progress-bar --size "$NTFS_BYTES_NEW" "$P2" \
    || die "ntfsresize failed. The partition table is unchanged. Do not retry blindly; send me the output."
  sync

  step "Shorten partition 2 (start, type, name and PARTUUID unchanged)"
  echo ",$NTFS_SECTORS_NEW" | sfdisk --wipe never --wipe-partitions never \
    --backup --backup-file "$STATE/sfdisk-shrink" -N 2 "$DEV"
  partx -u "$DEV" 2>/dev/null || blockdev --rereadpt "$DEV" 2>/dev/null || true
  udevadm settle
  [ "$(layout)" = "SHRUNK" ] || die "partition table did not come out as planned; saved table is in $STATE"

  verify_ntfs

  step "Done"
  say "NTFS shrunk and verified. The next time Windows sees the X9 it runs a quick chkdsk"
  say "that ntfsresize scheduled. Let it finish; it is expected."
  say "Next: sudo tools/ssd-node.sh partition"
}

# ------------------------------------------------------------------ partition

cmd_partition() {
  need_root
  resolve_disk
  no_automount
  local state; state="$(layout)"
  case "$state" in
    ORIGINAL) die "run 'shrink' first" ;;
    SHRUNK)
      [ -s "$STATE/verified-ok" ] || die "the shrink has not been verified; run 'sudo tools/ssd-node.sh verify' first"
      step "Create the ESP and the root in the freed space"
      say "p3  start $ESP_START  size $ESP_SECTORS  EFI system"
      say "p4  start $ROOT_START  size $ROOT_SECTORS  Linux filesystem"
      say "Partitions 1 and 2 are not written."
      confirm_word PARTITION
      mkdir -p "$STATE"
      printf 'start=%s, size=%s, type=%s, name="%s"\nstart=%s, size=%s, type=%s, name="%s"\n' \
        "$ESP_START" "$ESP_SECTORS" "$ESP_TYPE" "$ESP_LABEL" \
        "$ROOT_START" "$ROOT_SECTORS" "$LINUX_TYPE" "$ROOT_LABEL" \
        | sfdisk --wipe never --wipe-partitions never \
            --backup --backup-file "$STATE/sfdisk-append" --append "$DEV"
      partx -u "$DEV" 2>/dev/null || true
      udevadm settle
      [ "$(layout)" = "PARTITIONED" ] || die "partition table did not come out as planned"
      ;;
    PARTITIONED) say "partitions already exist" ;;
  esac

  # Format only a partition that is empty. A rerun must never reformat a populated root.
  step "Filesystems"
  local t3 t4
  t3="$(blkid -po value -s TYPE "$P3" 2>/dev/null || true)"
  t4="$(blkid -po value -s TYPE "$P4" 2>/dev/null || true)"
  if [ "$t3" = "vfat" ] && [ "$(blkid -po value -s LABEL "$P3")" = "$ESP_LABEL" ]; then
    say "p3 already vfat $ESP_LABEL"
  elif [ -z "$t3" ]; then
    mkfs.vfat -F 32 -n "$ESP_LABEL" "$P3" >/dev/null && say "p3 formatted vfat"
  else
    die "p3 holds a '$t3' signature, probably leftover bytes from the old NTFS tail. Check with 'wipefs $P3' (lists only); clear with 'wipefs -a $P3' if it is not ours."
  fi
  if [ "$t4" = "ext4" ] && [ "$(blkid -po value -s LABEL "$P4")" = "$ROOT_LABEL" ]; then
    say "p4 already ext4 $ROOT_LABEL"
  elif [ -z "$t4" ]; then
    mkfs.ext4 -q -L "$ROOT_LABEL" "$P4" && say "p4 formatted ext4"
  else
    die "p4 holds a '$t4' signature. Check with 'wipefs $P4'; clear with 'wipefs -a $P4' if it is not ours."
  fi
  lsblk -o NAME,SIZE,FSTYPE,LABEL,PARTTYPENAME "$DEV" | sed 's/^/    /'
  say "Next: sudo tools/ssd-node.sh install"
}

# ------------------------------------------------------------------ mounts

mount_node() {
  [ "$(layout)" = "PARTITIONED" ] || die "run 'partition' first"
  [ "$(blkid -po value -s LABEL "$P4" 2>/dev/null)" = "$ROOT_LABEL" ] || die "p4 is not the node root"
  mkdir -p "$R"
  findmnt -n "$R" >/dev/null || mount "$P4" "$R"
  [ "$(findmnt -no SOURCE "$R")" = "$P4" ] || die "$R is mounted from something other than $P4"
  mkdir -p "$R/boot/efi"
  findmnt -n "$R/boot/efi" >/dev/null || mount "$P3" "$R/boot/efi"
  ROOT_UUID="$(blkid -po value -s UUID "$P4")"
  ESP_UUID="$(blkid -po value -s UUID "$P3")"
}

# A fresh sysfs without efivarfs, deliberately. Package scriptlets that call efibootmgr then
# fail harmlessly instead of editing this laptop's boot entries.
chroot_up() {
  mkdir -p "$R"/{proc,sys,dev,run,tmp}
  findmnt -n "$R/proc" >/dev/null || mount -t proc proc "$R/proc"
  findmnt -n "$R/sys"  >/dev/null || mount -t sysfs sysfs "$R/sys"
  findmnt -n "$R/dev"  >/dev/null || mount --bind /dev "$R/dev"
  findmnt -n "$R/dev/pts" >/dev/null || mount -t devpts devpts "$R/dev/pts"
  findmnt -n "$R/run"  >/dev/null || mount -t tmpfs tmpfs "$R/run"
  if [ -e "$R/etc/resolv.conf" ] || [ -L "$R/etc/resolv.conf" ]; then
    [ -e "$R/etc/resolv.conf.ssd-node" ] || [ -L "$R/etc/resolv.conf.ssd-node" ] || mv "$R/etc/resolv.conf" "$R/etc/resolv.conf.ssd-node"
  fi
  mkdir -p "$R/etc"; cat /etc/resolv.conf > "$R/etc/resolv.conf"
  trap chroot_down EXIT
}

chroot_down() {
  local m
  for m in "$R/run" "$R/dev/pts" "$R/dev" "$R/sys" "$R/proc"; do
    findmnt -n "$m" >/dev/null 2>&1 && umount -l "$m"
  done
  if [ -e "$R/etc/resolv.conf.ssd-node" ] || [ -L "$R/etc/resolv.conf.ssd-node" ]; then
    rm -f "$R/etc/resolv.conf"; mv "$R/etc/resolv.conf.ssd-node" "$R/etc/resolv.conf"
  fi
  return 0
}

in_root() { chroot "$R" /usr/bin/env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin LANG=C.UTF-8 "$@"; }

# ------------------------------------------------------------------ shadow

shadow_has_password() {
  awk -F: -v u="$1" '$1 == u && $2 ~ /^\$/ { found = 1 } END { exit !found }' "$R/etc/shadow"
}

# Replace the password field of one user, or prefix it with "!" when the value is "lock".
# Written to a temporary file next to the original and moved over it, so a failure leaves
# the old file intact.
shadow_set() {
  local user="$1" val="$2" tmp
  tmp="$(mktemp "$R/etc/.shadow.ssd-node.XXXXXX")"
  if ! awk -F: -v OFS=: -v u="$user" -v v="$val" '
      $1 == u { if (v == "lock") { if ($2 !~ /^!/) $2 = "!" $2 } else $2 = v; seen = 1 }
      { print }
      END { exit !seen }' "$R/etc/shadow" > "$tmp"; then
    rm -f "$tmp"; die "no $user in the node's /etc/shadow"
  fi
  [ "$(wc -l < "$tmp")" -eq "$(wc -l < "$R/etc/shadow")" ] || { rm -f "$tmp"; die "shadow rewrite changed the line count"; }
  chmod 000 "$tmp"
  mv "$tmp" "$R/etc/shadow"
}

# ------------------------------------------------------------------ install

cmd_install() {
  need_root
  resolve_disk
  no_automount
  mount_node
  local uid gid
  uid="$(id -u "$USER_NAME")"; gid="$(id -g "$USER_NAME")"

  step "Configuration written before any package, so the kernel and initramfs are built for it"
  mkdir -p "$R"/etc/{kernel,dracut.conf.d,default,pki,yum.repos.d}
  cat > "$R/etc/fstab" <<EOF
UUID=$ROOT_UUID  /          ext4  defaults,noatime  1 1
UUID=$ESP_UUID   /boot/efi  vfat  umask=0077,shortname=winnt  0 2
EOF
  # The NTFS partition is deliberately absent. The node never writes to Windows' data.
  echo "$NODE_HOSTNAME" > "$R/etc/hostname"
  echo "root=UUID=$ROOT_UUID ro $KERNEL_ARGS" > "$R/etc/kernel/cmdline"
  # hostonly would build an initramfs for this laptop's hardware; the node is a different machine.
  printf 'hostonly="no"\nhostonly_cmdline="no"\n' > "$R/etc/dracut.conf.d/90-ssd-node-generic.conf"
  cat > "$R/etc/default/grub" <<EOF
GRUB_TIMEOUT=3
GRUB_DISTRIBUTOR="Fedora"
GRUB_DEFAULT=saved
GRUB_DISABLE_SUBMENU=true
GRUB_TERMINAL_OUTPUT="console"
GRUB_CMDLINE_LINUX="$KERNEL_ARGS"
GRUB_DISABLE_RECOVERY="true"
GRUB_ENABLE_BLSCFG=true
GRUB_DISABLE_OS_PROBER=true
EOF
  echo 'LANG="en_US.UTF-8"' > "$R/etc/locale.conf"
  cp /etc/vconsole.conf "$R/etc/vconsole.conf"
  ln -sfn "$(readlink /etc/localtime)" "$R/etc/localtime"
  [ -s "$R/etc/machine-id" ] || systemd-machine-id-setup --root="$R" >/dev/null
  cp -rn /etc/pki/rpm-gpg "$R/etc/pki/"
  local f
  for f in /etc/yum.repos.d/cuda-fedora43*.repo /etc/yum.repos.d/rpmfusion-nonfree-nvidia-driver.repo; do
    [ -e "$f" ] && cp -n "$f" "$R/etc/yum.repos.d/"
  done

  step "Packages (a few GB from the Fedora, RPM Fusion and NVIDIA mirrors)"
  local pkgs=(
    @core kernel kernel-devel dracut-config-generic linux-firmware
    grub2-efi-x64 grub2-efi-x64-modules grub2-tools shim-x64 efibootmgr dosfstools
    NetworkManager NetworkManager-wifi wpa_supplicant iw openssh-server chrony firewalld
    sudo git curl rsync tar cmake gcc-c++ make python3 tmux htop pciutils usbutils lsof
    vim-minimal glibc-langpack-en selinux-policy-targeted
    rpmfusion-free-release rpmfusion-nonfree-release
    akmod-nvidia xorg-x11-drv-nvidia-cuda "$CUDA_PKG"
  )
  # /proc, /sys and /dev go in before the first package. Scriptlets run chrooted into $R, and
  # without them grub2-common's %posttrans fails, which dnf reports as a failed transaction,
  # and the kernel's %posttrans cannot install the kernel into /boot.
  chroot_up
  dnf5 -y --installroot="$R" --releasever="$RELEASEVER" --use-host-config --repo="$REPOS" \
    install "${pkgs[@]}" || die "package install failed; the disk layout is fine, re-run 'install' after fixing the cause"

  local kver
  kver="$(ls "$R/lib/modules" | sort -V | tail -1)"
  say "kernel $kver"

  # A first run of this stage without /dev left the kernel out of /boot. kernel-install is
  # what the kernel's own %posttrans calls, so running it here repairs that and is a no-op
  # otherwise. It builds the initramfs (generic, per dracut.conf.d) and the BLS entry.
  step "Kernel into /boot"
  if [ ! -e "$R/boot/vmlinuz-$kver" ] || [ ! -e "$R/boot/initramfs-$kver.img" ]; then
    in_root BOOT_ROOT=/boot kernel-install add "$kver" "/lib/modules/$kver/vmlinuz"
  fi
  [ -e "$R/boot/vmlinuz-$kver" ] && [ -e "$R/boot/initramfs-$kver.img" ] \
    || die "no vmlinuz/initramfs for $kver in /boot after kernel-install; the node would not boot"
  say "vmlinuz-$kver and initramfs-$kver.img present"

  step "NVIDIA kernel module for $kver"
  if find "$R/lib/modules/$kver" -name 'nvidia.ko*' 2>/dev/null | grep -q .; then
    say "already built"
  elif in_root akmods --force --kernels "$kver"; then
    say "built"
  else
    say "WARNING: akmods could not build in the chroot. akmods.service builds it on the node's first boot instead (takes a few minutes)."
  fi

  step "Generic initramfs and GRUB"
  in_root dracut --force --kver "$kver"
  in_root grub2-mkconfig -o /boot/grub2/grub.cfg
  local stub
  stub="search --no-floppy --fs-uuid --set=dev $ROOT_UUID
set prefix=(\$dev)/boot/grub2
export \$prefix
configfile \$prefix/grub.cfg"
  mkdir -p "$R/boot/efi/EFI/fedora" "$R/boot/efi/EFI/BOOT"
  printf '%s\n' "$stub" > "$R/boot/efi/EFI/fedora/grub.cfg"
  printf '%s\n' "$stub" > "$R/boot/efi/EFI/BOOT/grub.cfg"
  # Boot from the removable-media path, and without the fallback loader. fbx64.efi would
  # write a permanent boot entry into the 4070 box's firmware on first boot; without it the
  # box keeps its own boot order and the X9 is picked from the one-time boot menu.
  cp "$R/boot/efi/EFI/fedora/shimx64.efi" "$R/boot/efi/EFI/BOOT/BOOTX64.EFI"
  cp "$R/boot/efi/EFI/fedora/grubx64.efi" "$R/boot/efi/EFI/BOOT/grubx64.efi"
  [ -e "$R/boot/efi/EFI/fedora/mmx64.efi" ] && cp "$R/boot/efi/EFI/fedora/mmx64.efi" "$R/boot/efi/EFI/BOOT/mmx64.efi"
  rm -f "$R/boot/efi/EFI/BOOT/fbx64.efi"
  ls "$R"/boot/loader/entries/ 2>/dev/null | sed 's/^/    /' || true
  ls "$R"/boot/loader/entries/*"$kver"*.conf >/dev/null 2>&1 \
    || die "no boot entry for $kver in /boot/loader/entries; GRUB would show nothing to boot"

  step "Account $USER_NAME (uid $uid), services"
  if ! in_root getent passwd "$USER_NAME" >/dev/null; then
    in_root groupadd -g "$gid" "$USER_NAME" 2>/dev/null || true
    in_root useradd -m -u "$uid" -g "$gid" -G wheel -s /bin/bash "$USER_NAME"
  fi
  # passwd and chpasswd run confined (passwd_t) under this laptop's SELinux policy, and it
  # refuses them the node root's unlabeled /etc/.pwd.lock. So the shadow file is edited from
  # this shell instead, which is unconfined. The node relabels on first boot.
  shadow_set root lock
  if ! shadow_has_password "$USER_NAME"; then
    local p1 p2 hash
    while :; do
      read -rs -p "  password $USER_NAME will log in with on the node: " p1; echo
      read -rs -p "  again: " p2; echo
      [ -n "$p1" ] && [ "$p1" = "$p2" ] && break
      say "empty, or the two did not match. Again."
    done
    hash="$(printf '%s' "$p1" | openssl passwd -6 -stdin)"
    unset p1 p2
    [[ "$hash" == '$6$'* ]] || die "openssl did not produce a SHA-512 crypt hash"
    shadow_set "$USER_NAME" "$hash"
    say "password set"
  else
    say "password already set"
  fi
  mkdir -p "$R/home/$USER_NAME/.ssh"
  cat "$USER_HOME"/.ssh/*.pub > "$R/home/$USER_NAME/.ssh/authorized_keys" 2>/dev/null || true
  chmod 700 "$R/home/$USER_NAME/.ssh"; chmod 600 "$R/home/$USER_NAME/.ssh/authorized_keys" 2>/dev/null || true
  chown -R "$uid:$gid" "$R/home/$USER_NAME/.ssh"
  # systemctl is confined here too, so a failed call is only a warning when the unit's enable
  # symlink already exists; most were created by the packages' own presets.
  local svc
  for svc in sshd chronyd NetworkManager firewalld akmods; do
    in_root systemctl enable "$svc" >/dev/null 2>&1 && continue
    if find "$R/etc/systemd/system" -name "$svc.service" -type l 2>/dev/null | grep -q .; then
      say "$svc already enabled"
    else
      say "WARNING: $svc is not enabled. On the node: sudo systemctl enable $svc"
    fi
  done
  # Weak dependencies brought in daemons a headless measurement node has no use for, and
  # each one is background load on the machine whose speed we are measuring. Removing the
  # wants/ symlinks is exactly what 'systemctl disable' does, without needing systemctl.
  for svc in avahi-daemon.service avahi-daemon.socket bluetooth.service ModemManager.service cups.service cups.socket cups.path; do
    find "$R/etc/systemd/system" -path '*.wants/*' -name "$svc" -type l -delete 2>/dev/null || true
  done
  ln -sfn /usr/lib/systemd/system/multi-user.target "$R/etc/systemd/system/default.target"
  # Files written from here carry no SELinux labels. The node relabels on first boot, then reboots once.
  touch "$R/.autorelabel"

  step "Done"
  say "Next: sudo tools/ssd-node.sh stage"
}

# ------------------------------------------------------------------ stage

cmd_stage() {
  need_root
  resolve_disk
  no_automount
  mount_node
  local uid gid H
  uid="$(id -u "$USER_NAME")"; gid="$(id -g "$USER_NAME")"
  H="$R/home/$USER_NAME"
  [ -d "$H" ] || die "no home for $USER_NAME on the node; run 'install' first"

  step "Repo -> ~/Documents/capstone"
  mkdir -p "$H/Documents/capstone" "$H/opt/llama.cpp/src" "$H/models/gguf" "$H/.local/bin"
  rsync -a --delete --info=progress2 \
    --exclude='.venv' --exclude='__pycache__' --exclude='/trash' --exclude='*.pyc' \
    --exclude='/controlplane/target' --exclude='/controlplane/build' \
    "$REPO_ROOT/" "$H/Documents/capstone/"

  step "Pinned llama.cpp source, patch included -> ~/opt/llama.cpp/src"
  rsync -a --delete --info=progress2 --exclude='/build/' --exclude='/build-*/' "$USER_HOME/opt/llama.cpp/src/" "$H/opt/llama.cpp/src/"

  step "Models -> ~/models/gguf"
  rsync -a --info=progress2 "$USER_HOME/models/gguf/" "$H/models/gguf/"

  step "uv"
  install -m 755 "$USER_HOME/.local/bin/uv" "$H/.local/bin/uv"
  [ -e "$USER_HOME/.local/bin/uvx" ] && install -m 755 "$USER_HOME/.local/bin/uvx" "$H/.local/bin/uvx"
  grep -qs '.local/bin' "$H/.bashrc" || echo 'export PATH="$HOME/.local/bin:/usr/local/cuda/bin:$PATH"' >> "$H/.bashrc"

  git -C "$REPO_ROOT" rev-parse HEAD > "$H/Documents/capstone/.staged-from-sha" 2>/dev/null || true
  chown -R "$uid:$gid" "$H"

  step "Python environment (uv sync inside the node's root, as $USER_NAME)"
  chroot_up
  in_root runuser -u "$USER_NAME" -- /usr/bin/env HOME="/home/$USER_NAME" PATH="/home/$USER_NAME/.local/bin:/usr/bin:/bin" \
    bash -c 'cd ~/Documents/capstone/dataplane && uv sync --all-groups' \
    || say "WARNING: uv sync failed here; pool-install.sh runs it again on the node"

  cat > "$H/FIRST_BOOT.txt" <<EOF
First boot of the RTX 4070 node

0. Firmware: Secure Boot OFF (the NVIDIA module is unsigned). Boot the X9 from the
   one-time boot menu, entry "UEFI: Micron CT1000X9SSD9". The first boot relabels
   SELinux and reboots once by itself.
1. Log in as $USER_NAME. Check the driver:     nvidia-smi
   If it says no device, akmods is still building:  sudo systemctl status akmods
2. Network:                                    nmcli device wifi list
   Pool LAN:  cd ~/Documents/capstone && tools/lan-up.sh --join --ssid ... --pass ... --ip 10.42.0.X
             tools/lan-up.sh --open node
3. Engine, clock, env:  tools/pool-install.sh --role node --backend cuda --reference <harness ip>
   The source is already at the pin with the patch applied, so it builds for the 4070
   (compute capability 8.9) without cloning.
EOF
  chown "$uid:$gid" "$H/FIRST_BOOT.txt"
  df -h "$R" | sed 's/^/    /'
  say "staged repo at $(cut -c1-7 "$H/Documents/capstone/.staged-from-sha" 2>/dev/null)"
  say "Next: sudo tools/ssd-node.sh unmount"
}

# ------------------------------------------------------------------ unmount

cmd_unmount() {
  need_root
  resolve_disk
  [ -d "$R" ] && chroot_down
  sync
  findmnt -n "$R/boot/efi" >/dev/null && umount "$R/boot/efi"
  findmnt -n "$R" >/dev/null && umount "$R"
  rm -f "$UDEV_RULE"; udevadm control --reload 2>/dev/null || true
  sync
  say "unmounted; automount restored. Safe to unplug once the drive light stops."
}

case "${1:-}" in
  check)     cmd_check ;;
  shrink)    cmd_shrink ;;
  verify)    cmd_verify ;;
  partition) cmd_partition ;;
  install)   cmd_install ;;
  stage)     cmd_stage ;;
  unmount)   cmd_unmount ;;
  -h|--help|"") sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown stage: $1" >&2; exit 2 ;;
esac
