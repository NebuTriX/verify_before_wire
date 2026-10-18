"""Voice & video forensics on real files: the synthetic CFO voice note, the still-image 'video
message', and generated clips that look like genuine recordings."""
import shutil
import subprocess

import pytest

from app import media
from conftest import OWNER, H, make_workspace, run_case

pytestmark = pytest.mark.skipif(not media.available(), reason="ffmpeg not installed")
DEMO = media.Path(__file__).resolve().parents[2] / "demo" / "files"


def clip(*args: str) -> bytes:
    return subprocess.run([shutil.which("ffmpeg"), "-v", "error", *args, "-f", "mp4", "-movflags", "frag_keyframe+empty_moov", "-"],
                          capture_output=True, check=True).stdout


def test_synthetic_voice_has_digital_silence():
    r = media.analyse((DEMO / "ceo_voice_note.wav").read_bytes())
    assert r.kind == "audio" and r.silent_ratio > 0.05 and r.noise_floor_db <= -100


def test_still_image_video_is_detected():
    r = media.analyse((DEMO / "ceo_video_message.mp4").read_bytes())
    assert r.kind == "video" and r.motion < 0.002 and r.speech_ratio > 0.1


def test_camera_like_clip_passes():
    data = clip("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=3", "-f", "lavfi", "-i", "anoisesrc=d=3:c=pink:a=0.02",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    r = media.analyse(data)
    assert r.motion > 0.01 and r.silent_ratio == 0 and r.noise_floor_db > -100 and r.synth_tool is None


def test_generator_named_in_metadata():
    data = clip("-f", "lavfi", "-i", "anoisesrc=d=1:a=0.01", "-c:a", "aac", "-metadata", "comment=Generated with ElevenLabs v2")
    assert media.analyse(data).synth_tool == "elevenlabs"


def test_video_case_is_held_on_forensic_evidence(client):
    tid = make_workspace(client, team=False)
    cid, events = run_case(client, tid, "ceo-video", who=OWNER)
    case = client.get(f"/api/t/{tid}/cases/{cid}", headers=H(OWNER)).json()
    codes = {f["code"] for f in case["findings"] if f["agent"] == "media"}
    assert {"DIGITAL_SILENCE", "STATIC_VIDEO"} <= codes
    assert case["verdict"]["decision"] == "HOLD"
    assert any(e["type"] == "source.done" and e["data"].get("source") == "forensics" for e in events)
