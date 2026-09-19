/* ===========================================================================
   Canvas export — get your REAL assignments, no token, no HTML parsing.

   WHY THIS EXISTS
   Saving a Canvas page with "Save as" gives you an empty skeleton, because
   Canvas draws its pages with JavaScript after the HTML arrives. The
   assignments are fetched separately from Canvas's own API.

   So: ask that API directly. You're already logged in, and the browser sends
   your session cookie automatically with same-origin requests, so no token
   and no password are needed. You get clean JSON instead of markup.

   HOW TO USE
   1. Open canvas.pitt.edu in Chrome and make sure you're logged in.
   2. Press Cmd-Option-J (Mac) to open the Console.
   3. Paste this ENTIRE file, press Enter.
   4. Wait. It prints progress as it goes.
   5. It downloads canvas_export.json. Move that into canvas_pages/.

   The file it writes contains courses, assignment titles, due dates, point
   values, and submission status. It deliberately does NOT include your name,
   email, user id, or any session data — see SAFE_FIELDS below.
   =========================================================================== */

(async () => {
  const PER_PAGE = 100;

  // Only these fields are kept. Everything else Canvas returns is discarded,
  // so the output file is safe to commit to a public repo.
  const SAFE_COURSE_FIELDS = ["id", "name", "course_code", "start_at",
                            "enrollment_term_id", "term"];
  const SAFE_ASSIGNMENT_FIELDS = [
    "id", "name", "due_at", "points_possible", "submission_types",
    "html_url", "published", "assignment_group_id",
  ];

  const pick = (obj, fields) => {
    const out = {};
    for (const f of fields) if (obj[f] !== undefined) out[f] = obj[f];
    return out;
  };

  /* Canvas paginates: it returns 100 items plus a Link header pointing at the
     next page. Keep following that until there's no "next" link left. */
  async function getAll(url) {
    const results = [];
    let next = url;
    while (next) {
      const resp = await fetch(next, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!resp.ok) {
        console.warn(`  ${resp.status} on ${next}`);
        break;
      }
      const page = await resp.json();
      results.push(...(Array.isArray(page) ? page : [page]));

      const link = resp.headers.get("Link") || "";
      const m = link.match(/<([^>]+)>;\s*rel="next"/);
      next = m ? m[1] : null;
    }
    return results;
  }

  console.log("%cCanvas export starting…", "font-weight:bold");

  // ---- 1. active courses -------------------------------------------------
  const courses = await getAll(
    `/api/v1/courses?enrollment_state=active&include[]=term&per_page=${PER_PAGE}`
  );
  console.log(`found ${courses.length} active courses`);

  if (courses.length === 0) {
    console.error(
      "No courses came back. Are you definitely logged in, and is this tab " +
      "actually on canvas.pitt.edu?"
    );
    return;
  }

  // ---- 2. assignments per course ----------------------------------------
  const out = { exported_at: new Date().toISOString(), courses: [] };
  let total = 0;

  for (const course of courses) {
    const label = course.course_code || course.name || course.id;
    let assignments = [];
    try {
      assignments = await getAll(
        `/api/v1/courses/${course.id}/assignments` +
        `?per_page=${PER_PAGE}&include[]=submission&order_by=due_at`
      );
    } catch (err) {
      console.warn(`  ${label}: ${err.message}`);
    }

    const cleaned = assignments.map((a) => {
      const item = pick(a, SAFE_ASSIGNMENT_FIELDS);
      // Submission status only — never the score comments or feedback text.
      const sub = a.submission || {};
      item.workflow_state = sub.workflow_state || "unsubmitted";
      item.submitted = Boolean(sub.submitted_at);
      item.score = sub.score ?? null;
      return item;
    });

    out.courses.push({ ...pick(course, SAFE_COURSE_FIELDS), assignments: cleaned });
    total += cleaned.length;
    console.log(`  ${label}: ${cleaned.length} assignments`);
  }

  out.total_assignments = total;

  // ---- 3. planner items (upcoming things, incl. non-assignment events) ---
  try {
    const start = new Date().toISOString().slice(0, 10);
    const planner = await getAll(
      `/api/v1/planner/items?start_date=${start}&per_page=${PER_PAGE}`
    );
    out.planner = planner.map((p) => ({
      type: p.plannable_type,
      title: p.plannable?.title || p.plannable?.name || null,
      due: p.plannable_date || p.plannable?.due_at || null,
      course_id: p.course_id ?? null,
    }));
    console.log(`  planner: ${out.planner.length} upcoming items`);
  } catch (err) {
    console.warn("  planner unavailable (fine, it's optional)");
  }

  // ---- 4. download -------------------------------------------------------
  const blob = new Blob([JSON.stringify(out, null, 2)], {
    type: "application/json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "canvas_export.json";
  document.body.appendChild(a);
  a.click();
  a.remove();

  console.log(
    `%cDone — ${total} assignments across ${out.courses.length} courses. ` +
    `Downloaded canvas_export.json`,
    "font-weight:bold;color:green"
  );
  console.log("Move it into canvas_pages/ then run: python3 canvas.py");
})();
