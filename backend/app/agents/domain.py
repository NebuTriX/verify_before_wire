"""Sender & domain agent: live RDAP + DNS lookups and the email's own authentication
headers. Answers 'is this message really from who it claims to be?'"""
from __future__ import annotations

import asyncio

from ..models import Evidence, Finding, Severity
from ..parsing import addr_domain, email_auth_results
from ..sources import SourceUnavailable
from .base import FREE_MAIL, AgentContext, Emit, domain_valid, lookalike_of

NAME = "domain"
TITLE = "Sender & domain"


async def run(ctx: AgentContext, emit: Emit) -> None:
    s, src = ctx.settings, ctx.sources
    sender = ctx.sender_address()
    domain = addr_domain(sender)
    if not domain and any(d.label == "erp_payment" for d in ctx.docs):
        await emit(Finding(agent=NAME, code="ERP_ORIGIN", severity=Severity.info,
                           title="Submitted by your ERP, not by email",
                           detail="There is no email sender to verify; the payment came from your own system.",
                           data_source="Payment request"))
        return
    if not domain:
        await emit(Finding(agent=NAME, code="NO_SENDER", severity=Severity.low,
                           title="No sender address to verify",
                           detail="The request has no email sender; domain checks were skipped.",
                           data_source="Payment request"))
        return

    if not domain_valid(domain):
        await emit(Finding(agent=NAME, code="MALFORMED_SENDER_DOMAIN", severity=Severity.high,
                           title="Sender domain is malformed",
                           detail="The sender address isn't a valid internet domain, so it can't be verified.",
                           evidence=[Evidence(source="email", excerpt=(sender or "")[:120])],
                           data_source="Domain format check"))
        return
    trusted_vendor = ctx.vendor.domains if ctx.vendor else []
    all_trusted = list(dict.fromkeys(s.company_domain_list + [d for v in ctx.vendors for d in v.domains]))

    # 1. Lookalike domain (deterministic)
    imitated = lookalike_of(domain, all_trusted)
    if imitated:
        await emit(Finding(
            agent=NAME, code="LOOKALIKE_DOMAIN", severity=Severity.critical,
            title=f"Sender domain imitates {imitated}",
            detail=f"Mail came from {domain}, which differs from the trusted domain {imitated} "
                   "by a character swap or lookalike glyph.",
            evidence=[Evidence(source="email", excerpt=sender or domain)],
            data_source="Trusted domain list (vendor master + company domains)",
            meta={"domain": domain, "imitates": imitated}))

    if domain in FREE_MAIL:
        await emit(Finding(
            agent=NAME, code="FREE_MAIL_SENDER", severity=Severity.high,
            title="Payment instruction from a free webmail address",
            detail=f"{domain} is a consumer mailbox; executives and vendors bill from corporate domains.",
            evidence=[Evidence(source="email", excerpt=sender or domain)], data_source="Free-mail provider list"))

    # 2. Reply-To / Return-Path diversion (from parsed headers)
    if ctx.email:
        h = ctx.email.headers
        for hdr in ("Reply-To", "Return-Path"):
            other = addr_domain(h.get(hdr))
            if other and other != domain:
                await emit(Finding(
                    agent=NAME, code=f"{hdr.upper().replace('-', '_')}_MISMATCH",
                    severity=Severity.high if hdr == "Reply-To" else Severity.medium,
                    title=f"{hdr} goes to a different domain",
                    detail=f"Replies would go to {other}, not {domain}: a classic way to hijack the thread.",
                    evidence=[Evidence(source="email", excerpt=f"{hdr}: {h.get(hdr)}")],
                    data_source="Email headers"))
        auth = email_auth_results(h)
        failed = [f"{k.upper()}={v}" for k, v in auth.items() if v in {"fail", "softfail", "permerror"}]
        if failed:
            await emit(Finding(
                agent=NAME, code="EMAIL_AUTH_FAILED", severity=Severity.high,
                title="Email failed sender authentication",
                detail=f"The receiving mail server recorded {', '.join(failed)}: the message was not "
                       f"sent by a server authorised for {domain}.",
                evidence=[Evidence(source="email", excerpt=h.get("Authentication-Results", "")[:240])],
                data_source="Email headers (Authentication-Results)"))
        elif auth:
            await emit(Finding(
                agent=NAME, code="EMAIL_AUTH_PASSED", severity=Severity.info,
                title="Email passed SPF/DKIM/DMARC",
                detail="Authentication proves which domain sent it, not that the mailbox wasn't compromised.",
                evidence=[Evidence(source="email", excerpt=", ".join(f"{k}={v}" for k, v in auth.items()))],
                data_source="Email headers (Authentication-Results)"))

    # 3. Live lookups in parallel
    rdap_t = asyncio.create_task(src.rdap.lookup(domain))
    dns_t = asyncio.create_task(src.dns.inspect(domain))
    try:
        reg = await rdap_t
        if not reg.found:
            sev = Severity.medium if domain in trusted_vendor else Severity.high
            await emit(Finding(
                agent=NAME, code="DOMAIN_NOT_REGISTERED", severity=sev,
                title=f"{domain} has no registration record",
                detail="The registry has no record of this domain, so no legitimate company operates it. "
                       "The message was spoofed or the domain has expired.",
                evidence=[Evidence(source=src.rdap.name, excerpt=f"{domain}: 404 not found")],
                data_source=src.rdap.name))
        elif reg.age_days is not None:
            age = reg.age_days
            sev = (Severity.critical if age < 30 else Severity.high if age < s.new_domain_days_high
                   else Severity.medium if age < s.new_domain_days_medium else Severity.info)
            if domain in trusted_vendor:  # verified at onboarding; age is context, not a flag
                sev = Severity.info
            await emit(Finding(
                agent=NAME, code="DOMAIN_AGE", severity=sev,
                title=f"{domain} was registered {age} days ago" if sev != Severity.info
                else f"{domain} is an established domain ({age // 365} years old)",
                detail=f"Registered {reg.registered_on:%d %b %Y}"
                       + (f" via {reg.registrar}" if reg.registrar else "") + ".",
                evidence=[Evidence(source=src.rdap.name,
                                   excerpt=f"registration: {reg.registered_on:%Y-%m-%d}")],
                data_source=src.rdap.name))
    except SourceUnavailable as e:
        await emit(Finding(agent=NAME, code="RDAP_UNAVAILABLE", source_unavailable=True, severity=Severity.low,
                           title="Domain registry lookup unavailable",
                           detail=f"{e}. Domain age was not verified.", data_source=src.rdap.name))

    try:
        d = await dns_t
        if not d.exists:
            pass  # covered by RDAP result above
        elif not d.mx:
            await emit(Finding(
                agent=NAME, code="NO_MX", severity=Severity.medium,
                title=f"{domain} cannot receive email",
                detail="No MX records: a real business domain normally accepts mail.",
                evidence=[Evidence(source=src.dns.name, excerpt=f"MX {domain}: none")],
                data_source=src.dns.name))
        if d.exists and not d.dmarc:
            await emit(Finding(
                agent=NAME, code="NO_DMARC", severity=Severity.low,
                title=f"{domain} publishes no DMARC policy",
                detail="Anyone can send mail that claims to be from this domain.",
                evidence=[Evidence(source=src.dns.name, excerpt=f"_dmarc.{domain}: no record")],
                data_source=src.dns.name))
        elif d.dmarc:
            await emit(Finding(
                agent=NAME, code="DMARC_PRESENT", severity=Severity.info,
                title=f"{domain} enforces DMARC p={d.dmarc_policy}",
                detail="Spoofing this exact domain is blocked by receiving servers.",
                evidence=[Evidence(source=src.dns.name, excerpt=d.dmarc[:120])],
                data_source=src.dns.name))
    except SourceUnavailable as e:
        await emit(Finding(agent=NAME, code="DNS_UNAVAILABLE", source_unavailable=True, severity=Severity.low,
                           title="DNS lookup unavailable", detail=str(e), data_source=src.dns.name))
