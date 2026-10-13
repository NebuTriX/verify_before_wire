"""Turn raw uploads into labelled SourceDocs. Emails are parsed with the stdlib so that
headers (From, Reply-To, Return-Path, Authentication-Results) are facts, not AI output."""
from __future__ import annotations

import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

from .models import SourceDoc

AUDIO = {"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg", "audio/webm",
         "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/flac"}
DOCS = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
TEXT = {"text/plain", "message/rfc822"}
EXT_MIME = {
    ".eml": "message/rfc822", ".txt": "text/plain", ".pdf": "application/pdf",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".webm": "audio/webm",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
}
ALLOWED = AUDIO | DOCS | TEXT


def pdf_text(data: bytes) -> str | None:
    """Text layer of a PDF, used for quote verification and the no-AI fallback.
    Gemini still receives the original PDF bytes."""
    try:
        import io

        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:10]) or None
    except Exception:
        return None


class UnsupportedFile(ValueError):
    pass


def guess_mime(filename: str, declared: str | None) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = EXT_MIME.get(ext) or (declared or "").split(";")[0].strip().lower()
    if mime not in ALLOWED:
        raise UnsupportedFile(f"{filename}: {mime or 'unknown type'} is not supported")
    return mime


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _kind_label(mime: str, filename: str) -> str:
    if mime in AUDIO:
        return "voice_note"
    if mime == "message/rfc822":
        return "email"
    if mime in DOCS:
        return "invoice"
    return "transcript" if "transcript" in filename.lower() else "message"


class _Labeler:
    def __init__(self):
        self.counts: dict[str, int] = {}

    def __call__(self, base: str) -> str:
        n = self.counts.get(base, 0) + 1
        self.counts[base] = n
        return base if n == 1 else f"{base}_{n}"


AUTH_RE = re.compile(r"\b(spf|dkim|dmarc)=([a-z]+)", re.I)


def email_auth_results(headers: dict[str, str]) -> dict[str, str]:
    """{'spf': 'pass'|'fail'|..., 'dkim': ..., 'dmarc': ...} from Authentication-Results."""
    out: dict[str, str] = {}
    for m in AUTH_RE.finditer(headers.get("Authentication-Results", "")):
        out.setdefault(m.group(1).lower(), m.group(2).lower())
    if "spf" not in out and (rs := headers.get("Received-SPF")):
        out["spf"] = rs.split()[0].lower()
    return out


def addr_domain(addr: str | None) -> str | None:
    if not addr:
        return None
    _, email = parseaddr(addr)
    return email.rsplit("@", 1)[1].lower() if "@" in email else None


def parse_upload(filename: str, declared_mime: str | None, data: bytes,
                 label: _Labeler) -> list[tuple[SourceDoc, bytes]]:
    mime = guess_mime(filename, declared_mime)
    if mime != "message/rfc822":
        text = data.decode("utf-8", errors="replace") if mime == "text/plain" else None
        if mime == "application/pdf":
            text = pdf_text(data)
        doc = SourceDoc(label=label(_kind_label(mime, filename)), filename=filename,
                        mime_type=mime, sha256=sha256(data), size=len(data), text=text)
        return [(doc, data)]

    msg = BytesParser(policy=policy.default).parsebytes(data)
    keep = ["From", "To", "Cc", "Reply-To", "Return-Path", "Subject", "Date", "Message-ID",
            "Authentication-Results", "Received-SPF"]
    headers = {k: str(msg[k]) for k in keep if msg[k]}
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    header_block = "\n".join(f"{k}: {v}" for k, v in headers.items() if k != "Authentication-Results")
    email_doc = SourceDoc(label=label("email"), filename=filename, mime_type=mime,
                          sha256=sha256(data), size=len(data),
                          text=f"{header_block}\n\n{text.strip()}", headers=headers)
    out = [(email_doc, data)]
    for part in msg.iter_attachments():
        fname = part.get_filename() or "attachment"
        payload = part.get_payload(decode=True) or b""
        try:
            sub = parse_upload(fname, part.get_content_type(), payload, label)
        except UnsupportedFile:
            continue
        for doc, raw in sub:
            doc.filename = f"{filename} › {fname}"
            out.append((doc, raw))
    return out


def recipients(headers: dict[str, str]) -> list[str]:
    return [a for _, a in getaddresses([headers.get("To", ""), headers.get("Cc", "")]) if a]
