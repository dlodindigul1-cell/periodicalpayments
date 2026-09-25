"""
பருவ இதழ்கள் தொகை செலுத்துதல் - 2026-27
Flask backend — Neon Postgres-ஐ பயன்படுத்தி GAS system-ஐ replace செய்யும்
Phase 1: Master data + Payment entry + Duplicate check + Quarter view +
         Payment processing + Transaction number tracking
(PDF உருவாக்கம் / Email அனுப்புதல் / Reports — Phase 2-ல் சேர்க்கப்படும்)
"""

import csv
import io
import os
import re
from datetime import date, datetime
from functools import wraps

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, Response

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
    இடைவெளிகள், எழுத்து அளவு வேறுபாடு ஆகியவற்றை நீக்கி normalize செய்யும்."""
    h = (h or "").replace("\ufeff", "").replace("\xa0", " ")
    return " ".join(h.split()).upper()


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


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
                # இதழ் ஒவ்வொன்றுக்கும் உள்ள Quarter விலைகள் அனைத்தும் எடுக்கிறோம் —
                # தேர்ந்தெடுத்த quarter-க்கு exact விலை இருந்தால் அதை; இல்லையெனில்
                # அதற்கு முந்தைய மிக அண்மைய quarter-ன் விலையை carry-forward செய்வோம்.
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
            else:
                # quarter குறிப்பிடாவிட்டால் — அனைத்து இதழ்களும் (price 0-உடன்)
                cur.execute(
                    "SELECT name, periodicity, language, NULL AS issue_price, "
                    "NULL AS price, NULL AS discount, NULL AS no_of_libraries "
                    "FROM magazines ORDER BY name"
                )
                rows = cur.fetchall()

        magazines = []
        master = {}

        if quarter:
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
        else:
            for r in rows:
                magazines.append(r["name"])
                master[r["name"]] = {
                    "issuePrice": float(r["issue_price"] or 0),
                    "noOfLibraries": int(r["no_of_libraries"] or 0),
                    "periodicity": r["periodicity"] or "",
                    "language": r["language"] or "",
                    "price": float(r["price"] or 0),
                    "discount": float(r["discount"] or 0),
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
_VENDOR_CSV_HEADERS = {
    "magazine": "NAME OF MAGAZINE",
    "vendor_name": "VENDOR",
    "code": "CODE",
    "bank_account_number": "BANK ACCOUNT NUMBER",
    "bank_name": "BANK NAME",
    "bank_place": "BANK PLACE",
    "ifsc_code": "IFSC CODE",
    "payee_name": "NAME OF PAYEE",
    "email_id": "EMAIL ID",
}


def upsert_vendor(cur, name, vendor_name, code, bank_account_number, bank_name, bank_place,
                   ifsc_code, payee_name, email_id):
    """இதழ் பெயரால் UPSERT — இதழ் இன்னும் magazines-ல் இல்லையெனில் Vendor விவரம் மட்டுமே கொண்ட
    ஒரு புதிய row உருவாகும் (மற்ற master விவரங்கள் பின்னால் சேர்க்கலாம்)."""
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
        (name, vendor_name, code, bank_account_number, bank_name, bank_place, ifsc_code, payee_name, email_id),
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

    required = {"NAME OF MAGAZINE", "CODE"}
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
    imported, errors = 0, []
    try:
        with conn.cursor() as cur:
            for i, row in enumerate(reader, start=2):
                name = (row.get(_VENDOR_CSV_HEADERS["magazine"]) or "").strip()
                if not name:
                    continue
                try:
                    upsert_vendor(
                        cur, name,
                        (row.get(_VENDOR_CSV_HEADERS["vendor_name"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["code"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["bank_account_number"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["bank_name"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["bank_place"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["ifsc_code"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["payee_name"]) or "").strip(),
                        (row.get(_VENDOR_CSV_HEADERS["email_id"]) or "").strip(),
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


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
