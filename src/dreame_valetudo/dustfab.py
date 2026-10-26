"""Static Dustfab guidance plus the maintainer-only live-page drift checker."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .context import Context
from .models import DUSTFAB_HOST, SUPPORTED_MODELS, ModelSpec, load_model_spec


@dataclass(frozen=True, slots=True)
class FormGuide:
    """The model-specific choice printed during rooting, captured from the current live page."""

    firmware_label: str


# Checked against every live fastboot page on 2026-10-26. Runtime deliberately uses this static
# table: an upstream outage or edit must never make a locally installed rooting tool stop working.
FORM_GUIDES: dict[str, FormGuide] = {
    "r2416": FormGuide("r2416 (ver 1782, 04/2026) (latest)"),
    "r2465": FormGuide("r2465 (ver 1782, 04/2026) (latest)"),
    "r9316": FormGuide("r9316 (ver 1726, 05/2024) (latest)"),
    "r2492": FormGuide("r2492 (ver 1782, 04/2026) (latest)"),
    "r2394": FormGuide("r2394 (ver 1639, 06/2024) (latest)"),
    "r2228": FormGuide("r2228 (ver 3407, 10/2024) (latest)"),
    "r2338": FormGuide("r2338 (ver 1633, 05/2025) (latest)"),
    "r2338h": FormGuide("r2338h (ver 1633, 05/2025) (latest)"),
    "r2250": FormGuide("r2250 (ver 1413, 08/2024) (latest)"),
    "r2240": FormGuide("r2240 (ver 1315, 08/2024) (latest)"),
    "r2104": FormGuide("r2104 (ver 1130, 12/2022) (latest)"),
    "r2385": FormGuide("r2385 (ver 1118, 05/2024) (latest)"),
    "r2491": FormGuide("r2491 (ver 1782, 04/2026) (latest)"),
}

# Xiaomi-branded robots are listed on their own brand page while their build pages live under
# /build/dreame, so a model that moves between listings is only caught by checking all three.
BRAND_LISTINGS = ("dreame", "mova", "xiaomi")


def form_guide(dust_code: str) -> FormGuide:
    try:
        return FORM_GUIDES[dust_code]
    except KeyError:
        raise ValueError(f"No Dustfab form guide for {dust_code}") from None


def forms_verified_on(libexec: Path) -> str:
    """The last successful all-model live check, shipped with this installed build."""
    stamp = libexec / "dustfab-forms" / "verified-on.txt"
    try:
        value = stamp.read_text().strip()
    except OSError:
        return "unknown"
    return value or "unknown"


@dataclass(slots=True)
class _Control:
    tag: str
    type: str
    name: str | None
    id: str | None
    value: str | None
    checked: bool
    placeholder: str | None
    required: bool
    disabled: bool
    maxlength: str | None
    pattern: str | None
    accept: str | None
    text_parts: list[str] = field(default_factory=list)


def _squash(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


class _PageParser(HTMLParser):
    """Controls with their <label for=…> text, plus visible text and links, inside one region.

    The region is the first element whose id (or, failing that, tag) is `region`, so navigation,
    styles and scripts around it never reach a snapshot.
    """

    def __init__(self, region: str) -> None:
        super().__init__(convert_charrefs=True)
        self._region = region
        self._depth = 0  # element depth inside the region; 0 means outside it
        self._hidden = 0
        self._label_stack: list[str | None] = []
        self._button: _Control | None = None
        self._link: list[str] | None = None
        self.controls: list[_Control] = []
        self.labels: dict[str, list[str]] = {}
        self.visible_text: list[str] = []
        self._block: list[str] = []
        self.links: list[tuple[str, list[str]]] = []

    _VOID = frozenset({"input", "br", "img", "hr", "meta", "link", "source", "path"})
    # Text is grouped per block, so an inline <i> or <b> does not split one sentence into several
    # snapshot lines.
    _BLOCK = frozenset({
        "p", "li", "div", "h1", "h2", "h3", "h4", "label", "button", "br", "form", "ol", "ul",
    })

    def _flush(self) -> None:
        text = _squash(self._block)
        if text:
            self.visible_text.append(text)
        self._block = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if not self._depth:
            if self._region in (values.get("id"), tag):
                self._depth = 1
            return
        if tag in self._BLOCK:
            self._flush()
        if tag not in self._VOID:
            self._depth += 1
        if tag in ("script", "style", "svg"):
            self._hidden += 1
        if tag == "label":
            self._label_stack.append(values.get("for"))
        if tag == "a":
            self.links.append((values.get("href") or "", []))
            self._link = self.links[-1][1]
        if tag in ("input", "button", "textarea"):
            control = _Control(
                tag=tag,
                type=(values.get("type") or ("text" if tag == "input" else tag)).lower(),
                name=values.get("name"),
                id=values.get("id"),
                value=values.get("value"),
                checked="checked" in values,
                placeholder=values.get("placeholder"),
                required="required" in values,
                disabled="disabled" in values,
                maxlength=values.get("maxlength"),
                pattern=values.get("pattern"),
                accept=values.get("accept"),
            )
            self.controls.append(control)
            if tag == "button":
                self._button = control

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self._depth and tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        if tag in self._BLOCK:
            self._flush()
        if tag in ("script", "style", "svg") and self._hidden:
            self._hidden -= 1
        if tag == "label" and self._label_stack:
            self._label_stack.pop()
        if tag == "a":
            self._link = None
        if tag == "button":
            self._button = None
        if tag not in self._VOID:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._depth or self._hidden or not data.strip():
            return
        self._block.append(data)
        for target in self._label_stack:
            if target:
                self.labels.setdefault(target, []).append(data)
        if self._button is not None:
            self._button.text_parts.append(data)
        if self._link is not None:
            self._link.append(data)


def _parse(html: str, region: str) -> _PageParser:
    parser = _PageParser(region)
    parser.feed(html)
    parser.close()
    parser._flush()
    return parser


# The Prepackage Valetudo label names the Valetudo release Dustfab currently bundles. That moves
# with every upstream release and says nothing about the form a user fills in, so it is masked.
_VALETUDO_VERSION = re.compile(r"(Valetudo) \d{4}\.\d{2}\.\d+")


def _control_lines(parser: _PageParser) -> list[str]:
    lines: list[str] = []
    for control in parser.controls:
        if control.type == "file" and control.name is None:
            continue  # the "Load from file" helper only fills the authorized_keys textarea
        parts = [control.tag, f"type={control.type}"]
        for key, value in (
            ("name", control.name), ("value", control.value),
            ("placeholder", control.placeholder), ("maxlength", control.maxlength),
            ("pattern", control.pattern), ("accept", control.accept),
        ):
            if value is not None:
                parts.append(f"{key}={value}")
        if control.required:
            parts.append("required=yes")
        if control.disabled:
            parts.append("disabled=yes")
        if control.tag == "input" and control.type in ("radio", "checkbox"):
            parts.append(f"checked={'yes' if control.checked else 'no'}")
        label = _squash(
            control.text_parts if control.tag == "button"
            else parser.labels.get(control.id or "", [])
        )
        if label:
            parts.append("label=" + _VALETUDO_VERSION.sub(r"\1 <version>", label))
        lines.append("\t".join(parts))
    return lines


def form_snapshot(html: str) -> str:
    """Normalize a build page to the controls and labels a user acts on.

    Layout, CSS and the captcha's internals are not represented, so only a meaningful change to
    what the rooting checklist tells a user to choose turns the scheduled drift check red.
    """
    lines = _control_lines(_parse(html, "build-form"))
    return "\n".join(lines) + ("\n" if lines else "")


def checker_snapshot(html: str) -> str:
    """The check page's instructions matter as much as its controls; keep both."""
    parser = _parse(html, "check-form")
    lines = _control_lines(parser)
    lines.extend(f"text={text}" for text in parser.visible_text)
    return "\n".join(lines) + ("\n" if lines else "")


def listing_snapshot(html: str) -> str:
    """Which build pages a brand listing links to, so a model that moves brands is noticed."""
    parser = _parse(html, "main")
    pages = sorted({
        href for href, _label in parser.links if re.fullmatch(r"/build/[a-z]+/[a-z0-9]+", href)
    })
    return "".join(f"{href}\n" for href in pages)


def _fetch(ctx: Context, label: str, url: str) -> str | None:
    for attempt in range(3):
        result = ctx.runner.run(["curl", "-fsSL", "-m", "20", url], check=False)
        if result.ok and result.stdout.strip():
            return result.stdout
        if attempt < 2:
            ctx.sleep(2)
    ctx.console.err(f"{label}: couldn't fetch {url} after 3 tries")
    return None


def _matches_golden(
    ctx: Context, *, label: str, url: str, golden_name: str, actual: str,
) -> bool:
    golden = ctx.libexec / "dustfab-forms" / golden_name
    if not golden.is_file():
        ctx.console.err(f"{label}: missing golden {golden}")
        return False
    expected = golden.read_text()
    if actual == expected:
        return True

    ctx.console.err(f"{label}: Dustfab page changed: {url}")
    for line in difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=f"golden/{golden_name}", tofile=f"live/{golden_name}", lineterm="",
    ):
        ctx.console.info(line)
    return False


def _verify_page(
    ctx: Context, *, label: str, url: str, golden_name: str, snapshot: Callable[[str], str],
) -> bool:
    html = _fetch(ctx, label, url)
    if html is None:
        return False
    return _matches_golden(
        ctx, label=label, url=url, golden_name=golden_name, actual=snapshot(html),
    )


def _verify_model_form(ctx: Context, model_spec: ModelSpec, *, url: str | None = None) -> bool:
    page = url or model_spec.fab_page
    if not _verify_page(
        ctx, label=model_spec.model, url=page,
        golden_name=f"{model_spec.dust_code}.txt", snapshot=form_snapshot,
    ):
        return False
    ctx.console.info(
        f"{model_spec.model}: matches ({form_guide(model_spec.dust_code).firmware_label})"
    )
    return True


def _verify_check_page(ctx: Context, model_spec: ModelSpec) -> bool:
    if not _verify_page(
        ctx, label=f"{model_spec.model} check", url=model_spec.fab_check_page,
        golden_name=f"{model_spec.dust_code}-check.txt", snapshot=checker_snapshot,
    ):
        return False
    ctx.console.info(f"{model_spec.model} check page: matches")
    return True


def _verify_listing(ctx: Context, brand: str) -> bool:
    if not _verify_page(
        ctx, label=f"{brand} listing", url=f"https://{DUSTFAB_HOST}/build/{brand}",
        golden_name=f"listing-{brand}.txt", snapshot=listing_snapshot,
    ):
        return False
    ctx.console.info(f"{brand} listing: matches")
    return True


def verify_form(ctx: Context) -> bool:
    """Check the selected model's live page; never called by the rooting flow."""
    return _verify_model_form(ctx, ctx.model_spec, url=ctx.fab_page)


def verify_all_forms(ctx: Context) -> bool:
    """Check every live build page, check page and brand listing without touching a robot.

    Dustfab rate-limits by address, so pages are fetched one at a time with a pause between.
    """
    ok = True
    checked = 0
    for key in SUPPORTED_MODELS:
        model_spec = load_model_spec(key)
        if model_spec.method != "fastboot":
            continue
        for verify in (_verify_model_form, _verify_check_page):
            checked += 1
            if not verify(ctx, model_spec):
                ok = False
            ctx.sleep(1)
    for brand in BRAND_LISTINGS:
        checked += 1
        if not _verify_listing(ctx, brand):
            ok = False
        ctx.sleep(1)
    if ok:
        ctx.console.say(f"All {checked} Dustfab pages match their committed goldens.")
    return ok
