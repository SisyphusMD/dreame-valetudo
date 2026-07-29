#!/usr/bin/env python3
"""Offline re-sign of an Allwinner sunxi-secure TOC0 with a fresh throwaway dev key.

TOC0 layout (D10S Plus r2240, confirmed byte-identical structure to the X40 boot0 build --
see chapter 06): header + configuration + item0 (764B X.509-like root cert) + item1
(94208B boot0/SPL executable) + zero pad, fixed total length 0x18000 (98304B).

Method: template-splice, mirroring resign_toc1_generic.py's approach but for TOC0's different
(non-standard) cert encoding:
  * modulus (256B) and exponent -> replaced with the fresh root key's public numbers.
  * serial (1B) / notBefore / notAfter (13B UTCTIME) -> refreshed for hygiene, non-critical.
  * item1 (boot0 code) is NOT touched, so the extensions field's pinned sha256(item1) stays
    valid with no recomputation needed.
  * the item0 signature is regenerated with the raw-RSA convention proved in chapter 06.
  * header add_sum recomputed with the same stamp-and-sum algorithm as TOC1's.

The signature is the right-aligned raw RSA result for a firmware-specific, off-by-four TBS
span. This produces an internally valid image, not an image trusted by the robot: the BROM still
rejects a new root key when its ROTPK is burned. See chapters 06 and 12.

Run:  uv run --with cryptography python3 resign_toc0.py --in IN.img --out OUT.img \
          [--root-key-in KEY.pem | --root-key-out KEY.pem]
"""

import argparse
import datetime
import json
import struct
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from toc0 import (
    CERT_LEN,
    CERT_OFF,
    ITEM1_LEN,
    ITEM1_OFF,
    STAMP,
    Cert0,
    check_layout,
    signed_tbs_digest,
    verify_raw_signature,
)


def resign(in_path, out_path, root_key, prov_path=None):
    input_path = Path(in_path)
    output_path = Path(out_path)
    buf = bytearray(input_path.read_bytes())
    check_layout(buf)
    cert = Cert0(buf, CERT_OFF, CERT_LEN)

    pub = root_key.public_key().public_numbers()
    newmod = pub.n.to_bytes(256, "big")
    explen = cert.exponent[1] - cert.exponent[0]
    newexp = pub.e.to_bytes(explen, "big")

    buf[cert.modulus[0] : cert.modulus[1]] = newmod
    buf[cert.exponent[0] : cert.exponent[1]] = newexp

    now = datetime.datetime(2026, 7, 20, 12, 0, 0)
    nb = now.strftime("%y%m%d%H%M%SZ").encode()
    na = (now + datetime.timedelta(days=30)).strftime("%y%m%d%H%M%SZ").encode()
    if len(nb) != 13 or len(na) != 13:
        raise RuntimeError("generated TOC0 validity dates do not fit their fixed-width fields")
    buf[cert.notbefore[0] : cert.notbefore[1]] = nb
    buf[cert.notafter[0] : cert.notafter[1]] = na
    buf[cert.serial[0] : cert.serial[1]] = bytes(
        [0x03]
    )  # 1B, must stay < 0x80 (positive INTEGER, no pad)

    digest = signed_tbs_digest(buf, cert)
    private = root_key.private_numbers()
    signature = pow(int.from_bytes(digest, "big"), private.d, private.public_numbers.n)
    buf[cert.sig[0] : cert.sig[1]] = signature.to_bytes(256, "big")
    if not verify_raw_signature(buf, cert):
        raise RuntimeError("generated TOC0 signature did not verify")

    struct.pack_into("<I", buf, 0xC, STAMP)
    s = sum(struct.unpack_from("<I", buf, i)[0] for i in range(0, len(buf), 4)) & 0xFFFFFFFF
    struct.pack_into("<I", buf, 0xC, s)

    output_path.write_bytes(buf)
    provenance = {
        "modulus": list(cert.modulus),
        "exponent": list(cert.exponent),
        "serial": list(cert.serial),
        "notbefore": list(cert.notbefore),
        "notafter": list(cert.notafter),
        "addsum": [0xC, 0x10],
        "signature_RESIGNED_raw_rsa": list(cert.sig),
        "item1_UNTOUCHED": [ITEM1_OFF, ITEM1_OFF + ITEM1_LEN],
        "root_key_sha256_of_modulus": __import__("hashlib").sha256(newmod).hexdigest(),
    }
    if prov_path is None:
        provenance_path = Path(f"{output_path}.provenance.json")
    else:
        provenance_path = Path(prov_path)
    provenance_path.write_text(json.dumps(provenance, indent=0))
    print(
        f"wrote {output_path} ({len(buf)} bytes); provenance -> {provenance_path}",
        file=sys.stderr,
    )


def main():
    ap = argparse.ArgumentParser(
        description="Re-sign a sunxi TOC0 (boot0/SPL container) with a fresh dev key."
    )
    ap.add_argument(
        "--in", dest="in_path", type=Path, required=True, help="input toc0 image (98304 B)"
    )
    ap.add_argument(
        "--out", dest="out_path", type=Path, required=True, help="output re-signed toc0"
    )
    ap.add_argument(
        "--root-key-in",
        dest="root_key_in",
        default=None,
        help="PEM private key to use as the root key (share with resign_toc1_generic.py "
        "--root-key-in to build a self-consistent chain)",
    )
    ap.add_argument(
        "--root-key-out",
        dest="root_key_out",
        default=None,
        help="write the (possibly freshly generated) root private key here as PEM",
    )
    ap.add_argument(
        "--prov",
        dest="prov_path",
        default=None,
        help="provenance JSON (default: <out>.provenance.json)",
    )
    a = ap.parse_args()

    if a.root_key_in:
        root_key = serialization.load_pem_private_key(
            Path(a.root_key_in).read_bytes(), password=None
        )
        print(f"using existing root key from {a.root_key_in}", file=sys.stderr)
    else:
        print("generating fresh RSA-2048 root key ...", file=sys.stderr)
        root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    if a.root_key_out:
        pem = root_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        Path(a.root_key_out).write_bytes(pem)
        print(f"wrote root key -> {a.root_key_out}", file=sys.stderr)

    resign(a.in_path, a.out_path, root_key, a.prov_path)


if __name__ == "__main__":
    main()
