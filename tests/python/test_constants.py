"""The pins in constants.py, checked for the drift no other test can see.

These read the source text, not the imported values: the coupling being pinned is between a
constant and an annotation that only exists in the file.
"""

from __future__ import annotations

import re
from pathlib import Path

from dreame_valetudo.constants import VALETUDO_SHA256, VALETUDO_VERSION_DEFAULT

_CONSTANTS = Path(__file__).resolve().parents[2] / "dreame_valetudo" / "constants.py"
_ANNOTATED_DIGEST = re.compile(
    r'"(?P<arch>[\w-]+)": "(?P<digest>[0-9a-f]{64})",\s*#\s*(?P<version>[0-9][\w.-]*)'
)


def _annotated_digests() -> dict[str, str]:
    return {
        m["arch"]: m["version"] for m in _ANNOTATED_DIGEST.finditer(_CONSTANTS.read_text())
    }


def test_every_pinned_digest_is_annotated_with_the_pinned_version() -> None:
    # The failure this exists for: Renovate moves VALETUDO_VERSION_DEFAULT but not the digests, so
    # the tool downloads the new release and checks it against the previous release's sha256 —
    # every default install dies on a digest mismatch. Nothing else catches it. The phase tests
    # deliberately derive their versions from the pin, so they stay green through exactly this bug,
    # and fetch.py only compares the digest against a download that never happens in tests.
    annotated = _annotated_digests()

    assert set(annotated) == set(VALETUDO_SHA256), (
        "every VALETUDO_SHA256 entry needs a '# <version>' annotation (and vice versa); "
        f"annotated={sorted(annotated)} pinned={sorted(VALETUDO_SHA256)}"
    )
    stale = {arch: v for arch, v in annotated.items() if v != VALETUDO_VERSION_DEFAULT}
    assert not stale, (
        f"digest annotations disagree with VALETUDO_VERSION_DEFAULT={VALETUDO_VERSION_DEFAULT}: "
        f"{stale}. The digests still describe another release — re-take them from that release's "
        "assets (each is published as the asset's sha256) before merging."
    )


def test_the_digest_annotations_are_actually_present_to_be_checked() -> None:
    # Guards the guard: if the annotation format changes, the regex above silently matches nothing
    # and the staleness check passes vacuously for every possible input.
    assert len(_annotated_digests()) == 3
