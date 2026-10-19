"""AI verification calls: answers need verbatim proof, outcomes follow a fixed rule, a person accepts."""
import shutil
import subprocess

import pytest

from app import calls
from app.models import AiCall, CallAnswer
from conftest import OWNER, H
from test_integrations import payment, setup

ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _call(*decisive_answers):
    return AiCall(id="call_x", vendor_id="v", vendor_name="V", contact="Mei", number="+65 1",
                  answers=[CallAnswer(question=f"Q{i}") for i in range(len(decisive_answers))] +
                  [CallAnswer(question="Anything else?", decisive=False)])


def test_quote_must_appear_in_transcript():
    assert calls.quote_in("Yes, that's right", "Mei here. yes that's right, we sent it")
    assert not calls.quote_in("Yes we changed banks", "Mei here. No, we did not change anything")
    assert not calls.quote_in("", "anything")


def test_polarity_is_conservative():
    assert calls.polarity("Yes, that's correct")[0] == "yes"
    assert calls.polarity("No, we never sent that")[0] == "no"
    assert calls.polarity("Yes... no, not that account")[0] == "unclear"
    assert calls.polarity("Let me check with my manager")[0] == "unclear"
    assert calls.polarity("No, we did not")[0] == "no"
    assert calls.polarity("Not sure, sorry")[0] == "unclear"


def M(i, a, q):
    return calls._ModelAnswer(index=i, answer=a, quote=q)


def test_confirmed_only_with_verified_quotes():
    c = _call(1, 1, 1)
    t = "Yes, this is Mei. Yes, we asked for that payment. Yes, the new account is ours."
    calls.apply_answers(c, t, [M(0, "yes", "Yes, this is Mei"), M(1, "yes", "Yes, we asked for that payment"),
                               M(2, "yes", "the new account is ours")], None, "gemini")
    assert c.outcome == "confirmed" and all(a.verified for a in c.answers[:3])


def test_invented_quote_is_discarded():
    """A model 'quote' that is not in the transcript counts for nothing (no hallucinated confirmations)."""
    c = _call(1, 1, 1)
    t = "Yes, this is Mei. Hmm, I'd have to check."
    calls.apply_answers(c, t, [M(0, "yes", "Yes, this is Mei"), M(1, "yes", "Yes we sent the request"),
                               M(2, "yes", "Yes the account is ours")], None, "gemini")
    assert c.outcome == "unclear" and [a.verified for a in c.answers[:3]] == [True, False, False]


def test_denial_and_disagreeing_transcripts():
    c = _call(1, 1, 1)
    t = "Yes, this is Mei. No, we never asked for that. No."
    calls.apply_answers(c, t, [M(0, "yes", "Yes, this is Mei"), M(1, "no", "No, we never asked for that")], None, "gemini")
    assert c.outcome == "denied"
    d = _call(1, 1, 1)
    calls.apply_answers(d, "Yes this is Mei. Yes we did. Yes it is.",
                        [M(0, "yes", "Yes this is Mei"), M(1, "yes", "Yes we did"), M(2, "yes", "Yes it is")],
                        ["Yes this is Mei", "No we did not", "Yes it is"], "gemini")
    assert d.answers[1].answer == "unclear" and d.outcome == "unclear"      # second transcript contradicts


def _wav() -> bytes:
    return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=1:a=0.01", "-f", "wav", "-"],
                          capture_output=True, check=True).stdout


@ffmpeg
def test_call_flow_browser_channel(client, monkeypatch):
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key,
                      json=payment("CALL:1", account="000011117777")).json()["case_id"]
    r = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls", headers=H(OWNER))
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["channel"] == "browser" and call["number"] == "+65 6123 4567"          # from the vendor master
    assert any("ending in 7777" in a["question"] for a in call["answers"])

    async def fake_json(schema, system, parts, attempts=2):
        assert schema.__name__ == "CallAnalysis"
        return calls.CallAnalysis(transcript="Yes, Mei Ling speaking. Yes, we sent that invoice. Yes, that new account is ours.",
                                  answers=[M(0, "yes", "Yes, Mei Ling speaking"), M(1, "yes", "Yes, we sent that invoice"),
                                           M(2, "yes", "that new account is ours")])
    monkeypatch.setattr(client.app.state.deps.gemini, "json", fake_json)
    up = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls/{call['id']}/recording", headers=H(OWNER),
                     files={"file": ("answers.wav", _wav(), "audio/wav")},
                     data={"browser_answers": '["Yes Mei Ling speaking", "Yes we sent that invoice", "Yes that new account is ours", ""]'})
    assert up.status_code == 200, up.text
    done = up.json()
    assert done["outcome"] == "confirmed" and done["recording_sha256"] and done["transcript_by"] == "gemini"
    assert client.get(f"/api/t/{tid}/cases/{cid}/ai-calls/{call['id']}/recording", headers=H(OWNER)).content[:4] == b"RIFF"
    acc = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls/{call['id']}/accept", headers=H(OWNER))
    assert acc.status_code == 200, acc.text
    wf = client.get(f"/api/t/{tid}/cases/{cid}", headers=H(OWNER)).json()["workflow"]
    assert wf["callbacks"][-1]["number_source"] == "vendor_master" and wf["callbacks"][-1]["outcome"] == "confirmed"
    assert wf["state"] == "awaiting_decision"                    # a person still decides
    acts = [e["action"] for e in client.get(f"/api/t/{tid}/audit", headers=H(OWNER)).json()]
    assert {"case.ai_call_started", "case.ai_call_completed", "case.callback_recorded"} <= set(acts)


@ffmpeg
def test_unclear_call_cannot_be_accepted(client, monkeypatch):
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("CALL:2", account="000022228888")).json()["case_id"]
    call = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls", headers=H(OWNER)).json()

    async def no_ai(*a, **k):
        from app.gemini import GeminiError
        raise GeminiError("AI credits used up", paused=True)
    monkeypatch.setattr(client.app.state.deps.gemini, "json", no_ai)
    up = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls/{call['id']}/recording", headers=H(OWNER),
                     files={"file": ("a.wav", _wav(), "audio/wav")},
                     data={"browser_answers": '["Hello?", "I would need to check", "Not sure", ""]'}).json()
    assert up["transcript_by"] == "browser" and up["outcome"] == "unclear" and "unavailable" in up["error"]
    assert client.post(f"/api/t/{tid}/cases/{cid}/ai-calls/{call['id']}/accept", headers=H(OWNER)).status_code == 409


def test_no_call_without_vendor_on_file(client):
    tid, key = setup(client)
    p = payment("NOVENDOR:1")
    p["vendor"] = {"id": "0000999999", "name": "Quantum Unknown Holdings", "country": "SG"}
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=p).json()["case_id"]
    r = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls", headers=H(OWNER))
    assert r.status_code == 409 and "vendor master" in r.json()["detail"]


def test_twilio_webhooks_require_a_valid_signature(client):
    from app.voice import twilio_signature
    d = client.app.state.deps
    d.settings.twilio_auth_token, d.settings.public_base_url = "secret-token", "https://vbw.example.com"
    tid, key = setup(client)
    cid = client.post(f"/api/t/{tid}/erp/payments?wait=30", headers=key, json=payment("TW:1", account="999988887777")).json()["case_id"]
    call = client.post(f"/api/t/{tid}/cases/{cid}/ai-calls", headers=H(OWNER)).json()
    path = f"/api/telephony/twilio/{tid}/{cid}/{call['id']}/q/1"
    form = {"CallSid": "CA123", "SpeechResult": "Yes, Mei speaking"}
    assert client.post(path, data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    sig = twilio_signature("secret-token", "https://vbw.example.com" + path, form)
    r = client.post(path, data=form, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200 and "<Gather" in r.text and "Did" in r.text
    saved = client.get(f"/api/t/{tid}/cases/{cid}", headers=H(OWNER)).json()["workflow"]["ai_calls"][0]
    assert saved["second_transcript"][0] == "Yes, Mei speaking"
