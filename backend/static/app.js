// Verify-Before-Wire web app. Routes: #/checks, #/case/<id>, #/setup, #/policy, #/audit, #/new
// No inline style attributes: the CSP blocks them. Use classes, or el.style from script.
import { reviewCall, startCall } from "./call.js";
import { landingAvailable, renderLanding } from "./landing.js";
import { startTour } from "./tour.js";
import { $, $$, esc, REDUCED, sleep } from "./util.js";
const money = (ccy, n) => (n == null ? "—" : `${ccy || ""} ${Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })}`.trim());
const label = (d) => (d === "CALLBACK" ? "Call back" : d === "HOLD" ? "Hold" : d === "RELEASE" ? "Release" : d);
const STATE = { investigating: "Checking", awaiting_decision: "Needs a decision", awaiting_second_approval: "Needs a second approver",
  released: "Released", held: "On hold", rejected: "Marked as fraud" };
const SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1, info: 0 };
// Check titles and data-source names come from the server (/api/config); only UI-only steps live here.
const CHECK_NAME = { guard: "Input guard" };
const AGENT_NAME = { intake: "Read the request", verdict: "Decision" };
const SOURCES = {};
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
// Event timestamps come with microseconds; trim to milliseconds so every browser parses them.
const tsOf = (ev) => Date.parse(String(ev.ts || "").replace(/(\.\d{3})\d+/, "$1")) || Date.now();
function countUp(el, to, dur = 800) {
  if (!el) return; to = Number(to) || 0;
  if (REDUCED || !to) { el.textContent = to; return; }
  const t0 = performance.now();
  const step = (t) => { const k = Math.min(1, (t - t0) / dur); el.textContent = Math.round(to * (1 - (1 - k) ** 3)); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
function toast(text, kind = "ok") {
  const box = $("#toasts"); if (!box) return;
  const t = document.createElement("div"); t.className = `toast ${kind}`; t.setAttribute("role", "status"); t.textContent = text; box.append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 3600);
}
// Page-scoped timers and teardown: route() clears them so a page never keeps polling after you leave it.
function every(ms, fn) { const id = setInterval(fn, ms); S.cleanup.push(() => clearInterval(id)); }
const S = { cfg: null, me: null, tid: localStorage.getItem("vbw.tid"), ws: null, fb: null, fbmod: null, replay: false,
  can: () => false, cleanup: [], status: null };

// ------------------------------------------------------------------ api --
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.cfg.auth_mode === "dev") headers["X-Dev-User"] = sessionStorage.getItem("vbw.dev") || "";
  else if (S.fb?.currentUser) headers.Authorization = `Bearer ${await S.fb.currentUser.getIdToken()}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  let res;
  try { res = await fetch(path, { ...opts, headers }); }
  catch (e) { if (e.name === "AbortError") throw e; throw new Error("Can't reach Verify-Before-Wire right now. Check your connection; we'll keep trying."); }
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
  Object.assign(CHECK_NAME, S.cfg.checks); Object.assign(AGENT_NAME, CHECK_NAME); Object.assign(SOURCES, S.cfg.sources);
  if (S.cfg.auth_mode === "dev") return;
  const FIREBASE_V = "10.14.1";
  const { initializeApp } = await import(`https://www.gstatic.com/firebasejs/${FIREBASE_V}/firebase-app.js`);
  S.fbmod = await import(`https://www.gstatic.com/firebasejs/${FIREBASE_V}/firebase-auth.js`);
  S.fb = S.fbmod.getAuth(initializeApp(Object.fromEntries(Object.entries(S.cfg.firebase).filter(([, v]) => v))));
  await new Promise((r) => S.fbmod.onAuthStateChanged(S.fb, () => r(), () => r()));
}
const signedIn = () => (S.cfg.auth_mode === "dev" ? !!sessionStorage.getItem("vbw.dev") : !!S.fb?.currentUser);

// ----------------------------------------------------------- signed out --
// Signed-out visitors see the landing page; #/signin and #/signup open the auth page; an invite link
// (#invite=…) opens the auth page with the invitation shown, and is accepted right after sign-in.
let landingTeardown = null;
const inviteToken = () => location.hash.match(/invite=([\w-]+)/)?.[1] || null;
const validEmail = (e) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(e);

function showSignedOut() {
  $("#side").hidden = true;
  if (!inviteToken() && landingAvailable(location.hash)) return showLanding();
  hideLanding();
  renderAuth(/signup/.test(location.hash) ? "signup" : "signin");
}
function showLanding() {
  document.body.classList.add("lp-landing-mode");
  const root = $("#landing"); root.hidden = false;
  landingTeardown?.();
  landingTeardown = renderLanding(root, { signIn: () => (location.hash = "#/signin"), signUp: () => (location.hash = "#/signup") });
  window.scrollTo(0, 0);
}
function hideLanding() {
  landingTeardown?.(); landingTeardown = null;
  document.body.classList.remove("lp-landing-mode");
  const root = $("#landing"); if (root) { root.hidden = true; root.innerHTML = ""; }
}

// One layout for every auth screen: brand panel + form panel.
function authPage(title, sub, body) {
  view.innerHTML = `<section class="auth-wrap">
    <aside class="auth-art" aria-hidden="true"><div class="auth-art-in">
      <a class="brand" href="#/"><span class="mark"></span>Verify-Before-Wire</a>
      <h2>Verify the request.<br><span>Not the voice.</span></h2>
      <ul><li>Seven checks against your records and official registers</li><li>Your policy decides, never the AI</li><li>Every action on a tamper-evident record</li></ul>
    </div></aside>
    <div class="auth-panel"><div class="auth-form">
      <a class="auth-back" href="#/">← Home</a>
      <div id="invite-note"></div>
      <h1>${title}</h1><p class="muted">${sub}</p>${body}
      <p class="msg" id="err" role="alert"></p></div></div></section>`;
  showInvite();
}
async function showInvite() {
  const token = inviteToken(), box = $("#invite-note");
  if (!token || !box) return;
  try { const pv = await (await fetch(`/api/invites/preview?token=${encodeURIComponent(token)}`)).json();
    box.innerHTML = pv.valid ? `<div class="invite-banner"><b>You're invited to join ${esc(pv.workspace)}</b><span>as ${esc(pv.role.replace("_", " "))}. Sign in or create an account with ${esc(pv.email)}.</span></div>`
      : `<div class="invite-banner bad"><b>This invite link is no longer valid.</b><span>It was used or has expired. Ask your administrator for a new one.</span></div>`; }
  catch { /* the sign-in itself still works */ }
}

function renderAuth(mode) {
  const dev = S.cfg.auth_mode === "dev", up = mode === "signup";
  const sw = up ? `Already have an account? <a href="#/signin${inviteToken() ? `#invite=${inviteToken()}` : ""}">Sign in</a>` : `New here? <a href="#/signup${inviteToken() ? `#invite=${inviteToken()}` : ""}">Create an account</a>`;
  if (dev) {
    authPage(up ? "Create your account" : "Sign in", "Development sign-in: any email works. Production uses Firebase Authentication.",
      `<label>Work email<input id="em" name="email" type="email" autocomplete="email" spellcheck="false" placeholder="you@company.com…"></label>
       <button class="btn primary block" id="go">Continue</button><p class="small center auth-switch">${sw}</p>`);
    const go = () => { const e = $("#em").value.trim();
      if (!validEmail(e)) return show($("#err"), "Enter your work email, e.g. you@company.com.");
      sessionStorage.setItem("vbw.dev", e); boot(); };
    $("#go").onclick = go; $("#em").addEventListener("keydown", (e) => e.key === "Enter" && go());
    return;
  }
  const F = S.fbmod;
  authPage(up ? "Create your account" : "Sign in", up ? "Use your work email. We'll send a link to verify it." : "Use your work account.",
    `<button class="btn secondary block google" id="google"><span class="g" aria-hidden="true">G</span>Continue with Google</button>
     <div class="or"><span>or</span></div>
     <form id="pw" novalidate>
       <label>Work email<input id="em" name="email" type="email" autocomplete="email" spellcheck="false" required placeholder="you@company.com…"></label>
       <label>Password<input id="pwd" name="password" type="password" autocomplete="${up ? "new-password" : "current-password"}" required minlength="8" placeholder="${up ? "At least 8 characters…" : "Your password…"}"></label>
       <button class="btn primary block" type="submit" id="go">${up ? "Create account" : "Sign in"}</button>
     </form>
     <p class="small center auth-switch">${sw}${up ? "" : ` · <button class="link" id="reset">Forgot password</button>`}</p>
     <p class="muted small note">Your administrator can also turn on SAML or OIDC single sign-on.</p>`);
  $("#google").onclick = () => busy($("#google"), "Opening Google…", async () => {
    try { await F.signInWithPopup(S.fb, new F.GoogleAuthProvider()); boot(); } catch (e) { show($("#err"), authError(e)); } });
  $("#reset") && ($("#reset").onclick = async () => { const em = $("#em").value.trim();
    if (!validEmail(em)) return show($("#err"), "Enter your email above, then choose Forgot password.");
    try { await F.sendPasswordResetEmail(S.fb, em); show($("#err"), `If ${em} has an account, a reset link is on its way.`, true); } catch (e) { show($("#err"), authError(e)); } });
  $("#pw").onsubmit = (ev) => { ev.preventDefault(); busy($("#go"), up ? "Creating…" : "Signing in…", async () => {
    const em = $("#em").value.trim(), pw = $("#pwd").value;
    if (!validEmail(em)) return show($("#err"), "Enter your work email, e.g. you@company.com.");
    if (pw.length < 8) return show($("#err"), "Use at least 8 characters for the password.");
    try {
      if (up) { const c = await F.createUserWithEmailAndPassword(S.fb, em, pw); await F.sendEmailVerification(c.user); return renderVerify(); }
      const c = await F.signInWithEmailAndPassword(S.fb, em, pw);
      if (!c.user.emailVerified && !S.cfg.allow_unverified_email) return renderVerify();
      boot();
    } catch (e) { show($("#err"), authError(e)); } }); };
}
function renderVerify() {
  const F = S.fbmod, u = S.fb.currentUser;
  authPage("Check your inbox", `We sent a verification link to <b>${esc(u?.email || "your email")}</b>. Open it, then come back here.`,
    `<button class="btn primary block" id="done">I've verified my email</button>
     <p class="small center auth-switch"><button class="link" id="resend">Send the link again</button> · <button class="link" id="other">Use another account</button></p>`);
  $("#done").onclick = () => busy($("#done"), "Checking…", async () => { await u.reload(); await u.getIdToken(true);
    if (S.fb.currentUser.emailVerified) boot(); else show($("#err"), "Not verified yet. Open the link in the email first."); });
  $("#resend").onclick = async () => { try { await F.sendEmailVerification(u); show($("#err"), "Sent. Check your inbox and spam folder.", true); } catch (e) { show($("#err"), authError(e)); } };
  $("#other").onclick = async () => { await F.signOut(S.fb); renderAuth("signin"); };
}
function authError(e) {
  const c = String(e?.code || "");
  return { "auth/invalid-credential": "That email and password don't match. Try again or reset your password.",
    "auth/wrong-password": "That email and password don't match. Try again or reset your password.",
    "auth/user-not-found": "No account with that email. Create one instead.",
    "auth/email-already-in-use": "That email already has an account. Sign in instead.",
    "auth/weak-password": "Choose a stronger password (at least 8 characters).",
    "auth/too-many-requests": "Too many attempts. Wait a minute, then try again.",
    "auth/popup-closed-by-user": "The Google window was closed before signing in.",
    "auth/operation-not-allowed": "This sign-in method is switched off. An admin can enable it in Firebase Console → Authentication → Sign-in method.",
    "auth/unauthorized-domain": `This address (${location.hostname}) isn't authorised. Add it in Firebase Console → Authentication → Settings → Authorized domains.`,
    "auth/api-key-not-valid.-please-pass-a-valid-api-key.": "The Firebase web API key is wrong. Copy it from Firebase Console → Project settings → Your apps.",
    "auth/network-request-failed": "Can't reach Firebase. Check your connection and try again." }[c] || (e?.message || "Sign-in failed. Try again.");
}
$("#tour-btn").onclick = () => { if (location.hash !== "#/setup") location.hash = "#/setup"; setTimeout(() => startTour(S.ws?.name, Object.values(S.cfg.checks || {})), 300); };
$("#signout").onclick = async () => {
  sessionStorage.removeItem("vbw.dev"); if (S.fb) await S.fbmod.signOut(S.fb);
  localStorage.removeItem("vbw.tid"); S.tid = null; history.replaceState(null, "", "#/"); boot();
};

// --------------------------------------------------------------- status --
// One banner for degraded services. Nothing here blocks a payment check: AI falls back to pattern
// reading, registers report "not checked", sanctions fail closed. The banner says what, and how to fix.
let statusTimer = null;
async function pollStatus() {
  clearTimeout(statusTimer);
  let st;
  try { st = await api("/api/status"); } catch { st = { ok: false, offline: true, degraded: ["server"] }; }
  S.status = st;
  paintStatus(st);
  statusTimer = setTimeout(pollStatus, st.ok ? 60000 : 20000);
}
function paintStatus(st) {
  const ai = st.ai || {}, store = { sql: "PostgreSQL", memory: "In-memory", firestore: "Firestore" }[st.store] || st.store || "";
  const aiCls = { ok: "on", paused: "warn", error: "bad", off: "off" }[ai.state] || "off";
  const aiTxt = { ok: "AI on", paused: "AI paused", error: "AI error", off: "AI off" }[ai.state] || "AI";
  $("#sysstat").innerHTML = `${st.offline ? `<span class="bad"><i></i>Offline</span>` : `<span class="${aiCls}" title="${esc(ai.reason || `Gemini ${ai.model || ""}`)}"><i></i>${aiTxt}</span>`}
    ${store ? `<span class="${st.database?.state === "error" ? "bad" : "on"}" title="Database"><i></i>${esc(store)}</span>` : ""}`;
  const bar = $("#statusbar"); if (!bar) return;
  let dismissed = ""; try { dismissed = sessionStorage.getItem("vbw.status.dismissed") || ""; } catch { /* ignore */ }
  const key = (st.degraded || []).join(",");
  const items = [];
  if (st.offline) items.push(["bad", "Can't reach the server.", "Your work is safe; we'll reconnect automatically.", null]);
  if (st.database?.state === "error") items.push(["bad", "The database isn't answering.", "New checks are paused until it's back; nothing is lost.", null]);
  if (ai.state === "paused") items.push(["warn", `AI reading is paused: ${ai.reason || "quota reached"}.`,
    "Checks keep running: requests are read with pattern matching and voice/video with on-server forensics. Decisions never depend on AI.", ai]);
  if (ai.state === "error") items.push(["warn", `AI isn't answering (${ai.reason}).`, "Checks continue without it.", ai]);
  const notLoaded = (st.sanctions || []).filter((x) => !x.loaded);
  if (notLoaded.length) items.push([notLoaded.some((x) => x.key === "ofac") ? "bad" : "warn",
    `${notLoaded.map((x) => x.label).join(", ")} ${notLoaded.length > 1 ? "are" : "is"} still loading.`,
    notLoaded.some((x) => x.key === "ofac") ? "Until OFAC is loaded, payments are held (fail-closed)." : "Screening uses the other lists meanwhile.", null]);
  const failing = Object.entries(st.sources || {}).filter(([k, v]) => v.status === "error" && k !== "gemini");
  if (failing.length) items.push(["warn", `${failing.map(([k]) => SOURCES[k]?.name || k).join(", ")} didn't answer recently.`,
    "Affected checks report “not verified” instead of guessing.", null]);
  if (!items.length || dismissed === key) { bar.hidden = true; bar.innerHTML = ""; return; }
  bar.hidden = false;
  bar.className = `statusbar ${items.some((x) => x[0] === "bad") ? "bad" : "warn"}`;
  bar.innerHTML = `<div class="sb-items">${items.map(([lvl, t, sub, aiFix]) => `<div class="sb-item ${lvl}"><span class="sb-dot" aria-hidden="true"></span>
      <p><b>${esc(t)}</b> <span>${esc(sub)}</span></p>
      ${aiFix?.fix ? `<span class="sb-actions"><a class="btn primary sm" href="${esc(aiFix.fix.url)}" target="_blank" rel="noopener noreferrer">${esc(aiFix.fix.label)} ↗</a>
        ${S.can("tenant:manage") ? `<button class="btn secondary sm" id="ai-retry">Try AI again</button>` : ""}</span>` : ""}</div>`).join("")}</div>
    <button class="sb-close" id="sb-close" aria-label="Dismiss until something changes">×</button>`;
  $("#sb-close").onclick = () => { try { sessionStorage.setItem("vbw.status.dismissed", key); } catch { /* ignore */ } bar.hidden = true; };
  const retry = $("#ai-retry");
  if (retry) retry.onclick = () => busy(retry, "Checking…", async () => {
    try { const r = await api(T("/ai/retry"), { method: "POST" });
      toast(r.state === "ok" ? "AI is answering again" : `Still paused: ${r.reason}`, r.state === "ok" ? "ok" : "warn"); } catch (e) { toast(e.message, "err"); }
    pollStatus(); });
}
addEventListener("online", () => pollStatus());

// ----------------------------------------------------------------- boot --
async function boot() {
  if (!S.cfg) await initAuth();
  if (!signedIn()) return showSignedOut();
  hideLanding();
  const invite = inviteToken();
  if (invite) {
    history.replaceState(null, "", "#/checks");
    try { const r = await api("/api/invites/accept", { method: "POST", json: { token: invite } });
      S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid); setTimeout(() => toast("You've joined the workspace"), 400); }
    catch (e) { setTimeout(() => toast(e.message, "err"), 400); }
  }
  S.me = await api("/api/me");
  $("#who").textContent = S.me.email;
  pollStatus();
  if (!S.me.workspaces.length) { S.tid = null; $("#side").hidden = true; location.hash = "#/new"; return route(); }
  if (!S.me.workspaces.some((w) => w.tenant_id === S.tid)) S.tid = S.me.workspaces[0].tenant_id;
  localStorage.setItem("vbw.tid", S.tid);
  const sw = $("#ws-switch");
  sw.innerHTML = S.me.workspaces.map((w) => `<option value="${w.tenant_id}" ${w.tenant_id === S.tid ? "selected" : ""}>${esc(w.name)}</option>`).join("") + `<option value="__new">New workspace…</option>`;
  sw.onchange = () => { if (sw.value === "__new") location.hash = "#/new"; else { S.tid = sw.value; localStorage.setItem("vbw.tid", S.tid); route(); } };
  $("#side").hidden = false;
  route();
}
window.addEventListener("hashchange", () => {
  if (!signedIn()) return showSignedOut();
  if (inviteToken()) return boot();
  if (landingAvailable(location.hash)) { location.hash = "#/checks"; return; }
  route();
});
document.addEventListener("keydown", (e) => {
  if (e.shiftKey && e.key.toLowerCase() === "r" && !e.target.closest("input,textarea,select")) { S.replay = !S.replay; $("#mode").textContent = S.replay ? "Recorded playback is on" : ""; }
});
document.addEventListener("focusin", (e) => { if (e.target.matches("input[readonly]")) e.target.select(); });

async function route() {
  const [page, arg] = (location.hash.replace(/^#\/?/, "") || "checks").split("/");
  S.cleanup.splice(0).forEach((f) => { try { f(); } catch { /* already gone */ } });
  try {
    if (page === "new") return renderNewWorkspace();
    if (!S.tid) { location.hash = "#/new"; return; }
    S.ws = await api(T(""));
    S.can = (p) => S.ws.my_permissions.includes(p);
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("on", a.dataset.page === (page === "case" ? "checks" : page)));
    window.scrollTo(0, 0);
    if (page === "setup") {
      await renderSetup();
      let pending = null; try { pending = localStorage.getItem("vbw.tour"); } catch { /* private mode */ }
      if (pending === S.tid) { try { localStorage.removeItem("vbw.tour"); } catch { /* ignore */ } startTour(S.ws?.name, Object.values(S.cfg.checks || {})); }
      return;
    }
    if (page === "policy") return await renderPolicy();
    if (page === "audit") return await renderAudit();
    if (page === "sources") return await renderSources();
    if (page === "case") return await renderCase(arg);
    return await renderChecks();
  } catch (e) { view.innerHTML = `<div class="panel"><p class="msg err">${esc(e.message)}</p></div>`; }
}

// ------------------------------------------------------- new workspace --
async function renderNewWorkspace() {
  const templates = await (await fetch("/api/templates")).json();
  view.innerHTML = `<section class="panel center-card wide">
    <h1>Set up your workspace</h1><p class="muted">About five minutes. You can change all of this later.</p>
    <label>Company name<input id="n" name="organization" autocomplete="organization" placeholder="Acme Holdings Pte Ltd…"></label>
    <label>Your company's email domains <span class="hint">Separate with commas. Used to spot lookalike domains.</span><input id="d" name="domains" autocomplete="off" spellcheck="false" placeholder="acme.com, acme.com.sg…"></label>
    <div class="row gap-b"><label>Base currency<input id="c" name="currency" autocomplete="off" spellcheck="false" value="USD" maxlength="3"></label>
      <label>Data region<select id="r"><option value="asia-southeast1">Singapore</option><option value="asia-south1">Mumbai</option><option value="asia-northeast1">Tokyo</option><option value="europe-west1">Belgium</option><option value="us-central1">Iowa</option></select></label></div>
    <label>Time zone<input id="tz" value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}"></label>
    <fieldset><legend>Starting policy</legend><div class="choices">${templates.map((t, i) => `<label class="choice"><input type="radio" name="tpl" value="${t.id}" ${i === 0 ? "checked" : ""}><span><b>${esc(t.name)}</b><span>${esc(t.summary)}</span></span></label>`).join("")}</div></fieldset>
    <button class="btn primary block" id="go">Create workspace</button><p class="msg" id="err"></p></section>`;
  $("#go").onclick = () => busy($("#go"), "Creating…", async () => {
    try {
      const r = await api("/api/workspaces", { method: "POST", json: { name: $("#n").value, company_domains: $("#d").value.split(",").map((x) => x.trim()).filter(Boolean),
        base_currency: $("#c").value.toUpperCase(), timezone: $("#tz").value, region: $("#r").value, template: $("input[name=tpl]:checked").value } });
      S.tid = r.tenant_id; localStorage.setItem("vbw.tid", S.tid);
      try { localStorage.setItem("vbw.tour", S.tid); } catch { /* the tour is optional */ }
      history.replaceState(null, "", "#/setup"); boot();
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
    <div class="row"><label>Email<input id="ie" name="invite-email" type="email" autocomplete="off" spellcheck="false" placeholder="colleague@company.com…"></label>
      <label class="narrow">Role<select id="ir"><option value="approver">Approver</option><option value="analyst">Analyst</option><option value="clerk">Clerk</option><option value="policy_admin">Policy admin</option><option value="auditor">Auditor</option><option value="owner">Owner</option></select></label>
      <button class="btn secondary" id="inv">Create invite link</button></div><div id="invout"></div><ul class="list spaced-t" id="members"></ul></div></div>
  <div class="step ${step("integration") ? "done" : ""}" id="st-api"><div class="n">4</div><div class="panel"><h2>Connect your accounting system <span class="muted small light">(optional)</span></h2>
    <p class="muted">SAP (or any ERP) sends each payment run to <code>POST ${esc(location.origin)}/api/t/${esc(S.tid)}/erp/payment-runs</code> and keeps the payment block until this workspace says it may be lifted. Vendor master and payment history sync to <code>PUT …/erp/vendors</code> and <code>…/erp/payment-history</code>. See <code>docs/sap-integration.md</code>.</p>
    <div class="row"><label>Key name<input id="kn" name="key-name" autocomplete="off" placeholder="SAP S/4HANA production…"></label>
      <label class="narrow">Access<select id="kr"><option value="integration">ERP connector</option><option value="clerk">Submit only</option><option value="auditor">Read only</option></select></label>
      <button class="btn secondary" id="kc">Create API key</button></div><div id="kout"></div>
    ${S.can("tenant:manage") ? `<h3 class="section">Webhooks to your ERP</h3>
    <p class="muted small">We call this URL when a recommendation is ready and when someone releases, holds or rejects a payment. Every call is signed (<code>VBW-Signature</code>, HMAC-SHA256) and retried for up to 4 hours.</p>
    <div class="row"><label>Receiver URL<input id="wu" name="webhook-url" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://your-tenant.it-cpi.cfapps.eu10.hana.ondemand.com/http/vbw"></label><button class="btn secondary" id="wc">Add webhook</button></div>
    <div id="wout"></div><ul class="list spaced-t" id="hooks"></ul><div id="dlv"></div>` : ""}</div></div>
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
  $("#sample").onclick = () => busy($("#sample"), "Loading preview…", async () => {
    try { previewSample(await api(T("/sample-data/preview")), refresh); } catch (e) { $("#imp").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  $("#inv").onclick = () => busy($("#inv"), "Creating…", async () => { try { const r = await api(T("/invites"), { method: "POST", json: { email: $("#ie").value, role: $("#ir").value } });
    $("#invout").innerHTML = `<p class="msg ok">Send this link. It works once, for that address, for ${r.expires_in_days} days.</p><div class="secret"><input readonly value="${esc(r.invite_link)}"></div>`; listMembers(); refresh(); }
    catch (e) { $("#invout").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  $("#kc").onclick = () => busy($("#kc"), "Creating…", async () => { try { const r = await api(T("/api-keys"), { method: "POST", json: { name: $("#kn").value, role: $("#kr").value } });
    $("#kout").innerHTML = `<p class="msg ok">Copy this key now. It's shown only once.</p><div class="secret"><input readonly value="${esc(r.api_key)}"></div>`; refresh(); }
    catch (e) { $("#kout").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } });
  const listHooks = async () => { if (!$("#hooks")) return;
    const [hooks, dl] = await Promise.all([api(T("/webhooks")), api(T("/webhooks/deliveries?limit=8"))]);
    $("#hooks").innerHTML = hooks.map((h) => `<li><span><b>${esc(h.url)}</b><span class="muted small block">${esc(h.events.join(", "))}</span></span>
      <span class="row-actions"><button class="btn secondary sm" data-test="${esc(h.id)}">Send test</button><button class="btn ghost sm" data-del="${esc(h.id)}">Remove</button></span></li>`).join("");
    $("#dlv").innerHTML = dl.length ? `<table class="mini"><tr><th>Event</th><th>Status</th><th>Tries</th><th>Last result</th></tr>${dl.map((x) => `<tr><td>${esc(x.event)}</td><td><span class="dl-${esc(x.status)}">${esc(x.status)}</span></td><td>${x.attempts}</td><td>${esc(x.last_error || (x.response_status ? `HTTP ${x.response_status}` : ""))}</td></tr>`).join("")}</table>` : "";
    $("#hooks").querySelectorAll("[data-test]").forEach((b) => b.onclick = () => busy(b, "Sending…", async () => { try { await api(T(`/webhooks/${b.dataset.test}/test`), { method: "POST" }); toast("Test event sent"); } catch (e) { toast(e.message, "err"); } listHooks(); }));
    $("#hooks").querySelectorAll("[data-del]").forEach((b) => b.onclick = () => busy(b, "Removing…", async () => { await api(T(`/webhooks/${b.dataset.del}`), { method: "DELETE" }); toast("Webhook removed", "warn"); listHooks(); })); };
  $("#wc") && ($("#wc").onclick = () => busy($("#wc"), "Adding…", async () => { try { const r = await api(T("/webhooks"), { method: "POST", json: { url: $("#wu").value } });
    $("#wout").innerHTML = `<p class="msg ok">Webhook added. Copy the signing secret now; it's shown only once.</p><div class="secret"><input readonly value="${esc(r.secret)}"></div>`; listHooks(); }
    catch (e) { $("#wout").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; } }));
  listHooks().catch(() => {});
  const roleName = (r) => r.replace("_", " ");
  const listMembers = async () => { const m = await api(T("/members"));
    $("#members").innerHTML = m.members.map((x) => `<li><span>${esc(x.email)}</span><span class="pill">${roleName(x.role)}</span></li>`).join("") +
      m.invites.map((x) => `<li><span class="muted">${esc(x.email)}</span><span class="pill">${roleName(x.role)} · invited</span></li>`).join(""); };
  listMembers();
}

// Sample data is shown before it is saved: the user sees every vendor, verified account and payment
// summary, plus integrity checks, and chooses whether it goes into the database.
function previewSample(pv, refresh) {
  let d = $("#sampledlg");
  if (!d) { d = document.createElement("dialog"); d.id = "sampledlg"; d.className = "viewer sample-dlg"; document.body.append(d); }
  const fmt = (n, c) => money(c, n);
  d.innerHTML = `<header><h2>Sample data preview</h2><button class="btn secondary sm" id="sd-x">Close</button></header>
    <div id="viewer-body"><p class="muted">Nothing has been saved yet. This is what would be added to your workspace's database${pv.already_loaded ? ` (${pv.already_loaded} of these vendors are already there and would be updated)` : ""}.</p>
    <ul class="sd-checks">${pv.checks.map((c) => `<li class="${c.ok ? "ok" : "bad"}"><span aria-hidden="true">${c.ok ? "✓" : "✗"}</span>${esc(c.text)}</li>`).join("")}</ul>
    ${pv.vendors.map((v) => `<article class="sd-vendor"><header><b>${esc(v.legal_name)}</b><span class="pill plain">${esc(v.country)} · ${esc(v.currency)}</span></header>
      <dl class="kv"><dt>Email domains</dt><dd>${esc(v.domains.join(", "))}</dd><dt>Callback</dt><dd>${esc(v.callback_contact)} · ${esc(v.callback_phone)}</dd>
        ${v.bank_accounts.map((a) => `<dt>Verified account</dt><dd>${esc(a.bank_name)} · <code>${esc(a.account)}</code>${a.swift_bic ? ` · ${esc(a.swift_bic)}` : ""} <span class="muted small">(verified ${esc(a.verified_on)})</span></dd>`).join("")}
        <dt>Payment history</dt><dd>${v.payments.count} payments, ${esc(fmt(v.payments.total, v.currency))} total, median ${esc(fmt(v.payments.median, v.currency))} · ${esc(v.payments.first)} to ${esc(v.payments.last)}</dd></dl></article>`).join("")}
    <p class="small muted">${pv.payment_count} payments in total. Each payment request you check is compared against these accounts, amounts and dates.</p></div>
    <footer class="sd-foot"><button class="btn secondary" id="sd-cancel">Not now</button><button class="btn primary" id="sd-save" ${pv.valid ? "" : "disabled"}>Add to my workspace</button></footer>`;
  d.showModal();
  $("#sd-x").onclick = $("#sd-cancel").onclick = () => d.close();
  $("#sd-save").onclick = () => busy($("#sd-save"), "Saving…", async () => {
    try { const r = await api(T("/sample-data"), { method: "POST" }); d.close();
      const where = { sql: "PostgreSQL", firestore: "Firestore", memory: "memory (not persistent)" }[r.store] || r.store;
      $("#imp").innerHTML = `<p class="msg ok">Saved to ${esc(where)}: ${r.stored_vendors} vendors and ${r.stored_payments} payments, read back from the database.</p>`;
      toast("Sample data added"); refresh(); }
    catch (e) { toast(e.message, "err"); } });
}

// --------------------------------------------------------------- checks --
const FILTERS = [["all", "All"], ["pending", "Needs a decision"], ["HOLD", "Hold"], ["CALLBACK", "Call back"], ["RELEASE", "Release"]];
async function renderChecks() {
  const [cases, demos] = await Promise.all([api(T("/cases")), (await fetch("/api/demo")).json()]);
  view.innerHTML = `<div class="page-head"><div><h1>Payment checks</h1><p>Check a payment request before any money is released.</p></div></div>
  <div class="stats" id="stats"></div>
  <div class="checks"><section class="panel"><h2>New check</h2>
    <label class="drop" id="drop" tabindex="0"><input type="file" id="file" multiple accept=".eml,.txt,.pdf,.png,.jpg,.jpeg,.mp3,.wav,.m4a,.ogg,.webm,.mp4,.mov,.m4v">
      <span class="drop-ic" aria-hidden="true"></span><strong>Drop the payment request here</strong><span>Email (.eml), voice note, video message, invoice PDF or screenshot, up to 6 files</span></label>
    <ul class="list picked" id="picked"></ul><button class="btn primary block" id="go" disabled>Check this payment</button>
    ${demos.length ? `<h3 class="section">Or try a sample case</h3><div class="samples">${demos.map((d) => `<button class="sample" data-id="${esc(d.id)}">${esc(d.title)}<small>${esc(d.description)}</small></button>`).join("")}</div>` : ""}
    <p class="msg" id="err"></p></section>
  <section class="panel"><header class="list-head"><h2>Recent checks</h2><span class="live-badge sm" id="poll" hidden><i></i>Updating</span></header>
    <div class="list-tools"><div class="chips" id="filters">${FILTERS.map(([k, t], i) => `<button class="chip ${i ? "" : "on"}" data-f="${k}">${t}<span></span></button>`).join("")}</div>
      <input type="search" id="q" name="q" autocomplete="off" placeholder="Search payee or reference…" aria-label="Search checks"></div>
    <div id="caselist"></div></section></div>`;

  let all = cases, filter = "all", q = "";
  const match = (c) => (filter === "all" || (filter === "pending" ? ["awaiting_decision", "awaiting_second_approval"].includes(c.state) : c.decision === filter))
    && (!q || `${c.payee || ""} ${c.external_ref || ""} ${c.id}`.toLowerCase().includes(q));
  const paintStats = (first) => {
    const n = (f) => all.filter(f).length;
    const tiles = [["Checks", all.length, ""], ["Need a decision", n((c) => ["awaiting_decision", "awaiting_second_approval"].includes(c.state)), "pending"],
      ["Held", n((c) => c.decision === "HOLD"), "HOLD"], ["Call back", n((c) => c.decision === "CALLBACK"), "CALLBACK"], ["Released", n((c) => c.state === "released"), "RELEASE"]];
    $("#stats").innerHTML = tiles.map(([t, v, k]) => `<div class="stat ${k}"><small>${t}</small><b data-to="${v}">${first ? 0 : v}</b></div>`).join("");
    if (first) $("#stats").querySelectorAll("b[data-to]").forEach((b) => countUp(b, +b.dataset.to, 700));
  };
  const paintList = () => {
    const rows = all.filter(match);
    $("#filters").querySelectorAll(".chip").forEach((b) => { const k = b.dataset.f;
      $("span", b).textContent = k === "all" ? all.length : all.filter((c) => (k === "pending" ? ["awaiting_decision", "awaiting_second_approval"].includes(c.state) : c.decision === k)).length; });
    $("#caselist").innerHTML = rows.length ? `<div class="table-wrap"><table class="data cards"><thead><tr><th>Received</th><th>Payee</th><th class="num">Amount</th><th>Recommendation</th><th>Status</th></tr></thead><tbody>
    ${rows.map((c) => `<tr data-id="${esc(c.id)}"><td data-label="Received" class="nowrap">${esc(when(c.created_at))}</td><td data-label="Payee" class="payee"><a href="#/case/${esc(c.id)}">${esc(c.payee || "Reading…")}</a></td>
      <td data-label="Amount" class="num nowrap">${c.amount ? esc(money(c.currency, c.amount)) : "—"}</td><td data-label="Recommendation">${c.decision ? `<span class="reco"><span class="decision ${c.decision}">${label(c.decision)}</span>
        <span class="riskbar ${c.decision}" title="Risk score ${c.score} of 100"><i data-w="${Number(c.score) || 0}"></i></span><small>${Number(c.score) || 0}</small></span>` : `<span class="checking"><span class="spinner xs"></span>Checking…</span>`}</td>
      <td data-label="Status"><span class="state-tag ${esc(c.state)}">${esc(STATE[c.state] || c.state)}</span></td></tr>`).join("")}</tbody></table></div>`
      : all.length ? `<div class="empty"><p>No checks match this filter.</p></div>`
      : `<div class="empty"><span class="empty-ic" aria-hidden="true"></span><p>No payments checked yet.</p><p class="small">Drop a payment request on the left${demos.length ? " or run a sample case" : ""}.</p></div>`;
    paintBars($("#caselist"));
    $("#caselist").querySelectorAll("tr[data-id]").forEach((tr) => tr.onclick = (e) => { if (!e.target.closest("a")) location.hash = `#/case/${tr.dataset.id}`; });
  };
  paintStats(true); paintList();
  $("#filters").onclick = (e) => { const b = e.target.closest(".chip"); if (!b) return; filter = b.dataset.f;
    $("#filters").querySelectorAll(".chip").forEach((x) => x.classList.toggle("on", x === b)); paintList(); };
  $("#q").oninput = (e) => { q = e.target.value.trim().toLowerCase(); paintList(); };
  // Keep the list live while any check is still running.
  const poll = async () => {
    const running = all.some((c) => !c.decision && c.state === "investigating");
    $("#poll").hidden = !running;
    if (!running) return;
    try { const fresh = await api(T("/cases"));
      const changed = JSON.stringify(fresh.map((c) => [c.id, c.decision, c.state])) !== JSON.stringify(all.map((c) => [c.id, c.decision, c.state]));
      all = fresh; if (changed) { paintStats(false); paintList(); } } catch { /* next tick */ }
  };
  every(3000, poll); poll();

  let files = [];
  const pick = (list) => { files = [...files, ...list].slice(0, 6);
    $("#picked").innerHTML = files.map((f, i) => `<li><span>${esc(f.name)}</span><span class="muted">${Math.ceil(f.size / 1024)} KB <button class="link" data-rm="${i}" aria-label="Remove ${esc(f.name)}">Remove</button></span></li>`).join("");
    $("#picked").querySelectorAll("[data-rm]").forEach((b) => b.onclick = () => { files.splice(+b.dataset.rm, 1); pick([]); });
    $("#go").disabled = !files.length; };
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
  return `<header><div><span class="eyebrow">Why this decision</span><h2>How this decision was made</h2></div><span class="by">Calculated by your policy rules. No AI involved in the decision. Click a signal to see its evidence.</span></header>
  <div class="explain-grid"><div>
    <h3>Risk score</h3>
    <div class="meter" role="img" aria-label="Risk score ${score} of 100. Call back from ${cb}, hold from ${hold}.">
      <div class="zones"><span class="z RELEASE" data-w="${cb}"><b>Release</b></span><span class="z CALLBACK" data-w="${hold - cb}"><b>Call back</b></span><span class="z HOLD" data-w="${100 - hold}"><b>Hold</b></span></div>
      <div class="pin ${zoneOf(score)}" data-left="${Math.min(97, Math.max(3, score))}"><b>${score}</b></div>
      <div class="ticks"><span data-left="0">0</span><span data-left="${cb}">${cb}</span><span data-left="${hold}">${hold}</span><span data-left="100">100</span></div>
    </div>
    <h3>What added to the score</h3>
    ${parts.length ? `<div class="stack" aria-hidden="true">${parts.map((p) => `<span class="${p.cls}" data-w="${(p.points / scale) * 100}" title="${esc(p.title)}: +${p.points}"></span>`).join("")}</div>
      ${(() => { const row = (p) => `<li ${p.agent === "policy" ? "" : `data-jump="${esc(p.title)}" role="button" tabindex="0" title="Show this finding"`}><span class="sw ${p.cls}"></span><span class="t">${esc(p.title)}<small>${esc(CHECK_NAME[p.agent] || (p.agent === "policy" ? "Policy rule" : p.agent))}</small></span><b>+${p.points}</b></li>`;
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

const KIND_TEXT = { live: "Live internet source", ai: "AI model", workspace: "Your workspace data", local: "Analysed on this server" };
const SRC_STATUS = { calling: "Querying…", ok: "Answered", cached: "Answered from cache", error: "Unavailable", paused: "Paused, fallback used", unused: "Not needed", off: "Switched off" };
const NS = "http://www.w3.org/2000/svg";
const fmtMs = (ms) => (ms < 1 ? "<1 ms" : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms} ms`);

// Live map of one investigation: data sources ⇄ checks → policy → recommendation.
// Nodes are HTML; the edges are an SVG overlay recomputed from the nodes' positions.
function liveGraph(root) {
  const svg = $("#edges", root), graph = $("#graph", root);
  const edges = new Map(), nodes = { src: {}, ag: {} };
  const node = (col, id, cls, title, sub, tag = "div") => {
    const el = document.createElement(tag); el.className = `gnode ${cls}`; el.dataset.id = id;
    el.innerHTML = `<span class="ic" aria-hidden="true"></span><span class="gt"><b>${esc(title)}</b><small class="sub">${esc(sub)}</small></span><span class="ms"></span>`;
    $(col, root).append(el); requestAnimationFrame(() => el.classList.add("in")); return el;
  };
  const anchor = (el, side) => { const g = graph.getBoundingClientRect(), r = el.getBoundingClientRect();
    return side === "b" ? { x: r.left + r.width / 2 - g.left, y: r.bottom - g.top } : side === "t" ? { x: r.left + r.width / 2 - g.left, y: r.top - g.top }
      : { x: (side === "r" ? r.right : r.left) - g.left, y: r.top + r.height / 2 - g.top }; };
  const shape = (e) => { const vertical = e.sides[0] === "b";
    const a = anchor(e.a, e.sides[0]), b = anchor(e.b, e.sides[1]);
    if (vertical) return `M${a.x},${a.y} L${b.x},${b.y}`;
    const dx = Math.max(24, (b.x - a.x) / 2); return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`; };
  const layout = () => { const g = graph.getBoundingClientRect(); svg.setAttribute("viewBox", `0 0 ${g.width} ${g.height}`);
    svg.setAttribute("width", g.width); svg.setAttribute("height", g.height); edges.forEach((e) => e.path.setAttribute("d", shape(e))); };
  const ro = new ResizeObserver(layout); ro.observe(graph);
  const edge = (key, a, b, sides, cls = "") => {
    let e = edges.get(key);
    if (!e) { const path = document.createElementNS(NS, "path"); path.setAttribute("class", `edge ${cls}`); svg.prepend(path);
      e = { a, b, sides, path }; edges.set(key, e); path.setAttribute("d", shape(e));
      const len = path.getTotalLength(); path.style.strokeDasharray = `${len}`; path.style.strokeDashoffset = `${len}`;
      requestAnimationFrame(() => { path.style.transition = "stroke-dashoffset .5s ease-out"; path.style.strokeDashoffset = "0";
        setTimeout(() => { path.style.strokeDasharray = ""; path.style.transition = ""; }, 550); }); }
    return e; };
  const packet = (e, reverse, cls) => {
    if (REDUCED) return;
    const c = document.createElementNS(NS, "circle"); c.setAttribute("r", "4.5"); c.setAttribute("class", `pkt ${cls}`); svg.append(c);
    const t0 = performance.now(), dur = 700;
    const step = (t) => { const k = Math.min(1, (t - t0) / dur), e2 = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      const len = e.path.getTotalLength(), p = e.path.getPointAtLength((reverse ? 1 - e2 : e2) * len);
      c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); if (k < 1) requestAnimationFrame(step); else c.remove(); };
    requestAnimationFrame(step); };
  return { node, edge, packet, layout, nodes, edges, stop: () => ro.disconnect() };
}

async function renderCase(cid) {
  view.innerHTML = `<a class="back" href="#/checks">← All payment checks</a>
  <div class="case-head" id="casehead" hidden><h1 id="ch-title"></h1><p id="ch-sub"></p></div>
  <section class="panel verdict pending" id="verdict"><div class="progress"><span class="spinner"></span><span id="status">Starting the investigation…</span><span class="muted small" id="tally"></span></div></section>
  <section class="panel live" id="live">
    <header class="live-head"><div><h2>Investigation, live</h2><p class="muted small flush" id="live-sub">Connecting to the event stream…</p></div>
      <div class="live-stats"><span class="kpi"><b id="k-ev">0</b><small>events</small></span><span class="kpi"><b id="k-calls">0</b><small>source calls</small></span>
        <span class="kpi"><b id="k-slow">—</b><small>slowest source</small></span><span class="kpi"><b id="k-t">0.0s</b><small>elapsed</small></span>
        <span class="live-badge" id="live-badge"><i></i><span id="live-word">Connecting</span></span>
        <button class="btn secondary sm" id="skip" hidden>Skip replay</button><button class="btn secondary sm" id="again" hidden>Replay</button></div></header>
    <div class="graph" id="graph"><svg class="edges" id="edges" aria-hidden="true"></svg>
      <div class="gcol"><h4>Data sources</h4><div class="gnodes" id="g-src"></div></div>
      <div class="gcol"><h4>Checks</h4><div class="gnodes" id="g-ag"></div></div>
      <div class="gcol"><h4>Decision</h4><div class="gnodes" id="g-out"></div></div></div>
    <div class="legend"><span class="lg lg-live">Live internet</span><span class="lg lg-ai">AI model</span><span class="lg lg-workspace">Your data</span><span class="lg lg-local">On this server</span>
      <span class="lg pk-ok">Answer</span><span class="lg pk-cached">From cache</span><span class="lg pk-error">Unavailable</span></div>
    <div class="live-grid">
      <div class="wf-wrap"><h3 class="eyebrow">Source timeline</h3><div class="wf" id="wf"><p class="muted small" id="wf-empty">Each lookup appears here the moment a check makes it.</p></div><div class="wf-axis" id="wf-axis"></div></div>
      <div class="console-wrap"><div class="console-head"><h3 class="eyebrow">Event stream</h3>
        <div class="chips sm" id="cfilter"><button class="chip on" data-f="all">All</button><button class="chip" data-f="source">Sources</button><button class="chip" data-f="check">Checks</button><button class="chip" data-f="finding">Findings</button></div></div>
        <ol class="console" id="console" aria-live="off"></ol></div></div>
  </section>
  <section class="panel explain" id="explain" hidden></section>
  <div class="case"><section class="panel"><header><h2>Evidence</h2><span class="muted small" id="policyname"></span></header><div id="checks"></div></section>
    <aside><section class="panel decide" id="decide" hidden></section>
      <section class="panel" id="slip" hidden><header><h2>The request</h2><span class="by" id="slip-by"></span></header><dl class="kv" id="slip-fields"></dl><p class="notice" id="slip-notes" hidden></p></section>
      <section class="panel" id="inputs" hidden><h2>What was received</h2><ul class="list inputs" id="input-list"></ul><div id="player"></div></section>
      <section class="panel" id="activity" hidden><header><h2>Case activity</h2><a class="small" href="#/audit">Audit log →</a></header><ol class="act" id="act-list"></ol></section></aside></div>
  <dialog class="viewer" id="viewer"><header><h2 id="viewer-title"></h2><button class="btn secondary sm" id="viewer-close">Close</button></header><div id="viewer-body"></div></dialog>`;

  const checks = {}, texts = {}, blobs = {};
  let total = 0, finished = 0, request = null;
  const c0 = await api(T(`/cases/${cid}`)).catch(() => null);
  const sources = c0?.sources || [];
  sources.forEach((s) => { if (s.text) texts[s.label] = s.text; });

  // ---- inputs + viewer
  const nice = (l) => l.replaceAll("_", " ");
  const kindOf = (s) => (s.mime_type.startsWith("audio/") ? "audio" : s.mime_type.startsWith("video/") ? "video" : s.mime_type === "application/pdf" ? "pdf" : s.mime_type.startsWith("image/") ? "image" : "text");
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
      $("#viewer-body").innerHTML = k === "pdf" ? `<iframe class="pdf" src="${url}" title="${esc(s.filename)}"></iframe>`
        : k === "video" ? `<video class="clip" controls autoplay src="${url}"></video>` : `<img class="shot" src="${url}" alt="${esc(s.filename)}">`;
      dlg.showModal();
    });
  };
  if (sources.length) {
    const verb = { audio: "Play", video: "Watch", pdf: "Open", image: "Open", text: "Read" };
    $("#input-list").innerHTML = sources.map((s, i) => `<li><span><b>${esc(nice(s.label))}</b><span class="muted small block">${esc(s.filename)}</span></span>
      <button class="btn secondary sm" data-src="${i}">${verb[kindOf(s)]}</button></li>`).join("");
    $("#input-list").querySelectorAll("[data-src]").forEach((b) => b.onclick = () => openSource(sources[+b.dataset.src], b));
    $("#inputs").hidden = false;
  }

  // ---- live graph
  const G = liveGraph($("#live"));
  S.cleanup.push(G.stop);
  const srcState = {}, agNode = (id) => G.nodes.ag[id] || (G.nodes.ag[id] = G.node("#g-ag", id, `ag waiting ag-${id}`, AGENT_NAME[id] || id, "Waiting", "button"));
  let catalog = SOURCES, policyNode = null, outNode = null;
  const buildGraph = (agents, cat, policy, gemini) => {
    $("#g-src").innerHTML = ""; $("#g-ag").innerHTML = ""; $("#g-out").innerHTML = ""; $("#edges").innerHTML = "";
    G.edges.clear(); G.nodes.src = {}; G.nodes.ag = {};
    catalog = { ...SOURCES, ...(cat || {}) };
    Object.entries(catalog).forEach(([id, s]) => { srcState[id] = { calls: 0, open: 0, worst: null, ms: 0, detail: "" };
      G.nodes.src[id] = G.node("#g-src", id, `src idle k-${s.kind}`, s.name, KIND_TEXT[s.kind] || ""); });
    if (gemini === false) setSrc("gemini", "off", "No API key set, so pattern matching is used instead");
    const intake = agNode("intake"); $(".sub", intake).textContent = sources.length ? sources.map((s) => nice(s.label)).join(", ") : "Waiting";
    agents.forEach((a) => agNode(a.id));
    Object.entries(G.nodes.ag).forEach(([id, n]) => { if (id !== "intake") n.onclick = () => checks[id]?.scrollIntoView({ behavior: "smooth", block: "start" }); });
    policyNode = G.node("#g-out", "policy", "policy idle", policy ? `Policy v${policy.version}` : "Your policy", policy?.name || "Waits for every check");
    outNode = G.node("#g-out", "outcome", "outcome idle", "Recommendation", "Pending");
    G.edge("policy>out", policyNode, outNode, ["b", "t"], "flow");
  };
  const setSrc = (id, status, detail, ms) => {
    const n = G.nodes.src[id]; if (!n) return;
    n.className = `gnode src in k-${catalog[id]?.kind} s-${status}`;
    const st = srcState[id];
    $(".sub", n).textContent = detail || SRC_STATUS[status] || "";
    $(".sub", n).title = detail || "";
    $(".ms", n).textContent = ms != null && status !== "calling" ? `${fmtMs(ms)}${st?.calls > 1 ? ` · ${st.calls}×` : ""}` : "";
    if (["ok", "cached", "error", "paused"].includes(status)) { n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); }
  };

  // ---- source timeline (waterfall) and KPIs
  const calls = {}; let t0 = null, tEnd = null, running = true, lastTs = null, slow = 0;
  const OPENED = Date.now();
  const nowTs = () => (running && liveMode ? Math.max(Date.now(), lastTs || 0) : lastTs || Date.now());
  let liveMode = true;
  const paintWf = () => {
    if (t0 == null) return;
    const end = Math.max(tEnd || nowTs(), t0 + 500), span = end - t0;
    Object.values(calls).forEach((c) => { const stop = c.end ?? nowTs();
      c.bar.style.left = `${((c.start - t0) / span) * 100}%`; c.bar.style.width = `${Math.max(.6, ((stop - c.start) / span) * 100)}%`; });
    const step = span > 6000 ? 2000 : span > 2500 ? 1000 : 500;
    let ticks = ""; for (let t = 0; t <= span; t += step) ticks += `<span data-l="${(t / span) * 100}">${(t / 1000).toFixed(t % 1000 ? 1 : 0)}s</span>`;
    const ax = $("#wf-axis"); if (ax) { ax.innerHTML = ticks; ax.querySelectorAll("span").forEach((s) => { s.style.left = `${s.dataset.l}%`; }); }
    const kt = $("#k-t"); if (kt) kt.textContent = `${((end - t0) / 1000).toFixed(1)}s`;
  };
  const tick = () => { if (!view.contains($("#live"))) return; paintWf(); if (running) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
  const wfRow = (c) => {
    $("#wf-empty")?.remove();
    const row = document.createElement("div"); row.className = `wf-row s-calling ag-${c.agent}`;
    row.innerHTML = `<span class="wf-l"><b>${esc(catalog[c.source]?.name || c.source)}</b><small title="${esc(c.target)}">${esc(AGENT_NAME[c.agent] || c.agent || "")} · ${esc(c.target)}</small></span>
      <span class="wf-track"><i class="wf-bar"></i></span><span class="wf-ms">…</span>`;
    $("#wf").append(row); c.row = row; c.bar = $(".wf-bar", row);
  };

  // ---- event console
  const cons = $("#console");
  $("#cfilter").onclick = (e) => { const b = e.target.closest(".chip"); if (!b) return;
    $("#cfilter").querySelectorAll(".chip").forEach((x) => x.classList.toggle("on", x === b)); cons.dataset.f = b.dataset.f; };
  const TAG = { case: "CASE", intake: "INTAKE", agent: "CHECK", source: "SOURCE", finding: "FINDING", verdict: "VERDICT" };
  const say = (ev) => { const d = ev.data || {}, who = AGENT_NAME[ev.agent] || ev.agent || "", sn = (id) => catalog[id]?.name || id;
    switch (ev.type) {
      case "case.started": return `Investigation started · policy v${d.policy?.version ?? "?"} · ${(d.agents || []).length} checks`;
      case "intake.started": return `Reading ${d.inputs.length} input${d.inputs.length > 1 ? "s" : ""}: ${d.inputs.map((i) => i.filename).join(", ")}`;
      case "intake.completed": return `Request ${d.read_by === "erp" ? "received from your ERP" : `read by ${d.read_by === "gemini" ? "Gemini" : "pattern matching"}`}: ${d.request?.amount != null ? money(d.request.currency, d.request.amount) : "amount unknown"} to ${d.request?.beneficiary_name || "unknown payee"}`;
      case "agent.started": return `${who} started`;
      case "agent.completed": return `${who} finished in ${d.ms} ms · ${d.findings} finding${d.findings === 1 ? "" : "s"}`;
      case "agent.failed": return `${who} could not run: ${d.error}`;
      case "source.call": return `${who} → ${sn(d.source)} · ${d.target}`;
      case "source.done": return `${sn(d.source)} → ${who} · ${d.status} in ${d.ms} ms${d.detail ? ` · ${d.detail}` : ""}`;
      case "finding": return `${(d.severity || "").toUpperCase()} · ${d.title}`;
      case "verdict": return `${DEC_TEXT[d.decision]} · risk ${d.risk_score}/100`;
      case "case.completed": return `Completed in ${(d.ms / 1000).toFixed(1)}s`;
      case "case.failed": return `Stopped: ${d.error}`;
      default: return ev.type; } };
  const logEv = (ev) => {
    const kind = ev.type.split(".")[0], fam = kind === "agent" ? "check" : kind;
    const li = document.createElement("li");
    li.className = `t-${fam} e-${ev.type.replace(".", "-")} ${ev.type === "finding" ? `sev-${ev.data?.severity}` : ""} ${ev.type === "source.done" ? `st-${ev.data?.status}` : ""} ${ev.type === "verdict" ? `v-${ev.data?.decision}` : ""}`;
    const t = t0 != null ? `+${((tsOf(ev) - t0) / 1000).toFixed(2)}s` : "+0.00s";
    li.innerHTML = `<details><summary><span class="ct">${t}</span><span class="tag">${TAG[kind] || kind.toUpperCase()}</span><span class="cm">${esc(say(ev))}</span></summary><pre>${esc(JSON.stringify(ev, null, 2))}</pre></details>`;
    const stick = cons.scrollHeight - cons.scrollTop - cons.clientHeight < 40;
    cons.append(li); if (stick) cons.scrollTop = cons.scrollHeight;
    $("#k-ev").textContent = cons.children.length;
  };

  // ---- evidence lanes
  const lane = (id, title) => { if (!checks[id]) { const el = document.createElement("article"); el.className = "check waiting"; el.id = `lane-${id}`;
      el.innerHTML = `<header><h3><span class="icon" aria-hidden="true"></span>${esc(title)}</h3><span class="state">Waiting</span></header><div class="flags"></div><details class="passed" hidden><summary></summary><ul></ul></details>`;
      $("#checks").append(el); checks[id] = el; agNode(id); } return checks[id]; };
  const setState = (a, cls, t) => { const el = checks[a]; if (el) { el.className = `check ${cls}`; $(".state", el).textContent = t; }
    const n = G.nodes.ag[a]; if (n) { n.className = `gnode ag in ag-${a} ${cls}`; $(".sub", n).textContent = t; } };
  const status = (t) => { const s = $("#status"); if (s) s.textContent = t; };
  const tally = () => { const t = $("#tally"); if (t && total) t.textContent = `${finished} of ${total} checks done`; };
  const addFinding = (el, f) => {
    if (f.meta?.lists?.length && !$(".lists", el)) {      // which list versions were screened: part of the evidence
      const p = document.createElement("p"); p.className = "lists";
      p.innerHTML = f.meta.lists.map((l) => `<span class="${l.screened === false ? "off" : ""}" title="${esc(l.sha256 ? `SHA-256 ${l.sha256}` : l.error || "")}">${esc(l.label)}${l.published ? ` · ${esc(l.published)}` : ""}${l.entries ? ` · ${Number(l.entries).toLocaleString()} names` : ""}${l.screened === false ? " · not loaded" : ""}</span>`).join("");
      $("header", el).after(p);
    }
    if (f.severity === "info") { const d = $(".passed", el); d.hidden = false; const li = document.createElement("li"); li.textContent = f.title; $("ul", d).append(li);
      $("summary", d).textContent = `${$("ul", d).children.length} passed`; return; }
    const item = document.createElement("div"); item.className = `finding sev-${f.severity}`; item.dataset.rank = SEV_RANK[f.severity]; item.dataset.title = f.title;
    const ai = /gemini/i.test(f.data_source);
    const quotes = (f.evidence || []).filter((e) => e.excerpt).slice(0, 2).map((e) => {
      const canShow = texts[e.source] || (e.source.startsWith("voice_note") && request?.transcript);
      return `<q>${esc(e.excerpt)}</q><cite>${e.source === f.data_source ? "" : `From the ${esc(nice(e.source))}`}${canShow ? ` <button class="link" data-show="${esc(e.source)}" data-q="${esc(e.excerpt)}">Show in ${esc(nice(e.source))}</button>` : ""}</cite>`; }).join("");
    item.innerHTML = `<b><span class="sev">${esc(f.severity)}</span>${esc(f.title)}${ai ? ` <span class="ai" title="Found by Gemini; the quote was verified against the source">AI</span>` : ""}</b><p>${esc(f.detail)}</p>
      ${f.meta?.domain && f.meta?.imitates ? domainDiff(f.meta.domain, f.meta.imitates) : ""}${quotes}<cite class="src-cite">Checked against: <span>${esc(f.data_source)}</span></cite>
      ${f.meta?.url ? `<cite class="src-cite"><a href="${esc(f.meta.url)}" target="_blank" rel="noopener noreferrer">Open the register entry ↗</a></cite>` : ""}`;
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
    $("#slip-by").textContent = { gemini: "Read by Gemini", patterns: "Read with patterns", erp: "From your ERP" }[by] || "";
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
      <div class="score"><b id="v-score">0</b><span>risk score out of 100</span><button class="btn secondary sm" id="pdf">Download case file</button></div></div>
      <div class="v-body"><p class="summary">${esc(v.summary)}</p>${v.narrative_by === "gemini" ? `<p class="by ai-by">Summary written by Gemini. The decision itself comes from your policy rules.</p>` : ""}
        ${why.length ? `<h3>Main reasons</h3><ul class="reasons">${why.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${(v.fired_rules || []).length ? `<h3>Your policy requires</h3><ul class="rules">${v.fired_rules.map((r) => `<li>${esc(r.description)}</li>`).join("")}</ul>` : ""}
        <a class="why-link" href="#explain" id="whylink">Why this decision? See the full reasoning ↓</a></div>`;
    countUp($("#v-score"), v.risk_score, 900);
    $("#whylink").onclick = (e) => { e.preventDefault(); $("#explain").scrollIntoView({ behavior: "smooth", block: "start" }); };
    const ex = $("#explain"); ex.innerHTML = explainCard(v); ex.hidden = !ex.innerHTML; paintBars(ex);
    ex.querySelectorAll(".path li").forEach((li, i) => { li.style.animationDelay = `${300 + i * 260}ms`; });
    const pinScore = $(".pin b", ex); if (pinScore) countUp(pinScore, Number(pinScore.textContent), 900);
    ex.querySelectorAll("[data-jump]").forEach((li) => li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); li.click(); } });
    ex.querySelectorAll("[data-jump]").forEach((li) => li.onclick = () => {
      const f = [...document.querySelectorAll(".finding")].find((x) => x.dataset.title === li.dataset.jump);
      if (f) { f.scrollIntoView({ behavior: "smooth", block: "center" }); f.classList.remove("flash"); void f.offsetWidth; f.classList.add("flash"); } });
    if (policyNode) { policyNode.className = "gnode policy in done"; $(".sub", policyNode).textContent = `${(v.explain?.path || []).length || 1} step${(v.explain?.path || []).length === 1 ? "" : "s"} applied`; }
    if (outNode) { outNode.className = `gnode outcome in ${v.decision}`; $(".gt b", outNode).textContent = DEC_TEXT[v.decision]; $(".sub", outNode).textContent = `Risk ${v.risk_score}/100`;
      const e = G.edges.get("policy>out"); if (e) { e.path.setAttribute("class", `edge flow on ${v.decision}`); G.packet(e, false, v.decision); } }
    $("#pdf").onclick = () => busy($("#pdf"), "Preparing…", async () => { const res = await api(T(`/cases/${cid}/report.pdf`), { raw: true }); if (!res.ok) return toast("The case file couldn't be prepared.", "err");
      const url = URL.createObjectURL(await res.blob()); const a = document.createElement("a"); a.href = url; a.download = `case-${cid}.pdf`; a.click(); toast("Case file downloaded"); loadActivity(); }); };
  const titleOf = (id) => ({ guard: "Input guard" }[id] || id);

  const handle = (ev) => { const d = ev.data || {}; const ts = tsOf(ev); lastTs = ts;
    if (ev.type === "case.started") t0 = ts;
    logEv(ev);
    switch (ev.type) {
      case "case.started": $("#checks").innerHTML = ""; for (const k in checks) delete checks[k];
        buildGraph(d.agents || [], d.sources, d.policy, d.gemini);
        (d.agents || []).forEach((a) => lane(a.id, a.title));
        total = (d.agents || []).length; finished = 0; tally();
        $("#policyname").textContent = d.policy ? `Policy v${d.policy.version}` : ""; break;
      case "intake.started": status(`Reading ${d.inputs.length} file${d.inputs.length > 1 ? "s" : ""}…`); setState("intake", "running", "Reading the files…"); break;
      case "intake.completed": showSlip(d.request, d.notes, d.read_by); status("Running checks in parallel…");
        setState("intake", "done clear", { gemini: "Read by Gemini", erp: "Structured data from your ERP" }[d.read_by] || "Read with patterns"); break;
      case "agent.started": if (ev.agent === "verdict") { status("Applying your policy…"); if (policyNode) { policyNode.className = "gnode policy in running"; $(".sub", policyNode).textContent = "Weighing the evidence…"; } }
        else setState(ev.agent, "running", "Checking…"); break;
      case "agent.completed": { const el = checks[ev.agent]; const flags = el ? $(".flags", el).children.length : 0;
        setState(ev.agent, flags ? "done flagged" : "done clear", flags ? `${flags} red flag${flags > 1 ? "s" : ""}` : "No red flags"); finished++; tally();
        const n = G.nodes.ag[ev.agent]; if (n && policyNode) { const e = G.edge(`${ev.agent}>policy`, n, policyNode, ["r", "l"], `flow ${flags ? "flagged" : "clear"}`); G.packet(e, false, flags ? "error" : "ok"); }
        if (n) $(".ms", n).textContent = fmtMs(d.ms); break; }
      case "agent.failed": setState(ev.agent, "failed", d.required ? "Couldn't run, release blocked" : "Couldn't run"); finished++; tally();
        { const n = G.nodes.ag[ev.agent]; if (n && policyNode) G.edge(`${ev.agent}>policy`, n, policyNode, ["r", "l"], "flow failed"); } break;
      case "source.call": { const ag = ev.agent || "intake", st = srcState[d.source] || (srcState[d.source] = { calls: 0, open: 0 });
        st.calls++; st.open++; const c = calls[d.call] = { ...d, agent: ag, start: ts, end: null }; wfRow(c);
        setSrc(d.source, "calling", `${d.target}`); const an = agNode(ag), sn = G.nodes.src[d.source];
        if (an && sn) { const e = G.edge(`${d.source}>${ag}`, sn, an, ["r", "l"], "call"); e.path.setAttribute("class", "edge call on"); G.packet(e, true, "out"); }
        $("#k-calls").textContent = Object.keys(calls).length; break; }
      case "source.done": { const c = calls[d.call]; const ag = c?.agent || ev.agent || "intake", st = srcState[d.source] || {};
        st.open = Math.max(0, (st.open || 1) - 1);
        if (c) { c.end = ts; c.row.className = `wf-row s-${d.status} ag-${ag}`; $(".wf-ms", c.row).textContent = fmtMs(d.ms); c.row.title = d.detail || ""; }
        if (st.open === 0) setSrc(d.source, d.status, d.detail, d.ms);
        const e = G.edges.get(`${d.source}>${ag}`); if (e) { e.path.setAttribute("class", `edge call done ${d.status}`); G.packet(e, false, d.status); }
        if (d.ms > slow) { slow = d.ms; $("#k-slow").textContent = fmtMs(d.ms); $("#k-slow").title = catalog[d.source]?.name || ""; } break; }
      case "finding": addFinding(lane(ev.agent, titleOf(ev.agent)), d); break;
      case "verdict": showVerdict(d); break;
      case "case.completed": case "case.failed": case "case.stalled":
        running = false; tEnd = ts;
        Object.entries(srcState).forEach(([id, st]) => { if (!st.calls && !G.nodes.src[id]?.className.includes("s-off")) setSrc(id, "unused", "Not needed for this request"); });
        if (ev.type === "case.failed") status(`The investigation stopped: ${d.error}. Don't release this payment.`);
        if (ev.type === "case.stalled") status("The investigation stopped responding. Reload the page to retry.");
        finishLive(ev.type === "case.completed"); break;
    } };
  const finishLive = (ok) => { $("#live h2").textContent = "How the investigation ran"; $("#live-badge").className = `live-badge ${ok ? "done" : "bad"}`; $("#live-word").textContent = ok ? "Complete" : "Stopped";
    $("#live-sub").textContent = `${cons.children.length} events streamed · ${Object.keys(calls).length} data-source lookups`; $("#skip").hidden = true; $("#again").hidden = false; paintWf(); };
  $("#again").onclick = () => { S.cleanup.splice(0).forEach((f) => f()); renderCase(cid); };

  // Stored events of a finished case arrive in one burst: pace them by their real timing (capped)
  // so the map replays the investigation instead of jumping to the end.
  const queue = []; let pumping = false, speed = 1, prev = null;
  $("#skip").onclick = () => { speed = 0; $("#skip").hidden = true; };
  const pump = async () => { pumping = true;
    while (queue.length) { const ev = queue.shift(), ts = tsOf(ev);
      const old = ts < OPENED - 1500;
      if (old) { liveMode = false; if (prev != null && speed && !REDUCED) await sleep(Math.min(420, Math.max(30, ts - prev)) * speed); }
      prev = ts;
      if (!view.contains($("#live"))) return;
      handle(ev); }
    pumping = false; };
  const enqueue = (ev) => { queue.push(ev); if (!pumping) pump(); };

  async function loadActivity() {
    if (!S.can("audit:read")) return;
    try { const entries = (await api(T("/audit?limit=1000"))).filter((e) => e.target === `case/${cid}`);
      $("#act-list").innerHTML = entries.map((e) => `<li class="cat-${auditCat(e.action)}"><span class="dot"></span><div><b>${esc(actionText(e.action))}</b>
        <small>${esc(actorName(e.actor))} · ${esc(when(e.at))}${e.detail?.decision ? ` · ${esc(DEC_TEXT[e.detail.decision] || e.detail.decision)} ${e.detail.score ?? ""}` : ""}${e.detail?.reason ? ` · “${esc(e.detail.reason)}”` : ""}</small></div></li>`).join("");
      $("#activity").hidden = !entries.length; } catch { /* optional panel */ }
  }

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
      ${v.decision !== "RELEASE" && pending ? `${v.callback_number ? `<div class="callto"><span>Call ${esc(v.callback_contact)}</span><b>${esc(v.callback_number)}</b><small>From your vendor list, not from the request.</small>
          ${S.can("callback:record") ? `<button class="btn primary sm ai-call" id="aicall"><span class="ai">AI</span> Let the AI agent call and record it</button>` : ""}</div>` : ""}
        <details class="script-box"><summary>What to say on the call${v.narrative_by === "gemini" ? ` <span class="ai">AI</span>` : ""}</summary><ol class="script">${v.callback_script.map((l) => `<li>${esc(l)}</li>`).join("")}</ol></details>` : ""}
      ${pending && S.can("callback:record") ? `<details class="form"><summary>Record a callback</summary>
        <label>Who you spoke to<input id="cbc" name="callback-contact" autocomplete="off" value="${esc(v.callback_contact || "")}"></label><label>Number you dialled<input id="cbn" name="callback-number" type="tel" inputmode="tel" autocomplete="off" value="${esc(v.callback_number || "")}"></label>
        <label>Where the number came from<select id="cbs"><option value="vendor_master">Vendor list</option><option value="company_directory">Company directory</option><option value="other">Somewhere else</option></select></label>
        <label>What they said<select id="cbo"><option value="confirmed">Confirmed the request</option><option value="denied">Said they didn't send it</option><option value="no_answer">No answer</option></select></label>
        <label>Notes<textarea class="prose" id="cbt" rows="2"></textarea></label><button class="btn secondary block" id="cbgo">Save callback</button></details>` : ""}
      ${pending && S.can("case:decide") ? `<label>Reason <span class="hint">Required to release a held payment</span><textarea class="prose" id="why" rows="2"></textarea></label>
        <div class="actions"><button class="btn primary" data-a="release">Release payment</button><button class="btn secondary" data-a="hold">Keep on hold</button><button class="btn danger" data-a="reject">Mark as fraud</button></div>` : ""}
      ${pending && !S.can("case:decide") ? `<p class="muted small">An approver will decide this payment.</p>` : ""}
      ${!pending ? `<p class="muted small">No further action needed.</p>` : ""}
      ${(wf.ai_calls || []).length ? `<h3 class="section">Verification calls</h3><ul class="list calls">${wf.ai_calls.map((c) => `<li>
        <span><b>${esc({ confirmed: "Confirmed", denied: "Denied", unclear: "Not confirmed" }[c.outcome] || (c.status === "failed" ? "Failed" : "In progress"))}</b>
          <span class="muted small block">${esc(c.contact)} · ${esc(when(c.started_at))}${c.accepted_by ? " · accepted" : ""}</span></span>
        <button class="btn secondary sm" data-call="${esc(c.id)}">Review</button></li>`).join("")}</ul>` : ""}
      <p class="msg" id="derr"></p>
      ${wf.callbacks.length || wf.approvals.length ? `<ul class="list history">${wf.callbacks.map((x) => `<li>${esc(x.email)} called: ${esc(x.outcome.replace("_", " "))} (${esc(x.number_source.replace("_", " "))})</li>`).join("")}${wf.approvals.map((x) => `<li>${esc(x.email)}: ${esc(x.action.replace("_", " "))}${x.reason ? ` — ${esc(x.reason)}` : ""}</li>`).join("")}</ul>` : ""}`;
    const DONE = { release: "Release recorded", hold: "Payment kept on hold", reject: "Marked as fraud" };
    const callCtx = { api, T, cid, toast, canAccept: S.can("callback:record"), onChange: () => { loadDecision(); loadActivity(); } };
    $("#aicall") && ($("#aicall").onclick = () => busy($("#aicall"), "Dialling…", async () => {
      try { await startCall(callCtx); } catch (e) { show($("#derr"), e.message); } }));
    el.querySelectorAll("[data-call]").forEach((b) => b.onclick = () => reviewCall(callCtx, wf.ai_calls.find((c) => c.id === b.dataset.call)));
    $("#cbgo") && ($("#cbgo").onclick = () => busy($("#cbgo"), "Saving…", async () => { try { await api(T(`/cases/${cid}/callbacks`), { method: "POST", json: { contact: $("#cbc").value, number: $("#cbn").value, number_source: $("#cbs").value, outcome: $("#cbo").value, notes: $("#cbt").value } }); toast("Callback saved to the audit log"); await loadDecision(); loadActivity(); } catch (e) { show($("#derr"), e.message); } }));
    el.querySelectorAll("[data-a]").forEach((b) => b.onclick = () => busy(b, "Saving…", async () => {
      try { await api(T(`/cases/${cid}/actions`), { method: "POST", json: { action: b.dataset.a, reason: $("#why").value } }); toast(DONE[b.dataset.a] || "Saved", b.dataset.a === "release" ? "ok" : "warn"); await loadDecision(); loadActivity(); } catch (e) { show($("#derr"), e.message); } }));
  }

  const ctl = new AbortController(); S.cleanup.push(() => ctl.abort());
  let res;
  const streamError = (msg) => { running = false; status(msg); finishLive(false); $("#live-sub").textContent = msg; };
  try { res = await api(T(`/cases/${cid}/stream${S.replay ? "?replay=1" : ""}`), { raw: true, signal: ctl.signal }); }
  catch (e) { if (e.name !== "AbortError") streamError("Couldn't connect to the live stream. Reload the page to try again."); return; }
  if (!res.ok) { $("#live").hidden = true; return status("This case couldn't be opened."); }
  $("#live-badge").className = "live-badge on"; $("#live-word").textContent = "Live"; $("#live-sub").textContent = "Streaming server-sent events as the investigation runs.";
  if (c0?.status === "completed") { $("#skip").hidden = false; $("#live-word").textContent = "Replaying"; $("#live-sub").textContent = "Replaying the recorded event stream with its real timing."; }
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  try {
    for (;;) { const { value, done } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true });
      let i; while ((i = buf.indexOf("\n\n")) >= 0) { const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join(""); if (data) enqueue(JSON.parse(data)); } }
  } catch (e) { if (e.name !== "AbortError" && view.contains($("#live"))) streamError("The live stream was interrupted. Reload to reconnect; the investigation continues on the server."); return; }
  while (pumping || queue.length) await sleep(80);
  if (!view.contains($("#live"))) return;
  // The stream closed without a final event: report it, never pretend the investigation completed.
  if (running) streamError("The live stream ended before the investigation finished. Reload to see the latest state.");
  loadDecision(); loadActivity();
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
    <label>What changed and why<input id="pn" name="change-note" autocomplete="off" placeholder="Lower the hold threshold after Q3 incidents…"></label><button class="btn primary" id="pd">Save draft and test it</button><div id="pout"></div>`
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

// -------------------------------------------------------------- sources --
async function renderSources() {
  const st = await api(T("/sources/status"));
  const ago = (iso) => { if (!iso) return "—"; const h = (Date.now() - Date.parse(iso)) / 36e5; return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`; };
  const lists = st.sanctions.lists || [];
  view.innerHTML = `<div class="page-head"><div><h1>Data sources</h1><p>Every decision is checked against your own records and against authoritative public registers. Here is what each one is, and how fresh it is right now.</p></div></div>
  <section class="panel"><header><h2>Sanctions lists</h2><span class="by">${st.sanctions.mode === "opensanctions" ? "OpenSanctions API" : "Downloaded from the publishing governments, refreshed every 12 hours"}</span></header>
    <div class="src-grid">${lists.map((l) => `<article class="src-card ${l.loaded ? "ok" : l.error ? "bad" : "wait"}"><b>${esc(l.label)}</b>
      <span class="big">${l.loaded ? Number(l.entries).toLocaleString() : "—"}<small> names</small></span>
      <dl><dt>Published</dt><dd>${esc(l.published || "not stated")}</dd><dt>Downloaded</dt><dd>${esc(ago(l.fetched_at))}</dd>
        <dt>SHA-256</dt><dd><code title="${esc(l.sha256 || "")}">${esc((l.sha256 || "—").slice(0, 12))}</code></dd></dl>
      <p class="small ${l.error ? "bad-t" : "muted"}">${l.error ? esc(l.error) : l.refreshing ? "Refreshing now…" : l.loaded ? "Ready" : "Loading…"}</p></article>`).join("")}</div>
    <p class="muted small">The exact list versions (publication date and SHA-256) used for a payment are stored with its decision, so an auditor can see what it was screened against on that day.</p></section>
  <section class="panel spaced-t"><h2>Live registers</h2><ul class="list">${st.live.map((x) => `<li><span><b>${esc(x.name)}</b><span class="muted small block">${esc(x.purpose)}</span></span><span class="pill plain">${x.configured ? "Queried live for each payment" : "Off"}</span></li>`).join("")}</ul></section>
  <div class="two spaced-t"><section class="panel"><h2>Your records</h2><p class="muted small">The strongest evidence is your own system of record: the bank accounts you verified, and how each vendor has been paid before.</p>
      <dl class="kv"><dt>Vendors</dt><dd>${st.workspace.vendors.toLocaleString()}</dd><dt>Verified bank accounts</dt><dd>${st.workspace.bank_accounts.toLocaleString()}</dd></dl>
      <a class="btn secondary sm spaced-t" href="#/setup">Import or sync</a></section>
    <section class="panel"><h2>AI</h2><p class="muted small">${esc(st.ai.role)}</p><dl class="kv"><dt>Status</dt><dd>${!st.ai.enabled ? "Off: pattern matching is used to read requests" : st.ai.unavailable ? `<span class="bad-t">Paused: ${esc(st.ai.unavailable)}</span>. Pattern matching and on-server checks are used meanwhile.` : `On (${esc(st.ai.model)})`}</dd></dl></section></div>`;
}

// ---------------------------------------------------------------- audit --
const AUDIT_CATS = [["all", "All"], ["decision", "Decisions"], ["case", "Cases"], ["policy", "Policy"], ["team", "Team & access"], ["data", "Data"]];
function auditCat(action) {
  if (/^case\.(recommended|release|hold|reject|clear_compliance|callback_recorded|fraud_signal)$/.test(action)) return "decision";
  if (action.startsWith("case.")) return "case";
  if (action.startsWith("policy.")) return "policy";
  if (action.startsWith("import.")) return "data";
  return "team";
}
const ACTION_TEXT = { "case.created": "Payment check submitted", "case.recommended": "Recommendation issued", "case.failed": "Investigation failed",
  "case.release": "Payment released", "case.hold": "Payment kept on hold", "case.reject": "Marked as fraud", "case.callback_recorded": "Callback recorded",
  "case.fraud_signal": "Fraud signal raised", "case.report_downloaded": "Case file downloaded", "case.clear_compliance": "Compliance cleared",
  "policy.drafted": "Policy draft saved", "policy.submitted": "Policy submitted for approval", "policy.activated": "Policy approved and activated",
  "policy.rejected": "Policy change rejected", "member.invited": "Member invited", "member.joined": "Member joined", "member.removed": "Member removed",
  "apikey.created": "API key created", "apikey.revoked": "API key revoked", "workspace.created": "Workspace created", "settings.updated": "Settings updated",
  "import.sample": "Sample data loaded", "import.vendors": "Vendor list imported", "import.payments": "Payment history imported" };
const actionText = (a) => ACTION_TEXT[a] || a.replace(".", ": ").replaceAll("_", " ");
const actorName = (a) => (a === "system" ? "Verify-Before-Wire" : a.replace(/^dev:/, "").replace(/^apikey:/, "API key "));

// Same bytes as audit.entry_hash on the server: json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=True) of the entry without its own hash, then SHA-256.
function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${canonical(k)}:${canonical(v[k])}`).join(",")}}`;
  if (typeof v === "string") return JSON.stringify(v).replace(/[\u0080-￿]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  return JSON.stringify(v);
}
async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function renderAudit() {
  if (!S.can("audit:read")) { view.innerHTML = `<div class="page-head"><h1>Audit log</h1></div><div class="panel empty"><p>Your role can't view the audit log.</p></div>`; return; }
  view.innerHTML = `<div class="page-head"><div><h1>Audit log</h1><p>Every action in this workspace. Each entry carries the hash of the one before it, so any edit or deletion breaks the chain.</p></div>
    <div class="head-actions"><button class="btn secondary sm" id="au-export">Export JSON</button><button class="btn primary sm" id="au-verify">Verify again</button></div></div>
  <section class="panel chain-card" id="chain">
    <div class="chain-top"><div class="seal checking" id="seal" aria-hidden="true"><span></span></div>
      <div class="chain-title"><h2 id="chain-title">Verifying the hash chain…</h2><p class="muted small flush" id="chain-sub">Recomputing every entry's SHA-256 in your browser and comparing it with the server's check.</p></div>
      <dl class="chain-stats"><div><dt>Entries</dt><dd id="cs-n">—</dd></div><div><dt>Server check</dt><dd id="cs-server">—</dd></div>
        <div><dt>Browser check</dt><dd id="cs-browser">—</dd></div><div><dt>Head hash</dt><dd><code id="cs-head">—</code></dd></div></dl></div>
    <div class="chain" id="chain-blocks"></div></section>
  <section class="panel spaced-t"><div class="list-tools"><div class="chips" id="au-filter">${AUDIT_CATS.map(([k, t], i) => `<button class="chip ${i ? "" : "on"}" data-f="${k}">${t}<span></span></button>`).join("")}</div>
    <span class="live-badge sm on" title="New entries appear automatically"><i></i>Live</span>
    <input type="search" id="au-q" name="q" autocomplete="off" placeholder="Search who, what or which case…" aria-label="Search the audit log"></div>
    <div class="au-list" id="au-list"></div><p class="muted small" id="au-count"></p></section>`;

  let entries = [], filter = "all", q = "", seen = new Set();
  const chainBlocks = (list) => { const asc = [...list].sort((a, b) => a.seq - b.seq).slice(-36);
    $("#chain-blocks").innerHTML = asc.map((e, i) => `${i ? `<span class="link-bar" data-seq="${e.seq}"></span>` : ""}<button class="blk cat-${auditCat(e.action)}" data-seq="${e.seq}" title="#${e.seq} ${esc(actionText(e.action))}&#10;hash ${e.hash}&#10;prev ${e.prev_hash}">
      <b>#${e.seq}</b><code>${e.hash.slice(0, 6)}</code></button>`).join("");
    $("#chain-blocks").querySelectorAll(".blk").forEach((b) => b.onclick = () => { const row = $(`#au-${b.dataset.seq}`); if (row) { row.open = true; row.scrollIntoView({ behavior: "smooth", block: "center" }); row.classList.remove("flash"); void row.offsetWidth; row.classList.add("flash"); } });
    $("#chain-blocks").scrollLeft = 1e6; };
  const verify = async () => {
    const seal = $("#seal"); seal.className = "seal checking"; $("#chain-title").textContent = "Verifying the hash chain…";
    const [server, list] = await Promise.all([api(T("/audit/verify")), api(T("/audit?limit=1000"))]);
    entries = list; chainBlocks(entries); paintList();
    $("#cs-n").textContent = server.entries ?? entries.length;
    $("#cs-server").innerHTML = server.ok ? `<span class="ok-t">Intact</span>` : `<span class="bad-t">Broken at #${server.broken_at}</span>`;
    const asc = [...entries].sort((a, b) => a.seq - b.seq);
    let browserOk = true, brokenAt = null, prevHash = asc[0]?.seq === 1 ? "0".repeat(64) : asc[0]?.prev_hash;
    for (const e of asc) {
      const { hash, ...body } = e;
      const ok = e.prev_hash === prevHash && (await sha256(canonical(body))) === hash;
      const blk = $(`.blk[data-seq="${e.seq}"]`), bar = $(`.link-bar[data-seq="${e.seq}"]`);
      if (blk) { blk.classList.add(ok ? "ok" : "bad"); bar?.classList.add(ok ? "ok" : "bad"); if (!REDUCED) await sleep(asc.length > 36 ? 12 : 45); }
      if (!ok && browserOk) { browserOk = false; brokenAt = e.seq; }
      prevHash = e.hash;
    }
    $("#cs-browser").innerHTML = browserOk ? `<span class="ok-t">Matches</span>` : `<span class="bad-t">Differs at #${brokenAt}</span>`;
    $("#cs-head").textContent = server.head ? `${server.head.slice(0, 12)}…${server.head.slice(-6)}` : "—"; $("#cs-head").title = server.head || "";
    const good = server.ok && browserOk;
    seal.className = `seal ${good ? "good" : "bad"}`;
    $("#chain-title").textContent = good ? `Chain verified: ${entries.length} entr${entries.length === 1 ? "y" : "ies"}, none changed or removed` : server.ok ? "Server says intact, but your browser computed a different hash" : `Integrity check failed at entry ${server.broken_at}: ${server.reason}`;
    $("#chain-sub").textContent = good ? `Your browser recomputed every SHA-256 independently${entries.length >= 1000 ? " (latest 1,000 entries)" : ""} and got the same head hash as the server.` : "Treat entries after this point as untrusted and contact your administrator.";
  };
  const paintList = () => {
    $("#au-filter").querySelectorAll(".chip").forEach((b) => { $("span", b).textContent = b.dataset.f === "all" ? entries.length : entries.filter((e) => auditCat(e.action) === b.dataset.f).length; });
    const rows = entries.filter((e) => (filter === "all" || auditCat(e.action) === filter)
      && (!q || `${e.actor} ${e.action} ${actionText(e.action)} ${e.target} ${JSON.stringify(e.detail)}`.toLowerCase().includes(q)));
    $("#au-list").innerHTML = rows.length ? rows.map((e) => { const cat = auditCat(e.action), isNew = seen.size && !seen.has(e.seq);
      const caseId = e.target.startsWith("case/") ? e.target.slice(5) : null;
      return `<details class="au-row cat-${cat} ${isNew ? "new" : ""}" id="au-${e.seq}"><summary>
        <span class="au-seq">#${e.seq}</span><span class="au-ic" aria-hidden="true"></span>
        <span class="au-what"><b>${esc(actionText(e.action))}</b><small>${esc(actorName(e.actor))}${e.detail?.decision ? ` · <span class="decision ${esc(e.detail.decision)}">${esc(DEC_TEXT[e.detail.decision] || e.detail.decision)}</span> ${esc(e.detail.score ?? "")}` : ""}</small></span>
        <span class="au-target">${caseId ? `<a href="#/case/${esc(caseId)}">${esc(e.target)}</a>` : esc(e.target)}</span>
        <span class="au-when" title="${esc(new Date(e.at).toLocaleString())}">${esc(when(e.at))}</span></summary>
        <div class="au-body"><dl class="kv"><dt>Action</dt><dd><code>${esc(e.action)}</code></dd><dt>Actor</dt><dd>${esc(e.actor)}</dd><dt>At</dt><dd>${esc(new Date(e.at).toLocaleString())}</dd>
          <dt>Previous hash</dt><dd><code class="hash">${esc(e.prev_hash)}</code></dd><dt>This hash</dt><dd><code class="hash">${esc(e.hash)}</code></dd></dl>
          ${Object.keys(e.detail || {}).length ? `<pre class="json">${esc(JSON.stringify(e.detail, null, 2))}</pre>` : ""}</div></details>`; }).join("")
      : `<div class="empty"><p>No entries match.</p></div>`;
    $("#au-count").textContent = `${rows.length} of ${entries.length} entries shown`;
    entries.forEach((e) => seen.add(e.seq));
  };
  $("#au-filter").onclick = (e) => { const b = e.target.closest(".chip"); if (!b) return; filter = b.dataset.f;
    $("#au-filter").querySelectorAll(".chip").forEach((x) => x.classList.toggle("on", x === b)); paintList(); };
  $("#au-q").oninput = (e) => { q = e.target.value.trim().toLowerCase(); paintList(); };
  $("#au-verify").onclick = () => busy($("#au-verify"), "Verifying…", verify);
  $("#au-export").onclick = () => { const blob = new Blob([JSON.stringify(entries.slice().reverse(), null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `audit-${S.tid}-${new Date().toISOString().slice(0, 10)}.json`; a.click(); toast("Audit log exported"); };
  await verify();
  every(5000, async () => { try { const list = await api(T("/audit?limit=1000"));
    if (list.length && list[0].seq !== entries[0]?.seq) { const added = list.filter((e) => !seen.has(e.seq)).length;
      if (added) toast(`${added} new audit entr${added === 1 ? "y" : "ies"}, re-verifying the chain`, "info"); await verify(); } } catch { /* next tick */ } });
}

boot();
