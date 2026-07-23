#!/usr/bin/env python3
"""Prove a re-signed sunxi-secure toc1 is (A) internally valid under boot0's verification
rules and (B) byte-identical to the input toc1 outside the crypto-variable cert fields
(serial / dates / modulus / signature / pinned-moduli / add_sum).

Path-parameterized (--in / --resigned) generalization of resign/verify_toc1.py, and fully
self-contained: it re-derives the crypto-variable field map by parsing the image structure,
so it needs no provenance sidecar. The layout offsets are the X40 (r2416) calibration; they
apply unchanged to the D10S Plus (r2240), whose toc1 layout is byte-for-byte identical.

(A) internal validity: for each of the 7 certs, RSA-verify its self-signature over its TBS;
    confirm the 6 content-key moduli are pinned in the rootkey cert; confirm the 4 embedded
    binaries' SHA-256s match the hashes stored in their certs; confirm the header add_sum.
    boot0 would accept the image IFF the per-unit eFuse ROTPK is unburned.
(B) byte-equivalence: classify every differing byte between input and re-signed as either
    inside the crypto-variable fields (expected) or outside them (must be zero).

Run: uv run --with cryptography python3 verify_toc1_generic.py --in IN.img --resigned OUT.bin
"""
import argparse, struct, hashlib, re, sys

STAMP = 0x5F0A6C39
SHA256_DI = bytes.fromhex("3031300d060960864801650304020105000420")
TOC1_LEN = 1245184

CERTS = {"rootkey": 0x1400, "monitor": 0x2400, "optee": 0x11c00, "u-boot": 0x4d400,
         "scp": 0xfd800, "boot": 0x112000, "rootfs": 0x112400}
# content-hash certs -> (embedded binary offset, binary len) inside toc1
ITEMS = {"monitor": (0x2800, 0xf30c), "optee": (0x12000, 0x3b338),
         "u-boot": (0x4d800, 0xb0000), "scp": (0xfdc00, 0x14008)}

# ---- DER helpers -----------------------------------------------------------
def der_len(b, i):
    l = b[i]; i += 1
    if l < 0x80: return l, i
    n = l & 0x7f; return int.from_bytes(b[i:i+n], "big"), i + n

def children(b, s, e):
    out = []; i = s
    while i < e:
        t = b[i]; ln, j = der_len(b, i + 1); out.append((t, i, j, j + ln)); i = j + ln
    return out

def cert_slices(buf, off):
    """Return (tbs, serial, notbefore, notafter, modulus, sig) as (start,end) slices."""
    ln, j = der_len(buf, off + 1); ce = j + ln
    ch = children(buf, j, ce)
    tbs = (ch[0][1], ch[0][3])
    tch = children(buf, ch[0][2], ch[0][3])
    serial = (tch[1][2], tch[1][3])
    vch = children(buf, tch[4][2], tch[4][3])
    nb = (vch[0][2], vch[0][3]); na = (vch[1][2], vch[1][3])
    spki = children(buf, tch[6][2], tch[6][3])
    bit = next(c for c in spki if c[0] == 0x03); pk = bit[2] + 1
    pln, pj = der_len(buf, pk + 1); rp = children(buf, pj, pj + pln); m = rp[0]
    ms = m[2] + (1 if buf[m[2]] == 0 else 0)
    mod = (ms, m[3])
    sig = (ch[2][2] + 1, ch[2][3])
    return tbs, serial, nb, na, mod, sig

def rsa_verify(tbs, sig, mod):
    n = int.from_bytes(mod, "big")
    em = pow(int.from_bytes(sig, "big"), 65537, n).to_bytes(256, "big")
    return em[0] == 0 and em[1] == 1 and em.endswith(SHA256_DI + hashlib.sha256(tbs).digest())

def pinned_moduli(buf):
    out = []
    for m in re.finditer(b"\x08\x82\x02\x07", buf):
        a = m.start() + 4; ascii_val = buf[a:a+519].decode()
        assert ascii_val.startswith("00") and ascii_val.endswith("10001")
        out.append(bytes.fromhex(ascii_val[2:2+512]))
    return set(out)

# ---- (A) internal validity -------------------------------------------------
def verify_internal(buf, label):
    print(f"\n===== (A) internal validity: {label} =====")
    ok = True
    pinned = pinned_moduli(buf)
    rk_tbs, _, _, _, rk_mod, rk_sig = cert_slices(buf, 0x1400)
    r = rsa_verify(buf[slice(*rk_tbs)], buf[slice(*rk_sig)], buf[slice(*rk_mod)])
    print(f"  rootkey cert self-signature .............. {'OK' if r else 'FAIL'}")
    ok &= r
    for nm, off in CERTS.items():
        if nm == "rootkey": continue
        tbs, _, _, _, mod, sig = cert_slices(buf, off)
        modb = bytes(buf[slice(*mod)])
        selfsig = rsa_verify(buf[slice(*tbs)], buf[slice(*sig)], modb)
        inpin = modb in pinned
        line = f"  {nm:8} self-sig {'OK' if selfsig else 'FAIL'}  key-pinned-in-rootkey {'OK' if inpin else 'FAIL'}"
        if nm in ITEMS:
            bo, bl = ITEMS[nm]; want = hashlib.sha256(buf[bo:bo+bl]).hexdigest().upper()
            blob = buf[off:off+0x400]; k = blob.find(b"\x08\x40")
            got = blob[k+2:k+2+64].decode()
            hok = got == want
            line += f"  content-sha256 {'OK' if hok else 'FAIL'}"
            ok &= hok
        print(line); ok &= selfsig and inpin
    b2 = bytearray(buf); struct.pack_into("<I", b2, 20, STAMP)
    calc = sum(struct.unpack_from("<I", b2, i)[0] for i in range(0, len(b2), 4)) & 0xffffffff
    stored = struct.unpack_from("<I", buf, 20)[0]
    print(f"  header add_sum .......................... {'OK' if calc == stored else 'FAIL'} (0x{stored:08x})")
    ok &= calc == stored
    print(f"  => image {'VALID (boot0 would accept if fuse unburned)' if ok else 'INVALID'}")
    return ok

# ---- (B) byte-equivalence-except-crypto ------------------------------------
def crypto_field_set(buf):
    """All byte offsets the re-sign is permitted to touch, derived from image structure."""
    allow = set()
    for off in CERTS.values():
        _, serial, nb, na, mod, sig = cert_slices(buf, off)
        for a, b in (serial, nb, na, mod, sig):
            allow.update(range(a, b))
    for m in re.finditer(b"\x08\x82\x02\x07", buf):     # pinned content moduli
        a = m.start() + 4; allow.update(range(a, a + 519))
    allow.update(range(20, 24))                          # header add_sum
    return allow

def verify_byte_equiv(inp, mine):
    allow = crypto_field_set(mine)
    diff = [i for i in range(len(inp)) if inp[i] != mine[i]]
    outside = [i for i in diff if i not in allow]
    print(f"\n===== (B) byte-equivalence: MINE vs INPUT =====")
    print(f"  total differing bytes ................... {len(diff)}")
    print(f"  inside crypto-variable fields ........... {len(diff) - len(outside)}")
    print(f"  OUTSIDE crypto-variable fields .......... {len(outside)}")
    if outside:
        # collapse to contiguous regions for a readable report
        runs = []; s = p = outside[0]
        for d in outside[1:]:
            if d == p + 1: p = d
            else: runs.append((s, p)); s = p = d
        runs.append((s, p))
        print(f"  !! {len(runs)} residual region(s):")
        for a, b in runs[:20]:
            print(f"       0x{a:x}..0x{b:x} ({b-a+1} B)")
    print(f"  => {'BYTE-EQUIVALENT except crypto fields (re-sign REPRODUCED)' if not outside else 'RESIDUAL DIFF OUTSIDE CRYPTO FIELDS'}")
    return len(diff), len(outside)

def main():
    ap = argparse.ArgumentParser(description="Validate a re-signed sunxi-secure toc1.")
    ap.add_argument("--in", dest="in_path", required=True, help="input toc1 (the re-sign source)")
    ap.add_argument("--resigned", dest="mine_path", required=True, help="the re-signed toc1")
    a = ap.parse_args()
    inp = open(a.in_path, "rb").read()
    mine = open(a.mine_path, "rb").read()
    assert len(inp) == TOC1_LEN and len(mine) == TOC1_LEN, "unexpected toc1 length"

    verify_internal(inp, "INPUT (reference)")
    a_ok = verify_internal(mine, "MINE (fresh dev key)")
    ndiff, nout = verify_byte_equiv(inp, mine)

    print("\n==================== SUMMARY ====================")
    print(f"(A) internal-valid (MINE) ................. {'PASS' if a_ok else 'FAIL'}")
    print(f"(B) byte-equiv-except-crypto ............. {'PASS' if nout == 0 else 'FAIL'} "
          f"({ndiff} diff bytes, {nout} outside crypto fields)")
    return 0 if (a_ok and nout == 0) else 1

if __name__ == "__main__":
    raise SystemExit(main())
