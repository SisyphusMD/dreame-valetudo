import sys, struct, hashlib
from cryptography.hazmat.primitives import serialization
sys.path.insert(0,"<research>/d10s-test")
from resign_toc0 import Cert0, CERT_OFF, CERT_LEN, STAMP
ART="<research>/d10s-builder-artifacts"
OUT="<scratch>"
buf=bytearray(open(f"{ART}/device_toc0_exact.img","rb").read())
key=serialization.load_pem_private_key(open(f"{ART}/root_dev_key.pem","rb").read(),password=None)
pn=key.private_numbers().public_numbers; n,e,d=pn.n,pn.e,key.private_numbers().d
cert=Cert0(buf,CERT_OFF,CERT_LEN)
# swap in our key
buf[cert.modulus[0]:cert.modulus[1]]=n.to_bytes(256,"big")
elen=cert.exponent[1]-cert.exponent[0]; buf[cert.exponent[0]:cert.exponent[1]]=e.to_bytes(elen,"big")
# Agent-1 hash range: tbs tag_start + declared-content-length bytes
ts=cert.tbs[0]; declared=(buf[ts+2]<<8)|buf[ts+3]
hin=bytes(buf[ts:ts+declared])
print(f"tbs tag@0x{ts:x} declared-len=0x{declared:x} hash_range=0x{ts:x}..0x{ts+declared:x} ({declared}B)")
H=hashlib.sha256(hin).digest()
sig=pow(int.from_bytes(H,"big"),d,n).to_bytes(256,"big")
buf[cert.sig[0]:cert.sig[1]]=sig
# add_sum
struct.pack_into("<I",buf,0xc,STAMP)
s=sum(struct.unpack_from("<I",buf,i)[0] for i in range(0,len(buf),4))&0xffffffff
struct.pack_into("<I",buf,0xc,s)
# STATIC VERIFY: recovered low-32 == our computed hash (raw RSA); modulus == our key
rec=pow(int.from_bytes(sig,"big"),e,n).to_bytes(256,"big")
ok_sig = rec[-32:]==H and rec[:224]==b"\x00"*224
ok_mod = bytes(buf[cert.modulus[0]:cert.modulus[1]])==n.to_bytes(256,"big")
# also re-derive hash AFTER modulus swap to be sure it covers our key
ok_covers_key = cert.modulus[0]>=ts and cert.modulus[1]<=ts+declared
open(OUT,"wb").write(buf)
print(f"wrote {OUT} sha256={hashlib.sha256(buf).hexdigest()[:16]}")
print(f"  item0 sig VERIFIES under our key (raw-RSA low32==H, 224 zero pad): {ok_sig}")
print(f"  modulus == our key: {ok_mod}   hash range covers modulus: {ok_covers_key}")
print(f"  ALL STATIC CHECKS: {'PASS' if (ok_sig and ok_mod and ok_covers_key) else 'FAIL'}")
