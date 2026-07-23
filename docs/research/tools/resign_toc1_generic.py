#!/usr/bin/env python3
"""Offline re-sign of an Allwinner sunxi-secure TOC1 with a fresh throwaway dev key.

Path-parameterized (--in / --out) generalization of resign/resign_toc1.py. The internal
layout offsets are the X40 (r2416) calibration; they apply unchanged to any MR813 "gen3"
toc1 with the identical sunxi-secure layout (magic 0x89119800, items_nr 13, valid_len
0x130000) -- confirmed byte-for-byte identical on the D10S Plus (r2240).

Reproduces exactly what the dustbuilder does to the toc1 EXCEPT it uses our own RSA keys
instead of Dreame's fleet-wide private key. The output is a structurally-identical,
internally-valid secure-boot image: boot0 would accept it IFF the per-unit eFuse ROTPK is
unburned ("any key" mode). Nothing here touches hardware.

Method: template-splice. Keep the input toc1 byte-for-byte and overwrite only the
crypto-variable fields, all length-preserving, then recompute the header add_sum:
  * every cert: serial (8B), notBefore/notAfter (UTCTIME), subject RSA modulus (256B),
    signature (256B) -> re-signed over the rebuilt TBS with the fresh key.
  * rootkey cert additionally: the 6 pinned content-key moduli carried in its extensions
    (ASCII-hex "00"+mod+"10001") are replaced with the fresh content moduli.

Run:  uv run --with cryptography python3 resign_toc1_generic.py --in IN.img --out OUT.bin
"""
import argparse, os, re, struct, hashlib, datetime, sys, json
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

STAMP = 0x5F0A6C39            # sunxi toc add_sum stamp
TOC1_LEN = 1245184
MAGIC = 0x89119800
ITEMS_NR = 13
VALID_LEN = 0x130000

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

def seq(b, off):
    assert b[off] == 0x30, f"not SEQ @{off:#x}"
    ln, j = der_len(b, off + 1); return j, j + ln   # content start, content end

class Cert:
    """Locate the length-fixed splice slices inside one sunxi cert (absolute file offsets)."""
    def __init__(self, buf, off, name):
        self.name = name; self.off = off
        cs, ce = seq(buf, off)
        ch = children(buf, cs, ce)
        self.tbs = (ch[0][1], ch[0][3])                       # whole tbsCertificate DER
        tch = children(buf, ch[0][2], ch[0][3])
        # tbs children: [0]=[0]version, [1]serial, [2]sigalg, [3]issuer, [4]validity,
        #               [5]subject, [6]spki, [7]=[3]extensions
        self.serial = (tch[1][2], tch[1][3])                  # INTEGER value bytes (incl leading 00)
        vch = children(buf, tch[4][2], tch[4][3])
        self.notbefore = (vch[0][2], vch[0][3])
        self.notafter  = (vch[1][2], vch[1][3])
        # SubjectPublicKeyInfo -> BIT STRING -> RSAPublicKey SEQ -> modulus INTEGER
        spki = children(buf, tch[6][2], tch[6][3])
        bit = next(c for c in spki if c[0] == 0x03)
        pk = bit[2] + 1                                       # skip unused-bits byte
        rsapub = children(buf, seq(buf, pk)[0], seq(buf, pk)[1])
        m = rsapub[0]                                         # modulus INTEGER
        ms = m[2] + (1 if buf[m[2]] == 0 else 0)             # skip DER sign byte
        self.modulus = (ms, m[3])
        assert m[3] - ms == 256, f"{name} modulus not 256B"
        # signature BIT STRING (outer cert child[2])
        sig = ch[2]
        self.sig = (sig[2] + 1, sig[3])                       # skip unused-bits byte
        assert self.sig[1] - self.sig[0] == 256
        # UTCTIME dates are the only variable-length field we overwrite in place; a stock
        # gen3 cert always uses 13-byte UTCTIME. Fail loudly rather than change the length.
        assert self.notbefore[1] - self.notbefore[0] == 13, f"{name} notBefore not 13B UTCTIME"
        assert self.notafter[1] - self.notafter[0] == 13, f"{name} notAfter not 13B UTCTIME"
        assert self.serial[1] - self.serial[0] == 9, f"{name} serial not 9B"

# cert-name -> cert item offset (X40/D10S identical layout)
CERTS = {"rootkey": 0x1400, "monitor": 0x2400, "optee": 0x11c00,
         "u-boot": 0x4d400, "scp": 0xfd800, "boot": 0x112000, "rootfs": 0x112400}
# stock key-sharing groups (mirror them so the pinned set is faithful):
#   monitor=g1  optee=scp=g2  u-boot=g3  boot=rootfs=g5
KEYGROUPS = {"monitor": "g1", "optee": "g2", "scp": "g2",
             "u-boot": "g3", "boot": "g5", "rootfs": "g5"}
PIN_ORDER = ["monitor", "optee", "u-boot", "scp", "boot", "rootfs"]

def check_layout(buf):
    assert len(buf) == TOC1_LEN, f"toc1 len {len(buf)} != {TOC1_LEN}"
    assert buf[:12] == b"sunxi-secure", "missing sunxi-secure magic"
    assert struct.unpack_from("<I", buf, 16)[0] == MAGIC, "bad head magic"
    assert struct.unpack_from("<I", buf, 32)[0] == ITEMS_NR, "items_nr != 13"
    assert struct.unpack_from("<I", buf, 36)[0] == VALID_LEN, "valid_len != 0x130000"

def resign(in_path, out_path, prov_path=None, root_key=None):
    buf = bytearray(open(in_path, "rb").read())
    check_layout(buf)
    certs = {nm: Cert(buf, off, nm) for nm, off in CERTS.items()}

    if root_key is None:
        print("generating fresh RSA-2048 root key ...", file=sys.stderr)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        print("using externally-supplied root key (chain re-sign mode)", file=sys.stderr)
    print("generating fresh RSA-2048 content-group keys ...", file=sys.stderr)
    grp_keys = {g: rsa.generate_private_key(public_exponent=65537, key_size=2048)
                for g in set(KEYGROUPS.values())}
    content_key = {nm: grp_keys[g] for nm, g in KEYGROUPS.items()}

    def mod_bytes(k):
        return k.public_key().public_numbers().n.to_bytes(256, "big")

    now = datetime.datetime(2026, 7, 20, 12, 0, 0)
    nb = now.strftime("%y%m%d%H%M%SZ").encode()
    na = (now + datetime.timedelta(days=30)).strftime("%y%m%d%H%M%SZ").encode()

    def fresh_serial():
        v = bytearray(os.urandom(8)); v[0] |= 0x80          # keep 9-byte DER (leading 00) length
        return b"\x00" + bytes(v)

    # --- splice each cert's fixed fields ---------------------------------------
    provenance = {}
    for nm, c in certs.items():
        newmod = mod_bytes(root_key) if nm == "rootkey" else mod_bytes(content_key[nm])
        buf[c.modulus[0]:c.modulus[1]] = newmod
        buf[c.serial[0]:c.serial[1]] = fresh_serial()
        buf[c.notbefore[0]:c.notbefore[1]] = nb
        buf[c.notafter[0]:c.notafter[1]] = na
        provenance[nm] = dict(serial=c.serial, nb=c.notbefore, na=c.notafter,
                              mod=c.modulus, sig=c.sig)

    # rootkey pinned content moduli (ASCII-hex "00"+mod(512)+"10001") -- replace each in file order
    blocks = [m.start() for m in re.finditer(b"\x08\x82\x02\x07", buf)]
    assert len(blocks) == 6, f"expected 6 pinned blocks, got {len(blocks)}"
    pinned_slices = []
    for nm, b0 in zip(PIN_ORDER, blocks):
        s = b0 + 4
        ascii_val = ("00" + mod_bytes(content_key[nm]).hex() + "10001").encode()
        assert len(ascii_val) == 519
        buf[s:s+519] = ascii_val
        pinned_slices.append((nm, s, s + 519))

    # --- re-sign every cert over its rebuilt TBS -------------------------------
    for nm, c in certs.items():
        tbs = bytes(buf[c.tbs[0]:c.tbs[1]])
        key = root_key if nm == "rootkey" else content_key[nm]
        sig = key.sign(tbs, padding.PKCS1v15(), hashes.SHA256())
        assert len(sig) == 256
        buf[c.sig[0]:c.sig[1]] = sig

    # --- recompute header add_sum ----------------------------------------------
    struct.pack_into("<I", buf, 20, STAMP)
    s = sum(struct.unpack_from("<I", buf, i)[0] for i in range(0, len(buf), 4)) & 0xffffffff
    struct.pack_into("<I", buf, 20, s)

    open(out_path, "wb").write(buf)
    if prov_path is None:
        prov_path = out_path + ".provenance.json"
    prov = {nm: {k: list(v) if isinstance(v, tuple) else v for k, v in p.items()}
            for nm, p in provenance.items()}
    prov["pinned"] = [[nm, a, b] for (nm, a, b) in pinned_slices]
    prov["addsum"] = [20, 24]
    json.dump(prov, open(prov_path, "w"), indent=0)
    print(f"wrote {out_path} ({len(buf)} bytes); provenance -> {prov_path}", file=sys.stderr)

def main():
    ap = argparse.ArgumentParser(description="Re-sign a sunxi-secure toc1 with a fresh dev key.")
    ap.add_argument("--in", dest="in_path", required=True, help="input toc1 image (1245184 B)")
    ap.add_argument("--out", dest="out_path", required=True, help="output re-signed toc1")
    ap.add_argument("--prov", dest="prov_path", default=None,
                    help="provenance JSON (default: <out>.provenance.json)")
    ap.add_argument("--root-key-in", dest="root_key_in", default=None,
                    help="PEM private key to use as the rootkey-cert key instead of generating one "
                         "(share with resign_toc0.py --root-key-in/--root-key-out to build a chain)")
    a = ap.parse_args()
    root_key = None
    if a.root_key_in:
        root_key = serialization.load_pem_private_key(open(a.root_key_in, "rb").read(), password=None)
    resign(a.in_path, a.out_path, a.prov_path, root_key)

if __name__ == "__main__":
    main()
