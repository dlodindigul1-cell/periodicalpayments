"""
பருவ இதழ்கள் தொகை செலுத்துதல் - 2026-27
Flask backend — Neon Postgres-ஐ பயன்படுத்தி GAS system-ஐ replace செய்யும்
Phase 1: Master data + Payment entry + Duplicate check + Quarter view +
         Payment processing + Transaction number tracking
(PDF உருவாக்கம் / Email அனுப்புதல் / Reports — Phase 2-ல் சேர்க்கப்படும்)
"""

import os
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
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()


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
            cur.execute("SELECT * FROM magazines ORDER BY name")
            rows = cur.fetchall()

        magazines = []
        master = {}
        for r in rows:
            eff = (r["effective_quarters"] or "").strip()
            if quarter:
                parts = [p.strip() for p in eff.split(",")] if eff else []
                if quarter not in parts:
                    continue
            magazines.append(r["name"])
            master[r["name"]] = {
                "issuePrice": float(r["issue_price"] or 0),
                "noOfLibraries": int(r["no_of_libraries"] or 0),
                "periodicity": r["periodicity"] or "",
                "effectiveQuarters": eff,
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
        if not row:
            return jsonify(
                {"issuePrice": 0, "totalIssues": 0, "requestedAmt": 0, "paymentDate": None, "billSetNo": ""}
            )
        return jsonify(
            {
                "issuePrice": float(row["issue_price"] or 0),
                "totalIssues": int(row["total_issues"] or 0),
                "requestedAmt": float(row["requested_amt"] or 0),
                "paymentDate": row["payment_date"].strftime("%d/%m/%Y") if row["payment_date"] else None,
                "billSetNo": row["bill_set_no"] or "",
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


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
