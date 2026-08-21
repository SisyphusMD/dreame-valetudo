"""Recovery provenance rejects ambiguous or incomplete generations before they become trusted."""

from __future__ import annotations

from pathlib import Path

import pytest

from dreame_valetudo.constants import RECOVERY_DUMP_NAMES
from dreame_valetudo.recovery import read_recovery_provenance, write_recovery_provenance


@pytest.mark.parametrize(
    ("binding", "firmware_state", "message"),
    [
        ("invented", "unverified", "unsupported recovery binding"),
        ("captured-same-session", "rooted", "unsupported recovery firmware state"),
    ],
)
def test_provenance_refuses_unknown_security_classifications(
    tmp_path: Path, binding: str, firmware_state: str, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        write_recovery_provenance(
            tmp_path,
            config="a" * 32,
            model_key="x40-ultra",
            binding=binding,
            firmware_state=firmware_state,
            expected_bytes=4,
        )


def test_provenance_refuses_to_publish_without_one_complete_generation(tmp_path: Path) -> None:
    for name in RECOVERY_DUMP_NAMES:
        (tmp_path / f"{name}.bin").write_bytes(b"short")

    with pytest.raises(ValueError, match="no complete recovery source generation"):
        write_recovery_provenance(
            tmp_path,
            config="a" * 32,
            model_key="x40-ultra",
            binding="captured-same-session",
            firmware_state="unverified",
            expected_bytes=1024,
        )

    assert not (tmp_path / "recovery-provenance.json").exists()


def test_provenance_record_must_be_a_regular_file(tmp_path: Path) -> None:
    (tmp_path / "recovery-provenance.json").mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        read_recovery_provenance(tmp_path)
