// AI verification call: the agent calls the vendor on the number in the vendor master, asks fixed
// questions, records the answers and shows what was said, with each answer tied to the vendor's
// own words. Browser channel: the questions are spoken here and the microphone records the answers
// (demo, or speakerphone). Twilio channel: a real phone call; this screen follows its progress.
import { $, esc } from "./util.js";
const ANSWER = { yes: "Yes", no: "No", unclear: "Unclear" };
const OUTCOME = { confirmed: ["Confirmed by the vendor", "ok"], denied: ["The vendor denied it", "bad"], unclear: ["Not confirmed", "warn"] };
const initials = (s) => (s || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");

function dialog() {
  let d = $("#calldlg");
  if (!d) { d = document.createElement("dialog"); d.id = "calldlg"; d.className = "calldlg"; document.body.append(d); }
  return d;
}

// ctx: { api, T, cid, toast, canAccept, onChange }
export async function startCall(ctx) {
  const call = await ctx.api(ctx.T(`/cases/${ctx.cid}/ai-calls`), { method: "POST" });
  const d = dialog();
  if (call.channel === "twilio") return followPhoneCall(ctx, call, d);
  return browserCall(ctx, call, d);
}

export async function reviewCall(ctx, call) {
  const d = dialog(); renderResult(ctx, call, d); if (!d.open) d.showModal();
}

function header(call, status) {
  return `<header class="cl-head"><span class="cl-avatar" aria-hidden="true">${esc(initials(call.contact))}</span>
    <div><b>${esc(call.contact)}</b><span>${esc(call.vendor_name)}</span>
      <span class="cl-num">${esc(call.number)} <em>from your vendor master</em></span></div>
    <span class="cl-status" id="cl-status">${status}</span>
    <button class="sb-close cl-x" id="cl-x" aria-label="Close">×</button></header>`;
}

async function browserCall(ctx, call, d) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  d.innerHTML = `${header(call, "Ready")}
    <div class="cl-body"><ol class="cl-convo" id="cl-convo" aria-live="polite"></ol></div>
    <div class="cl-note"><b>Demo mode:</b> you answer as ${esc(call.contact)}. The agent speaks each question; your microphone
      records the answers. In production the same questions go over a real phone line (Twilio).</div>
    <footer class="cl-foot"><span class="cl-rec" id="cl-rec" hidden><i></i><span id="cl-time">00:00</span></span>
      <button class="btn secondary" id="cl-cancel">Cancel</button><button class="btn primary" id="cl-go">Start call</button></footer>`;
  if (!d.open) d.showModal();
  let uploaded = false;
  // Closing before the answers were submitted ends the call on the server too, so it never stays "in progress".
  const close = () => { cleanup(); d.close();
    if (!uploaded) ctx.api(ctx.T(`/cases/${ctx.cid}/ai-calls/${call.id}/cancel`), { method: "POST" }).catch(() => {});
    ctx.onChange(); };
  $("#cl-x", d).onclick = close; $("#cl-cancel", d).onclick = close;
  let stream, rec, chunks = [], recog, t0 = 0, tick, idx = -1, finished = false;
  const answers = call.answers.map(() => ""), marks = [];
  const convo = $("#cl-convo", d), status = (t) => { $("#cl-status", d).textContent = t; };
  function cleanup() {
    finished = true; clearInterval(tick);
    try { recog?.stop(); } catch { /* not started */ }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    stream?.getTracks().forEach((t) => t.stop());
  }
  // Some systems never fire onend (no voice installed): cap the wait by the text's length.
  const say = (text) => new Promise((res) => {
    if (!("speechSynthesis" in window)) return res();
    const done = () => { clearTimeout(cap); res(); }, cap = setTimeout(done, 1500 + text.split(/\s+/).length * 450);
    const u = new SpeechSynthesisUtterance(text); u.rate = 1.02; u.onend = done; u.onerror = done; speechSynthesis.speak(u);
  });
  const bubble = (who, text, cls = "") => { const li = document.createElement("li"); li.className = `cl-${who} ${cls}`;
    li.innerHTML = `<span>${who === "agent" ? "AI agent" : esc(call.contact)}</span><p>${esc(text)}</p>`; convo.append(li); li.scrollIntoView({ block: "end" }); return li; };
  const ask = async (i) => {
    idx = i; marks[i] = (performance.now() - t0) / 1000;
    if (i >= call.answers.length) return end();
    bubble("agent", call.answers[i].question);
    status(`Asking question ${i + 1} of ${call.answers.length}`);
    await say(call.answers[i].question);
    if (finished) return;
    const mine = bubble("vendor", SR ? "Listening…" : "Answer out loud, then press Next.", "live");
    $("#cl-go", d).textContent = i === call.answers.length - 1 ? "Finish call" : "Next question";
    $("#cl-go", d).disabled = false;
    if (recog) { recog.onresult = (e) => { let fin = "", tmp = "";
      for (const r of e.results) (r.isFinal ? (fin += r[0].transcript + " ") : (tmp += r[0].transcript));
      answers[i] = fin.trim(); $("p", mine).textContent = (fin + tmp).trim() || "Listening…"; }; }
  };
  const end = async () => {
    status("Ending call…"); $("#cl-go", d).disabled = true; clearInterval(tick);
    await say("Thank you. A colleague will review this call. Goodbye.");
    try { recog?.stop(); } catch { /* ignore */ }
    const stopped = new Promise((r) => { rec.onstop = r; }); rec.stop(); await stopped;
    stream.getTracks().forEach((t) => t.stop()); finished = true;
    status("Transcribing and checking answers…");
    $(".cl-foot", d).innerHTML = `<span class="spinner"></span><span class="muted">Gemini is transcribing the recording; every answer must quote the vendor's own words…</span>`;
    const fd = new FormData();
    fd.append("file", new Blob(chunks, { type: rec.mimeType || "audio/webm" }), "call.webm");
    fd.append("browser_answers", JSON.stringify(answers)); fd.append("marks", JSON.stringify(marks.slice(0, call.answers.length)));
    uploaded = true;
    try { const done = await ctx.api(ctx.T(`/cases/${ctx.cid}/ai-calls/${call.id}/recording`), { method: "POST", body: fd });
      renderResult(ctx, done, d); ctx.onChange(); }
    catch (e) { $(".cl-foot", d).innerHTML = `<p class="msg err">${esc(e.message)}</p><button class="btn secondary" id="cl-cancel">Close</button>`; $("#cl-cancel", d).onclick = close; }
  };
  $("#cl-go", d).onclick = async () => {
    if (idx === -1) {
      status("Allow microphone access…"); $("#cl-go", d).disabled = true;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: false } }); }
      catch { status("Microphone blocked"); $("#cl-go", d).disabled = false; bubble("agent", "The microphone is blocked. Allow it in the browser's address bar, or record the callback by hand in the Decision panel."); return; }
      rec = new MediaRecorder(stream); rec.ondataavailable = (e) => e.data.size && chunks.push(e.data); rec.start(250);
      if (SR) { recog = new SR(); recog.continuous = true; recog.interimResults = true; recog.lang = navigator.language || "en-US";
        recog.onend = () => { if (!finished) try { recog.start(); } catch { /* restarting */ } }; try { recog.start(); } catch { recog = null; } }
      t0 = performance.now(); $("#cl-rec", d).hidden = false;
      tick = setInterval(() => { const s = Math.floor((performance.now() - t0) / 1000); $("#cl-time", d).textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; }, 500);
      $("#cl-cancel", d).hidden = true; $("#cl-go", d).disabled = true;
      bubble("agent", `Calling ${call.number}…`, "sys"); status("Connected · recording");
      await say(`Hello, this is the automated payment verification assistant. This call is recorded.`);
      return ask(0);
    }
    $("#cl-go", d).disabled = true;
    const live = convo.querySelector(".cl-vendor.live:last-child");
    if (live) { live.classList.remove("live"); if (!answers[idx]) $("p", live).textContent = SR ? "(no speech recognised; the recording is still analysed)" : "(recorded)"; }
    return ask(idx + 1);
  };
}

async function followPhoneCall(ctx, call, d) {
  d.innerHTML = `${header(call, call.status === "failed" ? "Call failed" : "Calling over the phone network…")}
    <div class="cl-body"><p class="muted">${call.status === "failed" ? esc(call.error) : "The agent is asking the questions on a real phone call. This page updates when the recording has been analysed."}</p>
    <ol class="cl-qs">${call.answers.map((a) => `<li>${esc(a.question)}</li>`).join("")}</ol></div>
    <footer class="cl-foot"><button class="btn secondary" id="cl-cancel">Close</button></footer>`;
  if (!d.open) d.showModal();
  $("#cl-x", d).onclick = $("#cl-cancel", d).onclick = () => { d.close(); ctx.onChange(); };
  const POLL_MS = 3000, GIVE_UP_MS = 6 * 60 * 1000;      // a verification call rarely lasts more than a few minutes
  for (let waited = 0; waited < GIVE_UP_MS && d.open && call.status !== "failed"; waited += POLL_MS) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const c = (await ctx.api(ctx.T(`/cases/${ctx.cid}`))).workflow.ai_calls.find((x) => x.id === call.id);
    if (c?.status === "completed") return renderResult(ctx, c, d);
    if (c?.status === "failed") { $(".cl-body p", d).textContent = c.error || "The call failed."; return; }
  }
  if (d.open) $(".cl-body p", d).textContent = "Still waiting for the phone network. Close this window; the result will appear under Verification calls when it arrives.";
}

function renderResult(ctx, call, d) {
  const [otxt, ocls] = OUTCOME[call.outcome] || ["Pending", "warn"];
  const by = { gemini: "Transcribed by Gemini from the recording", browser: "Transcribed by the browser's speech recognition", twilio: "Transcribed by the phone network" }[call.transcript_by] || "No transcript";
  d.innerHTML = `${header(call, call.accepted_by ? `Accepted by ${esc(call.accepted_by)}` : "Call ended")}
    <div class="cl-body">
      <div class="cl-outcome ${ocls}"><b>${otxt}</b><p>${esc(call.outcome_basis || "")}</p></div>
      <ol class="cl-answers">${call.answers.map((a) => `<li class="${a.decisive ? "" : "minor"}">
        <p class="q">${esc(a.question)}</p>
        <p class="a">${a.decisive ? `<span class="ans ${a.answer}">${ANSWER[a.answer]}</span>` : `<span class="ans note">Said</span>`}
          ${a.quote ? `<q>${esc(a.quote)}</q>` : `<em class="muted">No clear answer</em>`}
          ${a.verified ? `<span class="proof" title="These exact words appear in the transcript">✓ found in transcript</span>` : ""}</p></li>`).join("")}</ol>
      <div class="cl-media"><button class="btn secondary sm" id="cl-play">Play recording</button><span class="muted small">${call.duration_s ? `${call.duration_s}s · ` : ""}SHA-256 <code title="${esc(call.recording_sha256 || "")}">${esc((call.recording_sha256 || "").slice(0, 12))}</code></span><div id="cl-audio"></div></div>
      <details class="cl-transcript"><summary>Transcript</summary><p class="small muted">${by}${call.second_transcript_agreement != null ? ` · agrees ${Math.round(call.second_transcript_agreement * 100)}% with a second, independent transcript` : ""}</p>
        <pre>${esc(call.transcript || "(empty)")}</pre></details>
      ${call.error ? `<p class="notice">${esc(call.error)}</p>` : ""}
      <p class="small muted">The AI never releases a payment. Accepting counts this recorded call as the callback your policy requires; an approver still decides.</p>
    </div>
    <footer class="cl-foot"><button class="btn secondary" id="cl-cancel">Close</button>
      ${!call.accepted_by && ctx.canAccept && ["confirmed", "denied"].includes(call.outcome) ? `<button class="btn primary" id="cl-accept">Accept as ${call.outcome === "confirmed" ? "confirmed" : "denied"} callback</button>` : ""}</footer>`;
  $("#cl-x", d).onclick = $("#cl-cancel", d).onclick = () => { d.close(); ctx.onChange(); };
  $("#cl-play", d).onclick = async () => {
    const res = await ctx.api(ctx.T(`/cases/${ctx.cid}/ai-calls/${call.id}/recording`), { raw: true });
    if (!res.ok) return ctx.toast("The recording couldn't be loaded.", "err");
    $("#cl-audio", d).innerHTML = `<audio controls autoplay src="${URL.createObjectURL(await res.blob())}"></audio>`;
  };
  const acc = $("#cl-accept", d);
  if (acc) acc.onclick = async () => { acc.disabled = true;
    try { await ctx.api(ctx.T(`/cases/${ctx.cid}/ai-calls/${call.id}/accept`), { method: "POST" });
      ctx.toast(call.outcome === "confirmed" ? "Callback recorded: confirmed by the vendor" : "Recorded: the vendor denied the request", call.outcome === "confirmed" ? "ok" : "warn");
      d.close(); ctx.onChange(); } catch (e) { acc.disabled = false; ctx.toast(e.message, "err"); } };
}
