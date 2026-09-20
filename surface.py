"""
The website's HTML surface, and the agent that adds to it.

WHAT THIS IS FOR
----------------
The spec for this project asks for something display.py deliberately did not
do: Claude generating modular HTML, reading what the page currently shows,
matching the existing style, and being very hesitant to remove anything.

display.py's reasons for returning card DATA instead of HTML are all real,
and they're written out at the top of that file:
  - generating markup per request is seconds of latency, every request
  - the markup differs slightly each time, so the CSS breaks at random
  - the prompt box is a path from a user's text to rendered markup

So this file does both, by splitting the job in two:

  THE DATA PATH (premade format, the default)
      Claude chooses WHICH chunks the page should show and what to call
      them. The chunk's contents are rendered from the *validated* card data
      by a Jinja template in templates/cards/. The model never writes the
      markup for these, so they are fast, identical every time, and styled
      to match app.html by construction.

  THE CUSTOM PATH (the escape hatch the spec asks for)
      When no premade template fits, Claude may write an HTML chunk itself.
      That markup goes through sanitize() -- a tag/attribute allowlist --
      before it is stored, let alone rendered. So "Claude can generate new
      HTML chunks if needed" is true, and it is still not a way to get a
      <script> onto the page we're projecting in front of judges.

MODULARITY IS ENFORCED, NOT REQUESTED
-------------------------------------
Each chunk is a separate stored row with a stable id. Updating one chunk
can't disturb its neighbours, and "hesitant to remove" is a rule the server
applies (see apply_ops) rather than a sentence in a prompt that a model may
or may not honour: removals must be listed explicitly, are capped per turn,
and can never touch a chunk the student made.

POLLING
-------
GET /api/surface returns the current chunks plus STYLE_VOCAB. That is what
"Claude polls the website for the currently displayed HTML" means here: the
same endpoint the browser renders from is the one the agent reads before
deciding what to change.
"""

from __future__ import annotations

import html as html_module
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

CARD_DIR = Path(__file__).parent / "templates" / "cards"

# Hard ceilings. A model that loops, or a prompt that asks for "a card for
# every assignment", should hit a bound here rather than growing the page
# until the browser struggles.
MAX_CHUNKS = 12
MAX_REMOVALS_PER_TURN = 1
MAX_CUSTOM_HTML_CHARS = 6000
MAX_CUSTOM_PER_TURN = 2

# ---------------------------------------------------------------------------
# The style vocabulary we hand to the model so it can match the page
#
# These are real class names from app.html. Giving the model the actual
# vocabulary is what makes "mimic the existing style" achievable: it isn't
# guessing at a design, it is reusing the one that is already there.
# ---------------------------------------------------------------------------

STYLE_VOCAB = {
    "css_variables": [
        "--paper", "--panel", "--ink", "--ink-soft", "--muted", "--line",
        "--accent", "--red", "--amber", "--green", "--radius",
    ],
    "containers": ["panel", "panel-head", "panel-title", "count", "source", "list"],
    "rows": ["item", "rail", "item-body", "item-title", "item-meta",
             "item-when", "chip-row", "chip", "title-row"],
    "schedule": ["week", "day", "day-name", "day-date", "pills", "pill",
                 "pill-title", "pill-meta", "day-empty"],
    "misc": ["section-title", "empty", "detail", "detail-row", "detail-k",
             "detail-v", "files-empty", "src-badge", "src-ai"],
    "conventions": [
        "A chunk's outer element is <section class=\"panel\"> with a "
        "<div class=\"panel-head\"> holding a <div class=\"panel-title\">.",
        "Rows inside a panel go in <div class=\"list\"> as "
        "<div class=\"item\"> elements.",
        "Colour comes from the CSS variables above, never from hex codes.",
        "Font sizes and spacing come from the classes; do not restate them.",
    ],
}

# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

# Structural and text tags only. Nothing that loads, runs, or submits.
ALLOWED_TAGS = {
    "section", "div", "span", "p", "h2", "h3", "h4", "h5", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "strong", "em", "b", "i",
    "small", "br", "hr", "dl", "dt", "dd", "figure", "figcaption", "time",
    "code", "abbr", "a",
}

# Dropped along with everything inside them. <style> is here because a page-wide
# stylesheet from a model is a way to visually destroy the rest of the site,
# which the spec's "be hesitant to remove" is trying to prevent.
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template",
                     "noscript", "svg", "math", "form", "input", "button",
                     "select", "textarea", "link", "meta", "base", "title"}

VOID_TAGS = {"br", "hr"}

ALLOWED_ATTRS = {
    "class", "title", "datetime", "colspan", "rowspan", "scope", "role",
    "aria-label", "aria-hidden", "style", "href",
}

# A deliberately narrow style allowlist. The premade chart template needs
# `width: N%` for its bars and `background: var(--accent)` for their colour,
# so styles can't be banned outright -- but this is the whole list.
ALLOWED_STYLE_PROPS = {
    "width", "height", "max-width", "min-width", "flex", "text-align",
    "background", "background-color", "color", "border-left-color",
    "border-color", "opacity", "font-weight",
}
_STYLE_VALUE_OK = re.compile(r"^[A-Za-z0-9 ,.%#()_-]+$")
_CLASS_OK = re.compile(r"^[A-Za-z0-9 _-]+$")


class _Sanitizer(HTMLParser):
    """
    Allowlist HTML cleaner.

    Written against html.parser rather than a dependency on purpose: it's one
    file, it has no install step on Vercel, and the rules are visible right
    here where they can be read and tested. test_integration.py attacks it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropped: list[str] = []
        self._suppress_depth = 0
        self._open: list[str] = []

    # -- helpers ----------------------------------------------------------

    def _clean_attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts: list[str] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = raw_value or ""

            # Event handlers, in one rule, before anything else looks at them.
            if name.startswith("on"):
                self.dropped.append(f"attr:{name}")
                continue
            if name not in ALLOWED_ATTRS:
                self.dropped.append(f"attr:{name}")
                continue

            if name == "class":
                if not _CLASS_OK.match(value):
                    self.dropped.append("attr:class(bad-chars)")
                    continue
                value = " ".join(value.split())[:200]
            elif name == "style":
                value = self._clean_style(value)
                if not value:
                    continue
            elif name == "href":
                if tag != "a" or not re.match(r"^https?://", value.strip(), re.I):
                    self.dropped.append("attr:href")
                    continue
                value = value.strip()[:500]
            else:
                if re.search(r"javascript:|data:|vbscript:", value, re.I):
                    self.dropped.append(f"attr:{name}(scheme)")
                    continue
                value = value[:300]

            parts.append(f'{name}="{html_module.escape(value, quote=True)}"')

        if tag == "a":
            # Any link the agent produces opens in a new tab and can't reach
            # back into this page through window.opener.
            parts.append('rel="noopener noreferrer"')
            parts.append('target="_blank"')
        return (" " + " ".join(parts)) if parts else ""

    def _clean_style(self, value: str) -> str:
        keep: list[str] = []
        for declaration in value.split(";"):
            if ":" not in declaration:
                continue
            prop, _, val = declaration.partition(":")
            prop, val = prop.strip().lower(), val.strip()
            if prop not in ALLOWED_STYLE_PROPS:
                self.dropped.append(f"style:{prop}")
                continue
            lowered = val.lower()
            if "url(" in lowered or "expression" in lowered or "/*" in lowered:
                self.dropped.append(f"style:{prop}(value)")
                continue
            if not _STYLE_VALUE_OK.match(val):
                self.dropped.append(f"style:{prop}(chars)")
                continue
            keep.append(f"{prop}:{val}")
        return ";".join(keep)[:300]

    # -- parser callbacks -------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if self._suppress_depth:
            if tag in DROP_WITH_CONTENT:
                self._suppress_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self._suppress_depth = 1
            self.dropped.append(f"tag:{tag}")
            return
        if tag not in ALLOWED_TAGS:
            # Unwrap: the tag goes, its text stays. Losing a stray <marquee>
            # shouldn't also lose the sentence inside it.
            self.dropped.append(f"tag:{tag}")
            return
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}>")
            return
        self.out.append(f"<{tag}{self._clean_attrs(tag, attrs)}>")
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if self._suppress_depth or tag in DROP_WITH_CONTENT:
            self.dropped.append(f"tag:{tag}")
            return
        if tag in ALLOWED_TAGS:
            self.out.append(f"<{tag}>" if tag in VOID_TAGS
                            else f"<{tag}{self._clean_attrs(tag, attrs)}></{tag}>")
        else:
            self.dropped.append(f"tag:{tag}")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._suppress_depth:
            if tag in DROP_WITH_CONTENT:
                self._suppress_depth -= 1
            return
        if tag in VOID_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag in self._open:
            # Close anything left hanging inside it, so unbalanced input can't
            # leave the rest of the page nested inside the agent's chunk.
            while self._open:
                current = self._open.pop()
                self.out.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._suppress_depth:
            return
        self.out.append(html_module.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return  # comments are never useful here and can hide conditional markup

    def result(self) -> str:
        while self._open:
            self.out.append(f"</{self._open.pop()}>")
        return "".join(self.out)


def sanitize(raw_html: str) -> tuple[str, list[str]]:
    """
    Clean untrusted markup. Returns (html, what_was_dropped).

    The dropped list is surfaced in the agent's change report, so when a chunk
    looks wrong on screen there's a specific reason to read rather than a
    mystery.
    """
    parser = _Sanitizer()
    parser.feed(str(raw_html or "")[:MAX_CUSTOM_HTML_CHARS])
    parser.close()
    # dict.fromkeys to de-duplicate while keeping the order things were hit in.
    return parser.result().strip(), list(dict.fromkeys(parser.dropped))[:20]


# ---------------------------------------------------------------------------
# Premade templates
# ---------------------------------------------------------------------------

_env = None


def env():
    """Jinja environment with autoescaping on -- card values can't be markup."""
    global _env
    if _env is None:
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        _env = Environment(
            loader=FileSystemLoader(str(CARD_DIR)),
            autoescape=select_autoescape(default_for_string=True, default=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env


def premade_types() -> list[str]:
    if not CARD_DIR.exists():
        return []
    return sorted(path.stem for path in CARD_DIR.glob("*.html"))


def render_card(card: dict, title: str | None = None) -> str:
    """
    Render one validated card through its premade template.

    `title` lets the chunk's heading come from the layout plan ("Due before
    Friday") rather than from the card's generic title ("Assignments"), which
    is most of the value the layout agent adds on the premade path. Everything
    else in the chunk comes from validated card data, so the model is choosing
    a headline, not writing content.

    Falls back to the text template for a card type with no template yet, so
    adding a card type to display.py can't blank the page before someone
    writes its HTML.
    """
    kind = str(card.get("type") or "text")
    template_name = f"{kind}.html"
    if not (CARD_DIR / template_name).exists():
        template_name = "text.html"
        card = {"type": "text", "title": card.get("title") or kind,
                "body": json.dumps({k: v for k, v in card.items()
                                    if k not in ("type",)}, default=str)[:600]}

    fields = dict(card)
    if title:
        fields["title"] = title
    html = env().get_template(template_name).render(card=card, **fields)
    cleaned, _ = sanitize(html)
    return cleaned


# ---------------------------------------------------------------------------
# The surface agent
# ---------------------------------------------------------------------------

PLAN_CONTRACT = """You maintain the HTML surface of a study dashboard.

You are given: what the student asked, the orchestrator's answer, the
validated CARDS available to display, and the HTML the page is ALREADY
showing. Decide what the page should show now.

Return ONLY a JSON object, no prose and no markdown fences:

{
  "upsert":  [{"id": "<stable-id>", "card_index": <int>, "title": "<short>"}],
  "custom":  [{"id": "<stable-id>", "title": "<short>", "html": "<markup>"}],
  "remove":  ["<id>"],
  "note":    "<one sentence on what you changed and why>"
}

HOW TO DECIDE

- "upsert" is the normal path. card_index points into the CARDS list you were
  given; the server renders that card with a house template, so you do not
  write its markup and cannot change its data. Reuse the SAME id as an
  existing chunk to refresh it in place; use a new id to add a panel.
- "custom" is only for something no card type covers. Write markup in the
  page's existing vocabulary, which you are given as STYLE_VOCAB: reuse those
  class names, take colour from the CSS variables, and do not invent a new
  look. At most 2 custom chunks per turn.
- "remove" is a LAST RESORT. Prefer refreshing a chunk over deleting it, and
  prefer leaving a stale chunk alone over removing something the student may
  still want. At most one removal per turn, and only when a chunk is
  genuinely wrong or duplicated. An empty list is the right answer almost
  every time.
- Never put facts in a custom chunk that aren't in the cards or the answer.
  No invented assignments, due dates, grades, or events.
- Plain structural HTML only: no script, style, iframe, form, or event
  handlers. They are stripped before rendering, so including them just costs
  you the chunk.
- Ids are lowercase, hyphenated, and stable across turns: 'assignment-list',
  'workload-trend', 'exam-countdown'."""


def _chunk_digest(chunks: list[dict], html_chars: int = 400) -> list[dict]:
    """
    What the agent sees when it polls the page.

    The HTML is truncated: the point is for the model to recognise what is
    already there and match its shape, not to re-read every row it wrote last
    turn at full token cost.
    """
    return [
        {
            "id": chunk.get("id"),
            "kind": chunk.get("kind"),
            "title": chunk.get("title"),
            "source": chunk.get("source", "agent"),
            "html_preview": str(chunk.get("html") or "")[:html_chars],
            "updated_at": str(chunk.get("updated_at") or ""),
        }
        for chunk in chunks
    ]


def plan_ops(prompt: str, summary: str, cards: list[dict],
             current: list[dict]) -> dict:
    """
    Ask Claude what to change. Never raises.

    On any failure -- no API key, rate limit, malformed JSON -- this returns
    the deterministic plan instead. Same argument display.py makes: the layout
    agent is allowed to make the page smarter, and is not allowed to be the
    reason a correct answer never reaches the screen.
    """
    fallback = fallback_ops(cards, current)

    if not cards and not summary:
        return fallback

    payload = {
        "user_asked": prompt,
        "answer": summary,
        "cards": [{"index": i, "type": card.get("type"),
                   "title": card.get("title"),
                   "row_count": len(card.get("items") or card.get("blocks")
                                    or card.get("sections")
                                    or card.get("series") or [])}
                  for i, card in enumerate(cards)],
        "page_currently_shows": _chunk_digest(current),
        "style_vocab": STYLE_VOCAB,
        "premade_card_types": premade_types(),
    }

    try:
        import cache

        # The label carries the primary card type. In live mode it's just a
        # cache key; in mock mode it's what lets the fixture return a plan
        # that matches the prompt (a schedule panel for a scheduling
        # question, a chart for a trend question) instead of the same panel
        # every time. A mock that always produces one answer hides exactly
        # the behaviour you're trying to check.
        primary = str(cards[0].get("type")) if cards else "text"
        spec = cache.claude(
            system="You decide how a study dashboard displays a result.\n\n"
                   + PLAN_CONTRACT,
            user=json.dumps(payload, default=str),
            max_tokens=2500,
            label=f"surface:{primary}",
        )
    except Exception as exc:  # noqa: BLE001
        fallback["note"] = f"layout agent unavailable ({type(exc).__name__}); used house layout"
        return fallback

    if not isinstance(spec, dict) or "error" in spec:
        reason = str(spec.get("error"))[:120] if isinstance(spec, dict) else "bad response"
        fallback["note"] = f"layout agent returned no plan ({reason}); used house layout"
        return fallback

    ops = _validate_ops(spec, cards)
    if not ops["upsert"] and not ops["custom"]:
        # A plan that changes nothing, when there is fresh data to show, is
        # indistinguishable from a broken plan. Use the reliable one.
        if cards:
            fallback["note"] = "layout agent proposed no chunks; used house layout"
            return fallback
    return ops


def _slug(value: object, prefix: str = "chunk") -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")
    return (slug or prefix)[:48]


def _validate_ops(spec: dict, cards: list[dict]) -> dict:
    """Throw away anything that isn't in the contract, before it touches state."""
    upsert, custom, remove = [], [], []

    for item in (spec.get("upsert") or [])[:MAX_CHUNKS]:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("card_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(cards):
            continue
        upsert.append({
            "id": _slug(item.get("id") or cards[index].get("type"), "card"),
            "card_index": index,
            "title": str(item.get("title") or cards[index].get("title") or "")[:80],
        })

    for item in (spec.get("custom") or [])[:MAX_CUSTOM_PER_TURN]:
        if not isinstance(item, dict) or not item.get("html"):
            continue
        custom.append({
            "id": "custom-" + _slug(item.get("id"), "chunk"),
            "title": str(item.get("title") or "")[:80],
            "html": str(item.get("html"))[:MAX_CUSTOM_HTML_CHARS],
        })

    for item in (spec.get("remove") or []):
        if isinstance(item, str):
            remove.append(_slug(item))

    return {"upsert": upsert, "custom": custom, "remove": remove,
            "note": str(spec.get("note") or "")[:300]}


def fallback_ops(cards: list[dict], current: list[dict]) -> dict:
    """
    The plan with no model in it: show every validated card, keyed by type.

    Deterministic, free, instant, and identical every time. This is what runs
    with no ANTHROPIC_API_KEY at all, which means the website still updates
    itself on a laptop with no credits.
    """
    return {
        "upsert": [
            {"id": _slug(card.get("type"), "card"), "card_index": index,
             "title": str(card.get("title") or "")[:80]}
            for index, card in enumerate(cards)
        ],
        "custom": [],
        "remove": [],
        "note": "house layout: one panel per card",
    }


# ---------------------------------------------------------------------------
# Applying a plan
# ---------------------------------------------------------------------------


def apply_ops(current: list[dict], ops: dict, cards: list[dict]) -> tuple[list[dict], dict]:
    """
    Turn a plan into the new chunk list. Returns (chunks, report).

    THE REMOVAL RULES LIVE HERE, not in the prompt:
      - a chunk is only removed if its id is in ops["remove"]
      - at most MAX_REMOVALS_PER_TURN of them
      - never a chunk with source 'user'
      - never the last chunk on the page
    A model that ignores "be hesitant" still cannot clear the dashboard.

    The report is what the chat shows the student under "what changed", so
    every refusal above is recorded rather than silently applied.
    """
    chunks = [dict(chunk) for chunk in (current or [])]
    by_id = {chunk["id"]: chunk for chunk in chunks}
    now = datetime.now(timezone.utc).isoformat()

    added, updated, removed, refused, stripped = [], [], [], [], []

    def _place(chunk: dict) -> None:
        if chunk["id"] in by_id:
            existing = by_id[chunk["id"]]
            existing.update(chunk)
            updated.append(chunk["id"])
        else:
            by_id[chunk["id"]] = chunk
            chunks.append(chunk)
            added.append(chunk["id"])

    # --- premade chunks ---
    for item in ops.get("upsert") or []:
        card = cards[item["card_index"]]
        heading = item["title"] or str(card.get("title") or "")
        _place({
            "id": item["id"],
            "kind": str(card.get("type") or "text"),
            "title": heading,
            "html": render_card(card, title=heading),
            "source": "premade",
            "updated_at": now,
        })

    # --- custom chunks ---
    for item in ops.get("custom") or []:
        cleaned, dropped = sanitize(item["html"])
        if not cleaned:
            refused.append(f"{item['id']}: nothing left after sanitizing")
            continue
        if dropped:
            stripped.append({"id": item["id"], "dropped": dropped})
        _place({
            "id": item["id"],
            "kind": "custom",
            "title": item["title"],
            "html": cleaned,
            "source": "agent",
            "updated_at": now,
        })

    # --- removals, grudgingly ---
    allowance = MAX_REMOVALS_PER_TURN
    for chunk_id in ops.get("remove") or []:
        target = by_id.get(chunk_id)
        if target is None:
            continue
        if target.get("source") == "user":
            refused.append(f"{chunk_id}: student-created, kept")
            continue
        if len(chunks) <= 1:
            refused.append(f"{chunk_id}: last chunk on the page, kept")
            continue
        if allowance <= 0:
            refused.append(f"{chunk_id}: over the one-removal-per-turn limit, kept")
            continue
        chunks = [chunk for chunk in chunks if chunk["id"] != chunk_id]
        by_id.pop(chunk_id, None)
        removed.append(chunk_id)
        allowance -= 1

    # --- bound the page ---
    if len(chunks) > MAX_CHUNKS:
        # Evict the least recently touched agent chunk, and say so. A hard cap
        # has to give somewhere; doing it oldest-first and reporting it beats
        # either an unbounded page or a silent refusal to add anything new.
        evictable = sorted(
            (chunk for chunk in chunks
             if chunk.get("source") != "user" and chunk["id"] not in added),
            key=lambda chunk: str(chunk.get("updated_at") or ""),
        )
        while len(chunks) > MAX_CHUNKS and evictable:
            victim = evictable.pop(0)
            chunks = [chunk for chunk in chunks if chunk["id"] != victim["id"]]
            removed.append(victim["id"])
            refused.append(f"{victim['id']}: evicted, page was over {MAX_CHUNKS} chunks")

    # dict.fromkeys de-duplicates while keeping order. Two upserts to the
    # same id in one turn are legitimate (the layout agent refreshing a panel
    # it also just created), but reporting it as
    # "Refreshed panel: assignment-list, assignment-list" reads like a bug.
    report = {
        "added": list(dict.fromkeys(added)),
        "updated": list(dict.fromkeys(u for u in updated if u not in added)),
        "removed": list(dict.fromkeys(removed)),
        "refused": refused,
        "sanitized": stripped,
        "note": str(ops.get("note") or "")[:300],
        "chunk_count": len(chunks),
    }
    return chunks, report


def update(current: list[dict], prompt: str, summary: str,
           cards: list[dict]) -> tuple[list[dict], dict]:
    """One call: poll -> plan -> validate -> apply. What app.py uses."""
    ops = plan_ops(prompt, summary, cards, current)
    return apply_ops(current, ops, cards)


def seed_chunks() -> list[dict]:
    """
    What a brand-new account's surface starts as: nothing.

    The agent fills it on the first prompt. An empty surface renders as the
    site's own empty state, so a fresh account doesn't look broken.
    """
    return []


if __name__ == "__main__":
    # Quick manual check: python3 surface.py
    demo_cards = [{
        "type": "assignment_list", "title": "Due soon",
        "items": [{"title": "Problem Set 4", "course": "PHYS 1361",
                   "due": "Tue 11:59pm", "priority": 1, "est_minutes": 180}],
    }]
    os.environ.setdefault("MODE", "mock")
    chunks, report = apply_ops([], fallback_ops(demo_cards, []), demo_cards)
    print(json.dumps(report, indent=2))
    for chunk in chunks:
        print("\n" + chunk["html"])
