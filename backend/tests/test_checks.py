from app.agents.base import iban_valid, lookalike_of, swift_valid
from app.agents.language import verify_quote
from app.agents.verdict import decide
from app.models import Decision, Finding, Severity
from app.parsing import email_auth_results


def test_iban():
    assert iban_valid("GB82 WEST 1234 5698 7654 32")
    assert not iban_valid("GB82 WEST 1234 5698 7654 33")


def test_swift():
    assert swift_valid("DBSSSGSG") and swift_valid("HSBCHKHHXXX")
    assert not swift_valid("DBS1")


def test_lookalike():
    trusted = ["acme-holdings.com", "lioncitylogistics.com.sg"]
    assert lookalike_of("acme-hold1ngs.com", trusted) == "acme-holdings.com"
    assert lookalike_of("lioncity-logistics.com", trusted) == "lioncitylogistics.com.sg"
    assert lookalike_of("acme-holdings.com", trusted) is None
    assert lookalike_of("google.com", trusted) is None


def test_auth_results():
    h = {"Authentication-Results": "mx; spf=fail smtp.mailfrom=x; dkim=none; dmarc=fail (p=NONE)"}
    assert email_auth_results(h) == {"spf": "fail", "dkim": "none", "dmarc": "fail"}


def test_quote_verification():
    src = "I’m in board meetings all day so I can't take calls."
    assert verify_quote("I'm in board meetings all day", src)
    assert not verify_quote("wire it to my personal account", src)


def f(code, sev):
    return Finding(agent="t", code=code, severity=sev, title=code, detail="", data_source="t")


def test_decide():
    assert decide([f("A", Severity.info)])[0] == Decision.release
    assert decide([f("A", Severity.high)])[0] == Decision.callback
    assert decide([f("A", Severity.critical)])[0] == Decision.hold
    assert decide([f("A", Severity.high), f("B", Severity.high)])[0] == Decision.hold
    # duplicates of one code count once
    assert decide([f("A", Severity.high), f("A", Severity.high)])[1] == 30
