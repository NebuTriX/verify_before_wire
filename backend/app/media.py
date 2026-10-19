"""Media forensics for voice notes and video messages, computed on this server with ffmpeg and
numpy (no media leaves the installation for these checks).

What is measured, and why it is only ever a *signal*:
  * Digital silence: microphones always record some room noise, so real recordings almost never
    contain stretches of exact zero samples. Text-to-speech and spliced/edited audio often do.
    Aggressive noise suppression in some apps can also produce it, hence "signal", not "proof".
  * Container metadata: phone and camera recordings carry device make/model and recording time;
    generated or re-rendered files usually carry only an encoder tag, sometimes naming the tool.
  * Motion: a "video message" whose frames barely change while someone is speaking is a still image
    with a voice-over: the cheapest way to fake a video call from a photo.
None of this decides a payment. The request itself is verified against your records either way.
"""
from __future__ import annotations

import json
import shutil
# ffmpeg/ffprobe are run with fixed argument lists, never through a shell; the input is a temp file path.
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SYNTH_TOOLS = ("elevenlabs", "resemble", "descript", "murf", "play.ht", "playht", "wellsaid", "speechify",
               "synthesia", "heygen", "d-id", "deepfacelab", "faceswap", "hourone", "colossyan", "tts", "text-to-speech")
TIMEOUT_S = 30


class MediaUnavailable(RuntimeError):
    pass


@dataclass
class MediaReport:
    kind: str                                  # audio | video
    duration_s: float = 0.0
    sample_rate: int | None = None
    channels: int | None = None
    video_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    silent_ratio: float | None = None          # share of 20 ms frames that are exactly zero
    noise_floor_db: float | None = None        # 10th-percentile frame level, dBFS
    speech_ratio: float | None = None          # share of frames carrying voice-level energy
    motion: float | None = None                # mean absolute change between frames, 0..1
    device: str | None = None
    synth_tool: str | None = None

    def summary(self) -> str:
        parts = [f"{self.duration_s:.1f} s"]
        if self.kind == "video" and self.width:
            parts.append(f"{self.width}×{self.height}{f' @ {self.fps:.0f} fps' if self.fps else ''}")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz {'mono' if self.channels == 1 else f'{self.channels} ch'}")
        return ", ".join(parts)


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args: list[str], data: bytes | None = None) -> bytes:
    exe = shutil.which(args[0])               # absolute path; fixed argv, never a shell
    if exe is None:
        raise MediaUnavailable("ffmpeg is not installed on this server")
    try:
        return subprocess.run([exe, *args[1:]], input=data, capture_output=True, check=True,  # noqa: S603 # nosec
                              timeout=TIMEOUT_S).stdout
    except FileNotFoundError as e:
        raise MediaUnavailable("ffmpeg is not installed on this server") from e
    except subprocess.TimeoutExpired as e:
        raise MediaUnavailable("media analysis timed out") from e
    except subprocess.CalledProcessError as e:
        raise MediaUnavailable(f"the file could not be decoded ({(e.stderr or b'').decode(errors='replace')[-160:].strip()})") from e


WAV_RATE = 16000                                   # to_wav output: 16 kHz, mono, 16-bit
WAV_BYTES_PER_S = WAV_RATE * 2


def to_wav(data: bytes) -> bytes:
    """Any audio/video -> 16 kHz mono 16-bit WAV (one format to store, play back and transcribe)."""
    with tempfile.TemporaryDirectory(prefix="vbw-media-") as tmp:
        f = Path(tmp) / "input.media"
        f.write_bytes(data)
        return _run(["ffmpeg", "-v", "error", "-i", str(f), "-vn", "-ac", "1", "-ar", str(WAV_RATE), "-f", "wav", "-"])


def analyse(data: bytes) -> MediaReport:
    """Blocking: run in a thread."""
    import numpy as np
    if not available():
        raise MediaUnavailable("ffmpeg is not installed on this server")
    with tempfile.TemporaryDirectory(prefix="vbw-media-") as tmp:   # a path, not an open handle: works on Windows too
        f = Path(tmp) / "input.media"
        f.write_bytes(data)
        probe = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(f)]))
        streams = probe.get("streams", [])
        fmt = probe.get("format", {})
        v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        rep = MediaReport(kind="video" if v else "audio", duration_s=float(fmt.get("duration") or 0))
        tags = {str(k).lower(): str(val) for k, val in (fmt.get("tags") or {}).items()}
        for s in streams:
            tags.update({str(k).lower(): str(val) for k, val in (s.get("tags") or {}).items() if k.lower() in ("encoder", "handler_name")})
        rep.tags = {k: val[:120] for k, val in tags.items() if k not in ("major_brand", "minor_version", "compatible_brands", "language")}
        blob = " ".join(rep.tags.values()).lower()
        rep.synth_tool = next((t for t in SYNTH_TOOLS if t in blob), None)
        make = tags.get("com.apple.quicktime.make") or tags.get("make")
        model = tags.get("com.apple.quicktime.model") or tags.get("model") or tags.get("com.android.manufacturer")
        rep.device = " ".join(x for x in (make, model) if x) or None
        if a:
            rep.sample_rate, rep.channels = int(a.get("sample_rate") or 0) or None, a.get("channels")
            pcm = np.frombuffer(_run(["ffmpeg", "-v", "error", "-i", str(f), "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]),
                                np.int16)
            if len(pcm) >= 320 * 10:
                frames = pcm[: len(pcm) // 320 * 320].reshape(-1, 320).astype(np.float32) / 32768
                level = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-12)
                rep.silent_ratio = float((np.abs(frames).max(1) <= 1.5 / 32768).mean())
                rep.noise_floor_db = float(max(np.percentile(level, 10), -150.0))
                rep.speech_ratio = float((level > level.max() - 30).mean())
        if v:
            rep.video_codec, rep.width, rep.height = v.get("codec_name"), v.get("width"), v.get("height")
            num, _, den = str(v.get("avg_frame_rate") or "0/1").partition("/")
            rep.fps = float(num) / float(den) if den and float(den) else None
            gray = np.frombuffer(_run(["ffmpeg", "-v", "error", "-i", str(f), "-an", "-vf", "fps=4,scale=48:27,format=gray",
                                       "-f", "rawvideo", "-"]), np.uint8)
            n = len(gray) // (48 * 27)
            if n >= 3:
                fr = gray[: n * 48 * 27].reshape(n, -1).astype(np.float32) / 255
                rep.motion = float(np.abs(np.diff(fr, axis=0)).mean())
    return rep
