#!/usr/bin/env python3
"""Validate a re-signed sunxi TOC0 against its stock input.

(B) byte-equivalence: classify every differing byte between input and re-signed as either
    inside the known crypto/hygiene-variable fields (expected) or outside them (must be zero).
    Also confirms item1 (boot0/SPL code) is untouched, so its content-hash pin in the cert's
    extensions field is still self-consistent with no recomputation.
(A) internal validity is DELIBERATELY NOT CLAIMED for the item0 cert's own trailing signature
    field: recon established its scheme is unidentified offline (non-standard nested
    AlgorithmIdentifier + malformed BIT STRING, contradicts the OID declared in its own TBS,
    and does not verify as plain PKCS1v15/PSS-SHA256 under its own embedded key). This script
    reports that field as UNVERIFIED, not PASS/FAIL, rather than asserting something unproven.
    What IS checked: container length, magic, item table pointers, header add_sum, and
    (given a --toc1 path) whether the re-signed toc1's rootkey-cert modulus equals this toc0's
    new root modulus -- i.e. whether the two images share the same root key ("chains").

Run: uv run --with cryptography python3 verify_toc0_generic.py --in IN.img --resigned OUT.img \
        [--toc1 CHAIN_TOC1.img]
"""
import argparse, struct, sys
from resign_toc0 import Cert0, check_layout, STAMP, TOC0_LEN, CERT_OFF, CERT_LEN, ITEM1_OFF, ITEM1_LEN


def crypto_field_set(buf):
    cert = Cert0(buf, CERT_OFF, CERT_LEN)
    allow = set()
    for a, b in (cert.modulus, cert.exponent, cert.serial, cert.notbefore, cert.notafter):
        allow.update(range(a, b))
    allow.update(range(0xc, 0x10))   # header add_sum
    return allow


def verify_addsum(buf):
    b2 = bytearray(buf)
    struct.pack_into("<I", b2, 0xc, STAMP)
    calc = sum(struct.unpack_from("<I", b2, i)[0] for i in range(0, len(b2), 4)) & 0xffffffff
    stored = struct.unpack_from("<I", buf, 0xc)[0]
    return calc == stored, stored, calc


def verify_structural(buf, label):
    print(f"\n===== structural checks: {label} =====")
    ok = True
    try:
        check_layout(buf)
        print("  length == 0x18000, magic, items_nr, item pointers ..... OK")
    except AssertionError as e:
        print(f"  layout ................................................. FAIL ({e})"); ok = False
        return False
    a_ok, stored, calc = verify_addsum(buf)
    print(f"  header add_sum .......................................... {'OK' if a_ok else 'FAIL'} "
          f"(stored=0x{stored:08x} calc=0x{calc:08x})")
    ok &= a_ok
    cert = Cert0(buf, CERT_OFF, CERT_LEN)
    print(f"  item0 cert parses (2 outer children, 8 tbs children) ... OK")
    print(f"  item0 cert own trailing signature field ................ UNVERIFIED "
          f"(scheme unidentified offline -- see module docstring, not claimed valid or invalid)")
    return ok


def verify_byte_equiv(inp, mine):
    allow = crypto_field_set(mine)
    diff = [i for i in range(len(inp)) if inp[i] != mine[i]]
    outside = [i for i in diff if i not in allow]
    item1_touched = inp[ITEM1_OFF:ITEM1_OFF + ITEM1_LEN] != mine[ITEM1_OFF:ITEM1_OFF + ITEM1_LEN]
    print(f"\n===== byte-equivalence: MINE vs INPUT =====")
    print(f"  total differing bytes ................................... {len(diff)}")
    print(f"  inside crypto/hygiene-variable fields ................... {len(diff) - len(outside)}")
    print(f"  OUTSIDE crypto/hygiene-variable fields ................... {len(outside)}")
    print(f"  item1 (boot0/SPL code) touched ........................... {'YES (unexpected!)' if item1_touched else 'NO (untouched, as designed)'}")
    if outside:
        runs = []; s = p = outside[0]
        for d in outside[1:]:
            if d == p + 1: p = d
            else: runs.append((s, p)); s = p = d
        runs.append((s, p))
        print(f"  !! {len(runs)} residual region(s):")
        for a, b in runs[:20]:
            print(f"       0x{a:x}..0x{b:x} ({b - a + 1} B)")
    ok = not outside and not item1_touched
    print(f"  => {'BYTE-EQUIVALENT except crypto fields, item1 untouched' if ok else 'RESIDUAL DIFF -- investigate'}")
    return ok


def verify_chain(mine, toc1_path):
    print(f"\n===== chain check: toc0 root vs toc1 rootkey =====")
    cert = Cert0(mine, CERT_OFF, CERT_LEN)
    toc0_mod = bytes(mine[cert.modulus[0]:cert.modulus[1]])
    sys.path.insert(0, ".")
    from resign_toc1_generic import Cert as Cert1
    toc1 = open(toc1_path, "rb").read()
    c1 = Cert1(bytearray(toc1), 0x1400, "rootkey")
    toc1_mod = bytes(toc1[c1.modulus[0]:c1.modulus[1]])
    same = toc0_mod == toc1_mod
    print(f"  toc0 new root modulus == toc1 new rootkey-cert modulus ... {'YES (chained)' if same else 'NO'}")
    return same


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--resigned", dest="mine_path", required=True)
    ap.add_argument("--toc1", dest="toc1_path", default=None, help="re-signed chain toc1 to check root-key match against")
    a = ap.parse_args()
    inp = bytearray(open(a.in_path, "rb").read())
    mine = bytearray(open(a.mine_path, "rb").read())

    s_ok = verify_structural(mine, "MINE (re-signed)")
    b_ok = verify_byte_equiv(inp, mine)
    c_ok = True
    if a.toc1_path:
        c_ok = verify_chain(mine, a.toc1_path)

    print("\n==================== SUMMARY ====================")
    print(f"structural (length/magic/add_sum) ......... {'PASS' if s_ok else 'FAIL'}")
    print(f"byte-equiv-except-crypto, item1 untouched .. {'PASS' if b_ok else 'FAIL'}")
    print(f"item0 cert's own signature ................. UNVERIFIED (not claimed)")
    if a.toc1_path:
        print(f"chains to toc1 rootkey (same modulus) ...... {'PASS' if c_ok else 'FAIL'}")
    return 0 if (s_ok and b_ok and c_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
