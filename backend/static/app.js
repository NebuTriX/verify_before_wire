// Verify-Before-Wire frontend: upload or pick a sample, then render the live event stream.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TERMINAL = new Set(["case.completed", "case.failed", "case.stalled"]);
let files = [];
let source = null;
let replay = new URLSearchParams(location.search).get("replay") === "1";
let currentCase = null;

function setMode() {
  $("mode").textContent = replay ? "recorded playback" : "live";
  $("mode").classList.toggle("replay", replay);
}
setMode();
document.addEventListener("keydown", (e) => {
  if (e.shiftKey && e.key.toLowerCase() === "r" && !e.target.closest("input")) { replay = !replay; setMode(); }
});

// ---------------------------------------------------------------- intake --
const drop = $("drop");
$("file").addEventListener("change", (e) => addFiles(e.target.files));
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") $("file").click(); });

function addFiles(list) {
  files = [...files, ...list].slice(0, 6);
  $("picked").innerHTML = files.map((f, i) =>
    `<li><span>${esc(f.name)}</span><button class="link" data-i="${i}" aria-label="Remove ${esc(f.name)}">Remove</button></li>`).join("");
  $("picked").querySelectorAll("button").forEach((b) => b.onclick = () => { files.splice(+b.dataset.i, 1); addFiles([]); });
  $("go").disabled = files.length === 0;
}

$("go").addEventListener("click", async () => {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  await start(() => fetch("/api/cases", { method: "POST", body: form }));
});

async function loadDemos() {
  try {
    const demos = await (await fetch("/api/demo")).json();
    $("demo-list").innerHTML = demos.map((d) =>
      `<button class="demo" data-id="${esc(d.id)}">${esc(d.title)}<small>${esc(d.description)}</small></button>`).join("");
    $("demo-list").querySelectorAll("button").forEach((b) =>
      b.onclick = () => start(() => fetch(`/api/demo/${b.dataset.id}`, { method: "POST" })));
  } catch { $("demo-list").textContent = "Sample cases are unavailable."; }
}
loadDemos();

// ------------------------------------------------------------- the case --
async function start(create) {
  if (source) source.close();
  resetBoard();
  status("Uploading the request…");
  let res;
  try { res = await create(); } catch { return status("Could not reach the server. Check your connection and try again."); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) return status(body.detail || "The request could not be opened.");
  currentCase = body.case_id;
  status("Reading the request…");
  source = new EventSource(`/api/cases/${currentCase}/stream${replay ? "?replay=1" : ""}`);
  const types = ["case.started", "intake.started", "intake.completed", "agent.started", "finding",
                 "agent.completed", "agent.failed", "verdict", "case.completed", "case.failed", "case.stalled"];
  types.forEach((t) => source.addEventListener(t, (e) => {
    const ev = JSON.parse(e.data);
    handle(ev);
    if (TERMINAL.has(ev.type)) source.close();
  }));
  source.onerror = () => { if (source.readyState === EventSource.CLOSED) status("The connection closed. Reload to see the saved result."); };
}

function resetBoard() {
  $("lanes").innerHTML = ""; $("verdict").hidden = true; $("slip").hidden = true;
}
function status(t) { $("status").textContent = t; }

const lanes = {};
function lane(id, title) {
  if (!lanes[id] || !document.body.contains(lanes[id])) {
    const el = document.createElement("article");
    el.className = "lane"; el.id = `lane-${id}`;
    el.innerHTML = `<header><h3>${esc(title)}</h3><span class="state">Queued</span></header><div class="items"></div>`;
    $("lanes").append(el); lanes[id] = el;
  }
  return lanes[id];
}

function handle(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "case.started":
      resetBoard();
      (d.agents || []).forEach((a) => lane(a.id, a.title));
      break;
    case "intake.started":
      status(`Reading ${d.inputs.length} input${d.inputs.length > 1 ? "s" : ""} with Gemini…`); break;
    case "intake.completed": showSlip(d.request, d.notes); status("Five checks running in parallel…"); break;
    case "agent.started":
      if (ev.agent === "verdict") { status("Weighing the evidence…"); break; }
      setState(ev.agent, "running", "Checking"); break;
    case "agent.completed": setState(ev.agent, "done", `${d.findings} result${d.findings === 1 ? "" : "s"} · ${(d.ms / 1000).toFixed(1)}s`); break;
    case "agent.failed": setState(ev.agent, "failed", "Check failed — treat as unverified"); break;
    case "finding": addFinding(ev.agent, d); break;
    case "verdict": showVerdict(d); break;
    case "case.completed": status(`Finished in ${(d.ms / 1000).toFixed(1)} seconds.`); break;
    case "case.failed": status(`The investigation stopped: ${d.error}. Do not release the payment.`); break;
    case "case.stalled": status("The investigation stopped responding. Run it again."); break;
  }
}

function setState(agent, cls, text) {
  const el = lanes[agent]; if (!el) return;
  el.classList.remove("running", "done", "failed"); el.classList.add(cls);
  el.querySelector(".state").textContent = text;
}

function addFinding(agent, f) {
  const el = lanes[agent] || lane(agent, agent);
  const ev = (f.evidence || []).filter((e) => e.excerpt).slice(0, 2)
    .map((e) => `<q>${esc(e.excerpt)}</q><cite>${esc(e.source)}</cite>`).join("");
  const item = document.createElement("div");
  item.className = `finding sev-${f.severity}`;
  item.innerHTML = `<b>${esc(f.title)}</b><p>${esc(f.detail)}</p>${ev}<cite>Source: ${esc(f.data_source)}</cite>`;
  // red flags first, passed checks after
  const items = el.querySelector(".items");
  if (f.severity === "info") items.append(item);
  else items.insertBefore(item, items.querySelector(".sev-info"));
}

function showSlip(r, notes) {
  const rows = [
    ["Requested by", [r.requester_name, r.requester_claimed_role && `(${r.requester_claimed_role})`].filter(Boolean).join(" ")],
    ["From", r.requester_email],
    ["Amount", r.amount != null ? `${r.currency || ""} ${Number(r.amount).toLocaleString()}` : null],
    ["Pay to", r.beneficiary_name], ["Bank", r.beneficiary_bank],
    ["Account", r.iban || r.account_number], ["SWIFT", r.swift_bic],
    ["Invoice", r.invoice_number], ["Deadline", r.deadline],
  ].filter(([, v]) => v);
  $("slip-fields").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
  $("slip-notes").textContent = (notes || []).join(" ");
  $("slip").hidden = false;
}

function showVerdict(v) {
  $("stamp").textContent = v.decision === "CALLBACK" ? "CALL BACK" : v.decision;
  $("stamp").className = `stamp ${v.decision}`;
  $("score").textContent = `${v.risk_score}/100`;
  $("summary").textContent = v.summary;
  $("callback").innerHTML = v.decision === "RELEASE" ? "" :
    `<h3>Before releasing, call ${esc(v.callback_contact || "the requester via the company directory")}` +
    `${v.callback_number ? ` on ${esc(v.callback_number)}` : ""}</h3>` +
    `<ol>${v.callback_script.map((l) => `<li>${esc(l)}</li>`).join("")}</ol>`;
  $("pdf").href = `/api/cases/${currentCase}/report.pdf`;
  $("verdict").hidden = false;
  status(v.decision === "RELEASE" ? "Cleared to release." : "Payment stopped for review.");
}
