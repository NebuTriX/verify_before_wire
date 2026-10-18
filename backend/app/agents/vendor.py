"""Vendor-record agent: compares the requested beneficiary with the vendor master.
Fully deterministic; Gemini is never asked whether an account 'looks right'."""
from __future__ import annotations

from ..models import Evidence, Finding, Severity
from ..sources import SourceUnavailable
from .base import AgentContext, Emit, gstin_valid, iban_valid, ifsc_valid, norm_account, swift_valid

NAME = "vendor"
TITLE = "Vendor record"
SRC = "Your vendor list"


async def run(ctx: AgentContext, emit: Emit) -> None:
    req, v = ctx.request, ctx.vendor

    if v is None:
        await emit(Finding(
            agent=NAME, code="VENDOR_UNKNOWN", severity=Severity.high,
            title="Payee is not in the vendor master",
            detail=f"No approved vendor matches '{req.vendor_name or req.beneficiary_name or 'unknown'}'. "
                   "New payees must go through vendor onboarding before any payment.",
            evidence=[Evidence(source="request", excerpt=req.vendor_name or req.beneficiary_name or "—")],
            data_source=SRC))
    else:
        await emit(Finding(
            agent=NAME, code="VENDOR_MATCHED", severity=Severity.info,
            title=f"Matched {v.legal_name}",
            detail=f"Matched because {ctx.vendor_match_reason}. Vendor on file since onboarding, "
                   f"{len(v.bank_accounts)} verified account(s).",
            evidence=[], data_source=SRC))

    req_acct = norm_account(req.iban or req.account_number)
    if v and req_acct:
        on_file = {norm_account(a.iban or a.account_number): a for a in v.bank_accounts}
        if req_acct in on_file:
            a = on_file[req_acct]
            await emit(Finding(
                agent=NAME, code="ACCOUNT_ON_FILE", severity=Severity.info,
                title="Bank account matches the verified account on file",
                detail=f"{a.bank_name}, verified {a.verified_on}.",
                evidence=[Evidence(source="request", excerpt=req.iban or req.account_number or "")],
                data_source=SRC))
        else:
            known = ", ".join(f"{a.bank_name} …{norm_account(a.iban or a.account_number)[-4:]}"
                              for a in v.bank_accounts)
            await emit(Finding(
                agent=NAME, code="BANK_ACCOUNT_CHANGED", severity=Severity.critical,
                title="Bank account differs from the verified account on file",
                detail=f"Request pays {req.beneficiary_bank or 'an unknown bank'} account "
                       f"…{req_acct[-4:]}. On file: {known}. Account changes are the most common "
                       "signature of payment-diversion fraud.",
                evidence=[Evidence(source="request", excerpt=req.iban or req.account_number or "")],
                data_source=SRC))

    if v and req.beneficiary_name:
        from rapidfuzz import fuzz
        names = [v.legal_name, *v.aliases]
        score = max(fuzz.token_set_ratio(req.beneficiary_name.lower(), n.lower()) for n in names)
        if score < 85:
            await emit(Finding(
                agent=NAME, code="BENEFICIARY_NAME_MISMATCH", severity=Severity.high,
                title="Account holder name is not the vendor's legal name",
                detail=f"Beneficiary '{req.beneficiary_name}' vs vendor '{v.legal_name}' "
                       f"({score:.0f}% similar).",
                evidence=[Evidence(source="request", excerpt=req.beneficiary_name)], data_source=SRC))

    if v and req.beneficiary_country and req.beneficiary_country.upper() != v.country.upper():
        await emit(Finding(
            agent=NAME, code="COUNTRY_MISMATCH", severity=Severity.medium,
            title="Beneficiary country differs from the vendor's country",
            detail=f"Funds would go to {req.beneficiary_country.upper()}; vendor is registered in {v.country}.",
            evidence=[Evidence(source="request", excerpt=req.beneficiary_country)], data_source=SRC))

    if req.iban and not iban_valid(req.iban):
        await emit(Finding(
            agent=NAME, code="IBAN_INVALID", severity=Severity.high,
            title="IBAN fails the ISO 13616 checksum",
            detail="The IBAN's mod-97 check digits are wrong: it was mistyped or fabricated.",
            evidence=[Evidence(source="request", excerpt=req.iban)], data_source="ISO 13616 checksum"))

    if req.swift_bic:
        bic = norm_account(req.swift_bic)
        if not swift_valid(bic):
            await emit(Finding(
                agent=NAME, code="SWIFT_INVALID", severity=Severity.medium,
                title="SWIFT/BIC code is malformed",
                detail=f"'{req.swift_bic}' is not a valid ISO 9362 BIC.",
                evidence=[Evidence(source="request", excerpt=req.swift_bic)], data_source="ISO 9362 format"))
        elif req.beneficiary_country and bic[4:6] != req.beneficiary_country.upper():
            await emit(Finding(
                agent=NAME, code="SWIFT_COUNTRY_MISMATCH", severity=Severity.medium,
                title="Bank's country does not match the stated beneficiary country",
                detail=f"BIC {bic} is a {bic[4:6]} bank; request says {req.beneficiary_country.upper()}.",
                evidence=[Evidence(source="request", excerpt=req.swift_bic)], data_source="ISO 9362 format"))

    if req.claims_bank_details_changed:
        await emit(Finding(
            agent=NAME, code="CLAIMS_NEW_DETAILS", severity=Severity.medium,
            title="Request itself says the bank details have changed",
            detail="Bank-detail changes must be confirmed through the vendor contact on file, "
                   "never through the channel that announced them.",
            evidence=[Evidence(source=c.source, excerpt=c.quote) for c in ctx.request.citations
                      if "bank" in c.quote.lower() or "account" in c.quote.lower()][:2],
            data_source="Payment request"))

    # ---- India: GSTIN (tax ID) and IFSC (bank branch) ------------------------
    if req.gstin:
        g = norm_account(req.gstin)
        if not gstin_valid(g):
            await emit(Finding(
                agent=NAME, code="GSTIN_INVALID", severity=Severity.high,
                title="GST number on the invoice is not valid",
                detail="Its format or check digit is wrong, so the invoice wasn't issued by a GST-registered entity.",
                evidence=[Evidence(source="invoice", excerpt=req.gstin)], data_source="GSTIN check-digit algorithm"))
        elif v and v.gstin and g != norm_account(v.gstin):
            same_pan = g[2:12] == norm_account(v.gstin)[2:12]
            await emit(Finding(
                agent=NAME, code="GSTIN_MISMATCH", severity=Severity.medium if same_pan else Severity.high,
                title="Invoice GST number differs from the vendor on file",
                detail=("Same company (PAN matches), different state registration." if same_pan else
                        f"The invoice was issued by a different legal entity than {v.legal_name}."),
                evidence=[Evidence(source="invoice", excerpt=req.gstin)], data_source=SRC))
    if req.ifsc:
        code = norm_account(req.ifsc)
        if not ifsc_valid(code):
            await emit(Finding(agent=NAME, code="IFSC_INVALID", severity=Severity.high,
                               title="IFSC code is malformed", detail=f"'{req.ifsc}' is not a valid IFSC.",
                               evidence=[Evidence(source="request", excerpt=req.ifsc)], data_source="IFSC format"))
        elif ctx.sources.ifsc is not None:
            try:
                info = await ctx.sources.ifsc.lookup(code)
                if not info.found:
                    await emit(Finding(
                        agent=NAME, code="IFSC_UNKNOWN", severity=Severity.high,
                        title="IFSC code does not exist", detail="No Indian bank branch has this code.",
                        evidence=[Evidence(source=ctx.sources.ifsc.name, excerpt=f"{code}: not found")],
                        data_source=ctx.sources.ifsc.name))
                else:
                    from rapidfuzz import fuzz
                    claimed = req.beneficiary_bank or ""
                    mismatch = claimed and fuzz.token_set_ratio(claimed.lower(), (info.bank or "").lower()) < 70
                    await emit(Finding(
                        agent=NAME, code="IFSC_BANK_MISMATCH" if mismatch else "IFSC_CONFIRMED",
                        severity=Severity.medium if mismatch else Severity.info,
                        title=(f"IFSC belongs to {info.bank}, not {claimed}" if mismatch
                               else f"IFSC is {info.bank}, {info.branch}"),
                        detail=f"Branch: {info.branch}, {info.city}.",
                        evidence=[Evidence(source=ctx.sources.ifsc.name, excerpt=f"{code} → {info.bank}")],
                        data_source=ctx.sources.ifsc.name))
            except SourceUnavailable as e:
                await emit(Finding(agent=NAME, code="IFSC_LOOKUP_UNAVAILABLE", source_unavailable=True, severity=Severity.low,
                                   title="Bank branch lookup unavailable", detail=str(e),
                                   data_source=ctx.sources.ifsc.name))
