"""Terminal output, prompts, and the Die control-flow exception.

Injected into phases so they are testable without real IO (a scripted console feeds canned
answers and captures output).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import NoReturn


class Die(Exception):
    """Abort with a message. Caught in main() -> print + exit 1."""


def die(message: str) -> NoReturn:
    raise Die(message)


def warn_if_low_disk(console: Console, dest: Path, need_bytes: int) -> None:
    """Emit a single warning (never blocks) when `dest`'s filesystem has less than `need_bytes`
    free, so a multi-GB pull/backup fails a check up front instead of part way through. Silently
    skips if free space can't be read."""
    probe = dest
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need_bytes:
        console.warn(f"Low disk space: {free // (1 << 20)} MB free at {dest}, but this needs "
                     f"~{need_bytes // (1 << 20)} MB. Free some space or it may fail partway.")


class Console:
    """Human-facing IO. Subclass to script prompts / capture output in tests."""

    def __init__(self, *, color: bool | None = None) -> None:
        # Colour is gated on the stream being a TTY, per stream, so redirected output is clean.
        # err() writes to stderr, so its colour tracks stderr's ttyness, not stdout's.
        self.color = sys.stdout.isatty() if color is None else color
        self.color_err = sys.stderr.isatty() if color is None else color

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def _ce(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color_err else text

    def say(self, message: str) -> None:
        print("\n" + self._c("1;36", f">> {message}"))

    def action(self, message: str) -> None:
        """A hands-on step the user must physically perform (PCB buttons, cables, power). Rendered
        as a high-visibility banner (bold black-on-yellow) so the one thing a script can't do
        stands out from the scrolling narrative and isn't skipped."""
        print("\n" + self._c("1;30;103", f"  ACTION >  {message}  "))

    def info(self, message: str) -> None:
        print(f"   {message}")

    def warn(self, message: str) -> None:
        print(self._c("1;33", f"!! {message}"))

    def err(self, message: str) -> None:
        print(self._ce("1;31", f"XX {message}"), file=sys.stderr)

    def confirm(self, prompt: str) -> bool:
        return self._prompt(self._c("1;35", f"?? {prompt} [y/N] ")).strip().lower() in ("y", "yes")

    def ask(self, prompt: str) -> str:
        return self._prompt(self._c("1;35", f"?? {prompt} "))

    def _prompt(self, rendered: str) -> str:
        try:
            return input(rendered)
        except EOFError:
            return ""
