"""Per-model "are you REALLY this model?" guards, for the cases where a wrong pick BRICKS.

Look-alikes that share a marketing name but not the rootable silicon, and hardware revisions that
need different firmware. Recon (non-destructive) reads the real device next, so these are
pre-flight sanity checks, not the last line of defence.
"""

from __future__ import annotations

from .console import die
from .context import Context


def model_hazard_check(ctx: Context) -> None:
    key = ctx.profile.key
    if key.startswith("l10s-pro-ultra-heat"):
        ctx.console.warn("The L10s Pro Ultra Heat has TWO hardware revisions — R2338 and R2338H —")
        ctx.console.warn("that take DIFFERENT firmware and differ by a SINGLE character in the")
        ctx.console.warn("serial number. Flashing the wrong image BRICKS the robot. Read the serial")
        ctx.console.warn(f"from under the dustbin and confirm it matches '{ctx.profile.model}'.")
        if ctx.interactive and not ctx.console.confirm(
            f"Does the serial confirm this is '{ctx.profile.model}'?"
        ):
            die("Verify the serial, then re-run and choose the matching entry.")
    elif key == "l20-ultra":
        ctx.console.warn("The L20 Ultra ships in TWO look-alike variants: only the MR813 hardware")
        ctx.console.warn("(model code R2394) is rootable. An identical-looking R2253 unit is NOT")
        ctx.console.warn("supported — attempting it can BRICK the robot. Recon (next, non-")
        ctx.console.warn("destructive) reads the real model code; stop if it isn't r2394.")
        if ctx.interactive and not ctx.console.confirm(
            "Continue with the L20 Ultra (R2394 / MR813)?"
        ):
            die("Verify the model code first (recon is non-destructive), then continue only if "
                "it's R2394.")
