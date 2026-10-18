// Onboarding tutorial: an animated "how it works" film, then a spotlight walkthrough of the real UI.
// Plays once when a company creates its workspace; replay any time from the sidebar ("Product tour").
// No inline styles in markup (the CSP blocks them): positions are set from script via el.style.
import { $, esc, REDUCED, sleep } from "./util.js";

const CHAPTERS = [
  { title: "A payment request arrives", text: "An email from the “CFO”, a voice note, a video message, or a payment run from SAP. Anything that asks your company to send money." },
  { title: "Seven checks run in parallel", text: "Your vendor master and payment history, official sanctions lists, company and bank registers, the sender’s domain, and voice & video forensics. Each check cites its source." },
  { title: "Your policy decides, not the AI", text: "Every signal adds risk points. Your rules, approved by two admins, turn them into Release, Call back or Hold. The AI only reads and summarises." },
  { title: "Every step is on the record", text: "Each action is chained to the one before it with SHA-256. Nobody can edit or delete history, and your browser can verify it." },
];

const DEFAULT_CHECKS = ["Vendor record", "Sender & domain", "Payment history", "Sanctions", "Registers", "Language", "Voice & video"];
let CHECKS = DEFAULT_CHECKS;
const SOURCES = ["Your vendor master", "OFAC · UN · EU · UK", "GLEIF · VIES", "RDAP · DNS", "On-server forensics"];

function film(company) {
  return new Promise((resolve) => {
    const veil = document.createElement("div");
    veil.className = "tour-veil";
    veil.innerHTML = `<section class="welcome" role="dialog" aria-modal="true" aria-labelledby="w-title">
      <header class="w-head"><span class="w-brand"><span class="mark" aria-hidden="true"></span>Welcome${company ? `, ${esc(company)}` : ""}</span>
        <button class="btn ghost sm" id="w-skip">Skip intro</button></header>
      <div class="w-stage ch-0" id="w-stage" aria-hidden="true">
        <div class="w-inputs">${["Email", "Voice note", "Video", "SAP F110"].map((t, i) => `<span class="w-in i${i}">${t}</span>`).join("")}</div>
        <div class="w-pay"><small>Payment request</small><b>SGD 480,000</b><span>to Lion City Logistic Services</span></div>
        <div class="w-checks">${CHECKS.map((t, i) => `<span class="w-chk c${i} ${[0, 1, 3, 6].includes(i) ? "bad" : "ok"}">${t}</span>`).join("")}</div>
        <div class="w-srcs">${SOURCES.map((t, i) => `<span class="w-src s${i}">${t}</span>`).join("")}</div>
        <div class="w-pkts">${[0, 1, 2, 3, 4].map((i) => `<i class="p${i}"></i>`).join("")}</div>
        <div class="w-policy"><small>Risk score</small><div class="w-meter"><span class="z1"></span><span class="z2"></span><span class="z3"></span><i class="w-needle"></i></div>
          <div class="w-stamp">HOLD</div><p class="w-rule">Callback to the number on file · second approver</p></div>
        <div class="w-chain">${[1, 2, 3, 4, 5].map((n) => `<span class="w-blk b${n}"><b>#${n}</b><code>${["9acc20", "ea194b", "d1f212", "b845f8", "4c2d6b"][n - 1]}</code></span>`).join("<em></em>")}
          <span class="w-seal"></span></div>
      </div>
      <div class="w-copy"><p class="w-step" id="w-step"></p><h2 id="w-title"></h2><p id="w-text"></p></div>
      <footer class="w-foot"><div class="w-dots" id="w-dots">${CHAPTERS.map((_, i) => `<button aria-label="Chapter ${i + 1}" data-i="${i}"></button>`).join("")}</div>
        <div class="w-nav"><button class="btn secondary sm" id="w-back">Back</button><button class="btn primary sm" id="w-next">Next</button></div></footer>
    </section>`;
    document.body.append(veil);
    requestAnimationFrame(() => veil.classList.add("in"));
    let i = 0, timer = null;
    const stage = $("#w-stage", veil);
    const show = (n) => {
      i = Math.max(0, Math.min(CHAPTERS.length - 1, n));
      stage.className = `w-stage ch-${i + 1}`;
      void stage.offsetWidth;                       // restart the chapter's animations
      stage.classList.add("go");
      $("#w-step", veil).textContent = `${i + 1} of ${CHAPTERS.length}`;
      const t = $("#w-title", veil), p = $("#w-text", veil);
      t.textContent = CHAPTERS[i].title; p.textContent = CHAPTERS[i].text;
      for (const el of [t, p]) { el.classList.remove("swap"); void el.offsetWidth; el.classList.add("swap"); }
      veil.querySelectorAll("#w-dots button").forEach((b, k) => b.classList.toggle("on", k === i));
      $("#w-back", veil).disabled = i === 0;
      $("#w-next", veil).textContent = i === CHAPTERS.length - 1 ? "Set up my workspace" : "Next";
      clearTimeout(timer);
      if (!REDUCED && i < CHAPTERS.length - 1) timer = setTimeout(() => show(i + 1), 7000);   // gentle autoplay
    };
    const close = (go) => {
      clearTimeout(timer); document.removeEventListener("keydown", key);
      veil.classList.remove("in"); veil.classList.add("out");
      setTimeout(() => { veil.remove(); resolve(go); }, REDUCED ? 0 : 320);
    };
    const key = (e) => {
      if (e.key === "Escape") close(false);
      else if (e.key === "ArrowRight") i === CHAPTERS.length - 1 ? close(true) : show(i + 1);
      else if (e.key === "ArrowLeft") show(i - 1);
    };
    document.addEventListener("keydown", key);
    $("#w-next", veil).onclick = () => (i === CHAPTERS.length - 1 ? close(true) : show(i + 1));
    $("#w-back", veil).onclick = () => show(i - 1);
    $("#w-skip", veil).onclick = () => close(true);
    veil.querySelectorAll("#w-dots button").forEach((b) => b.onclick = () => show(+b.dataset.i));
    show(0);
    $("#w-next", veil).focus();
  });
}

// ------------------------------------------------------------- spotlight ----
const STEPS = [
  { page: "setup", sel: "#st-data", title: "Your records are the strongest evidence",
    text: "Sync vendors and payment history from SAP or import CSV files. Every bank account you verify here becomes the reference that requests are checked against." },
  { page: "setup", sel: "#sample", title: "Try it with sample data", text: "Load three sample vendors and their payment history, so the sample cases have something real to check against.", action: "Click it now, or carry on." },
  { page: "setup", sel: "#st-team", title: "Four eyes on every release", text: "Invite approvers. Whoever submits a payment can never release it, and holds need a reason." },
  { page: "setup", sel: "#st-api", title: "Plug into SAP", text: "Create an ERP connector key and a signed webhook. SAP keeps the payment block until this workspace says the payment may go." },
  { page: "setup", sel: '.nav a[data-page="checks"]', title: "Check a payment", text: "Drop in an email, voice note, video or invoice, or run a sample, including a deepfake “CFO video message”. Then watch every lookup live." },
  { page: "setup", sel: '.nav a[data-page="sources"]', title: "Know what you checked against", text: "Each sanctions list and register, with its publication date, fingerprint and freshness." },
  { page: "setup", sel: '.nav a[data-page="audit"]', title: "History nobody can rewrite", text: "Every action is hash-chained. Your browser re-verifies the whole chain in seconds." },
];

async function waitFor(sel, ms = 3000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const el = $(sel); if (el && el.getBoundingClientRect().width) return el; await sleep(80); }
  return null;
}

async function walk() {
  const spot = document.createElement("div"); spot.className = "tour-spot";
  const card = document.createElement("div"); card.className = "tour-card"; card.setAttribute("role", "dialog"); card.setAttribute("aria-live", "polite");
  document.body.append(spot, card);
  let i = 0, target = null, done = false;
  const place = () => {
    if (!target) return;
    const r = target.getBoundingClientRect(), pad = 8;
    Object.assign(spot.style, { top: `${r.top - pad}px`, left: `${r.left - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
    const cw = Math.min(340, innerWidth - 32), ch = card.offsetHeight || 180, gap = 16;
    let x, y, side;
    if (innerWidth < 700) { x = 16; y = innerHeight - ch - 16; side = "sheet"; }
    else if (r.right + gap + cw < innerWidth - 16) { x = r.right + gap; y = r.top; side = "right"; }
    else if (r.bottom + gap + ch < innerHeight - 16) { x = r.left; y = r.bottom + gap; side = "bottom"; }
    else { x = r.left; y = r.top - ch - gap; side = "top"; }
    x = Math.max(16, Math.min(x, innerWidth - cw - 16)); y = Math.max(16, Math.min(y, innerHeight - ch - 16));
    Object.assign(card.style, { left: `${x}px`, top: `${y}px`, width: `${cw}px` });
    card.dataset.side = side;
  };
  const finish = () => {
    if (done) return; done = true;
    removeEventListener("resize", place); removeEventListener("scroll", place, true); document.removeEventListener("keydown", key);
    spot.classList.add("out"); card.classList.add("out");
    setTimeout(() => { spot.remove(); card.remove(); }, 300);
  };
  const show = async (n) => {
    i = n;
    if (i >= STEPS.length) return outro();
    const s = STEPS[i];
    if (!location.hash.startsWith(`#/${s.page}`)) location.hash = `#/${s.page}`;
    target = await waitFor(s.sel);
    if (done) return;
    if (!target) return show(i + 1);                 // e.g. a section this role can't see
    target.scrollIntoView({ block: "center", behavior: REDUCED ? "auto" : "smooth" });
    card.innerHTML = `<p class="t-step"><span>${i + 1}</span> of ${STEPS.length}</p><h3>${esc(s.title)}</h3><p>${esc(s.text)}</p>
      ${s.action ? `<p class="t-action">${esc(s.action)}</p>` : ""}
      <div class="t-progress"><i></i></div>
      <footer><button class="btn ghost sm" data-t="skip">Skip tour</button><span>${i ? `<button class="btn secondary sm" data-t="back">Back</button>` : ""}
        <button class="btn primary sm" data-t="next">${i === STEPS.length - 1 ? "Finish" : "Next"}</button></span></footer>`;
    $(".t-progress i", card).style.width = `${((i + 1) / STEPS.length) * 100}%`;
    card.classList.remove("swap"); void card.offsetWidth; card.classList.add("swap");
    setTimeout(place, REDUCED ? 0 : 280); place();
    $('[data-t="next"]', card).onclick = () => show(i + 1);
    $('[data-t="skip"]', card).onclick = finish;
    const back = $('[data-t="back"]', card); if (back) back.onclick = () => show(i - 1);
    $('[data-t="next"]', card).focus({ preventScroll: true });
  };
  const outro = () => {
    target = null;
    Object.assign(spot.style, { top: `${innerHeight / 2}px`, left: `${innerWidth / 2}px`, width: "0px", height: "0px" });
    card.dataset.side = "center";
    card.innerHTML = `<div class="t-done"><svg viewBox="0 0 52 52" aria-hidden="true"><circle cx="26" cy="26" r="24"/><path d="M15 27l7 7 15-16"/></svg></div>
      <h3>You're set up to verify before you wire</h3><p>Run a sample case next. The deepfake video one shows the voice & video forensics.</p>
      <footer><button class="btn secondary sm" data-t="close">Stay here</button><button class="btn primary sm" data-t="go">Run a sample case</button></footer>`;
    const cw = Math.min(380, innerWidth - 32);
    Object.assign(card.style, { width: `${cw}px`, left: `${(innerWidth - cw) / 2}px`, top: `${Math.max(16, innerHeight / 2 - 150)}px` });
    card.classList.remove("swap"); void card.offsetWidth; card.classList.add("swap");
    $('[data-t="close"]', card).onclick = finish;
    $('[data-t="go"]', card).onclick = () => { finish(); location.hash = "#/checks"; };
    $('[data-t="go"]', card).focus();
  };
  const key = (e) => {
    if (e.target.closest("input,textarea,select")) return;
    if (e.key === "Escape") finish();
    else if (e.key === "ArrowRight" && i < STEPS.length) show(i + 1);
    else if (e.key === "ArrowLeft" && i > 0) show(i - 1);
  };
  addEventListener("resize", place); addEventListener("scroll", place, true); document.addEventListener("keydown", key);
  requestAnimationFrame(() => { spot.classList.add("in"); card.classList.add("in"); });
  show(0);
}

export async function startTour(company, checks) {
  if (checks?.length) CHECKS = checks;
  if (document.querySelector(".tour-veil, .tour-spot")) return;
  const go = await film(company);
  if (go) await walk();
}
