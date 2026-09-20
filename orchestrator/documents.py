"""
Turning things into files for the Files tab.

WHY THIS IS ITS OWN MODULE
--------------------------
Filing used to happen as a side effect: agent.py noticed a study-guide result
and quietly built a file from it. That worked, and it was still wrong, because
the side effect was invisible to the one participant who needed to know about
it. Asked "add the module to the files section", Nemotron answered:

    "I'm not able to add modules or files to a 'files' section -- my tools
     let me create study guides, schedule study time, manage assignments,
     and handle campus events, but there isn't a way to write or upload
     arbitrary files to a files area."

That was an honest and accurate description of its tool list. Filing wasn't a
tool; it was something that happened behind its back. A capability the model
can't name is a capability the student can't ask for.

So the rendering lives here, where both sides can use it: tools.py builds a
file and RETURNS it, so the tool result says a file was created and the model
can report it truthfully; agent.py forwards whatever files a result contains
to the store. Same code path, no hidden step.

WHY HTML
--------
The frontend opens a file by decoding its data URL into a blob and opening
that in a tab (see openAttachment in app.html). HTML displays immediately --
no reader, no download, no print step -- and it prints cleanly if the student
wants paper. PDFs would need a dependency and a font; markdown would
download instead of displaying.

EVERYTHING INTERPOLATED IS ESCAPED
----------------------------------
This is model output being written to a document a student will open in a
browser. Escaping is not optional here, and it's tested with script and
onerror payloads.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timezone

MAX_DOCUMENT_CHARS = 200_000


def esc(value: object) -> str:
    return (str(value if value is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _shell(title: str, subtitle: str, body: str) -> str:
    """One house style for every generated document."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
          max-width: 46rem; margin: 3rem auto; padding: 0 1.5rem 4rem;
          color: #1B1F3B; line-height: 1.6; }}
  h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
  .sub {{ color: #6B7280; font-size: .9rem; margin-bottom: 2.5rem; }}
  section {{ border-top: 1px solid #E6E7EB; padding-top: 1.5rem; margin-top: 2rem; }}
  h2 {{ font-size: 1.15rem; margin-bottom: .5rem; }}
  h3 {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .05em;
        color: #6B7280; margin: 1.25rem 0 .5rem; }}
  ol, ul {{ padding-left: 1.4rem; }}
  li {{ margin-bottom: .4rem; }}
  code {{ background: #F4F5F7; padding: .1rem .3rem; border-radius: 4px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .95rem; }}
  td, th {{ border-bottom: 1px solid #E6E7EB; padding: .4rem .3rem;
            text-align: left; }}
  @media print {{ body {{ margin: 0; }} section {{ page-break-inside: avoid; }} }}
</style></head>
<body>
<h1>{esc(title)}</h1>
<div class="sub">{subtitle}</div>
{body}
</body></html>"""


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M")


def descriptor(name: str, html: str, collection: str = "Documents") -> dict:
    """Wrap rendered HTML as a file the store can save."""
    payload = html[:MAX_DOCUMENT_CHARS].encode("utf-8")
    return {
        "name": _safe_filename(name),
        "type": "text/html",
        "size": len(payload),
        "dataUrl": "data:text/html;base64,"
                   + base64.b64encode(payload).decode("ascii"),
        "collectionName": collection,
    }


def _safe_filename(name: str) -> str:
    """
    Turn a model-supplied title into a filename.

    Separators go first, which already stops traversal. Runs of dots go too:
    "../../etc/passwd" became ".. .. etc passwd.html" -- harmless, since
    nothing can traverse without a separator, and still not something to hand
    to a filesystem or show in a UI. A leading dot would also hide the file on
    a Unix box if it were ever written to disk.
    """
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(name or "Document"))
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)[:120] or "Document"
    return cleaned if cleaned.lower().endswith(".html") else f"{cleaned}.html"


# --------------------------------------------------------------------------
# Study guides
# --------------------------------------------------------------------------


def study_guide(guide: dict) -> dict | None:
    """Render a study set. Returns a file descriptor, or None if it's empty."""
    sections = guide.get("sections") or []
    if not sections:
        return None

    course = str(guide.get("course") or "").strip()
    topic = str(guide.get("topic") or "").strip()
    title = f"{course} study guide".strip() or "Study guide"

    body = []
    for section in sections[:30]:
        body.append(f"<section><h2>{esc(section.get('topic'))}</h2>")
        summary = str(section.get("summary") or "").strip()
        if summary:
            body.append(f"<p>{esc(summary)}</p>")
        questions = [q for q in (section.get("questions") or []) if q][:15]
        if questions:
            body.append("<h3>Practice questions</h3><ol>")
            body.extend(f"<li>{esc(q)}</li>" for q in questions)
            body.append("</ol>")
        body.append("</section>")

    plural = "s" if len(sections) != 1 else ""
    subtitle = (f"{esc(topic)} &middot; " if topic else "") + \
               f"{len(sections)} topic{plural} &middot; made by Office Hours, {_stamp()}"
    return descriptor(title, _shell(title, subtitle, "".join(body)),
                      collection="Study guides")


# --------------------------------------------------------------------------
# Plain text and light markdown
# --------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*[-*•]\s+(.*)")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)")
_HEADING = re.compile(r"^\s*(#{1,3})\s+(.*)")


def text_document(title: str, content: str,
                  collection: str = "Documents") -> dict | None:
    """
    Render plain text or light markdown.

    Handles the small subset a model actually produces -- headings, bullets,
    numbered lists, paragraphs -- and escapes everything else. Not a markdown
    implementation: a reliable one for the shapes that turn up, which beats a
    partial one that renders half the document as literal asterisks.
    """
    text = str(content or "").strip()
    if not text:
        return None

    body, list_kind, buffer = [], None, []

    def flush_list() -> None:
        nonlocal list_kind, buffer
        if buffer and list_kind:
            tag = "ol" if list_kind == "ol" else "ul"
            body.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in buffer)
                        + f"</{tag}>")
        list_kind, buffer = None, []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            flush_list()
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_list()
            level = min(len(heading.group(1)) + 1, 3)
            body.append(f"<h{level}>{esc(heading.group(2))}</h{level}>")
            continue

        bullet = _BULLET.match(line)
        if bullet:
            if list_kind != "ul":
                flush_list()
                list_kind = "ul"
            buffer.append(esc(bullet.group(1)))
            continue

        numbered = _NUMBERED.match(line)
        if numbered:
            if list_kind != "ol":
                flush_list()
                list_kind = "ol"
            buffer.append(esc(numbered.group(1)))
            continue

        flush_list()
        body.append(f"<p>{esc(line.strip())}</p>")

    flush_list()
    if not body:
        return None

    return descriptor(title or "Document",
                      _shell(title or "Document",
                             f"Made by Office Hours, {_stamp()}",
                             "".join(body)),
                      collection=collection)


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------


def schedule_document(blocks: list[dict], title: str = "Study plan",
                      rationale: str = "") -> dict | None:
    """Render a saved schedule as a printable table, grouped by day."""
    rows = [b for b in (blocks or []) if b.get("starts_at")]
    if not rows:
        return None

    by_day: dict[str, list[dict]] = {}
    for block in sorted(rows, key=lambda b: str(b.get("starts_at"))):
        by_day.setdefault(str(block["starts_at"])[:10], []).append(block)

    body = []
    if rationale:
        body.append(f"<p>{esc(rationale)}</p>")
    for day, blocks_today in by_day.items():
        try:
            heading = datetime.fromisoformat(day).strftime("%A %d %B")
        except ValueError:
            heading = day
        body.append(f"<section><h2>{esc(heading)}</h2><table><tbody>")
        for block in blocks_today:
            start = str(block.get("starts_at") or "")[11:16]
            end = str(block.get("ends_at") or "")[11:16]
            when = f"{start}&ndash;{end}" if end else start
            minutes = block.get("est_minutes")
            body.append(
                f"<tr><td style='width:8rem'>{esc(when)}</td>"
                f"<td>{esc(block.get('task'))}</td>"
                f"<td style='width:5rem'>{esc(str(minutes) + ' min') if minutes else ''}</td></tr>")
        body.append("</tbody></table></section>")

    total = sum(int(b.get("est_minutes") or 0) for b in rows) / 60
    subtitle = (f"{len(rows)} session{'s' if len(rows) != 1 else ''} &middot; "
                f"{total:.1f}h &middot; made by Office Hours, {_stamp()}")
    return descriptor(title, _shell(title, subtitle, "".join(body)),
                      collection="Schedules")
