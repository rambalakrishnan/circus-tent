"""SPA stabilization + in-browser pruning (EXTRACT/HEAL profiles). See spec.

The pruning scripts run on the LIVE DOM (not a cloneNode): cloneNode cannot
see open shadow roots, so the traversal walks the real tree and explicitly
descends into .shadowRoot children. It BUILDS a detached string — the live
DOM is never mutated.
"""

from __future__ import annotations

import asyncio
import contextlib
import html.parser
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

PruneProfile = Literal["EXTRACT", "HEAL"]

_COMMON_JS = """
const TAG_STRIP = new Set(['script','style','noscript','iframe','link','meta']);
const IMG_TAGS = new Set(['img','svg']);
const SEM_ATTRS = ['aria-label','role','title','alt'];
const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
"""

#: EXTRACT-profile pruning script (≤250KB target). All tags retained with
#: semantic attributes; images/svgs become [ICON:x]; shadow DOM uncloaked.
PRUNE_JS_EXTRACT: str = (
    """
() => {
"""
    + _COMMON_JS
    + """
  const out = [];
  const visit = (node) => {
    if (node.nodeType === 3) {
      const t = clean(node.textContent);
      if (t) out.push(t);
      return;
    }
    if (node.nodeType !== 1) return;
    const tag = (node.tagName || '').toLowerCase();
    if (TAG_STRIP.has(tag)) return;
    if (IMG_TAGS.has(tag)) {
      const label = node.getAttribute('aria-label') || node.getAttribute('alt')
        || node.getAttribute('title') || node.getAttribute('role') || '';
      if (label) out.push('[ICON:' + clean(label) + ']');
      return;
    }
    if (tag === 'button' && node.getAttribute('role')) {
      out.push('[BUTTON:' + clean(node.getAttribute('role')) + ':' + clean(node.textContent) + ']');
      return;
    }
    const attrs = [];
    for (const a of SEM_ATTRS) {
      const v = node.getAttribute(a);
      if (v) attrs.push(a + '="' + clean(v) + '"');
    }
    out.push('<' + tag + (attrs.length ? ' ' + attrs.join(' ') : '') + '>');
    const kids = node.shadowRoot
      ? Array.from(node.shadowRoot.childNodes)
      : Array.from(node.childNodes);
    kids.forEach(visit);
    out.push('</' + tag + '>');
  };
  visit(document.documentElement);
  return out.join(' ');
}
"""
)

#: HEAL-profile pruning script (≤50KB target): aggressive — only interactive
#: and textual nodes retained; layout wrappers (div/span/section...) are
#: descended into but not emitted.
PRUNE_JS_HEAL: str = (
    """
() => {
"""
    + _COMMON_JS
    + """
  const INTERACTIVE = new Set([
    'button','input','select','textarea','a','form',
    'label','option','details','summary',
  ]);
  const out = [];
  const visit = (node) => {
    if (node.nodeType === 3) {
      const t = clean(node.textContent);
      if (t) out.push(t);
      return;
    }
    if (node.nodeType !== 1) return;
    const tag = (node.tagName || '').toLowerCase();
    if (TAG_STRIP.has(tag)) return;
    if (IMG_TAGS.has(tag)) {
      const label = node.getAttribute('aria-label') || node.getAttribute('alt')
        || node.getAttribute('title') || node.getAttribute('role') || '';
      if (label) out.push('[ICON:' + clean(label) + ']');
      return;
    }
    const emit = INTERACTIVE.has(tag) || !!node.getAttribute('role');
    if (emit) {
      const attrs = [];
      for (const a of SEM_ATTRS) {
        const v = node.getAttribute(a);
        if (v) attrs.push(a + '="' + clean(v) + '"');
      }
      if (tag === 'input') {
        const t = node.getAttribute('type') || '';
        if (t) attrs.push('type="' + clean(t) + '"');
      }
      const attrsStr = attrs.length ? ' ' + attrs.join(' ') : '';
      out.push('<' + tag + attrsStr + '>');
    }
    const kids = node.shadowRoot
      ? Array.from(node.shadowRoot.childNodes)
      : Array.from(node.childNodes);
    kids.forEach(visit);
    if (emit) out.push('</' + tag + '>');
  };
  visit(document.documentElement);
  return out.join(' ');
}
"""
)

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class StabilizationResult:
    ok: bool
    partial: bool
    waited_ms: int
    missing_signatures: tuple[str, ...]


async def wait_for_stability(
    page: Any,
    signatures: Sequence[str] | None = None,
    network_idle_ms: int = 500,
    dom_quiet_ms: int = 300,
    timeout_ms: int = 15000,
) -> StabilizationResult:
    """Conditions: network idle (no in-flight requests for network_idle_ms),
    DOM quiet (no mutations for dom_quiet_ms), signatures present in body text.
    Timeout → partial with which conditions failed."""
    started = time.monotonic()
    sigs = list(signatures or [])

    in_flight = 0
    last_request = time.monotonic()

    def on_request(*_args: Any) -> None:
        nonlocal in_flight, last_request
        in_flight += 1
        last_request = time.monotonic()

    def on_done(*_args: Any) -> None:
        nonlocal in_flight, last_request
        in_flight = max(0, in_flight - 1)
        last_request = time.monotonic()

    for event in ("request",):
        with contextlib.suppress(AttributeError, NotImplementedError):
            page.on(event, on_request)
    for event in ("requestfinished", "requestfailed"):
        with contextlib.suppress(AttributeError, NotImplementedError):
            page.on(event, on_done)

    installed_observer = False
    try:
        await page.evaluate(
            "() => { window.__ct_mutations = Date.now();"
            " if (!window.__ct_observer) { window.__ct_observer = new MutationObserver("
            " () => { window.__ct_mutations = Date.now(); });"
            " window.__ct_observer.observe(document.documentElement,"
            " {childList: true, subtree: true, attributes: true, characterData: true}); } }"
        )
        installed_observer = True
    except AttributeError, NotImplementedError:
        pass

    net_ok = False
    dom_ok = False
    sig_ok = not sigs
    missing = tuple(sigs)

    while time.monotonic() - started < timeout_ms / 1000:
        now = time.monotonic()
        if not net_ok and in_flight == 0 and now - last_request >= network_idle_ms / 1000:
            net_ok = True
        if installed_observer and not dom_ok:
            try:
                last_mut = float(await page.evaluate("window.__ct_mutations"))
                if now * 1000 - last_mut >= dom_quiet_ms:
                    dom_ok = True
            except AttributeError, NotImplementedError:
                dom_ok = True
        if not sig_ok:
            try:
                text = await page.evaluate("document.body ? document.body.innerText : ''")
                missing = tuple(s for s in sigs if s not in text)
                sig_ok = not missing
            except AttributeError, NotImplementedError:
                sig_ok = True
        if net_ok and dom_ok and sig_ok:
            return StabilizationResult(True, False, int((time.monotonic() - started) * 1000), ())
        await asyncio.sleep(0.05)

    return StabilizationResult(False, True, int((time.monotonic() - started) * 1000), missing)


async def prune_page(page: Any, profile: PruneProfile = "EXTRACT") -> str:
    """Prune the live page: main frame + every other frame flattened."""
    script = PRUNE_JS_EXTRACT if profile == "EXTRACT" else PRUNE_JS_HEAL
    parts = []
    try:
        main = await page.evaluate(script)
        parts.append("MAIN:\n" + main)
    except Exception:  # noqa: BLE001
        parts.append("MAIN:\n")
    try:
        frames = page.frames() if callable(getattr(page, "frames", None)) else []
    except Exception:  # noqa: BLE001
        frames = []
    for frame in frames:
        url = ""
        try:
            url = frame.url if callable(getattr(frame, "url", None)) else ""
        except Exception:  # noqa: BLE001
            url = "?"
        try:
            text = await frame.evaluate(script)
            parts.append(f"FRAME {url}:\n{text}")
        except Exception:  # noqa: BLE001
            parts.append(f"FRAME {url}:\n[FRAME:{url}]")
    return "\n".join(parts)


class _Pruner(html.parser.HTMLParser):
    """Deterministic stdlib pruner mirroring the JS scripts (no shadow DOM,
    no frames). EXTRACT keeps all tags with semantic attrs; HEAL keeps
    interactive/textual only."""

    #: Tags whose *content* must be dropped. Tracked as a single open tag (not a
    #: depth counter): HTML void elements (`<meta>`, `<link>`, `<br>`, ...) have
    #: no closing tag, so a counter never returns to zero and silently swallows
    #: the rest of the document — the bug that pruned a 5KB LinkedIn authwall to
    #: 13 bytes.
    _SKIP_CONTENT = {"script", "style", "noscript", "iframe", "template"}
    #: Void/structural tags with nothing to emit and nothing to skip.
    _DROP_VOID = {
        "link",
        "meta",
        "base",
        "col",
        "embed",
        "param",
        "source",
        "track",
        "wbr",
        "area",
        "hr",
        "br",
    }
    _SEM = ("aria-label", "role", "title", "alt")
    _INTERACTIVE = {
        "button",
        "input",
        "select",
        "textarea",
        "a",
        "form",
        "label",
        "option",
        "details",
        "summary",
    }

    def __init__(self, profile: PruneProfile) -> None:
        super().__init__(convert_charrefs=True)
        self.profile = profile
        self.parts: list[str] = []
        self._skip_tag: str | None = None
        self._last_data_tag: str | None = None
        self._marker_close: str | None = None  # set while inside a [BUTTON:...] marker
        self._marker_role: str = ""
        self._marker_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            return
        if tag in self._SKIP_CONTENT:
            self._skip_tag = tag
            return
        if tag in self._DROP_VOID:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "img":
            label = a.get("aria-label") or a.get("alt") or a.get("title") or a.get("role") or ""
            if label:
                self.parts.append(f"[ICON:{_WS_RE.sub(' ', label).strip()}]")
            return
        if tag == "svg":
            label = (
                a.get("aria-label") or a.get("aria-label") or a.get("title") or a.get("role") or ""
            )
            if label:
                self.parts.append(f"[ICON:{_WS_RE.sub(' ', label).strip()}]")
            self._skip_tag = "svg"  # drop <path>/<g> noise
            return
        if tag == "button" and a.get("role"):
            self._marker_role = _WS_RE.sub(" ", a["role"]).strip()
            self._marker_close = "button"
            self._marker_text = []
            return
        emit = self.profile == "EXTRACT" or tag in self._INTERACTIVE or bool(a.get("role"))
        if emit:
            kept = [f'{k}="{_WS_RE.sub(" ", a.get(k, "")).strip()}"' for k in self._SEM if a.get(k)]
            if tag == "input" and a.get("type"):
                kept.append(f'type="{a["type"]}"')
            self.parts.append(f"<{tag}" + ((" " + " ".join(kept)) if kept else "") + ">")
        self._last_data_tag = tag if emit else None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_tag = None
            return
        if self._marker_close == tag:
            text = _WS_RE.sub(" ", " ".join(self._marker_text)).strip()
            self.parts.append(f"[BUTTON:{self._marker_role}:{text}]")
            self._marker_close = None
            self._marker_role = ""
            self._marker_text = []
            return
        if tag == self._last_data_tag:
            self.parts.append(f"</{tag}>")
            self._last_data_tag = None

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        text = _WS_RE.sub(" ", data).strip()
        if not text:
            return
        if self._marker_close is not None:
            self._marker_text.append(text)
            return
        self.parts.append(text)

    def text(self) -> str:
        return " ".join(p for p in self.parts if p)


async def prune_html(html: str, profile: PruneProfile = "EXTRACT") -> str:
    parser = _Pruner(profile)
    parser.feed(html)
    parser.close()
    return parser.text()
