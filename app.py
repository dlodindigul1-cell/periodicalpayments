"""
பருவ இதழ்கள் தொகை செலுத்துதல் - 2026-27
Flask backend — Neon Postgres-ஐ பயன்படுத்தி GAS system-ஐ replace செய்யும்
Phase 1: Master data + Payment entry + Duplicate check + Quarter view +
         Payment processing + Transaction number tracking
(PDF உருவாக்கம் / Email அனுப்புதல் / Reports — Phase 2-ல் சேர்க்கப்படும்)
"""

import base64
import csv
import io
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import date, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response, send_file
from xhtml2pdf import pisa

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Dlodgl@789")


# --------------------------------------------------------------------------- #
# Basic Auth — முழு தளமும் Username/Password-க்குப் பின்னால்
# --------------------------------------------------------------------------- #
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def authenticate():
    return Response(
        "இந்தத் தளத்தை பயன்படுத்த Username / Password தேவை.",
        401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)

    return decorated


@app.before_request
def global_auth():
    # Render health checks hit this path without credentials — no login here.
    if request.path == "/healthz":
        return
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()


@app.route("/healthz")
def healthz():
    """Render health-check endpoint. Also confirms the DB connection is alive."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
        return jsonify({"status": "ok"}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "message": str(e)}), 503


# --------------------------------------------------------------------------- #
# DB helper
# --------------------------------------------------------------------------- #
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def q_num(v, default=0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def q_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def classify_vendor_code(code):
    """Vendor Code இரண்டு வகைகளில் ஒன்று: எண்கள் மட்டும் (BENEFICIARY) அல்லது
    எண்கள்-V (BUSINESS VENDOR, எ.கா 15317-V). பொருந்தாவிட்டால் None."""
    code = (code or "").strip().upper()
    if not code:
        return None
    if re.fullmatch(r"\d+", code):
        return "BENEFICIARY"
    if re.fullmatch(r"\d+-V", code):
        return "BUSINESS VENDOR"
    return "OTHER"


def norm_code(code):
    """Vendor/Beneficiary Code ஒப்பீட்டுக்கு: இடைவெளி நீக்கி, பெரிய எழுத்தாக்கும்."""
    return (code or "").strip().upper()


def voucher_sort_key(v):
    """Voucher No எண் மதிப்பின்படி வரிசைப்படுத்த (2, 10, 11 — '10' < '2' என்ற எழுத்து வரிசை அல்ல).
    Voucher No இல்லாதவை கடைசியில்."""
    v = (v or "").strip()
    m = re.match(r"^(\d+)", v)
    return (0, int(m.group(1)), v) if m else (1, 0, v)


def fetch_group_blockers(cur, quarter=None):
    """(quarter, CODE) -> {'pending': [...], 'missingTxn': [...]}
    pending    = அதே Code + அதே Quarter-ல் Invoice பதிவாகி, இன்னும் தொகை வழங்காத இதழ்கள்
    missingTxn = தொகை வழங்கியும் Bank Transaction No பதிவாகாத இதழ்கள்"""
    sql = (
        "SELECT p.magazine, p.quarter, p.payment_date, p.bill_set_no, m.tnpfts_code "
        "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
        "WHERE (p.payment_date IS NULL OR p.transaction_no IS NULL OR p.transaction_no = '')"
    )
    params = ()
    if quarter:
        sql += " AND p.quarter=%s"
        params = (quarter,)
    cur.execute(sql, params)
    out = {}
    for r in cur.fetchall():
        code = norm_code(r["tnpfts_code"])
        if not code:
            continue
        d = out.setdefault((r["quarter"], code), {"pending": [], "missingTxn": [], "missingTxnBySet": {}})
        if r["payment_date"] is None:
            d["pending"].append(r["magazine"])
        else:
            d["missingTxn"].append(r["magazine"])
            s = (r["bill_set_no"] or "").strip()
            d["missingTxnBySet"].setdefault(s, []).append(r["magazine"])
    for d in out.values():
        d["pending"].sort()
        d["missingTxn"].sort()
        for lst in d["missingTxnBySet"].values():
            lst.sort()
    return out


def group_key_for(r):
    """குழு விதி: ஒரே Quarter + ஒரே Vendor/Beneficiary Code + ஒரே Bill Set No உள்ள இதழ்கள் மட்டுமே ஒரு குழு.
    Code அல்லது Set No இல்லாத பதிவு தனியாகவே இருக்கும்.  r-ல்: id, quarter, tnpfts_code, bill_set_no."""
    code = norm_code(r["tnpfts_code"])
    s = (r["bill_set_no"] or "").strip()
    if code and s:
        return (r["quarter"], code, s)
    return (r["quarter"], "#%d" % r["id"], "")


def vendor_display_name(vendor_name, payee_name, fallback):
    return (vendor_name or "").strip() or (payee_name or "").strip() or fallback


def normalize_csv_header(h):
    """CSV header ஒப்பீடு நம்பகமாக இருக்க — BOM, non-breaking space, தேவையற்ற
    இடைவெளிகள், எழுத்து அளவு வேறுபாடு, முடிவில் உள்ள '.' ஆகியவற்றை நீக்கி normalize செய்யும்
    (எ.கா: Excel-ல் " EMAIL ID." எனும் column-ஐயும் "EMAIL ID" ஆக அடையாளம் காணும்)."""
    h = (h or "").replace("\ufeff", "").replace("\xa0", " ")
    h = " ".join(h.split()).upper()
    return h.rstrip(".")


# --------------------------------------------------------------------------- #
# Mail — இரண்டு வழிகள் ஆதரிக்கப்படுகின்றன:
#
#  1) SMTP (Gmail App Password) — MAIL_PROVIDER=smtp (default)
#       SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD, MAIL_FROM
#     பல hosting platforms (Vercel, சில free-tier hosts) SMTP port (587/465)-ஐ
#     block செய்துவிடும் — அப்போது "[Errno 101] Network is unreachable" பிழை வரும்.
#
#  2) Brevo HTTP API (port 443 வழியாக மட்டுமே, எந்த hosting platform-லும் வேலை
#     செய்யும்) — MAIL_PROVIDER=brevo
#       BREVO_API_KEY, MAIL_FROM (Brevo-ல் verify செய்த sender email), MAIL_FROM_NAME
#     பதிவு: https://app.brevo.com (இலவசமாக ஒரு நாளைக்கு 300 மெயில்கள் அனுப்பலாம்).
# --------------------------------------------------------------------------- #
def mail_provider():
    return os.environ.get("MAIL_PROVIDER", "smtp").strip().lower()


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"))


def send_email(to_addr, subject, html_body, attachment_bytes=None, attachment_name=None):
    """MAIL_PROVIDER env var-ஐ பொருத்து SMTP அல்லது Brevo HTTP API வழியாக மெயில் அனுப்பும்."""
    provider = mail_provider()
    if provider == "brevo":
        _send_email_brevo(to_addr, subject, html_body, attachment_bytes, attachment_name)
    else:
        _send_email_smtp(to_addr, subject, html_body, attachment_bytes, attachment_name)


def _send_email_smtp(to_addr, subject, html_body, attachment_bytes=None, attachment_name=None):
    if not smtp_configured():
        raise RuntimeError(
            "SMTP settings configure செய்யப்படவில்லை. Server-ல் SMTP_HOST, SMTP_USER, SMTP_PASSWORD "
            "environment variables அமைக்கவும் (எ.கா. Gmail App Password)."
        )
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    mail_from = os.environ.get("MAIL_FROM", user)

    msg = MIMEMultipart()
    msg["From"] = mail_from
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if attachment_bytes:
        part = MIMEApplication(attachment_bytes, _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=attachment_name or "attachment.pdf")
        msg.attach(part)

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(mail_from, [to_addr], msg.as_string())
    except OSError as e:
        raise RuntimeError(
            f"SMTP-ல் இணைக்க முடியவில்லை ({e}). இந்த hosting platform SMTP port ({port})-ஐ "
            "block செய்திருக்கலாம் — MAIL_PROVIDER=brevo பயன்படுத்தி பாருங்கள் (BREVO_API_KEY தேவை)."
        ) from e


def _send_email_brevo(to_addr, subject, html_body, attachment_bytes=None, attachment_name=None):
    api_key = os.environ.get("BREVO_API_KEY")
    if not api_key:
        raise RuntimeError("BREVO_API_KEY environment variable அமைக்கப்படவில்லை.")
    mail_from = os.environ.get("MAIL_FROM")
    if not mail_from:
        raise RuntimeError("MAIL_FROM environment variable அமைக்கப்படவில்லை (Brevo-ல் verify செய்த sender email).")
    from_name = os.environ.get("MAIL_FROM_NAME", "District Library Office")

    payload = {
        "sender": {"email": mail_from, "name": from_name},
        "to": [{"email": to_addr}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if attachment_bytes:
        payload["attachment"] = [
            {
                "content": base64.b64encode(attachment_bytes).decode("ascii"),
                "name": attachment_name or "attachment.pdf",
            }
        ]

    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise RuntimeError(f"Brevo API பிழை ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Brevo API-ஐ அடைய முடியவில்லை: {e}") from e


def html_to_pdf_bytes(html_str):
    """HTML string-ஐ PDF bytes ஆக மாற்றும் (xhtml2pdf). ASCII/English content-க்கு
    ஏற்றது — Tamil எழுத்துகளுக்கு தனி font embed தேவை."""
    buf = io.BytesIO()
    result = pisa.CreatePDF(src=html_str, dest=buf, encoding="utf-8")
    if result.err:
        raise RuntimeError("PDF உருவாக்கத்தில் பிழை")
    return buf.getvalue()


_QUARTER_PERIOD_TEXT = {
    "2025-2026-Q4": "JAN-26 TO MARCH-26",
    "2026-2027-Q1": "APR-26 TO JUNE-26",
    "2026-2027-Q2": "JUL-26 TO SEP-26",
    "2026-2027-Q3": "OCT-26 TO DEC-26",
    "2026-2027-Q4": "JAN-27 TO MARCH-27",
}


def quarter_period_text(quarter):
    return _QUARTER_PERIOD_TEXT.get((quarter or "").strip(), "N/A")


def fmt_date(d):
    return d.strftime("%d/%m/%Y") if d else ""


def _money(v):
    return indian_grouping(v or 0)


def build_group_intimation_html(items):
    """ஒரே Vendor/Beneficiary Code உள்ள ஒன்று அல்லது பல இதழ்களுக்கான ஒரே Payment Intimation letter.
    items: fetch_payment_for_mail() dict-களின் பட்டியல் (ஒரு இதழ் என்றாலும் பட்டியலாகவே)."""
    import html as _h

    def esc(v):
        return _h.escape(str(v if v is not None else ""))

    def distinct(vals):
        seen, out = set(), []
        for v in vals:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    bank = next((i["bank"] for i in items if (i.get("bank") or {}).get("accNo")), items[0].get("bank") or {})
    quarter = items[0].get("quarter") or "---"
    multi = len(items) > 1
    total_bill = sum(i.get("billAmount") or 0 for i in items)
    total_ded = sum(i.get("deduction") or 0 for i in items)
    total_net = sum(i.get("netAmount") or 0 for i in items)
    txns = distinct([i.get("transactionNo") for i in items])
    dates = distinct([i.get("paymentDate") for i in items])
    remarks = distinct([i.get("remarks") for i in items])
    mag_names = [i["magazine"] for i in items]

    rows_html = ""
    for n, i in enumerate(items, 1):
        rows_html += (
            f'<tr><td class="ctr">{n}</td><td>{esc(i["magazine"])}</td>'
            f'<td class="ctr">{esc(i.get("invoiceNo") or "---")}</td>'
            f'<td class="ctr">{esc(i.get("invoiceDate") or "---")}</td>'
            f'<td class="right">{_money(i.get("billAmount"))}</td>'
            f'<td class="right">{_money(i.get("deduction"))}</td>'
            f'<td class="right">{_money(i.get("netAmount"))}</td></tr>'
        )
    if multi:
        rows_html += (
            f'<tr class="tot"><td colspan="4" class="right"><strong>TOTAL</strong></td>'
            f'<td class="right"><strong>{_money(total_bill)}</strong></td>'
            f'<td class="right"><strong>{_money(total_ded)}</strong></td>'
            f'<td class="right"><strong>{_money(total_net)}</strong></td></tr>'
        )

    if multi:
        ref_html = (
            "<p>Ref: Your Invoices listed in the table below, for the supply of the magazines "
            f"<strong>{esc(', '.join(mag_names))}</strong></p>"
        )
        para_html = (
            "<p>Sir,<br>Kindly see below the details of the <strong>single consolidated payment of "
            f"Rs.{_money(total_net)}</strong> transferred to your Bank Account from "
            "<strong>The District Library Officer, Dindigul</strong>, for the supply of the above magazines, "
            "with the break-up shown against each magazine. "
            "We kindly request you to acknowledge receipt of the same.</p>"
        )
    else:
        i0 = items[0]
        ref_html = (
            f'<p>Ref: Your Invoice Number <strong>{esc(i0.get("invoiceNo") or "---")}</strong> dated '
            f'<strong>{esc(i0.get("invoiceDate") or "---")}</strong> for the supply of Magazine '
            f'<strong>{esc(i0["magazine"])}</strong></p>'
        )
        para_html = (
            "<p>Sir,<br>Kindly see below the details of the payment transferred to your Bank Account from "
            "<strong>The District Library Officer, Dindigul</strong>, for the supply of the magazine "
            f'<strong>{esc(i0["magazine"])}</strong>, as per the invoice under reference cited. '
            "We kindly request you to acknowledge receipt of the same.</p>"
        )

    remarks_txt = f"{esc(quarter)} SETTLED" + (" | " + esc(" | ".join(remarks)) if remarks else "")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 25mm 18mm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; color:#111; font-size:12px; }}
  h2 {{ text-align:center; border-bottom:3px solid #000; padding-bottom:8px; font-size:20px; margin-bottom:4px; }}
  h3 {{ text-align:center; font-size:14px; margin-top:6px; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; margin:16px 0; }}
  th,td {{ border:1px solid #000; padding:6px; font-size:11px; }}
  th {{ background:#1e4d8c; color:#fff; }}
  tr.tot td {{ background:#eef2f8; }}
  .right {{ text-align:right; }}
  .ctr {{ text-align:center; }}
</style></head>
<body>
  <div class="right">Date: {esc(', '.join(dates) or '---')}</div>
  <h2>District Library Office, Dindigul</h2>
  <h3>PAYMENT CLEARED INTIMATION FOR {esc(quarter)}</h3>
  <p>Sir,</p>
  {ref_html}
  {para_html}
  <table>
    <tr><th>S.No</th><th>Magazine</th><th>Invoice No</th><th>Invoice Date</th>
        <th>Bill Amt</th><th>Deduction</th><th>Paid Amt</th></tr>
    {rows_html}
  </table>
  <p><strong>Transaction Ref No:</strong> {esc(', '.join(txns) or '---')}
     &nbsp;&nbsp;|&nbsp;&nbsp; <strong>Payment Date:</strong> {esc(', '.join(dates) or '---')}</p>
  <p><strong>Remarks:</strong> {remarks_txt}</p>
  <p>Please send back the Acknowledgement receipt.</p>
  <p><strong>Bank Details:</strong></p>
  <table>
    <tr><td>PAYEE NAME</td><td>{esc(bank.get('payeeName') or '---')}</td></tr>
    <tr><td>BANK NAME</td><td>{esc(bank.get('bankName') or '---')}</td></tr>
    <tr><td>BRANCH</td><td>{esc(bank.get('branch') or '---')}</td></tr>
    <tr><td>A/C NO</td><td>{esc(bank.get('accNo') or '---')}</td></tr>
    <tr><td>IFSC</td><td>{esc(bank.get('ifsc') or '---')}</td></tr>
  </table>
  <br><br>
  <p class="right">Thanking and Regards,<br>District Library Officer<br>Dindigul</p>
</body></html>"""


def build_payment_intimation_html(p):
    """ஒற்றை இதழுக்கான letter — குழு தர்க்கத்தையே பயன்படுத்துகிறது."""
    return build_group_intimation_html([p])


def fetch_payment_for_mail(cur, payment_id):
    """ஒரு payments.id-க்கான, மெயில்/PDF-க்குத் தேவையான தகவல்கள் அனைத்தையும் ஒரே dict-ஆக எடுக்கும்."""
    cur.execute(
        """
        SELECT p.*, m.email_id, m.payee_name, m.bank_name, m.bank_place,
               m.bank_account_number, m.ifsc_code, m.vendor_name, m.tnpfts_code
        FROM payments p
        LEFT JOIN magazines m ON m.name = p.magazine
        WHERE p.id=%s
        """,
        (payment_id,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "magazine": r["magazine"],
        "voucherNo": r["voucher_no"] or "",
        "vendorCode": (r["tnpfts_code"] or "").strip(),
        "vendorName": vendor_display_name(r["vendor_name"], r["payee_name"], r["magazine"]),
        "invoiceNo": r["invoice_no"] or "",
        "invoiceDate": fmt_date(r["invoice_date"]),
        "supplyQty": float(r["total_issues"] or 0),
        "billAmount": float(r["requested_amt"] or 0),
        "deduction": float(r["deduction"] or 0),
        "netAmount": float(r["paid_amt"] or 0),
        "transactionNo": r["transaction_no"] or "",
        "paymentDate": fmt_date(r["payment_date"]),
        "quarter": r["quarter"] or "",
        "email": r["email_id"] or "",
        "remarks": r["remarks"] or "",
        "bank": {
            "payeeName": r["payee_name"] or "",
            "bankName": r["bank_name"] or "",
            "branch": r["bank_place"] or "",
            "accNo": r["bank_account_number"] or "",
            "ifsc": r["ifsc_code"] or "",
        },
    }


def fetch_mail_group(cur, lead_id):
    """lead_id உள்ள இதழின் Vendor Code + Quarter-ல் மெயிலுக்குத் தயாரான (தொகை வழங்கி, Transaction No
    பதிவாகி, இன்னும் மெயில் அனுப்பாத) அனைத்து இதழ்களையும் voucher வரிசையில் திருப்பும்.
    திருப்புவது: (members | None, blockers | None)."""
    cur.execute(
        "SELECT p.id, p.quarter, p.bill_set_no, p.transaction_no, m.tnpfts_code FROM payments p "
        "LEFT JOIN magazines m ON m.name = p.magazine WHERE p.id=%s",
        (lead_id,),
    )
    lead = cur.fetchone()
    if not lead:
        return None, None
    code = norm_code(lead["tnpfts_code"])
    set_no = (lead["bill_set_no"] or "").strip()
    if code and set_no:
        # ஒரே Quarter + Code + Set No + Transaction No → ஒரே மெயில்/PDF; இல்லையெனில் தனியாக
        cur.execute(
            """
            SELECT p.id FROM payments p
            LEFT JOIN magazines m ON m.name = p.magazine
            WHERE p.quarter=%s AND UPPER(TRIM(COALESCE(m.tnpfts_code,'')))=%s
              AND TRIM(COALESCE(p.bill_set_no,''))=%s
              AND p.payment_date IS NOT NULL
              AND p.transaction_no = %s
              AND p.mail_sent = FALSE
            """,
            (lead["quarter"], code, set_no, lead["transaction_no"]),
        )
        ids = [r["id"] for r in cur.fetchall()]
    else:
        ids = [lead_id]
    members = [fetch_payment_for_mail(cur, i) for i in ids]
    members = [m for m in members if m]
    members.sort(key=lambda m: (voucher_sort_key(m["voucherNo"]), m["magazine"]))
    blockers = fetch_group_blockers(cur, lead["quarter"]).get((lead["quarter"], code)) if code else None
    return members, blockers


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


def magazines_and_master_for_quarter(cur, quarter):
    """கொடுக்கப்பட்ட quarter-க்குப் பொருந்தும் இதழ்கள் பட்டியலையும் (carry-forward
    விலை உள்பட) அந்த quarter-க்கான master dict-ஐயும் திரும்பத் தரும்.
    /api/magazines இலும், Magazine-wise Details report-லும் பயன்படுகிறது."""
    cur.execute(
        """
        SELECT m.name, m.periodicity, m.language,
               q.quarter, q.issue_price, q.price, q.discount, q.no_of_libraries
        FROM magazine_quarters q
        JOIN magazines m ON m.id = q.magazine_id
        ORDER BY m.name, q.quarter
        """
    )
    rows = cur.fetchall()

    best = {}  # name -> (rank, key, row)   rank 0=exact, 1=முந்தைய அண்மையது, 2=பிந்தைய அண்மையது
    for r in rows:
        name = r["name"]
        q = r["quarter"]
        if q == quarter:
            rank, key = 0, q
        elif q < quarter:
            rank, key = 1, q
        else:
            rank, key = 2, q
        cur_best = best.get(name)
        if cur_best is None:
            best[name] = (rank, key, r)
            continue
        brank, bkey, _ = cur_best
        if rank < brank:
            best[name] = (rank, key, r)
        elif rank == brank:
            if rank == 1 and key > bkey:      # முந்தையதில் — quarter-க்கு மிக அண்மையதைத் தேர்வு (பெரியது)
                best[name] = (rank, key, r)
            elif rank == 2 and key < bkey:     # பிந்தையதில் — மிக அண்மையதைத் தேர்வு (சிறியது)
                best[name] = (rank, key, r)

    magazines = []
    master = {}
    for name, (rank, key, r) in best.items():
        magazines.append(name)
        master[name] = {
            "issuePrice": float(r["issue_price"] or 0),
            "noOfLibraries": int(r["no_of_libraries"] or 0),
            "periodicity": r["periodicity"] or "",
            "language": r["language"] or "",
            "price": float(r["price"] or 0),
            "discount": float(r["discount"] or 0),
            "priceQuarter": key,
            "isCarriedForward": rank != 0,
        }
    return magazines, master


# --------------------------------------------------------------------------- #
# 1) getAllData — quarter-க்கான இதழ் master data
# --------------------------------------------------------------------------- #
@app.route("/api/magazines")
def api_magazines():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                magazines, master = magazines_and_master_for_quarter(cur, quarter)
            else:
                # quarter குறிப்பிடாவிட்டால் — அனைத்து இதழ்களும் (price 0-உடன்)
                cur.execute(
                    "SELECT name, periodicity, language FROM magazines ORDER BY name"
                )
                rows = cur.fetchall()
                magazines, master = [], {}
                for r in rows:
                    magazines.append(r["name"])
                    master[r["name"]] = {
                        "issuePrice": 0.0,
                        "noOfLibraries": 0,
                        "periodicity": r["periodicity"] or "",
                        "language": r["language"] or "",
                        "price": 0.0,
                        "discount": 0.0,
                    }

        return jsonify({"success": True, "magazines": sorted(magazines), "master": master})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 2) checkDuplicate
# --------------------------------------------------------------------------- #
@app.route("/api/payments/check-duplicate")
def api_check_duplicate():
    magazine = request.args.get("magazine", "").strip()
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM payments WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            row = cur.fetchone()
        if not row:
            return jsonify({"exists": False})
        return jsonify(
            {
                "exists": True,
                "invoiceNo": row["invoice_no"] or "",
                "invoiceDate": row["invoice_date"].strftime("%d/%m/%Y") if row["invoice_date"] else "",
                "invoiceDateISO": row["invoice_date"].isoformat() if row["invoice_date"] else "",
                "requestedAmt": float(row["requested_amt"] or 0),
            }
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3) addPayment (add / update)
# --------------------------------------------------------------------------- #
@app.route("/api/payments", methods=["POST"])
def api_add_payment():
    payload = request.get_json(force=True)
    is_update = bool(request.args.get("update", "false").lower() == "true")

    magazine = payload.get("magazine")
    quarter = payload.get("quarter")
    issue_price = q_num(payload.get("issuePrice"))
    libraries = q_int(payload.get("noOfLibraries"))
    qtr_issues = q_int(payload.get("qtrIssues"))
    non_supply = q_int(payload.get("nonSupply"))

    total_issues = libraries * qtr_issues
    actual_cost = issue_price * total_issues
    deduction = issue_price * non_supply
    net_payable = actual_cost - deduction

    invoice_date = payload.get("invoiceDate") or None
    requested_amt = q_num(payload.get("requestedAmt"))
    invoice_no = payload.get("invoiceNo")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if is_update:
                cur.execute(
                    """
                    UPDATE payments SET
                        issue_price=%s, subscriptions=%s, qtr_issues=%s, total_issues=%s,
                        actual_cost=%s, non_supply=%s, deduction=%s, net_payable=%s,
                        invoice_no=%s, invoice_date=%s, requested_amt=%s, updated_at=now()
                    WHERE magazine=%s AND quarter=%s
                    RETURNING id
                    """,
                    (
                        issue_price, libraries, qtr_issues, total_issues,
                        actual_cost, non_supply, deduction, net_payable,
                        invoice_no, invoice_date, requested_amt,
                        magazine, quarter,
                    ),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return jsonify({"success": False, "message": "Update செய்ய record கிடைக்கவில்லை."}), 404
                conn.commit()
                return jsonify({"success": True, "message": "Updated"})
            else:
                cur.execute("SELECT COALESCE(MAX(sno), 0) + 1 AS next_sno FROM payments")
                next_sno = cur.fetchone()["next_sno"]
                cur.execute(
                    """
                    INSERT INTO payments
                        (sno, magazine, issue_price, subscriptions, qtr_issues, total_issues,
                         actual_cost, non_supply, deduction, net_payable,
                         invoice_no, invoice_date, requested_amt, quarter)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        next_sno, magazine, issue_price, libraries, qtr_issues, total_issues,
                        actual_cost, non_supply, deduction, net_payable,
                        invoice_no, invoice_date, requested_amt, quarter,
                    ),
                )
                new_id = cur.fetchone()["id"]
                conn.commit()
                return jsonify({"success": True, "row": new_id, "message": "Appended"})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 4) getMagazinesByQuarter — paid / unpaid பட்டியல்
# --------------------------------------------------------------------------- #
@app.route("/api/payments/by-quarter")
def api_by_quarter():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT magazine, payment_date FROM payments WHERE quarter=%s ORDER BY magazine",
                (quarter,),
            )
            rows = cur.fetchall()

        unpaid, paid = [], []
        seen_paid = set()
        for r in rows:
            if r["payment_date"]:
                if r["magazine"] not in seen_paid:
                    paid.append({"name": r["magazine"], "paymentDate": r["payment_date"].strftime("%d/%m/%Y")})
                    seen_paid.add(r["magazine"])
            else:
                unpaid.append(r["magazine"])

        return jsonify({"unpaid": sorted(set(unpaid)), "paid": paid})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 5) getNonSupplyFromDespatch
# --------------------------------------------------------------------------- #
@app.route("/api/despatch/non-supply")
def api_non_supply():
    quarter = request.args.get("quarter", "").strip()
    magazine = request.args.get("magazine", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT non_supply FROM despatch_nonsupply WHERE quarter=%s AND magazine=%s",
                (quarter, magazine),
            )
            row = cur.fetchone()
        return jsonify({"nonSupply": row["non_supply"] if row else 0})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6a) magazines-for-quarter — அந்த quarter-ல் Invoice பதிவான இதழ்கள் மட்டும் (தொகை வழங்கல் Tab dropdown-க்கு)
# --------------------------------------------------------------------------- #
@app.route("/api/payments/magazines-for-quarter")
def api_magazines_for_quarter():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT magazine FROM payments WHERE quarter=%s ORDER BY magazine",
                (quarter,),
            )
            rows = cur.fetchall()
        return jsonify({"success": True, "magazines": [r["magazine"] for r in rows]})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6b) fy-summary — இந்த நிதி ஆண்டில் (Q1-Q4) இந்த இதழுக்கு ஏற்கனவே தொகை வழங்கப்பட்ட Quarter-கள்
# --------------------------------------------------------------------------- #
@app.route("/api/payments/fy-summary")
def api_fy_summary():
    magazine = request.args.get("magazine", "").strip()
    quarter = request.args.get("quarter", "").strip()
    parts = quarter.split("-")
    fy_prefix = "-".join(parts[:2]) if len(parts) >= 2 else quarter  # "2026-2027"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT quarter, paid_amt FROM payments
                   WHERE magazine=%s AND quarter LIKE %s AND paid_amt > 0
                   ORDER BY quarter""",
                (magazine, fy_prefix + "-Q%"),
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "success": True,
                "rows": [{"quarter": r["quarter"], "paidAmt": float(r["paid_amt"] or 0)} for r in rows],
            }
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 6) getPaymentDetails
# --------------------------------------------------------------------------- #
@app.route("/api/payments/details")
def api_payment_details():
    quarter = request.args.get("quarter", "").strip()
    magazine = request.args.get("magazine", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM payments WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            row = cur.fetchone()
            cur.execute("SELECT tnpfts_code FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
            vendor_code = (m["tnpfts_code"] or "").strip() if m else ""
            # இந்த Quarter-க்கான Master விலை விவரங்கள் (விலை / கழிவு / நிகர விலை)
            cur.execute(
                """SELECT q.price, q.discount, q.issue_price
                   FROM magazine_quarters q
                   JOIN magazines mg ON mg.id = q.magazine_id
                   WHERE mg.name=%s AND q.quarter=%s""",
                (magazine, quarter),
            )
            mq = cur.fetchone()
        master_price = float(mq["price"] or 0) if mq else 0.0
        master_discount = float(mq["discount"] or 0) if mq else 0.0
        master_net = float(mq["issue_price"] or 0) if mq else 0.0
        if not row:
            return jsonify(
                {
                    "masterPrice": master_price, "masterDiscount": master_discount, "masterNetPrice": master_net,
                    "issuePrice": 0, "totalIssues": 0, "requestedAmt": 0, "paymentDate": None, "billSetNo": "",
                    "vendorCode": vendor_code, "vendorType": classify_vendor_code(vendor_code),
                    "voucherNo": "", "transactionNo": "", "mailSent": False,
                }
            )
        return jsonify(
            {
                "masterPrice": master_price,
                "masterDiscount": master_discount,
                "masterNetPrice": master_net or float(row["issue_price"] or 0),
                "issuePrice": float(row["issue_price"] or 0),
                "totalIssues": int(row["total_issues"] or 0),
                "requestedAmt": float(row["requested_amt"] or 0),
                "paymentDate": row["payment_date"].strftime("%d/%m/%Y") if row["payment_date"] else None,
                "billSetNo": row["bill_set_no"] or "",
                "vendorCode": vendor_code,
                "vendorType": classify_vendor_code(vendor_code),
                "voucherNo": row["voucher_no"] or "",
                "transactionNo": row["transaction_no"] or "",
                "mailSent": bool(row["mail_sent"]),
            }
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 7) savePaymentProcessing — தொகை வழங்கியதை பதிவு + Voucher உருவாக்கம்
# --------------------------------------------------------------------------- #
@app.route("/api/payments/process", methods=["POST"])
def api_save_payment_processing():
    data = request.get_json(force=True)
    magazine = data.get("magazine")
    quarter = data.get("quarter")
    non_supply = q_int(data.get("nonSupply"))
    net_payable = q_num(data.get("netPayable"))
    amount_now_paid = round(q_num(data.get("amountNowPaid")))  # Rupees மட்டும் — Paisa இல்லை
    payment_date = data.get("paymentDate") or None
    remarks = data.get("remarks")
    bill_set_no = data.get("billSetNo")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT issue_price FROM payments WHERE magazine=%s AND quarter=%s", (magazine, quarter))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "Record not found in PAYMENTS"}), 404
            issue_price = float(row["issue_price"] or 0)
            deduction = issue_price * non_supply

            # Vendor Code கட்டாயம் இருக்க வேண்டும் — இல்லையெனில் தொகை வழங்கல் பதிவு தடுக்கப்படும்
            cur.execute("SELECT tnpfts_code FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
            vendor_code = (m["tnpfts_code"] or "").strip() if m else ""
            if not vendor_code:
                return jsonify(
                    {"success": False, "message": f"'{magazine}' இதழுக்கு Vendor Code இல்லை. முதலில் Master Data → Vendors-ல் Vendor Code சேர்க்கவும்."}
                ), 400
            vendor_type = classify_vendor_code(vendor_code)

            # ஒரே Quarter-க்குள் ஒரே Bill Set-ல் BENEFICIARY மற்றும் BUSINESS VENDOR கலக்கக்கூடாது
            if bill_set_no:
                cur.execute(
                    """
                    SELECT p.magazine, m.tnpfts_code
                    FROM payments p
                    LEFT JOIN magazines m ON m.name = p.magazine
                    WHERE p.quarter=%s AND p.bill_set_no=%s AND p.magazine != %s
                    """,
                    (quarter, bill_set_no, magazine),
                )
                others = cur.fetchall()
                for o in others:
                    other_type = classify_vendor_code(o["tnpfts_code"])
                    if other_type and vendor_type and other_type != vendor_type:
                        return jsonify(
                            {
                                "success": False,
                                "message": (
                                    f"Bill Set {bill_set_no} ({quarter})-ல் ஏற்கனவே '{o['magazine']}' "
                                    f"({other_type}) உள்ளது. '{magazine}' ({vendor_type}) — BENEFICIARY மற்றும் "
                                    f"BUSINESS VENDOR ஒரே Set-ல் கலக்க முடியாது. வேறு Bill Set தேர்வு செய்யவும்."
                                ),
                            }
                        ), 400

            cur.execute(
                """
                UPDATE payments SET
                    non_supply=%s, deduction=%s, net_payable=%s,
                    paid_amt=%s, payment_date=%s, remarks=%s, bill_set_no=%s, updated_at=now()
                WHERE magazine=%s AND quarter=%s
                RETURNING sno, invoice_no, invoice_date, requested_amt
                """,
                (non_supply, deduction, net_payable, amount_now_paid, payment_date, remarks, bill_set_no,
                 magazine, quarter),
            )
            fresh = cur.fetchone()

            cur.execute("SELECT tnpfts_code FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
            tnpfts_code = m["tnpfts_code"] if m else ""

            cur.execute(
                """
                INSERT INTO vouchers
                    (payment_sno, magazine, tnpfts_code, invoice_no, invoice_date,
                     requested_amt, deduction, amount_paid, quarter)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fresh["sno"], magazine, tnpfts_code, fresh["invoice_no"], fresh["invoice_date"],
                    fresh["requested_amt"], deduction, amount_now_paid, quarter,
                ),
            )
            conn.commit()
        return jsonify({"success": True, "message": "Payment Processed & Voucher Generated Successfully!"})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 8) getPaidMagazinesList
# --------------------------------------------------------------------------- #
@app.route("/api/payments/paid")
def api_paid_list():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE paid_amt > 0 ORDER BY id")
            rows = cur.fetchall()

        result = []
        for r in rows:
            result.append(
                {
                    "magazine": r["magazine"],
                    "quarter": r["quarter"],
                    "invoiceNo": r["invoice_no"] or "",
                    "invoiceDate": r["invoice_date"].strftime("%d/%m/%Y") if r["invoice_date"] else "",
                    "requestedAmt": float(r["requested_amt"] or 0),
                    "paidAmt": float(r["paid_amt"] or 0),
                    "paidDate": r["payment_date"].strftime("%d/%m/%Y") if r["payment_date"] else "",
                    "mailSent": bool(r["mail_sent"]),
                    "pdfUrl": r["pdf_url"] or "",
                }
            )
        mag_list = sorted({r["magazine"] for r in result})
        return jsonify({"success": True, "rows": result, "magList": mag_list})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 9) getPaymentsWithTransactionDate / saveTransactionNumbers
# --------------------------------------------------------------------------- #
@app.route("/api/payments/transactions")
def api_get_transactions():
    """Bank Transaction பதிவுக்கான பட்டியல் — Voucher எண் (எண் மதிப்பு) வரிசையில், ஒரே Vendor/Beneficiary Code
    (அதே Quarter) உள்ள இதழ்கள் ஒரே குழுவாக. குழுவில் இன்னும் தொகை வழங்காத இதழ் இருந்தால் blocked=true."""
    quarter = request.args.get("quarter") or None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT p.id, p.voucher_no, p.magazine, p.paid_amt, p.payment_date, p.quarter, p.bill_set_no, "
                "       m.tnpfts_code, m.vendor_name, m.payee_name "
                "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
                "WHERE p.payment_date IS NOT NULL AND (p.transaction_no IS NULL OR p.transaction_no='')"
            )
            params = ()
            if quarter:
                sql += " AND p.quarter=%s"
                params = (quarter,)
            cur.execute(sql, params)
            rows = cur.fetchall()
            blockers = fetch_group_blockers(cur, quarter)

        groups = {}
        for r in rows:
            code = norm_code(r["tnpfts_code"])
            key = group_key_for(r)
            g = groups.get(key)
            if g is None:
                b = blockers.get((r["quarter"], code)) if code else None
                pending = list(b["pending"]) if b else []
                g = groups[key] = {
                    "key": "|".join(key),
                    "quarter": r["quarter"] or "",
                    "code": (r["tnpfts_code"] or "").strip(),
                    "billSetNo": (r["bill_set_no"] or "").strip(),
                    "vendorName": vendor_display_name(r["vendor_name"], r["payee_name"], r["magazine"]),
                    "items": [],
                    "total": 0.0,
                    "pending": pending,   # எச்சரிக்கைக்கு மட்டும் — தடை இல்லை
                    "blocked": False,
                }
            amt = float(r["paid_amt"] or 0)
            g["items"].append(
                {
                    "row": r["id"],
                    "voucherNo": r["voucher_no"] or "",
                    "magazine": r["magazine"],
                    "amountPaid": amt,
                    "paymentDate": r["payment_date"].strftime("%d-%m-%Y") if r["payment_date"] else "",
                }
            )
            g["total"] += amt

        out = []
        for g in groups.values():
            g["items"].sort(key=lambda it: (voucher_sort_key(it["voucherNo"]), it["magazine"]))
            g["rows"] = [it["row"] for it in g["items"]]
            out.append(g)
        out.sort(key=lambda g: (voucher_sort_key(g["items"][0]["voucherNo"]), g["vendorName"], g["quarter"]))
        return jsonify({"success": True, "groups": out})
    finally:
        conn.close()


@app.route("/api/payments/transactions", methods=["POST"])
def api_save_transactions():
    updates = request.get_json(force=True) or []  # [{row, transactionNo}]
    conn = get_conn()
    updated, errors, blocked_msgs = 0, [], set()
    try:
        with conn.cursor() as cur:
            ids = []
            for item in updates:
                try:
                    ids.append(int(item.get("row")))
                except (TypeError, ValueError):
                    pass
            info = {}
            if ids:
                cur.execute(
                    "SELECT p.id, p.magazine, p.quarter, m.tnpfts_code FROM payments p "
                    "LEFT JOIN magazines m ON m.name = p.magazine WHERE p.id = ANY(%s)",
                    (ids,),
                )
                info = {r["id"]: r for r in cur.fetchall()}
            for item in updates:
                try:
                    row = int(item.get("row"))
                    tx = str(item.get("transactionNo", "")).strip()
                    # குழுவில் மற்ற இதழ்கள் பாக்கி இருந்தாலும் தடை இல்லை — எச்சரிக்கை திரையில் மட்டும்
                    cur.execute(
                        "UPDATE payments SET transaction_no=%s, updated_at=now() WHERE id=%s",
                        (tx, row),
                    )
                    updated += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Row {item.get('row')}: {e}")
            conn.commit()
        errors = sorted(blocked_msgs) + errors
        if errors:
            return jsonify({"success": False, "updated": updated, "errors": errors,
                            "message": "சில பதிவுகள் சேமிக்கப்படவில்லை: " + " | ".join(errors)})
        return jsonify({"success": True, "updated": updated, "message": f"{updated} பதிவுகள் வெற்றிகரமாக புதுப்பிக்கப்பட்டன"})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/payments/group-status")
def api_group_status():
    """ஒரு இதழைத் தேர்ந்தெடுக்கும்போது, அதே Vendor/Beneficiary Code உள்ள மற்ற இதழ்கள் (அதே Quarter)
    ✅ வழங்கப்பட்டது / ⏳ வழங்க வேண்டும் / ❗ Invoice பதிவாகவில்லை என்ற நிலையுடன்."""
    quarter = request.args.get("quarter", "").strip()
    magazine = request.args.get("magazine", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tnpfts_code, vendor_name, payee_name FROM magazines WHERE name=%s", (magazine,)
            )
            m = cur.fetchone()
            code = norm_code(m["tnpfts_code"]) if m else ""
            if not code:
                return jsonify({"success": True, "code": "", "vendorName": "", "siblings": []})
            vendor = vendor_display_name(m["vendor_name"], m["payee_name"], code)

            cur.execute(
                "SELECT name FROM magazines WHERE UPPER(TRIM(COALESCE(tnpfts_code,'')))=%s AND name<>%s",
                (code, magazine),
            )
            names = {r["name"] for r in cur.fetchall()}
            if not names:
                return jsonify({"success": True, "code": code, "vendorName": vendor, "siblings": []})

            cur.execute(
                "SELECT magazine, payment_date, paid_amt, requested_amt, voucher_no, bill_set_no "
                "FROM payments WHERE quarter=%s AND magazine = ANY(%s)",
                (quarter, list(names)),
            )
            pay = {r["magazine"]: r for r in cur.fetchall()}
            # carry-forward இதழ்கள் குழுவில் வரக்கூடாது — அந்த Quarter-க்கு நேரடி Master பதிவு உள்ளவை மட்டும்
            in_quarter = get_direct_master_magazines_for_quarter(cur, quarter)

        siblings = []
        for name in sorted(names):
            p = pay.get(name)
            if p is None and name not in in_quarter:
                continue  # இந்த Quarter-க்குப் பொருந்தாத இதழ்
            if p is None:
                siblings.append({"magazine": name, "status": "no_invoice"})
            elif p["payment_date"]:
                siblings.append({
                    "magazine": name, "status": "paid",
                    "paidAmt": float(p["paid_amt"] or 0), "paymentDate": fmt_date(p["payment_date"]),
                    "billSetNo": (p["bill_set_no"] or "").strip(),
                })
            else:
                siblings.append({
                    "magazine": name, "status": "pending",
                    "requestedAmt": float(p["requested_amt"] or 0),
                })
        return jsonify({"success": True, "code": code, "vendorName": vendor, "siblings": siblings})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 10) Admin — Master Data (magazines + magazine_quarters)
# --------------------------------------------------------------------------- #

# CSV-ல் இருக்க வேண்டிய column headers (எழுத்துக்கள் பெரிய/சிறியதாக இருந்தாலும் பரவாயில்லை)
_CSV_HEADERS = {
    "language": "LANGUAGE",
    "name": "MAGAZINE NAME",
    "periodicity": "PERIODICITY",
    "price": "PRICE",
    "discount": "DISCOUNT",
    "issue_price": "AFTER DISCOUNT",
    "no_of_libraries": "SUBSCRIPTION",
    "quarters": "YEAR AND QUARTER",
}


def upsert_magazine_base(cur, name, language, periodicity):
    cur.execute(
        """
        INSERT INTO magazines (name, language, periodicity)
        VALUES (%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET
            language=EXCLUDED.language, periodicity=EXCLUDED.periodicity
        RETURNING id
        """,
        (name, language, periodicity),
    )
    return cur.fetchone()["id"]


def upsert_quarter_rows(cur, magazine_id, quarters, price, discount, issue_price, no_of_libraries):
    """quarters-ல் உள்ள ஒவ்வொரு quarter-க்கும் ஒரே price/discount/issue_price/no_of_libraries-உடன்
    magazine_quarters-ல் UPSERT செய்யும். quarters தேர்வு செய்யாத Quarter-களை இது தொடாது."""
    for quarter in quarters:
        quarter = quarter.strip()
        if not quarter:
            continue
        cur.execute(
            """
            INSERT INTO magazine_quarters
                (magazine_id, quarter, price, discount, issue_price, no_of_libraries)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (magazine_id, quarter) DO UPDATE SET
                price=EXCLUDED.price, discount=EXCLUDED.discount,
                issue_price=EXCLUDED.issue_price, no_of_libraries=EXCLUDED.no_of_libraries
            """,
            (magazine_id, quarter, price, discount, issue_price, no_of_libraries),
        )


def upsert_magazine_and_quarters(cur, name, language, periodicity, price, discount,
                                  issue_price, no_of_libraries, quarters_raw):
    """CSV import-க்காக — comma-separated quarters string ஏற்கும்."""
    magazine_id = upsert_magazine_base(cur, name, language, periodicity)
    quarters = [q.strip() for q in (quarters_raw or "").split(",") if q.strip()]
    upsert_quarter_rows(cur, magazine_id, quarters, price, discount, issue_price, no_of_libraries)
    return magazine_id, quarters


@app.route("/api/admin/magazines-list")
def api_admin_magazines_list():
    """Admin பக்கத்தில் காட்ட — ஒவ்வொரு இதழுக்கும் எத்தனை quarter records உள்ளன."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.name, m.language, m.periodicity,
                       (SELECT COUNT(*) FROM magazine_quarters c WHERE c.magazine_id = m.id) AS quarter_count,
                       ARRAY(SELECT c2.quarter FROM magazine_quarters c2
                             WHERE c2.magazine_id = m.id ORDER BY c2.quarter) AS quarters,
                       lq.quarter AS latest_quarter,
                       lq.price AS price, lq.discount AS discount, lq.issue_price AS issue_price
                FROM magazines m
                LEFT JOIN LATERAL (
                    SELECT quarter, price, discount, issue_price
                    FROM magazine_quarters
                    WHERE magazine_id = m.id
                    ORDER BY quarter DESC
                    LIMIT 1
                ) lq ON TRUE
                ORDER BY m.name
                """
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "success": True,
                "rows": [
                    {
                        "name": r["name"],
                        "language": r["language"] or "",
                        "periodicity": r["periodicity"] or "",
                        "quarterCount": r["quarter_count"],
                        "quarters": list(r["quarters"] or []),
                        "latestQuarter": r["latest_quarter"] or "",
                        "price": float(r["price"]) if r["price"] is not None else None,
                        "discount": float(r["discount"]) if r["discount"] is not None else None,
                        "issuePrice": float(r["issue_price"]) if r["issue_price"] is not None else None,
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.route("/api/admin/add-quarter", methods=["POST"])
def api_admin_add_quarter():
    """ஒரு அல்லது பல இதழ்களுக்கு புதிய Quarter சேர்க்கும். விலை/கழிவு/நிகர விலை/நூலகங்கள் —
    அந்த இதழின் அருகிலுள்ள முந்தைய Quarter-லிருந்து (இல்லையெனில் அருகிலுள்ள பிந்தையதிலிருந்து) நகலெடுக்கப்படும்.
    ஏற்கனவே அந்த Quarter உள்ள இதழ்களை மாற்றாது.
    body: {quarter, magazines:[...]} அல்லது {quarter, magazine}"""
    data = request.get_json(force=True) or {}
    quarter = (data.get("quarter") or "").strip()
    names = data.get("magazines") or ([data.get("magazine")] if data.get("magazine") else [])
    names = [str(n).strip() for n in names if str(n or "").strip()]
    if not re.fullmatch(r"\d{4}-\d{4}-Q[1-4]", quarter):
        return jsonify({"success": False, "message": "Quarter வடிவம் தவறு (எ.கா. 2026-2027-Q2)"}), 400
    if not names:
        return jsonify({"success": False, "message": "குறைந்தது ஒரு இதழைத் தேர்ந்தெடுக்கவும்"}), 400

    conn = get_conn()
    added, existed, no_source, missing = [], [], [], []
    try:
        with conn.cursor() as cur:
            for name in names:
                cur.execute("SELECT id FROM magazines WHERE name=%s", (name,))
                m = cur.fetchone()
                if not m:
                    missing.append(name)
                    continue
                mid = m["id"]
                cur.execute("SELECT 1 FROM magazine_quarters WHERE magazine_id=%s AND quarter=%s", (mid, quarter))
                if cur.fetchone():
                    existed.append(name)
                    continue
                cur.execute(
                    """SELECT price, discount, issue_price, no_of_libraries FROM magazine_quarters
                       WHERE magazine_id=%s AND quarter<%s ORDER BY quarter DESC LIMIT 1""",
                    (mid, quarter),
                )
                src = cur.fetchone()
                if not src:
                    cur.execute(
                        """SELECT price, discount, issue_price, no_of_libraries FROM magazine_quarters
                           WHERE magazine_id=%s AND quarter>%s ORDER BY quarter ASC LIMIT 1""",
                        (mid, quarter),
                    )
                    src = cur.fetchone()
                if not src:
                    no_source.append(name)
                    continue
                cur.execute(
                    """INSERT INTO magazine_quarters (magazine_id, quarter, price, discount, issue_price, no_of_libraries)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (magazine_id, quarter) DO NOTHING""",
                    (mid, quarter, src["price"], src["discount"], src["issue_price"], src["no_of_libraries"]),
                )
                added.append(name)
            conn.commit()
        msg = f"{quarter}: {len(added)} இதழ்களுக்குச் சேர்க்கப்பட்டது"
        if existed:
            msg += f" · {len(existed)} இதழ்களுக்கு ஏற்கனவே இருந்தது"
        if no_source or missing:
            msg += f" · {len(no_source) + len(missing)} இதழ்களுக்கு விலை மூலம் கிடைக்கவில்லை"
        return jsonify({"success": True, "message": msg, "added": added, "existed": existed,
                        "noSource": no_source, "missing": missing})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/import-magazines", methods=["POST"])
def api_admin_import_magazines():
    """CSV text (Google Sheet Export) படித்து magazines + magazine_quarters-ல் bulk UPSERT."""
    payload = request.get_json(force=True)
    csv_text = (payload.get("csvText") or "").strip()
    if not csv_text:
        return jsonify({"success": False, "message": "CSV தரவு காலியாக இருக்கிறது."}), 400

    reader = csv.DictReader(io.StringIO(csv_text))
    # header-களை trim + uppercase செய்து match செய்கிறோம்
    if reader.fieldnames:
        reader.fieldnames = [normalize_csv_header(h) for h in reader.fieldnames]

    required = set(_CSV_HEADERS.values()) - {"LANGUAGE"}  # LANGUAGE optional
    missing = [h for h in required if h not in (reader.fieldnames or [])]
    if missing:
        return jsonify(
            {
                "success": False,
                "message": (
                    f"CSV-ல் இந்த columns காணவில்லை: {', '.join(missing)}. "
                    f"கண்டறியப்பட்ட columns: {', '.join(reader.fieldnames or []) or '(ஏதுமில்லை)'}"
                ),
            }
        ), 400

    conn = get_conn()
    imported, quarter_rows, errors = 0, 0, []
    try:
        with conn.cursor() as cur:
            for i, row in enumerate(reader, start=2):  # header row = 1
                name = (row.get(_CSV_HEADERS["name"]) or "").strip()
                if not name:
                    continue
                try:
                    language = (row.get(_CSV_HEADERS["language"]) or "").strip()
                    periodicity = (row.get(_CSV_HEADERS["periodicity"]) or "").strip()
                    price = q_num(row.get(_CSV_HEADERS["price"]))
                    discount = q_num(row.get(_CSV_HEADERS["discount"]))
                    issue_price = q_num(row.get(_CSV_HEADERS["issue_price"]))
                    no_of_libraries = q_int(row.get(_CSV_HEADERS["no_of_libraries"]))
                    quarters_raw = row.get(_CSV_HEADERS["quarters"]) or ""

                    _, qs = upsert_magazine_and_quarters(
                        cur, name, language, periodicity, price, discount,
                        issue_price, no_of_libraries, quarters_raw,
                    )
                    imported += 1
                    quarter_rows += len(qs)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Row {i} ({name}): {e}")

            conn.commit()
        return jsonify(
            {
                "success": True,
                "imported": imported,
                "quarterRows": quarter_rows,
                "errors": errors,
                "message": f"{imported} இதழ்கள் import ஆயின ({quarter_rows} quarter records).",
            }
        )
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/add-magazine", methods=["POST"])
def api_admin_add_magazine():
    """புதிய ஒரு இதழை (ஒரு quarter-உடன்) நேரடியாக website-லேயே சேர்க்க."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    if not name or not quarter:
        return jsonify({"success": False, "message": "Magazine Name மற்றும் Quarter அவசியம்."}), 400

    language = (data.get("language") or "").strip()
    periodicity = (data.get("periodicity") or "").strip()
    price = q_num(data.get("price"))
    discount = q_num(data.get("discount"))
    issue_price = q_num(data.get("issuePrice"))
    no_of_libraries = q_int(data.get("noOfLibraries"))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            upsert_magazine_and_quarters(
                cur, name, language, periodicity, price, discount,
                issue_price, no_of_libraries, quarter,
            )
            conn.commit()
        return jsonify({"success": True, "message": f"'{name}' சேர்க்கப்பட்டது."})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/magazine-detail")
def api_admin_magazine_detail():
    name = request.args.get("name", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, language, periodicity FROM magazines WHERE name=%s", (name,))
            mag = cur.fetchone()
            if not mag:
                return jsonify({"success": False, "message": "இதழ் கிடைக்கவில்லை"}), 404
            cur.execute(
                """SELECT quarter, price, discount, issue_price, no_of_libraries
                   FROM magazine_quarters WHERE magazine_id=%s ORDER BY quarter""",
                (mag["id"],),
            )
            qrows = cur.fetchall()
        return jsonify(
            {
                "success": True,
                "name": mag["name"],
                "language": mag["language"] or "",
                "periodicity": mag["periodicity"] or "",
                "quarters": {
                    r["quarter"]: {
                        "price": float(r["price"] or 0),
                        "discount": float(r["discount"] or 0),
                        "issuePrice": float(r["issue_price"] or 0),
                        "noOfLibraries": int(r["no_of_libraries"] or 0),
                    }
                    for r in qrows
                },
            }
        )
    finally:
        conn.close()


@app.route("/api/admin/update-magazine", methods=["POST"])
def api_admin_update_magazine():
    """Master Data Edit — தேர்வு செய்த Quarter-கள் மட்டும் புதிய விலையுடன் புதுப்பிக்கப்படும்;
    தேர்வு செய்யாத Quarter-களின் பழைய விலை அப்படியே இருக்கும்."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    quarters = data.get("quarters") or []
    if not name:
        return jsonify({"success": False, "message": "Magazine Name அவசியம்."}), 400

    language = (data.get("language") or "").strip()
    periodicity = (data.get("periodicity") or "").strip()
    price = q_num(data.get("price"))
    discount = q_num(data.get("discount"))
    issue_price = q_num(data.get("issuePrice"))
    no_of_libraries = q_int(data.get("noOfLibraries"))

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            magazine_id = upsert_magazine_base(cur, name, language, periodicity)
            if quarters:
                upsert_quarter_rows(cur, magazine_id, quarters, price, discount, issue_price, no_of_libraries)
            conn.commit()
        return jsonify({"success": True, "message": f"'{name}' புதுப்பிக்கப்பட்டது ({len(quarters)} quarter(s))."})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/delete-magazine-quarters", methods=["POST"])
def api_admin_delete_magazine_quarters():
    """இதழ்கள் நீக்குதல் — Master விலைப் பதிவுகளை மட்டும் நீக்கும் (Invoice/Payment records தொடாது).
    scope='one'    -> தேர்ந்தெடுத்த ஒரே quarter-க்கான விலைப் பதிவு நீக்கப்படும்.
    scope='onward' -> தேர்ந்தெடுத்த quarter முதல் அதற்குப் பிறகுள்ள அனைத்து quarter விலைப் பதிவுகளும் நீக்கப்படும்.
    """
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    scope = (data.get("scope") or "one").strip()
    if not name or not quarter:
        return jsonify({"success": False, "message": "Magazine Name மற்றும் Quarter அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM magazines WHERE name=%s", (name,))
            mag = cur.fetchone()
            if not mag:
                return jsonify({"success": False, "message": "இதழ் கிடைக்கவில்லை"}), 404

            if scope == "onward":
                cur.execute(
                    "DELETE FROM magazine_quarters WHERE magazine_id=%s AND quarter>=%s",
                    (mag["id"], quarter),
                )
            else:
                cur.execute(
                    "DELETE FROM magazine_quarters WHERE magazine_id=%s AND quarter=%s",
                    (mag["id"], quarter),
                )
            deleted = cur.rowcount
            conn.commit()
        return jsonify(
            {
                "success": True,
                "deleted": deleted,
                "message": f"'{name}' — {deleted} quarter விலைப் பதிவு(கள்) நீக்கப்பட்டன. "
                           f"ஏற்கனவே பதிவான Invoice/Payment தரவுகள் தொடரவில்லை.",
            }
        )
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/payment-delete", methods=["POST"])
def api_admin_payment_delete():
    """Payment Details Delete — தொகை வழங்கியதை (Payment stage) மட்டும் நீக்கி,
    பதிவை மீண்டும் 'Invoice மட்டும் பதிவான' நிலைக்கு கொண்டு செல்லும்.
    Invoice details (invoice_no/invoice_date/requested_amt) தொடப்படாது."""
    data = request.get_json(force=True)
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    if not magazine or not quarter:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Quarter அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, paid_amt FROM payments WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "பதிவு கிடைக்கவில்லை."}), 404

            cur.execute(
                """
                UPDATE payments SET
                    paid_amt=0, payment_date=NULL, transaction_no=NULL,
                    voucher_no=NULL, bill_set_no=NULL, remarks=NULL,
                    mail_sent=FALSE, pdf_url=NULL, updated_at=now()
                WHERE magazine=%s AND quarter=%s
                """,
                (magazine, quarter),
            )
            cur.execute(
                "DELETE FROM vouchers WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            conn.commit()
        return jsonify(
            {
                "success": True,
                "message": f"'{magazine}' ({quarter}) — Payment விவரங்கள் நீக்கப்பட்டு, மீண்டும் "
                           f"Invoice பதிவு நிலைக்கு சென்றது. Invoice விவரங்கள் மாறவில்லை.",
            }
        )
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/delete-payment-details", methods=["POST"])
def api_admin_delete_payment_details():
    """Payment processing விவரங்களை (paid amt/date/txn/voucher) அழித்து, அந்த Invoice-ஐ
    மீண்டும் 'தொகை வழங்கப்படாதது' நிலைக்கு கொண்டு வரும். Invoice விவரங்கள் தொடாது."""
    data = request.get_json(force=True)
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    if not magazine or not quarter:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Quarter அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sno FROM payments WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "பதிவு கிடைக்கவில்லை."}), 404

            cur.execute(
                """
                UPDATE payments SET
                    non_supply=0, deduction=0, net_payable=0,
                    paid_amt=0, payment_date=NULL, transaction_no=NULL,
                    remarks=NULL, bill_set_no=NULL, mail_sent=FALSE, pdf_url=NULL,
                    voucher_no=NULL, updated_at=now()
                WHERE magazine=%s AND quarter=%s
                """,
                (magazine, quarter),
            )
            cur.execute(
                "DELETE FROM vouchers WHERE magazine=%s AND quarter=%s",
                (magazine, quarter),
            )
            conn.commit()
        return jsonify({"success": True, "message": f"'{magazine}' ({quarter}) மீண்டும் Invoice நிலைக்கு மாற்றப்பட்டது."})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/delete-transaction", methods=["POST"])
def api_admin_delete_transaction():
    """Master Data → 'தவறான / TEST பதிவு நீக்கு' — ஒரு இதழ் + Quarter-க்கான Invoice/Payment
    தரவை, தேர்ந்தெடுத்த அளவுக்கு நீக்கும். இதழ் விலை Master (magazines / magazine_quarters)
    எப்போதும் தொடப்படாது.

    scope:
      voucher  -> voucher_no மட்டும் நீக்கும்
      bank     -> Payment Processing-ல் பதிவான தொகை/தேதி/Transaction No/Non-Supply/
                  Deduction/Net Payable/Bill Set + vouchers table row நீக்கும்
      mail     -> mail_sent/pdf_url மட்டும் நீக்கும்
      payment  -> voucher + bank + mail — மூன்றும் சேர்ந்து நீக்கும் (Invoice விவரம் தொடாது)
      invoice  -> ஒட்டுமொத்தமாக இந்த Invoice/Payment பதிவையே (payments row) நீக்கும்
    """
    data = request.get_json(force=True)
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    scope = (data.get("scope") or "").strip()
    if not magazine or not quarter or not scope:
        return jsonify({"success": False, "message": "இதழ், Quarter மற்றும் என்ன நீக்க வேண்டும் என தேர்ந்தெடுக்கவும்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM payments WHERE magazine=%s AND quarter=%s", (magazine, quarter))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "இந்த இதழ் / Quarter-க்கு பதிவு கிடைக்கவில்லை."}), 404

            if scope == "voucher":
                cur.execute(
                    "UPDATE payments SET voucher_no=NULL, updated_at=now() WHERE magazine=%s AND quarter=%s",
                    (magazine, quarter),
                )
                msg = "Voucher Number நீக்கப்பட்டது."

            elif scope == "bank":
                cur.execute(
                    """
                    UPDATE payments SET
                        non_supply=0, deduction=0, net_payable=0,
                        paid_amt=0, payment_date=NULL, transaction_no=NULL,
                        remarks=NULL, bill_set_no=NULL, updated_at=now()
                    WHERE magazine=%s AND quarter=%s
                    """,
                    (magazine, quarter),
                )
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s", (magazine, quarter))
                msg = "Bank Transaction (தொகை/தேதி/Transaction No) விவரங்கள் நீக்கப்பட்டன."

            elif scope == "mail":
                cur.execute(
                    "UPDATE payments SET mail_sent=FALSE, pdf_url=NULL, updated_at=now() WHERE magazine=%s AND quarter=%s",
                    (magazine, quarter),
                )
                msg = "Mail Send நிலை நீக்கப்பட்டது."

            elif scope == "payment":
                cur.execute(
                    """
                    UPDATE payments SET
                        non_supply=0, deduction=0, net_payable=0,
                        paid_amt=0, payment_date=NULL, transaction_no=NULL,
                        voucher_no=NULL, remarks=NULL, bill_set_no=NULL,
                        mail_sent=FALSE, pdf_url=NULL, updated_at=now()
                    WHERE magazine=%s AND quarter=%s
                    """,
                    (magazine, quarter),
                )
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s", (magazine, quarter))
                msg = "Payment Details (Voucher Number + Bank Transaction + Mail Send) அனைத்தும் நீக்கப்பட்டன."

            elif scope == "invoice":
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s", (magazine, quarter))
                cur.execute("DELETE FROM payments WHERE magazine=%s AND quarter=%s", (magazine, quarter))
                msg = "Invoice Details உட்பட இந்த Quarter பதிவு முழுவதும் நீக்கப்பட்டது. (இதழ் விலை Master தொடப்படவில்லை.)"

            else:
                return jsonify({"success": False, "message": "தவறான தேர்வு."}), 400

            conn.commit()
        return jsonify({"success": True, "message": f"'{magazine}' ({quarter}) — {msg}"})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 11) Vendors — magazines table-ல் உள்ள Vendor/Bank columns (tnpfts_code = Vendor Code)
# --------------------------------------------------------------------------- #
# ஒவ்வொரு field-க்கும் ஏற்கக்கூடிய column பெயர்கள் (Excel-ல் பயன்படுத்துபவர்கள் "VENDOR" மற்றும்
# "CODE" என தனித்தனியாக வைக்காமல் "VENDOR CODE" என ஒரே column-ஆக வைப்பதும் உண்டு — இரண்டையும்
# ஏற்றுக்கொள்வோம்).
_VENDOR_CSV_HEADER_ALIASES = {
    "magazine": ["NAME OF MAGAZINE"],
    "vendor_name": ["VENDOR", "VENDOR NAME"],
    "code": ["CODE", "VENDOR CODE"],
    "bank_account_number": ["BANK ACCOUNT NUMBER"],
    "bank_name": ["BANK NAME"],
    "bank_place": ["BANK PLACE"],
    "ifsc_code": ["IFSC CODE"],
    "payee_name": ["NAME OF PAYEE"],
    "email_id": ["EMAIL ID"],
}


def _resolve_vendor_csv_headers(fieldnames):
    """CSV-ல் கிடைத்த fieldnames-ஐ வைத்து, ஒவ்வொரு field-க்கும் எந்த actual column
    பெயர் பொருந்துகிறது என கண்டறிந்து ஒரு dict ஆக தரும் (field -> actual header, அல்லது
    கிடைக்கவில்லை எனில் None)."""
    fieldnames = fieldnames or []
    resolved = {}
    for field, aliases in _VENDOR_CSV_HEADER_ALIASES.items():
        resolved[field] = next((a for a in aliases if a in fieldnames), None)
    return resolved


def upsert_vendor(cur, name, vendor_name, code, bank_account_number, bank_name, bank_place,
                   ifsc_code, payee_name, email_id):
    """இதழ் பெயரால் UPSERT — இதழ் இன்னும் magazines-ல் இல்லையெனில் Vendor விவரம் மட்டுமே கொண்ட
    ஒரு புதிய row உருவாகும் (மற்ற master விவரங்கள் பின்னால் சேர்க்கலாம்).

    ஒரே இதழை மீண்டும் மீண்டும் CSV-ல் upload செய்தால் (எழுத்து அளவு / இடைவெளி சிறிது
    வேறுபட்டாலும்) double entry வராமல், ஏற்கனவே உள்ள அதே இதழ் row-ஐயே கண்டறிந்து அதை
    UPDATE செய்யும்படி — முதலில் case/space-insensitive ஆக பொருந்தும் பெயரைத் தேடுகிறோம்."""
    cur.execute(
        "SELECT name FROM magazines WHERE lower(regexp_replace(name, '\\s+', ' ', 'g')) = lower(%s) LIMIT 1",
        (name,),
    )
    existing = cur.fetchone()
    match_name = existing["name"] if existing else name

    cur.execute(
        """
        INSERT INTO magazines (name, vendor_name, tnpfts_code, bank_account_number, bank_name,
                                bank_place, ifsc_code, payee_name, email_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (name) DO UPDATE SET
            vendor_name=EXCLUDED.vendor_name, tnpfts_code=EXCLUDED.tnpfts_code,
            bank_account_number=EXCLUDED.bank_account_number, bank_name=EXCLUDED.bank_name,
            bank_place=EXCLUDED.bank_place, ifsc_code=EXCLUDED.ifsc_code,
            payee_name=EXCLUDED.payee_name, email_id=EXCLUDED.email_id
        RETURNING id
        """,
        (match_name, vendor_name, code, bank_account_number, bank_name, bank_place, ifsc_code, payee_name, email_id),
    )
    return cur.fetchone()["id"]


@app.route("/api/admin/vendors-list")
def api_admin_vendors_list():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT name, vendor_name, tnpfts_code, bank_account_number, bank_name,
                          bank_place, ifsc_code, payee_name, email_id
                   FROM magazines ORDER BY name"""
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "success": True,
                "rows": [
                    {
                        "name": r["name"],
                        "vendorName": r["vendor_name"] or "",
                        "code": r["tnpfts_code"] or "",
                        "vendorType": classify_vendor_code(r["tnpfts_code"]) or "",
                        "bankAccountNumber": r["bank_account_number"] or "",
                        "bankName": r["bank_name"] or "",
                        "bankPlace": r["bank_place"] or "",
                        "ifscCode": r["ifsc_code"] or "",
                        "payeeName": r["payee_name"] or "",
                        "emailId": r["email_id"] or "",
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.route("/api/admin/vendor-detail")
def api_admin_vendor_detail():
    name = request.args.get("name", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT name, vendor_name, tnpfts_code, bank_account_number, bank_name,
                          bank_place, ifsc_code, payee_name, email_id
                   FROM magazines WHERE name=%s""",
                (name,),
            )
            r = cur.fetchone()
        if not r:
            return jsonify({"success": False, "message": "இதழ் கிடைக்கவில்லை"}), 404
        return jsonify(
            {
                "success": True,
                "name": r["name"],
                "vendorName": r["vendor_name"] or "",
                "code": r["tnpfts_code"] or "",
                "bankAccountNumber": r["bank_account_number"] or "",
                "bankName": r["bank_name"] or "",
                "bankPlace": r["bank_place"] or "",
                "ifscCode": r["ifsc_code"] or "",
                "payeeName": r["payee_name"] or "",
                "emailId": r["email_id"] or "",
            }
        )
    finally:
        conn.close()


@app.route("/api/admin/update-vendor", methods=["POST"])
def api_admin_update_vendor():
    """புதிய Vendor சேர்ப்பதற்கும், ஏற்கனவே உள்ள ஒன்றை திருத்துவதற்கும் — இரண்டுக்கும் இதுவே."""
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "message": "இதழ் பெயர் அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            upsert_vendor(
                cur, name,
                (data.get("vendorName") or "").strip(),
                (data.get("code") or "").strip(),
                (data.get("bankAccountNumber") or "").strip(),
                (data.get("bankName") or "").strip(),
                (data.get("bankPlace") or "").strip(),
                (data.get("ifscCode") or "").strip(),
                (data.get("payeeName") or "").strip(),
                (data.get("emailId") or "").strip(),
            )
            conn.commit()
        return jsonify({"success": True, "message": f"'{name}' Vendor விவரம் சேமிக்கப்பட்டது."})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/import-vendors", methods=["POST"])
def api_admin_import_vendors():
    """CSV text (Excel Export) படித்து magazines-ல் Vendor/Bank விவரங்களை bulk UPSERT."""
    payload = request.get_json(force=True)
    csv_text = (payload.get("csvText") or "").strip()
    if not csv_text:
        return jsonify({"success": False, "message": "CSV தரவு காலியாக இருக்கிறது."}), 400

    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames:
        reader.fieldnames = [normalize_csv_header(h) for h in reader.fieldnames]

    header_map = _resolve_vendor_csv_headers(reader.fieldnames)
    required_fields = ["magazine", "code"]
    missing = [
        " / ".join(_VENDOR_CSV_HEADER_ALIASES[f])
        for f in required_fields
        if not header_map.get(f)
    ]
    if missing:
        return jsonify(
            {
                "success": False,
                "message": (
                    f"CSV-ல் இந்த columns காணவில்லை: {', '.join(missing)}. "
                    f"கண்டறியப்பட்ட columns: {', '.join(reader.fieldnames or []) or '(ஏதுமில்லை)'}"
                ),
            }
        ), 400

    conn = get_conn()
    imported, errors = 0, []
    try:
        with conn.cursor() as cur:
            def _val(row, field):
                col = header_map.get(field)
                return (row.get(col) or "").strip() if col else ""

            for i, row in enumerate(reader, start=2):
                name = " ".join(_val(row, "magazine").split())  # extra spaces நீக்கம்
                if not name:
                    continue
                try:
                    upsert_vendor(
                        cur, name,
                        _val(row, "vendor_name"),
                        _val(row, "code"),
                        _val(row, "bank_account_number"),
                        _val(row, "bank_name"),
                        _val(row, "bank_place"),
                        _val(row, "ifsc_code"),
                        _val(row, "payee_name"),
                        _val(row, "email_id"),
                    )
                    imported += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Row {i} ({name}): {e}")
            conn.commit()
        return jsonify(
            {
                "success": True,
                "imported": imported,
                "errors": errors,
                "message": f"{imported} Vendor பதிவுகள் import ஆயின.",
            }
        )
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# 12) REPORTS — Quarter Summary / Magazine-wise / Payment Status /
#     Voucher Register / Email Status
# =============================================================================
@app.route("/api/reports/quarter-summary")
def api_report_quarter_summary():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT quarter, COUNT(*) AS magazines,
                       COALESCE(SUM(requested_amt),0) AS requested,
                       COALESCE(SUM(deduction),0) AS deduction,
                       COALESCE(SUM(paid_amt),0) AS paid
                FROM payments
                WHERE quarter IS NOT NULL AND quarter <> ''
                GROUP BY quarter ORDER BY quarter
                """
            )
            rows = cur.fetchall()
        result = [
            {
                "quarter": r["quarter"],
                "magazines": r["magazines"],
                "requested": float(r["requested"]),
                "deduction": float(r["deduction"]),
                "paid": float(r["paid"]),
            }
            for r in rows
        ]
        grand = {
            "magazines": sum(r["magazines"] for r in result),
            "requested": sum(r["requested"] for r in result),
            "deduction": sum(r["deduction"] for r in result),
            "paid": sum(r["paid"] for r in result),
        }
        return jsonify({"success": True, "rows": result, "grand": grand})
    finally:
        conn.close()


@app.route("/api/reports/magazine-wise")
def api_report_magazine_wise():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                # அந்த quarter-க்குப் பொருந்தும் இதழ்கள் முழுப் பட்டியலையும்
                # (magazine_quarters carry-forward வழியாக) எடுத்து, அதில் எந்தெந்த
                # இதழுக்கு அந்த quarter-ல் invoice பதிவு செய்யப்பட்டுள்ளது என்பதைப்
                # பொருத்திப் பார்க்கிறோம் — invoice பதிவே செய்யாத இதழுக்கு payments
                # அட்டவணையில் வரிசையே இருக்காது என்பதால், payments அட்டவணையை மட்டும்
                # வைத்து invoice வராதவற்றை கண்டுபிடிக்க முடியாது.
                expected_magazines, _ = magazines_and_master_for_quarter(cur, quarter)

                cur.execute(
                    "SELECT * FROM payments WHERE quarter=%s ORDER BY sno NULLS LAST, magazine",
                    (quarter,),
                )
                rows = cur.fetchall()
                invoiced_by_magazine = {
                    r["magazine"]: r for r in rows if (r["invoice_no"] or "").strip()
                }

                received, not_received = [], []
                for name in sorted(expected_magazines):
                    r = invoiced_by_magazine.get(name)
                    if r:
                        received.append(
                            {
                                "serial": len(received) + 1,
                                "magazine": name,
                                "invoiceNo": (r["invoice_no"] or "").strip(),
                                "invoiceDate": fmt_date(r["invoice_date"]),
                                "requestedAmt": float(r["requested_amt"] or 0),
                                "quarter": r["quarter"] or "",
                            }
                        )
                    else:
                        not_received.append({"serial": len(not_received) + 1, "magazine": name})

                return jsonify({"success": True, "received": received, "notReceived": not_received})

            # quarter தேர்வு செய்யாதபோது — எல்லா quarter-களின் payments பதிவுகளையும்
            # invoice உள்ளதா/இல்லையா என்பதன் அடிப்படையில் காட்டுகிறோம் (பழைய நடத்தை).
            cur.execute("SELECT * FROM payments ORDER BY quarter, sno NULLS LAST, magazine")
            rows = cur.fetchall()

        received, not_received = [], []
        for r in rows:
            invoice_no = (r["invoice_no"] or "").strip()
            if invoice_no:
                received.append(
                    {
                        "serial": len(received) + 1,
                        "magazine": r["magazine"],
                        "invoiceNo": invoice_no,
                        "invoiceDate": fmt_date(r["invoice_date"]),
                        "requestedAmt": float(r["requested_amt"] or 0),
                        "quarter": r["quarter"] or "",
                    }
                )
            else:
                not_received.append({"serial": len(not_received) + 1, "magazine": r["magazine"]})

        return jsonify({"success": True, "received": received, "notReceived": not_received})
    finally:
        conn.close()


@app.route("/api/reports/payment-status")
def api_report_payment_status():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                cur.execute(
                    "SELECT * FROM payments WHERE payment_date IS NOT NULL AND quarter=%s ORDER BY payment_date",
                    (quarter,),
                )
            else:
                cur.execute("SELECT * FROM payments WHERE payment_date IS NOT NULL ORDER BY payment_date")
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "magazine": r["magazine"],
                "quarter": r["quarter"] or "",
                "paidAmt": float(r["paid_amt"] or 0),
                "paidDate": fmt_date(r["payment_date"]),
                "transactionNo": r["transaction_no"] or "",
            }
            for i, r in enumerate(rows, start=1)
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


@app.route("/api/reports/voucher-register")
def api_report_voucher_register():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT v.*, p.voucher_no AS pay_voucher_no FROM vouchers v "
                "LEFT JOIN payments p ON p.magazine=v.magazine AND p.quarter=v.quarter ORDER BY v.id"
            )
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "paymentSNo": r["payment_sno"] or "",
                "voucherNo": r["pay_voucher_no"] or "",
                "magazine": r["magazine"] or "",
                "tnpftsCode": r["tnpfts_code"] or "",
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": fmt_date(r["invoice_date"]),
                "requestedAmt": float(r["requested_amt"] or 0),
                "deduction": float(r["deduction"] or 0),
                "amountPaid": float(r["amount_paid"] or 0),
                "quarter": r["quarter"] or "",
            }
            for i, r in enumerate(rows, start=1)
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


@app.route("/api/reports/email-status")
def api_report_email_status():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM payments WHERE mail_sent=TRUE OR (pdf_url IS NOT NULL AND pdf_url<>'') ORDER BY quarter, magazine"
            )
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "magazine": r["magazine"],
                "quarter": r["quarter"] or "",
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": fmt_date(r["invoice_date"]),
                "paidAmt": float(r["paid_amt"] or 0),
                "transactionNo": r["transaction_no"] or "",
                "paymentDate": fmt_date(r["payment_date"]),
                "mailSent": bool(r["mail_sent"]),
                "pdfUrl": r["pdf_url"] or "",
            }
            for i, r in enumerate(rows, start=1)
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


# =============================================================================
# 13) தொகை வித்தியாசம் — Net Payable vs Requested Amount Mismatch
# =============================================================================
@app.route("/api/reports/amount-mismatch")
def api_amount_mismatch():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments ORDER BY quarter, magazine")
            rows = cur.fetchall()
        result = []
        for r in rows:
            magazine = (r["magazine"] or "").strip()
            if not magazine:
                continue
            net_payable = float(r["net_payable"] or 0)
            requested_amt = float(r["requested_amt"] or 0)
            if net_payable == 0 and requested_amt == 0:
                continue
            if net_payable == requested_amt:
                continue
            difference = requested_amt - net_payable
            result.append(
                {
                    "sno": r["sno"] or "",
                    "magazine": magazine,
                    "quarter": r["quarter"] or "",
                    "invoiceNo": r["invoice_no"] or "",
                    "netPayable": net_payable,
                    "requestedAmt": requested_amt,
                    "difference": difference,
                    "status": "EXCESS" if difference > 0 else "SHORT",
                }
            )
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


# =============================================================================
# 14) Pending Invoice Reminder
# =============================================================================
def get_direct_master_magazines_for_quarter(cur, quarter):
    """அந்த Quarter-க்கே நேரடியாகச் சேர்க்கப்பட்ட (carry-forward அல்லாத) இதழ்களின் பெயர் set."""
    cur.execute(
        """
        SELECT m.name FROM magazine_quarters q
        JOIN magazines m ON m.id = q.magazine_id
        WHERE q.quarter = %s
        """,
        (quarter,),
    )
    return {r["name"] for r in cur.fetchall()}


def get_master_magazines_for_quarter(cur, quarter):
    """/api/magazines-ல் உள்ள carry-forward தர்க்கத்தையே பயன்படுத்தி, ஒரு quarter-க்கு
    பொருந்தும் இதழ்களின் பெயர் பட்டியலை மட்டும் தரும்."""
    cur.execute(
        """
        SELECT m.name, q.quarter
        FROM magazine_quarters q
        JOIN magazines m ON m.id = q.magazine_id
        """
    )
    rows = cur.fetchall()
    best = {}
    for r in rows:
        name, q = r["name"], r["quarter"]
        rank = 0 if q == quarter else (1 if q < quarter else 2)
        cur_best = best.get(name)
        if cur_best is None or rank < cur_best[0] or (rank == cur_best[0] and (
            (rank == 1 and q > cur_best[1]) or (rank == 2 and q < cur_best[1])
        )):
            best[name] = (rank, q)
    return sorted(best.keys())


@app.route("/api/reminders/pending-invoices")
def api_pending_invoices():
    quarter = request.args.get("quarter", "").strip()
    if not quarter:
        return jsonify({"success": False, "message": "Quarter தேர்வு செய்யவும்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            master_magazines = get_master_magazines_for_quarter(cur, quarter)

            cur.execute(
                "SELECT magazine FROM payments WHERE quarter=%s AND invoice_no IS NOT NULL AND invoice_no<>''",
                (quarter,),
            )
            has_invoice = {r["magazine"] for r in cur.fetchall()}

            cur.execute("SELECT name, email_id FROM magazines")
            email_map = {r["name"]: (r["email_id"] or "").strip() for r in cur.fetchall()}

        pending = [
            {"magazine": name, "quarter": quarter, "email": email_map.get(name, "")}
            for name in master_magazines
            if name not in has_invoice
        ]
        return jsonify({"success": True, "rows": pending})
    finally:
        conn.close()


@app.route("/api/reminders/send", methods=["POST"])
def api_send_reminders():
    payload = request.get_json(force=True)
    items = payload.get("items") or []
    if not items:
        return jsonify({"success": False, "message": "எந்த Magazine-ஐயும் தேர்வு செய்யவில்லை"}), 400

    success_count, failed = 0, []
    for item in items:
        magazine = item.get("magazine", "")
        email = (item.get("email") or "").strip()
        quarter = item.get("quarter", "")
        if not email:
            failed.append(f"{magazine}: Email இல்லை")
            continue
        period_text = quarter_period_text(quarter)
        subject = f"Request for Invoice Submission - {magazine} for {quarter}"
        body = f"""Dear Sir/Madam,<br><br>
We have not yet received the invoice for the supply of <strong>{magazine}</strong> for the
<strong>{quarter}</strong> (i.e., {period_text}).<br><br>
Kindly issue and send the invoice at the earliest so that we can process the payment without delay.<br><br>
Thank you for your kind cooperation.<br><br>
Regards,<br>District Library Officer<br>Dindigul"""
        try:
            send_email(email, subject, body)
            success_count += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{magazine}: {e}")

    message = f"{success_count} மெயில்கள் வெற்றிகரமாக அனுப்பப்பட்டன."
    if failed:
        message += f" ({len(failed)} தோல்வி)"
    return jsonify({"success": True, "sent": success_count, "errors": failed, "message": message})


# =============================================================================
# 15) Voucher Numbers Dashboard
# =============================================================================
@app.route("/api/vouchers/set-numbers")
def api_voucher_set_numbers():
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                cur.execute(
                    "SELECT DISTINCT bill_set_no, quarter FROM payments "
                    "WHERE bill_set_no IS NOT NULL AND bill_set_no<>'' AND quarter=%s",
                    (quarter,),
                )
            else:
                cur.execute(
                    "SELECT DISTINCT bill_set_no, quarter FROM payments "
                    "WHERE bill_set_no IS NOT NULL AND bill_set_no<>''"
                )
            rows = cur.fetchall()
        set_map = {}
        for r in rows:
            s = r["bill_set_no"]
            set_map.setdefault(s, set()).add(r["quarter"])

        def sort_key(s):
            try:
                return (0, int(s))
            except (TypeError, ValueError):
                return (1, s)

        result = [
            {"setNo": s, "quarters": sorted(qs)}
            for s, qs in sorted(set_map.items(), key=lambda kv: sort_key(kv[0]))
        ]
        return jsonify({"success": True, "sets": result})
    finally:
        conn.close()


@app.route("/api/vouchers/by-set")
def api_vouchers_by_set():
    set_no = request.args.get("setNo", "").strip()
    quarter = request.args.get("quarter", "").strip()
    if not set_no:
        return jsonify({"success": False, "message": "Set No தேவை"}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                cur.execute(
                    "SELECT id, magazine, paid_amt, voucher_no, quarter FROM payments "
                    "WHERE bill_set_no=%s AND quarter=%s ORDER BY magazine",
                    (set_no, quarter),
                )
            else:
                cur.execute(
                    "SELECT id, magazine, paid_amt, voucher_no, quarter FROM payments "
                    "WHERE bill_set_no=%s ORDER BY magazine",
                    (set_no,),
                )
            rows = cur.fetchall()
        result = [
            {
                "row": r["id"],
                "magazine": r["magazine"],
                "amountPaid": float(r["paid_amt"] or 0),
                "voucherNo": r["voucher_no"] or "",
                "quarter": r["quarter"] or "",
            }
            for r in rows
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


@app.route("/api/vouchers/save-numbers", methods=["POST"])
def api_save_voucher_numbers():
    updates = request.get_json(force=True) or []
    conn = get_conn()
    saved = 0
    try:
        with conn.cursor() as cur:
            for item in updates:
                if item.get("row") is not None and item.get("voucherNo") is not None:
                    cur.execute(
                        "UPDATE payments SET voucher_no=%s, updated_at=now() WHERE id=%s",
                        (str(item["voucherNo"]).strip(), item["row"]),
                    )
                    saved += 1
            conn.commit()
        return jsonify({"success": True, "message": f"{saved} வவுச்சர் நம்பர்கள் சேமிக்கப்பட்டன"})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/vouchers/all")
def api_all_vouchers():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT voucher_no, magazine, paid_amt, quarter, bill_set_no FROM payments "
                "WHERE voucher_no IS NOT NULL AND voucher_no<>''"
            )
            rows = cur.fetchall()

        def sort_key(r):
            v = r["voucher_no"] or ""
            m = re.match(r"^(\d+)", v)
            return (0, int(m.group(1)), v) if m else (1, 0, v)

        rows = sorted(rows, key=sort_key)
        result = [
            {
                "voucherNo": r["voucher_no"],
                "magazine": r["magazine"],
                "amountPaid": float(r["paid_amt"] or 0),
                "quarter": r["quarter"] or "",
                "setNo": r["bill_set_no"] or "",
            }
            for r in rows
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


# =============================================================================
# 15-b) Payment Advice — GAS "getPaymentAdviceData" / "savePaymentAdvicePDF" இதே தர்க்கம்
# =============================================================================
def get_payment_advice_data(cur, set_no, quarter):
    """ஒரு Set No + Quarter-க்கான Payment Advice வரிசைகளை உருவாக்கும்.
    GAS-ன் getPaymentAdviceData()-ஐ போலவே: Voucher No இல்லாத பதிவு இருந்தால் தடுக்கும்."""
    cur.execute(
        """
        SELECT p.magazine, p.voucher_no, p.invoice_no, p.invoice_date,
               p.requested_amt, p.paid_amt, p.quarter, m.tnpfts_code
        FROM payments p
        LEFT JOIN magazines m ON m.name = p.magazine
        WHERE p.bill_set_no = %s AND p.quarter = %s
        """,
        (set_no, quarter),
    )
    prows = cur.fetchall()

    if not prows:
        return {"success": False, "message": "இந்த Quarter / Set-ல் பதிவுகள் இல்லை"}

    no_voucher = [r["magazine"] for r in prows if not (r["voucher_no"] or "").strip()]
    if no_voucher:
        return {
            "success": False,
            "message": "முதலில் வவுச்சர் நம்பர் கொடுக்கவும் — "
            + str(len(no_voucher))
            + " இதழ்களுக்கு Voucher No இல்லை: "
            + ", ".join(no_voucher),
        }

    rows = []
    for r in prows:
        requested_amt = float(r["requested_amt"] or 0)
        paid_amt = float(r["paid_amt"] or 0)
        rows.append(
            {
                "voucherNo": (r["voucher_no"] or "").strip(),
                "magazine": r["magazine"],
                "tnpftsCode": r["tnpfts_code"] or "",
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": fmt_date(r["invoice_date"]),
                "requestedAmt": requested_amt,
                "deduction": requested_amt - paid_amt,
                "netPayable": paid_amt,
            }
        )

    def voucher_sort_key(r):
        v = r["voucherNo"]
        try:
            return (0, float(v), v)
        except ValueError:
            return (1, 0, v)

    rows.sort(key=voucher_sort_key)
    total_net = sum(r["netPayable"] for r in rows)

    return {
        "success": True,
        "setNo": set_no,
        "quarter": quarter,
        "totalRows": len(rows),
        "totalNet": total_net,
        "rows": rows,
    }


def amount_to_english_words(amount):
    """GAS-ன் numberToEnglishWords()-ஐ போலவே."""
    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen",
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def convert_hundreds(n):
        result = ""
        if n >= 100:
            result += ones[n // 100] + " Hundred "
            n %= 100
        if n >= 20:
            result += tens[n // 10] + " "
            n %= 10
        if n > 0:
            result += ones[n] + " "
        return result

    if amount == 0:
        return "Zero Rupees Only"
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    words = ""
    if rupees >= 10000000:
        words += convert_hundreds(rupees // 10000000) + "Crore "
    if rupees >= 100000:
        words += convert_hundreds((rupees % 10000000) // 100000) + "Lakh "
    if rupees >= 1000:
        words += convert_hundreds((rupees % 100000) // 1000) + "Thousand "
    words += convert_hundreds(rupees % 1000)
    words = words.strip() + " Rupees"
    if paise > 0:
        words += " and " + convert_hundreds(paise).strip() + " Paise"
    words += " Only"
    return re.sub(r"\s+", " ", words).strip()


TAMIL_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "NotoSansTamil-Regular.ttf")


def build_payment_advice_pdf(d):
    """Payment Advice PDF — fpdf2 + uharfbuzz (தமிழ் எழுத்துகள் சரியாக shaping ஆக).
    Letter landscape; அட்டவணை பக்க அகலம் முழுவதும்; பல பக்கம் ஆனால் header மீண்டும் வரும்."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    quarter_display = (d["quarter"] or "").replace("-Q", " Q")
    total_in_words = amount_to_english_words(d["totalNet"])

    pdf = FPDF(orientation="L", unit="mm", format="Letter")
    pdf.set_margins(12, 14, 15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("Tamil", "", TAMIL_FONT_PATH)
    pdf.set_text_shaping(True)
    pdf.add_page()

    pdf.set_font("Tamil", size=17)
    pdf.cell(0, 9, "திண்டுக்கல் மாவட்ட நூலக ஆணைக்குழு", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Tamil", size=12)
    pdf.cell(
        0, 8,
        f"{quarter_display} தொகை வழங்கல் — Set {d['setNo']}  |  மொத்தப் பட்டியல்கள்: {d['totalRows']}",
        align="C", new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(3)

    # பக்க அகலம் = 279.4 - 12 - 15 = 252.4 mm
    widths = (20, 22, 54, 28, 28, 27, 24, 24, 25.4)
    pdf.set_font("Tamil", size=10)
    head_style = FontFace(color=(255, 255, 255), fill_color=(15, 35, 71))
    with pdf.table(
        col_widths=widths,
        text_align=("CENTER", "CENTER", "LEFT", "CENTER", "CENTER", "CENTER", "RIGHT", "RIGHT", "RIGHT"),
        headings_style=head_style,
        line_height=7,
        padding=1.2,
        borders_layout="ALL",
    ) as table:
        h = table.row()
        for t in ["வ.எண்.", "வவுச்சர் எண்", "இதழ் பெயர்", "TNPFTS CODE", "பட்டியல் எண்",
                  "பட்டியல் நாள்", "கோரப்பட்ட தொகை", "பிடித்தம்", "நிகரத் தொகை"]:
            h.cell(t, align="C")
        for i, r in enumerate(d["rows"], start=1):
            row = table.row()
            row.cell(str(i))
            row.cell(str(r["voucherNo"]))
            row.cell(str(r["magazine"]))
            row.cell(str(r["tnpftsCode"] or "—"))
            row.cell(str(r["invoiceNo"] or "—"))
            row.cell(str(r["invoiceDate"] or "—"))
            row.cell(f"{r['requestedAmt']:.2f}")
            row.cell(f"{r['deduction']:.2f}")
            row.cell(f"{r['netPayable']:.2f}")
        tot = table.row()
        tot.cell("மொத்த நிகரத் தொகை :", colspan=8, align="R",
                 style=FontFace(fill_color=(232, 237, 245)))
        tot.cell(f"Rs. {d['totalNet']:.2f}", align="R", style=FontFace(fill_color=(232, 237, 245)))
        wr = table.row()
        wr.cell(f"Rupees in Words : {total_in_words}", colspan=9, align="L",
                style=FontFace(fill_color=(247, 249, 252)))

    pdf.ln(14)
    if pdf.get_y() > 175:
        pdf.add_page()
    pdf.set_font("Tamil", size=11)
    pdf.cell(0, 6, "மாவட்ட நூலக அலுவலர்", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "திண்டுக்கல்", align="R", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# =============================================================================
# 15-c) Payment Voucher (P.U. Form No. 33) — GAS "fillAndPrintPaymentVoucher" / Voucher_print sheet
#       மாதிரி PDF-ஐ (Legal portrait, புள்ளிக் கோடு கட்டங்கள்) அப்படியே மீண்டும் உருவாக்குகிறது
# =============================================================================
TAMIL_BOLD_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "NotoSansTamil-Bold.ttf")

TAMIL_MONTHS = ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "சூன்",
                "சூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"]

VOUCHER_MAX_ROWS = 10
VOUCHER_DEFAULT_FILE_NO = "999/இ1/2026"


def voucher_period_text(quarter):
    """'2026-2027-Q1' -> 'ஏப்ரல் 2026 முதல் சூன் 2026 வரை' (GAS periodMap-ஐப் போலவே)."""
    m = re.match(r"^(\d{4})-(\d{4})-Q([1-4])$", (quarter or "").strip())
    if not m:
        return ""
    y1, y2, q = int(m.group(1)), int(m.group(2)), int(m.group(3))
    spans = {1: (3, 5, y1, y1), 2: (6, 8, y1, y1), 3: (9, 11, y1, y1), 4: (0, 2, y2, y2)}
    a, b, ya, yb = spans[q]
    return f"{TAMIL_MONTHS[a]} {ya} முதல் {TAMIL_MONTHS[b]} {yb} வரை"


def voucher_fin_year(quarter):
    m = re.match(r"^(\d{4})-(\d{4})-Q[1-4]$", (quarter or "").strip())
    return f"{m.group(1)}-{m.group(2)[-2:]}" if m else ""


def indian_grouping(amount):
    """58367 -> '58,367'; 180405 -> '1,80,405'; 180405.5 -> '1,80,405.50'"""
    amount = round(float(amount), 2)
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return s + (f".{paise:02d}" if paise else "")


def amount_to_words_voucher(amount):
    """58367 -> 'Fifty Eight Thousand Three Hundred and Sixty Seven'  (Indian: Lakh / Crore)."""
    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
            "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen",
            "Eighteen", "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def below100(n):
        return ones[n] if n < 20 else (tens[n // 10] + (" " + ones[n % 10] if n % 10 else ""))

    def below1000(n):
        out = ""
        if n >= 100:
            out = ones[n // 100] + " Hundred"
            n %= 100
            if n:
                out += " and " + below100(n)
        elif n:
            out = below100(n)
        return out

    amount = round(float(amount), 2)
    rupees = int(amount)
    paise = round((amount - rupees) * 100)
    if rupees == 0 and paise == 0:
        return "Zero"
    parts = []
    crore, rem = divmod(rupees, 10000000)
    lakh, rem = divmod(rem, 100000)
    thousand, rem = divmod(rem, 1000)
    if crore:
        parts.append(below1000(crore) + " Crore")
    if lakh:
        parts.append(below100(lakh) + " Lakh")
    if thousand:
        parts.append(below100(thousand) + " Thousand")
    if rem:
        tail = below1000(rem)
        if parts and rem < 100:
            tail = "and " + tail
        parts.append(tail)
    words = " ".join(parts)
    if paise:
        words += (" and " if words else "") + below100(paise) + " Paise"
    return words.strip()


def _is_tamil_char(ch):
    return "\u0b80" <= ch <= "\u0bff"


def build_payment_voucher_pdf(d, file_no=None, doc_date=None):
    """Payment Voucher (P.U. Form No. 33) — Legal portrait, மாதிரிக்குச் சரியான ஆயத்தொலைவுகளில்."""
    from fpdf import FPDF

    file_no = (file_no or VOUCHER_DEFAULT_FILE_NO).strip()
    doc_date = doc_date or date.today()
    rows = d["rows"]
    total = sum(r["netPayable"] for r in rows)
    quarter = d["quarter"] or ""

    v_nos = [r["voucherNo"] for r in rows]
    if len(v_nos) == 1:
        v_range = f"Vouchers Numbers : {v_nos[0]}/{voucher_fin_year(quarter)}"
    else:
        v_range = f"Vouchers Numbers : {v_nos[0]} to {v_nos[-1]}/{voucher_fin_year(quarter)}"

    def fmt_amt(x):
        x = float(x)
        return str(int(x)) if x == int(x) else f"{x:.2f}"

    words_line = f"Rs.{indian_grouping(total)} (Rupees {amount_to_words_voucher(total)})"

    pdf = FPDF(unit="pt", format=(612, 1008))
    pdf.set_auto_page_break(False)
    pdf.set_margins(0, 0, 0)
    pdf.add_font("TamilR", "", TAMIL_FONT_PATH)
    pdf.add_font("TamilB", "", TAMIL_BOLD_FONT_PATH)
    pdf.set_text_shaping(True)
    pdf.add_page()
    TAMIL_SCALE = 0.925  # Noto Sans Tamil, மாதிரியின் Latha அகலத்துடன் பொருந்த

    # ---------- எழுத்து உதவிகள் ----------
    def run_font(is_tamil, bold, latin, italic=False):
        if is_tamil:
            return ("TamilB" if bold else "TamilR"), ""
        style = ("B" if bold else "") + ("I" if italic else "")
        return latin, style

    def split_runs(text):
        runs, cur, cur_t = [], "", None
        for ch in text:
            t = _is_tamil_char(ch)
            if ch in " " and cur_t is not None:
                t = cur_t          # இடைவெளி முந்தைய run-உடன் சேரும்
            if cur_t is None or t == cur_t:
                cur += ch
                cur_t = t if cur_t is None else cur_t
            else:
                runs.append((cur, cur_t))
                cur, cur_t = ch, t
        if cur:
            runs.append((cur, cur_t))
        return runs

    def text_width(text, size, bold=False, latin="Times", italic=False):
        w = 0.0
        for seg, is_t in split_runs(text):
            fam, st = run_font(is_t, bold, latin, italic)
            pdf.set_font(fam, st, size * (TAMIL_SCALE if is_t else 1))
            w += pdf.get_string_width(seg)
        return w

    def draw(x, y, text, size=8.9, bold=False, latin="Times", italic=False, color=(0, 0, 0)):
        # pdf.text() தமிழ் shaping செய்யாது; pdf.cell() செய்யும் — அதனால் cell வழியாக,
        # baseline = y என்று வரும்படி மேல் ஆயத்தொலைவை (y - 0.8*எழுத்தளவு) கணக்கிடுகிறோம்.
        pdf.set_text_color(*color)
        cx = x
        for seg, is_t in split_runs(text):
            fam, st = run_font(is_t, bold, latin, italic)
            fs = size * (TAMIL_SCALE if is_t else 1)
            pdf.set_font(fam, st, fs)
            w = pdf.get_string_width(seg)
            pdf.set_xy(cx, y - 0.8 * fs)
            pdf.cell(w + 0.5, fs, seg, border=0, new_x="RIGHT", new_y="TOP")
            cx += w
        pdf.set_text_color(0, 0, 0)

    def draw_c(cx, y, text, **kw):
        draw(cx - text_width(text, kw.get("size", 8.9), kw.get("bold", False),
                             kw.get("latin", "Times"), kw.get("italic", False)) / 2, y, text, **kw)

    def draw_r(rx, y, text, **kw):
        draw(rx - text_width(text, kw.get("size", 8.9), kw.get("bold", False),
                             kw.get("latin", "Times"), kw.get("italic", False)), y, text, **kw)

    def wrap(text, max_w, size, **kw):
        lines, cur = [], ""
        for word in text.split(" "):
            trial = (cur + " " + word).strip()
            if cur and text_width(trial, size, kw.get("bold", False), kw.get("latin", "Times"),
                                  kw.get("italic", False)) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    # ---------- புள்ளிக்கோடு கட்டங்கள் (மாதிரி PDF-ன் அதே ஆயத்தொலைவுகள்) ----------
    H = [(69.2, 96.8, 499.7), (101.3, 96.8, 499.7), (148.4, 96.8, 499.7), (175.6, 96.8, 499.7),
         (210.3, 155.6, 426.1), (242.5, 155.6, 426.1), (264.1, 155.6, 426.1), (284.5, 155.6, 426.1),
         (302.5, 155.6, 426.1), (320.4, 155.6, 426.1), (338.4, 155.6, 426.1), (356.3, 155.6, 499.7),
         (375.5, 155.6, 499.7), (393.5, 155.6, 426.1), (415.7, 155.6, 426.1), (437.4, 155.6, 426.1),
         (455.3, 155.6, 426.1), (474.5, 155.6, 426.1), (530.9, 96.8, 499.7), (563.0, 96.8, 499.7)]
    V = [(155.9, 68.9, 563.3), (172.6, 242.1, 455.7), (190.6, 242.1, 455.7), (225.9, 210.0, 242.8),
         (355.2, 263.8, 474.8), (378.7, 242.1, 474.8), (425.8, 68.9, 563.3), (499.4, 68.9, 563.3)]
    pdf.set_draw_color(0, 0, 0)
    pdf.set_line_width(0.62)
    pdf.set_dash_pattern(dash=1.238, gap=1.238)
    for y, x0, x1 in H:
        pdf.line(x0, y, x1, y)
    for x, y0, y1 in V:
        pdf.line(x, y0, x, y1)
    pdf.set_dash_pattern()

    # ---------- தலைப்புப் பகுதி ----------
    draw(99.3, 63.5, "P.U.Form No. 33", size=8.9, latin="Times")
    draw_c(290.85, 83.2, "BILL FOR CONTINGENT CHARGES OFFICE OF THE DISTRICT", size=8.0, bold=True, latin="Helvetica")
    draw_c(290.85, 92.5, "LIBRARY OFFICER,DINDIGUL", size=8.0, bold=True, latin="Helvetica")
    month_text = f"{TAMIL_MONTHS[doc_date.month - 1]} {doc_date.year}"
    draw_c(462.6, 79.3, "மாதம்:", size=8.9)
    draw_c(462.6, 94.0, month_text, size=8.9)

    draw_c(126.35, 111.2, "Head of", size=8.9)
    draw_c(126.35, 121.3, "Service", size=8.9)
    draw_c(289.4, 126.5, "பருவ இதழ்கள் வாங்குதல்", size=11.3, bold=True)
    for i, ln in enumerate(wrap(v_range, 72, 8.9, bold=True)[:3]):
        draw_c(462.6, 117.3 + 10.2 * i, ln, size=8.9, bold=True)

    draw_c(126.35, 158.2, "Nos.of sub", size=8.9)
    draw_c(126.35, 168.4, "Vouchers", size=8.9)
    draw_c(290.6, 158.2, "Description of Charges and No and date of Authority where Special", size=8.9, latin="Helvetica")
    draw_c(290.6, 168.4, "Sanction is nessary", size=8.9, latin="Helvetica")
    draw_c(462.2, 158.2, "Amount", size=8.9)
    draw(442.2, 171.8, "Rs.", size=8.9)
    draw(477.1, 171.8, "P", size=8.9)

    draw_c(289.0, 186.2, "நூலகங்களுக்கு பருவ இதழ் வாங்கியமைக்கான", size=8.9, bold=True)
    draw_c(289.0, 202.9, "சந்தாத் தொகை செலுத்துதல்", size=8.9, bold=True)

    draw(158.1, 227.3, "காலம்", size=8.0)
    period_line = f"{quarter.replace('-Q', ' Q')} ( {voucher_period_text(quarter)} )"
    plines = wrap(period_line, 196, 8.9, bold=True, latin="Helvetica")[:2]
    base0 = 220.0 if len(plines) == 2 else 227.3
    for i, ln in enumerate(plines):
        draw_c(325.85, base0 + 14.7 * i, ln, size=8.9, bold=True, latin="Helvetica")

    # கோப்பு எண் / நாள் — செங்குத்து எழுத்து
    vt1 = f"கோப்பு எண்.{file_no}"
    vt2 = f"நாள்:- {doc_date.strftime('%d-%m-%Y')}"
    for ox, txt in ((116.2, vt1), (127.9, vt2)):
        w = text_width(txt, 8.9)
        oy = 341.0 + w / 2
        with pdf.rotation(angle=90, x=ox, y=oy):
            draw(ox, oy, txt, size=8.9)

    # ---------- அட்டவணை தலைப்பு ----------
    blue = (17, 85, 204)
    for x, y, t, xe in ((158.1, 251.6, "S.", 165.7), (158.1, 260.9, "No", 168.4),
                        (174.8, 251.6, "Vr.", 184.3), (174.8, 260.9, "No", 185.1)):
        draw(x, y, t, size=8.0, latin="Helvetica", color=blue)
        pdf.set_draw_color(*blue)
        pdf.set_line_width(0.589)
        pdf.line(x, y + 0.9, xe, y + 0.9)
    pdf.set_draw_color(0, 0, 0)
    draw_c(284.6, 260.9, "Name of periodical", size=8.0, latin="Helvetica")
    draw(380.9, 260.9, "Amount", size=8.0, latin="Helvetica")

    # ---------- 10 வரிசைகள் ----------
    base_no = [277.1, 296.2, 314.2, 332.1, 350.1, 368.6, 387.2, 407.6, 429.3, 449.1]
    base_amt = [277.8, 296.9, 314.9, 332.8, 350.8, 369.4, 387.9, 407.8, 430.1, 449.9]
    base_rs = [275.7, 294.9, 312.9, 330.8, 348.8, 367.3, 385.9, 405.7, 428.0, 447.8]
    for i, r in enumerate(rows[:VOUCHER_MAX_ROWS]):
        name = str(r["magazine"])
        has_tamil = any(_is_tamil_char(c) for c in name)
        draw(158.1, base_no[i], str(i + 1), size=8.9, bold=True, latin="Helvetica")
        draw(174.8, base_no[i], str(r["voucherNo"]), size=8.9, bold=True, latin="Helvetica")
        draw(192.7, base_no[i] - (1.6 if has_tamil else 0), name, size=8.9, bold=True, latin="Times")
        draw(357.4, base_rs[i], "ரூ.", size=8.9, bold=True)
        draw_r(423.6, base_amt[i], fmt_amt(r["netPayable"]), size=9.7, bold=True, latin="Times")

    # மொத்தம் வரிசை
    draw_r(347.3, 466.6, "மொத்தம்", size=9.7, bold=True)
    draw(357.4, 467.1, "ரூ.", size=8.9, bold=True)
    draw_r(423.6, 470.9, fmt_amt(total), size=9.7, bold=True, latin="Times")

    # வலது பத்தியில் மொத்தத் தொகை (இரு இடங்களில்)
    draw_r(497.2, 367.5, "ரூ. " + fmt_amt(total), size=9.7, bold=True, latin="Helvetica")
    draw_r(497.2, 548.2, "ரூ. " + fmt_amt(total), size=9.7, bold=True, latin="Helvetica")

    # எழுத்தில் தொகை (ஆங்கிலம்) — கட்டத்தின் நடுவில்
    wl = wrap(words_line, 250, 8.9, bold=True, latin="Helvetica")[:2]
    if len(wl) == 1:
        draw_c(290.85, 551.0, wl[0], size=8.9, bold=True, latin="Helvetica")
    else:
        draw_c(290.85, 540.6, wl[0], size=8.9, bold=True, latin="Helvetica")
        draw_c(290.85, 555.3, wl[1], size=8.9, bold=True, latin="Helvetica")

    # ---------- கீழ்ப் பகுதி (மாதிரிப்படி அப்படியே) ----------
    para1 = [
        "          Recived Payment, I Certifity that the expenditure charged in this bill could not, with due regarded to the ",
        "interest of the public service be avoided and that, so for as I could as certain the rates allowed are reasonbale ",
        "and do not exceed local current rates.  I have satisfied myself that the charges entered in this bill have been ",
        "really paid or will be paid on receipt of the money drawn on this bill.  voucher for all sums above Rs. 25 in ",
        "amount and for all sums paid for postage stamps telegrams and house rents are attached to the bill save thouse ",
        "noted below which will be obtained as soon as the amounts have been paid.  i have as for posible obtained ",
        "voucher for other sums and i am personally reasonable that they been on defaced that they cannot be used again.",
    ]
    for i, ln in enumerate(para1):
        draw(99.3, 572.8 + 10.18 * i, ln, size=8.9, latin="Times")
    para2 = [
        "          Certified that the work truned out is satsfactory and is worth the amount paid for received the above ",
        "articles in good condition and entered in the stock register, quantities are correct and qualities are good and ",
        "suitable for the purpose.",
    ]
    for i, ln in enumerate(para2):
        draw(99.3, 647.0 + 10.15 * i, ln, size=8.9, latin="Times")

    draw(411.2, 728.2, "Head of Office", size=8.9, bold=True)
    draw(411.2, 743.7, "Countersigned", size=8.9, bold=True)
    draw(99.3, 759.2, "Station", size=8.9, bold=True, latin="Helvetica")
    draw(148.8, 759.1, ":", size=8.9)
    draw(158.1, 759.7, "திண்டுக்கல்", size=8.9)
    draw(213.8, 759.7, " - 624 003", size=8.9)
    draw(286.8, 759.1, "(Signature)", size=8.9, bold=True)
    draw(357.4, 759.1, ". . . . . . . . . . . . . . . . . . . . .", size=8.9, bold=True)
    draw(99.3, 777.1, "Date", size=8.9, bold=True, latin="Helvetica")
    draw(148.8, 777.1, ":     .06.2026", size=8.9)
    draw(286.8, 777.1, "(Designation)", size=8.9, bold=True)
    draw(357.4, 777.1, "District Library Officer", size=8.9, bold=True, latin="Helvetica")
    draw(286.8, 792.5, "(Date)", size=8.9, bold=True)
    draw(391.4, 792.6, "Dindigul", size=8.9, bold=True, latin="Helvetica")
    for y, label in ((808.1, "Allotment for 2025-2026"), (826.0, "Expendure including this bill"),
                     (844.0, "Balance available")):
        draw(99.3, y, label, size=8.9, latin="Helvetica")
        draw(230.5, y - 0.1, ":", size=8.9)
        draw(244.9, y + 0.5, "ரூ.", size=8.9, bold=True)

    passed = f"Passed for Rupees. Rs.{indian_grouping(total)}/-(Rupees {amount_to_words_voucher(total)})"
    pl = wrap(passed, 455, 11.5, bold=True, italic=True, latin="Times")[:2]
    for i, ln in enumerate(pl):
        draw(99.3, 863.0 + 14.0 * i, ln, size=11.5, bold=True, italic=True, latin="Times")

    draw(99.3, 894.1, "Head of Account", size=8.9, bold=True, latin="Helvetica")
    draw(204.5, 894.1, "Classification", size=8.9, bold=True, latin="Helvetica")
    draw(298.6, 894.1, "Accountant", size=8.9, bold=True, latin="Helvetica")
    draw(416.2, 894.1, "Commissioner", size=8.9, bold=True, latin="Helvetica")

    return bytes(pdf.output())


@app.route("/api/reports/payment-voucher")
def api_payment_voucher():
    set_no = request.args.get("setNo", "").strip()
    quarter = request.args.get("quarter", "").strip()
    if not set_no or not quarter:
        return jsonify({"success": False, "message": "Quarter மற்றும் Set No தேவை"}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            d = get_payment_advice_data(cur, set_no, quarter)
        if not d["success"]:
            return jsonify(d)
        if d["totalRows"] > VOUCHER_MAX_ROWS:
            return jsonify({
                "success": False,
                "message": f"இந்த Set-ல் {d['totalRows']} இதழ்கள் உள்ளன. ஒரு Payment Voucher-ல் அதிகபட்சம் {VOUCHER_MAX_ROWS} இதழ்கள் மட்டுமே இடம்பெறும் — Set-ஐ பிரித்துக்கொள்ளவும்.",
            })
        total = d["totalNet"]
        return jsonify({
            "success": True,
            "setNo": set_no,
            "quarter": quarter,
            "totalRows": d["totalRows"],
            "totalNet": total,
            "wordsLine": f"Rs.{indian_grouping(total)} (Rupees {amount_to_words_voucher(total)})",
            "voucherFrom": d["rows"][0]["voucherNo"],
            "voucherTo": d["rows"][-1]["voucherNo"],
            "finYear": voucher_fin_year(quarter),
            "periodText": voucher_period_text(quarter),
            "defaultFileNo": VOUCHER_DEFAULT_FILE_NO,
            "rows": d["rows"],
        })
    finally:
        conn.close()


@app.route("/api/reports/payment-voucher/pdf")
def api_payment_voucher_pdf():
    set_no = request.args.get("setNo", "").strip()
    quarter = request.args.get("quarter", "").strip()
    file_no = request.args.get("fileNo", "").strip() or VOUCHER_DEFAULT_FILE_NO
    date_str = request.args.get("date", "").strip()
    if not set_no or not quarter:
        return jsonify({"success": False, "message": "Quarter மற்றும் Set No தேவை"}), 400
    try:
        doc_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except ValueError:
        return jsonify({"success": False, "message": "தேதி வடிவம் தவறு"}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            d = get_payment_advice_data(cur, set_no, quarter)
        if not d["success"]:
            return jsonify(d), 400
        if d["totalRows"] > VOUCHER_MAX_ROWS:
            return jsonify({"success": False, "message": f"அதிகபட்சம் {VOUCHER_MAX_ROWS} இதழ்கள் மட்டுமே"}), 400
        pdf_bytes = build_payment_voucher_pdf(d, file_no, doc_date)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Payment_voucher-{set_no}.pdf",
        )
    finally:
        conn.close()


@app.route("/api/reports/payment-advice")
def api_payment_advice():
    set_no = request.args.get("setNo", "").strip()
    quarter = request.args.get("quarter", "").strip()
    if not set_no or not quarter:
        return jsonify({"success": False, "message": "Quarter மற்றும் Set No தேவை"}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            d = get_payment_advice_data(cur, set_no, quarter)
        return jsonify(d)
    finally:
        conn.close()


@app.route("/api/reports/payment-advice/pdf")
def api_payment_advice_pdf():
    set_no = request.args.get("setNo", "").strip()
    quarter = request.args.get("quarter", "").strip()
    if not set_no or not quarter:
        return jsonify({"success": False, "message": "Quarter மற்றும் Set No தேவை"}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            d = get_payment_advice_data(cur, set_no, quarter)
        if not d["success"]:
            return jsonify(d), 400
        pdf_bytes = build_payment_advice_pdf(d)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"Payment_Advice_Set_{set_no}.pdf",
        )
    finally:
        conn.close()
@app.route("/api/mail/ready")
def api_mail_ready():
    """மெயில் அனுப்பத் தயாரான பட்டியல் — ஒரே Vendor/Beneficiary Code (அதே Quarter) உள்ள இதழ்கள் ஒரே வரியாக.
    குழுவில் தொகை வழங்காத / Transaction No பதிவாகாத இதழ் இருந்தால் blocked=true."""
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT p.id, p.magazine, p.quarter, p.voucher_no, p.transaction_no, p.payment_date, p.bill_set_no, "
                "p.requested_amt, p.paid_amt, m.email_id, m.tnpfts_code, m.vendor_name, m.payee_name "
                "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
                "WHERE p.transaction_no IS NOT NULL AND p.transaction_no<>'' "
                "AND p.payment_date IS NOT NULL AND p.mail_sent = FALSE"
            )
            params = ()
            if quarter:
                sql += " AND p.quarter=%s"
                params = (quarter,)
            cur.execute(sql, params)
            rows = cur.fetchall()
            blockers = fetch_group_blockers(cur, quarter or None)

        groups = {}
        for r in rows:
            code = norm_code(r["tnpfts_code"])
            key = group_key_for(r) + ((r["transaction_no"] or "").strip(),)   # ஒரே Transaction No மட்டும் ஒரே மெயில்
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "key": "|".join(key),
                    "quarter": r["quarter"] or "",
                    "code": (r["tnpfts_code"] or "").strip(),
                    "billSetNo": (r["bill_set_no"] or "").strip(),
                    "vendorName": vendor_display_name(r["vendor_name"], r["payee_name"], r["magazine"]),
                    "emails": [],
                    "items": [],
                    "totalBill": 0.0,
                    "totalNet": 0.0,
                    "_bk": blockers.get((r["quarter"], code)) if code else None,
                }
            em = (r["email_id"] or "").strip()
            if em and em not in g["emails"]:
                g["emails"].append(em)
            bill, net = float(r["requested_amt"] or 0), float(r["paid_amt"] or 0)
            g["items"].append({
                "row": r["id"], "voucherNo": r["voucher_no"] or "", "magazine": r["magazine"],
                "billAmount": bill, "netAmount": net,
                "transactionNo": r["transaction_no"] or "", "paymentDate": fmt_date(r["payment_date"]),
            })
            g["totalBill"] += bill
            g["totalNet"] += net

        out = []
        for g in groups.values():
            if not g["emails"]:
                continue  # மெயில் ID இல்லாத குழு — முன்பும் பட்டியலில் வராது
            bk = g.pop("_bk")
            reasons = []
            in_group = len(g["items"]) > 1
            if bk and bk["pending"] and in_group:
                reasons.append("இன்னும் தொகை வழங்காதவை: " + ", ".join(bk["pending"]))
            same_set_missing = (bk["missingTxnBySet"].get(g["billSetNo"]) if bk and g["billSetNo"] else None)
            if same_set_missing:
                reasons.append("இதே Set-ல் Transaction No பதிவாகாதவை (தனி மெயிலாகச் செல்லும்): " + ", ".join(same_set_missing))
            g["blocked"] = False          # இனி தடை இல்லை — எச்சரிக்கை மட்டும்
            g["warning"] = " | ".join(reasons)
            g["blockReason"] = g["warning"]
            g["items"].sort(key=lambda it: (voucher_sort_key(it["voucherNo"]), it["magazine"]))
            g["rows"] = [it["row"] for it in g["items"]]
            g["email"] = g["emails"][0]
            out.append(g)
        out.sort(key=lambda g: (g["quarter"], voucher_sort_key(g["items"][0]["voucherNo"]), g["vendorName"]))
        return jsonify({"success": True, "groups": out})
    finally:
        conn.close()


@app.route("/api/mail/send", methods=["POST"])
def api_mail_send():
    """ஒரு Vendor/Beneficiary Code குழுவுக்கு ஒரே மெயில் + ஒரே PDF. `rows` (அல்லது பழைய `row`) — குழுவின்
    ஏதாவது ஒரு payment id; குழுவிலுள்ள மெயிலுக்குத் தயாரான அனைத்து இதழ்களும் சேர்த்து அனுப்பப்படும்."""
    payload = request.get_json(force=True) or {}
    ids = payload.get("rows") or ([payload.get("row")] if payload.get("row") else [])
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        ids = []
    if not ids:
        return jsonify({"success": False, "message": "Payment row தேவை"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            members, blockers = fetch_mail_group(cur, ids[0])
            if members is None:
                return jsonify({"success": False, "message": "Payment record கிடைக்கவில்லை"}), 404
            member_ids = {m["id"] for m in members}
            if ids[0] not in member_ids:
                return jsonify({"success": False, "message": "இந்த இதழுக்கு ஏற்கனவே மெயில் அனுப்பப்பட்டுள்ளது அல்லது Transaction No பதிவாகவில்லை"}), 400
            if not set(ids) <= member_ids:
                return jsonify({"success": False, "message": "தேர்ந்தெடுத்த இதழ்கள் ஒரே Vendor Code குழுவைச் சேர்ந்தவை அல்ல"}), 400
            email = next((m["email"] for m in members if m["email"]), "")
            names = ", ".join(m["magazine"] for m in members)
            if not email:
                return jsonify({"success": False, "message": f"'{names}'-க்கு Email இல்லை. Master Data → Vendors-ல் சேர்க்கவும்."}), 400

            quarter = members[0]["quarter"]
            pdf_bytes = html_to_pdf_bytes(build_group_intimation_html(members))
            if len(members) == 1:
                m0 = members[0]
                subject = f"Magazine Payment - {m0['magazine']} - {quarter}"
                body = f"""Hello,<br><br>
The payment for your magazine "{m0['magazine']}" has been completed.<br>
Please find the PDF attached.<br><br>Thank you."""
                attachment_name = f"{m0['magazine']} - {quarter}.pdf"
                label = m0["magazine"]
            else:
                vendor = members[0]["vendorName"]
                total = sum(m["netAmount"] for m in members)
                subject = f"Magazine Payment - {vendor} ({len(members)} magazines) - {quarter}"
                items_html = "".join(f"<li>{m['magazine']}</li>" for m in members)
                body = f"""Hello,<br><br>
The payment of Rs.{indian_grouping(total)} for the following magazines has been completed as a single transfer:
<ul>{items_html}</ul>
Please find the PDF attached with the break-up for each magazine.<br><br>Thank you."""
                attachment_name = f"{vendor} - {quarter}.pdf".replace("/", "-")
                label = f"{vendor} ({len(members)} இதழ்கள்)"

            send_email(email, subject, body, pdf_bytes, attachment_name)

            pdf_url = f"/api/mail/pdf/{members[0]['id']}"
            cur.execute(
                "UPDATE payments SET mail_sent=TRUE, pdf_url=%s, updated_at=now() WHERE id = ANY(%s)",
                (pdf_url, [m["id"] for m in members]),
            )
            conn.commit()
        return jsonify({"success": True, "count": len(members), "message": f"{label} → மெயில் + PDF அனுப்பப்பட்டது", "pdfUrl": pdf_url})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/mail/pdf/<int:payment_id>")
def api_mail_pdf(payment_id):
    """Drive-ல் சேமிக்காமல், தேவைப்படும்போது PDF-ஐ மீண்டும் உருவாக்கி காட்டும்/பதிவிறக்கும்.
    ஒரே மெயிலில் அனுப்பப்பட்ட குழுவின் இதழ்கள் அனைத்தும் (அதே pdf_url) ஒரே PDF-ல் வரும்."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pdf_url FROM payments WHERE id=%s", (payment_id,))
            r0 = cur.fetchone()
            ids = [payment_id]
            if r0 and r0["pdf_url"]:
                cur.execute("SELECT id FROM payments WHERE pdf_url=%s", (r0["pdf_url"],))
                ids = [r["id"] for r in cur.fetchall()] or ids
            members = [fetch_payment_for_mail(cur, i) for i in ids]
            members = [m for m in members if m]
        if not members:
            return jsonify({"success": False, "message": "Payment record கிடைக்கவில்லை"}), 404
        members.sort(key=lambda m: (voucher_sort_key(m["voucherNo"]), m["magazine"]))
        pdf_bytes = html_to_pdf_bytes(build_group_intimation_html(members))
        base = members[0]["magazine"] if len(members) == 1 else members[0]["vendorName"]
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{base} - {members[0]['quarter']}.pdf".replace("/", "-"),
        )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
