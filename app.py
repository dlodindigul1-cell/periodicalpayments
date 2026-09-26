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


def build_payment_intimation_html(p):
    """generatePaymentIntimationPDF (GAS) — English intimation letter, PDF-க்கு தயார்."""
    bank = p.get("bank") or {}
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: A4; margin: 25mm 18mm; }}
  body {{ font-family: Helvetica, Arial, sans-serif; color:#111; font-size:12px; }}
  h2 {{ text-align:center; border-bottom:3px solid #000; padding-bottom:8px; font-size:20px; margin-bottom:4px; }}
  h3 {{ text-align:center; font-size:14px; margin-top:6px; font-weight:600; }}
  table {{ width:100%; border-collapse:collapse; margin:16px 0; }}
  th,td {{ border:1px solid #000; padding:6px; font-size:11px; }}
  th {{ background:#1e4d8c; color:#fff; }}
  .right {{ text-align:right; }}
  .ctr {{ text-align:center; }}
</style></head>
<body>
  <div class="right">Date: {p.get('paymentDate') or '---'}</div>
  <h2>District Library Office, Dindigul</h2>
  <h3>PAYMENT CLEARED INTIMATION FOR {p.get('quarter') or '---'}</h3>
  <p>Sir,</p>
  <p>Ref: Your Invoice Number <strong>{p.get('invoiceNo') or '---'}</strong> dated
     <strong>{p.get('invoiceDate') or '---'}</strong> for the supply of Magazine
     <strong>{p.get('magazine')}</strong></p>
  <p>Sir,<br>Kindly see below the details of the payment transferred to your Bank Account from
     <strong>The District Library Officer, Dindigul</strong>, for the supply of the magazine
     <strong>{p.get('magazine')}</strong>, as per the invoice under reference cited.
     We kindly request you to acknowledge receipt of the same.</p>
  <table>
    <tr><th>S.No</th><th>Magazine</th><th>Qty</th><th>Bill Amt</th><th>Deduction</th>
        <th>Paid Amt</th><th>Date</th><th>Ref</th></tr>
    <tr>
      <td class="ctr">1</td><td>{p.get('magazine')}</td>
      <td class="ctr">{p.get('supplyQty') or 0}</td>
      <td class="right">{p.get('billAmount') or 0}</td>
      <td class="right">{p.get('deduction') or 0}</td>
      <td class="right">{p.get('netAmount') or 0}</td>
      <td class="ctr">{p.get('paymentDate') or '---'}</td>
      <td class="ctr">{p.get('transactionNo') or '---'}</td>
    </tr>
  </table>
  <p><strong>Remarks:</strong> {p.get('quarter')} SETTLED{(' | ' + p['remarks']) if p.get('remarks') else ''}</p>
  <p>Please send back the Acknowledgement receipt.</p>
  <p><strong>Bank Details:</strong></p>
  <table>
    <tr><td>PAYEE NAME</td><td>{bank.get('payeeName') or '---'}</td></tr>
    <tr><td>BANK NAME</td><td>{bank.get('bankName') or '---'}</td></tr>
    <tr><td>BRANCH</td><td>{bank.get('branch') or '---'}</td></tr>
    <tr><td>A/C NO</td><td>{bank.get('accNo') or '---'}</td></tr>
    <tr><td>IFSC</td><td>{bank.get('ifsc') or '---'}</td></tr>
  </table>
  <br><br>
  <p class="right">Thanking and Regards,<br>District Library Officer<br>Dindigul</p>
</body></html>"""


def fetch_payment_for_mail(cur, payment_id):
    """ஒரு payments.id-க்கான, மெயில்/PDF-க்குத் தேவையான தகவல்கள் அனைத்தையும் ஒரே dict-ஆக எடுக்கும்."""
    cur.execute(
        """
        SELECT p.*, m.email_id, m.payee_name, m.bank_name, m.bank_place,
               m.bank_account_number, m.ifsc_code
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
        if not row:
            return jsonify(
                {
                    "issuePrice": 0, "totalIssues": 0, "requestedAmt": 0, "paymentDate": None, "billSetNo": "",
                    "vendorCode": vendor_code, "vendorType": classify_vendor_code(vendor_code),
                }
            )
        return jsonify(
            {
                "issuePrice": float(row["issue_price"] or 0),
                "totalIssues": int(row["total_issues"] or 0),
                "requestedAmt": float(row["requested_amt"] or 0),
                "paymentDate": row["payment_date"].strftime("%d/%m/%Y") if row["payment_date"] else None,
                "billSetNo": row["bill_set_no"] or "",
                "vendorCode": vendor_code,
                "vendorType": classify_vendor_code(vendor_code),
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
    amount_now_paid = q_num(data.get("amountNowPaid"))
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
    quarter = request.args.get("quarter") or None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if quarter:
                cur.execute(
                    """SELECT id, voucher_no, magazine, paid_amt, payment_date
                       FROM payments
                       WHERE payment_date IS NOT NULL AND (transaction_no IS NULL OR transaction_no='')
                         AND quarter=%s
                       ORDER BY sno""",
                    (quarter,),
                )
            else:
                cur.execute(
                    """SELECT id, voucher_no, magazine, paid_amt, payment_date
                       FROM payments
                       WHERE payment_date IS NOT NULL AND (transaction_no IS NULL OR transaction_no='')
                       ORDER BY sno"""
                )
            rows = cur.fetchall()
        result = [
            {
                "row": r["id"],
                "sno": r["voucher_no"] or "",
                "magazine": r["magazine"],
                "amountPaid": float(r["paid_amt"] or 0),
                "paymentDate": r["payment_date"].strftime("%d-%m-%Y") if r["payment_date"] else "",
            }
            for r in rows
        ]
        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/payments/transactions", methods=["POST"])
def api_save_transactions():
    updates = request.get_json(force=True)  # [{row, transactionNo}]
    conn = get_conn()
    updated, errors = 0, []
    try:
        with conn.cursor() as cur:
            for item in updates:
                try:
                    cur.execute(
                        "UPDATE payments SET transaction_no=%s, updated_at=now() WHERE id=%s",
                        (str(item.get("transactionNo", "")).strip(), item.get("row")),
                    )
                    updated += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Row {item.get('row')}: {e}")
            conn.commit()
        if errors:
            return jsonify({"success": False, "updated": updated, "errors": errors, "message": "சில row-களில் பிழை ஏற்பட்டது"})
        return jsonify({"success": True, "updated": updated, "message": f"{updated} பதிவுகள் வெற்றிகரமாக புதுப்பிக்கப்பட்டன"})
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
                       COUNT(q.id) AS quarter_count,
                       MAX(q.quarter) AS latest_quarter
                FROM magazines m
                LEFT JOIN magazine_quarters q ON q.magazine_id = m.id
                GROUP BY m.id, m.name, m.language, m.periodicity
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
                        "latestQuarter": r["latest_quarter"] or "",
                    }
                    for r in rows
                ],
            }
        )
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
            cur.execute("SELECT * FROM vouchers ORDER BY id")
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "paymentSNo": r["payment_sno"] or "",
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


def build_payment_advice_html(d):
    """GAS-ன் savePaymentAdvicePDF()-ல் இருந்த HTML/CSS அப்படியே."""
    quarter_display = (d["quarter"] or "").replace("-Q", " Q")
    total_in_words = amount_to_english_words(d["totalNet"])

    rows_html = ""
    for i, r in enumerate(d["rows"], start=1):
        rows_html += f"""
      <tr>
        <td class="ctr">{i}</td>
        <td class="ctr bold">{r['voucherNo']}</td>
        <td class="mag">{r['magazine']}</td>
        <td class="ctr">{r['tnpftsCode'] or '—'}</td>
        <td class="invno">{r['invoiceNo'] or '—'}</td>
        <td class="ctr">{r['invoiceDate'] or '—'}</td>
        <td class="amt">{r['requestedAmt']:.2f}</td>
        <td class="amt">{r['deduction']:.2f}</td>
        <td class="amt bold">{r['netPayable']:.2f}</td>
      </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
  @page {{ size: Letter landscape; margin: 15mm 15mm 15mm 12mm; }}
  body  {{ font-family: Arial, sans-serif; color: #111; font-size: 11px; margin:0; }}
  h2   {{ text-align:center; font-size:16px; font-weight:700; margin-bottom:4px; }}
  h3   {{ text-align:center; font-size:13px; font-weight:600; margin-bottom:12px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:0; }}
  thead tr th {{
    background:#0f2347; color:#fff; padding:8px 5px; font-size:12px; font-weight:900;
    border:2px solid #000; text-align:center; letter-spacing:0.3px;
  }}
  td {{ border:2px solid #555; padding:6px 5px; vertical-align:middle; }}
  .ctr  {{ text-align:center; }}
  .amt  {{ text-align:right; }}
  .bold {{ font-weight:700; }}
  .mag  {{ word-wrap:break-word; max-width:130px; width:130px; font-size:13px; }}
  .invno {{ word-wrap:break-word; max-width:62px; width:62px; text-align:center; }}
  .total-row td {{ background:#e8edf5; font-weight:700; border-top:3px solid #0f2347; border-bottom:3px solid #0f2347; }}
  .words-row td {{ border:2px solid #555; padding:6px 8px; font-size:11px; font-style:italic; background:#f7f9fc; }}
  .sig-block {{ margin-top:36px; text-align:right; }}
  .sig-line {{ display:inline-block; text-align:center; border-top:1.5px solid #111; padding-top:6px; min-width:180px; font-size:11.5px; }}
</style></head>
<body>
  <h2>திண்டுக்கல் மாவட்ட நூலக ஆணைக்குழு</h2>
  <h3>{quarter_display} தொகை வழங்கல் — Set {d['setNo']} &nbsp;|&nbsp; மொத்தப் பட்டியல்கள்: {d['totalRows']}</h3>
  <table>
    <thead>
      <tr>
        <th style="width:28px;">வ.எண்.</th>
        <th style="width:52px;">வவுச்சர் எண்</th>
        <th style="width:130px;">இதழ் பெயர்</th>
        <th style="width:70px;">TNPFTS CODE</th>
        <th style="width:62px;">பட்டியல் எண்</th>
        <th style="width:66px;">பட்டியல் நாள்</th>
        <th style="width:62px;">கோரப்பட்ட தொகை</th>
        <th style="width:62px;">பிடித்தம்</th>
        <th style="width:70px;">நிகரத் தொகை</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
      <tr class="total-row">
        <td colspan="8" style="text-align:right; font-size:12px; padding-right:8px;">மொத்த நிகரத் தொகை :</td>
        <td class="amt" style="font-size:13px;">₹ {d['totalNet']:.2f}</td>
      </tr>
      <tr class="words-row">
        <td colspan="9"><strong>Rupees in Words :</strong> {total_in_words}</td>
      </tr>
    </tbody>
  </table>
  <br><br>
  <div class="sig-block">
    <div class="sig-line">
      <div style="font-weight:700;">மாவட்ட நூலக அலுவலர்</div>
      <div>திண்டுக்கல்</div>
    </div>
  </div>
</body></html>"""


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
        pdf_bytes = html_to_pdf_bytes(build_payment_advice_html(d))
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
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            base = (
                "SELECT p.id, p.magazine, p.quarter, p.transaction_no, p.payment_date, "
                "p.requested_amt, p.paid_amt, m.email_id "
                "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
                "WHERE p.transaction_no IS NOT NULL AND p.transaction_no<>'' "
                "AND p.payment_date IS NOT NULL AND p.mail_sent = FALSE "
                "AND m.email_id IS NOT NULL AND m.email_id <> ''"
            )
            if quarter:
                cur.execute(base + " AND p.quarter=%s ORDER BY p.magazine", (quarter,))
            else:
                cur.execute(base + " ORDER BY p.quarter, p.magazine")
            rows = cur.fetchall()
        result = [
            {
                "row": r["id"],
                "magazine": r["magazine"],
                "quarter": r["quarter"] or "",
                "transactionNo": r["transaction_no"] or "",
                "paymentDate": fmt_date(r["payment_date"]),
                "billAmount": float(r["requested_amt"] or 0),
                "netAmount": float(r["paid_amt"] or 0),
                "email": r["email_id"] or "",
            }
            for r in rows
        ]
        return jsonify({"success": True, "rows": result})
    finally:
        conn.close()


@app.route("/api/mail/send", methods=["POST"])
def api_mail_send():
    payload = request.get_json(force=True)
    payment_id = payload.get("row")
    if not payment_id:
        return jsonify({"success": False, "message": "Payment row தேவை"}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            p = fetch_payment_for_mail(cur, payment_id)
            if not p:
                return jsonify({"success": False, "message": "Payment record கிடைக்கவில்லை"}), 404
            if not p["email"]:
                return jsonify({"success": False, "message": f"'{p['magazine']}'-க்கு Email இல்லை. Master Data → Vendors-ல் சேர்க்கவும்."}), 400

            pdf_bytes = html_to_pdf_bytes(build_payment_intimation_html(p))
            subject = f"Magazine Payment - {p['magazine']} - {p['quarter']}"
            body = f"""Hello,<br><br>
The payment for your magazine "{p['magazine']}" has been completed.<br>
Please find the PDF attached.<br><br>Thank you."""
            attachment_name = f"{p['magazine']} - {p['quarter']}.pdf"

            send_email(p["email"], subject, body, pdf_bytes, attachment_name)

            pdf_url = f"/api/mail/pdf/{payment_id}"
            cur.execute(
                "UPDATE payments SET mail_sent=TRUE, pdf_url=%s, updated_at=now() WHERE id=%s",
                (pdf_url, payment_id),
            )
            conn.commit()
        return jsonify({"success": True, "message": f"{p['magazine']} → மெயில் + PDF அனுப்பப்பட்டது", "pdfUrl": pdf_url})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/mail/pdf/<int:payment_id>")
def api_mail_pdf(payment_id):
    """Drive-ல் சேமிக்காமல், தேவைப்படும்போது PDF-ஐ மீண்டும் உருவாக்கி காட்டும்/பதிவிறக்கும்."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            p = fetch_payment_for_mail(cur, payment_id)
        if not p:
            return jsonify({"success": False, "message": "Payment record கிடைக்கவில்லை"}), 404
        pdf_bytes = html_to_pdf_bytes(build_payment_intimation_html(p))
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=False,
            download_name=f"{p['magazine']} - {p['quarter']}.pdf",
        )
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
