"""Registry agent: checks the payee and its bank against official registers.

- SWIFT/BIC -> GLEIF: which legal entity really owns this bank code, in which country, and does
  that match the bank named in the request and the IBAN's country?
- Payee -> GLEIF: is the beneficiary a registered legal entity, and is it still active?
- VAT number -> EU VIES: is the invoicing company's VAT registration valid?
Absence from GLEIF is not suspicious on its own (most small companies have no LEI), so it is
reported for information only; contradictions are what add risk."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import Evidence, Finding, Severity
from ..sources import SourceUnavailable
from .base import AgentContext, Emit, norm_account

NAME = "registry"
TITLE = "Company & bank registers"
BANK_WORDS = {"BANK", "BANKING", "CORPORATION", "LIMITED", "LTD", "PLC", "AG", "SA", "NA", "THE", "CO", "AND", "OF"}


def _core(name: str) -> str:
    return " ".join(w for w in "".join(c if c.isalnum() else " " for c in name.upper()).split() if w not in BANK_WORDS)


async def run(ctx: AgentContext, emit: Emit) -> None:
    req, src = ctx.request, ctx.sources
    gleif, vies = src.gleif, src.vies
    if gleif is None:
        await emit(Finding(agent=NAME, code="REGISTRY_OFF", severity=Severity.info, title="Registry checks are switched off",
                           detail="No registry source is configured on this installation.", data_source="Configuration"))
        return
    ran = False

    # ---- the beneficiary's bank: does the SWIFT code belong to who the request says?
    if bic := norm_account(req.swift_bic):
        ran = True
        try:
            bank = await gleif.by_bic(bic)
        except SourceUnavailable as e:
            await emit(Finding(agent=NAME, code="REGISTRY_UNAVAILABLE", source_unavailable=True, severity=Severity.info, title="GLEIF did not answer",
                               detail=f"{e}. The bank's SWIFT code was not cross-checked.", data_source=gleif.name))
        else:
            bic_cc = bic[4:6]
            if bank:
                await emit(Finding(agent=NAME, code="BIC_OWNER_FOUND", severity=Severity.info,
                                   title=f"SWIFT code {bic} belongs to {bank.legal_name}",
                                   detail=f"LEI {bank.lei}, registered in {bank.country}.",
                                   evidence=[Evidence(source=gleif.name, excerpt=bank.url)], data_source=gleif.name,
                                   meta={"lei": bank.lei, "url": bank.url}))
                named = req.beneficiary_bank or ""
                if named and fuzz.token_set_ratio(_core(named), _core(bank.legal_name)) < 60:
                    await emit(Finding(agent=NAME, code="BANK_NAME_MISMATCH", severity=Severity.medium,
                                       title="Bank name does not match its SWIFT code",
                                       detail=f"The request names '{named}', but {bic} is registered to {bank.legal_name}.",
                                       evidence=[Evidence(source="request", excerpt=named),
                                                 Evidence(source=gleif.name, excerpt=bank.legal_name)],
                                       data_source=gleif.name))
            iban = norm_account(req.iban)
            if iban and iban[:2] != bic_cc:
                await emit(Finding(agent=NAME, code="IBAN_BIC_COUNTRY_MISMATCH", severity=Severity.medium,
                                   title="IBAN and SWIFT code point to different countries",
                                   detail=f"The IBAN is from {iban[:2]} but the SWIFT code {bic} is a {bic_cc} bank. "
                                          "Account details assembled from two different banks are a common fraud pattern.",
                                   evidence=[Evidence(source="request", excerpt=req.iban or iban)], data_source="ISO 13616 / ISO 9362"))

    # ---- the payee: a registered, active legal entity?
    payee = req.beneficiary_name or req.vendor_name
    if payee:
        ran = True
        try:
            ent = await gleif.by_name(payee)
        except SourceUnavailable as e:
            await emit(Finding(agent=NAME, code="REGISTRY_UNAVAILABLE", source_unavailable=True, severity=Severity.info, title="GLEIF did not answer",
                               detail=f"{e}. The payee was not looked up.", data_source=gleif.name))
        else:
            if ent is None:
                await emit(Finding(agent=NAME, code="PAYEE_NO_LEI", severity=Severity.info,
                                   title="Payee has no LEI record",
                                   detail=f"No legal entity close to '{payee}' is in GLEIF. Common for smaller companies; "
                                          "not a risk signal by itself.", data_source=gleif.name))
            elif ent.entity_status and ent.entity_status != "ACTIVE":
                await emit(Finding(agent=NAME, code="PAYEE_ENTITY_INACTIVE", severity=Severity.high,
                                   title=f"Payee is registered as {ent.entity_status.lower()}",
                                   detail=f"{ent.legal_name} (LEI {ent.lei}) is not an active legal entity. "
                                          "Payments to dissolved or merged companies are a known fraud route.",
                                   evidence=[Evidence(source=gleif.name, excerpt=ent.url)], data_source=gleif.name))
            else:
                await emit(Finding(agent=NAME, code="PAYEE_LEI_ACTIVE", severity=Severity.info,
                                   title=f"Payee is a registered legal entity: {ent.legal_name}",
                                   detail=f"LEI {ent.lei}, {ent.country}, registered since {ent.registered_on}.",
                                   evidence=[Evidence(source=gleif.name, excerpt=ent.url)], data_source=gleif.name,
                                   meta={"lei": ent.lei, "url": ent.url}))
                if req.beneficiary_country and ent.country and ent.country != req.beneficiary_country.upper():
                    await emit(Finding(agent=NAME, code="PAYEE_COUNTRY_MISMATCH", severity=Severity.low,
                                       title="Payee's registered country differs from the account's country",
                                       detail=f"{ent.legal_name} is registered in {ent.country}; the account is in "
                                              f"{req.beneficiary_country}.", data_source=gleif.name))

    # ---- EU VAT number
    vat = req.vat_number
    if vat and vies and vies.split(vat):
        ran = True
        try:
            chk = await vies.check(vat)
        except SourceUnavailable as e:
            await emit(Finding(agent=NAME, code="REGISTRY_UNAVAILABLE", source_unavailable=True, severity=Severity.info, title="VIES did not answer",
                               detail=f"{e}. The VAT number was not verified.", data_source=vies.name))
        else:
            if chk.valid:
                await emit(Finding(agent=NAME, code="VAT_VALID", severity=Severity.info, title=f"VAT number {vat} is valid",
                                   detail="Confirmed by the EU VIES register" + (f", registered to {chk.name}." if chk.name else "."),
                                   data_source=vies.name))
            else:
                await emit(Finding(agent=NAME, code="VAT_INVALID", severity=Severity.high, title=f"VAT number {vat} is not valid",
                                   detail="The EU VIES register does not recognise this VAT registration. Invoices with "
                                          "invalid VAT numbers are a hallmark of fake suppliers.",
                                   evidence=[Evidence(source="request", excerpt=vat)], data_source=vies.name))
    if not ran:
        await emit(Finding(agent=NAME, code="REGISTRY_NOTHING", severity=Severity.info, title="Nothing to look up",
                           detail="The request has no SWIFT code, payee name or VAT number.", data_source=gleif.name))
