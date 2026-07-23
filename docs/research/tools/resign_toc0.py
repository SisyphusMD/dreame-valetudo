#!/usr/bin/env python3
"""Offline re-sign of an Allwinner sunxi-secure TOC0 with a fresh throwaway dev key.

TOC0 layout (D10S Plus r2240, confirmed byte-identical structure to the X40 boot0 build --
see recon task A/C in the working notes): header (32B) + 2-item table + item0 (764B X.509-like
root cert, non-RFC5280 DER: bare RSAPublicKey with no BIT STRING wrapper, 1-byte serial, a
custom extensions field pinning sha256(item1), and a non-standard trailing signature field
whose scheme could not be identified offline -- see NOTE below) + item1 (94208B boot0/SPL
executable) + zero pad, fixed total length 0x18000 (98304B).

Method: template-splice, mirroring resign_toc1_generic.py's approach but for TOC0's different
(non-standard) cert encoding:
  * modulus (256B) and exponent -> replaced with the fresh root key's public numbers.
  * serial (1B) / notBefore / notAfter (13B UTCTIME) -> refreshed for hygiene, non-critical.
  * item1 (boot0 code) is NOT touched, so the extensions field's pinned sha256(item1) stays
    valid with no recomputation needed.
  * header add_sum recomputed with the same stamp-and-sum algorithm as TOC1's.

NOTE -- the item0 cert's trailing 273-byte field is left BYTE-IDENTICAL to stock (not
re-signed). Offline analysis (recon task C) exhausted PKCS1v15-SHA256 and RSASSA-PSS-SHA256
(salt 0/32/max) over every plausible TBS byte range and found no valid signature under the
cert's own embedded key -- the scheme is unidentified (likely an older/proprietary Allwinner
boot0-signing convention, not the "sunxi-secure" X.509 tooling TOC1 uses). Since this cert is
verified only by the mask-ROM BROM -- which cannot be statically disassembled and which recon
task B's hardware evidence (unburned eFuse ROTPK) already established SKIPS toc0-signature
verification entirely on this unit -- leaving stale signature bytes is harmless for the
unburned-eFuse bench-test path. It would NOT be valid on a burned-eFuse unit; do not assume
otherwise. This tool refuses to claim the field is valid: see verify_toc0_generic.py.

Run:  uv run --with cryptography python3 resign_toc0.py --in IN.img --out OUT.img \
          [--root-key-in KEY.pem | --root-key-out KEY.pem]
"""
import argparse, struct, sys, json, datetime
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

STAMP = 0x5F0A6C39                 # sunxi toc add_sum stamp (same algorithm as toc1)
TOC0_LEN = 0x18000                 # 98304 -- fixed container length, hard constraint
MAGIC = 0x89119800
ITEMS_NR = 2
CERT_OFF, CERT_LEN = 0xc80, 0x2fc  # item0 (root cert)
ITEM1_OFF, ITEM1_LEN = 0xf80, 0x17000  # item1 (boot0/SPL binary) -- left untouched

# ---- DER helpers (same as resign_toc1_generic.py) --------------------------
def der_len(b, i):
    l = b[i]; i += 1
    if l < 0x80: return l, i
    n = l & 0x7f; return int.from_bytes(b[i:i+n], "big"), i + n

def children(b, s, e):
    out = []; i = s
    while i < e:
        t = b[i]; ln, j = der_len(b, i + 1); out.append((t, i, j, j + ln)); i = j + ln
    return out

def seq(b, off):
    assert b[off] == 0x30, f"not SEQ @{off:#x} (tag={b[off]:#x})"
    ln, j = der_len(b, off + 1); return j, j + ln


class Cert0:
    """Locate the length-fixed splice slices inside TOC0's item0 cert (absolute file offsets).

    Structurally different from resign_toc1_generic.py's Cert -- not a subclass:
      * outer cert has 2 DER children (tbs, signature), not 3 (no separate sigAlgorithm)
      * serial is 1 byte, not 9
      * SubjectPublicKeyInfo wraps RSAPublicKey directly, no BIT STRING
      * signature is a non-standard nested AlgorithmIdentifier + malformed BIT STRING header
        (no unused-bits byte) around a raw 256B field -- located but NOT verified (see module
        docstring); exposed as .sig purely so callers can assert it is left untouched.
    """
    def __init__(self, buf, off, ln):
        self.off, self.len = off, ln
        cs, ce = seq(buf, off)
        assert ce == off + ln, f"cert outer SEQ end {ce:#x} != declared item end {off+ln:#x}"
        ch = children(buf, cs, ce)
        assert len(ch) == 2, f"toc0 cert: expected 2 outer children (tbs, sig), got {len(ch)}"

        self.tbs = (ch[0][1], ch[0][3])
        tch = children(buf, ch[0][2], ch[0][3])
        assert len(tch) == 8, f"tbs: expected 8 children, got {len(tch)}"
        # tbs children: [0]=[0]version [1]serial [2]sigalg [3]issuer [4]validity
        #               [5]subject [6]spki [7]=[3]extensions
        self.serial = (tch[1][2], tch[1][3])
        assert self.serial[1] - self.serial[0] == 1, "serial not 1B"

        vch = children(buf, tch[4][2], tch[4][3])
        self.notbefore = (vch[0][2], vch[0][3])
        self.notafter = (vch[1][2], vch[1][3])
        assert self.notbefore[1] - self.notbefore[0] == 13, "notBefore not 13B UTCTIME"
        assert self.notafter[1] - self.notafter[0] == 13, "notAfter not 13B UTCTIME"

        # SubjectPublicKeyInfo -> AlgorithmIdentifier, RSAPublicKey SEQUENCE (bare, no BIT STRING)
        spki = children(buf, tch[6][2], tch[6][3])
        assert len(spki) == 2, "unexpected SPKI shape"
        rsapub = children(buf, *seq(buf, spki[1][1]))
        assert len(rsapub) == 2, "unexpected RSAPublicKey shape"
        m = rsapub[0]
        ms = m[2] + (1 if buf[m[2]] == 0 else 0)   # skip DER sign byte if present
        self.modulus = (ms, m[3])
        assert m[3] - ms == 256, "modulus not 256B"
        e = rsapub[1]
        self.exponent = (e[2], e[3])                # keep whatever length stock uses (3B/010001)

        # extensions [3]: holds sha256(item1) -- located for provenance/reporting only,
        # never written (item1 is never modified so this stays valid unchanged).
        self.extensions = (tch[7][1], tch[7][3])

        # signature: outer child[1] is a (non-standard) BIT STRING; its content is
        # AlgorithmIdentifier(13B) + malformed-BIT-STRING-header(4B) + raw 256B field.
        sig_field = ch[1]
        assert sig_field[0] == 0x03, "expected BIT STRING tag for signature field"
        content = sig_field[2]
        algid_len = 2 + buf[content + 1]             # SEQ header(2) + declared content len
        bitstr_hdr = content + algid_len
        assert buf[bitstr_hdr] == 0x03, "expected nested BIT STRING header"
        raw_start = bitstr_hdr + 4                    # tag(1) + 3-byte length form, no unused-bits byte
        self.sig_algid = (content, content + algid_len)
        self.sig = (raw_start, raw_start + 256)
        assert self.sig[1] == sig_field[3], "signature field does not close at cert end"


def check_layout(buf):
    assert len(buf) == TOC0_LEN, f"toc0 len {len(buf)} != {TOC0_LEN:#x}"
    assert buf[:8] == b"TOC0.GLH", "missing TOC0.GLH magic"
    assert struct.unpack_from("<I", buf, 8)[0] == MAGIC, "bad head magic"
    assert struct.unpack_from("<I", buf, 0x18)[0] == ITEMS_NR, "items_nr != 2"
    assert struct.unpack_from("<I", buf, 0x1c)[0] == TOC0_LEN, "declared length != 0x18000"
    item0_off, item0_len = struct.unpack_from("<II", buf, 0x2c + 8)
    item1_off, item1_len = struct.unpack_from("<II", buf, 0x4c + 8)
    assert (item0_off, item0_len) == (CERT_OFF, CERT_LEN), "item0 (cert) offset/len drifted"
    assert (item1_off, item1_len) == (ITEM1_OFF, ITEM1_LEN), "item1 (boot0 code) offset/len drifted"


def resign(in_path, out_path, root_key, prov_path=None):
    buf = bytearray(open(in_path, "rb").read())
    check_layout(buf)
    cert = Cert0(buf, CERT_OFF, CERT_LEN)

    pub = root_key.public_key().public_numbers()
    newmod = pub.n.to_bytes(256, "big")
    explen = cert.exponent[1] - cert.exponent[0]
    newexp = pub.e.to_bytes(explen, "big")

    buf[cert.modulus[0]:cert.modulus[1]] = newmod
    buf[cert.exponent[0]:cert.exponent[1]] = newexp

    now = datetime.datetime(2026, 7, 20, 12, 0, 0)
    nb = now.strftime("%y%m%d%H%M%SZ").encode()
    na = (now + datetime.timedelta(days=30)).strftime("%y%m%d%H%M%SZ").encode()
    assert len(nb) == 13 and len(na) == 13
    buf[cert.notbefore[0]:cert.notbefore[1]] = nb
    buf[cert.notafter[0]:cert.notafter[1]] = na
    buf[cert.serial[0]:cert.serial[1]] = bytes([0x03])   # 1B, must stay < 0x80 (positive INTEGER, no pad)

    # item1 (boot0 code) and cert.sig are deliberately left untouched -- see module docstring.

    struct.pack_into("<I", buf, 0xc, STAMP)
    s = sum(struct.unpack_from("<I", buf, i)[0] for i in range(0, len(buf), 4)) & 0xffffffff
    struct.pack_into("<I", buf, 0xc, s)

    open(out_path, "wb").write(buf)
    provenance = {
        "modulus": list(cert.modulus), "exponent": list(cert.exponent),
        "serial": list(cert.serial), "notbefore": list(cert.notbefore),
        "notafter": list(cert.notafter), "addsum": [0xc, 0x10],
        "signature_UNTOUCHED_unverified": list(cert.sig),
        "item1_UNTOUCHED": [ITEM1_OFF, ITEM1_OFF + ITEM1_LEN],
        "root_key_sha256_of_modulus": __import__("hashlib").sha256(newmod).hexdigest(),
    }
    if prov_path is None:
        prov_path = out_path + ".provenance.json"
    json.dump(provenance, open(prov_path, "w"), indent=0)
    print(f"wrote {out_path} ({len(buf)} bytes); provenance -> {prov_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="Re-sign a sunxi TOC0 (boot0/SPL container) with a fresh dev key.")
    ap.add_argument("--in", dest="in_path", required=True, help="input toc0 image (98304 B)")
    ap.add_argument("--out", dest="out_path", required=True, help="output re-signed toc0")
    ap.add_argument("--root-key-in", dest="root_key_in", default=None,
                     help="PEM private key to use as the root key (share with resign_toc1_generic.py "
                          "--root-key-in to build a self-consistent chain)")
    ap.add_argument("--root-key-out", dest="root_key_out", default=None,
                     help="write the (possibly freshly generated) root private key here as PEM")
    ap.add_argument("--prov", dest="prov_path", default=None, help="provenance JSON (default: <out>.provenance.json)")
    a = ap.parse_args()

    if a.root_key_in:
        root_key = serialization.load_pem_private_key(open(a.root_key_in, "rb").read(), password=None)
        print(f"using existing root key from {a.root_key_in}", file=sys.stderr)
    else:
        print("generating fresh RSA-2048 root key ...", file=sys.stderr)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    if a.root_key_out:
        pem = root_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption())
        open(a.root_key_out, "wb").write(pem)
        print(f"wrote root key -> {a.root_key_out}", file=sys.stderr)

    resign(a.in_path, a.out_path, root_key, a.prov_path)


if __name__ == "__main__":
    main()
