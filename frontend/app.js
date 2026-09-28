const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const pct = (x) => (x == null ? null : `${Math.round(x * 100)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
const dateTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");
const day = (iso) => (iso ? String(iso).slice(0, 10) : "");

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") : body.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}

async function withBusy(btn, label, fn) {
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${label}`;
  try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = old; }
}

const loading = (html) => `<div class="panel loading"><span class="spinner"></span><div>${html}</div></div>`;
const errorBox = (msg) => `<div class="panel error-panel"><b>Something went wrong</b><p>${esc(msg)}</p></div>`;

// ---------- header: Hindsight connection ----------
async function loadStatus() {
  try {
    const h = await api("/api/health");
    let dot, title, sub;
    if (h.memory_backend !== "hindsight") {
      [dot, title, sub] = ["off", "Offline memory", "No Hindsight key - using the local store"];
    } else if (h.connected) {
      [dot, title, sub] = ["ok", "Hindsight connected", `Bank <b>${esc(h.bank_id)}</b> · <b>${h.memory_count}</b> memories`];
    } else {
      [dot, title, sub] = ["bad", "Hindsight unreachable", `Bank ${esc(h.bank_id)} · check your key / network`];
    }
    const llm = h.llm ? `<span class="chip">LLM: ${esc(h.llm)}</span>` : `<span class="chip warn">LLM off (rule-based)</span>`;
    $("#status").innerHTML = `<div class="hs-status"><span class="dot ${dot}"></span><div><b>${title}</b><span>${sub}</span></div></div>${llm}`;
  } catch {
    $("#status").innerHTML = `<div class="hs-status"><span class="dot bad"></span><div><b>Backend offline</b><span>Start the server and refresh</span></div></div>`;
  }
}

async function refreshActiveCount() {
  const items = await api("/api/incidents?status=investigating").catch(() => []);
  $("#active-count").textContent = items.length ? items.length : "";
}

// ---------- tabs & links (#tab=insights, #incident=INC-2011) ----------
function setHash(hash) { history.replaceState(null, "", hash ? `#${hash}` : location.pathname); }

function showTab(name, { keepHash = false } = {}) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (!keepHash) setHash(name === "new" ? "" : `tab=${name}`);
  if (name === "active") loadActive();
  if (name === "past") loadPast();
  if (name === "insights") loadInsights();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

// ---------- new incident form ----------
let examples = [];
async function loadExamples() {
  const r = await api("/api/examples").catch(() => ({ examples: [], services: [] }));
  examples = r.examples;
  $("#services").innerHTML = r.services.map((s) => `<option value="${esc(s)}">`).join("");
  $("#examples").innerHTML = examples.map((e, i) => `<button type="button" class="chip-btn" data-i="${i}">${esc(e.label)}</button>`).join("");
}
$("#examples").addEventListener("click", (e) => {
  const ex = examples[e.target.closest("[data-i]")?.dataset.i];
  if (!ex) return;
  $("#f-service").value = ex.service;
  $("#f-problem").value = ex.problem;
  $("#f-error").value = ex.error;
  $("#f-details").value = ex.details || "";
});

function readForm() {
  const body = {
    service: $("#f-service").value.trim(),
    problem: $("#f-problem").value.trim(),
    error: $("#f-error").value.trim(),
    details: $("#f-details").value.trim(),
  };
  if (!body.service || !body.problem || !body.error) throw new Error("Please fill in Service, Problem and Error.");
  return body;
}

$("#incident-form").addEventListener("submit", (e) => {
  e.preventDefault();
  $("#form-error").textContent = "";
  let body;
  try { body = readForm(); } catch (err) { $("#form-error").textContent = err.message; return; }
  const previous = $("#incident-view").innerHTML;
  $("#incident-view").innerHTML = loading(`<b>Investigating…</b>
    <ul class="load-steps"><li>🧠 Searching Hindsight memory</li><li>🔍 Checking which memories are the same failure</li><li>💡 Generating the recommendation</li></ul>`);
  withBusy($("#btn-investigate"), "Investigating…", async () => {
    try {
      renderIncident(await api("/api/incidents", { method: "POST", body: JSON.stringify(body) }));
      refreshActiveCount();
      loadStatus();
    } catch (err) {
      $("#form-error").textContent = err.message;
      $("#incident-view").innerHTML = previous;
    }
  });
});

// ---------- incident: building blocks ----------
function outcomeText(o) {
  if (!o) return "";
  if (o.status === "resolved") return `✅ Resolved${o.minutes ? ` in ${o.minutes} min` : ""}`;
  if (o.status === "failed_fix") return "❌ A fix was tried and did not work";
  return "Outcome not recorded";
}

function modeBanner(a) {
  return a.mode === "memory"
    ? `<div class="mode-banner known"><span class="mb-kicker">KNOWN PROBLEM</span><b>🧠 Hindsight memory found</b></div>`
    : `<div class="mode-banner fresh"><span class="mb-kicker">NEW PROBLEM</span><b>🆕 No relevant memory found</b></div>`;
}

function memoryErrorPanel(a) {
  return a.memory_error ? `<div class="panel error-panel"><b>⚠ Hindsight recall failed</b>
    <p>This recommendation was made <b>without</b> memory. Check the server terminal.</p><p class="muted">${esc(a.memory_error)}</p></div>` : "";
}

function flow(rec, a, top) {
  const total = a.matched.reduce((n, m) => n + (m.memories_recalled || 0), 0);
  const worked = top.outcome?.status === "resolved" && top.solution;
  const box = (label, main, sub, cls = "") =>
    `<div class="flow-box ${cls}"><span>${label}</span><b>${esc(main)}</b>${sub ? `<small>${esc(clip(sub, 90))}</small>` : ""}</div>`;
  const n = a.matched.length;
  return `<div class="flow">
    ${box("Current incident", rec.id, rec.input.error)}<i>→</i>
    ${box("Hindsight memory", `${total} memor${total === 1 ? "y" : "ies"} recalled`, `${n} matching incident${n === 1 ? "" : "s"}`, "mem")}<i>→</i>
    ${box("Previous incident", top.id, top.problem || top.error)}<i>→</i>
    ${box("Successful fix", worked ? outcomeText(top.outcome) : "No confirmed fix", worked ? top.solution : "", "ok")}<i>→</i>
    ${box("Current recommendation", `${a.recommendation.confidence} confidence`, a.recommendation.suggested_fix, "rec")}
  </div>`;
}

function memoryUsedPanel(rec, a) {
  const top = a.matched[0];
  if (!top) return "";
  const sim = pct(top.similarity);
  const others = a.matched.slice(1).map((m) => `<span class="mini-chip">${esc(m.id)}${m.similarity != null ? ` · ${pct(m.similarity)}` : ""}</span>`).join("");
  return `<div class="panel memory-used">
    <div class="head"><h3>🧠 MEMORY USED</h3><span class="muted">Retrieved from Hindsight bank · ${a.matched.length} matching incident${a.matched.length === 1 ? "" : "s"}</span></div>
    <div class="mu-grid">
      <div class="mu-item"><span class="k">Previous incident</span><span class="v">${esc(top.id)}</span><span class="sub">${esc(top.service || "")}${top.date ? " · " + esc(day(top.date)) : ""}</span></div>
      <div class="mu-item"><span class="k">Similarity</span><span class="v">${sim || "—"}</span><span class="sub">${sim ? "Hindsight semantic match" : "score not recorded for this incident"}</span></div>
      <div class="mu-item"><span class="k">Previous outcome</span><span class="v ${top.outcome?.status === "resolved" ? "ok" : ""}">${outcomeText(top.outcome)}</span></div>
      <div class="mu-item wide"><span class="k">Previous problem</span><span class="v">${esc(top.problem || "—")}${top.error ? ` <code>${esc(clip(top.error, 80))}</code>` : ""}</span></div>
      <div class="mu-item wide"><span class="k">Previous solution</span><span class="v">${esc(top.solution || "—")}</span></div>
    </div>
    ${others ? `<p class="also">Also matched: ${others}</p>` : ""}
    ${(a.rejected || []).length ? `<p class="also">Not used (different failure): ${a.rejected.map((x) => esc(x.id)).join(", ")}</p>` : ""}
    ${top.facts?.length ? `<details><summary>What Hindsight recalled about ${esc(top.id)} (${top.facts.length})</summary><ul class="facts">${top.facts.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></details>` : ""}
    ${flow(rec, a, top)}
  </div>`;
}

function newPatternPanel(rec, a) {
  const r = a.recommendation;
  const rejected = (a.rejected || []).length
    ? `<div class="rejected"><b>Hindsight returned look-alikes, but none is the same failure, so none was used:</b>
        <ul>${a.rejected.map((x) => `<li><b>${esc(x.id)}</b>${x.similarity != null ? ` (${pct(x.similarity)} similar)` : ""}: ${esc(x.reason || "judged a different failure")}</li>`).join("")}</ul>
        ${a.match_reason ? `<span>Relevance check: <i>${esc(a.match_reason)}</i></span>` : ""}</div>`
    : "";
  return `<div class="panel new-pattern">
    <div class="head"><h3>🆕 NEW PATTERN</h3><span class="badge conf-${r.confidence}">Confidence: ${esc(r.confidence)}</span></div>
    <p><b>No relevant Hindsight memory found.</b></p>
    ${rejected}
    <p>This recommendation is based on:</p>
    <ul class="checks">
      <li>Current error information</li><li>Problem description</li>
      ${rec.input.details ? "<li>Logs / recent changes</li>" : ""}
      <li>Engineering knowledge</li>
    </ul></div>`;
}

function recommendationPanel(a) {
  const r = a.recommendation;
  const top = a.matched?.[0];
  const hypothesis = a.root_cause_status !== "historical";
  const causeTag = hypothesis
    ? `<span class="tag hypo">Hypothesis · not yet confirmed</span>`
    : `<span class="tag hist">From memory${top ? ` · confirmed in ${esc(top.id)}` : ""}</span>`;
  const causeNote = hypothesis
    ? `<p class="warn-note">⚠️ Root cause should be confirmed after applying the diagnostic/fix.</p>`
    : `<p class="info-note">ℹ️ This root cause was confirmed in a past incident. Check the evidence matches this one.</p>`;
  const evidence = r.evidence.length ? `<div class="field"><div class="label">📋 Evidence</div><ul>${r.evidence.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  const avoid = r.avoid.length ? `<div class="field"><div class="label">⛔ Don't do this</div><ul class="avoid">${r.avoid.map((x) => `<li>${esc(x.action)}<span class="reason">${esc(x.reason)}</span></li>`).join("")}</ul></div>` : "";
  const steps = r.steps.length ? `<details><summary>How to apply it (${r.steps.length} steps)</summary><ol class="steps">${r.steps.map((s) =>
    `<li>${esc(s.step)}${s.source && s.source !== "general" && s.source !== "current-data" ? `<span class="src">${esc(s.source)}</span>` : ""}</li>`).join("")}</ol></details>` : "";
  const meta = [r.estimated_time_to_resolve_min ? `⏱ ~${r.estimated_time_to_resolve_min} min to fix` : "", r.generated_by === "llm" ? "AI analysis" : "rule-based (AI unavailable)"].filter(Boolean).join(" · ");
  return `<div class="panel rec">
    <div class="head"><h3>💡 Recommendation${a.n > 1 ? ` · attempt ${a.n}` : ""}</h3><span class="badge conf-${r.confidence}">${esc(r.confidence)} confidence</span></div>
    <p class="summary">${esc(r.summary)}</p>
    <div class="field"><div class="label">Likely root cause ${causeTag}</div><div>${esc(r.root_cause)}</div>${causeNote}</div>
    <div class="field fix"><div class="label">Suggested fix</div><div class="fix-text">${esc(r.suggested_fix)}</div></div>
    ${evidence}${avoid}${steps}
    <p class="muted">${meta}</p></div>`;
}

function whyPanel(rec, a) {
  const cur = `<code>${esc(clip(rec.input.error, 80))}</code> on ${esc(rec.input.service)}`;
  if (a.mode === "memory" && a.matched?.length) {
    const top = a.matched[0];
    const signals = [...(top.signals || [])];
    if (a.match_reason) signals.push(`Relevance check: ${a.match_reason}`);
    return `<div class="panel why">
      <h3>🔍 WHY THIS RECOMMENDATION?</h3>
      <div class="kv-rows">
        <div><span>Current incident</span><b>${cur}</b></div>
        <div><span>Previous incident</span><b>${esc(top.id)}${top.problem ? ` · ${esc(top.problem)}` : ""}</b></div>
        <div><span>Matching signals</span><ul class="checks">${signals.length ? signals.map((s) => `<li>${esc(s)}</li>`).join("") : "<li>Recalled by Hindsight as the same failure</li>"}</ul></div>
        <div><span>Previous successful solution</span><b>${esc(top.solution || "—")}</b></div>
        <div><span>Previous outcome</span><b>${outcomeText(top.outcome)}</b></div>
      </div>
      <p class="why-foot mem">🧠 Recommendation generated using previous incident memory.</p></div>`;
  }
  return `<div class="panel why">
    <h3>🔍 WHY THIS RECOMMENDATION?</h3>
    <div class="kv-rows">
      <div><span>Current incident</span><b>${cur}</b></div>
      <div><span>Hindsight memory</span><b>No past incident with the same failure${(a.rejected || []).length ? ` (${a.rejected.length} look-alike${a.rejected.length === 1 ? "" : "s"} rejected)` : ""}</b></div>
      <div><span>Based on</span><ul class="checks">${a.recommendation.evidence.map((e) => `<li>${esc(e)}</li>`).join("") || "<li>Current incident details</li>"}</ul></div>
    </div>
    <p class="why-foot fresh">🆕 Recommendation generated from current incident data only. No memory used.</p></div>`;
}

function failedAttempts(rec) {
  return rec.attempts.filter((a) => a.outcome === "failed").map((a) => `<div class="panel attempt-failed">
    ❌ <b>Attempt ${a.n} didn't work:</b> ${esc(a.recommendation.suggested_fix)}${a.note ? `<span class="reason">Engineer: ${esc(a.note)}</span>` : ""}
    <span class="reason">🧠 Saved to Hindsight as a failed fix, so it won't be suggested again.</span></div>`).join("");
}

function reviewPanel(rec) {
  const a = rec.attempts[rec.attempts.length - 1];
  const r = a.recommendation;
  const rootCause = r.root_cause.startsWith("Unknown") ? "" : r.root_cause;
  return `<div class="panel review" id="review">
    <h3>👤 Your review</h3>
    <p class="muted">Apply the suggested fix, then tell DejaFix what happened. Only a confirmed fix is saved to Hindsight as a solution.</p>
    <div class="actions">
      <button class="good" data-act="worked">✅ Fix worked</button>
      <button class="bad" data-act="failed">❌ Didn't work / Reject</button>
    </div>
    <form class="sub hidden" data-form="worked">
      <label>Confirmed root cause <textarea name="root_cause" rows="2">${esc(rootCause)}</textarea></label>
      <label>Solution that worked <textarea name="solution" rows="2">${esc(r.suggested_fix)}</textarea></label>
      <label>Minutes to fix <span class="opt">(optional)</span> <input name="minutes" type="number" min="1"></label>
      <button class="primary" type="submit">✅ Resolve &amp; save to Hindsight</button>
    </form>
    <form class="sub hidden" data-form="failed">
      <label>What happened? <span class="opt">(optional)</span> <input name="note" placeholder="e.g. errors came back after 5 minutes"></label>
      <button class="primary" type="submit">🔍 Save failed fix &amp; investigate again</button>
    </form>
    <p class="error" data-err></p></div>`;
}

function newMemoryPanel(rec) {
  const m = rec.resolution.new_memory;
  const didnt = m.didnt_work.length ? `<div class="row-kv"><b>Didn't work</b><span>${m.didnt_work.map(esc).join("; ")}</span></div>` : "";
  return `<div class="panel new-memory">
    <h3>🧠 Hindsight learned a new memory</h3>
    <div class="memory-card">
      <div class="row-kv"><b>Problem</b><span>${esc(m.problem)}</span></div>
      <div class="row-kv"><b>Confirmed root cause</b><span>${esc(m.root_cause)}</span></div>
      <div class="row-kv"><b>Solution</b><span>${esc(m.solution)}</span></div>
      ${didnt}
      <div class="row-kv"><b>Outcome</b><span class="ok">✅ ${esc(m.outcome)}</span></div>
    </div>
    <p class="muted">Next time a similar incident happens, DejaFix will recall this and recommend the fix straight away.</p></div>`;
}

function timeline(rec) {
  const steps = [];
  const add = (icon, label, at, detail = "", state = "done") => steps.push({ icon, label, at, detail, state });
  add("🚨", "Incident detected", rec.detected_at || rec.created_at, `${rec.input.service} · ${clip(rec.input.error, 60)}`);
  for (const a of rec.attempts) {
    const ids = (a.matched || []).map((m) => m.id);
    add("🔍", a.n === 1 ? "Investigation started" : `Investigation restarted (attempt ${a.n})`, a.started_at);
    add("🧠", "Hindsight searched", a.searched_at, a.mode === "memory" ? `Memory found: ${ids.join(", ")}` : "No relevant memory");
    add("💡", "Recommendation generated", a.recommended_at, `${a.recommendation.confidence} confidence`);
    if (a.outcome === "failed") {
      add("🔧", "Fix applied → didn't work", a.outcome_at, a.note, "bad");
      add("🧠", "Failed fix saved to Hindsight", a.failed_saved_at);
    } else if (a.outcome === "worked") {
      add("🔧", "Fix applied → worked", a.outcome_at);
    }
  }
  if (rec.status === "resolved") {
    add("✅", "Incident resolved", rec.resolved_at);
    add("🧠", "Outcome saved to Hindsight", rec.resolution?.saved_at);
  } else {
    add("⏳", "Waiting for your review", null, "", "pending");
  }
  return `<details class="panel timeline-panel" open><summary><h3>🕒 Incident timeline</h3></summary>
    <ol class="timeline">${steps.map((s) => `<li class="${s.state}"><span class="t-icon">${s.icon}</span>
      <div><b>${esc(s.label)}</b>${s.detail ? `<span class="t-detail">${esc(s.detail)}</span>` : ""}</div>
      <span class="t-time">${s.at ? esc(time(s.at)) : s.state === "pending" ? "" : "time not recorded"}</span></li>`).join("")}</ol></details>`;
}

// ---------- incident: page ----------
function renderIncident(rec) {
  const a = rec.attempts[rec.attempts.length - 1];
  const status = rec.status === "resolved" ? `<span class="badge st-resolved">✅ Resolved</span>` : `<span class="badge st-open">Investigating</span>`;
  $("#incident-view").innerHTML = `<div class="stack">
    <div class="panel inc-head">
      <div class="head"><h3>${esc(rec.id)} · ${esc(rec.input.problem)}</h3>${status}</div>
      <p class="muted">${esc(rec.input.service)} · Error: <code>${esc(rec.input.error)}</code></p>
      ${rec.input.details ? `<p class="muted">Recent changes: ${esc(rec.input.details)}</p>` : ""}
      ${modeBanner(a)}
    </div>
    ${memoryErrorPanel(a)}
    ${failedAttempts(rec)}
    ${a.mode === "memory" ? memoryUsedPanel(rec, a) : newPatternPanel(rec, a)}
    ${recommendationPanel(a)}
    ${whyPanel(rec, a)}
    ${rec.status === "resolved" ? newMemoryPanel(rec) : reviewPanel(rec)}
    ${timeline(rec)}
  </div>`;
  setHash(`incident=${rec.id}`);
  if (rec.status !== "resolved") wireReview(rec);
}

function wireReview(rec) {
  const panel = $("#review");
  const err = $("[data-err]", panel);
  const forms = { worked: $("[data-form=worked]", panel), failed: $("[data-form=failed]", panel) };
  panel.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
    Object.entries(forms).forEach(([k, f]) => f.classList.toggle("hidden", k !== b.dataset.act));
  }));

  forms.worked.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fd = new FormData(forms.worked);
    const body = { root_cause: fd.get("root_cause").trim(), solution: fd.get("solution").trim(), time_to_resolve_min: fd.get("minutes") ? Number(fd.get("minutes")) : null };
    if (body.root_cause.length < 3 || body.solution.length < 3) { err.textContent = "Please fill in the confirmed root cause and the solution."; return; }
    withBusy($("button[type=submit]", forms.worked), "Saving to Hindsight…", async () => {
      try {
        renderIncident(await api(`/api/incidents/${rec.id}/resolve`, { method: "POST", body: JSON.stringify(body) }));
        loadStatus();
        refreshActiveCount();
      } catch (e) { err.textContent = e.message; }
    });
  });

  forms.failed.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const note = new FormData(forms.failed).get("note").trim();
    withBusy($("button[type=submit]", forms.failed), "Saving & investigating again…", async () => {
      try {
        renderIncident(await api(`/api/incidents/${rec.id}/fail`, { method: "POST", body: JSON.stringify({ note }) }));
        loadStatus();
      } catch (e) { err.textContent = e.message; }
    });
  });
}

async function openIncident(id) {
  showTab("new", { keepHash: true });
  $("#incident-view").innerHTML = loading("Loading incident…");
  try { renderIncident(await api(`/api/incidents/${encodeURIComponent(id)}`)); } catch (err) { $("#incident-view").innerHTML = errorBox(err.message); }
}

// ---------- active incidents ----------
async function loadActive() {
  const box = $("#active-list");
  let items;
  try { items = await api("/api/incidents?status=investigating"); } catch (err) { box.className = ""; box.innerHTML = errorBox(err.message); return; }
  if (!items.length) { box.className = "empty"; box.innerHTML = "No active incidents 🎉<br><span class=\"muted\">Report one from the New incident tab.</span>"; return; }
  box.className = "cards";
  box.innerHTML = items.map((i) => {
    const a = i.attempts[i.attempts.length - 1];
    const top = a.matched?.[0];
    const mode = a.mode === "memory" ? `<span class="badge known">🧠 Known Problem</span>` : `<span class="badge fresh">🆕 Fresh Investigation</span>`;
    const memory = a.mode === "memory" && top
      ? `<div class="row-kv"><b>Hindsight</b><span>Recalled ${esc(top.id)}${top.similarity != null ? ` · similarity ${pct(top.similarity)}` : ""}</span></div>` : "";
    return `<button class="panel inc-card" data-id="${esc(i.id)}">
      <div class="head"><span class="inc-id">${esc(i.id)} · ${esc(i.input.service)}</span><span class="badges">${mode}<span class="badge st-open">${a.n > 1 ? `Attempt ${a.n} · ` : ""}Awaiting review</span></span></div>
      <b class="inc-problem">${esc(i.input.problem)}</b>
      <div class="row-kv"><b>Error</b><span><code>${esc(clip(i.input.error, 120))}</code></span></div>
      ${i.input.details ? `<div class="row-kv"><b>Recent changes</b><span>${esc(clip(i.input.details, 140))}</span></div>` : ""}
      ${memory}
      <div class="row-kv"><b>Suggested fix</b><span>${esc(a.recommendation.suggested_fix)}</span></div>
      <span class="muted">Detected ${esc(dateTime(i.detected_at || i.created_at))} · ${esc(a.recommendation.confidence)} confidence</span></button>`;
  }).join("");
  box.querySelectorAll("[data-id]").forEach((c) => c.addEventListener("click", () => openIncident(c.dataset.id)));
}

// ---------- past incidents ----------
async function loadPast() {
  const box = $("#past-list");
  let items;
  try { items = await api("/api/history"); } catch (err) { box.className = ""; box.innerHTML = errorBox(err.message); return; }
  if (!items.length) { box.className = "empty"; box.textContent = "No past incidents yet."; return; }
  box.className = "panel";
  box.innerHTML = `<p class="muted">Every incident here is in Hindsight memory. <span class="badge learned">learned</span> = resolved in DejaFix.</p>
    <table><thead><tr><th>Date</th><th>Incident</th><th>Problem</th><th>Root cause</th><th>Solution</th><th>Time</th></tr></thead><tbody>
    ${items.map((i) => `<tr${i.source === "dejafix" ? ` class="clickable" data-id="${esc(i.id)}"` : ""}>
      <td>${esc(day(i.date))}</td>
      <td class="nowrap">${esc(i.id)}${i.source === "dejafix" ? `<span class="badge learned">learned</span>` : ""}<div class="muted">${esc(i.service)}</div></td>
      <td>${esc(i.problem)}</td><td>${esc(i.root_cause)}</td><td>${esc(i.solution)}</td>
      <td class="nowrap">${i.minutes ? `${i.minutes} min` : "-"}</td></tr>`).join("")}
    </tbody></table>`;
  box.querySelectorAll("tr[data-id]").forEach((r) => r.addEventListener("click", () => openIncident(r.dataset.id)));
}

// ---------- memory insights ----------
function statCard(icon, label, value, sub) {
  return `<div class="panel stat"><span class="stat-label">${icon} ${label}</span><span class="stat-value">${value ?? "—"}</span><span class="muted">${sub}</span></div>`;
}

function bars(rows) {
  const max = Math.max(...rows.map((r) => r[1]), 1);
  return rows.length ? `<ul class="bars">${rows.map(([k, v]) => `<li><span>${esc(k)}</span><span class="bar"><i style="width:${(v / max) * 100}%"></i></span><b>${v}</b></li>`).join("")}</ul>` : `<p class="muted">No data yet.</p>`;
}

function progressionPanel(p) {
  if (!p) {
    return `<div class="panel progression"><h3>📈 Learning progression</h3>
      <div class="prog"><div class="prog-col"><span class="prog-title">Incident 1</span><ol><li>🆕 Unknown problem</li><li>Agent investigates</li><li>Fix confirmed</li><li>🧠 Memory created</li></ol></div>
      <i>→</i><div class="prog-col"><span class="prog-title">Incident 2</span><ol><li>🧠 Similar memory found</li><li>Previous fix recalled</li><li>Better recommendation</li></ol></div></div>
      <p class="muted">No real example yet: resolve a new incident, then report the same problem again to see it here with your own data.</p></div>`;
  }
  const f = p.first, s = p.second;
  return `<div class="panel progression"><h3>📈 Learning progression <span class="muted">real incidents from this app</span></h3>
    <div class="prog">
      <div class="prog-col"><span class="prog-title">${esc(f.id)} · ${esc(f.service)}</span><ol>
        <li>🆕 Unknown problem: <code>${esc(clip(f.error, 40))}</code></li>
        <li>Agent investigated (${esc(f.confidence)} confidence${f.attempts > 1 ? `, ${f.attempts} attempts` : ""})</li>
        <li>Fix confirmed: ${esc(clip(f.solution, 80))}${f.minutes ? ` (${f.minutes} min)` : ""}</li>
        <li>🧠 Memory created in Hindsight</li></ol></div>
      <i>→</i>
      <div class="prog-col"><span class="prog-title">${esc(s.id)}</span><ol>
        <li>🧠 Similar memory found: ${esc(f.id)}${s.similarity != null ? ` (${pct(s.similarity)})` : ""}</li>
        <li>Previous fix recalled</li>
        <li>Better recommendation: ${esc(s.confidence)} confidence<br><span class="muted">${esc(clip(s.suggested_fix, 90))}</span></li></ol></div>
    </div>
    <p class="prog-foot">The more incidents DejaFix experiences, the more useful its recommendations become.</p></div>`;
}

let reflectSections = null;

function sectionPanels(s) {
  const patterns = s.patterns.length ? `<ul class="plain">${s.patterns.map((p) => `<li><b>${p.count}×</b> ${esc(p.services.join(", "))}: ${esc(clip(p.problem, 70))}
      <span class="reason">${p.incidents.map(esc).join(" → ")}${p.fix ? ` · latest fix (${esc(p.fix_from)}): ${esc(clip(p.fix, 90))}` : ""}</span></li>`).join("")}</ul>` : `<p class="muted">No recurring pattern yet.</p>`;
  const fixes = `<ul class="plain">${s.successful_fixes.map((f) => `<li><b>${esc(f.id)}</b> ${esc(clip(f.problem, 50))}<span class="reason">✅ ${esc(clip(f.solution, 110))}${f.minutes ? ` · ${f.minutes} min` : ""}</span></li>`).join("")}</ul>`;
  const failed = (s.ineffective.length ? bars(s.ineffective) : "") +
    (s.failed_fixes.length ? `<ul class="plain small">${s.failed_fixes.slice(0, 5).map((f) => `<li><b>${esc(f.id)}</b> ✕ ${esc(clip(f.action, 100))}</li>`).join("")}</ul>` : `<p class="muted">No failed fixes recorded.</p>`);
  const preventive = reflectSections?.["preventive actions"]
    ? miniMarkdown(reflectSections["preventive actions"])
    : `<p class="muted">Run <b>Hindsight reflect</b> below to generate preventive actions from memory.</p>`;
  const card = (title, body, note = "") => `<div class="panel"><h3>${title}</h3>${note ? `<p class="muted">${note}</p>` : ""}${body}</div>`;
  return card("🔁 Recurring incident patterns", patterns, "Incidents linked by the same service + trigger, or recalled by Hindsight as the same failure.")
    + card("📊 Most affected services", bars(s.services))
    + card("⚡ Common triggers", bars(s.triggers), "As recorded in postmortems.")
    + card("✅ Successful fixes", fixes, "Most recent confirmed solutions.")
    + card("❌ Failed fixes / ineffective responses", failed)
    + card("🛡️ Preventive actions", preventive, "From Hindsight reflect.");
}

let lastSummary = null;
async function loadInsights() {
  $("#insight-cards").innerHTML = loading("Loading memory statistics…");
  try {
    const s = await api("/api/insights/summary");
    lastSummary = s;
    const c = s.cards;
    $("#insight-cards").innerHTML =
      statCard("🧠", "Total memories", c.total_memories, "facts &amp; observations in Hindsight")
      + statCard("✅", "Resolved incidents", c.resolved_incidents, "history + resolved in DejaFix")
      + statCard("🔁", "Recurring patterns", c.recurring_patterns, `${c.recognised_by_memory} incident${c.recognised_by_memory === 1 ? "" : "s"} recognised from memory`)
      + statCard("🆕", "New problems learned", c.new_problems_learned, "unknown → confirmed fix → memory");
    $("#progression").innerHTML = progressionPanel(s.progression);
    $("#insight-sections").innerHTML = sectionPanels(s);
  } catch (err) {
    $("#insight-cards").innerHTML = errorBox(err.message);
  }
}

// Hindsight reflect answers in Markdown; render headings, bullets, numbered lists, bold and code (input is escaped first).
function miniMarkdown(md) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  let html = "", list = null;
  const close = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of String(md || "").split("\n")) {
    const line = raw.trim();
    let m;
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { close(); html += `<h4>${inline(m[1])}</h4>`; }
    else if ((m = line.match(/^[-*]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
    else if ((m = line.match(/^\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
    else if (line) { close(); html += `<p>${inline(line)}</p>`; }
  }
  close();
  return html;
}

// Split the reflect answer into its "## Heading" sections.
function splitSections(md) {
  const out = {};
  let key = null;
  for (const line of String(md || "").split("\n")) {
    const m = line.match(/^#{1,3}\s+(.*)/);
    if (m) { key = m[1].replace(/[*_`]/g, "").trim().toLowerCase(); out[key] = ""; }
    else if (key) out[key] += line + "\n";
  }
  return out;
}

$("#btn-insights").addEventListener("click", (e) =>
  withBusy(e.currentTarget, "Hindsight is reflecting…", async () => {
    $("#insights-result").innerHTML = loading("Hindsight is reasoning over all memories. This takes ~15 seconds.");
    try {
      const r = await api("/api/insights");
      const src = { hindsight_reflect: "Hindsight reflect", llm_over_memory: "LLM over local memory", stats: "memory statistics" }[r.source] || r.source;
      const sections = splitSections(r.text);
      const keys = Object.keys(sections);
      reflectSections = sections;
      $("#insights-result").innerHTML = (keys.length >= 2
        ? `<div class="reflect-grid">${keys.map((k) => `<div class="reflect-card"><h4>${esc(k.replace(/^./, (c) => c.toUpperCase()))}</h4>${miniMarkdown(sections[k])}</div>`).join("")}</div>`
        : `<div class="insights-text">${miniMarkdown(r.text)}</div>`) + `<p class="muted">Source: ${src}</p>`;
      if (lastSummary) $("#insight-sections").innerHTML = sectionPanels(lastSummary);
    } catch (err) {
      $("#insights-result").innerHTML = errorBox(err.message);
    }
  })
);

// ---------- start ----------
loadStatus();
loadExamples();
refreshActiveCount();
(function route() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("incident")) openIncident(h.get("incident"));
  else if (h.get("tab")) showTab(h.get("tab"), { keepHash: true });
})();
