"""Static DustBuilder guidance plus the maintainer-only live-form drift checker."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from .context import Context
from .models import SUPPORTED_MODELS, ModelSpec, load_model_spec


@dataclass(frozen=True, slots=True)
class FormGuide:
    """The model-specific choices printed during rooting, captured from the current live page."""

    firmware_label: str
    prepackage_valetudo: bool = False


# Checked against every live fastboot form on 2026-07-27. Runtime deliberately uses this static
# table: an upstream outage or edit must never make a locally installed rooting tool stop working.
FORM_GUIDES: dict[str, FormGuide] = {
    "r2416": FormGuide("r2416 (ver 1782, 04/2026) latest"),
    "r2465": FormGuide("r2465 (ver 1782, 04/2026) latest"),
    "r9316": FormGuide("r9316 (ver 1726, 05/2024)"),
    "r2492": FormGuide("r2492 (ver 1782, 04/2026) latest"),
    "r2394": FormGuide("r2394 (ver 1639, 06/2024)"),
    "r2228": FormGuide("r2228 (ver 3407, 10/2024) latest"),
    "r2338": FormGuide("r2338 (ver 1633, 05/2025) latest"),
    "r2338h": FormGuide("r2338h (ver 1633, 05/2025) latest"),
    "r2250": FormGuide("r2250 (ver 1413, 08/2024) latest", prepackage_valetudo=True),
    "r2240": FormGuide("r2240 (ver 1315, 08/2024) latest", prepackage_valetudo=True),
    "r2104": FormGuide("r2104 (ver 1130, 12/2022) latest", prepackage_valetudo=True),
    "r2385": FormGuide("r2385 (ver 1118, 05/2024) latest"),
    "r2491": FormGuide("r2491 (ver 1782, 04/2026) latest"),
}

CONFIG_CHECKER_URL = "https://check.builder.dontvacuum.me"
GUIDE_INDEX_URL = "https://builder.dontvacuum.me/nextgen/"

# The checker currently has no separate R2338H choice. Keeping that absence explicit prevents the
# guide from inventing a selection that the page does not offer.
CHECKER_MODEL_CHOICES: dict[str, str] = {
    "r2416": "X40 Ultra",
    "r2465": "X40 Master",
    "r9316": "X30 Ultra",
    "r2492": "L40 Ultra",
    "r2394": "L20 (MR813) Ultra",
    "r2228": "L10S Ultra",
    "r2338": "L10S Pro Ultra Heat",
    "r2250": "D10S Pro",
    "r2240": "D10S Plus",
    "r2104": "W10 Pro",
    "r2385": "MOVA S20 Ultra",
    "r2491": "MOVA P10 Pro Ultra",
}


def form_guide(dust_code: str) -> FormGuide:
    try:
        return FORM_GUIDES[dust_code]
    except KeyError:
        raise ValueError(f"No DustBuilder form guide for {dust_code}") from None


def checker_model_choice(dust_code: str) -> str | None:
    return CHECKER_MODEL_CHOICES.get(dust_code)


def forms_verified_on(libexec: Path) -> str:
    """The last successful all-model live check, shipped with this installed build."""
    stamp = libexec / "dustbuilder-forms" / "verified-on.txt"
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
    value: str | None
    checked: bool
    selected: bool
    placeholder: str | None
    required: bool
    disabled: bool
    readonly: bool
    maxlength: str | None
    accept: str | None
    label_parts: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.label_parts)).strip()


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: list[_Control] = []
        self.hcaptcha = False
        self.visible_text: list[str] = []
        self.links: list[tuple[str, list[str]]] = []
        self._label_for: int | None = None
        self._link_for: int | None = None
        self._hidden_text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in ("script", "style"):
            self._hidden_text_depth += 1
        if tag in ("br", "div", "script", "style"):
            self._label_for = None
        if tag == "div" and "h-captcha" in (values.get("class") or "").split():
            self.hcaptcha = True
        if tag == "a":
            self.links.append((values.get("href") or "", []))
            self._link_for = len(self.links) - 1
        if tag not in ("input", "button", "select", "textarea", "option"):
            return
        self._label_for = None
        control = _Control(
            tag=tag,
            type=(
                values.get("type")
                or ("text" if tag == "input" else "submit" if tag == "button" else tag)
            ).lower(),
            name=values.get("name"),
            value=values.get("value"),
            checked="checked" in values,
            selected="selected" in values,
            placeholder=values.get("placeholder"),
            required="required" in values,
            disabled="disabled" in values,
            readonly="readonly" in values,
            maxlength=values.get("maxlength"),
            accept=values.get("accept"),
        )
        self.controls.append(control)
        if control.type != "hidden":
            self._label_for = len(self.controls) - 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("button", "select", "textarea", "option"):
            self._label_for = None
        if tag == "a":
            self._link_for = None
        if tag in ("script", "style") and self._hidden_text_depth:
            self._hidden_text_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_text_depth:
            text = re.sub(r"\s+", " ", data).strip()
            if text:
                self.visible_text.append(text)
        if self._label_for is not None and data.strip():
            self.controls[self._label_for].label_parts.append(data)
        if self._link_for is not None and data.strip():
            self.links[self._link_for][1].append(data)


def _parsed_form(html: str) -> _FormParser:
    parser = _FormParser()
    parser.feed(html)
    return parser


def _control_snapshot(parser: _FormParser) -> list[str]:
    lines: list[str] = []
    for control in parser.controls:
        parts = [control.tag, f"type={control.type}"]
        if control.name is not None:
            parts.append(f"name={control.name}")
        value = control.value
        if control.name == "auth" and value is not None:
            value = "<dynamic>"
        elif control.name == "image" and value is not None:
            value = value.split("_", 1)[0]
        if value is not None:
            parts.append(f"value={value}")
        if control.placeholder is not None:
            parts.append(f"placeholder={control.placeholder}")
        if control.maxlength is not None:
            parts.append(f"maxlength={control.maxlength}")
        if control.accept is not None:
            parts.append(f"accept={control.accept}")
        if control.required:
            parts.append("required=yes")
        if control.disabled:
            parts.append("disabled=yes")
        if control.readonly:
            parts.append("readonly=yes")
        parts.append(f"checked={'yes' if control.checked else 'no'}")
        if control.tag == "option":
            parts.append(f"selected={'yes' if control.selected else 'no'}")
        if control.label:
            parts.append(f"label={control.label}")
        lines.append("\t".join(parts))
    if parser.hcaptcha:
        lines.append("hcaptcha")
    return lines


def form_snapshot(html: str) -> str:
    """Normalize a live page to the controls and labels a user acts on.

    Per-request authorization values and opaque image hashes are excluded. Layout and CSS are not
    represented, so only a meaningful form change turns the scheduled drift check red.
    """
    lines = _control_snapshot(_parsed_form(html))
    return "\n".join(lines) + ("\n" if lines else "")


def checker_snapshot(html: str) -> str:
    """The checker has instructions outside its form; preserve those as well as its controls."""
    parser = _parsed_form(html)
    lines = _control_snapshot(parser)
    for href, label_parts in parser.links:
        label = re.sub(r"\s+", " ", " ".join(label_parts)).strip()
        lines.append(f"link={href}\tlabel={label}")
    lines.extend(f"text={text}" for text in parser.visible_text)
    return "\n".join(lines) + ("\n" if lines else "")


def guide_index_snapshot(html: str) -> str:
    """Pin where the checker sends Dreame users, without byte-pinning the linked PDF itself."""
    parser = _parsed_form(html)
    relevant = {"dreame_gen3.pdf", "fastboot-cheatcheat.txt"}
    lines = []
    for href, label_parts in parser.links:
        if href not in relevant:
            continue
        label = re.sub(r"\s+", " ", " ".join(label_parts)).strip()
        lines.append(f"link={href}\tlabel={label}")
    return "\n".join(lines) + ("\n" if lines else "")


def _fetch(ctx: Context, label: str, url: str) -> str | None:
    for attempt in range(3):
        result = ctx.runner.run(["curl", "-fsSL", "-m", "20", url], check=False)
        if result.ok and result.stdout.strip():
            return result.stdout
        if attempt < 2:
            ctx.sleep(1)
    ctx.console.err(f"{label}: couldn't fetch {url} after 3 tries")
    return None


def _matches_golden(
    ctx: Context, *, label: str, url: str, golden_name: str, actual: str,
) -> bool:
    golden = ctx.libexec / "dustbuilder-forms" / golden_name
    if not golden.is_file():
        ctx.console.err(f"{label}: missing form golden {golden}")
        return False
    expected = golden.read_text()
    if actual == expected:
        return True

    ctx.console.err(f"{label}: DustBuilder form changed: {url}")
    for line in difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=f"golden/{golden_name}", tofile=f"live/{golden_name}", lineterm="",
    ):
        ctx.console.info(line)
    return False


def _verify_profile_form(ctx: Context, model_spec: ModelSpec, *, url: str | None = None) -> bool:
    page = url or model_spec.dustbuilder_page
    html = _fetch(ctx, model_spec.model, page)
    if html is None:
        return False
    if _matches_golden(
        ctx, label=model_spec.model, url=page,
        golden_name=f"{model_spec.dust_code}.txt", actual=form_snapshot(html),
    ):
        guide = form_guide(model_spec.dust_code)
        extra = ", Prepackage Valetudo present" if guide.prepackage_valetudo else ""
        ctx.console.info(f"{model_spec.model}: matches ({guide.firmware_label}{extra})")
        return True
    return False


def _verify_checker_form(ctx: Context) -> bool:
    html = _fetch(ctx, "config-support checker", CONFIG_CHECKER_URL)
    if html is None:
        return False
    if not _matches_golden(
        ctx, label="config-support checker", url=CONFIG_CHECKER_URL,
        golden_name="config-checker.txt", actual=checker_snapshot(html),
    ):
        return False
    ctx.console.info("Config-support checker: matches")
    return True


def _verify_guide_index(ctx: Context) -> bool:
    html = _fetch(ctx, "Dreame guide index", GUIDE_INDEX_URL)
    if html is None:
        return False
    if not _matches_golden(
        ctx, label="Dreame guide index", url=GUIDE_INDEX_URL,
        golden_name="nextgen-index.txt", actual=guide_index_snapshot(html),
    ):
        return False
    ctx.console.info("Dreame guide index: links match")
    return True


def verify_form(ctx: Context) -> bool:
    """Check the selected model's live page; never called by the rooting flow."""
    return _verify_profile_form(ctx, ctx.model_spec, url=ctx.dustbuilder_page)


def verify_all_forms(ctx: Context) -> bool:
    """Check every live builder/support form without selecting or touching a robot."""
    ok = True
    checked = 0
    for key in SUPPORTED_MODELS:
        model_spec = load_model_spec(key)
        if model_spec.method != "fastboot":
            continue
        checked += 1
        if not _verify_profile_form(ctx, model_spec):
            ok = False
    checked += 1
    if not _verify_checker_form(ctx):
        ok = False
    if not _verify_guide_index(ctx):
        ok = False
    if ok:
        ctx.console.say(f"All {checked} DustBuilder forms and the linked-guide index match their "
                        "committed goldens.")
    return ok
