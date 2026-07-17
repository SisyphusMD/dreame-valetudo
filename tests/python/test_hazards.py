"""Per-model brick-guard prompts (model_hazard_check).

These are the look-alike / hardware-revision warnings whose whole purpose is to stop a wrong-image
flash. The interactive path must ABORT on a declined confirm; the non-interactive path warns but
does not block (recon reads the real model code next, non-destructively).
"""

from __future__ import annotations

import pytest
from conftest import CtxFactory

from dreame_valetudo.console import Die
from dreame_valetudo.hazards import model_hazard_check


def _warned(ctx: object, needle: str) -> bool:
    return any(needle in msg for kind, msg in ctx.console.lines if kind == "warn")  # type: ignore[attr-defined]


def test_heat_revision_aborts_when_serial_not_confirmed(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat", confirms=[False])
    with pytest.raises(Die, match="Verify the serial"):
        model_hazard_check(ctx)
    assert _warned(ctx, "R2338H")


def test_heat_revision_proceeds_when_serial_confirmed(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l10s-pro-ultra-heat-h", confirms=[True])
    model_hazard_check(ctx)  # no raise
    assert _warned(ctx, "SINGLE character")


def test_l20_variant_aborts_when_declined(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="l20-ultra", confirms=[False])
    with pytest.raises(Die, match="R2394"):
        model_hazard_check(ctx)
    assert _warned(ctx, "R2253")


def test_hazard_check_is_silent_for_an_unaffected_model(make_ctx: CtxFactory) -> None:
    ctx = make_ctx(model="x40-ultra", confirms=[])
    model_hazard_check(ctx)  # no prompt, no raise
    assert ctx.console.lines == []  # type: ignore[attr-defined]


def test_heat_revision_does_not_block_non_interactive(make_ctx: CtxFactory) -> None:
    # This confirm is tty-gated; a piped run warns but proceeds (recon reads the real model next).
    ctx = make_ctx(model="l10s-pro-ultra-heat", interactive=False, confirms=[])
    model_hazard_check(ctx)  # no raise despite no confirmation
    assert _warned(ctx, "BRICKS the robot")
