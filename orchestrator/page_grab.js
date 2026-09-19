/* ===========================================================================
   Page grab — capture any page AFTER its JavaScript has run.

   WHY YOU NEED THIS
   Notion, Canvas, and most modern sites send an empty HTML skeleton and draw
   the real page with JavaScript. So downloading the URL gets you nothing. But
   your browser has already run that JavaScript: the content is sitting in the
   tab in front of you.

   This copies it out. No install, no headless browser, no API token.

   HOW TO USE
   1. Open the page in Chrome. Wait until you can actually see the content.
   2. Press Cmd-Option-J (Mac) to open the Console.
   3. Paste this whole file and press Enter.
   4. It downloads a .txt file. Move it into ./canvas_pages/.

   Then: python3 ask.py "what are the important dates for CS 1684?"

   The saved file is plain text with no scripts, styles, or tracking, so it's
   safe to commit -- but read it first if the page was behind a login, since
   anything personal on screen ends up in the file.
   =========================================================================== */

(() => {
  // Elements whose contents are never worth keeping.
  const DROP = "script, style, noscript, svg, iframe, nav, footer, " +
               "[aria-hidden='true'], .notion-overlay-container";

  // Lines that appear on every page of a site and carry no information.
  // Single line and only the /i flag: JavaScript has no /x free-spacing flag,
  // so a multi-line regex here is a syntax error, not a tidy regex.
  const BOILERPLATE = /^(home|search|settings|share|duplicate|edit|comment|sign in|log in|log out|menu|close|back|next|previous|skip to content|loading|new page|add a page|favorites|private|shared|templates|trash|cookie|privacy policy|terms|copy link|more options)$/i;

  function extract() {
    // Work on a copy so we don't damage the live page.
    const copy = document.body.cloneNode(true);
    copy.querySelectorAll(DROP).forEach((el) => el.remove());

    // innerText respects what's actually visible; textContent would include
    // hidden elements and run words together.
    const raw = copy.innerText || copy.textContent || "";

    const seen = new Set();
    const lines = [];
    for (let line of raw.split("\n")) {
      line = line.replace(/\s+/g, " ").trim();
      if (!line) continue;
      if (line.length === 1 && !/[a-z0-9]/i.test(line)) continue;
      if (BOILERPLATE.test(line)) continue;
      // Collapse the immediate duplicates that accessibility markup creates.
      if (lines.length && lines[lines.length - 1] === line) continue;
      lines.push(line);
    }
    return lines.join("\n");
  }

  const text = extract();

  if (text.length < 100) {
    console.warn(
      "Only " + text.length + " characters found. Is the page finished " +
      "loading? Scroll through it once, then run this again."
    );
  }

  const header =
    "SOURCE: " + location.href + "\n" +
    "TITLE: " + document.title + "\n" +
    "CAPTURED: " + new Date().toISOString() + "\n" +
    "-".repeat(70) + "\n\n";

  const slug = (document.title || "page")
    .replace(/[^a-z0-9]+/gi, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase()
    .slice(0, 50);

  const blob = new Blob([header + text], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = slug + ".txt";
  document.body.appendChild(a);
  a.click();
  a.remove();

  console.log(
    "%cSaved " + text.length + " characters to " + slug + ".txt",
    "font-weight:bold;color:green"
  );
  console.log("Move it into ./canvas_pages/ then run: python3 canvas.py");
})();
