"""dustbuilder web-form signature + drift check.

The image phase tells you what to enter in Dennis Giese's web form. If he renames a field or moves
an option, that guidance goes stale, so the check records a stable structural signature (control field
names + the format/key option values; the per-load auth/image tokens are excluded) and diff it
against a committed baseline to flag drift loudly.
"""

from __future__ import annotations

from html.parser import HTMLParser


class _FormSig(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sig: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("input", "select", "textarea", "button"):
            a = dict(attrs)
            name = a.get("name")
            if name:
                # Include option values only for the fields the runbook branches on.
                self.sig.add(f"{name}={a.get('value') or ''}" if name in ("format", "key") else name)


def form_signature(html: str) -> str:
    """A stable, sorted structural signature of the dustbuilder form HTML (one line per field)."""
    parser = _FormSig()
    parser.feed(html)
    return "\n".join(sorted(parser.sig))
