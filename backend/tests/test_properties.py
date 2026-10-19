"""Property-based tests: generate thousands of hostile or random inputs and check that the
parsers and the policy engine never crash and never return an unsafe answer."""
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.agents.base import gstin_valid, iban_valid, lookalike_of, swift_valid
from app.guard import redact, screen
from app.models import Decision, Finding, Severity, SourceDoc
from app.onboarding import parse_payments, parse_vendors
from app.parsing import UnsupportedFile, Labeler, parse_upload
from app.policy import PolicySettings, Policy, build_facts, decide

FAST = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@FAST
@given(st.text())
def test_validators_never_crash(s):
    iban_valid(s), swift_valid(s), gstin_valid(s), redact(s)
    lookalike_of(s.lower()[:60] or "x.com", ["acme-holdings.com"])


@FAST
@given(st.binary(max_size=4000), st.sampled_from(["a.eml", "a.txt", "a.pdf", "a.png", "a.mp3"]))
def test_parser_never_crashes(data, name):
    try:
        parse_upload(name, None, data, Labeler())
    except UnsupportedFile:
        pass


@FAST
@given(st.text(max_size=3000))
def test_csv_import_never_crashes(text):
    parse_vendors(text)
    parse_payments(text, {"v1"})


@FAST
@given(st.text(max_size=2000))
def test_guard_never_crashes(text):
    screen([SourceDoc(label="email", filename="x", mime_type="text/plain", sha256="", size=1, text=text)])


sev = st.sampled_from(list(Severity))


@FAST
@given(st.lists(st.tuples(st.text(min_size=1, max_size=8), sev), max_size=12),
       st.lists(st.sampled_from(["vendor", "domain", "history", "sanctions", "language"]), unique=True))
def test_policy_is_safe(items, failed):
    findings = [Finding(agent="t", code=c, severity=s, title=c, detail="", data_source="t") for c, s in items]
    p = Policy(tenant_id="t", version=1, name="p", created_by="u", settings=PolicySettings())
    out = decide(p, findings, build_facts(findings, {"amount": 10}, True, "SG", failed, [], 10.0))
    assert 0 <= out.score <= 100
    if any(f.severity == Severity.critical for f in findings):
        assert out.decision == Decision.hold          # critical always holds under default policy
    if set(failed) & {"vendor", "sanctions"}:
        assert out.decision != Decision.release       # fail closed
