// Verify-Before-Wire web app. Routes: #/checks, #/case/<id>, #/setup, #/policy, #/audit, #/new
// No inline style attributes: the CSP blocks them. Use classes, or el.style from script.
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (ccy, n) => (n == null ? "—" : `${ccy || ""} ${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`.trim());
const label = (d) => (d === "CALLBACK" ? "Call back" : d === "HOLD" ? "Hold" : d === "RELEASE" ? "Release" : d);
const STATE = { investigating: "Checking", awaiting_decision: "Needs a decision", awaiting_second_approval: "Needs a second approver",
  released: "Released", held: "On hold", rejected: "Marked as fraud" };
const SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
const CHECK_NAME = { vendor: "Vendor record", domain: "Sender & domain", history: "Payment history", sanctions: "Sanctions",
  language: "Language", guard: "Input guard" };
const DEC_TEXT = { RELEASE: "Release", CALLBACK: "Call back", HOLD: "Hold" };
// The CSP blocks style attributes in markup, so bar widths/positions travel as data-w / data-left
// and are applied from script (allowed). Starts at 0 so the bars animate in.
function paintBars(root) {
  const els = root.querySelectorAll("[data-w],[data-left]");
  els.forEach((e) => { if (e.dataset.w) e.style.width = "0%"; });
  requestAnimationFrame(() => requestAnimationFrame(() => els.forEach((e) => {
    if (e.dataset.w) e.style.width = `${e.dataset.w}%`;
    if (e.dataset.left) e.style.left = `${e.dataset.left}%`;
  })));
}
const when = (iso) => { const d = new Date(iso);
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };
const view = $("#view");
const S = { cfg: null, me: null, tid: localStorage.getItem("vbw.tid"), ws: null, fb: null, replay: false, can: () => false };

// ------------------------------------------------------------------ api --
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.cfg.auth_mode === "dev") headers["X-Dev-User"] = sessionStorage.getItem("vbw.dev") || "";
  else if (S.fb?.currentUser) headers.Authorization = `Bearer ${await S.fb.currentUser.getIdToken()}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (opts.raw) return res;
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const d = body?.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${x.field}: ${x.message}`).join("; ") : d || `Request failed (${res.status}).`);
  }
  return body;
}
const T = (p) => `/api/t/${S.tid}${p}`;
const show = (el, text, kind = "err") => { el.className = `msg ${kind === true ? "ok" : kind}`; el.textContent = text; };
// Disable a button and show progress while fn runs, so slow steps never look frozen.
async function busy(btn, text, fn) {
  const old = btn.innerHTML; btn.disabled = true; btn.classList.add("busy"); btn.innerHTML = `<span class="spinner sm"></span>${esc(text)}`;
  try { return await fn(); } finally { if (btn.isConnected) { btn.disabled = false; btn.classList.remove("busy"); btn.innerHTML = old; } }
}

// ----------------------------------------------------------------- auth --
async function initAuth() {
  S.cfg = await (await fetch("/api/config")).json();
  if (S.cfg.auth_mode === "dev") return;
  const v = "10.14.1";
  const { initializeApp } = await import(`https://www.gstatic.com/firebasejs/${v}/firebase-app.js`);
  S.fbmod = await import(`https://www.gstatic.com/firebasejs/${v}/firebase-auth.js`);
  S.fb = S.fbmod.getAuth(initializeApp(S.cfg.firebase));
  await new Promise((r) => S.fbmod.onAuthStateChanged(S.fb, () => r(), () => r()));
}
const signedIn = () => (S.cfg.auth_mode === "dev" ? !!sessionStorage.getItem("vbw.dev") : !!S.fb?.currentUser);

function renderSignIn() {
  $("#side").hidden = true;
  const dev = S.cfg.auth_mode === "dev";
  view.innerHTML = `<section class="panel center-card">
    <div class="brand spaced"><span class="mark" aria-hidden="true"></span>Verify-Before-Wire</div>
    <h1>Sign in</h1><p class="muted">Use your work account. Signing in for the first time creates your account.</p>
    ${dev ? `<label>Work email<input id="dev-email" type="email" autocomplete="email" placeholder="you@company.com"></label>
      <button class="btn primary block" id="go">Continue</button>
      <p class="muted small note">Development sign-in. Production uses your company's single sign-on.</p>`
    : `<button class="btn primary block" id="go">Continue with Google</button>
      <p class="muted small note">Your administrator can also turn on SAML or OIDC single sign-on.</p>`}
    <p class="msg" id="err"></p></section>`;
  const go = async () => {
    if (dev) {
      const e = $("#dev-email").value.trim();
      if (!e.includes("@")) return show($("#err"), "Enter your work email.");
      sessionStorage.setItem("vbw.dev", e); return boot();
    }
    try { await S.fbmod.signInWithPopup(S.fb, new S.fbmod.GoogleAuthProvider()); boot(); } catch (e) { show($("#err"), e.message); }
  };
  $("#go").onclick = go;
  if (dev) $("#dev-email").addEventListener("keydown", (e) => e.key === "Enter" && go());
}
$("#signout").onclick = async () => {
  sessionStorage.removeItem("vbw.dev"); if (S.fb) await S.fbmod.signOut(S.fb);
  localStorage.removeItem("vbw.tid"); S.tid = null; location.hash = ""; boot();
};

// ----------------------------------------------------------------- boot --
async function boot() {
  if (!S.cfg) await initAuth();
  if (!signedIn()) return renderSignIn();
  const invite = location.hash.match(/invite=([\w-]+)/)?.[1];
  if (invite) {
    try { const r = await api("/api/invites/accept", { method: "POST", json: { token: invite } }); S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid); }
    catch (e) { alert(e.message); }
    history.replaceState(null, "", "#/checks");
  }
  S.me = await api("/api/me");
  $("#who").textContent = S.me.email;
  if (!S.me.workspaces.length) { S.tid = null; $("#side").hidden = true; location.hash = "#/new"; return route(); }
  if (!S.me.workspaces.some((w) => w.tenant_id === S.tid)) S.tid = S.me.workspaces[0].tenant_id;
  localStorage.setItem("vbw.tid", S.tid);
  const sw = $("#ws-switch");
  sw.innerHTML = S.me.workspaces.map((w) => `<option value="${w.tenant_id}" ${w.tenant_id === S.tid ? "selected" : ""}>${esc(w.name)}</option>`).join("") + `<option value="__new">New workspace…</option>`;
  sw.onchange = () => { if (sw.value === "__new") location.hash = "#/new"; else { S.tid = sw.value; localStorage.setItem("vbw.tid", S.tid); route(); } };
  $("#side").hidden = false;
  route();
}
window.addEventListener("hashchange", () => { if (!signedIn()) return; if (/invite=/.test(location.hash)) return boot(); route(); });
document.addEventListener("keydown", (e) => {
  if (e.shiftKey && e.key.toLowerCase() === "r" && !e.target.closest("input,textarea,select")) { S.replay = !S.replay; $("#mode").textContent = S.replay ? "Recorded playback is on" : ""; }
});
document.addEventListener("focusin", (e) => { if (e.target.matches("input[readonly]")) e.target.select(); });

async function route() {
  const [page, arg] = (location.hash.replace(/^#\/?/, "") || "checks").split("/");
  try {
    if (page === "new") return renderNewWorkspace();
    if (!S.tid) { location.hash = "#/new"; return; }
    S.ws = await api(T(""));
    S.can = (p) => S.ws.my_permissions.includes(p);
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.page === (page === "case" ? "checks" : page)));
    window.scrollTo(0, 0);
    if (page === "setup") return await renderSetup();
    if (page === "policy") return await renderPolicy();
    if (page === "audit") return await renderAudit();
    if (page === "case") return await renderCase(arg);
    return await renderChecks();
  } catch (e) { view.innerHTML = `<div class="panel"><p class="msg err">${esc(e.message)}</p></div>`; }
}

// ------------------------------------------------------- new workspace --
async function renderNewWorkspace() {
  const templates = await (await fetch("/api/templates")).json();
  view.innerHTML = `<section class="panel center-card wide">
    <h1>Set up your workspace</h1><p class="muted">About five minutes. You can change all of this later.</p>
    <label>Company name<input id="n" placeholder="Acme Holdings Pte Ltd"></label>
    <label>Your company's email domains <span class="hint">Separate with commas. Used to spot lookalike domains.</span><input id="d" placeholder="acme.com, acme.com.sg"></label>
    <div class="row gap-b"><label>Base currency<input id="c" value="USD" maxlength="3"></label>
      <label>Data region<select id="r"><option value="asia-southeast1">Singapore</option><option value="asia-south1">Mumbai</option><option value="asia-northeast1">Tokyo</option><option value="europe-west1">Belgium</option><option value="us-central1">Iowa</option></select></label></div>
    <label>Time zone<input id="tz" value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}"></label>
    <fieldset><legend>Starting policy</legend><div class="choices">${templates.map((t, i) => `<label class="choice"><input type="radio" name="tpl" value="${t.id}" ${i === 0 ? "checked" : ""}><span><b>${esc(t.name)}</b><span>${esc(t.summary)}</span></span></label>`).join("")}</div></fieldset>
    <button class="btn primary block" id="go">Create workspace</button><p class="msg" id="err"></p></section>`;
  $("#go").onclick = () => busy($("#go"), "Creating…", async () => {
    try {
      const r = await api("/api/workspaces", { method: "POST", json: { name: $("#n").value, company_domains: $("#d").value.split(",").map((x) => x.trim()).filter(Boolean),
        base_currency: $("#c").value.toUpperCase(), timezone: $("#tz").value, region: $("#r").value, template: $("input[name=tpl]:checked").value } });
      S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid); history.replaceState(null, "", "#/setup"); boot();
    } catch (e) { show($("#err"), e.message); }
  });
}

// ---------------------------------------------------------------- setup --
async function renderSetup() {
  const ob = await api(T("/onboarding"));
  const step = (id) => ob.steps.find((s) => s.id === id)?.done;
  view.innerHTML = `<div class="page-head"><div><h1>Setup</h1><p id="progtext"></p></div></div>
  <div class="progressbar"><span id="bar"></span></div>
  <div class="step done"><div class="n">1</div><div class="panel"><h2>Workspace and policy</h2><p class="muted flush">Created with your starting policy. Adjust it any time under Policy.</p></div></div>
  <div class="step ${step("vendors") && step("payments") ? "done" : ""}" id="st-data"><div class="n">2</div><div class="panel"><h2>Vendors and payment history</h2>
    <p class="muted">Export both lists from your accounting system as CSV. Every row is checked before anything is saved.</p>
    <div class="uploads"><label>Vendor list<input type="file" id="vf" accept=".csv"><span class="hint"><a href="/api/csv-templates/vendors">Download the vendor template</a></span></label>
      <label>Payment history<input type="file" id="pf" accept=".csv"><span class="hint"><a href="/api/csv-templates/payments">Download the payment template</a></span></label></div>
    <button class="btn secondary" id="sample">Use sample data</button><div id="imp"></div></div></div>
  <div class="step ${step("team") ? "done" : ""}" id="st-team"><div class="n">3</div><div class="panel"><h2>Your team</h2>
    <p class="muted">Approvers release payments. Nobody can approve a payment they submitted.</p>
    <div class="row"><label>Email<input id="ie" type="email" placeholder="colleague@company.com"></label>
      <label class="narrow">Role<select id="ir"><option value="approver">Approver</option><option value="analyst">Analyst</option><option value="clerk">Clerk</option><option value="policy_admin">Policy admin</option><option value="auditor">Auditor</option><option value="owner">Owner</option></select></label>
      <button class="btn secondary" id="inv">Create invite link</button></div><div id="invout"></div><ul class="list spaced-t" id="members"></ul></div></div>
  <div class="step ${step("integration") ? "done" : ""}" id="st-api"><div class="n">4</div><div class="panel"><h2>Connect your accounting system <span class="muted small light">(optional)</span></h2>
    <p class="muted">Your system sends each payment to <code>POST ${esc(location.origin)}/api/t/${esc(S.tid)}/checks</code> before releasing it. Include an <code>Idempotency-Key</code> header so a retry never creates a duplicate.</p>
    <div class="row"><label>Key name<input id="kn" placeholder="SAP production"></label><button class="btn secondary" id="kc">Create API key</button></div><div id="kout"></div></div></div>
  <div class="step ${step("first_case") ? "done" : ""}"><div class="n">5</div><div class="panel"><h2>Check your first payment</h2>
    <p class="muted">Upload a payment request or run a sample case.</p><a class="btn primary" href="#/checks">Go to payment checks</a></div></div>`;

  const paint = (o) => { const d = o.steps.filter((s) => s.done).length;
    $("#bar").style.width = `${Math.round((d / o.steps.length) * 100)}%`; $("#progtext").textContent = `${d} of ${o.steps.length} done`; };
  paint(ob);
  const refresh = async () => { const o = await api(T("/onboarding")); paint(o);
    const done = (id) => o.steps.find((s) => s.id === id)?.done;
    $("#st-data").classList.toggle("done", done("vendors") && done("payments")); $("#st-team").classList.toggle("done", !!done("team")); $("#st-api").classList.toggle("done", !!done("integration")); };
  const imp = async (kind, file) => {
    const fd = new FormData(); fd.append("file", file);
    $("#imp").innerHTML = `<p class="msg info">Checking ${esc(file.name)}…</p>`;
    try { const r = await api(T(`/import/${kind}`), { method: "POST", body: fd });
      $("#imp").innerHTML = r.error_count ? `<p class="msg err">${r.error_count} row(s) need fixing. ${esc(r.note)}</p><table class="mini"><tr><th>Row</th><th>Column</th><th>Problem</th></tr>${r.errors.map((e) => `<tr><td>${e.row}</td><td>${esc(e.field)}</td><td>${esc(e.message)}</td></tr>`).join("")}</table>`
        : `<p class="msg ok">Imported ${r.imported} ${kind}.</p>`; refresh(); }
    catch (e) { $("#imp").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; }
  };
  $("#vf").onchange = (e) => imp("vendors", e.target.files[0]);
  $("#pf").onchange = (e) => imp("payments", e.target.files[0]);
  $("#sample").onclick = () => busy($("#sample"), "Loading…", async () => { try { const r = await api(T("/sample-data"), { method: "POST" });
    $("#imp").innerHTML = `<p class="msg ok">Loaded ${r.vendors} sample vendors and ${r.payments} payments.</p>`; refresh(); } catch (e) { $("#imp").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  $("#inv").onclick = () => busy($("#inv"), "Creating…", async () => { try { const r = await api(T("/invites"), { method: "POST", json: { email: $("#ie").value, role: $("#ir").value } });
    $("#invout").innerHTML = `<p class="msg ok">Send this link. It works once, for that address, for ${r.expires_in_days} days.</p><div class="secret"><input readonly value="${esc(r.invite_link)}"></div>`; listMembers(); refresh(); }
    catch (e) { $("#invout").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  $("#kc").onclick = () => busy($("#kc"), "Creating…", async () => { try { const r = await api(T("/api-keys"), { method: "POST", json: { name: $("#kn").value } });
    $("#kout").innerHTML = `<p class="msg ok">Copy this key now. It's shown only once.</p><div class="secret"><input readonly value="${esc(r.api_key)}"></div>`; refresh(); }
    catch (e) { $("#kout").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  const roleName = (r) => r.replace("_", " ");
  const listMembers = async () => { const m = await api(T("/members"));
    $("#members").innerHTML = m.members.map((x) => `<li><span>${esc(x.email)}</span><span class="pill">${roleName(x.role)}</span></li>`).join("") +
      m.invites.map((x) => `<li><span class="muted">${esc(x.email)}</span><span class="pill">${roleName(x.role)} · invited</span></li>`).join(""); };
  listMembers();
}

// --------------------------------------------------------------- checks --
async function renderChecks() {
  const [cases, demos] = await Promise.all([api(T("/cases")), (await fetch("/api/demo")).json()]);
  view.innerHTML = `<div class="page-head"><div><h1>Payment checks</h1><p>Check a payment request before any money is released.</p></div></div>
  <div class="checks"><section class="panel"><h2>New check</h2>
    <label class="drop" id="drop" tabindex="0"><input type="file" id="file" multiple accept=".eml,.txt,.pdf,.png,.jpg,.jpeg,.mp3,.wav,.m4a,.ogg,.webm">
      <strong>Drop the payment request here</strong><span>Email (.eml), voice note, invoice PDF or screenshot, up to 6 files</span></label>
    <ul class="list picked" id="picked"></ul><button class="btn primary block" id="go" disabled>Check this payment</button>
    ${demos.length ? `<h3 class="section">Or try a sample case</h3><div class="samples">${demos.map((d) => `<button class="sample" data-id="${esc(d.id)}">${esc(d.title)}<small>${esc(d.description)}</small></button>`).join("")}</div>` : ""}
    <p class="msg" id="err"></p></section>
  <section class="panel"><h2>Recent checks</h2>${cases.length ? `<div class="table-wrap"><table class="data cards"><thead><tr><th>Received</th><th>Payee</th><th class="num">Amount</th><th>Recommendation</th><th>Status</th></tr></thead><tbody>
    ${cases.map((c) => `<tr data-id="${esc(c.id)}"><td data-label="Received" class="nowrap">${esc(when(c.created_at))}</td><td data-label="Payee" class="payee">${esc(c.payee || "Reading…")}</td>
      <td data-label="Amount" class="num nowrap">${c.amount ? esc(money(c.currency, c.amount)) : "—"}</td><td data-label="Recommendation">${c.decision ? `<span class="reco"><span class="decision ${c.decision}">${label(c.decision)}</span>
        <span class="riskbar ${c.decision}" title="Risk score ${c.score} of 100"><i data-w="${Number(c.score) || 0}"></i></span><small>${Number(c.score) || 0}</small></span>` : `<span class="muted">Checking…</span>`}</td>
      <td data-label="Status"><span class="state-tag ${esc(c.state)}">${esc(STATE[c.state] || c.state)}</span></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty"><p>No payments checked yet.</p><p class="small">Drop a payment request on the left${demos.length ? " or run a sample case" : ""}.</p></div>`}</section></div>`;
  paintBars(view);
  let files = [];
  const pick = (list) => { files = [...files, ...list].slice(0, 6); $("#picked").innerHTML = files.map((f) => `<li><span>${esc(f.name)}</span><span class="muted">${Math.ceil(f.size / 1024)} KB</span></li>`).join(""); $("#go").disabled = !files.length; };
  $("#file").onchange = (e) => pick(e.target.files);
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => pick(e.dataTransfer.files));
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") $("#file").click(); });
  const wait = () => show($("#err"), "Reading the files. Large PDFs can take a little while.", "info");
  $("#go").onclick = () => busy($("#go"), "Uploading and reading files…", async () => { wait(); const fd = new FormData(); files.forEach((f) => fd.append("files", f));
    try { const r = await api(T("/cases"), { method: "POST", body: fd, headers: { "Idempotency-Key": crypto.randomUUID() } }); location.hash = `#/case/${r.case_id}`; } catch (e) { show($("#err"), e.message); } });
  view.querySelectorAll(".sample").forEach((b) => b.onclick = () => {
    view.querySelectorAll(".sample").forEach((x) => { if (x !== b) x.disabled = true; });
    return busy(b, "Starting this case…", async () => { wait();
      try { const r = await api(T(`/demo/${b.dataset.id}`), { method: "POST" }); location.hash = `#/case/${r.case_id}`; }
      catch (e) { show($("#err"), e.message); view.querySelectorAll(".sample").forEach((x) => { x.disabled = false; }); } });
  });
  view.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => (location.hash = `#/case/${tr.dataset.id}`));
}

// ----------------------------------------------------------------- case --
// Wrap the quoted words in <mark>. Matching ignores whitespace, curly quotes and masking
// tokens like [EMAIL] (the AI sees masked text; reviewers see the original).
function highlight(text, quote) {
  const fold = (s) => s.replace(/[’‘]/g, "'").replace(/[“”]/g, '"');
  const words = fold(quote || "").trim().split(/\s+/).filter(Boolean).slice(0, 60)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\[[A-Z_]+\\\]/g, "\\S+?"));
  const m = words.length ? new RegExp(words.join("\\s+"), "i").exec(fold(text)) : null;
  if (!m) return { html: esc(text), found: false };
  return { html: `${esc(text.slice(0, m.index))}<mark id="hit">${esc(text.slice(m.index, m.index + m[0].length))}</mark>${esc(text.slice(m.index + m[0].length))}`, found: true };
}

// Show what changed between a lookalike and the real domain (common prefix/suffix, differing middle).
function domainDiff(fake, real) {
  let p = 0; while (p < fake.length && p < real.length && fake[p] === real[p]) p++;
  let s = 0; while (s < fake.length - p && s < real.length - p && fake[fake.length - 1 - s] === real[real.length - 1 - s]) s++;
  const part = (d) => `${esc(d.slice(0, p))}<b>${esc(d.slice(p, d.length - s)) || "&#8203;"}</b>${esc(d.slice(d.length - s))}`;
  return `<div class="lookalike"><div><span>Sent from</span><code class="fake">${part(fake)}</code></div><div><span>Your real domain</span><code class="real">${part(real)}</code></div></div>`;
}

// "How this decision was made": risk meter with the policy's zones, what added the points,
// and each step that set or raised the decision. Built only from the policy engine's trace.
function explainCard(v) {
  const x = v.explain || {};
  if (!x.thresholds) return "";
  const { callback_at: cb, hold_at: hold } = x.thresholds;
  const score = Math.max(0, Math.min(100, x.score ?? v.risk_score));
  const parts = [...(x.contributions || []).map((c) => ({ ...c, cls: `ag-${c.agent}` })),
    ...(x.rule_points || []).map((r) => ({ title: r.label, agent: "policy", points: r.points, cls: "ag-policy" }))];
  const total = parts.reduce((s, p) => s + p.points, 0);
  const scale = Math.max(100, total);
  const zoneOf = (n) => (n >= hold ? "HOLD" : n >= cb ? "CALLBACK" : "RELEASE");
  const step = (p) => {
    const chip = `<span class="decision ${p.decision}">${DEC_TEXT[p.decision]}</span>`;
    const text = { score: `Risk score <b>${score}</b> lands in the <b>${DEC_TEXT[zoneOf(score)]}</b> zone`,
      critical: `${esc(p.label)}. Your policy holds any critical finding`,
      rule: `Policy rule: ${esc(p.label)}`, failed: `${esc(p.label)}. Your policy fails closed` }[p.kind] || esc(p.label);
    const note = p.kind === "score" ? "" : p.raised ? `<em class="raised">raised to</em>` : `<em>at least</em>`;
    return `<li class="${p.raised ? "raised" : ""} k-${p.kind}"><span class="dot"></span><div><p>${text}</p><span class="to">${note}${chip}</span></div></li>`;
  };
  return `<header><h2>How this decision was made</h2><span class="by">Calculated by your policy rules. No AI involved in the decision.</span></header>
  <div class="explain-grid"><div>
    <h3>Risk score</h3>
    <div class="meter" role="img" aria-label="Risk score ${score} of 100. Call back from ${cb}, hold from ${hold}.">
      <div class="zones"><span class="z RELEASE" data-w="${cb}"><b>Release</b></span><span class="z CALLBACK" data-w="${hold - cb}"><b>Call back</b></span><span class="z HOLD" data-w="${100 - hold}"><b>Hold</b></span></div>
      <div class="pin ${zoneOf(score)}" data-left="${Math.min(97, Math.max(3, score))}"><b>${score}</b></div>
      <div class="ticks"><span data-left="0">0</span><span data-left="${cb}">${cb}</span><span data-left="${hold}">${hold}</span><span data-left="100">100</span></div>
    </div>
    <h3>What added to the score</h3>
    ${parts.length ? `<div class="stack" aria-hidden="true">${parts.map((p) => `<span class="${p.cls}" data-w="${(p.points / scale) * 100}" title="${esc(p.title)}: +${p.points}"></span>`).join("")}</div>
      ${(() => { const row = (p) => `<li><span class="sw ${p.cls}"></span><span class="t">${esc(p.title)}<small>${esc(CHECK_NAME[p.agent] || (p.agent === "policy" ? "Policy rule" : p.agent))}</small></span><b>+${p.points}</b></li>`;
        const shown = parts.slice(0, 5), rest = parts.slice(5);
        return `<ul class="contrib">${shown.map(row).join("")}</ul>${rest.length ? `<details class="more"><summary>${rest.length} smaller signal${rest.length > 1 ? "s" : ""} (+${rest.reduce((s, p) => s + p.points, 0)})</summary><ul class="contrib">${rest.map(row).join("")}</ul></details>` : ""}`; })()}
      ${total > 100 ? `<p class="muted small">These add up to ${total}; the score is capped at 100.</p>` : ""}`
    : `<p class="muted small">Nothing added risk points. Every check came back clean.</p>`}
  </div><div>
    <h3>Decision path</h3>
    <ol class="path">${(x.path || []).map(step).join("")}
      <li class="final"><span class="dot"></span><div><p>Recommendation</p><span class="to"><span class="decision big ${v.decision}">${DEC_TEXT[v.decision]}</span></span>
        ${(v.requirements || []).length ? `<p class="muted small">Before release: ${v.requirements.map((r) => REQ_SHORT[r] || r).join(", ").toLowerCase()}.</p>` : ""}</div></li></ol>
  </div></div>`;
}
const REQ_SHORT = { callback: "Callback to the number on file", second_approver: "Second approver", compliance_review: "Compliance review" };

async function renderCase(cid) {
  view.innerHTML = `<a class="back" href="#/checks">← All payment checks</a>
  <div class="case-head" id="casehead" hidden><h1 id="ch-title"></h1><p id="ch-sub"></p></div>
  <section class="panel verdict pending" id="verdict"><div class="progress"><span class="spinner"></span><span id="status">Starting the investigation…</span><span class="muted small" id="tally"></span></div></section>
  <section class="panel explain" id="explain" hidden></section>
  <section class="panel flowcard" id="flowcard"><div class="flow">
    <div class="stage"><h4>Received</h4><div class="nodes" id="flow-in"></div></div><span class="arrow" aria-hidden="true"></span>
    <div class="stage grow"><h4>Checked in parallel</h4><div class="nodes" id="flow-checks"></div></div><span class="arrow" aria-hidden="true"></span>
    <div class="stage"><h4>Your policy</h4><div class="nodes"><span class="node waiting" id="flow-policy"><span class="icon"></span><span id="flow-policy-name">Policy</span></span></div></div><span class="arrow" aria-hidden="true"></span>
    <div class="stage"><h4>Recommendation</h4><div class="nodes"><span class="node outcome" id="flow-out">…</span></div></div>
  </div></section>
  <div class="case"><section class="panel"><header><h2>Evidence</h2><span class="muted small" id="policyname"></span></header><div id="checks"></div></section>
    <aside><section class="panel decide" id="decide" hidden></section>
      <section class="panel" id="slip" hidden><header><h2>The request</h2><span class="by" id="slip-by"></span></header><dl class="kv" id="slip-fields"></dl><p class="notice" id="slip-notes" hidden></p></section>
      <section class="panel" id="inputs" hidden><h2>What was received</h2><ul class="list inputs" id="input-list"></ul><div id="player"></div></section></aside></div>
  <dialog class="viewer" id="viewer"><header><h2 id="viewer-title"></h2><button class="btn secondary sm" id="viewer-close">Close</button></header><div id="viewer-body"></div></dialog>`;

  const checks = {}, texts = {}, blobs = {};
  let total = 0, finished = 0, request = null;
  const c0 = await api(T(`/cases/${cid}`)).catch(() => null);
  const sources = c0?.sources || [];
  sources.forEach((s) => { if (s.text) texts[s.label] = s.text; });

  // ---- inputs + viewer
  const nice = (l) => l.replaceAll("_", " ");
  const kindOf = (s) => (s.mime_type.startsWith("audio/") ? "audio" : s.mime_type === "application/pdf" ? "pdf" : s.mime_type.startsWith("image/") ? "image" : "text");
  const blobUrl = async (lbl) => blobs[lbl] || (blobs[lbl] = URL.createObjectURL(await (await api(T(`/cases/${cid}/sources/${encodeURIComponent(lbl)}`), { raw: true })).blob()));
  const dlg = $("#viewer");
  $("#viewer-close").onclick = () => dlg.close();
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  const openText = (lbl, quote) => {
    const text = texts[lbl] || (lbl.startsWith("voice_note") && request?.transcript) || "";
    const h = highlight(text, quote);
    $("#viewer-title").textContent = lbl.startsWith("voice_note") ? "Voice note transcript" : `The ${nice(lbl)}`;
    $("#viewer-body").innerHTML = `${quote && !h.found ? `<p class="notice">The quote was checked against the text the AI read; it couldn't be pinpointed in this view.</p>` : ""}<pre class="source">${h.html}</pre>`;
    dlg.showModal(); $("#hit")?.scrollIntoView({ block: "center" });
  };
  const openSource = async (s, btn) => {
    const k = kindOf(s);
    if (k === "text") return openText(s.label);
    await busy(btn, "Loading…", async () => {
      const url = await blobUrl(s.label);
      if (k === "audio") { $("#player").innerHTML = `<p class="small muted">${esc(s.filename)}</p><audio controls autoplay src="${url}"></audio>`; return; }
      $("#viewer-title").textContent = s.filename;
      $("#viewer-body").innerHTML = k === "pdf" ? `<iframe class="pdf" src="${url}" title="${esc(s.filename)}"></iframe>` : `<img class="shot" src="${url}" alt="${esc(s.filename)}">`;
      dlg.showModal();
    });
  };
  if (sources.length) {
    const verb = { audio: "Play", pdf: "Open", image: "Open", text: "Read" };
    $("#input-list").innerHTML = sources.map((s, i) => `<li><span><b>${esc(nice(s.label))}</b><span class="muted small block">${esc(s.filename)}</span></span>
      <button class="btn secondary sm" data-src="${i}">${verb[kindOf(s)]}</button></li>`).join("");
    $("#input-list").querySelectorAll("[data-src]").forEach((b) => b.onclick = () => openSource(sources[+b.dataset.src], b));
    $("#inputs").hidden = false;
    $("#flow-in").innerHTML = sources.map((s) => `<span class="node in in-${kindOf(s)}" title="${esc(s.filename)}"><span class="icon"></span>${esc(nice(s.label))}</span>`).join("");
  }
  const flowNodes = {};

  // ---- evidence lanes
  const lane = (id, title) => { if (!checks[id]) { const el = document.createElement("article"); el.className = "check waiting";
      el.innerHTML = `<header><h3><span class="icon" aria-hidden="true"></span>${esc(title)}</h3><span class="state">Waiting</span></header><div class="flags"></div><details class="passed" hidden><summary></summary><ul></ul></details>`;
      $("#checks").append(el); checks[id] = el;
      const n = document.createElement("button"); n.className = "node waiting"; n.title = `Jump to ${title}`;
      n.innerHTML = `<span class="icon"></span>${esc(CHECK_NAME[id] || title)}<small></small>`;
      n.onclick = () => el.scrollIntoView({ behavior: "smooth", block: "start" });
      $("#flow-checks").append(n); flowNodes[id] = n; } return checks[id]; };
  const setState = (a, cls, t) => { const el = checks[a]; if (!el) return; el.className = `check ${cls}`; $(".state", el).textContent = t;
    const n = flowNodes[a]; if (n) { n.className = `node ${cls}`; $("small", n).textContent = /^\d+ red flag/.test(t) ? t.split(" ")[0] : ""; } };
  const status = (t) => { const s = $("#status"); if (s) s.textContent = t; };
  const tally = () => { const t = $("#tally"); if (t && total) t.textContent = `${finished} of ${total} checks done`; };
  const addFinding = (el, f) => {
    if (f.severity === "info") { const d = $(".passed", el); d.hidden = false; const li = document.createElement("li"); li.textContent = f.title; $("ul", d).append(li);
      $("summary", d).textContent = `${$("ul", d).children.length} passed`; return; }
    const item = document.createElement("div"); item.className = `finding sev-${f.severity}`; item.dataset.rank = SEV_RANK[f.severity];
    const ai = /gemini/i.test(f.data_source);
    const quotes = (f.evidence || []).filter((e) => e.excerpt).slice(0, 2).map((e) => {
      const canShow = texts[e.source] || (e.source.startsWith("voice_note") && request?.transcript);
      return `<q>${esc(e.excerpt)}</q><cite>${e.source === f.data_source ? "" : `From the ${esc(nice(e.source))}`}${canShow ? ` <button class="link" data-show="${esc(e.source)}" data-q="${esc(e.excerpt)}">Show in ${esc(nice(e.source))}</button>` : ""}</cite>`; }).join("");
    item.innerHTML = `<b>${esc(f.title)}${ai ? ` <span class="ai" title="Found by Gemini; the quote was verified against the source">AI</span>` : ""}</b><p>${esc(f.detail)}</p>
      ${f.meta?.domain && f.meta?.imitates ? domainDiff(f.meta.domain, f.meta.imitates) : ""}${quotes}<cite>Checked against: ${esc(f.data_source)}</cite>`;
    item.querySelectorAll("[data-show]").forEach((b) => b.onclick = () => openText(b.dataset.show, b.dataset.q));
    const flags = $(".flags", el); // most severe first
    const after = [...flags.children].find((c) => +c.dataset.rank < SEV_RANK[f.severity]);
    flags.insertBefore(item, after || null);
  };
  const showSlip = (r, notes, by) => {
    request = r;
    const rows = [["Requested by", [r.requester_name, r.requester_claimed_role && `(${r.requester_claimed_role})`].filter(Boolean).join(" ")], ["From", r.requester_email],
      ["Amount", r.amount != null ? money(r.currency, r.amount) : null], ["Pay to", r.beneficiary_name], ["Bank", r.beneficiary_bank],
      ["Account", r.iban || r.account_number], ["SWIFT", r.swift_bic], ["IFSC", r.ifsc], ["GSTIN", r.gstin], ["Invoice", r.invoice_number], ["Deadline", r.deadline]].filter(([, v]) => v);
    $("#slip-fields").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    $("#slip-by").textContent = by === "gemini" ? "Read by Gemini" : by === "patterns" ? "Read with patterns" : "";
    $("#slip-by").className = `by ${by === "gemini" ? "ai-by" : ""}`;
    const n = $("#slip-notes"); n.textContent = (notes || []).join(" "); n.hidden = !(notes || []).length; $("#slip").hidden = false;
    $("#ch-title").textContent = [r.amount != null ? money(r.currency, r.amount) : "Payment", r.beneficiary_name && `to ${r.beneficiary_name}`].filter(Boolean).join(" ");
    $("#ch-sub").textContent = [r.requester_name && `Requested by ${r.requester_name}${r.requester_claimed_role ? ` (${r.requester_claimed_role})` : ""}`,
      r.requester_email, c0?.created_at && `received ${when(c0.created_at)}`].filter(Boolean).join(" · ");
    $("#casehead").hidden = false; };
  const showVerdict = (v) => {
    const el = $("#verdict"); el.className = `panel verdict v-${v.decision}`;
    const why = v.decision !== "RELEASE" ? (v.top_reasons || []).slice(0, 3) : [];
    el.innerHTML = `<div class="v-side"><div class="stamp ${v.decision}">${v.decision === "CALLBACK" ? "CALL BACK" : v.decision}</div>
      <div class="score"><b>${v.risk_score}</b><span>risk score out of 100</span><button class="btn secondary sm" id="pdf">Download case file</button></div></div>
      <div class="v-body"><p class="summary">${esc(v.summary)}</p>${v.narrative_by === "gemini" ? `<p class="by ai-by">Summary written by Gemini. The decision itself comes from your policy rules.</p>` : ""}
        ${why.length ? `<h3>Main reasons</h3><ul class="reasons">${why.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${(v.fired_rules || []).length ? `<h3>Your policy requires</h3><ul class="rules">${v.fired_rules.map((r) => `<li>${esc(r.description)}</li>`).join("")}</ul>` : ""}</div>`;
    const ex = $("#explain"); ex.innerHTML = explainCard(v); ex.hidden = !ex.innerHTML; paintBars(ex);
    $("#flow-policy").className = "node done clear";
    const out = $("#flow-out"); out.className = `node outcome ${v.decision}`; out.innerHTML = `<b>${DEC_TEXT[v.decision]}</b><small>${v.risk_score}/100</small>`;
    $("#pdf").onclick = () => busy($("#pdf"), "Preparing…", async () => { const res = await api(T(`/cases/${cid}/report.pdf`), { raw: true }); if (!res.ok) return;
      const url = URL.createObjectURL(await res.blob()); const a = document.createElement("a"); a.href = url; a.download = `case-${cid}.pdf`; a.click(); }); };
  const titleOf = (id) => ({ guard: "Input guard" }[id] || id);
  const handle = (ev) => { const d = ev.data || {};
    switch (ev.type) {
      case "case.started": $("#checks").innerHTML = ""; $("#flow-checks").innerHTML = ""; for (const k in checks) delete checks[k];
        (d.agents || []).forEach((a) => lane(a.id, a.title));
        total = (d.agents || []).length; finished = 0; tally();
        $("#policyname").textContent = d.policy ? `Policy v${d.policy.version}` : "";
        $("#flow-policy-name").textContent = d.policy ? `Policy v${d.policy.version}` : "Policy"; break;
      case "intake.started": status(`Reading ${d.inputs.length} file${d.inputs.length > 1 ? "s" : ""}…`); break;
      case "intake.completed": showSlip(d.request, d.notes, d.read_by); status("Running checks in parallel…"); break;
      case "agent.started": if (ev.agent === "verdict") { status("Applying your policy…"); $("#flow-policy").className = "node running"; }
        else setState(ev.agent, "running", "Checking…"); break;
      case "agent.completed": { const el = checks[ev.agent]; const flags = el ? $(".flags", el).children.length : 0;
        setState(ev.agent, flags ? "done flagged" : "done clear", flags ? `${flags} red flag${flags > 1 ? "s" : ""}` : "No red flags"); finished++; tally(); break; }
      case "agent.failed": setState(ev.agent, "failed", d.required ? "Couldn't run — release blocked" : "Couldn't run"); finished++; tally(); break;
      case "finding": addFinding(lane(ev.agent, titleOf(ev.agent)), d); break;
      case "verdict": showVerdict(d); break;
      case "case.failed": status(`The investigation stopped: ${d.error}. Don't release this payment.`); break;
      case "case.stalled": status("The investigation stopped responding. Reload the page to retry."); break;
    } };

  async function loadDecision() {
    const c = await api(T(`/cases/${cid}`)); const wf = c.workflow, v = c.verdict; if (!v) return;
    const pending = ["awaiting_decision", "awaiting_second_approval"].includes(wf.state);
    const cbOk = wf.callbacks.some((x) => x.outcome === "confirmed" && x.number_source !== "other");
    const releases = wf.approvals.filter((x) => x.action === "release").length;
    const reqs = wf.requirements.map((r) => ({ callback: ["Confirm by phone, using the number on file", cbOk], second_approver: ["Second approver signs off", releases >= 1],
      compliance_review: ["Compliance clears the sanctions alert", wf.approvals.some((x) => x.action === "clear_compliance")] }[r] || [r, false]));
    if (v.decision === "HOLD" && !wf.requirements.includes("second_approver")) reqs.push(["Second approver signs off (overriding a hold)", releases >= 1]);
    const el = $("#decide"); el.hidden = false;
    const end = { released: ["Released", "ok"], held: ["Kept on hold", "stop"], rejected: ["Marked as fraud", "stop"] }[wf.state] || ["Release", ""];
    const nextIdx = reqs.findIndex(([, d]) => !d);
    el.innerHTML = `<h2>Decision <span class="state-tag ${esc(wf.state)}">${esc(STATE[wf.state] || wf.state)}</span></h2>
      <ol class="track" aria-label="Steps before this payment can be released">
        <li class="done"><span class="dot"></span>Checked</li>
        ${reqs.map(([t, d], i) => `<li class="${d ? "done" : pending && i === nextIdx ? "next" : ""}"><span class="dot"></span>${esc(t.replace(", using the number on file", "").replace(" (overriding a hold)", ""))}</li>`).join("")}
        <li class="end ${end[1]} ${pending && nextIdx < 0 ? "next" : ""}"><span class="dot"></span>${end[0]}</li></ol>
      ${v.decision !== "RELEASE" && pending ? `${v.callback_number ? `<div class="callto"><span>Call ${esc(v.callback_contact)}</span><b>${esc(v.callback_number)}</b><small>From your vendor list, not from the request.</small></div>` : ""}
        <details class="script-box"><summary>What to say on the call${v.narrative_by === "gemini" ? ` <span class="ai">AI</span>` : ""}</summary><ol class="script">${v.callback_script.map((l) => `<li>${esc(l)}</li>`).join("")}</ol></details>` : ""}
      ${pending && S.can("callback:record") ? `<details class="form"><summary>Record a callback</summary>
        <label>Who you spoke to<input id="cbc" value="${esc(v.callback_contact || "")}"></label><label>Number you dialled<input id="cbn" value="${esc(v.callback_number || "")}"></label>
        <label>Where the number came from<select id="cbs"><option value="vendor_master">Vendor list</option><option value="company_directory">Company directory</option><option value="other">Somewhere else</option></select></label>
        <label>What they said<select id="cbo"><option value="confirmed">Confirmed the request</option><option value="denied">Said they didn't send it</option><option value="no_answer">No answer</option></select></label>
        <label>Notes<textarea class="prose" id="cbt" rows="2"></textarea></label><button class="btn secondary block" id="cbgo">Save callback</button></details>` : ""}
      ${pending && S.can("case:decide") ? `<label>Reason <span class="hint">Required to release a held payment</span><textarea class="prose" id="why" rows="2"></textarea></label>
        <div class="actions"><button class="btn primary" data-a="release">Release payment</button><button class="btn secondary" data-a="hold">Keep on hold</button><button class="btn danger" data-a="reject">Mark as fraud</button></div>` : ""}
      ${pending && !S.can("case:decide") ? `<p class="muted small">An approver will decide this payment.</p>` : ""}
      ${!pending ? `<p class="muted small">No further action needed.</p>` : ""}
      <p class="msg" id="derr"></p>
      ${wf.callbacks.length || wf.approvals.length ? `<ul class="list history">${wf.callbacks.map((x) => `<li>${esc(x.email)} called: ${esc(x.outcome.replace("_", " "))} (${esc(x.number_source.replace("_", " "))})</li>`).join("")}${wf.approvals.map((x) => `<li>${esc(x.email)}: ${esc(x.action.replace("_", " "))}${x.reason ? ` — ${esc(x.reason)}` : ""}</li>`).join("")}</ul>` : ""}`;
    $("#cbgo") && ($("#cbgo").onclick = () => busy($("#cbgo"), "Saving…", async () => { try { await api(T(`/cases/${cid}/callbacks`), { method: "POST", json: { contact: $("#cbc").value, number: $("#cbn").value, number_source: $("#cbs").value, outcome: $("#cbo").value, notes: $("#cbt").value } }); await loadDecision(); } catch (e) { show($("#derr"), e.message); } }));
    el.querySelectorAll("[data-a]").forEach((b) => b.onclick = () => busy(b, "Saving…", async () => {
      try { await api(T(`/cases/${cid}/actions`), { method: "POST", json: { action: b.dataset.a, reason: $("#why").value } }); await loadDecision(); } catch (e) { show($("#derr"), e.message); } }));
  }

  const res = await api(T(`/cases/${cid}/stream${S.replay ? "?replay=1" : ""}`), { raw: true });
  if (!res.ok) { $("#flowcard").hidden = true; return status("This case couldn't be opened."); }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) { const { value, done } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf("\n\n")) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join(""); if (data) handle(JSON.parse(data)); } }
  loadDecision();
}

// --------------------------------------------------------------- policy --
async function renderPolicy() {
  const list = await api(T("/policies")); const active = list.find((p) => p.status === "active"); const st = active.settings, th = st.thresholds;
  const statusText = { active: "Active", pending_approval: "Waiting for approval", draft: "Draft", retired: "Retired", rejected: "Rejected" };
  view.innerHTML = `<div class="page-head"><div><h1>Policy</h1><p>Your company's rules. Changes are tested on past checks and approved by a second admin before they apply.</p></div></div>
  <div class="two"><section class="panel"><header><h2>${esc(active.name)}</h2><span class="status active">Version ${active.version}, active</span></header>
    <dl class="kv"><dt>Hold when score reaches</dt><dd>${st.hold_at}</dd><dt>Call back when score reaches</dt><dd>${st.callback_at}</dd>
      <dt>Checks that must run</dt><dd>${st.required_checks.join(", ")}</dd><dt>If one can't run</dt><dd>${st.fail_mode === "hold" ? "Hold the payment" : "Require a callback"}</dd>
      <dt>Suspiciously new domain</dt><dd>Under ${th.domain_new_days_high} days old</dd><dt>Unusual amount</dt><dd>${th.amount_ratio_high}× the vendor's typical invoice</dd></dl>
    <h3 class="section">Rules</h3>${active.rules.map((r) => `<div class="rule">${esc(r.description)}<small>${[r.min_decision && `At least ${label(r.min_decision).toLowerCase()}`, r.require.length && `Needs ${r.require.map((x) => x.replace("_", " ")).join(", ")}`, r.add_points && `+${r.add_points} risk points`].filter(Boolean).join(". ")}</small></div>`).join("")}</section>
  <section class="panel"><h2>Propose a change</h2>${S.can("policy:edit") ? `<p class="muted small">Edit the rules below. Saving creates a draft and shows how it would have changed past decisions.</p>
    <label>Policy<textarea id="pj" rows="14" spellcheck="false">${esc(JSON.stringify({ name: active.name, settings: active.settings, rules: active.rules }, null, 2))}</textarea></label>
    <label>What changed and why<input id="pn" placeholder="Lower the hold threshold after Q3 incidents"></label><button class="btn primary" id="pd">Save draft and test it</button><div id="pout"></div>`
    : `<p class="muted">Your role can view the policy but not change it.</p>`}</section></div>
  <section class="panel spaced-t"><h2>Versions</h2><div class="table-wrap"><table class="data"><thead><tr><th>Version</th><th>Name</th><th>Status</th><th>Change</th><th></th></tr></thead><tbody>
    ${list.map((p) => `<tr><td>v${p.version}</td><td>${esc(p.name)}</td><td><span class="status ${p.status}">${statusText[p.status] || p.status}</span></td><td>${esc(p.change_note)}</td><td class="right">
      ${p.status === "draft" && S.can("policy:edit") ? `<button class="btn ghost sm" data-sub="${p.version}">Submit for approval</button>` : ""}
      ${p.status === "pending_approval" && S.can("policy:approve") ? `<button class="btn ghost sm" data-ap="${p.version}">Approve</button>&nbsp;&nbsp;<button class="btn ghost sm" data-rj="${p.version}">Reject</button>` : ""}</td></tr>`).join("")}</tbody></table></div><p class="msg" id="herr"></p></section>`;
  $("#pd") && ($("#pd").onclick = () => busy($("#pd"), "Testing on past checks…", async () => {
    try { const d = await api(T("/policies"), { method: "POST", json: { ...JSON.parse($("#pj").value), change_note: $("#pn").value } });
      const sim = await api(T(`/policies/${d.version}/simulate`), { method: "POST" }); const m = sim.metrics;
      $("#pout").innerHTML = `<div class="msg ok">Draft v${d.version} saved. Tested on ${sim.cases} past check${sim.cases === 1 ? "" : "s"}: ${sim.changed} decision${sim.changed === 1 ? "" : "s"} would change${sim.newly_released.length ? `, and ${sim.newly_released.length} payment${sim.newly_released.length > 1 ? "s" : ""} would now be released` : ""}.
        Confirmed fraud stopped: ${m.proposed.fraud_stopped} of ${m.proposed.fraud_cases} (now ${m.current.fraud_stopped}). Legitimate payments interrupted: ${m.proposed.legit_interrupted} of ${m.proposed.legit_cases} (now ${m.current.legit_interrupted}).</div>
        <button class="btn secondary spaced-t" id="refresh">Show in versions</button>`;
      $("#refresh").onclick = renderPolicy;
    } catch (e) { $("#pout").innerHTML = `<p class="msg err">${esc(e.message.startsWith("Unexpected") ? "The policy isn't valid JSON." : e.message)}</p>`; } }));
  const act = (sel, path, body) => view.querySelectorAll(sel).forEach((b) => b.onclick = () => busy(b, "Saving…", async () => {
    try { await api(T(`/policies/${Object.values(b.dataset)[0]}/${path}`), { method: "POST", json: body?.() ?? {} }); renderPolicy(); } catch (e) { show($("#herr"), e.message); } }));
  act("[data-sub]", "submit"); act("[data-ap]", "approve"); act("[data-rj]", "reject", () => ({ reason: prompt("Why are you rejecting this change?") || "" }));
}

// ---------------------------------------------------------------- audit --
async function renderAudit() {
  if (!S.can("audit:read")) { view.innerHTML = `<div class="page-head"><h1>Audit log</h1></div><div class="panel empty"><p>Your role can't view the audit log.</p></div>`; return; }
  const [entries, check] = await Promise.all([api(T("/audit")), api(T("/audit/verify"))]);
  view.innerHTML = `<div class="page-head"><div><h1>Audit log</h1><p>Every action in this workspace. Entries are chained, so any edit or deletion is detectable.</p></div></div>
  <section class="panel"><header><div class="integrity ${check.ok ? "" : "bad"}"><span class="dot"></span><b>${check.ok ? `Verified: ${check.entries} entries, none changed or removed` : `Integrity check failed at entry ${check.broken_at}: ${esc(check.reason)}`}</b></div></header>
    <div class="table-wrap"><table class="data cards"><thead><tr><th>#</th><th>When</th><th>Who</th><th>What</th><th>On</th></tr></thead><tbody>${entries.map((e) => `<tr><td data-label="#">${e.seq}</td><td data-label="When" class="nowrap">${esc(when(e.at))}</td><td data-label="Who">${esc(e.actor.replace(/^dev:/, ""))}</td><td data-label="What">${esc(e.action.replace(".", ": ").replaceAll("_", " "))}</td><td data-label="On" class="muted">${esc(e.target)}</td></tr>`).join("")}</tbody></table></div></section>`;
}

boot();
