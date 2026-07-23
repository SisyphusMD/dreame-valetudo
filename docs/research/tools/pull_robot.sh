#!/usr/bin/env bash
# Full offline mirror of the D10S eMMC over SSH (Mac pulls; robot gzips). Order: metadata, the reserved
# head (GPT + pre-partition boot region), the hardware boot partitions (toc0/boot0), then code/config
# partitions, then the big /data (UDISK) last. Each partition -> its own gzipped image, labelled by name.
#
# PREREQ: an SSH key the robot's dropbear will accept. The robot's HOME=/tmp and rootfs is read-only
# squashfs, so dropbear reads $HOME/.ssh/authorized_keys under /tmp — WIPED on every robot reboot. Before
# running this, re-inject a throwaway pubkey via the UART root shell:
#   mkdir -p /tmp/.ssh && echo '<pubkey>' > /tmp/.ssh/authorized_keys && chmod 600 /tmp/.ssh/authorized_keys
# then point KEY below at the matching private key. Generate a throwaway pair with `ssh-keygen -t ed25519`.
set -u
KEY="${KEY:-$HOME/dreame-self-root-research/robot_key}"          # throwaway private key (see PREREQ)
DUMP="${DUMP:-$HOME/dreame-self-root-research/robot_emmc_dump}"
RH="${RH:-root@<robot-ip>}"
mkdir -p "$DUMP"

ssh_do() {
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=15 "$RH" "$@"
}

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "metadata"
ssh_do 'echo ===PARTITIONS; cat /proc/partitions; echo ===CMDLINE; cat /proc/cmdline; echo ===MOUNT; mount; echo ===BYNAME; ls -l /dev/by-name; echo ===UNAME; uname -a; echo ===OSREL; cat /etc/os-release /etc/build.prop 2>/dev/null; echo ===DMESG_PART; dmesg 2>/dev/null | grep -iE "mmc|partition|gpt|rootfs" | head -80' > "$DUMP/metadata.txt" 2>&1

log "head 48M (GPT + reserved/toc1 region)"
ssh_do 'dd if=/dev/mmcblk0 bs=1M count=48 2>/dev/null | gzip -1' > "$DUMP/mmcblk0_head48M.img.gz"

log "hw boot partitions (toc0/boot0)"
ssh_do 'dd if=/dev/mmcblk0boot0 bs=1M 2>/dev/null | gzip -1' > "$DUMP/hw-boot0.mmcblk0boot0.img.gz"
ssh_do 'dd if=/dev/mmcblk0boot1 bs=1M 2>/dev/null | gzip -1' > "$DUMP/hw-boot1.mmcblk0boot1.img.gz"

log "code/config partitions"
for pair in mmcblk0p5:rootfs1 mmcblk0p7:rootfs2 mmcblk0p4:boot1 mmcblk0p6:boot2 \
            mmcblk0p1:boot-resource mmcblk0p2:env mmcblk0p3:env-redund \
            mmcblk0p9:misc mmcblk0p8:private mmcblk0p10:pstore; do
  dev="${pair%%:*}"; label="${pair##*:}"
  log "  - $label ($dev)"
  ssh_do "dd if=/dev/$dev bs=1M 2>/dev/null | gzip -1" > "$DUMP/$label.$dev.img.gz"
done

log "UDISK /data (mmcblk0p11, ~3.5G) — last"
ssh_do 'dd if=/dev/mmcblk0p11 bs=1M 2>/dev/null | gzip -1' > "$DUMP/UDISK-data.mmcblk0p11.img.gz"

log "DONE"
ls -la "$DUMP"
