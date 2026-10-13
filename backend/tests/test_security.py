"""Security regression tests: each one pins down a specific attack we defend against."""
import email.message

import pytest

from app.blobs import FileBlobs, _check
from app.parsing import UnsupportedFile, _Labeler, parse_upload
from tests.conftest import OWNER, H, make_workspace


def test_blob_keys_cannot_escape_storage(tmp_path):
    for bad in ("../etc/passwd", "/abs/path", "a/../../b", "a b", "x\x00y"):
        with pytest.raises(ValueError):
            _check(bad)
    fb = FileBlobs(str(tmp_path))
    with pytest.raises(ValueError):
        fb._path("..%2F..")  # not in allowed charset


def _nest(levels: int) -> bytes:
    inner = email.message.EmailMessage()
    inner["From"], inner["Subject"] = "a@b.com", "x"
    inner.set_content("hello")
    raw = bytes(inner)
    for _ in range(levels):
        m = email.message.EmailMessage()
        m["From"], m["Subject"] = "a@b.com", "wrap"
        m.set_content("see attached")
        m.add_attachment(raw, maintype="message", subtype="rfc822", filename="fwd.eml")
        raw = bytes(m)
    return raw


def test_deeply_nested_email_rejected():
    parse_upload("ok.eml", None, _nest(1), _Labeler())
    with pytest.raises(UnsupportedFile):
        parse_upload("bomb.eml", None, _nest(5), _Labeler())


def test_security_headers(client):
    r = client.get("/healthz")
    for h in ("Content-Security-Policy", "X-Frame-Options", "Strict-Transport-Security", "Permissions-Policy"):
        assert h in r.headers
    assert "unsafe-inline" not in r.headers["Content-Security-Policy"].split("script-src")[1].split(";")[0]


def test_oversized_request_rejected_before_reading(client):
    r = client.post("/api/workspaces", headers={**H(OWNER), "Content-Length": str(500 * 1024 * 1024)}, content=b"{}")
    assert r.status_code == 413


def test_bad_idempotency_key_rejected(client):
    tid = make_workspace(client, team=False)
    r = client.post(f"/api/t/{tid}/cases", headers={**H(OWNER), "Idempotency-Key": "x" * 300},
                    files=[("files", ("a.txt", b"pay USD 10 to x", "text/plain"))])
    assert r.status_code == 422


def test_dev_auth_refused_on_cloud_run(monkeypatch):
    from app.auth import assert_safe_auth_mode
    monkeypatch.setenv("K_SERVICE", "verify-before-wire")
    with pytest.raises(RuntimeError):
        assert_safe_auth_mode("dev", False)


def test_unauthenticated_requests_rejected(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/t/t_x/cases").status_code == 401
    assert client.get("/api/t/t_x/cases", headers={"Authorization": "Bearer vbw_nope_x"}).status_code == 401


def test_demo_disabled_by_default():
    from app.config import Settings
    assert Settings().demo_enabled is False
