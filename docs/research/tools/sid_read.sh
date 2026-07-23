# On-device eFuse/SID read attempt — run ON the robot (UART root shell or SSH), NOT on the Mac.
# Reads the ROTPK region (SID offset 0x70, 32B) three ways: nvmem sysfs, devmem RO-shadow
# 0x03006270, plus secure-boot dmesg/sysinfo hints. NOTE: this is a NON-secure Linux read; the
# ROTPK region is plausibly secure-read-only, so all-zero here does NOT prove the fuse is empty
# (the open direct-read question — see the selfroot-resume memory).
echo "=== nvmem devices ==="
ls -l /sys/bus/nvmem/devices/ 2>/dev/null
for f in /sys/bus/nvmem/devices/*/nvmem; do
  [ -r "$f" ] || continue
  echo "-- $f  full first 160B (od) --"
  dd if="$f" bs=1 count=160 2>/dev/null | od -An -tx1
  echo "-- $f  ROTPK @0x70 (32B) --"
  dd if="$f" bs=1 skip=112 count=32 2>/dev/null | od -An -tx1 | tr -d '\n'; echo
done
echo "=== devmem ROTPK RO-shadow 0x03006270 (8 words) ==="
if command -v devmem >/dev/null 2>&1; then
  for a in 0x03006270 0x03006274 0x03006278 0x0300627c 0x03006280 0x03006284 0x03006288 0x0300628c; do
    printf "%s " "$a"; devmem "$a" 2>/dev/null || echo "(fail)"
  done
else echo "no devmem on device"; fi
echo "=== secure-boot hints ==="
dmesg 2>/dev/null | grep -iE "secure|rotpk|efuse|sunxi.?sid|verified" | head -20
cat /sys/class/sunxi_info/sys_info 2>/dev/null
