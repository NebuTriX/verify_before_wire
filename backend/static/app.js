// Verify-Before-Wire UI. Hash routes: #/checks, #/case/<id>, #/setup, #/policy, #/audit, #/new
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const view = $("#view");
const S = { cfg: null, me: null, tid: localStorage.getItem("vbw.tid"), ws: null, fb: null, replay: false };

// ------------------------------------------------------------------ auth --
async function token() {
  if (S.cfg.auth_mode === "dev") return null;
  return S.fb?.currentUser ? S.fb.currentUser.getIdToken() : null;
}
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.cfg.auth_mode === "dev") headers["X-Dev-User"] = sessionStorage.getItem("vbw.dev") || "";
  else { const t = await token(); if (t) headers.Authorization = `Bearer ${t}`; }
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (opts.raw) return res;
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const d = body?.detail;
    throw new Error(Array.isArray(d) ? d.map((x) => `${x.field}: ${x.message}`).join("; ") : d || `Request failed (${res.status})`);
  }
  return body;
}
const T = (p) => `/api/t/${S.tid}${p}`;

async function initAuth() {
  S.cfg = await (await fetch("/api/config")).json();
  if (S.cfg.auth_mode === "dev") return;
  const v = "10.14.1";
  const { initializeApp } = await import(`https://www.gstatic.com/firebasejs/${v}/firebase-app.js`);
  const fa = await import(`https://www.gstatic.com/firebasejs/${v}/firebase-auth.js`);
  S.fbmod = fa;
  S.fb = fa.getAuth(initializeApp(S.cfg.firebase));
  await new Promise((r) => fa.onAuthStateChanged(S.fb, () => r(), () => r()));
}
function signedIn() { return S.cfg.auth_mode === "dev" ? !!sessionStorage.getItem("vbw.dev") : !!S.fb?.currentUser; }

function renderSignIn() {
  $("#nav").hidden = true;
  view.innerHTML = `<section class="card narrow">
    <h1>Sign in</h1><p class="muted">Use your work account. New here? Signing in creates your account.</p>
    ${S.cfg.auth_mode === "dev" ? `<label>Work email<input id="dev-email" type="email" autocomplete="email"></label>
      <button class="primary" id="dev-go">Continue</button><p class="muted small">Development sign-in. Production uses your company's SSO.</p>`
    : `<button class="primary" id="google">Continue with Google</button>
      <p class="muted small">Your administrator can also enable SAML or OIDC single sign-on.</p>`}
    <p class="error" id="err"></p></section>`;
  if (S.cfg.auth_mode === "dev") $("#dev-go").onclick = () => {
    const e = $("#dev-email").value.trim(); if (!e.includes("@")) return ($("#err").textContent = "Enter your work email.");
    sessionStorage.setItem("vbw.dev", e); boot();
  };
  else $("#google").onclick = async () => {
    try { await S.fbmod.signInWithPopup(S.fb, new S.fbmod.GoogleAuthProvider()); boot(); }
    catch (e) { $("#err").textContent = e.message; }
  };
}
$("#signout").onclick = async () => {
  sessionStorage.removeItem("vbw.dev"); if (S.fb) await S.fbmod.signOut(S.fb);
  localStorage.removeItem("vbw.tid"); location.hash = ""; boot();
};

// ------------------------------------------------------------------ boot --
async function boot() {
  if (!S.cfg) await initAuth();
  if (!signedIn()) return renderSignIn();
  const invite = location.hash.match(/invite=([\w-]+)/)?.[1];
  if (invite) {
    try { const r = await api("/api/invites/accept", { method: "POST", json: { token: invite } }); S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid); }
    catch (e) { alert(e.message); }
    location.hash = "#/checks";
  }
  S.me = await api("/api/me");
  $("#who").textContent = S.me.email; $("#signout").hidden = false;
  if (!S.me.workspaces.length) { location.hash = "#/new"; return route(); }
  if (!S.me.workspaces.some((w) => w.tenant_id === S.tid)) S.tid = S.me.workspaces[0].tenant_id;
  localStorage.setItem("vbw.tid", S.tid);
  const sw = $("#ws-switch");
  sw.innerHTML = S.me.workspaces.map((w) => `<option value="${w.tenant_id}" ${w.tenant_id === S.tid ? "selected" : ""}>${esc(w.name)}</option>`).join("") + `<option value="__new">+ New workspace</option>`;
  sw.onchange = () => { if (sw.value === "__new") location.hash = "#/new"; else { S.tid = sw.value; localStorage.setItem("vbw.tid", S.tid); route(); } };
  $("#nav").hidden = false;
  route();
}
window.addEventListener("hashchange", () => {
  if (!signedIn()) return;
  if (/invite=/.test(location.hash)) return boot();   // invite opened in an already-loaded tab
  route();
});
document.addEventListener("keydown", (e) => {
  if (e.shiftKey && e.key.toLowerCase() === "r" && !e.target.closest("input,textarea")) {
    S.replay = !S.replay; $("#mode").textContent = S.replay ? "recorded playback" : "live"; $("#mode").classList.toggle("replay", S.replay);
  }
});

async function route() {
  const h = location.hash.replace(/^#\/?/, "") || "checks";
  const [page, arg] = h.split("/");
  try {
    if (page === "new") return renderNewWorkspace();
    if (!S.tid) { location.hash = "#/new"; return; }
    S.ws = await api(T(""));
    const can = (p) => S.ws.my_permissions.includes(p);
    S.can = can;
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#/${page}`));
    if (page === "setup") return renderSetup();
    if (page === "policy") return renderPolicy();
    if (page === "audit") return renderAudit();
    if (page === "case") return renderCase(arg);
    return renderChecks();
  } catch (e) { view.innerHTML = `<p class="error">${esc(e.message)}</p>`; }
}

// ------------------------------------------------------- new workspace --
async function renderNewWorkspace() {
  const templates = await (await fetch("/api/templates")).json();
  view.innerHTML = `<section class="card narrow"><h1>Set up your workspace</h1>
    <p class="muted">Takes about five minutes. You can change everything later.</p>
    <label>Company name<input id="n" placeholder="Acme Holdings Pte Ltd"></label>
    <label>Your company's email domains <span class="muted small">comma separated, used to catch lookalikes</span><input id="d" placeholder="acme.com, acme.com.sg"></label>
    <div class="row"><label>Base currency<input id="c" value="USD" maxlength="3"></label>
    <label>Time zone<input id="tz" value="${Intl.DateTimeFormat().resolvedOptions().timeZone}"></label>
    <label>Data region<select id="r"><option value="asia-southeast1">Singapore</option><option value="asia-south1">Mumbai</option><option value="asia-northeast1">Tokyo</option><option value="europe-west1">Belgium</option><option value="us-central1">Iowa</option></select></label></div>
    <fieldset><legend>Starting policy</legend>${templates.map((t, i) => `<label class="choice"><input type="radio" name="tpl" value="${t.id}" ${i === 0 ? "checked" : ""}><span><b>${esc(t.name)}</b><br><span class="muted small">${esc(t.summary)}</span></span></label>`).join("")}</fieldset>
    <button class="primary" id="go">Create workspace</button><p class="error" id="err"></p></section>`;
  $("#go").onclick = async () => {
    try {
      const r = await api("/api/workspaces", { method: "POST", json: { name: $("#n").value, company_domains: $("#d").value.split(",").map((x) => x.trim()).filter(Boolean),
        base_currency: $("#c").value.toUpperCase(), timezone: $("#tz").value, region: $("#r").value, template: $("input[name=tpl]:checked").value } });
      S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid); location.hash = "#/setup"; boot();
    } catch (e) { $("#err").textContent = e.message; }
  };
}

// ----------------------------------------------------------------- setup --
async function renderSetup() {
  const ob = await api(T("/onboarding"));
  view.innerHTML = `<h1>Setup</h1><ol class="steps" id="steps">${ob.steps.map((s) => `<li class="${s.done ? "done" : ""}">${esc(s.title)}</li>`).join("")}</ol>
  <div class="grid2">
   <section class="card"><h2>Vendor master and payment history</h2>
    <p class="muted">Export from your ERP as CSV. Rows are checked before anything is saved.</p>
    <p><a href="/api/csv-templates/vendors">Vendor template</a> · <a href="/api/csv-templates/payments">Payment template</a></p>
    <label>Vendors CSV<input type="file" id="vf" accept=".csv"></label>
    <label>Payments CSV<input type="file" id="pf" accept=".csv"></label>
    <button class="secondary" id="sample">Load sample data instead</button><div id="imp"></div></section>
   <section class="card"><h2>Team</h2><p class="muted">Approvers decide payments. Nobody can approve a payment they submitted.</p>
    <div class="row"><input id="ie" type="email" placeholder="colleague@company.com" aria-label="Email">
    <select id="ir" aria-label="Role"><option value="approver">Approver</option><option value="analyst">Analyst</option><option value="clerk">Clerk</option><option value="policy_admin">Policy admin</option><option value="auditor">Auditor</option><option value="owner">Owner</option></select>
    <button class="secondary" id="inv">Create invite link</button></div><div id="invout"></div><ul id="members" class="plain"></ul></section>
   <section class="card"><h2>Connect your ERP or treasury system</h2>
    <p class="muted">Call <code>POST ${location.origin}/api/t/${S.tid}/checks</code> with the payment files before releasing. Send an <code>Idempotency-Key</code> header so retries never create duplicates.</p>
    <div class="row"><input id="kn" placeholder="Key name, e.g. SAP production" aria-label="Key name"><button class="secondary" id="kc">Create API key</button></div><div id="kout"></div><ul id="keys" class="plain"></ul></section>
  </div>`;
  const refreshSteps = async () => { const o = await api(T("/onboarding"));
    $("#steps").innerHTML = o.steps.map((s) => `<li class="${s.done ? "done" : ""}">${esc(s.title)}</li>`).join(""); };
  const imp = async (kind, file) => {
    const fd = new FormData(); fd.append("file", file);
    try { const r = await api(T(`/import/${kind}`), { method: "POST", body: fd });
      $("#imp").innerHTML = r.error_count ? `<p class="error">${r.error_count} problem(s). ${esc(r.note)}</p><table class="mini"><tr><th>Row</th><th>Field</th><th>Problem</th></tr>${r.errors.map((e) => `<tr><td>${e.row}</td><td>${esc(e.field)}</td><td>${esc(e.message)}</td></tr>`).join("")}</table>`
        : `<p class="ok">Imported ${r.imported} ${kind}.</p>`; refreshSteps(); } catch (e) { $("#imp").innerHTML = `<p class="error">${esc(e.message)}</p>`; }
  };
  $("#vf").onchange = (e) => imp("vendors", e.target.files[0]);
  $("#pf").onchange = (e) => imp("payments", e.target.files[0]);
  $("#sample").onclick = async () => { const r = await api(T("/sample-data"), { method: "POST" }); $("#imp").innerHTML = `<p class="ok">Loaded ${r.vendors} sample vendors and ${r.payments} payments.</p>`; refreshSteps(); };
  $("#inv").onclick = async () => { try { const r = await api(T("/invites"), { method: "POST", json: { email: $("#ie").value, role: $("#ir").value } });
    $("#invout").innerHTML = `<p class="ok">Send this link (works once, for that address, ${r.expires_in_days} days):</p><input readonly value="${esc(r.invite_link)}">`; listMembers(); refreshSteps(); }
    catch (e) { $("#invout").innerHTML = `<p class="error">${esc(e.message)}</p>`; } };
  $("#kc").onclick = async () => { try { const r = await api(T("/api-keys"), { method: "POST", json: { name: $("#kn").value } });
    $("#kout").innerHTML = `<p class="ok">${esc(r.note)}</p><input readonly value="${esc(r.api_key)}">`; refreshSteps(); }
    catch (e) { $("#kout").innerHTML = `<p class="error">${esc(e.message)}</p>`; } };
  const listMembers = async () => { const m = await api(T("/members"));
    $("#members").innerHTML = m.members.map((x) => `<li>${esc(x.email)} <span class="pill">${x.role}</span></li>`).join("") + m.invites.map((x) => `<li class="muted">${esc(x.email)} <span class="pill">${x.role}, invited</span></li>`).join(""); };
  listMembers();
}

document.addEventListener("focusin", (e) => { if (e.target.matches("input[readonly]")) e.target.select(); });

// ---------------------------------------------------------------- checks --
async function renderChecks() {
  const [cases, demos] = await Promise.all([api(T("/cases")), (await fetch("/api/demo")).json()]);
  view.innerHTML = `<div class="grid-side"><section><h1>Check a payment</h1>
    <label class="drop" id="drop" tabindex="0"><input type="file" id="file" multiple accept=".eml,.txt,.pdf,.png,.jpg,.jpeg,.mp3,.wav,.m4a,.ogg,.webm">
      <strong>Drop the request here</strong><span>Email (.eml), voice note, invoice PDF or screenshot. Up to 6 files.</span></label>
    <ul class="plain small" id="picked"></ul><button class="primary" id="go" disabled>Investigate payment</button>
    <h3 class="gap">Sample cases</h3><div class="demo-list">${demos.map((d) => `<button class="demo" data-id="${d.id}">${esc(d.title)}<small>${esc(d.description)}</small></button>`).join("")}</div></section>
    <section><h2>Recent checks</h2>${cases.length ? `<table class="list"><tr><th>When</th><th>Payee</th><th>Amount</th><th>Recommendation</th><th>Status</th></tr>
      ${cases.map((c) => `<tr data-id="${c.id}"><td>${new Date(c.created_at).toLocaleString()}</td><td>${esc(c.payee || "—")}</td><td>${c.amount ? `${esc(c.currency)} ${Number(c.amount).toLocaleString()}` : "—"}</td>
      <td>${c.decision ? `<span class="tag ${c.decision}">${c.decision === "CALLBACK" ? "Call back" : c.decision}</span>` : "…"}</td><td>${esc(c.state.replaceAll("_", " "))}</td></tr>`).join("")}</table>`
      : `<p class="muted">No checks yet. Drop a payment request or run a sample case.</p>`}</section></div>`;
  let files = [];
  const pick = (list) => { files = [...files, ...list].slice(0, 6); $("#picked").innerHTML = files.map((f) => `<li>${esc(f.name)}</li>`).join(""); $("#go").disabled = !files.length; };
  $("#file").onchange = (e) => pick(e.target.files);
  const drop = $("#drop");
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => pick(e.dataTransfer.files));
  $("#go").onclick = async () => { const fd = new FormData(); files.forEach((f) => fd.append("files", f));
    try { const r = await api(T("/cases"), { method: "POST", body: fd, headers: { "Idempotency-Key": crypto.randomUUID() } }); location.hash = `#/case/${r.case_id}`; }
    catch (e) { alert(e.message); } };
  view.querySelectorAll(".demo").forEach((b) => b.onclick = async () => {
    try { const r = await api(T(`/demo/${b.dataset.id}`), { method: "POST" }); location.hash = `#/case/${r.case_id}`; } catch (e) { alert(e.message); } });
  view.querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = () => (location.hash = `#/case/${tr.dataset.id}`));
}

// ------------------------------------------------------------------ case --
async function renderCase(cid) {
  view.innerHTML = `<p><a href="#/checks">← All checks</a></p><div class="grid-side">
   <section><div class="slip card" id="slip" hidden><h3>What the request asks for</h3><dl id="slip-fields"></dl><p class="notes" id="slip-notes"></p></div>
     <div id="decide"></div></section>
   <section><div class="board-head"><h1>Investigation</h1><p class="status" id="status" role="status">Starting…</p></div>
    <div class="verdict card" id="verdict" hidden><div class="stamp" id="stamp"></div><div><p class="score"><span id="score"></span> risk score</p>
     <p class="summary" id="summary"></p><ul class="plain small" id="rules"></ul><a class="secondary" id="pdf" href="#">Download case file (PDF)</a></div></div>
    <div class="lanes" id="lanes"></div></section></div>`;
  const lanes = {};
  const lane = (id, title) => { if (!lanes[id]) { const el = document.createElement("article"); el.className = "lane";
    el.innerHTML = `<header><h3>${esc(title)}</h3><span class="state">Queued</span></header><div class="items"></div>`; $("#lanes").append(el); lanes[id] = el; } return lanes[id]; };
  const setState = (a, cls, t) => { const el = lanes[a]; if (!el) return; el.className = `lane ${cls}`; $(".state", el).textContent = t; };
  const status = (t) => ($("#status").textContent = t);
  const handle = (ev) => { const d = ev.data || {};
    switch (ev.type) {
      case "case.started": $("#lanes").innerHTML = ""; for (const k in lanes) delete lanes[k];
        (d.agents || []).forEach((a) => lane(a.id, a.title)); status(`Policy v${d.policy?.version ?? "?"}: ${d.policy?.name ?? ""}`); break;
      case "intake.started": status(`Reading ${d.inputs.length} input(s) with Gemini…`); break;
      case "intake.completed": showSlip(d.request, d.notes); status("Checks running in parallel…"); break;
      case "agent.started": if (ev.agent === "verdict") status("Applying your company's policy…"); else setState(ev.agent, "running", "Checking"); break;
      case "agent.completed": setState(ev.agent, "done", `${d.findings} result(s) · ${(d.ms / 1000).toFixed(1)}s`); break;
      case "agent.failed": setState(ev.agent, "failed", d.required ? "Failed — required check, payment cannot be released" : "Failed — treated as unverified"); break;
      case "finding": addFinding(lane(ev.agent, ev.agent === "guard" ? "Input guard" : ev.agent), d); break;
      case "verdict": showVerdict(d); break;
      case "case.completed": status(`Finished in ${(d.ms / 1000).toFixed(1)}s.`); loadDecision(); break;
      case "case.failed": status(`The investigation stopped: ${d.error}. Do not release this payment.`); break;
      case "case.stalled": status("The investigation stopped responding. Reload to retry."); break;
    } };
  const addFinding = (el, f) => { const item = document.createElement("div"); item.className = `finding sev-${f.severity}`;
    item.innerHTML = `<b>${esc(f.title)}</b><p>${esc(f.detail)}</p>${(f.evidence || []).filter((e) => e.excerpt).slice(0, 2).map((e) => `<q>${esc(e.excerpt)}</q><cite>${esc(e.source)}</cite>`).join("")}<cite>Source: ${esc(f.data_source)}</cite>`;
    const items = $(".items", el); f.severity === "info" ? items.append(item) : items.insertBefore(item, $(".sev-info", items)); };
  const showSlip = (r, notes) => { const rows = [["Requested by", [r.requester_name, r.requester_claimed_role && `(${r.requester_claimed_role})`].filter(Boolean).join(" ")], ["From", r.requester_email],
    ["Amount", r.amount != null ? `${r.currency || ""} ${Number(r.amount).toLocaleString()}` : null], ["Pay to", r.beneficiary_name], ["Bank", r.beneficiary_bank],
    ["Account", r.iban || r.account_number], ["SWIFT", r.swift_bic], ["Invoice", r.invoice_number], ["Deadline", r.deadline]].filter(([, v]) => v);
    $("#slip-fields").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join(""); $("#slip-notes").textContent = (notes || []).join(" "); $("#slip").hidden = false; };
  const showVerdict = (v) => { $("#stamp").textContent = v.decision === "CALLBACK" ? "CALL BACK" : v.decision; $("#stamp").className = `stamp ${v.decision}`;
    $("#score").textContent = `${v.risk_score}/100`; $("#summary").textContent = v.summary;
    $("#rules").innerHTML = (v.fired_rules || []).map((r) => `<li>Policy rule: ${esc(r.description)}</li>`).join("");
    $("#pdf").onclick = async (e) => { e.preventDefault(); const res = await api(T(`/cases/${cid}/report.pdf`), { raw: true });
      const url = URL.createObjectURL(await res.blob()); const a = document.createElement("a"); a.href = url; a.download = `case-${cid}.pdf`; a.click(); };
    $("#verdict").hidden = false; };

  async function loadDecision() {
    const c = await api(T(`/cases/${cid}`)); const wf = c.workflow, v = c.verdict; if (!v) return;
    const pending = ["awaiting_decision", "awaiting_second_approval"].includes(wf.state);
    const reqText = { callback: "Confirmed callback to a number on file", second_approver: "A second approver", compliance_review: "Compliance clearance" };
    $("#decide").innerHTML = `<div class="card"><h3>Decision: ${esc(wf.state.replaceAll("_", " "))}</h3>
      ${wf.requirements.length ? `<p class="small">Before release:</p><ul class="plain small">${wf.requirements.map((r) => `<li>${esc(reqText[r] || r)}</li>`).join("")}</ul>` : ""}
      ${v.callback_number ? `<p class="small">Call <b>${esc(v.callback_contact)}</b> on <b>${esc(v.callback_number)}</b> (vendor master).</p>` : ""}
      ${v.decision !== "RELEASE" ? `<ol class="small">${v.callback_script.map((l) => `<li>${esc(l)}</li>`).join("")}</ol>` : ""}
      ${[...wf.callbacks.map((x) => `<li>Callback by ${esc(x.email)}: ${esc(x.outcome)} (${esc(x.number_source.replace("_", " "))})</li>`), ...wf.approvals.map((x) => `<li>${esc(x.action)} by ${esc(x.email)} ${esc(x.reason)}</li>`)].length ? `<ul class="plain small">${wf.callbacks.map((x) => `<li>Callback by ${esc(x.email)}: ${esc(x.outcome)} (${esc(x.number_source.replace("_", " "))})</li>`).join("")}${wf.approvals.map((x) => `<li>${esc(x.action.replace("_", " "))} by ${esc(x.email)}${x.reason ? `: ${esc(x.reason)}` : ""}</li>`).join("")}</ul>` : ""}
      ${pending && S.can("callback:record") ? `<details><summary>Record a callback</summary>
        <label>Who you spoke to<input id="cbc" value="${esc(v.callback_contact || "")}"></label><label>Number dialled<input id="cbn" value="${esc(v.callback_number || "")}"></label>
        <label>Where the number came from<select id="cbs"><option value="vendor_master">Vendor master</option><option value="company_directory">Company directory</option><option value="other">Somewhere else</option></select></label>
        <label>Outcome<select id="cbo"><option value="confirmed">Confirmed the request</option><option value="denied">Denied sending it</option><option value="no_answer">No answer</option></select></label>
        <label>Notes<textarea id="cbt" rows="2"></textarea></label><button class="secondary" id="cbgo">Save callback</button></details>` : ""}
      ${pending && S.can("case:decide") ? `<label>Reason <span class="muted small">(required to release a held payment)</span><textarea id="why" rows="2"></textarea></label>
        <div class="row"><button class="primary" data-a="release">Release payment</button><button class="secondary" data-a="hold">Keep on hold</button><button class="danger" data-a="reject">Mark as fraud</button></div>` : ""}
      <p class="error" id="derr"></p></div>`;
    $("#cbgo") && ($("#cbgo").onclick = async () => { try { await api(T(`/cases/${cid}/callbacks`), { method: "POST", json: { contact: $("#cbc").value, number: $("#cbn").value, number_source: $("#cbs").value, outcome: $("#cbo").value, notes: $("#cbt").value } }); loadDecision(); } catch (e) { $("#derr").textContent = e.message; } });
    $("#decide").querySelectorAll("[data-a]").forEach((b) => b.onclick = async () => {
      try { await api(T(`/cases/${cid}/actions`), { method: "POST", json: { action: b.dataset.a, reason: $("#why").value } }); loadDecision(); } catch (e) { $("#derr").textContent = e.message; } });
  }

  // Stream with fetch so the auth header is sent (EventSource can't send headers).
  const res = await api(T(`/cases/${cid}/stream${S.replay ? "?replay=1" : ""}`), { raw: true });
  if (!res.ok) return status("Could not open this case.");
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) { const { value, done } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf("\n\n")) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("");
      if (data) handle(JSON.parse(data)); } }
  loadDecision();
}

// ---------------------------------------------------------------- policy --
async function renderPolicy() {
  const list = await api(T("/policies")); const active = list.find((p) => p.status === "active");
  const th = active.settings.thresholds;
  view.innerHTML = `<h1>Policy</h1><p class="muted">Your company's rules. Changes are drafted, simulated on past checks, then approved by a second admin.</p>
   <div class="grid2"><section class="card"><h2>Active: v${active.version} · ${esc(active.name)}</h2>
    <dl class="kv"><dt>Hold at score</dt><dd>${active.settings.hold_at}</dd><dt>Call back at score</dt><dd>${active.settings.callback_at}</dd>
    <dt>Required checks</dt><dd>${active.settings.required_checks.join(", ")}</dd><dt>If a required check fails</dt><dd>${active.settings.fail_mode}</dd>
    <dt>New domain (high risk)</dt><dd>under ${th.domain_new_days_high} days</dd><dt>Unusual amount</dt><dd>${th.amount_ratio_high}× vendor median</dd>
    <dt>Fingerprint</dt><dd class="mono">${active.hash.slice(0, 16)}…</dd></dl>
    <h3>Rules</h3><ul class="plain">${active.rules.map((r) => `<li><b>${esc(r.description)}</b>${r.min_decision ? ` → at least ${r.min_decision}` : ""}${r.require.length ? ` · needs ${r.require.join(", ")}` : ""}</li>`).join("")}</ul></section>
   <section class="card"><h2>Propose a change</h2>${S.can("policy:edit") ? `<label>Policy (JSON)<textarea id="pj" rows="16" spellcheck="false">${esc(JSON.stringify({ name: active.name, settings: active.settings, rules: active.rules }, null, 2))}</textarea></label>
    <label>What changed and why<input id="pn"></label><div class="row"><button class="secondary" id="pd">Save draft and simulate</button></div><div id="pout"></div>` : `<p class="muted">Your role can view the policy but not change it.</p>`}</section></div>
   <section class="card"><h2>History</h2><table class="list"><tr><th>Version</th><th>Name</th><th>Status</th><th>Note</th><th></th></tr>
    ${list.map((p) => `<tr><td>v${p.version}</td><td>${esc(p.name)}</td><td>${p.status.replace("_", " ")}</td><td>${esc(p.change_note)}</td><td>
      ${p.status === "draft" && S.can("policy:edit") ? `<button class="link" data-sub="${p.version}">Submit for approval</button>` : ""}
      ${p.status === "pending_approval" && S.can("policy:approve") ? `<button class="link" data-ap="${p.version}">Approve</button> <button class="link" data-rj="${p.version}">Reject</button>` : ""}</td></tr>`).join("")}</table><p class="error" id="herr"></p></section>`;
  $("#pd") && ($("#pd").onclick = async () => {
    try { const body = { ...JSON.parse($("#pj").value), change_note: $("#pn").value };
      const d = await api(T("/policies"), { method: "POST", json: body }); const sim = await api(T(`/policies/${d.version}/simulate`), { method: "POST" });
      const m = sim.metrics;
      $("#pout").innerHTML = `<p class="ok">Draft v${d.version} saved.</p><p>Replayed against ${sim.cases} past checks: <b>${sim.changed} would change</b>${sim.newly_released.length ? `, <b class="bad">${sim.newly_released.length} would now be released</b>` : ""}.</p>
        <p class="small">Confirmed fraud stopped: ${m.current.fraud_stopped}/${m.current.fraud_cases} now → ${m.proposed.fraud_stopped}/${m.proposed.fraud_cases}. Legit payments interrupted: ${m.current.legit_interrupted}/${m.current.legit_cases} → ${m.proposed.legit_interrupted}/${m.proposed.legit_cases}.</p>`;
      setTimeout(renderPolicy, 4000);
    } catch (e) { $("#pout").innerHTML = `<p class="error">${esc(e.message)}</p>`; } });
  const act = (sel, path, body) => view.querySelectorAll(sel).forEach((b) => b.onclick = async () => {
    try { await api(T(`/policies/${Object.values(b.dataset)[0]}/${path}`), { method: "POST", json: body?.() ?? {} }); renderPolicy(); } catch (e) { $("#herr").textContent = e.message; } });
  act("[data-sub]", "submit"); act("[data-ap]", "approve"); act("[data-rj]", "reject", () => ({ reason: prompt("Why reject?") || "" }));
}

// ----------------------------------------------------------------- audit --
async function renderAudit() {
  if (!S.can("audit:read")) { view.innerHTML = `<p class="muted">Your role can't view the audit log.</p>`; return; }
  const [entries, check] = await Promise.all([api(T("/audit")), api(T("/audit/verify"))]);
  view.innerHTML = `<h1>Audit log</h1><p class="${check.ok ? "ok" : "error"}">${check.ok ? `Integrity verified: ${check.entries} entries, none changed or removed.` : `Integrity check FAILED at entry ${check.broken_at}: ${esc(check.reason)}.`}</p>
   <table class="list"><tr><th>#</th><th>When</th><th>Who</th><th>What</th><th>Target</th></tr>${entries.map((e) => `<tr><td>${e.seq}</td><td>${new Date(e.at).toLocaleString()}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td>${esc(e.target)}</td></tr>`).join("")}</table>`;
}

boot();
