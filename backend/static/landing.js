// Public landing page (signed-out visitors). Everything on it is true of the product: the data
// sources named are the ones it queries, the numbers are measured (README "Engineering, by the
// numbers"), and there are no invented customers or testimonials.
// Motion: IntersectionObserver reveals, a scroll-linked parallax hero (transform only, paused when
// off-screen), CSS loops for the product mockups. Everything respects prefers-reduced-motion.
import { $, $$, REDUCED } from "./util.js";

const ARROW = `<span class="lp-arrow" aria-hidden="true"><svg viewBox="0 0 16 16"><path d="M4 12 12 4M6 4h6v6"/></svg></span>`;
const CHECKS = [["Vendor record", "bad"], ["Sender & domain", "bad"], ["Payment history", "ok"], ["Sanctions screen", "ok"],
  ["Company & bank registers", "ok"], ["Language", "bad"], ["Voice & video", "bad"]];

const mountains = `<svg class="lp-mtn" viewBox="0 0 1440 420" preserveAspectRatio="none" aria-hidden="true">
  <path class="m1" data-depth="0.08" d="M0 260 160 170 300 230 470 120 640 210 820 100 1000 200 1180 130 1440 220V420H0Z"/>
  <path class="m2" data-depth="0.16" d="M0 300 210 220 380 280 560 190 760 270 930 200 1120 280 1300 210 1440 260V420H0Z"/>
  <path class="m3" data-depth="0.26" d="M0 340 180 290 400 340 620 280 820 330 1040 290 1240 340 1440 300V420H0Z"/>
  <path class="m4" data-depth="0.36" d="M0 380 240 350 520 385 780 345 1060 380 1300 350 1440 370V420H0Z"/></svg>`;

const mock = `<div class="lp-shell lp-mock" aria-hidden="true"><div class="lp-core">
  <div class="mk-bar"><i></i><i></i><i></i><span>verify-before-wire · case c_8f21</span></div>
  <div class="mk-body">
    <div class="mk-head"><small>Payment request · received 14:02</small><b>SGD 480,000 to Lion City Logistic Services</b>
      <span>Requested by "Daniel Koh (CFO)" · daniel.koh@acme-hold1ngs.com</span></div>
    <div class="mk-grid">
      <ul class="mk-checks">${CHECKS.map(([t, s], i) => `<li class="c${i} ${s}"><i></i>${t}<em>${s === "bad" ? "red flag" : "clear"}</em></li>`).join("")}</ul>
      <div class="mk-verdict"><div class="mk-stamp">HOLD</div><div class="mk-meter"><span></span><span></span><span></span><i></i></div>
        <p>Sender domain imitates acme-holdings.com · voice note has digital silence · new bank account</p>
        <span class="mk-req">Callback to the number on file · second approver</span></div>
    </div></div></div></div>
  <div class="lp-shell lp-phone" aria-hidden="true"><div class="lp-core">
    <div class="ph-top"><span class="ph-av">ML</span><b>Tan Mei Ling</b><small>+65 6123 4567 · from your vendor master</small></div>
    <ol class="ph-convo"><li class="ag">Did Lion City Logistics ask us to pay a new account ending in 6838?</li>
      <li class="vd">No. We never changed our bank details.</li>
      <li class="pf"><b>Denied</b> · "No. We never changed our bank details." ✓ found in transcript</li></ol></div></div>`;

const bento = [
  ["wide", "Seven checks, in parallel", "Your vendor master and payment history, official sanctions lists, company and bank registers, the sender's domain, the wording and the media itself. Every lookup is streamed live and cites its source.",
    `<div class="bx-flow"><div class="bx-col">${["Vendor master", "OFAC · UN · EU · UK", "GLEIF · VIES", "RDAP · DNS"].map((s) => `<span>${s}</span>`).join("")}</div>
     <div class="bx-wires"><i></i><i></i><i></i><i></i></div><div class="bx-col chk">${["Vendor", "Sanctions", "Registers", "Domain"].map((s, i) => `<span class="k${i}">${s}</span>`).join("")}</div>
     <div class="bx-wires out"><i></i></div><div class="bx-out">Policy<b>Hold</b></div></div>`],
  ["", "Deepfake voice & video forensics", "Synthetic speech has no room noise between words, and a cheap fake video is a still photo with a voice-over. Both are measured on the server.",
    `<div class="bx-wave">${Array.from({ length: 42 }, (_, i) => `<i class="${[9, 10, 11, 24, 25, 26, 27, 36].includes(i) ? "gap" : ""}"></i>`).join("")}</div><p class="bx-note"><b>20%</b> exact digital silence · <b>0.0</b> frame motion</p>`],
  ["", "The AI calls the vendor", "On a hold, an agent calls the number in your vendor master, asks questions built from the case facts and records the answers. An answer only counts if its words are in the transcript.",
    `<div class="bx-call"><span class="q">Did you ask us to pay a new account?</span><span class="a">"No, we never changed our bank details."</span><span class="v">✓ quote found in transcript · recording SHA-256 kept</span></div>`],
  ["", "Official sanctions lists, with provenance", "OFAC, UN, EU and UK lists come straight from the governments that publish them, refreshed every 12 hours. Each decision records the exact list versions it was screened against.",
    `<ul class="bx-lists"><li>OFAC SDN<span>US Treasury</span></li><li>UN Security Council<span>United Nations</span></li><li>EU consolidated<span>European Commission</span></li><li>UK Sanctions List<span>FCDO</span></li></ul>`],
  ["", "History nobody can rewrite", "Every action is chained to the one before it with SHA-256, and the database refuses edits and deletions. Your browser can re-verify the whole chain.",
    `<div class="bx-chain">${[1, 2, 3, 4, 5].map((n) => `<span class="b${n}">#${n}</span>`).join("<em></em>")}</div>`],
  ["full", "Plugs into SAP, without touching F110", "SAP keeps the payment block until this checkpoint says a payment may go. Payment runs and vendor master sync over a REST API, results come back as signed webhooks, and if anything is down the block simply stays.",
    `<div class="bx-sap"><span>SAP F110 proposal</span><i></i><span class="hl">Verify-Before-Wire</span><i></i><span>Payment block lifted<br><small>only after release</small></span></div>`],
];

const frauds = [
  ["The deepfake CFO", "A voice note and an email from the “CFO”: urgent, confidential, a new Hong Kong account, before 5pm.",
    ["Sender domain imitates yours (acme-hold1ngs.com)", "Voice note: exact digital silence between words", "Payee not in your vendor master"], "HOLD"],
  ["The changed bank account", "A genuine supplier's real, authenticated email asks you to pay a new account from now on.",
    ["Account differs from the one you verified", "Bank country doesn't match its SWIFT code", "AI call: vendor denies sending it"], "HOLD"],
  ["The routine invoice", "Your logistics supplier's monthly invoice, to the account you've paid 18 times.",
    ["Account on file, amount in the usual range", "Sender authenticated (SPF, DKIM, DMARC)", "No sanctions match on four lists"], "RELEASE"],
];

const stats = [["100", "ms", "to check one SAP payment, end to end"], ["70", "k+", "sanctioned names across OFAC, UN, EU and UK"],
  ["7", "", "checks run in parallel on every request"], ["0", "", "payments released by AI: your policy decides"]];

const trust = [["Four-eyes by design", "Whoever submits a payment can never release it. Holds need a reason and, by policy, a second approver."],
  ["Fail-closed", "If a required check cannot run, the payment is held. In SAP, the block simply stays."],
  ["Your cloud, your data", "Run on Google Cloud, AWS, Azure, SAP BTP or on-premise. Gemini runs in your own project."],
  ["Callbacks to numbers on file", "Never to a number from the request: that is exactly what a fraudster would send."],
  ["Append-only audit", "Hash-chained in the application, enforced again in the database. Exportable for your auditors."],
  ["Provenance on every decision", "Policy version and hash, input hashes, and the exact list versions used, stored with the case."]];

export function renderLanding(root, { signIn, signUp }) {
  root.innerHTML = `<div class="lp">
  <nav class="lp-nav" aria-label="Main"><div class="lp-navin">
    <a class="lp-brand" href="#/"><span class="mark" aria-hidden="true"></span>Verify-Before-Wire</a>
    <div class="lp-links"><a href="#how">How it works</a><a href="#sap">SAP</a><a href="#trust">Trust</a><a href="/docs">API</a></div>
    <div class="lp-cta"><button class="lp-btn ghost" data-go="signin">Sign in</button><button class="lp-btn primary" data-go="signup">Start free${ARROW}</button></div>
    <button class="lp-burger" aria-label="Menu" aria-expanded="false"><i></i><i></i></button></div>
    <div class="lp-menu" hidden><a href="#how">How it works</a><a href="#sap">SAP</a><a href="#trust">Trust</a><a href="/docs">API</a>
      <button class="lp-btn ghost" data-go="signin">Sign in</button><button class="lp-btn primary" data-go="signup">Start free${ARROW}</button></div></nav>

  <header class="lp-hero">
    <div class="lp-sky" aria-hidden="true"><div class="lp-sun"></div><div class="lp-cloud c1"></div><div class="lp-cloud c2"></div><div class="lp-cloud c3"></div></div>
    ${mountains}
    <div class="lp-hero-in">
      <p class="lp-pill rv"><span class="dot"></span>Payment-fraud checkpoint for finance teams</p>
      <h1 class="lp-h1"><span class="w">Verify</span> <span class="w">the</span> <span class="w">request.</span><br><span class="w soft">Not</span> <span class="w soft">the</span> <span class="w soft">voice.</span></h1>
      <p class="lp-lead rv">Before money leaves the company, an AI-assisted checkpoint investigates every payment request (email, voice note, video or SAP payment run) against your own records and official registers, and your policy decides.</p>
      <div class="lp-hero-cta rv"><button class="lp-btn primary lg" data-go="signup">Start free${ARROW}</button><button class="lp-btn glass lg" data-go="signin">Sign in</button></div>
      <div class="lp-stage rv">${mock}</div>
    </div>
  </header>

  <section class="lp-strip" aria-label="Data sources"><p>Every decision is checked against</p>
    <div class="lp-marquee"><div class="lp-track">${[0, 1].map(() => ["Your SAP vendor master", "OFAC SDN", "UN Security Council", "EU consolidated list", "UK Sanctions List", "GLEIF", "EU VIES", "RDAP & DNS", "ECB FX rates", "Google Gemini"].map((s) => `<span>${s}</span>`).join("")).join("")}</div></div></section>

  <section class="lp-sec" id="how">
    <div class="lp-head rv"><span class="lp-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/><path d="M9 12l2 2 4-4"/></svg></span>
      <h2>Every payment request, investigated</h2><p>Not a rules engine bolted on a mailbox. A live investigation you can watch, with evidence you can show an auditor.</p></div>
    <div class="lp-bento">${bento.map(([cls, t, d, art], i) => `<article class="lp-shell lp-card ${cls} rv" data-d="${i % 3}"><div class="lp-core"><div class="art">${art}</div><h3>${t}</h3><p>${d}</p></div></article>`).join("")}</div>
  </section>

  <section class="lp-sec lp-policy">
    <div class="lp-split">
      <div class="rv"><span class="lp-eyebrow">Explainable by construction</span><h2>Your policy decides.<br>Not the AI.</h2>
        <p>Gemini reads messy requests and writes summaries. Every risk point comes from a deterministic check, and your rules, approved by two admins and tested on past payments first, turn them into Release, Call back or Hold.</p>
        <ul class="lp-ticks"><li>Every signal shows its source and the evidence behind it</li><li>Policy changes are simulated on history before they apply</li><li>No AI output can release a payment</li></ul></div>
      <div class="lp-shell rv" data-d="1"><div class="lp-core lp-meterdemo"><small>Risk score</small><div class="md-meter"><span>Release</span><span>Call back</span><span>Hold</span><i></i></div>
        <ol class="md-path"><li>Sender domain imitates yours <b>+60</b></li><li>Payee not in vendor master <b>+30</b></li><li>Voice note: digital silence <b>+15</b></li><li>Policy rule: new bank account needs a callback</li><li class="fin">Recommendation <span>Hold</span></li></ol></div></div>
    </div>
  </section>

  <section class="lp-sec">
    <div class="lp-head rv"><span class="lp-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg></span>
      <h2>Three requests. Three honest answers.</h2><p>These are the sample cases you can run yourself after signing up.</p></div>
    <div class="lp-frauds">${frauds.map(([t, d, sig, out], i) => `<article class="lp-shell rv" data-d="${i}"><div class="lp-core lp-fraud">
      <span class="out ${out}">${out === "HOLD" ? "Hold" : "Release"}</span><h3>${t}</h3><p>${d}</p><ul>${sig.map((s) => `<li>${s}</li>`).join("")}</ul></div></article>`).join("")}</div>
  </section>

  <section class="lp-stats" aria-label="Measured performance">${stats.map(([n, u, t], i) => `<div class="rv" data-d="${i}"><b><span class="cnt" data-to="${n}">${n}</span>${u}</b><span>${t}</span></div>`).join("")}</section>

  <section class="lp-sec" id="sap">
    <div class="lp-split rev">
      <div class="lp-shell rv"><div class="lp-core lp-int">${["SAP S/4HANA", "SAP ECC", "SAP BTP Integration Suite", "Any ERP via REST", "Signed webhooks", "PostgreSQL", "Cloud Firestore", "Google Cloud", "AWS", "Azure", "Twilio voice", "Google Gemini"].map((s) => `<span>${s}</span>`).join("")}</div></div>
      <div class="rv" data-d="1"><span class="lp-eyebrow">Fits the stack you already run</span><h2>A plug-in for SAP, and for everything else</h2>
        <p>Check a whole F110 payment run in one call, sync the vendor master as it changes, and get signed webhooks back. Low-risk payments can flow straight through under a policy you approve; everything else waits for a person.</p>
        <a class="lp-link" href="/docs">Explore the API${ARROW}</a></div>
    </div>
  </section>

  <section class="lp-dark" id="trust">
    <div class="lp-head rv on-dark"><span class="lp-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg></span>
      <h2>Built to be trusted with money</h2><p>Controls a finance team, a compliance officer and an auditor can each sign off on.</p></div>
    <div class="lp-trust">${trust.map(([t, d], i) => `<article class="rv" data-d="${i % 3}"><h3>${t}</h3><p>${d}</p></article>`).join("")}</div>
  </section>

  <section class="lp-final"><div class="lp-final-in rv"><h2>Verify before you wire.</h2><p>Set up a workspace in five minutes, load the sample data and watch your first investigation live.</p>
    <div class="lp-hero-cta"><button class="lp-btn primary lg" data-go="signup">Start free${ARROW}</button><button class="lp-btn glass dark lg" data-go="signin">Sign in</button></div></div></section>

  <footer class="lp-foot"><div><a class="lp-brand" href="#/"><span class="mark" aria-hidden="true"></span>Verify-Before-Wire</a><p>We verify the request, not the voice.</p></div>
    <nav aria-label="Footer"><a href="#how">How it works</a><a href="#sap">SAP integration</a><a href="#trust">Trust</a><a href="/docs">API reference</a></nav></footer>
  </div>`;

  root.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => (b.dataset.go === "signup" ? signUp() : signIn()));
  root.querySelectorAll('a[href^="#"]:not([href="#/"])').forEach((a) => a.onclick = (e) => {
    e.preventDefault(); menu(false); $(a.getAttribute("href"), root)?.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth" }); });
  const burger = $(".lp-burger", root), drawer = $(".lp-menu", root);
  const menu = (open) => { burger.setAttribute("aria-expanded", String(open)); burger.classList.toggle("x", open); drawer.hidden = !open; };
  burger.onclick = () => menu(drawer.hidden);
  return animate(root);
}

// Returns a teardown function (observers and listeners) for when the app replaces the page.
function animate(root) {
  const words = $$(".lp-h1 .w", root);
  words.forEach((w, i) => { w.style.transitionDelay = `${120 + i * 70}ms`; });
  // One continuous gradient across "Not the voice.": each word shows its own slice of it.
  const soft = words.filter((w) => w.classList.contains("soft"));
  soft.forEach((w, k) => { w.style.backgroundSize = `${soft.length * 100}% 100%`; w.style.backgroundPosition = `${soft.length > 1 ? (k / (soft.length - 1)) * 100 : 0}% 0`; });
  requestAnimationFrame(() => root.querySelector(".lp")?.classList.add("ready"));
  $$(".rv[data-d]", root).forEach((el) => { el.style.transitionDelay = `${Number(el.dataset.d) * 110}ms`; });
  if (REDUCED) { $$(".rv", root).forEach((el) => el.classList.add("in")); return () => {}; }

  const counted = new WeakSet();
  const io = new IntersectionObserver((entries) => entries.forEach((e) => {
    if (!e.isIntersecting) return;
    e.target.classList.add("in");
    e.target.querySelectorAll?.(".cnt").forEach((c) => { if (!counted.has(c)) { counted.add(c); countTo(c); } });
    io.unobserve(e.target);
  }), { threshold: 0.01, rootMargin: "0px 0px -8% 0px" });   // tall elements must reveal as soon as they enter
  $$(".rv", root).forEach((el) => io.observe(el));

  // Parallax: only while the hero is on screen, one rAF per frame, transforms only.
  const hero = $(".lp-hero", root), layers = $$(".lp-mtn path, .lp-sun, .lp-cloud, .lp-stage", root);
  let visible = true, queued = false;
  const paint = () => { queued = false; const y = Math.min(window.scrollY, 900);
    layers.forEach((l) => { const k = Number(l.dataset.depth || (l.classList.contains("lp-sun") ? 0.05 : l.classList.contains("lp-stage") ? -0.06 : 0.12));
      l.style.transform = `translate3d(0, ${(y * k).toFixed(1)}px, 0)`; });
    $(".lp-nav", root)?.classList.toggle("solid", window.scrollY > 24); };
  const onScroll = () => { if (visible && !queued) { queued = true; requestAnimationFrame(paint); } else if (!visible) $(".lp-nav", root)?.classList.toggle("solid", true); };
  const heroIO = new IntersectionObserver(([e]) => { visible = e.isIntersecting; }, { threshold: 0 });
  heroIO.observe(hero);
  addEventListener("scroll", onScroll, { passive: true });
  paint();
  return () => { io.disconnect(); heroIO.disconnect(); removeEventListener("scroll", onScroll); };
}

function countTo(el) {
  const to = Number(el.dataset.to), t0 = performance.now(), dur = 1200;
  const step = (t) => { const k = Math.min(1, (t - t0) / dur); el.textContent = Math.round(to * (1 - (1 - k) ** 4)); if (k < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

export function landingAvailable(hash) {
  return hash === "" || hash === "#" || hash === "#/" || hash === "#/home";
}
