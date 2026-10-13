"""Audit case file: PDF (reportlab) with an HTML fallback."""
from __future__ import annotations

import html
import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from .models import Case, Decision, Event, Severity

INK = colors.HexColor("#16202A")
MUTED = colors.HexColor("#5B6770")
RULE = colors.HexColor("#CBD3D6")
DECISION_COLOR = {Decision.hold: "#B4231F", Decision.callback: "#A55A00", Decision.release: "#1E6B47"}
SEV_COLOR = {Severity.critical: "#B4231F", Severity.high: "#C2410C", Severity.medium: "#A55A00",
             Severity.low: "#5B6770", Severity.info: "#1E6B47"}


def _esc(t) -> str:
    return html.escape(str(t if t is not None else "—"))


def build_pdf(case: Case, events: list[Event]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Case {case.id}", author="Verify-Before-Wire")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=9, leading=12,
                          textColor=INK, alignment=TA_LEFT)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, leading=10, textColor=MUTED)
    h1 = ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=16, leading=20)
    h2 = ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=11, leading=14,
                        spaceBefore=10, spaceAfter=4)
    v, r = case.verdict, case.request
    story = [Paragraph("Payment investigation case file", h1),
             Paragraph(f"Case {case.id} · generated {datetime.now(timezone.utc):%d %b %Y %H:%M} UTC", small),
             Spacer(1, 6)]
    if v:
        c = DECISION_COLOR[v.decision]
        big = ParagraphStyle("v", parent=body, fontSize=20, leading=26)
        story += [Paragraph(f'<font color="{c}"><b>{v.decision.value}</b></font>'
                            f'&nbsp;&nbsp;<font size="11">risk {v.risk_score}/100</font>', big),
                  Spacer(1, 4), Paragraph(_esc(v.summary), body)]
    if r:
        rows = [["Requested by", f"{_esc(r.requester_name)} ({_esc(r.requester_claimed_role)}) {_esc(r.requester_email)}"],
                ["Amount", f"{_esc(r.currency)} {r.amount:,.2f}" if r.amount else "—"],
                ["Beneficiary", _esc(r.beneficiary_name)],
                ["Bank", f"{_esc(r.beneficiary_bank)} · SWIFT {_esc(r.swift_bic)}"],
                ["Account", _esc(r.iban or r.account_number)],
                ["Invoice", _esc(r.invoice_number)], ["Deadline", _esc(r.deadline)]]
        story += [Paragraph("Request", h2), _kv(rows, body)]
    flagged = sorted([f for f in case.findings if f.severity != Severity.info], key=lambda f: -f.points)
    clear = [f for f in case.findings if f.severity == Severity.info]
    story.append(Paragraph(f"Red flags ({len(flagged)})", h2))
    for f in flagged:
        ev = "<br/>".join(f'<i>{_esc(e.source)}:</i> “{_esc(e.excerpt)}”' for e in f.evidence)
        story.append(KeepTogether([Paragraph(
            f'<font color="{SEV_COLOR[f.severity]}"><b>{f.severity.value.upper()}</b></font> '
            f'<b>{_esc(f.title)}</b><br/>{_esc(f.detail)}'
            + (f"<br/>{ev}" if ev else "") +
            f'<br/><font color="#5B6770" size="7">Source: {_esc(f.data_source)} · agent: {f.agent}</font>',
            body), Spacer(1, 5)]))
    if clear:
        story += [Paragraph("Checks passed", h2),
                  Paragraph("<br/>".join(f"✓ {_esc(f.title)} — <font color='#5B6770'>{_esc(f.data_source)}</font>"
                                         for f in clear), body)]
    if v and v.decision != Decision.release:
        story += [Paragraph("Callback script", h2)]
        if v.callback_number:
            story.append(Paragraph(f"Call <b>{_esc(v.callback_contact)}</b> on <b>{_esc(v.callback_number)}</b> "
                                   "(vendor master — not from the request).", body))
        story.append(Paragraph("<br/>".join(f"{i}. {_esc(l)}" for i, l in enumerate(v.callback_script, 1)), body))
    if v:
        story += [Paragraph("Recommended actions", h2),
                  Paragraph("<br/>".join(f"• {_esc(a)}" for a in v.recommended_actions), body)]
    if case.record:
        rec = case.record
        fired = "<br/>".join(f"• {_esc(r['description'])}" for r in case.verdict.fired_rules) if v else ""
        story += [Paragraph("Decision basis", h2),
                  _kv([["Policy", f"version {rec.policy_version} · {rec.policy_hash[:16]}…"],
                       ["Rules applied", fired or "none beyond the score thresholds"],
                       ["Requirements", _esc(", ".join(v.requirements) if v and v.requirements else "none")],
                       ["Model", f"{_esc(rec.model)} · prompts {_esc(rec.prompt_version)}"],
                       ["Data region", _esc(rec.region)]], body)]
    wf = case.workflow
    if wf.callbacks or wf.approvals:
        rows = [[f"{cb.at:%d %b %H:%M}", f"Callback by {_esc(cb.email)} to {_esc(cb.contact)} on {_esc(cb.number)} "
                 f"({cb.number_source.replace('_', ' ')}): <b>{cb.outcome}</b> {_esc(cb.notes)}"] for cb in wf.callbacks]
        rows += [[f"{ap.at:%d %b %H:%M}", f"<b>{ap.action}</b> by {_esc(ap.email)} {_esc(ap.reason)}"] for ap in wf.approvals]
        story += [Paragraph(f"Human decisions — state: {wf.state}", h2), _kv(rows, body)]
    story += [Paragraph("Inputs (SHA-256)", h2),
              _kv([[d.label, f"{_esc(d.filename)}<br/><font size='6.5'>{d.sha256}</font>"] for d in case.sources], body),
              Paragraph("Audit trail", h2),
              Paragraph("<br/>".join(f"{e.ts:%H:%M:%S.%f}"[:-3] + f" #{e.seq} {e.type}"
                                     + (f" [{e.agent}]" if e.agent else "")
                                     + (f" — {_esc(e.data.get('title', ''))}" if e.type == 'finding' else "")
                                     for e in events), small)]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


def _kv(rows, style):
    t = Table([[Paragraph(f"<b>{k}</b>", style), Paragraph(v, style)] for k, v in rows],
              colWidths=[32 * mm, None])
    t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    return t


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, "Verify-Before-Wire · decision support; final approval rests with the payment approver")
    canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"page {doc.page}")
    canvas.restoreState()


def build_html(case: Case) -> str:
    v = case.verdict
    items = "".join(f"<li><b>[{f.severity.value}] {_esc(f.title)}</b> — {_esc(f.detail)} "
                    f"<small>({_esc(f.data_source)})</small></li>" for f in case.findings)
    head = f"<h1>{v.decision.value} · risk {v.risk_score}/100</h1><p>{_esc(v.summary)}</p>" if v else "<h1>In progress</h1>"
    return (f"<!doctype html><meta charset=utf-8><title>Case {case.id}</title>"
            f"<body style='font:14px system-ui;max-width:760px;margin:2rem auto'>{head}<ul>{items}</ul></body>")
