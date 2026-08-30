/* ReachOut local UI. No framework: the whole app is one file of plain DOM.
   Every panel is a thin view over one API route, and every number it shows
   is something the engine actually computed. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = v => Math.round((Number(v) || 0) * 100) + "%";
const gcls = g => "g-" + String(g || "").trim().replace(/\s+/g, "-");
const bar = v => `<span class="bar"><i style="width:${Math.max(0, Math.min(100, Number(v) || 0))}%"></i></span>`;
const link = u => u ? `<a href="${esc(u)}" target="_blank" rel="noreferrer noopener">${esc(u)}</a>` : "";
/* In a table the URL is provenance, not prose: show where it points, keep the
   whole thing on the link itself. */
const linkTo = (u, n = 46) => {
  if (!u) return "";
  let label = u;
  try {
    const parsed = new URL(u);
    label = parsed.host.replace(/^www\./, "") + parsed.pathname;
  } catch { /* not a URL — show it as it came */ }
  return `<a href="${esc(u)}" target="_blank" rel="noreferrer noopener"
    title="${esc(u)}">${esc(clip(label, n))}</a>`;
};
const clip = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const card = (body, cls = "") => `<div class="card ${cls}">${body}</div>`;
const note = (text, cls = "hint") => `<div class="${cls}">${esc(text)}</div>`;

async function api(path, { params, body, method } = {}) {
  const url = params ? `${path}?${new URLSearchParams(params)}` : path;
  const init = { method: method || (body ? "POST" : "GET") };
  if (body) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  const res = await fetch(url, init);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); }
  catch { throw new Error(clip(text, 200) || `${res.status} ${res.statusText}`); }
  if (!res.ok || data.error) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

/* A lookup can take half a minute. Say so, and keep counting, so nobody
   wonders whether the thing has died. */
function working(node, message) {
  const started = Date.now();
  node.innerHTML = card(`${esc(message)} <span class="tick"></span>`, "busy");
  const tick = $(".tick", node);
  const timer = setInterval(() => {
    tick.textContent = ((Date.now() - started) / 1000).toFixed(0) + "s";
  }, 500);
  return () => clearInterval(timer);
}

async function run(node, message, work, button) {
  const stop = working(node, message);
  if (button) button.disabled = true;
  try {
    return await work();
  } catch (err) {
    node.innerHTML = card(esc(err.message || err), "err");
    return null;
  } finally {
    stop();
    if (button) button.disabled = false;
  }
}

/* --------------------------------------------------------------------- tabs */
const LOADERS = {};
const TABS = $$("#tabs button").map(b => b.dataset.tab);

function go(tab) {
  if (!TABS.includes(tab)) tab = TABS[0];
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  $$(".panel").forEach(p => p.classList.toggle("on", p.id === `panel-${tab}`));
  if (location.hash.slice(1) !== tab) location.hash = tab;
  if (LOADERS[tab]) LOADERS[tab]();
}
$("#tabs").addEventListener("click", ev => {
  const button = ev.target.closest("button[data-tab]");
  if (button) go(button.dataset.tab);
});
/* A tab is a place: bookmarkable, and the back button works. */
addEventListener("hashchange", () => go(location.hash.slice(1)));

document.addEventListener("click", async ev => {
  const button = ev.target.closest("[data-copy]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    const was = button.textContent;
    button.textContent = "copied";
    setTimeout(() => { button.textContent = was; }, 1200);
  } catch { /* a browser that will not allow it; the text is on screen anyway */ }
});

/* --------------------------------------------------------------------- find */
/* Only what the signals above have not already said. A working MX is scored as a
   signal; it does not need repeating under every candidate. */
function renderChecks(c) {
  if (!c || c.syntax_ok === undefined) return "";
  const bits = [];
  if (!c.syntax_ok) bits.push("not a valid address");
  if (!c.has_mx) bits.push("no MX record — this domain cannot receive mail");
  if (c.is_role) bits.push("role mailbox, not a person");
  if (c.is_free) bits.push("consumer provider, not a company domain");
  if (c.is_disposable) bits.push("disposable domain");
  if (c.catch_all === 1) bits.push("catch-all domain: acceptance proves nothing");
  if (c.smtp && c.smtp !== "not-checked") {
    bits.push(`SMTP ${c.smtp}${c.smtp_detail ? " — " + c.smtp_detail : ""}`);
  }
  return bits.length ? `<div class="cap">${esc(bits.join(" · "))}</div>` : "";
}

function renderEmail(e) {
  const signals = (e.signals || []).filter(s => s.points !== 0).map(s =>
    `<li><span class="${s.points > 0 ? "p" : "n"}">${s.points > 0 ? "+" : "−"}</span>
      ${esc(s.label)} <b>${Math.abs(Math.round(s.points))}</b>
      ${s.source ? `<br>${link(s.source)}` : ""}</li>`).join("");
  const caps = (e.caps || []).map(c => `<div class="cap">${esc(c)}</div>`).join("");
  const flag = { observed: "found published", confirmed: "you confirmed this" }[e.status];
  return card(`
    <div class="row">
      <span class="addr">${esc(e.address)}</span>
      ${flag ? `<span class="pill">${flag}</span>` : ""}
      ${bar(e.confidence)}
      <span class="grade ${gcls(e.grade)}">${e.confidence} ${esc(e.grade)}</span>
      <button class="link" data-copy="${esc(e.address)}">copy</button>
    </div>
    <ul class="ev">${signals}</ul>${caps}${renderChecks(e.checks)}`);
}

function renderPhoneEntry(p) {
  const signals = (p.signals || []).filter(s => s.points !== 0).map(s =>
    `<li><span class="${s.points > 0 ? "p" : "n"}">${s.points > 0 ? "+" : "−"}</span>
      ${esc(s.label)} <b>${Math.abs(Math.round(s.points))}</b></li>`).join("");
  return card(`
    <div class="row">
      <span class="addr">${esc(p.pretty || p.number)}</span>
      <span class="pill">${esc(p.kind)}${p.kind_certain ? "" : " (line type unverifiable)"}</span>
      ${bar(p.confidence)}
      <span class="grade ${gcls(p.grade)}">${p.confidence} ${esc(p.grade)}</span>
      <button class="link" data-copy="${esc(p.number)}">copy</button>
    </div>
    ${p.context ? `<div class="cap">${esc(clip(p.context, 180))}</div>` : ""}
    <ul class="ev">${signals}</ul>
    ${(p.caps || []).map(c => `<div class="cap">${esc(c)}</div>`).join("")}`);
}

function renderFindPhones(phones) {
  if (!phones) return "";
  let out = `<div class="meta" style="margin-top:18px">Phone — numbers that reach this person</div>`;
  if (!phones.personal.length) {
    out += card("No public page ties a number to this person. Numbers are never inferred.", "hint");
  }
  out += phones.personal.map(renderPhoneEntry).join("");
  if (phones.company.length) {
    out += card(`<div class="meta">Company numbers — these reach the switchboard, not the person</div>`
      + phones.company.map(c => `<div class="row"><span class="addr">${esc(c.pretty)}</span>
        <span class="pill">${esc(c.kind)}</span></div>`).join(""));
  }
  return out;
}

function renderFind(d) {
  let head = `<div class="row"><b>${esc(d.name || "(unparsed name)")}</b>`;
  if (d.domain) {
    head += `<span class="pill">${esc(d.domain)}`
      + (d.domain_facts && d.domain_facts.provider ? " · " + esc(d.domain_facts.provider) : "")
      + `</span>`;
  }
  head += `</div><div class="meta">`;
  if (d.domain) {
    head += `domain ${pct(d.domain_confidence)} — ${esc((d.domain_evidence || [])[0] || "")}<br>`;
  }
  if (d.pattern) {
    const backing = d.pattern_pinned
      ? (d.pattern_samples ? `you set this; ${d.pattern_samples} real address(es) agree` : "you set this")
      : `from ${d.pattern_samples} real addresses`
        + (d.learned ? `, ${d.learned} learned just now` : "");
    head += `pattern <b>${esc(d.pattern_example)}</b> — ${pct(d.pattern_confidence)} ${esc(backing)}<br>`;
  }
  if (d.profile && d.profile.login) {
    head += `github @${esc(d.profile.login)} (${pct(d.profile.match)} match)<br>`;
  }
  head += `${d.elapsed}s</div>`;
  head += (d.warnings || []).map(w => `<div class="cap">! ${esc(w)}</div>`).join("");
  if (d.domain) {
    head += `<div class="actions">
      <button class="link" data-act="teach" data-domain="${esc(d.domain)}"
        data-name="${esc(d.name || "")}">none of these is right — teach it</button>
      <button class="link" data-act="domain" data-domain="${esc(d.domain)}">what do I know about ${esc(d.domain)}?</button>
    </div>`;
  }

  const body = d.emails.length ? d.emails.map(renderEmail).join("")
    : card("No candidates. Give the company domain directly, or teach one real address.", "err");
  $("#find-out").innerHTML = card(head) + body + renderFindPhones(d.phones);
}

$("#find-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const name = $("#find-name").value.trim();
  if (!name) return;
  const params = {
    name, company: $("#find-company").value.trim(),
    github: $("#opt-github").checked ? 1 : 0,
    web: $("#opt-web").checked ? 1 : 0,
    search: $("#opt-search").checked ? 1 : 0,
    relearn: $("#opt-relearn").checked ? 1 : 0,
    phone: $("#opt-phone").checked ? 1 : 0,
    smtp: $("#opt-smtp").checked ? 1 : 0,
  };
  const out = $("#find-out");
  const data = await run(out, "Resolving the company, learning its naming pattern, checking DNS…",
    () => api("/api/find", { params }), ev.submitter);
  if (data) { renderFind(data); loadStats(); }
});

$("#find-out").addEventListener("click", ev => {
  const button = ev.target.closest("button[data-act]");
  if (!button) return;
  if (button.dataset.act === "teach") {
    $("#ta-domain").value = button.dataset.domain;
    $("#ta-name").value = button.dataset.name;
    go("teach");
    $("#ta-value").focus();
  } else {
    go("domains");
    loadDomain(button.dataset.domain);
  }
});

/* -------------------------------------------------------------------- phone */
const PROVIDER_MARK = {
  ok: '<span class="ok">✓</span>', skipped: '<span class="dot">–</span>',
  error: '<span class="err">✗</span>', degraded: '<span class="warn">!</span>',
};

function renderCandidate(c, index) {
  const evidence = (c.evidence || []).map(e =>
    `<li>${esc(e.how)} — ${linkTo(e.source_url, 70)}${e.owner ? ` <b>${esc(e.owner)}</b>` : ""}
     ${e.context ? `<br>${esc(clip(e.context, 160))}` : ""}</li>`).join("");
  const signals = (c.signals || []).filter(s => s.points !== 0).map(s =>
    `<li><span class="${s.points > 0 ? "p" : "n"}">${s.points > 0 ? "+" : "−"}</span>
     ${esc(s.label)} <b>${Math.abs(Math.round(s.points))}</b></li>`).join("");
  return card(`
    <div class="row">
      <span class="addr">${esc(c.pretty)}</span>
      <span class="pill">${esc(c.kind)}${c.line_type_certain ? "" : " (line type unverifiable)"}</span>
      <span class="pill">${c.independent_sources} independent source(s)</span>
      ${bar(c.confidence)}
      <span class="grade ${gcls(c.grade)}">${c.confidence} ${esc(c.grade)}</span>
      <button class="link" data-copy="${esc(c.e164)}">copy</button>
    </div>
    <ul class="ev">${signals}</ul>
    ${(c.caps || []).map(x => `<div class="cap">${esc(x)}</div>`).join("")}
    ${(c.contradictions || []).map(x => `<div class="cap warn">! ${esc(x)}</div>`).join("")}
    <details><summary>where it was seen</summary><ul class="ev">${evidence}</ul></details>`,
    index === 0 ? "" : "sunk");
}

function renderLookup(d) {
  const s = d.subject || {};
  const label = String(d.status || "").replace(/_/g, " ").toUpperCase();
  let head = `<div class="row"><b>${esc(s.name || "")}</b>`;
  if (s.work_email) head += `<span class="pill">${esc(s.work_email)}</span>`;
  else if (s.domain) head += `<span class="pill">${esc(s.domain)}</span>`;
  head += `</div><div class="meta"><span class="status s-${esc(d.status)}">${esc(label)}</span>
    — ${esc(d.reason)}</div>`;
  // A blocked source is stated once, in its own block below - not twice.
  const blind = new Set((d.blind_spots || []).map(b => b.provider));
  head += (d.warnings || [])
    .filter(w => !blind.has(String(w).split(" ")[0]))
    .map(w => `<div class="cap">! ${esc(w)}</div>`).join("");
  head += `<div class="hint">${esc(d.coverage)} · ${d.checked} sighting(s) considered`
    + (d.dropped_untraceable ? ` · ${d.dropped_untraceable} dropped for having no source URL` : "")
    + ` · ${d.elapsed}s</div>`;

  const blindCard = (d.blind_spots || []).length
    ? card(`<div class="meta">Sources that could not do their job</div>`
      + (d.blind_spots || []).map(b =>
        `<div class="hint">${esc(b.provider)} — ${esc(b.reason)}</div>`).join("")
      + note("A lookup where sources were blocked has not established that nothing is "
        + "published, only that it could not find out."))
    : "";

  const providers = card(`<div class="meta">Sources consulted</div><div class="scroll"><table>
    <tr><th></th><th>source</th><th>kind</th><th class="num">found</th><th>detail</th></tr>
    ${(d.providers || []).map(p => `<tr>
      <td>${PROVIDER_MARK[p.state] || ""}</td>
      <td>${esc(p.provider)}</td><td>${esc(p.kind)}</td>
      <td class="num">${p.found}</td>
      <td>${esc(clip(p.skipped || p.error || p.degraded || `${p.elapsed}s`, 90))}</td>
    </tr>`).join("")}</table></div>`);

  const candidates = (d.candidates || []).length
    ? (d.candidates || []).map(renderCandidate).join("")
    : card("Nothing published anywhere this tool can read. Phone numbers are never "
      + "inferred or generated — only reported when already public.", "hint");

  const company = (d.company_numbers || []).length
    ? card(`<div class="meta">Company numbers — these reach the switchboard, not the person</div>`
      + d.company_numbers.map(c => `<div class="row"><span class="addr">${esc(c.pretty)}</span>
        <span class="pill">${esc(c.kind)}</span>${linkTo(c.source_url)}</div>`).join(""))
    : "";

  $("#phone-out").innerHTML = card(head) + candidates + company + blindCard + providers;
}

$("#phone-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const name = $("#ph-name").value.trim();
  if (!name) return;
  const body = {
    name, company: $("#ph-company").value.trim(), email: $("#ph-email").value.trim(),
    region: $("#ph-region").value.trim(), search: $("#ph-search").checked,
    pages: Number($("#ph-pages").value) || 6,
  };
  const data = await run($("#phone-out"), "Reading the company site, staff directories and "
    + "public documents…", () => api("/api/enrich", { body }), ev.submitter);
  if (data) { renderLookup(data); loadStats(); }
});

$("#checkphone-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const params = { number: $("#cp-number").value.trim(), region: $("#cp-region").value.trim() };
  if (!params.number) return;
  const d = await run($("#cp-out"), "Checking…", () => api("/api/checkphone", { params }),
    ev.submitter);
  if (!d) return;
  if (!d.valid) {
    $("#cp-out").innerHTML = card(`<b>${esc(d.input)}</b> is not a valid number`
      + (d.notes || []).map(n => `<div class="hint">${esc(n)}</div>`).join(""), "err");
    return;
  }
  const rows = [["E.164", d.e164], ["country", `${d.country} (+${d.calling_code})`],
    ["line type", d.kind_certain ? d.kind : `${d.kind} — cannot be determined here`],
    ["area code", d.area], ["extension", d.extension]];
  $("#cp-out").innerHTML = card(`<div class="row"><span class="addr">${esc(d.pretty)}</span></div>
    <table>${rows.filter(r => r[1]).map(([k, v]) =>
      `<tr><td>${esc(k)}</td><td class="mono">${esc(v)}</td></tr>`).join("")}</table>
    ${d.placeholder ? `<div class="cap warn">looks like a placeholder: ${esc(d.placeholder)}</div>` : ""}
    ${(d.notes || []).map(n => `<div class="hint">${esc(n)}</div>`).join("")}`);
});

/* -------------------------------------------------------------------- teach */
function patternTable(rows) {
  if (!rows || !rows.length) return "";
  return `<table>${rows.slice(0, 6).map(r => `<tr>
    <td class="mono">${esc(r.example)}</td>
    <td>${bar(Math.round(r.confidence * 100))}</td>
    <td class="num">${pct(r.confidence)}</td>
    <td class="hint">${r.pinned ? "you set this" : `${r.samples} samples`}</td>
  </tr>`).join("")}</table>`;
}

$("#teach-addr").addEventListener("submit", async ev => {
  ev.preventDefault();
  const body = {
    mode: "address", domain: $("#ta-domain").value.trim(),
    name: $("#ta-name").value.trim(), value: $("#ta-value").value.trim(),
  };
  const d = await run($("#ta-out"), "Recording…", () => api("/api/teach", { body }), ev.submitter);
  if (!d) return;
  if (d.kind === "phone") {
    $("#ta-out").innerHTML = d.recorded
      ? card(`<b>${esc(d.pretty)}</b> recorded as ${esc(d.name)}'s number at ${esc(d.domain)}
        <div class="hint">${esc(d.country)} ${esc(d.line)}${d.line_certain ? "" : " (line type not determinable)"}</div>`)
      : card(`<b>${esc(d.pretty)}</b> looks like a placeholder: ${esc(d.refused)}. Not recorded.`, "err");
  } else {
    $("#ta-out").innerHTML = card(
      `<div class="row"><span class="addr">${esc(d.address)}</span>
        <span class="pill">${d.banked ? "banked as real" : "already known"}</span></div>`
      + (d.error ? `<div class="cap warn">${esc(d.error)}</div>` : "")
      + (d.example ? `<div class="hint">it follows <b>${esc(d.example)}</b></div>` : "")
      + patternTable(d.patterns));
  }
  loadStats();
  invalidateDomains();
});

$("#tp-template").addEventListener("input", () => {
  const t = $("#tp-template").value.trim();
  $("#tp-preview").textContent = t ? `  →  ${t.replace(/\{first\}/g, "jane")
    .replace(/\{last\}/g, "doe").replace(/\{middle\}/g, "ann")
    .replace(/\{f\}/g, "j").replace(/\{l\}/g, "d").replace(/\{m\}/g, "a")}@…` : "";
});

$("#teach-pattern").addEventListener("submit", async ev => {
  ev.preventDefault();
  const body = {
    mode: "pattern", domain: $("#tp-domain").value.trim(),
    template: $("#tp-template").value.trim(),
    confidence: (Number($("#tp-confidence").value) || 90) / 100,
  };
  const d = await run($("#tp-out"), "Pinning…", () => api("/api/teach", { body }), ev.submitter);
  if (!d) return;
  $("#tp-out").innerHTML = card(
    `<div class="row"><b>${esc(d.domain)}</b> → <span class="addr">${esc(d.example)}</span></div>
     <div class="hint">e.g. <span class="mono">${esc(d.probe)}</span> — pinned at ${pct(d.confidence)};
     harvesting will not overwrite it</div>`);
  loadStats();
  invalidateDomains();
});

$("#teach-bulk").addEventListener("submit", async ev => {
  ev.preventDefault();
  const file = $("#tb-file").files[0];
  const text = file ? await file.text() : $("#tb-text").value;
  if (!text.trim()) {
    $("#tb-out").innerHTML = card("Choose a CSV file, or paste some rows.", "err");
    return;
  }
  const d = await run($("#tb-out"), "Importing — this teaches one company per address…",
    () => api("/api/teach", { body: { mode: "bulk", text } }), ev.submitter);
  if (!d) return;
  $("#tb-out").innerHTML = card(
    `<b>Imported ${d.contacts} contacts</b> across ${d.domains} companies
     <div class="hint">${d.banked} addresses banked, ${d.taught} taught a format`
    + (d.skipped ? `, ${d.skipped} consumer addresses skipped` : "") + `</div>`
    + (d.learned.length ? `<div class="meta" style="margin-top:12px">Companies you can now
        guess confidently</div><table>${d.learned.slice(0, 25).map(r => `<tr>
        <td>${esc(r.domain)}</td><td class="mono">${esc(patternExample(r.template))}</td>
        <td class="num">${pct(r.confidence)}</td></tr>`).join("")}</table>` : ""));
  loadStats();
  invalidateDomains();
});

/* The server sends templates; rendering the {first}.{last} form is display-only. */
function patternExample(template) {
  return String(template || "").replace(/\{first\}/g, "john").replace(/\{last\}/g, "smith")
    .replace(/\{middle\}/g, "q").replace(/\{f\}/g, "j").replace(/\{l\}/g, "s")
    .replace(/\{m\}/g, "q");
}

$("#teach-forget").addEventListener("submit", async ev => {
  ev.preventDefault();
  const domain = $("#tf-domain").value.trim();
  if (!domain) return;
  const d = await run($("#tf-out"), "Removing…",
    () => api("/api/teach", { body: { mode: "unpin", domain } }), ev.submitter);
  if (!d) return;
  $("#tf-out").innerHTML = card(`Removed ${d.removed} stated pattern(s) for
    <b>${esc(d.domain)}</b>. Harvested samples are untouched.`, d.removed ? "" : "hint");
  invalidateDomains();
});

/* ------------------------------------------------------------------ domains */
let domainsLoaded = false;
function invalidateDomains() { domainsLoaded = false; }

LOADERS.domains = () => { if (!domainsLoaded) loadDomains(); };

async function loadDomains() {
  const out = $("#domains-list");
  const rows = await run(out, "Reading the database…",
    () => api("/api/domains", { params: { q: $("#dm-filter").value.trim() } }));
  if (!rows) return;
  domainsLoaded = true;
  if (!rows.length) {
    out.innerHTML = card("Nothing learned yet. Run a lookup, or teach one address.", "hint");
    return;
  }
  out.innerHTML = card(`<div class="meta">${rows.length} domain(s) — click one</div>
    <div class="scroll"><table>
    <tr><th>domain</th><th>company</th><th>pattern</th><th class="num">confidence</th>
        <th class="num">samples</th><th class="num">banked</th></tr>
    ${rows.map(r => `<tr class="clickable" data-domain="${esc(r.domain)}">
      <td class="mono">${esc(r.domain)}${r.pinned ? ' <span class="pill">stated</span>' : ""}</td>
      <td>${esc(clip(r.company, 28))}</td>
      <td class="mono">${esc(r.example)}</td>
      <td class="num">${r.template ? pct(r.confidence) : ""}</td>
      <td class="num">${r.samples}</td>
      <td class="num">${r.banked_emails}${r.banked_phones ? ` + ${r.banked_phones}☎` : ""}</td>
    </tr>`).join("")}</table></div>`);
}

$("#domains-filter").addEventListener("submit", ev => { ev.preventDefault(); loadDomains(); });
$("#domains-list").addEventListener("click", ev => {
  const row = ev.target.closest("tr[data-domain]");
  if (row) loadDomain(row.dataset.domain);
});

async function loadDomain(domain) {
  const out = $("#domain-detail");
  out.scrollIntoView({ behavior: "smooth", block: "nearest" });
  const d = await run(out, `Reading ${domain}…`,
    () => api("/api/domain", { params: { domain } }));
  if (!d) return;

  const facts = d.facts && Object.keys(d.facts).length ? `<div class="hint">
    ${d.facts.has_mx ? "accepts mail" : "no MX record"}
    ${d.facts.provider ? " · " + esc(d.facts.provider) : ""}
    ${d.facts.catch_all === 1 ? " · catch-all" : ""}</div>` : "";

  const samples = d.samples.length ? `<details><summary>${d.samples.length} pattern sample(s)
    — the real addresses everything here is inferred from</summary>
    <div class="scroll"><table>
    <tr><th>name</th><th>local part</th><th>template</th><th>source</th></tr>
    ${d.samples.slice(0, 200).map(s => `<tr>
      <td>${esc(s.first)} ${esc(s.last)}</td><td class="mono">${esc(s.local)}</td>
      <td class="mono">${esc(s.template)}</td>
      <td>${s.source_url === "you told me" ? "you told me" : linkTo(s.source_url)}</td>
    </tr>`).join("")}</table></div></details>` : "";

  const emails = d.emails.length ? `<details><summary>${d.emails.length} address(es) seen
    published at this domain</summary><div class="scroll"><table>
    ${d.emails.slice(0, 200).map(e => `<tr>
      <td class="mono">${esc(e.address)}${e.is_role ? ' <span class="pill">role</span>' : ""}</td>
      <td>${esc(clip(e.context, 40))}</td><td>${linkTo(e.source_url)}</td></tr>`).join("")}
    </table></div></details>` : "";

  const phones = d.phones.length ? `<details><summary>${d.phones.length} number(s) banked
    </summary><div class="scroll"><table>
    ${d.phones.map(p => `<tr><td class="mono">${esc(p.e164)}</td><td>${esc(p.kind)}</td>
      <td>${esc(p.scope)}</td><td>${esc(clip(p.owner, 24))}</td>
      <td>${linkTo(p.source_url)}</td></tr>`).join("")}</table></div></details>` : "";

  out.innerHTML = card(`
    <div class="row"><b class="mono">${esc(d.domain)}</b></div>${facts}
    ${d.patterns.length ? patternTable(d.patterns)
      : note("No pattern inferred yet. Harvest it, or state the format on the Teach tab.")}
    <div class="actions">
      <button class="ghost" data-act="learn" data-domain="${esc(d.domain)}">Harvest now</button>
      <button class="link" data-act="pin" data-domain="${esc(d.domain)}">state the format</button>
      <button class="link" data-act="drop" data-domain="${esc(d.domain)}">forget this domain</button>
    </div>
    ${samples}${emails}${phones}`)
    + card(`<div class="meta">Guess an address here — offline, instant, no network</div>
      <form id="guess-form" class="inline">
        <input type="text" id="gs-name" placeholder="Full name">
        <input type="hidden" id="gs-domain" value="${esc(d.domain)}">
        <button type="submit" class="ghost">Guess</button>
      </form><div id="gs-out"></div>`);
}

$("#domain-detail").addEventListener("submit", async ev => {
  if (ev.target.id !== "guess-form") return;
  ev.preventDefault();
  const params = { name: $("#gs-name").value.trim(), domain: $("#gs-domain").value };
  if (!params.name) return;
  const d = await run($("#gs-out"), "Applying what you know…",
    () => api("/api/guess", { params }), ev.submitter);
  if (!d) return;
  $("#gs-out").innerHTML = (d.known ? "" : note("No pattern known for this domain — these are "
    + "global format priors, not evidence about this company."))
    + `<table>${d.candidates.map(c => `<tr>
      <td class="mono">${esc(c.address)}</td><td class="num">${pct(c.prior)}</td>
      <td class="hint">${esc(c.source)}</td>
      <td><button class="link" data-copy="${esc(c.address)}">copy</button></td>
    </tr>`).join("")}</table>`;
});

$("#domain-detail").addEventListener("click", async ev => {
  const button = ev.target.closest("button[data-act]");
  if (!button) return;
  const domain = button.dataset.domain;
  if (button.dataset.act === "pin") {
    $("#tp-domain").value = domain;
    go("teach");
    $("#tp-template").focus();
  } else if (button.dataset.act === "drop") {
    $("#fg-kind").value = "domain";
    $("#fg-value").value = domain;
    go("data");
    $("#forget-form").requestSubmit();
  } else {
    const out = $("#domain-detail");
    const d = await run(out, `Harvesting ${domain} — GitHub commits, the company site, `
      + `web search…`, () => api("/api/learn", { body: { domain } }), button);
    if (!d) return;
    out.innerHTML = card(`<b class="mono">${esc(d.domain)}</b> +${d.learned} new sample(s)
      ${(d.sources || []).map(s => `<div class="hint">from ${esc(s)}</div>`).join("")}
      ${(d.errors || []).map(e => `<div class="cap">! ${esc(e)}</div>`).join("")}
      ${d.patterns.length ? patternTable(d.patterns)
        : note("No pattern could be inferred yet.")}`);
    invalidateDomains();
    loadStats();
    loadDomain(domain);
  }
});

/* ----------------------------------------------------------------- database */
const STAT_LABELS = [
  ["people", "people"], ["companies", "companies"], ["emails", "email candidates"],
  ["emails_observed", "found published"], ["pattern_samples", "pattern samples"],
  ["pattern_domains", "domains mapped"], ["domain_emails", "addresses banked"],
  ["phones", "phone numbers"], ["phone_lookups_found", "phones found"],
  ["observations", "evidence records"], ["searches", "searches run"],
];

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("#dbchip").textContent = `${s.db_size_kb} KB`;
    $("#dbchip").title = s.db_path;
    $("#dbpath").textContent = `Everything is stored locally in ${s.db_path}`;
    $("#stats").innerHTML = STAT_LABELS.map(([key, label]) =>
      `<div class="stat"><b>${s[key]}</b><span>${esc(label)}</span></div>`).join("");
  } catch { /* the panel simply stays as it was */ }
}

LOADERS.data = () => { loadStats(); if (!$("#people-out").innerHTML) loadPeople(); };

async function loadPeople() {
  const out = $("#people-out");
  const rows = await run(out, "Reading…",
    () => api("/api/people", { params: { q: $("#pp-q").value.trim() } }));
  if (!rows) return;
  if (!rows.length) {
    out.innerHTML = note("No people stored yet.");
    return;
  }
  out.innerHTML = `<div class="scroll"><table>
    <tr><th>name</th><th>company</th><th>best address</th><th class="num">score</th>
        <th>status</th><th class="num">candidates</th></tr>
    ${rows.map(r => `<tr>
      <td>${esc(r.name_raw)}</td><td>${esc(clip(r.company, 24))}</td>
      <td class="mono">${esc(r.address || "")}</td>
      <td class="num ${gcls(r.grade)}">${r.confidence ?? ""}</td>
      <td class="hint">${esc(r.status || "")}${r.phones ? ` · ${r.phones}☎` : ""}</td>
      <td class="num">${r.candidates}</td>
    </tr>`).join("")}</table></div>`;
}

$("#people-form").addEventListener("submit", ev => { ev.preventDefault(); loadPeople(); });

$$("#export-form button[data-format]").forEach(button => {
  button.addEventListener("click", () => {
    const min = Number($("#ex-min").value) || 0;
    window.location.href = `/api/export?format=${button.dataset.format}&min=${min}`;
  });
});

$("#forget-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const body = { kind: $("#fg-kind").value, value: $("#fg-value").value.trim() };
  if (!body.value) return;
  const d = await run($("#forget-out"), "Checking what matches…",
    () => api("/api/forget", { body }), ev.submitter);
  if (d) renderForget(d);
});

function renderForget(d) {
  if (d.confirmed) {
    $("#forget-out").innerHTML = card(`Removed ${d.removed} row(s) permanently.`, "ok");
    loadStats();
    invalidateDomains();
    return;
  }
  if (!d.total) {
    $("#forget-out").innerHTML = note(`Nothing matches ${d.value} — nothing to remove.`);
    return;
  }
  $("#forget-out").innerHTML = card(`<div class="meta">Would remove ${d.total} record(s)</div>`
    + Object.entries(d.found).map(([table, rows]) => `<div class="hint"><b>${esc(table)}</b>
      (${rows.length})<br>${rows.slice(0, 10).map(esc).join("<br>")}
      ${rows.length > 10 ? `<br>… and ${rows.length - 10} more` : ""}</div>`).join("")
    + `<div class="actions">
        <button class="ghost" id="fg-confirm" data-kind="${esc(d.kind)}"
          data-value="${esc(d.value)}">Delete permanently</button>
       </div>`, "danger");
}

$("#forget-out").addEventListener("click", async ev => {
  const button = ev.target.closest("#fg-confirm");
  if (!button) return;
  const body = { kind: button.dataset.kind, value: button.dataset.value, confirm: true };
  const d = await run($("#forget-out"), "Deleting…", () => api("/api/forget", { body }), button);
  if (d) renderForget(d);
});

/* ------------------------------------------------------------------- health */
LOADERS.health = () => { if (!$("#providers-out").innerHTML) loadProviders(); };

async function loadProviders() {
  const rows = await run($("#providers-out"), "Reading the registry…",
    () => api("/api/providers"));
  if (!rows) return;
  $("#providers-out").innerHTML = `<div class="scroll"><table>
    <tr><th>provider</th><th>kind</th><th>state</th><th>basis</th></tr>
    ${rows.map(r => `<tr>
      <td>${esc(r.name)}</td><td class="hint">${esc(r.kind)}</td>
      <td class="${r.ready ? "ok" : "warn"}">${r.ready ? "ready" : "not configured"}</td>
      <td class="hint">${esc(clip(r.basis, 70))}${r.ready ? "" : `<br>${esc(clip(r.note, 70))}`}</td>
    </tr>`).join("")}</table></div>
    ${note("A provider must state the basis on which its data may be used, and give a URL for "
      + "every number it returns.")}`;
}

$("#doctor-run").addEventListener("click", async ev => {
  const out = $("#doctor-out");
  const d = await run(out, "Probing search backends and the GitHub rate limit…",
    () => api("/api/doctor"), ev.target);
  if (!d) return;
  const gh = d.github;
  out.innerHTML = `<table>
    <tr><td>database</td><td class="mono">${esc(d.db_path)}</td></tr>
    <tr><td>cache</td><td class="mono">${esc(d.cache_dir)}</td></tr>
    ${d.key_file ? `<tr><td>keys file</td><td class="mono">${esc(d.key_file)}</td></tr>` : ""}
    <tr><td>robots.txt</td><td class="${d.respect_robots ? "" : "warn"}">
      ${d.respect_robots ? "respected" : "IGNORED"}</td></tr>
    <tr><td>per-host delay</td><td>${d.host_delay}s</td></tr>
    <tr><td>DNS</td><td class="${d.dns_ok ? "ok" : "err"}">${d.dns_ok ? "working" : "failing"}</td></tr>
    <tr><td>SMTP probing</td><td>${d.smtp_enabled ? "enabled" : "disabled (default)"}</td></tr>
    <tr><td>GitHub</td><td class="${gh.error ? "err" : (gh.authenticated ? "ok" : "warn")}">
      ${gh.error ? esc(gh.error)
        : `${gh.authenticated ? "authenticated" : "anonymous"} — ${gh.remaining}/${gh.limit} left this hour`}</td></tr>
  </table>
  <div class="meta" style="margin-top:14px">Search backends — tried in order</div>
  <table>${d.search_backends.map(b => `<tr>
    <td>${b.configured ? (b.working ? '<span class="ok">✓</span>' : '<span class="err">✗</span>')
      : '<span class="dot">–</span>'}</td>
    <td>${esc(b.name)}</td><td class="hint">${esc(clip(b.detail, 80))}</td></tr>`).join("")}</table>
  ${d.any_search ? "" : card("No working search backend — staff directories, public documents "
    + "and public pages cannot find anything without one.", "err")}
  ${gh.authenticated ? "" : note("Set GITHUB_TOKEN for 5,000 requests an hour instead of 60. "
    + "It needs no scopes at all, and it is the single highest-value thing you can do here.")}`;
});

$("#eval-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const out = $("#eval-out");
  const d = await run(out, "Re-deriving every known address without itself…",
    () => api("/api/eval", { params: { min: Number($("#ev-min").value) || 3 } }), ev.submitter);
  if (!d) return;
  if (!d.rows.length) {
    out.innerHTML = card(`Not enough samples yet — a domain needs at least ${d.min_samples}
      real addresses before this can say anything. Harvest a domain, or import contacts.`, "hint");
    return;
  }
  const misses = $("#ev-misses").checked;
  out.innerHTML = `<div class="scroll"><table>
    <tr><th>domain</th><th class="num">n</th><th class="num">top-1</th><th class="num">top-3</th></tr>
    ${d.rows.map(r => `<tr>
      <td class="mono">${esc(r.domain)}</td><td class="num">${r.trials}</td>
      <td class="num ${r.top1_rate >= 0.7 ? "ok" : (r.top1_rate >= 0.4 ? "warn" : "err")}">
        ${pct(r.top1_rate)}</td>
      <td class="num">${pct(r.top3_rate)}</td></tr>`).join("")}
    </table></div>
    <div class="hint">${d.totals.domains} domains · ${d.totals.trials} trials ·
      top-1 ${pct(d.totals.top1_rate)} · top-3 ${pct(d.totals.top3_rate)}</div>
    ${misses ? `<div class="meta" style="margin-top:12px">Misses</div><div class="scroll"><table>
      ${d.rows.flatMap(r => r.missed.map(m => `<tr><td>${esc(m.person)}</td>
        <td class="mono">${esc(m.actual)}</td><td class="mono hint">guessed ${esc(m.guessed)}</td>
      </tr>`)).join("")}</table></div>` : ""}`;
});

/* --------------------------------------------------------------------- boot */
go(location.hash.slice(1) || "find");
loadStats();
