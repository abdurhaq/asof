/* AsOf front-end — vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const DAY = 86400000;
  const parseISO = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)); };
  const toISO = (dt) => dt.toISOString().slice(0, 10);
  const fmt = (s, opts = { day: "numeric", month: "short", year: "numeric" }) =>
    s ? parseISO(s).toLocaleDateString("en-GB", { ...opts, timeZone: "UTC" }) : "—";
  const TYPE_LABEL = { master_direction: "Master Direction", master_circular: "Master Circular", circular: "Circular" };
  const SYS_LABEL = { asof: "AsOf", naive_rag: "Naive RAG", hybrid_rag: "Hybrid RAG" };
  const STEP_LABEL = { understand: "Understand", retrieve: "Retrieve", rewrite: "Rewrite", graph: "Walk graph", generate: "Write", verify: "Verify" };

  const state = { min: null, max: null, today: toISO(new Date()), docCache: new Map() };

  /* ---------- theme ---------- */
  const root = document.documentElement;
  const storedTheme = (() => { try { return localStorage.getItem("asof-theme"); } catch { return null; } })();
  if (storedTheme) root.dataset.theme = storedTheme;
  $("#theme-toggle").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("asof-theme", root.dataset.theme); } catch {}
  });

  /* ---------- api ---------- */
  async function api(path, body) {
    const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  /* ---------- boot ---------- */
  async function boot() {
    try {
      const [health, stats] = await Promise.all([api("/api/health"), api("/api/stats")]);
      const st = $("#status");
      st.classList.add(health.offline_llm ? "offline" : "ready");
      $("#status-text").textContent =
        `${health.documents.toLocaleString()} docs · ${health.relations.toLocaleString()} links · ` +
        (health.offline_llm ? "offline answerer (add GROQ_API_KEY)" : health.llm.replace("groq:", "Groq · ")) +
        ` · ${health.graph_backend === "Neo4jStore" ? "Neo4j" : "in-memory graph"}`;
      st.title = `Embedder: ${health.embedder} · Reranker: ${health.reranker ? "on" : "off"} · Chunks: ${health.chunks}`;
      setupTimeMachine(stats);
    } catch (e) {
      $("#status").classList.add("error");
      $("#status-text").textContent = "Index not loaded — run `python -m asof.cli build`";
    }
    try {
      const ex = await api("/api/examples");
      $("#examples").innerHTML = ex.map((q) =>
        `<button class="chip" type="button" data-q="${esc(q.question)}"><b>${esc(q.kind.replace("_", " "))}</b>${esc(q.question)}</button>`).join("");
    } catch {}
  }

  $("#examples").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    $("#question").value = b.dataset.q;
    autosize();
    submit();
  });

  /* ---------- time machine ---------- */
  const range = $("#tm-range");
  function setupTimeMachine(stats) {
    const minYear = stats.min_date ? parseISO(stats.min_date).getUTCFullYear() : 2015;
    state.min = Date.UTC(minYear, 0, 1);
    state.max = parseISO(state.today).getTime();
    const days = Math.round((state.max - state.min) / DAY);
    range.max = String(days);
    range.value = String(days);

    // histogram: one bar per year, height = documents issued that year
    const counts = new Map(stats.by_year.map((r) => [r.year, r.count]));
    const lastYear = new Date(state.max).getUTCFullYear();
    const years = [];
    for (let y = minYear; y <= lastYear; y++) years.push(y);
    const peak = Math.max(1, ...years.map((y) => counts.get(y) || 0));
    $("#tm-hist").innerHTML = years.map((y) =>
      `<span data-y="${y}" style="height:${Math.max(6, ((counts.get(y) || 0) / peak) * 100)}%" title="${y}: ${counts.get(y) || 0} documents"></span>`).join("");
    const step = years.length > 14 ? Math.ceil(years.length / 8) : years.length > 8 ? 2 : 1;
    $("#tm-years").innerHTML = years.filter((y, i) => i % step === 0 || y === lastYear).map((y) => {
      const left = ((Date.UTC(y, 0, 1) - state.min) / (state.max - state.min)) * 100;
      return `<span style="left:${Math.min(100, left)}%">${y}</span>`;
    }).join("");
    updateTM();
  }
  function sliderDate() { return toISO(new Date(state.min + Number(range.value) * DAY)); }
  function updateTM() {
    if (state.min == null) return;
    const iso = sliderDate();
    const pinned = $("#pin-date").checked;
    $("#timemachine").classList.toggle("pinned", pinned);
    $("#tm-date").textContent = iso === state.today ? "Today" : fmt(iso, { day: "numeric", month: "long", year: "numeric" });
    $("#tm-mode").textContent = pinned ? "pinned — overrides dates in your question" : "from your question, else today · drag to pin a date";
    const y = parseISO(iso).getUTCFullYear();
    document.querySelectorAll("#tm-hist span").forEach((s) => s.classList.toggle("past", Number(s.dataset.y) <= y));
  }
  function showParsedDates(dates) {
    if (state.min == null || !dates.length) return;
    const last = parseISO(dates.at(-1)).getTime();
    range.value = String(Math.round((Math.min(last, state.max) - state.min) / DAY));
    updateTM();
    $("#tm-date").textContent = dates.map((d) => fmt(d, { day: "numeric", month: "long", year: "numeric" })).join(" → ");
    $("#tm-mode").textContent = "read from your question · drag to pin a different date";
  }
  range.addEventListener("input", () => { $("#pin-date").checked = true; updateTM(); });
  $("#pin-date").addEventListener("change", updateTM);

  /* ---------- ask ---------- */
  const q = $("#question");
  function autosize() { q.style.height = "auto"; q.style.height = Math.min(q.scrollHeight, 180) + "px"; }
  q.addEventListener("input", autosize);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); submit(); });

  let runId = 0;
  async function submit() {
    const question = q.value.trim();
    if (!question) { q.focus(); return; }
    const my = ++runId;
    const asOf = $("#pin-date").checked ? sliderDate() : null;
    const compare = $("#compare-naive").checked;
    const systems = compare ? ["asof", "naive_rag"] : ["asof"];
    document.body.classList.add("has-results");
    $("#results").hidden = false;
    $("#compare-banner").hidden = true;
    const answers = $("#answers");
    answers.className = "answers" + (compare ? " two" : "");
    answers.innerHTML = systems.map((s) => `<div class="run" id="run-${s}">${loadingCard(s)}</div>`).join("");
    const timers = systems.map((s) => animatePipeline($(`#run-${s}`)));
    $("#ask-btn").disabled = true;
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });

    const results = await Promise.all(systems.map((s) =>
      api("/api/ask", { question, as_of: asOf, system: s }).then((r) => ({ ok: true, r }), (e) => ({ ok: false, e }))));
    timers.forEach(clearInterval);
    if (my !== runId) return;
    $("#ask-btn").disabled = false;
    results.forEach((res, i) => {
      const el = $(`#run-${systems[i]}`);
      el.innerHTML = res.ok ? renderRun(res.r, compare) : `<div class="card error-card">Something went wrong: ${esc(res.e.message)}</div>`;
    });
    drawTimelines(answers);
    const first = results.find((r) => r.ok);
    if (first && !$("#pin-date").checked) showParsedDates(first.r.parsed.dates);
    if (compare && results.every((r) => r.ok)) renderBanner(results[0].r, results[1].r);
  }

  function loadingCard(system) {
    return `<div class="card loading reveal">
      <div class="run-head"><div class="system-name">${SYS_LABEL[system]}</div></div>
      <div class="pipeline">${["Understand", "Retrieve", "Walk graph", "Write", "Verify"].map((s) => `<span>${s}</span>`).join("")}</div>
      <div class="bars"><span></span><span></span><span></span></div></div>`;
  }
  function animatePipeline(el) {
    let i = 0;
    const tick = () => {
      const spans = el.querySelectorAll(".pipeline span");
      spans.forEach((s, j) => { s.className = j < i ? "done" : j === i ? "on" : ""; });
      i = Math.min(i + 1, spans.length - 1);
    };
    tick();
    return setInterval(tick, 700);
  }

  /* ---------- render ---------- */
  function renderAnswer(text, contexts, asofDates) {
    const stale = new Set(contexts.filter((c) => c.doc.in_force === false).map((c) => c.n));
    const inline = (s) => esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\[(\d+)\]/g, (_, n) => `<button type="button" class="cite${stale.has(+n) ? " stale" : ""}" data-n="${n}" aria-label="Source ${n}">${n}</button>`);
    const lines = text.split(/\n/);
    let html = "", list = null, liOpen = false, sub = false, lastItem = "";
    const closeSub = () => { if (sub) { html += "</ul>"; sub = false; } };
    const closeLi = () => { closeSub(); if (liOpen) { html += "</li>"; liOpen = false; } };
    const closeList = () => { closeLi(); if (list) { html += `</${list}>`; list = null; } };
    for (const raw of lines) {
      const line = raw.trim();
      const bullet = line.match(/^[-*•]\s+(.*)/);
      const num = line.match(/^\d+[.)]\s+(.*)/);
      // bullets under a numbered item ("1. Periodicity: <bullets>") are nested inside that item,
      // so the numbered list continues 1, 2, 3 instead of restarting
      if (bullet && list === "ol" && liOpen && (sub || /^\s/.test(raw) || /[:\u2013\u2014-]\s*$/.test(lastItem))) {
        if (!sub) { html += "<ul>"; sub = true; }
        html += `<li>${inline(bullet[1])}</li>`;
        continue;
      }
      if (bullet || num) {
        const tag = bullet ? "ul" : "ol";
        if (list !== tag) { closeList(); html += `<${tag}>`; list = tag; } else closeLi();
        // keep the model's own numbers (value=...) so numbering survives blank lines
        const n = num ? Number(line.match(/^(\d+)/)[1]) : 0;
        html += num ? `<li value="${n}">${inline(num[1])}` : `<li>${inline(bullet[1])}`;
        liOpen = true;
        lastItem = num ? num[1] : "";
        continue;
      }
      if (!line) continue;  // blank lines between list items must not restart the numbering
      closeList();
      const h = line.match(/^#{1,4}\s+(.*)/);
      html += h ? `<p><strong>${inline(h[1])}</strong></p>` : `<p>${inline(line)}</p>`;
    }
    closeList();
    return html;
  }

  function badgesFor(c) {
    const out = [];
    if (c.doc.in_force === false) out.push(`<span class="badge bad">Not in force on ${fmt(c.group_date)}</span>`);
    else if (c.doc.in_force) out.push(`<span class="badge ok">In force</span>`);
    if (/amended by/i.test(c.status_note)) out.push(`<span class="badge warn">Amended</span>`);
    if (/later replaced/i.test(c.status_note)) out.push(`<span class="badge neutral">Replaced later</span>`);
    if (c.doc.updated_on && c.doc.updated_on > c.group_date) out.push(`<span class="badge warn" title="RBI edits this document in place; the text shown is the later consolidated version">Text updated ${fmt(c.doc.updated_on)}</span>`);
    if (c.via && c.via.startsWith("graph")) out.push(`<span class="badge graph">${esc(c.via.replace("graph: ", "via graph · "))}</span>`);
    out.push(`<span class="badge neutral">${TYPE_LABEL[c.doc.doc_type] || "Document"}</span>`);
    return out.join("");
  }

  function renderRun(r, compact) {
    const dates = r.parsed.dates;
    const cited = new Set(r.citations.map((c) => c.n));
    const sources = r.contexts.filter((c) => cited.size === 0 || cited.has(c.n) || !compact);
    const trace = r.trace.map((t) =>
      `<div class="step ${t.step}" title="${esc(t.detail)}"><span class="s-name"><i></i>${STEP_LABEL[t.step] || t.step}</span><span class="s-ms">${t.ms} ms</span></div>`).join("");
    const intent = { current: "Current rules", as_of: "Point in time", compare: "Comparison", history: "History" }[r.parsed.intent] || r.parsed.intent;
    const answerHtml = renderAnswer(r.answer, r.contexts, dates);
    const ungrounded = r.contexts.length && !r.citations.length ? `<div class="ungrounded">No valid citations in this answer — treat it with caution.</div>` : "";

    const answerCard = `<div class="card answer-card reveal">
      <div class="run-head">
        <div class="system-name">${SYS_LABEL[r.system]}<span class="tag ${r.system === "asof" ? "" : "naive"}">${r.system === "asof" ? "graph + time" : "vector only"}</span></div>
        <div class="meta-row">
          <span class="pill">${esc(intent)}</span>
          ${dates.map((d) => `<span class="pill date">${fmt(d, { day: "numeric", month: "long", year: "numeric" })}</span>`).join("")}
        </div>
      </div>
      <div class="answer">${answerHtml}</div>${ungrounded}
      <div class="trace" aria-label="Agent steps">${trace}</div>
    </div>`;

    const srcCards = sources.map((c) => `
      <button type="button" class="src${c.doc.in_force === false ? " stale" : ""}" data-doc="${esc(c.doc.doc_id)}" data-n="${c.n}">
        <div class="src-top"><span class="src-n">${c.n}</span>
          <div><div class="src-title">${esc(c.doc.title)}</div>
          <div class="src-sub"><span class="mono">${esc(c.doc.ref_no)}</span><span>issued ${fmt(c.doc.issue_date)}</span></div></div>
        </div>
        <div class="badges">${badgesFor(c)}</div>
        <div class="src-snippet">${esc(c.text)}</div>
      </button>`).join("");
    const sourcesCol = `<div class="sources reveal"><h4>Sources${compact ? " cited" : ""} · ${sources.length}</h4>${srcCards || '<div class="tl-empty">No sources.</div>'}</div>`;

    const tl = r.system === "asof" && r.timelines.length
      ? `<div class="card timeline-card reveal"><h4>Version timeline</h4>
          <div class="tl-legend"><span><i style="background:var(--accent)"></i>in force on your date</span><span><i style="background:var(--line-2)"></i>other versions</span><span><i style="background:var(--gold);border-radius:50%;width:9px;height:9px"></i>amendment</span></div>
          ${r.timelines.map((t) => tlHost(t, dates)).join("")}</div>`
      : "";
    return answerCard + sourcesCol + tl;
  }

  function renderBanner(asof, naive) {
    const staleNaive = new Set(naive.contexts.filter((c) => c.doc.in_force === false && naive.citations.some((x) => x.n === c.n)).map((c) => c.doc.doc_id));
    const staleNaiveRetrieved = new Set(naive.contexts.filter((c) => c.doc.in_force === false).map((c) => c.doc.doc_id));
    const staleAsof = new Set(asof.contexts.filter((c) => c.doc.in_force === false).map((c) => c.doc.doc_id));
    const b = $("#compare-banner");
    const when = fmt(asof.parsed.dates.at(-1), { day: "numeric", month: "long", year: "numeric" });
    b.hidden = false;
    if (staleNaive.size || staleNaiveRetrieved.size) {
      b.className = "compare-banner reveal";
      b.innerHTML = `<span>⚠</span><span><strong>Naive RAG ${staleNaive.size ? `cited ${staleNaive.size}` : `retrieved ${staleNaiveRetrieved.size}`} document${(staleNaive.size || staleNaiveRetrieved.size) > 1 ? "s" : ""} that ${(staleNaive.size || staleNaiveRetrieved.size) > 1 ? "were" : "was"} not in force on ${when}.</strong> AsOf used ${staleAsof.size ? staleAsof.size + " out-of-date" : "none"}.</span>`;
    } else {
      b.className = "compare-banner good reveal";
      b.innerHTML = `<span>✓</span><span>Both systems used documents in force on ${when} for this question. Try a date before a rule was replaced to see the difference.</span>`;
    }
  }

  /* ---------- timeline (Gantt of versions) ---------- */
  const tlData = new Map();
  let tlSeq = 0;
  function tlHost(t, dates, focusId = null) {
    const id = `tl${++tlSeq}`;
    tlData.set(id, [t, dates, focusId]);
    return `<div class="tl-host" data-tl="${id}"></div>`;
  }
  function drawTimelines(scope = document) {
    scope.querySelectorAll(".tl-host").forEach((h) => {
      const d = tlData.get(h.dataset.tl);
      if (d) h.innerHTML = timelineSVG(d[0], d[1], Math.max(300, h.clientWidth), d[2]);
    });
  }
  let rsz;
  addEventListener("resize", () => { clearTimeout(rsz); rsz = setTimeout(() => drawTimelines(), 150); });

  function timelineSVG(t, asofDates, width = 900, focusId = null) {
    const rows = t.versions;
    if (!rows.length) return `<div class="tl-empty">No version history.</div>`;
    const W = width, labelW = width < 520 ? 104 : 150, rowH = 30, top = 26, padR = 16;
    const H = top + rows.length * rowH + 30;
    const starts = rows.map((v) => parseISO(v.effective_date).getTime());
    const ends = rows.map((v) => (v.valid_to ? parseISO(v.valid_to).getTime() : parseISO(state.today).getTime() + DAY));
    const asofTs = asofDates.map((d) => parseISO(d).getTime());
    let lo = Math.min(...starts, ...asofTs), hi = Math.max(...ends, ...asofTs);
    const pad = Math.max((hi - lo) * 0.04, 30 * DAY);
    lo -= pad; hi += pad;
    const x = (ts) => labelW + ((ts - lo) / (hi - lo)) * (W - labelW - padR);
    const y0 = new Date(lo).getUTCFullYear() + 1, y1 = new Date(hi).getUTCFullYear();
    const yStep = Math.max(1, Math.ceil((y1 - y0 + 1) / Math.max(3, Math.floor((W - labelW) / 70))));
    let grid = "";
    for (let y = y0; y <= y1; y += yStep) {
      const gx = x(Date.UTC(y, 0, 1));
      grid += `<line class="grid" x1="${gx}" x2="${gx}" y1="${top - 6}" y2="${H - 22}"/><text x="${gx}" y="${H - 6}" text-anchor="middle">${y}</text>`;
    }
    const idx = new Map(rows.map((v, i) => [v.doc_id, i]));
    const bars = rows.map((v, i) => {
      const x1 = x(starts[i]), x2 = Math.max(x1 + 4, x(ends[i]));
      const active = asofTs.some((a) => a >= starts[i] && a < ends[i]);
      const yy = top + i * rowH;
      const max = labelW < 120 ? 15 : 22;
      const label = v.ref_no.length > max ? v.ref_no.slice(0, max - 1) + "…" : v.ref_no;
      return `<g><text class="lbl" x="0" y="${yy + 15}">${esc(label)}</text>
        <rect class="bar${active ? " active" : ""}${v.doc_id === focusId ? " focus" : ""}" data-doc="${esc(v.doc_id)}" x="${x1}" y="${yy + 4}" width="${x2 - x1}" height="16" rx="5">
          <title>${esc(v.title)}\n${fmt(v.effective_date)} → ${v.valid_to ? fmt(v.valid_to) : "today"}</title></rect></g>`;
    }).join("");
    const amends = t.amendments.filter((a) => idx.has(a.target)).map((a) => {
      const cx = x(parseISO(a.effective_date).getTime()), cy = top + idx.get(a.target) * rowH + 12;
      return `<path class="amend" data-doc="${esc(a.doc_id)}" d="M${cx} ${cy - 7} L${cx + 7} ${cy} L${cx} ${cy + 7} L${cx - 7} ${cy} Z"><title>Amended by ${esc(a.ref_no)} · ${fmt(a.effective_date)}\n${esc(a.title)}</title></path>`;
    }).join("");
    const lines = asofDates.map((d) => {
      const ax = x(parseISO(d).getTime());
      const anchor = ax > W - 60 ? "end" : ax < labelW + 40 ? "start" : "middle";
      return `<line class="asof" x1="${ax}" x2="${ax}" y1="${top - 10}" y2="${H - 22}"/><text class="asof-lbl" x="${ax}" y="${top - 14}" text-anchor="${anchor}">${fmt(d)}</text>`;
    }).join("");
    return `<svg class="tl-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="Version timeline">${grid}${bars}${amends}${lines}</svg>`;
  }

  /* ---------- interactions: citations, sources, drawer ---------- */
  $("#answers").addEventListener("click", (e) => {
    const cite = e.target.closest(".cite");
    if (cite) {
      const run = cite.closest(".run");
      const src = run.querySelector(`.src[data-n="${cite.dataset.n}"]`);
      if (src) { src.scrollIntoView({ behavior: "smooth", block: "center" }); flash(src); }
      else openDoc(null);
      return;
    }
    const src = e.target.closest("[data-doc]");
    if (src) openDoc(src.dataset.doc);
  });
  $("#answers").addEventListener("mouseover", (e) => {
    const cite = e.target.closest(".cite");
    document.querySelectorAll(".src.hl").forEach((s) => s.classList.remove("hl"));
    if (cite) cite.closest(".run").querySelector(`.src[data-n="${cite.dataset.n}"]`)?.classList.add("hl");
  });
  function flash(el) { el.classList.add("hl"); setTimeout(() => el.classList.remove("hl"), 1400); }

  const drawer = $("#drawer");
  let lastFocus = null;
  async function openDoc(id) {
    if (!id) return;
    lastFocus = document.activeElement;
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    const body = $("#drawer-body");
    body.innerHTML = `<div class="loading"><div class="bars"><span></span><span></span><span></span></div></div>`;
    $(".drawer-close").focus();
    try {
      const data = state.docCache.get(id) || await api(`/api/doc/${encodeURIComponent(id)}`);
      state.docCache.set(id, data);
      body.innerHTML = renderDoc(data);
      drawTimelines(body);
    } catch (e) {
      body.innerHTML = `<div class="card error-card">${esc(e.message)}</div>`;
    }
  }
  function closeDrawer() {
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    lastFocus?.focus?.();
  }
  drawer.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) return closeDrawer();
    const rel = e.target.closest("[data-doc]");
    if (rel) openDoc(rel.dataset.doc);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && drawer.classList.contains("open")) closeDrawer(); });

  function renderDoc({ doc, relations, timeline }) {
    const REL = {
      "out:SUPERSEDES": ["Replaces", "ok"], "in:SUPERSEDES": ["Replaced by", "bad"],
      "out:AMENDS": ["Amends", "warn"], "in:AMENDS": ["Amended by", "warn"],
      "out:REFERS_TO": ["Refers to", "neutral"], "in:REFERS_TO": ["Referred to by", "neutral"],
    };
    const status = doc.valid_to ? `<span class="badge bad">Replaced from ${fmt(doc.valid_to)}</span>` : `<span class="badge ok">In force today</span>`;
    const rels = relations.map((r) => {
      const [label, cls] = REL[`${r.direction}:${r.type}`] || [r.type, "neutral"];
      return `<button type="button" class="rel" data-doc="${esc(r.doc.doc_id)}">
        <div class="rel-head"><span class="badge ${cls}">${label}</span><strong>${esc(r.doc.title)}</strong></div>
        <div class="src-sub"><span class="mono">${esc(r.doc.ref_no)}</span><span>effective ${fmt(r.effective_date)}</span><span>found by ${esc(r.method.replace("_", " "))}</span></div>
        ${r.evidence ? `<div class="rel-ev">“${esc(r.evidence)}”</div>` : ""}</button>`;
    }).join("");
    const tlDates = [state.today];
    return `
      <div class="doc-kicker">${TYPE_LABEL[doc.doc_type] || "Document"}</div>
      <h2 class="doc-title">${esc(doc.title)}</h2>
      <div class="doc-actions">${status}</div>
      <div class="doc-grid">
        <div><span>Reference</span><b class="mono">${esc(doc.ref_no)}</b></div>
        <div><span>Department ref.</span><b class="mono">${esc(doc.dept_ref || "—")}</b></div>
        <div><span>Issued</span><b>${fmt(doc.issue_date, { day: "numeric", month: "long", year: "numeric" })}</b></div>
        <div><span>In force from</span><b>${fmt(doc.effective_date, { day: "numeric", month: "long", year: "numeric" })}</b></div>
      </div>
      ${doc.url ? `<div class="doc-actions"><a class="btn-ghost" href="${esc(doc.url)}" target="_blank" rel="noopener">Open on rbi.org.in ↗</a></div>` : ""}
      ${timeline.versions.length > 1 || timeline.amendments.length ? `<div class="doc-section">Version timeline</div>${tlHost(timeline, tlDates, doc.doc_id)}` : ""}
      <div class="doc-section">Links in the amendment graph · ${relations.length}</div>
      <div class="rel-list">${rels || '<div class="tl-empty">No links found for this document.</div>'}</div>
      <div class="doc-section">Full text</div>
      <div class="doc-text">${esc(doc.text)}</div>`;
  }

  boot();
})();
