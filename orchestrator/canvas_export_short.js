/* Canvas export, short version. Paste this whole block into the DevTools
   Console on canvas.pitt.edu while logged in, then press Enter.
   Downloads canvas_export.json. Move it into canvas_pages/. */
(async () => {
  const P = 100;
  const get = async (u) => {
    const out = [];
    let next = u;
    while (next) {
      const r = await fetch(next, { headers: { Accept: "application/json" }, credentials: "same-origin" });
      if (!r.ok) { console.warn(r.status, next); break; }
      const page = await r.json();
      out.push(...(Array.isArray(page) ? page : [page]));
      const m = (r.headers.get("Link") || "").match(/<([^>]+)>;\s*rel="next"/);
      next = m ? m[1] : null;
    }
    return out;
  };

  const courses = await get(`/api/v1/courses?enrollment_state=active&include[]=term&per_page=${P}`);
  console.log(`${courses.length} active courses`);
  if (!courses.length) return console.error("No courses. Logged in? On canvas.pitt.edu?");

  const out = { exported_at: new Date().toISOString(), courses: [] };
  let total = 0;

  for (const c of courses) {
    const a = await get(`/api/v1/courses/${c.id}/assignments?per_page=${P}&include[]=submission&order_by=due_at`);
    const assignments = a.map((x) => ({
      id: x.id,
      name: x.name,
      due_at: x.due_at,
      points_possible: x.points_possible,
      submission_types: x.submission_types,
      html_url: x.html_url,
      workflow_state: (x.submission || {}).workflow_state || "unsubmitted",
      submitted: Boolean((x.submission || {}).submitted_at),
      score: (x.submission || {}).score ?? null,
    }));
    out.courses.push({ id: c.id, name: c.name, course_code: c.course_code,
                     term: c.term ? c.term.name : null,
                     enrollment_term_id: c.enrollment_term_id ?? null,
                     assignments });
    total += assignments.length;
    console.log(`  ${c.course_code || c.name}: ${assignments.length}`);
  }

  out.total_assignments = total;
  const el = document.createElement("a");
  el.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }));
  el.download = "canvas_export.json";
  document.body.appendChild(el);
  el.click();
  el.remove();
  console.log(`%cDone: ${total} assignments downloaded to canvas_export.json`, "font-weight:bold;color:green");
})();
