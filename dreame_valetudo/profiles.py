"""Device profiles — the single, typed source of truth for every supported robot.

Every robot is the same Allwinner MR813 "gen3" family; a model is just a profile (name, dustbuilder
page, Valetudo class, DRAM, and — for the older UART devices — method/arch/secure-boot). The table
is pinned by ``tests/python/test_profiles.py`` against checked-in goldens so it cannot silently
drift.

The three lookups are deliberately kept separate:
  * ``load_profile``          — the picker roster (models this tool drives end to end)
  * ``impl_class_for_model``  — a SUPERSET map (robot-reported code -> Valetudo class) used by
                                fix-impl, covering codes not in the picker
  * ``model_key_for_dir``     — infer a robot's key from its work-dir name on resume
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

# The stringly-typed profile discriminants. The string VALUES are the on-disk state-file /
# golden-TSV contract (recon writes model_key; goldens pin every field), so these Literals only
# narrow the types — the serialized forms stay byte-identical.
Method = Literal["fastboot", "uart"]
Dram = Literal["ddr3", "ddr4"]
YesNo = Literal["yes", "no"]
Autodetect = Literal["yes", "no", "maybe"]
Arch = Literal["aarch64", "armv7", "armv7-lowmem"]

DUSTBUILDER_HOST = "builder.dontvacuum.me"
STAGE1_URL = f"https://{DUSTBUILDER_HOST}/nextgen/dust-fel-mr813.tar.gz"

# The model worked on when nothing else selects one (also the pre-picker default).
DEFAULT_MODEL_KEY = "x40-ultra"

# Interactive picker order: the MR813 gen3 FASTBOOT flow first, then the older UART serial-shell
# devices. The order is load-bearing — the menu numbers robots by it — so it is pinned by a test.
SUPPORTED_MODELS: list[str] = [
    "x40-ultra", "x40-master", "x30-ultra",
    "l40-ultra", "l20-ultra", "l10s-ultra", "l10s-pro-ultra-heat", "l10s-pro-ultra-heat-h",
    "d10s-pro", "d10s-plus", "w10-pro",
    "mova-s20-ultra", "mova-p10-pro-ultra",
    "z10-pro", "xiaomi-1t", "l10-pro", "x10-plus", "p2148", "vacuum-mop-2-ultra",
    "d9", "d9-pro", "f9", "xiaomi-1c", "w10", "mova-z500",
]

@dataclass(frozen=True, slots=True)
class Profile:
    """One robot's resolved profile. Fields with defaults are the shared MR813 gen3 platform
    values; a model overrides only what differs (DRAM for the ddr3 units; method/arch/secure-boot
    for the UART family). FEL load addresses are fixed for this SoC family."""

    key: str
    model: str
    dust_code: str
    model_code: str
    impl_class: str
    autodetect_ok: Autodetect
    method: Method = "fastboot"
    arch: Arch = "aarch64"
    dram: Dram = "ddr4"           # only changes which FSBL is pushed
    secure_boot: YesNo = "yes"
    baud: str = "115200"
    fsbl_addr: str = "0x28000"
    payload_addr: str = "0x4a000000"

    def __post_init__(self) -> None:
        # A static table, so these are dev-time guards against a bad edit (mypy catches bad literals
        # statically; this catches a stray value from a non-checked edit), not runtime validation.
        for field, allowed in (
            ("method", get_args(Method)),
            ("dram", get_args(Dram)),
            ("secure_boot", get_args(YesNo)),
            ("autodetect_ok", get_args(Autodetect)),
            ("arch", get_args(Arch)),
        ):
            value = getattr(self, field)
            if value not in allowed:
                raise ValueError(f"{self.key}: bad {field} {value!r}")

    @property
    def stage1_url(self) -> str:
        return STAGE1_URL

    @property
    def dustbuilder_page(self) -> str:
        return f"https://{DUSTBUILDER_HOST}/_dreame_{self.dust_code}.html"


_PROFILES: dict[str, Profile] = {
    p.key: p
    for p in [
        # ---- X-series (ddr4) ----
        Profile("x40-ultra", "Dreame X40 Ultra", "r2416", "r2416",
                "DreameX40UltraValetudoRobot", "no"),
        Profile("x40-master", "Dreame X40 Master", "r2465", "r2465",
                "DreameX40MasterValetudoRobot", "maybe"),
        Profile("x30-ultra", "Dreame X30 Ultra", "r9316", "r9316",
                "DreameX30UltraValetudoRobot", "yes"),
        # ---- L-series (ddr4) ----
        Profile("l40-ultra", "Dreame L40 Ultra", "r2492", "r2492",
                "DreameL40UltraValetudoRobot", "maybe"),
        Profile("l20-ultra", "Dreame L20 Ultra", "r2394", "r2394",
                "DreameL20UltraValetudoRobot", "maybe"),
        Profile("l10s-ultra", "Dreame L10s Ultra", "r2228", "r2228",
                "DreameL10SUltraValetudoRobot", "maybe"),
        Profile("l10s-pro-ultra-heat", "Dreame L10s Pro Ultra Heat", "r2338", "r2338",
                "DreameL10SProUltraHeatValetudoRobot", "maybe"),
        Profile("l10s-pro-ultra-heat-h",
                "Dreame L10s Pro Ultra Heat (R2338H hardware revision)", "r2338h", "r2338h",
                "DreameL10SProUltraHeatValetudoRobot", "maybe"),
        # ---- D-series + W10 Pro: same MR813 flow, but ddr3 DRAM ----
        Profile("d10s-pro", "Dreame D10s Pro", "r2250", "r2250",
                "DreameD10SProValetudoRobot", "maybe", dram="ddr3"),
        Profile("d10s-plus", "Dreame D10s Plus", "r2240", "r2240",
                "DreameD10SPlusValetudoRobot", "maybe", dram="ddr3"),
        Profile("w10-pro", "Dreame W10 Pro", "r2104", "r2104",
                "DreameW10ProValetudoRobot", "maybe", dram="ddr3"),
        # ---- Mova-branded (ddr4) ----
        Profile("mova-s20-ultra", "Mova S20 Ultra", "r2385", "r2385",
                "DreameMovaS20UltraValetudoRobot", "maybe"),
        Profile("mova-p10-pro-ultra", "Mova P10 Pro Ultra", "r2491", "r2491",
                "DreameMovaP10ProUltraValetudoRobot", "maybe"),
        # ---- UART serial-shell method (older MR813-NAND + armv7 devices) ----
        Profile("z10-pro", "Dreame Z10 Pro", "p2028", "p2028",
                "DreameZ10ProValetudoRobot", "maybe", method="uart", secure_boot="yes"),
        Profile("xiaomi-1t", "Xiaomi 1T", "p2041", "p2041",
                "Dreame1TValetudoRobot", "maybe", method="uart", secure_boot="no"),
        Profile("l10-pro", "Dreame L10 Pro", "p2029", "p2029",
                "DreameL10ProValetudoRobot", "maybe", method="uart", secure_boot="yes"),
        Profile("x10-plus", "Xiaomi X10+", "p2114", "p2114",
                "DreameX10PlusValetudoRobot", "maybe", method="uart", secure_boot="yes"),
        Profile("p2148", "Xiaomi P2148 (Mijia Ultra Slim)", "p2148", "p2148",
                "DreameP2148ValetudoRobot", "maybe", method="uart", secure_boot="no"),
        Profile("vacuum-mop-2-ultra", "Xiaomi Vacuum-Mop 2 Ultra (P2150)", "p2150", "p2150",
                "DreameP2150ValetudoRobot", "maybe", method="uart", secure_boot="yes"),
        Profile("d9", "Dreame D9", "p2009", "p2009",
                "DreameD9ValetudoRobot", "maybe", method="uart", arch="armv7-lowmem",
                secure_boot="no"),
        Profile("d9-pro", "Dreame D9 Pro", "p2187", "p2187",
                "DreameD9ProValetudoRobot", "maybe", method="uart", arch="armv7-lowmem",
                secure_boot="no"),
        Profile("f9", "Dreame F9", "p2008", "p2008",
                "DreameF9ValetudoRobot", "maybe", method="uart", arch="armv7", secure_boot="no"),
        Profile("xiaomi-1c", "Xiaomi 1C", "mc1808", "mc1808",
                "Dreame1CValetudoRobot", "maybe", method="uart", arch="armv7", secure_boot="no"),
        Profile("w10", "Dreame W10", "p2027", "p2027",
                "DreameW10ValetudoRobot", "maybe", method="uart", arch="armv7-lowmem",
                secure_boot="no"),
        Profile("mova-z500", "Mova Z500", "p2156", "p2156",
                "DreameMovaZ500ValetudoRobot", "maybe", method="uart", arch="armv7",
                secure_boot="no"),
    ]
}


def load_profile(key: str) -> Profile:
    """Resolve a model key to its profile, or raise on an unknown key."""
    try:
        return _PROFILES[key]
    except KeyError:
        raise ValueError(
            f"Unknown model key {key!r} — supported: {' '.join(SUPPORTED_MODELS)}"
        ) from None


# Robot-reported dreame.vacuum.<code> -> Valetudo implementation class. A SUPERSET of the picker
# (used by fix-impl) that follows Valetudo's own per-class codes for BOTH families. Matched by
# PREFIX, so regional/colour suffixes (…a/…c/…k/…o/…t) resolve to the base class. Ordered so no
# earlier prefix is a prefix of a later, different-class code.
# p2187 (D9 Pro / D9 Pro+) is intentionally ABSENT: the two share the code and are told apart by a
# /etc/dustbuilder_backport marker, not the code, so autodetect decides rather than a wrong pin.
_IMPL_PREFIXES: list[tuple[str, str]] = [
    ("dreame.vacuum.r2416", "DreameX40UltraValetudoRobot"),
    ("dreame.vacuum.r2449", "DreameX40UltraValetudoRobot"),
    ("dreame.vacuum.r2465", "DreameX40MasterValetudoRobot"),
    ("dreame.vacuum.r9316", "DreameX30UltraValetudoRobot"),
    ("dreame.vacuum.r2338", "DreameL10SProUltraHeatValetudoRobot"),
    ("dreame.vacuum.r2228", "DreameL10SUltraValetudoRobot"),
    ("dreame.vacuum.r2216", "DreameL10SProValetudoRobot"),
    ("dreame.vacuum.r2257", "DreameL10UltraValetudoRobot"),
    ("dreame.vacuum.r2250", "DreameD10SProValetudoRobot"),
    ("dreame.vacuum.r2240", "DreameD10SPlusValetudoRobot"),
    ("dreame.vacuum.r2211", "DreameS10PlusValetudoRobot"),
    ("dreame.vacuum.r2104", "DreameW10ProValetudoRobot"),
    ("dreame.vacuum.r2394", "DreameL20UltraValetudoRobot"),
    ("dreame.vacuum.r2492", "DreameL40UltraValetudoRobot"),
    ("dreame.vacuum.r2385", "DreameMovaS20UltraValetudoRobot"),
    ("dreame.vacuum.r2491", "DreameMovaP10ProUltraValetudoRobot"),
    ("mova.vacuum.r2491", "DreameMovaP10ProUltraValetudoRobot"),
    ("dreame.vacuum.ma1808", "Dreame1CValetudoRobot"),
    ("dreame.vacuum.mb1808", "Dreame1CValetudoRobot"),
    ("dreame.vacuum.mc1808", "Dreame1CValetudoRobot"),
    ("dreame.vacuum.p2041", "Dreame1TValetudoRobot"),
    ("dreame.vacuum.p2148", "DreameP2148ValetudoRobot"),
    ("dreame.vacuum.p2149", "DreameP2149ValetudoRobot"),
    ("dreame.vacuum.p2150", "DreameP2150ValetudoRobot"),
    ("dreame.vacuum.p2008", "DreameF9ValetudoRobot"),
    ("dreame.vacuum.p2009", "DreameD9ValetudoRobot"),
    ("dreame.vacuum.p2027", "DreameW10ValetudoRobot"),
    ("dreame.vacuum.p2028", "DreameZ10ProValetudoRobot"),
    ("dreame.vacuum.p2029", "DreameL10ProValetudoRobot"),
    ("dreame.vacuum.p2114", "DreameX10PlusValetudoRobot"),
    ("dreame.vacuum.p2156", "DreameMovaZ500ValetudoRobot"),
]


def impl_class_for_model(code: str) -> str | None:
    """Valetudo implementation class for a robot-reported model code, or None if unknown."""
    for prefix, cls in _IMPL_PREFIXES:
        if code.startswith(prefix):
            return cls
    return None


# Robot work-dir name prefix (<code>-) -> model key, for resuming a robot created before models
# were selectable. r2338h- MUST precede r2338-; with dash-delimited prefixes neither actually
# swallows the other, but the order is defence in depth.
_DIR_PREFIX_TO_KEY: list[tuple[str, str]] = [
    ("r2416-", "x40-ultra"),
    ("r2449-", "x40-ultra"),
    ("r2465-", "x40-master"),
    ("r9316-", "x30-ultra"),
    ("r2492-", "l40-ultra"),
    ("r2394-", "l20-ultra"),
    ("r2228-", "l10s-ultra"),
    ("r2338h-", "l10s-pro-ultra-heat-h"),
    ("r2338-", "l10s-pro-ultra-heat"),
    ("r2250-", "d10s-pro"),
    ("r2240-", "d10s-plus"),
    ("r2104-", "w10-pro"),
    ("r2385-", "mova-s20-ultra"),
    ("r2491-", "mova-p10-pro-ultra"),
    ("p2028-", "z10-pro"),
    ("p2041-", "xiaomi-1t"),
    ("p2029-", "l10-pro"),
    ("p2114-", "x10-plus"),
    ("p2148-", "p2148"),
    ("p2150-", "vacuum-mop-2-ultra"),
    ("p2009-", "d9"),
    ("p2187-", "d9-pro"),
    ("p2008-", "f9"),
    ("mc1808-", "xiaomi-1c"),
    ("p2027-", "w10"),
    ("p2156-", "mova-z500"),
]


def key_from_dirname(basename: str) -> str:
    """Infer a model key from a robot work-dir basename, falling back to the default key."""
    for prefix, key in _DIR_PREFIX_TO_KEY:
        if basename.startswith(prefix):
            return key
    return DEFAULT_MODEL_KEY


def model_key_for_dir(dir_path: str | os.PathLike[str]) -> str:
    """The saved state/model_key marker if present and non-empty, else inferred from the dir name."""
    d = Path(dir_path)
    marker = d / "state" / "model_key"
    if marker.is_file():
        saved = marker.read_text().strip()
        if saved:
            return saved
    return key_from_dirname(d.name)
