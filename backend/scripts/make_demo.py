"""Generates the demo data: vendor master + payment history (seed_data.json), the three
scenario inputs (emails with PDF invoices, voice-note transcript) and scenarios.json.

    python scripts/make_demo.py

Everything here is synthetic. To make the domain checks more striking, set
FRAUD_DOMAIN to a lookalike domain you actually registered, and VENDOR_DOMAIN to a real
domain you control (see the plan)."""
from __future__ import annotations

import io
import json
import os
import random
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "demo" / "files"
FILES.mkdir(parents=True, exist_ok=True)

COMPANY = os.getenv("COMPANY_DOMAIN", "acme-holdings.com")
FRAUD_DOMAIN = os.getenv("FRAUD_DOMAIN", "acme-hold1ngs.com")
VENDOR_DOMAIN = os.getenv("VENDOR_DOMAIN", "lioncitylogistics.com.sg")
VENDOR2_DOMAIN = os.getenv("VENDOR2_DOMAIN", "pacrim-components.co.jp")

VENDORS = [
    {
        "id": "v-lcl", "legal_name": "Lion City Logistics Pte Ltd",
        "aliases": ["Lion City Logistics", "LCL"], "country": "SG", "domains": [VENDOR_DOMAIN],
        "bank_accounts": [{"bank_name": "DBS Bank Ltd", "account_number": "0721234567",
                           "swift_bic": "DBSSSGSG", "country": "SG", "verified_on": "2024-03-14"}],
        "callback_contact": "Tan Mei Ling, Finance Manager", "callback_phone": "+65 6123 4567",
        "default_currency": "SGD", "payment_terms_days": 30,
    },
    {
        "id": "v-prc", "legal_name": "Pacific Rim Components Co., Ltd.",
        "aliases": ["Pacific Rim Components", "PRC"], "country": "JP", "domains": [VENDOR2_DOMAIN],
        "bank_accounts": [{"bank_name": "MUFG Bank, Ltd.", "account_number": "1234567",
                           "swift_bic": "BOTKJPJT", "country": "JP", "verified_on": "2023-11-02"}],
        "callback_contact": "Kenji Watanabe, Accounts Receivable", "callback_phone": "+81 3 5555 0142",
        "default_currency": "USD", "payment_terms_days": 45,
    },
    {
        "id": "v-hos", "legal_name": "Harbourline Office Supplies Pte Ltd",
        "aliases": ["Harbourline"], "country": "SG", "domains": ["harbourline.sg"],
        "bank_accounts": [{"bank_name": "OCBC Bank", "account_number": "5012345001",
                           "swift_bic": "OCBCSGSG", "country": "SG", "verified_on": "2025-01-20"}],
        "callback_contact": "Ravi Kumar, Accounts", "callback_phone": "+65 6789 0011",
        "default_currency": "SGD", "payment_terms_days": 30,
    },
]


def history():
    rnd = random.Random(42)
    out, today = [], date.today()
    plans = [("v-lcl", "LCL-INV-2025-", "SGD", 60000, 95000, 30, "0721234567", 18),
             ("v-prc", "PRC-", "USD", 38000, 72000, 45, "1234567", 12),
             ("v-hos", "HOS-", "SGD", 800, 4200, 30, "5012345001", 14)]
    for vid, prefix, ccy, lo, hi, gap, acct, n in plans:
        d = today - timedelta(days=gap * n + 10)
        for i in range(n):
            d += timedelta(days=gap + rnd.randint(-4, 4))
            out.append({"id": f"p-{vid}-{i:03d}", "vendor_id": vid,
                        "invoice_number": f"{prefix}{1000 + i * 7}",
                        "amount": round(rnd.uniform(lo, hi), -2), "currency": ccy,
                        "paid_on": d.isoformat(), "account_ref": acct})
    return out


def invoice_pdf(path_name, vendor, inv_no, amount_str, bank_lines, notes):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 18); c.drawString(20 * mm, h - 25 * mm, vendor)
    c.setFont("Helvetica", 10); c.drawString(20 * mm, h - 32 * mm, "TAX INVOICE")
    c.drawString(130 * mm, h - 25 * mm, f"Invoice no: {inv_no}")
    c.drawString(130 * mm, h - 31 * mm, f"Date: {date.today():%d %b %Y}")
    c.drawString(20 * mm, h - 48 * mm, "Bill to: Acme Holdings Pte Ltd, 1 Raffles Place, Singapore")
    c.line(20 * mm, h - 55 * mm, w - 20 * mm, h - 55 * mm)
    c.drawString(20 * mm, h - 63 * mm, "Freight forwarding and customs brokerage services")
    c.drawRightString(w - 20 * mm, h - 63 * mm, amount_str)
    c.line(20 * mm, h - 70 * mm, w - 20 * mm, h - 70 * mm)
    c.setFont("Helvetica-Bold", 11); c.drawString(20 * mm, h - 78 * mm, f"Total due: {amount_str}")
    c.setFont("Helvetica", 10); y = h - 95 * mm
    c.drawString(20 * mm, y, "Remit to:"); y -= 6 * mm
    for line in bank_lines:
        c.drawString(25 * mm, y, line); y -= 5.5 * mm
    y -= 4 * mm
    for line in notes:
        c.drawString(20 * mm, y, line); y -= 5.5 * mm
    c.showPage(); c.save()
    return buf.getvalue()


def eml(name, frm, to, subject, body, headers, attachment=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    m["Date"] = format_datetime(datetime.now(timezone.utc))
    for k, v in headers.items():
        m[k] = v
    m.set_content(body)
    if attachment:
        m.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    (FILES / name).write_bytes(bytes(m))


def main():
    (ROOT / "backend" / "scripts" / "seed_data.json").write_text(
        json.dumps({"vendors": VENDORS, "payments": history()}, indent=1))

    # 1. CEO / CFO deepfake fraud -----------------------------------------
    pdf = invoice_pdf("LCL-INV-2026-1047.pdf", "Lion City Logistic Services Ltd", "LCL-INV-2026-1047",
                      "SGD 480,000.00",
                      ["Beneficiary: Lion City Logistic Services Ltd", "Bank: HSBC Hong Kong",
                       "Account: 848-123456-838", "SWIFT: HSBCHKHH"],
                      ["Please note our new banking details effective immediately."])
    eml("ceo_fraud.eml", f'"Daniel Koh" <daniel.koh@{FRAUD_DOMAIN}>', f"ap-team@{COMPANY}",
        "URGENT - confidential payment before 5pm",
        "Hi team,\n\nI need you to process a payment to Lion City Logistics today. It's the deposit "
        "for the Jurong warehouse deal and it is strictly confidential until the announcement, so "
        "please keep this between us and don't mention it to anyone else.\n\n"
        "Amount: SGD 480,000\nBeneficiary: Lion City Logistic Services Ltd\nBank: HSBC Hong Kong\n"
        "Account: 848-123456-838\nSWIFT: HSBCHKHH\n\nThey have changed banks, the invoice is attached. "
        "The wire must go out before 5pm today or we lose the deal.\n\nI'm in board meetings all day "
        "so I can't take calls. Reply to this email only once it's done.\n\nDaniel Koh\nCFO, Acme Holdings",
        {"Reply-To": "dkoh.cfo.office@gmail.com", "Return-Path": f"<bounce@{FRAUD_DOMAIN}>",
         "Authentication-Results": f"mx.{COMPANY}; spf=fail smtp.mailfrom={FRAUD_DOMAIN}; "
                                   f"dkim=none; dmarc=fail (p=NONE) header.from={FRAUD_DOMAIN}"},
        ("LCL-INV-2026-1047.pdf", pdf))
    (FILES / "ceo_voice_note.transcript.txt").write_text(
        "Hi, it's Daniel. Just left you an email. I need the Lion City payment out today, four hundred "
        "and fifty thousand Singapore dollars, before five o'clock. This is very sensitive, please keep "
        "this between us until the announcement. I'm stuck in back-to-back board meetings so don't try "
        "calling me, just reply to the email once it's sent. Thanks, I really appreciate you handling this.\n")

    # 2. Legitimate invoice -----------------------------------------------
    pdf = invoice_pdf("LCL-INV-2026-0988.pdf", "Lion City Logistics Pte Ltd", "LCL-INV-2026-0988",
                      "SGD 78,400.00",
                      ["Beneficiary: Lion City Logistics Pte Ltd", "Bank: DBS Bank Ltd",
                       "Account: 0721234567", "SWIFT: DBSSSGSG"],
                      ["Payment terms: 30 days from invoice date."])
    eml("legit_invoice.eml", f'"LCL Billing" <billing@{VENDOR_DOMAIN}>', f"ap-team@{COMPANY}",
        "Invoice LCL-INV-2026-0988 - September freight",
        "Dear Accounts Payable,\n\nPlease find attached invoice LCL-INV-2026-0988 for September freight "
        "forwarding services, SGD 78,400.00, payable to our DBS account on file within 30 days.\n\n"
        "Thank you for your business.\n\nTan Mei Ling\nFinance Manager, Lion City Logistics Pte Ltd",
        {"Authentication-Results": f"mx.{COMPANY}; spf=pass smtp.mailfrom={VENDOR_DOMAIN}; "
                                   f"dkim=pass header.d={VENDOR_DOMAIN}; dmarc=pass header.from={VENDOR_DOMAIN}"},
        ("LCL-INV-2026-0988.pdf", pdf))

    # 3. Vendor email compromise (real domain, changed account) ----------
    pdf = invoice_pdf("PRC-24117.pdf", "Pacific Rim Components Co., Ltd.", "PRC-24117", "USD 61,250.00",
                      ["Beneficiary: Pacific Rim Components Co., Ltd.", "Bank: Bangkok Bank PCL",
                       "Account: 101-4-55821-9", "SWIFT: BKKBTHBK"], ["Please use our new account."])
    eml("vendor_bank_change.eml", f'"Kenji Watanabe" <accounts@{VENDOR2_DOMAIN}>', f"ap-team@{COMPANY}",
        "Updated remittance details - PRC-24117",
        "Hello,\n\nDue to an internal restructuring we have moved our banking to a new provider. "
        "Please use the details on the attached invoice PRC-24117 (USD 61,250.00) and for all future "
        "payments. Our MUFG account will be closed at the end of the month.\n\nBest regards,\n"
        "Kenji Watanabe\nAccounts Receivable, Pacific Rim Components",
        {"Authentication-Results": f"mx.{COMPANY}; spf=pass smtp.mailfrom={VENDOR2_DOMAIN}; "
                                   f"dkim=pass header.d={VENDOR2_DOMAIN}; dmarc=pass header.from={VENDOR2_DOMAIN}"},
        ("PRC-24117.pdf", pdf))

    scenarios = [
        {"id": "ceo-fraud", "title": "CFO voice note + email, SGD 480k",
         "description": "Deepfake-style CFO request to a new Hong Kong account before 5pm.",
         "files": ["ceo_fraud.eml", "ceo_voice_note.wav", "ceo_voice_note.transcript.txt"],
         "expected": "HOLD"},
        {"id": "legit-invoice", "title": "Routine Lion City invoice, SGD 78.4k",
         "description": "Known vendor, account on file, normal amount.",
         "files": ["legit_invoice.eml"], "expected": "RELEASE"},
        {"id": "vendor-bank-change", "title": "Real vendor, new bank account, USD 61k",
         "description": "Authenticated email from a genuine vendor domain asking to pay a new account.",
         "files": ["vendor_bank_change.eml"], "expected": "HOLD"},
        {"id": "ceo-video", "title": "CFO \"video message\", SGD 480k",
         "description": "A still photo with a synthetic voice-over, forwarded on chat. Voice & video forensics.",
         "files": ["ceo_video_message.mp4", "ceo_video_message.transcript.txt"], "expected": "HOLD"},
    ]
    video_message()
    (ROOT / "demo" / "scenarios.json").write_text(json.dumps(scenarios, indent=1))
    print("demo files written to", FILES)


def video_message() -> None:
    """The cheap deepfake: a still 'portrait' card with the synthetic CFO voice over it (needs ffmpeg;
    the committed file is kept when ffmpeg is missing)."""
    import shutil
    import subprocess  # nosec B404
    (FILES / "ceo_video_message.transcript.txt").write_text((FILES / "ceo_voice_note.transcript.txt").read_text())
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found: keeping the existing ceo_video_message.mp4")
        return
    boxes = ("drawbox=x=250:y=60:w=140:h=140:color=0xc9b8ff:t=fill,drawbox=x=220:y=210:w=200:h=150:color=0x533afd:t=fill,"
             "drawbox=x=0:y=318:w=640:h=42:color=0x0a2540:t=fill")
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x1c1e54:s=640x360:d=25",  # nosec B603
                    "-i", str(FILES / "ceo_voice_note.wav"), "-vf", boxes, "-c:v", "libx264", "-tune", "stillimage",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest", "-fflags", "+bitexact",
                    "-map_metadata", "-1", str(FILES / "ceo_video_message.mp4")], check=True)


if __name__ == "__main__":
    main()
