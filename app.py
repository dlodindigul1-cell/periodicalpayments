"""
பருவ இதழ்கள் தொகை செலுத்துதல் - 2026-27
Flask backend — Neon Postgres-ஐ பயன்படுத்தி GAS system-ஐ replace செய்யும்
Phase 1: Master data + Payment entry + Duplicate check + Quarter view +
         Payment processing + Transaction number tracking
(PDF உருவாக்கம் / Email அனுப்புதல் / Reports — Phase 2-ல் சேர்க்கப்படும்)
"""

import base64
import csv
import hashlib
import time
import io
import json
import os
import re
import smtplib
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, render_template_string, request, Response, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from xhtml2pdf import pisa

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# --------------------------------------------------------------------------- #
# உள் நுழைவு (Login card + Session) — பழைய Basic Auth popup நீக்கப்பட்டது
#   • பயனர்கள் DB-யின் app_users table-ல் (hash செய்யப்பட்ட password) சேமிக்கப்படுகிறார்கள்
#   • முதல் முறை இரு பயனர்கள் தானாக உருவாகும்: admin (role=admin), Section (role=section)
#   • Render-ல் SECRET_KEY environment variable அமைக்கவும் (இல்லையெனில் DATABASE_URL-ல் இருந்து பெறப்படும்)
#   • Master Data / /api/admin/* → role=admin மட்டும் (இதழ்/Vendor படிக்கும் vendors-list தவிர)
# --------------------------------------------------------------------------- #
INITIAL_USERS = (("admin", "admin"), ("Section", "section"))
INITIAL_PASSWORD = os.environ.get("INITIAL_PASSWORD", "dlodgl@789")

app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("periodicalpayments|" + (DATABASE_URL or "dev")).encode("utf-8")
).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),   # Render = https
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

_auth_ready = False


def ensure_auth_tables():
    """app_users table இல்லையெனில் உருவாக்கி, காலியாக இருந்தால் admin & Section பயனர்களை சேர்க்கும்."""
    global _auth_ready
    if _auth_ready:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS app_users ("
                " id SERIAL PRIMARY KEY,"
                " username TEXT NOT NULL,"
                " password_hash TEXT NOT NULL,"
                " role TEXT NOT NULL DEFAULT 'section',"
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_app_users_username ON app_users (LOWER(username))")
            cur.execute("SELECT COUNT(*) AS n FROM app_users")
            if cur.fetchone()["n"] == 0:
                for uname, role in INITIAL_USERS:
                    cur.execute(
                        "INSERT INTO app_users (username, password_hash, role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (uname, generate_password_hash(INITIAL_PASSWORD), role),
                    )
        conn.commit()
    finally:
        conn.close()
    _auth_ready = True


_part_ready = False


def ensure_part_schema():
    """payments table-ல் Part (ஒரே Quarter-க்கு பல இன்வாய்ஸ்) ஆதரவு.
    - part  : 1,2,3… (பழைய பதிவுகள் எல்லாம் தானாக 1)
    - months: NULL = Quarter முழுமை; இல்லையெனில் '1,2' (Quarter-ன் 1-வது,2-வது மாதம்)
    - (magazine, quarter) UNIQUE -> (magazine, quarter, part) UNIQUE
    மீண்டும் மீண்டும் இயக்கினாலும் பாதுகாப்பானது (idempotent)."""
    global _part_ready
    if _part_ready:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS part INTEGER NOT NULL DEFAULT 1")
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS months TEXT")
            # படி 3: vouchers வரிசையும் எந்த Part-க்குரியது என்பதை அறிய (பழையவை எல்லாம் Part 1)
            cur.execute("ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS part INTEGER NOT NULL DEFAULT 1")
            # பழைய UNIQUE (magazine, quarter) constraint / index-ஐ கண்டறிந்து நீக்கு
            cur.execute(
                """
                SELECT c.conname
                FROM pg_constraint c
                WHERE c.conrelid = 'payments'::regclass AND c.contype = 'u'
                  AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
                       FROM pg_attribute a
                       WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey))
                      = ARRAY['magazine','quarter']::text[]
                """
            )
            for r in cur.fetchall():
                cur.execute('ALTER TABLE payments DROP CONSTRAINT "%s"' % r["conname"].replace('"', '""'))
            cur.execute(
                """
                SELECT i.relname AS idx
                FROM pg_index x
                JOIN pg_class i ON i.oid = x.indexrelid
                WHERE x.indrelid = 'payments'::regclass AND x.indisunique AND NOT x.indisprimary
                  AND x.indnatts = 2
                  AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
                       FROM pg_attribute a
                       WHERE a.attrelid = x.indrelid AND a.attnum = ANY(x.indkey))
                      = ARRAY['magazine','quarter']::text[]
                """
            )
            for r in cur.fetchall():
                cur.execute('DROP INDEX IF EXISTS "%s"' % r["idx"].replace('"', '""'))
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_mag_qtr_part "
                "ON payments (magazine, quarter, part)"
            )
        conn.commit()
    finally:
        conn.close()
    _part_ready = True


# --- Quarter / மாதம் / Part உதவிகள் ------------------------------------------------
# நிதியாண்டு: Q1=Apr–Jun, Q2=Jul–Sep, Q3=Oct–Dec, Q4=Jan–Mar
_QUARTER_MONTHS = {"Q1": ("Apr", "May", "Jun"), "Q2": ("Jul", "Aug", "Sep"),
                   "Q3": ("Oct", "Nov", "Dec"), "Q4": ("Jan", "Feb", "Mar")}
# மாத வாரியாகப் பிரித்து அனுப்ப அர்த்தமுள்ள இதழ் வகைகள்
MONTH_TICK_PERIODICITIES = {"monthly", "fortnightly", "weekly", "triweekly"}


def quarter_month_names(quarter):
    m = re.search(r"-(Q[1-4])$", quarter or "")
    return list(_QUARTER_MONTHS[m.group(1)]) if m else ["M1", "M2", "M3"]


def uses_month_tick(periodicity):
    return (periodicity or "").strip().lower() in MONTH_TICK_PERIODICITIES


def parse_months(v):
    """'1,2' / [1,2] / None -> [1,2] போன்ற பட்டியல். None/காலி = Quarter முழுமை [1,2,3]."""
    if v is None or v == "" or v == []:
        return [1, 2, 3]
    items = v.split(",") if isinstance(v, str) else v
    out = set()
    for x in items:
        try:
            n = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 3:
            out.add(n)
    return sorted(out) or [1, 2, 3]


def fetch_quarter_parts(cur, magazine, quarter):
    cur.execute(
        "SELECT id, part, invoice_no, invoice_date, requested_amt, qtr_issues, non_supply, "
        "months, paid_amt FROM payments WHERE magazine=%s AND quarter=%s ORDER BY part",
        (magazine, quarter),
    )
    return cur.fetchall()


def compute_part_non_supply(cur, magazine, quarter, months, other_rows, payload_ns):
    """Quarter அளவு Non-supply ஒரே ஒரு முறை மட்டும் கழிக்கப்படும்:
    - ஒரே முழு-Quarter இன்வாய்ஸ் (முன்பு போல்)  -> payload மதிப்பு
    - Quarter முழுமையாகும் Part                  -> Quarter Non-supply − முந்தைய Part-களில் கழித்தது
    - Quarter இன்னும் முழுமையடையாத Part          -> 0"""
    covered = set(months)
    for o in other_rows:
        covered |= set(parse_months(o["months"]))
    if not other_rows and covered == {1, 2, 3}:
        return payload_ns
    if covered != {1, 2, 3}:
        return 0
    cur.execute("SELECT non_supply FROM despatch_nonsupply WHERE quarter=%s AND magazine=%s", (quarter, magazine))
    r = cur.fetchone()
    ns_q = q_int(r["non_supply"]) if r else payload_ns
    already = sum(q_int(o["non_supply"]) for o in other_rows)
    return max(ns_q - already, 0)


def _part_arg(v):
    n = q_int(v, 1)
    return n if n >= 1 else 1


def month_span_text(quarter, months_db):
    """'1,2' -> 'Jul–Aug' ; '1,3' -> 'Jul, Sep' ; NULL -> 'Jul–Sep'."""
    names = quarter_month_names(quarter)
    ms = parse_months(months_db)
    groups, cur_g = [], [ms[0]]
    for m in ms[1:]:
        if m == cur_g[-1] + 1:
            cur_g.append(m)
        else:
            groups.append(cur_g)
            cur_g = [m]
    groups.append(cur_g)
    return ", ".join(names[g[0] - 1] if len(g) == 1 else names[g[0] - 1] + "–" + names[g[-1] - 1] for g in groups)


def part_label(magazine, part, months_db, quarter):
    """Part 1 + முழு Quarter -> இதழ் பெயர் மட்டும் (பழைய தோற்றம்). இல்லையெனில் 'இதழ் (Part 2 · Aug–Sep)'."""
    part = part or 1
    ms = parse_months(months_db)
    if part == 1 and ms == [1, 2, 3]:
        return magazine
    if ms == [1, 2, 3]:
        return "%s (Part %d)" % (magazine, part)
    return "%s (Part %d · %s)" % (magazine, part, month_span_text(quarter, months_db))


def row_label(r):
    """payments வரிசை (magazine, part, months, quarter உள்ளது) -> காட்சிப் பெயர்."""
    return part_label(r["magazine"], r["part"], r["months"], r["quarter"])


def missing_months(rows):
    """payments வரிசைகளின் months-ல் இன்னும் வராத மாதங்கள் (1,2,3-ல்)."""
    used = set()
    for r in rows:
        used |= set(parse_months(r["months"]))
    return [m for m in (1, 2, 3) if m not in used]


def rebalance_non_supply(cur, magazine, quarter):
    """Quarter Non-supply ஒரே முறை மட்டும் கழிவதை உறுதி செய்யும் (மாதத் திருத்தத்துக்குப் பின் / Part மாற்றத்துக்குப் பின்).
    - Quarter முழுமையாக்கும் முதல் Part (Part எண் வரிசையில்) = Quarter Non-supply − தொகை வழங்கிய மற்ற Part-களின் கழிவு
    - மற்ற Part-கள் = 0
    - தொகை வழங்கிய (paid) Part-கள் தொடப்படாது. despatch தரவு இல்லையெனில் எதுவும் மாறாது.
    திருப்புவது: மாற்றப்பட்ட Part எண்களின் பட்டியல்."""
    cur.execute("SELECT non_supply FROM despatch_nonsupply WHERE quarter=%s AND magazine=%s", (quarter, magazine))
    d = cur.fetchone()
    if not d:
        return []
    ns_q = q_int(d["non_supply"])
    cur.execute(
        "SELECT id, part, months, non_supply, issue_price, actual_cost, payment_date, paid_amt "
        "FROM payments WHERE magazine=%s AND quarter=%s ORDER BY part", (magazine, quarter))
    rows = cur.fetchall()
    if len(rows) <= 1 and all(parse_months(r["months"]) == [1, 2, 3] for r in rows):
        return []      # பழைய முறை (ஒரே முழு-Quarter இன்வாய்ஸ்) — எதுவும் மாற்றப்படாது
    cover, owner = set(), None
    for r in rows:
        cover |= set(parse_months(r["months"]))
        if cover == {1, 2, 3} and owner is None:
            owner = r["part"]
    is_paid = lambda r: r["payment_date"] is not None or float(r["paid_amt"] or 0) > 0
    paid_other = sum(q_int(r["non_supply"]) for r in rows if is_paid(r) and r["part"] != owner)
    changed = []
    for r in rows:
        if is_paid(r):
            continue
        want = max(ns_q - paid_other, 0) if (owner is not None and r["part"] == owner) else 0
        if want != q_int(r["non_supply"]):
            price = float(r["issue_price"] or 0)
            ded = price * want
            cur.execute(
                "UPDATE payments SET non_supply=%s, deduction=%s, net_payable=%s, updated_at=now() WHERE id=%s",
                (want, ded, float(r["actual_cost"] or 0) - ded, r["id"]))
            changed.append(r["part"])
    return changed


_LOGIN_FAILS = {}   # ip -> [எண்ணிக்கை, கடைசி நேரம்]


def _client_ip():
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "")


def _login_blocked(ip):
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return False
    if time.time() - rec[1] > 600:
        _LOGIN_FAILS.pop(ip, None)
        return False
    return rec[0] >= 5


def _login_failed(ip):
    rec = _LOGIN_FAILS.get(ip)
    if not rec or time.time() - rec[1] > 600:
        rec = [0, 0]
    rec[0] += 1
    rec[1] = time.time()
    _LOGIN_FAILS[ip] = rec


def current_user():
    return session.get("user")


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>உள் நுழைவு — பருவ இதழ்கள் தொகை செலுத்துதல்</title>
<style>
  *{box-sizing:border-box} body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:linear-gradient(135deg,#0f2347,#1e4d8c);font-family:"Noto Sans Tamil","Segoe UI",Arial,sans-serif;padding:16px}
  .card{background:#fff;width:100%;max-width:400px;border-radius:18px;padding:32px 28px;box-shadow:0 20px 50px rgba(0,0,0,.35)}
  .emb{font-size:44px;text-align:center} h1{font-size:19px;text-align:center;color:#0f2347;margin:8px 0 2px}
  p.sub{text-align:center;color:#6b7690;font-size:13px;margin:0 0 22px}
  label{display:block;font-size:13px;font-weight:600;color:#1e4d8c;margin:14px 0 6px}
  input{width:100%;padding:12px 14px;border:1.5px solid #d5dcec;border-radius:10px;font-size:15px;outline:none}
  input:focus{border-color:#1e4d8c}
  button{width:100%;margin-top:22px;padding:13px;border:0;border-radius:10px;background:#1e4d8c;color:#fff;font-size:16px;font-weight:700;cursor:pointer}
  button:hover{background:#0f2347}
  .err{background:#fdecea;color:#a12b20;border-radius:10px;padding:10px 12px;font-size:13.5px;margin-top:14px}
</style></head><body>
<form class="card" method="post" action="/login" autocomplete="on">
  <div class="emb">📚</div>
  <h1>பருவ இதழ்கள் தொகை செலுத்துதல்</h1>
  <p class="sub">மாவட்ட நூலக அலுவலகம், திண்டுக்கல்</p>
  <label for="u">Username</label>
  <input id="u" name="username" type="text" autocomplete="username" required autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password" required>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <button type="submit">🔐 உள் நுழை</button>
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        ip = _client_ip()
        if _login_blocked(ip):
            error = "அதிக முறை தவறான முயற்சி. 10 நிமிடம் கழித்து மீண்டும் முயலவும்."
        else:
            uname = (request.form.get("username") or "").strip()
            pw = request.form.get("password") or ""
            row = None
            try:
                ensure_auth_tables()
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT * FROM app_users WHERE LOWER(username)=LOWER(%s)", (uname,))
                        row = cur.fetchone()
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001
                return render_template_string(LOGIN_HTML, error="Database இணைப்பு பிழை: " + str(e)), 503
            if row and check_password_hash(row["password_hash"], pw):
                session.clear()
                session.permanent = True
                session["user"] = {"id": row["id"], "username": row["username"], "role": row["role"]}
                _LOGIN_FAILS.pop(ip, None)
                return redirect("/")
            _login_failed(ip)
            error = "Username அல்லது Password தவறானது."
    elif current_user():
        return redirect("/")
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/api/me")
def api_me():
    u = current_user() or {}
    return jsonify({"success": True, "username": u.get("username", ""), "role": u.get("role", "")})


_PUBLIC_PATHS = {"/healthz", "/login", "/logout"}


@app.before_request
def global_auth():
    path = request.path
    if path in _PUBLIC_PATHS or path.startswith("/static/"):
        return
    try:
        ensure_auth_tables()
        ensure_part_schema()
    except Exception as e:  # noqa: BLE001
        return Response("Database இணைப்பு பிழை: " + str(e), 503)
    user = current_user()
    if not user:
        if path.startswith("/api/"):
            return jsonify({"success": False, "needLogin": True,
                            "message": "மீண்டும் உள் நுழையவும் (Session முடிந்தது)"}), 401
        return redirect("/login")
    # Master / Admin API-கள் — admin மட்டும் (வங்கி விவரப் பக்கம் படிக்கும் vendors-list தவிர)
    if path.startswith("/api/admin/") and user.get("role") != "admin":
        if not (path == "/api/admin/vendors-list" and request.method == "GET"):
            return jsonify({"success": False, "message": "இந்த வசதி Admin-க்கு மட்டுமே."}), 403


@app.route("/api/admin/users")
def api_admin_users():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username, role FROM app_users ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, id")
            rows = cur.fetchall()
        return jsonify({"success": True, "users": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/admin/update-user", methods=["POST"])
def api_admin_update_user():
    """{id, username, newPassword?} — Username மாற்றம் + (விருப்பம்) புதிய Password. DB-ல் hash ஆகச் சேமிக்கப்படும்."""
    data = request.get_json(force=True) or {}
    uid = q_int(data.get("id"), 0)
    username = (data.get("username") or "").strip()
    new_pw = data.get("newPassword") or ""
    if not uid:
        return jsonify({"success": False, "message": "பயனர் தேர்வு தவறு."}), 400
    if len(username) < 3 or re.search(r"\s", username):
        return jsonify({"success": False, "message": "Username குறைந்தது 3 எழுத்து; இடைவெளி கூடாது."}), 400
    if new_pw and len(new_pw) < 6:
        return jsonify({"success": False, "message": "Password குறைந்தது 6 எழுத்துகள் இருக்க வேண்டும்."}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, role FROM app_users WHERE id=%s", (uid,))
            target = cur.fetchone()
            if not target:
                return jsonify({"success": False, "message": "பயனர் இல்லை."}), 404
            cur.execute("SELECT id FROM app_users WHERE LOWER(username)=LOWER(%s) AND id<>%s", (username, uid))
            if cur.fetchone():
                return jsonify({"success": False, "message": "இந்த Username ஏற்கனவே உள்ளது."}), 400
            if new_pw:
                cur.execute(
                    "UPDATE app_users SET username=%s, password_hash=%s, updated_at=now() WHERE id=%s",
                    (username, generate_password_hash(new_pw), uid),
                )
            else:
                cur.execute("UPDATE app_users SET username=%s, updated_at=now() WHERE id=%s", (username, uid))
        conn.commit()
        me = current_user()
        if me and me.get("id") == uid:
            session["user"] = {**me, "username": username}
        msg = "✅ Username / Password மாற்றப்பட்டது." if new_pw else "✅ Username மாற்றப்பட்டது."
        return jsonify({"success": True, "message": msg})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


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


def vendor_mix_allowed(quarter):
    """2025-2026 Q1, Q2, Q3 (பழைய Quarter-கள்) — ஒரே Bill Set-ல் BENEFICIARY + BUSINESS VENDOR கலக்கலாம்.
    2025-2026 Q4 முதல் (அதற்குப் பின்) கலக்கக்கூடாது."""
    m = re.match(r"^(\d{4})-(\d{4})-Q([1-4])$", (quarter or "").strip())
    if not m:
        return False
    return (int(m.group(1)), int(m.group(3))) < (2025, 4)


def norm_code(code):
    """Vendor/Beneficiary Code ஒப்பீட்டுக்கு: இடைவெளி நீக்கி, பெரிய எழுத்தாக்கும்."""
    return (code or "").strip().upper()


def voucher_sort_key(v):
    """Voucher No எண் மதிப்பின்படி வரிசைப்படுத்த (2, 10, 11 — '10' < '2' என்ற எழுத்து வரிசை அல்ல).
    Voucher No இல்லாதவை கடைசியில்."""
    v = (v or "").strip()
    m = re.match(r"^(\d+)", v)
    return (0, int(m.group(1)), v) if m else (1, 0, v)


def _qlist(quarter):
    """None / "" / "Q1" / "Q1,Q2" / ["Q1","Q2"]  ->  ["Q1","Q2"] (வரிசைப்படுத்தி, நகல் நீக்கி)."""
    if not quarter:
        return []
    items = [quarter] if isinstance(quarter, str) else list(quarter)
    out = set()
    for it in items:
        for part in str(it or "").split(","):
            part = part.strip()
            if part:
                out.add(part)
    return sorted(out)


def get_quarters_arg():
    """?quarter=Q1&quarter=Q2 அல்லது ?quarter=Q1,Q2 (அல்லது ?quarters=...) -> ["Q1","Q2"].  காலி = அனைத்தும்."""
    vals = request.args.getlist("quarter") + request.args.getlist("quarters")
    return _qlist(vals)


def quarter_label(q):
    return (q or "").replace("-Q", " Q")


def fetch_group_blockers(cur, quarter=None):
    """(quarter, CODE) -> {'pending': [...], 'missingTxn': [...]}
    pending    = அதே Code + அதே Quarter-ல் Invoice பதிவாகி, இன்னும் தொகை வழங்காத இதழ்கள்
    missingTxn = தொகை வழங்கியும் Bank Transaction No பதிவாகாத இதழ்கள்"""
    sql = (
        "SELECT p.magazine, p.part, p.months, p.quarter, p.payment_date, p.bill_set_no, m.tnpfts_code "
        "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
        "WHERE (p.payment_date IS NULL OR p.transaction_no IS NULL OR p.transaction_no = '')"
    )
    params = ()
    ql = _qlist(quarter)
    if ql:
        sql += " AND p.quarter = ANY(%s)"
        params = (ql,)
    cur.execute(sql, params)
    out = {}
    for r in cur.fetchall():
        code = norm_code(r["tnpfts_code"])
        if not code:
            continue
        d = out.setdefault((r["quarter"], code), {"pending": [], "missingTxn": [], "missingTxnBySet": {}})
        lbl = row_label(r)
        if r["payment_date"] is None:
            d["pending"].append(lbl)
        else:
            d["missingTxn"].append(lbl)
            s = (r["bill_set_no"] or "").strip()
            d["missingTxnBySet"].setdefault(s, []).append(lbl)
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


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")


def parse_email_list(value):
    """ஒரு பெட்டியில் காற்புள்ளி / semicolon / இடைவெளி / புதிய வரியால் பிரிக்கப்பட்ட பல முகவரிகளைப்
    (அல்லது முகவரிப் பட்டியலை) தனித்தனியாகப் பிரித்து, நகல் நீக்கி (வரிசை மாறாமல்) தரும்.
    திருப்புவது: (valid_list, invalid_list)"""
    if value is None:
        return [], []
    parts = []
    for v in (value if isinstance(value, (list, tuple, set)) else [value]):
        parts += re.split(r"[,;\s]+", str(v or ""))
    valid, invalid, seen = [], [], set()
    for a in parts:
        a = a.strip().strip("<>").strip()
        if not a:
            continue
        key = a.lower()
        if key in seen:
            continue
        seen.add(key)
        (valid if _EMAIL_RE.match(a) else invalid).append(a)
    return valid, invalid


def send_email(to_addr, subject, html_body, attachment_bytes=None, attachment_name=None):
    """MAIL_PROVIDER env var-ஐ பொருத்து SMTP அல்லது Brevo HTTP API வழியாக மெயில் அனுப்பும்.
    to_addr: ஒரு முகவரி, காற்புள்ளியால் பிரித்த பல முகவரிகள், அல்லது பட்டியல் — எல்லாவற்றுக்கும் ஒரே மெயில்."""
    to_list, bad = parse_email_list(to_addr)
    if bad:
        raise RuntimeError("தவறான Email முகவரி: " + ", ".join(bad) + " — Master Data → Vendors-ல் திருத்தவும்.")
    if not to_list:
        raise RuntimeError("Email முகவரி இல்லை")
    to_addr = to_list
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
    msg["To"] = ", ".join(to_addr)
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
            refused = server.sendmail(mail_from, list(to_addr), msg.as_string())
            if refused:
                raise RuntimeError("SMTP சர்வர் இந்த முகவரிகளை ஏற்கவில்லை: " + ", ".join(refused))
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
        "to": [{"email": a} for a in to_addr],
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
        "magazine": row_label(r),
        "part": r["part"],
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
    """இதழ் + Quarter-க்கு ஏற்கனவே உள்ள Part-கள், பயன்படுத்தப்பட்ட மாதங்கள், அடுத்த Part எண்."""
    magazine = request.args.get("magazine", "").strip()
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = fetch_quarter_parts(cur, magazine, quarter)
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (magazine,))
            mrow = cur.fetchone()
            cur.execute("SELECT non_supply FROM despatch_nonsupply WHERE quarter=%s AND magazine=%s", (quarter, magazine))
            nsrow = cur.fetchone()
        month_tick = uses_month_tick(mrow["periodicity"] if mrow else "")
        names = quarter_month_names(quarter)
        used, parts = set(), []
        for r in rows:
            ms = parse_months(r["months"])
            used |= set(ms)
            parts.append({
                "id": r["id"], "part": r["part"], "months": ms,
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": r["invoice_date"].strftime("%d/%m/%Y") if r["invoice_date"] else "",
                "invoiceDateISO": r["invoice_date"].isoformat() if r["invoice_date"] else "",
                "requestedAmt": float(r["requested_amt"] or 0),
                "qtrIssues": r["qtr_issues"] or 0,
                "paid": float(r["paid_amt"] or 0) > 0,
            })
        first = parts[0] if parts else {}
        return jsonify({
            "exists": bool(parts),
            "parts": parts,
            "usedMonths": sorted(used),
            "complete": used == {1, 2, 3},
            "nextPart": (max(p["part"] for p in parts) + 1) if parts else 1,
            "monthTick": month_tick,
            "monthNames": names,
            "quarterNonSupply": q_int(nsrow["non_supply"]) if nsrow else 0,
            # பழைய பதிவுப் புலங்கள் (முதல் Part) — பின்னோக்கு இணக்கத்துக்காக
            "invoiceNo": first.get("invoiceNo", ""),
            "invoiceDate": first.get("invoiceDate", ""),
            "invoiceDateISO": first.get("invoiceDateISO", ""),
            "requestedAmt": first.get("requestedAmt", 0),
        })
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 3) addPayment (add / update) — Part ஆதரவுடன்
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
    payload_ns = q_int(payload.get("nonSupply"))
    req_part = q_int(payload.get("part"), 0)

    total_issues = libraries * qtr_issues
    actual_cost = issue_price * total_issues

    invoice_date = payload.get("invoiceDate") or None
    requested_amt = q_num(payload.get("requestedAmt"))
    invoice_no = payload.get("invoiceNo")

    full_q = payload.get("fullQuarter")
    full_q = True if full_q is None else bool(full_q)

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (magazine,))
            mrow = cur.fetchone()
            month_tick = uses_month_tick(mrow["periodicity"] if mrow else "")
            if not month_tick:
                full_q = True
            names = quarter_month_names(quarter)

            if full_q:
                months = [1, 2, 3]
            else:
                months = sorted({m for m in (q_int(x) for x in (payload.get("months") or [])) if 1 <= m <= 3})
                if not months:
                    return jsonify({"success": False, "message": "குறைந்தது ஒரு மாதத்தைத் தேர்ந்தெடுக்கவும்."}), 400
            months_db = None if months == [1, 2, 3] else ",".join(str(m) for m in months)

            rows = fetch_quarter_parts(cur, magazine, quarter)
            if is_update:
                part = req_part or 1
                if not any(r["part"] == part for r in rows):
                    conn.rollback()
                    return jsonify({"success": False, "message": "Update செய்ய record கிடைக்கவில்லை."}), 404
                others = [r for r in rows if r["part"] != part]
            else:
                part = (max(r["part"] for r in rows) + 1) if rows else 1
                others = rows

            used = set()
            for o in others:
                used |= set(parse_months(o["months"]))
            overlap = sorted(used & set(months))
            if overlap:
                if not month_tick:
                    msg = "'%s' (%s) பதிவு ஏற்கனவே உள்ளது." % (magazine, quarter)
                else:
                    msg = ("இந்த மாதங்கள் (%s) ஏற்கனவே மற்றொரு Part-ல் பதிவாகியுள்ளன. "
                           "மீதமுள்ள மாதங்களை மட்டும் தேர்ந்தெடுக்கவும்." % ", ".join(names[m - 1] for m in overlap))
                return jsonify({"success": False, "message": msg}), 409

            non_supply = compute_part_non_supply(cur, magazine, quarter, months, others, payload_ns)
            deduction = issue_price * non_supply
            net_payable = actual_cost - deduction
            complete = (used | set(months)) == {1, 2, 3}

            if is_update:
                cur.execute(
                    """
                    UPDATE payments SET
                        issue_price=%s, subscriptions=%s, qtr_issues=%s, total_issues=%s,
                        actual_cost=%s, non_supply=%s, deduction=%s, net_payable=%s,
                        invoice_no=%s, invoice_date=%s, requested_amt=%s, months=%s, updated_at=now()
                    WHERE magazine=%s AND quarter=%s AND part=%s
                    RETURNING id
                    """,
                    (
                        issue_price, libraries, qtr_issues, total_issues,
                        actual_cost, non_supply, deduction, net_payable,
                        invoice_no, invoice_date, requested_amt, months_db,
                        magazine, quarter, part,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                return jsonify({"success": True, "row": row["id"], "part": part, "complete": complete,
                                "nonSupply": non_supply, "message": "Updated"})
            else:
                cur.execute("SELECT COALESCE(MAX(sno), 0) + 1 AS next_sno FROM payments")
                next_sno = cur.fetchone()["next_sno"]
                cur.execute(
                    """
                    INSERT INTO payments
                        (sno, magazine, issue_price, subscriptions, qtr_issues, total_issues,
                         actual_cost, non_supply, deduction, net_payable,
                         invoice_no, invoice_date, requested_amt, quarter, part, months)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        next_sno, magazine, issue_price, libraries, qtr_issues, total_issues,
                        actual_cost, non_supply, deduction, net_payable,
                        invoice_no, invoice_date, requested_amt, quarter, part, months_db,
                    ),
                )
                new_id = cur.fetchone()["id"]
                conn.commit()
                return jsonify({"success": True, "row": new_id, "part": part, "complete": complete,
                                "nonSupply": non_supply, "message": "Appended"})
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"success": False,
                        "message": "இதே இதழ்/Quarter/Part வேறொருவரால் இப்போதுதான் பதிவாகியுள்ளது. மீண்டும் தேர்ந்தெடுத்து முயலவும்."}), 409
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
                "SELECT magazine, part, months, quarter, payment_date FROM payments "
                "WHERE quarter=%s ORDER BY magazine, part",
                (quarter,),
            )
            rows = cur.fetchall()
            cur.execute("SELECT name, periodicity FROM magazines")
            ticks = {x["name"] for x in cur.fetchall() if uses_month_tick(x["periodicity"])}

        by_mag = {}
        for r in rows:
            by_mag.setdefault(r["magazine"], []).append(r)

        # ஒரு இதழ்: ஏதாவது ஒரு Part தொகை வழங்கப்படாவிட்டால் "நிலுவை"; எல்லா Part-உம் வழங்கப்பட்டால் "வழங்கப்பட்டது".
        unpaid, paid, info = [], [], {}
        for name, rs in by_mag.items():
            n_unpaid = sum(1 for r in rs if not r["payment_date"])
            miss = missing_months(rs) if name in ticks else []
            partial_rows = [r for r in rs if parse_months(r["months"]) != [1, 2, 3]]
            info[name] = {
                "parts": len(rs),
                "unpaidParts": n_unpaid,
                "partial": bool(partial_rows),
                "missing": month_span_text(quarter, ",".join(str(m) for m in miss)) if miss else "",
            }
            if n_unpaid:
                unpaid.append(name)
            else:
                last = max(r["payment_date"] for r in rs)
                paid.append({"name": name, "paymentDate": last.strftime("%d/%m/%Y")})
        paid.sort(key=lambda x: x["name"])

        return jsonify({"unpaid": sorted(unpaid), "paid": paid, "info": info})
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
# 5b) Part பட்டியல் — தொகை வழங்கல் / நீக்கும் திரைகளில் Part தேர்வுக்கு (படி 2)
# --------------------------------------------------------------------------- #
@app.route("/api/payments/parts")
def api_payment_parts():
    magazine = request.args.get("magazine", "").strip()
    quarter = request.args.get("quarter", "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, part, months, quarter, magazine, invoice_no, invoice_date, requested_amt, qtr_issues, "
                "non_supply, net_payable, paid_amt, payment_date, voucher_no, transaction_no "
                "FROM payments WHERE magazine=%s AND quarter=%s ORDER BY part", (magazine, quarter))
            rows = cur.fetchall()
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
        month_tick = uses_month_tick(m["periodicity"] if m else "")
        names = quarter_month_names(quarter)
        used, parts = set(), []
        for r in rows:
            ms = parse_months(r["months"])
            used |= set(ms)
            parts.append({
                "id": r["id"], "part": r["part"], "months": ms,
                "monthsText": month_span_text(quarter, r["months"]),
                "partial": ms != [1, 2, 3],
                "label": row_label(r),
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": fmt_date(r["invoice_date"]),
                "requestedAmt": float(r["requested_amt"] or 0),
                "qtrIssues": r["qtr_issues"] or 0,
                "nonSupply": q_int(r["non_supply"]),
                "paid": r["payment_date"] is not None,
                "paymentDate": fmt_date(r["payment_date"]),
                "paidAmt": float(r["paid_amt"] or 0),
                "voucherNo": r["voucher_no"] or "",
                "transactionNo": r["transaction_no"] or "",
            })
        miss = [x for x in (1, 2, 3) if x not in used]
        return jsonify({
            "success": True, "monthTick": month_tick, "monthNames": names, "parts": parts,
            "usedMonths": sorted(used), "complete": not miss,
            "missingText": month_span_text(quarter, ",".join(str(x) for x in miss)) if miss else "",
        })
    finally:
        conn.close()


@app.route("/api/payments/part-months", methods=["POST"])
def api_payment_part_months():
    """தொகை வழங்கல் திரையில் சிவப்பு (குறை மாத) Part-ஐக் கிளிக் செய்து, எந்த மாதங்களுக்கு என்பதைத் திருத்த.
    - மற்ற Part-களில் உள்ள மாதங்களைத் தேர்ந்தெடுக்க முடியாது (இரட்டிப்புக் கட்டணம் தடுப்பு)
    - மாதங்களின் எண்ணிக்கை மாறினால் தொகை வழங்காத Part-ல் QTR Issues விகிதப்படி மாறும்
    - தொகை வழங்கிய Part-ன் தொகை/கழிவு தொடப்படாது (மாத அடையாளம் மட்டும் மாறும்)
    - Quarter Non-supply ஒரே முறை மட்டும் கழிவதை மீண்டும் சமன் செய்யும்"""
    data = request.get_json(force=True) or {}
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    part = _part_arg(data.get("part"))
    months = sorted({m for m in (q_int(x) for x in (data.get("months") or [])) if 1 <= m <= 3})
    if not magazine or not quarter:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Quarter அவசியம்."}), 400
    if not months:
        return jsonify({"success": False, "message": "குறைந்தது ஒரு மாதத்தைத் தேர்ந்தெடுக்கவும்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
            if not uses_month_tick(m["periodicity"] if m else ""):
                return jsonify({"success": False, "message": "இந்த இதழ் வகைக்கு மாத வாரியாகப் பிரிக்கும் வசதி இல்லை."}), 400
            rows = fetch_quarter_parts(cur, magazine, quarter)
            me = next((r for r in rows if r["part"] == part), None)
            if not me:
                return jsonify({"success": False, "message": "Part கிடைக்கவில்லை."}), 404
            names = quarter_month_names(quarter)
            used_by_others = set()
            for r in rows:
                if r["part"] != part:
                    used_by_others |= set(parse_months(r["months"]))
            overlap = sorted(used_by_others & set(months))
            if overlap:
                return jsonify({"success": False, "message":
                                "இந்த மாதங்கள் (%s) ஏற்கனவே மற்றொரு Part-ல் உள்ளன." % ", ".join(names[x - 1] for x in overlap)}), 409

            old_ms = parse_months(me["months"])
            months_db = None if months == [1, 2, 3] else ",".join(str(x) for x in months)
            paid = me["paid_amt"] is not None and float(me["paid_amt"] or 0) > 0
            note = ""
            if len(months) != len(old_ms) and not paid:
                new_q = max(1, round((me["qtr_issues"] or 0) * len(months) / max(len(old_ms), 1)))
                cur.execute("SELECT issue_price, subscriptions FROM payments WHERE id=%s", (me["id"],))
                pr = cur.fetchone()
                price, libs = float(pr["issue_price"] or 0), q_int(pr["subscriptions"])
                total, cost = libs * new_q, price * libs * new_q
                cur.execute(
                    "UPDATE payments SET months=%s, qtr_issues=%s, total_issues=%s, actual_cost=%s, "
                    "net_payable=%s - deduction, updated_at=now() WHERE id=%s",
                    (months_db, new_q, total, cost, cost, me["id"]))
                note = " QTR Issues %d ஆக மாற்றப்பட்டது." % new_q
            else:
                cur.execute("UPDATE payments SET months=%s, updated_at=now() WHERE id=%s", (months_db, me["id"]))
                if paid and len(months) != len(old_ms):
                    note = " (தொகை வழங்கப்பட்ட Part — தொகை / QTR Issues மாறவில்லை; தேவைப்பட்டால் Master Data → பதிவுத் திருத்தம்-ல் சரிசெய்யவும்.)"
            changed = rebalance_non_supply(cur, magazine, quarter)
            if changed:
                note += " Non-supply கழிவு Part %s-ல் சமன் செய்யப்பட்டது." % ", ".join(str(x) for x in changed)
            conn.commit()
        return jsonify({"success": True, "message": "Part %d மாதங்கள்: %s.%s" % (part, month_span_text(quarter, months_db), note)})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
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
                """SELECT quarter, SUM(paid_amt) AS paid_amt FROM payments
                   WHERE magazine=%s AND quarter LIKE %s AND paid_amt > 0
                   GROUP BY quarter ORDER BY quarter""",
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
    part = _part_arg(request.args.get("part"))
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM payments WHERE magazine=%s AND quarter=%s AND part=%s",
                (magazine, quarter, part),
            )
            row = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS n FROM payments WHERE magazine=%s AND quarter=%s", (magazine, quarter))
            part_count = cur.fetchone()["n"]
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
                    "part": part, "months": [1, 2, 3], "partial": False, "nonSupply": 0, "partCount": 0,
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
                "part": row["part"], "months": parse_months(row["months"]),
                "partial": parse_months(row["months"]) != [1, 2, 3],
                "monthsText": month_span_text(quarter, row["months"]),
                "nonSupply": q_int(row["non_supply"]),
                "partCount": part_count,
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
    part = _part_arg(data.get("part"))
    magazine = data.get("magazine")
    quarter = data.get("quarter")
    non_supply = q_int(data.get("nonSupply"))
    net_payable = q_num(data.get("netPayable"))
    amount_now_paid = round(q_num(data.get("amountNowPaid")))  # Rupees மட்டும் — Paisa இல்லை
    payment_date = data.get("paymentDate") or None
    remarks = data.get("remarks")
    bill_set_no = data.get("billSetNo")

    # கட்டாய விவரங்கள்: தொகை (>0), வழங்கும் தேதி, Bill Set No
    missing = []
    if amount_now_paid <= 0:
        missing.append("இப்போது வழங்கும் தொகை")
    if not payment_date:
        missing.append("வழங்கும் தேதி")
    if not str(bill_set_no or "").strip():
        missing.append("பில் செட் நம்பர்")
    if missing:
        return jsonify({"success": False,
                        "message": "கீழ்க்கண்ட விவரங்களை நிரப்பவும்: " + ", ".join(missing)}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT issue_price, months FROM payments WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "Record not found in PAYMENTS"}), 404
            issue_price = float(row["issue_price"] or 0)
            deduction = issue_price * non_supply

            # பல Part உள்ள Quarter-ல் Non-supply மொத்தம் Quarter அளவைத் தாண்டக்கூடாது (இரட்டிப்புக் கழிவு தடுப்பு)
            cur.execute(
                "SELECT COALESCE(SUM(non_supply),0) AS s, COUNT(*) AS n FROM payments "
                "WHERE magazine=%s AND quarter=%s AND part<>%s", (magazine, quarter, part))
            oth = cur.fetchone()
            if oth["n"] or parse_months(row["months"]) != [1, 2, 3]:
                cur.execute("SELECT non_supply FROM despatch_nonsupply WHERE quarter=%s AND magazine=%s", (quarter, magazine))
                dq = cur.fetchone()
                if dq and non_supply + q_int(oth["s"]) > q_int(dq["non_supply"]):
                    return jsonify({"success": False, "message":
                        "இந்த Quarter-ன் மொத்த Non-supply %d. மற்ற Part-களில் %d ஏற்கனவே கழிக்கப்பட்டுள்ளது — "
                        "இந்த Part-ல் அதிகபட்சம் %d மட்டுமே கழிக்கலாம்." % (
                            q_int(dq["non_supply"]), q_int(oth["s"]), max(q_int(dq["non_supply"]) - q_int(oth["s"]), 0))}), 400

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
            if bill_set_no and not vendor_mix_allowed(quarter):
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
                WHERE magazine=%s AND quarter=%s AND part=%s
                RETURNING sno, invoice_no, invoice_date, requested_amt
                """,
                (non_supply, deduction, net_payable, amount_now_paid, payment_date, remarks, bill_set_no,
                 magazine, quarter, part),
            )
            fresh = cur.fetchone()

            cur.execute("SELECT tnpfts_code FROM magazines WHERE name=%s", (magazine,))
            m = cur.fetchone()
            tnpfts_code = m["tnpfts_code"] if m else ""

            cur.execute(
                """
                INSERT INTO vouchers
                    (payment_sno, magazine, tnpfts_code, invoice_no, invoice_date,
                     requested_amt, deduction, amount_paid, quarter, part)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    fresh["sno"], magazine, tnpfts_code, fresh["invoice_no"], fresh["invoice_date"],
                    fresh["requested_amt"], deduction, amount_now_paid, quarter, part,
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
                    "part": r["part"],
                    "partLabel": row_label(r),
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
    quarter = get_quarters_arg() or None
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT p.id, p.voucher_no, p.magazine, p.part, p.months, p.paid_amt, p.payment_date, p.quarter, p.bill_set_no, "
                "       m.tnpfts_code, m.vendor_name, m.payee_name "
                "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
                "WHERE p.payment_date IS NOT NULL AND (p.transaction_no IS NULL OR p.transaction_no='')"
            )
            params = ()
            if quarter:
                sql += " AND p.quarter = ANY(%s)"
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
                    "magazine": row_label(r),
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

            # மற்ற Quarter-களில் Invoice பதிவாகி, இன்னும் தொகை வழங்காதவை —
            # தேர்ந்தெடுத்த இதழ் + அதே Vendor Code-ன் அனைத்து இதழ்களும் (தற்போதைய Quarter தவிர)
            cur.execute(
                "SELECT magazine, part, months, quarter, requested_amt FROM payments "
                "WHERE magazine = ANY(%s) AND quarter<>%s AND payment_date IS NULL "
                "ORDER BY quarter, magazine, part",
                (list(names | {magazine}), quarter),
            )
            other_pending = cur.fetchall()

            pay, ticks, in_quarter = {}, set(), set()
            if names:
                cur.execute(
                    "SELECT magazine, part, months, quarter, payment_date, paid_amt, requested_amt, voucher_no, bill_set_no "
                    "FROM payments WHERE quarter=%s AND magazine = ANY(%s) ORDER BY magazine, part",
                    (quarter, list(names)),
                )
                for r in cur.fetchall():
                    pay.setdefault(r["magazine"], []).append(r)
                cur.execute("SELECT name, periodicity FROM magazines WHERE name = ANY(%s)", (list(names),))
                ticks = {x["name"] for x in cur.fetchall() if uses_month_tick(x["periodicity"])}
                # carry-forward இதழ்கள் குழுவில் வரக்கூடாது — அந்த Quarter-க்கு நேரடி Master பதிவு உள்ளவை மட்டும்
                in_quarter = get_direct_master_magazines_for_quarter(cur, quarter)

        siblings = []
        for name in sorted(names):
            plist = pay.get(name)
            if plist is None and name not in in_quarter:
                continue  # இந்த Quarter-க்குப் பொருந்தாத இதழ்
            if plist is None:
                siblings.append({"magazine": name, "label": name, "part": 1, "status": "no_invoice"})
                continue
            for p in plist:
                lbl = row_label(p)
                if p["payment_date"]:
                    siblings.append({
                        "magazine": name, "label": lbl, "part": p["part"], "status": "paid",
                        "paidAmt": float(p["paid_amt"] or 0), "paymentDate": fmt_date(p["payment_date"]),
                        "billSetNo": (p["bill_set_no"] or "").strip(),
                    })
                else:
                    siblings.append({
                        "magazine": name, "label": lbl, "part": p["part"], "status": "pending",
                        "requestedAmt": float(p["requested_amt"] or 0),
                    })
            miss = missing_months(plist) if name in ticks else []
            if miss:
                siblings.append({
                    "magazine": name, "part": 1, "status": "no_invoice", "partialMissing": True,
                    "label": "%s — %s Invoice பதிவாகவில்லை" % (name, month_span_text(quarter, ",".join(str(x) for x in miss))),
                })
        for p in other_pending:
            siblings.append({
                "magazine": p["magazine"], "label": row_label(p), "part": p["part"], "status": "pending",
                "requestedAmt": float(p["requested_amt"] or 0),
                "quarter": p["quarter"], "quarterLabel": quarter_label(p["quarter"]), "otherQuarter": True,
            })
        return jsonify({"success": True, "code": code, "vendorName": vendor, "siblings": siblings})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Quarter பட்டியல் — DB-யில் உள்ளவை (புதிய Quarter சேர்ந்தால் தானாக வரும்)
# --------------------------------------------------------------------------- #
def all_known_quarters(cur):
    """magazine_quarters ∪ payments-ல் உள்ள அனைத்து Quarter-களும் (வரிசைப்படி)."""
    cur.execute(
        "SELECT quarter FROM magazine_quarters WHERE quarter IS NOT NULL AND quarter<>'' "
        "UNION SELECT quarter FROM payments WHERE quarter IS NOT NULL AND quarter<>'' "
        "ORDER BY quarter"
    )
    return [r["quarter"] for r in cur.fetchall()]


def next_quarters(last, n=4):
    """'2026-2027-Q4' -> ['2027-2028-Q1', ...n]"""
    m = re.match(r"^(\d{4})-(\d{4})-Q([1-4])$", last or "")
    if not m:
        return []
    y1, y2, q = int(m.group(1)), int(m.group(2)), int(m.group(3))
    out = []
    for _ in range(n):
        q += 1
        if q > 4:
            q, y1, y2 = 1, y1 + 1, y2 + 1
        out.append(f"{y1}-{y2}-Q{q}")
    return out


@app.route("/api/quarters")
def api_quarters():
    """quarters = DB-யில் உள்ள Quarter-கள் (வடிகட்டிகளுக்கு);
    addable = quarters + அடுத்த 4 (புதிய Quarter உருவாக்க / Invoice பதிவுக்கு)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            qs = all_known_quarters(cur)
    finally:
        conn.close()
    addable = sorted(set(qs) | set(next_quarters(qs[-1] if qs else "")))
    return jsonify({"success": True, "quarters": qs, "addable": addable})


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


@app.route("/api/admin/delete-magazine", methods=["POST"])
def api_admin_delete_magazine():
    """இதழை முழுமையாக நீக்கும் (magazines + magazine_quarters).
    body: {name, force?}
      - அந்த இதழுக்கு Payments / Vouchers / Despatch பதிவுகள் இருந்தால், force=true இல்லாமல் நீக்காது;
        எண்ணிக்கையுடன் blocked=true திருப்பித் தரும்.
      - force=true -> அந்தப் பதிவுகளையும் சேர்த்து நீக்கும் (ஒரே transaction; பிழை வந்தால் rollback)."""
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    force = bool(data.get("force"))
    if not name:
        return jsonify({"success": False, "message": "Magazine Name அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM magazines WHERE name=%s", (name,))
            mag = cur.fetchone()
            if not mag:
                return jsonify({"success": False, "message": "இதழ் கிடைக்கவில்லை."}), 404

            cur.execute("SELECT COUNT(*) AS c FROM payments WHERE magazine=%s", (name,))
            n_pay = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM vouchers WHERE magazine=%s", (name,))
            n_vou = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM despatch_nonsupply WHERE magazine=%s", (name,))
            n_des = cur.fetchone()["c"]

            if (n_pay or n_vou or n_des) and not force:
                return jsonify({
                    "success": False, "blocked": True,
                    "payments": n_pay, "vouchers": n_vou, "despatch": n_des,
                    "message": f"'{name}' இதழுக்கு Payments {n_pay}, Vouchers {n_vou}, "
                               f"Despatch/Non-supply {n_des} பதிவுகள் உள்ளன.",
                }), 409

            if force:
                cur.execute("DELETE FROM vouchers WHERE magazine=%s", (name,))
                cur.execute("DELETE FROM payments WHERE magazine=%s", (name,))
                cur.execute("DELETE FROM despatch_nonsupply WHERE magazine=%s", (name,))
            cur.execute("DELETE FROM magazine_quarters WHERE magazine_id=%s", (mag["id"],))
            n_q = cur.rowcount
            cur.execute("DELETE FROM magazines WHERE id=%s", (mag["id"],))
            conn.commit()
        msg = f"'{name}' இதழ் முழுமையாக நீக்கப்பட்டது ({n_q} quarter விலைப் பதிவு(கள்))."
        if force and (n_pay or n_vou or n_des):
            msg += f" Payments {n_pay}, Vouchers {n_vou}, Despatch {n_des} பதிவுகளும் நீக்கப்பட்டன."
        return jsonify({"success": True, "message": msg})
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
    part = _part_arg(data.get("part"))
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    if not magazine or not quarter:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Quarter அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, paid_amt FROM payments WHERE magazine=%s AND quarter=%s AND part=%s",
                (magazine, quarter, part),
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
                WHERE magazine=%s AND quarter=%s AND part=%s
                """,
                (magazine, quarter, part),
            )
            cur.execute(
                "DELETE FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s",
                (magazine, quarter, part),
            )
            rebalance_non_supply(cur, magazine, quarter)
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
    part = _part_arg(data.get("part"))
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    if not magazine or not quarter:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Quarter அவசியம்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT sno FROM payments WHERE magazine=%s AND quarter=%s AND part=%s",
                (magazine, quarter, part),
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
                WHERE magazine=%s AND quarter=%s AND part=%s
                """,
                (magazine, quarter, part),
            )
            cur.execute(
                "DELETE FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s",
                (magazine, quarter, part),
            )
            rebalance_non_supply(cur, magazine, quarter)
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
    part = _part_arg(data.get("part"))
    magazine = (data.get("magazine") or "").strip()
    quarter = (data.get("quarter") or "").strip()
    scope = (data.get("scope") or "").strip()
    if not magazine or not quarter or not scope:
        return jsonify({"success": False, "message": "இதழ், Quarter மற்றும் என்ன நீக்க வேண்டும் என தேர்ந்தெடுக்கவும்."}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM payments WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "இந்த இதழ் / Quarter-க்கு பதிவு கிடைக்கவில்லை."}), 404

            if scope == "voucher":
                cur.execute(
                    "UPDATE payments SET voucher_no=NULL, updated_at=now() WHERE magazine=%s AND quarter=%s AND part=%s",
                    (magazine, quarter, part),
                )
                msg = "Voucher Number நீக்கப்பட்டது."

            elif scope == "bank":
                cur.execute(
                    """
                    UPDATE payments SET
                        non_supply=0, deduction=0, net_payable=0,
                        paid_amt=0, payment_date=NULL, transaction_no=NULL,
                        remarks=NULL, bill_set_no=NULL, updated_at=now()
                    WHERE magazine=%s AND quarter=%s AND part=%s
                    """,
                    (magazine, quarter, part),
                )
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
                rebalance_non_supply(cur, magazine, quarter)
                msg = "Bank Transaction (தொகை/தேதி/Transaction No) விவரங்கள் நீக்கப்பட்டன."

            elif scope == "mail":
                cur.execute(
                    "UPDATE payments SET mail_sent=FALSE, pdf_url=NULL, updated_at=now() WHERE magazine=%s AND quarter=%s AND part=%s",
                    (magazine, quarter, part),
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
                    WHERE magazine=%s AND quarter=%s AND part=%s
                    """,
                    (magazine, quarter, part),
                )
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
                rebalance_non_supply(cur, magazine, quarter)
                msg = "Payment Details (Voucher Number + Bank Transaction + Mail Send) அனைத்தும் நீக்கப்பட்டன."

            elif scope == "invoice":
                cur.execute("DELETE FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
                cur.execute("DELETE FROM payments WHERE magazine=%s AND quarter=%s AND part=%s", (magazine, quarter, part))
                rebalance_non_supply(cur, magazine, quarter)
                msg = ("Invoice Details உட்பட Part %d பதிவு நீக்கப்பட்டது. (இதழ் விலை Master தொடப்படவில்லை.)" % part
                       if part != 1 else
                       "Invoice Details உட்பட இந்த பதிவு நீக்கப்பட்டது. (இதழ் விலை Master தொடப்படவில்லை.)")

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
                SELECT quarter, COUNT(DISTINCT magazine) AS magazines,
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
                invoiced_by_magazine = {}
                for r in rows:
                    if (r["invoice_no"] or "").strip():
                        invoiced_by_magazine.setdefault(r["magazine"], []).append(r)
                cur.execute("SELECT name, periodicity FROM magazines")
                ticks = {x["name"] for x in cur.fetchall() if uses_month_tick(x["periodicity"])}

                received, not_received = [], []
                for name in sorted(expected_magazines):
                    rs = invoiced_by_magazine.get(name)
                    if rs:
                        for r in rs:
                            received.append(
                                {
                                    "serial": len(received) + 1,
                                    "magazine": row_label(r),
                                    "invoiceNo": (r["invoice_no"] or "").strip(),
                                    "invoiceDate": fmt_date(r["invoice_date"]),
                                    "requestedAmt": float(r["requested_amt"] or 0),
                                    "quarter": r["quarter"] or "",
                                }
                            )
                        miss = missing_months(rs) if name in ticks else []
                        if miss:      # சில மாதங்களுக்கு மட்டும் Invoice வந்துள்ளது — மீதமுள்ளவை நிலுவை
                            not_received.append({
                                "serial": len(not_received) + 1,
                                "magazine": "%s — நிலுவை மாதம்: %s" % (
                                    name, month_span_text(quarter, ",".join(str(x) for x in miss))),
                            })
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
                        "magazine": row_label(r),
                        "invoiceNo": invoice_no,
                        "invoiceDate": fmt_date(r["invoice_date"]),
                        "requestedAmt": float(r["requested_amt"] or 0),
                        "quarter": r["quarter"] or "",
                    }
                )
            else:
                not_received.append({"serial": len(not_received) + 1, "magazine": row_label(r)})

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
                "magazine": row_label(r),
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


@app.route("/api/reports/unpaid")
def api_report_unpaid():
    """இன்னும் தொகை வழங்கப்படாதவை — Invoice பெறப்பட்டு (payments-ல் பதிவு உள்ளது), payment_date இல்லாதவை.
    ?quarter=Q1  /  ?quarter=Q1,Q2  /  காலி = அனைத்து Quarter-களும்."""
    quarters = get_quarters_arg()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = ("SELECT * FROM payments WHERE payment_date IS NULL "
                   "AND (COALESCE(TRIM(invoice_no),'')<>'' OR COALESCE(requested_amt,0)>0)")
            args = []
            if quarters:
                sql += " AND quarter = ANY(%s)"
                args.append(quarters)
            sql += " ORDER BY quarter, magazine, part"
            cur.execute(sql, args)
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "magazine": row_label(r),
                "quarter": r["quarter"] or "",
                "invoiceNo": r["invoice_no"] or "",
                "invoiceDate": fmt_date(r["invoice_date"]),
                "requestedAmt": float(r["requested_amt"] or 0),
                "netPayable": float(r["net_payable"] or 0),
            }
            for i, r in enumerate(rows, start=1)
        ]
        return jsonify({"success": True, "rows": result,
                        "total": sum(x["requestedAmt"] for x in result)})
    finally:
        conn.close()


@app.route("/api/reports/voucher-register")
def api_report_voucher_register():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT v.*, p.voucher_no AS pay_voucher_no, p.months AS pay_months FROM vouchers v "
                "LEFT JOIN payments p ON p.magazine=v.magazine AND p.quarter=v.quarter AND p.part=v.part ORDER BY v.id"
            )
            rows = cur.fetchall()
        result = [
            {
                "serial": i,
                "paymentSNo": r["payment_sno"] or "",
                "voucherNo": r["pay_voucher_no"] or "",
                "magazine": part_label(r["magazine"] or "", r["part"], r["pay_months"], r["quarter"]) if r["magazine"] else "",
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
                "magazine": row_label(r),
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
                    "magazine": part_label(magazine, r["part"], r["months"], r["quarter"]),
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
    quarters = get_quarters_arg()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if not quarters:                      # காலி = அனைத்து Quarter-களும்
                quarters = all_known_quarters(cur)
            if not quarters:
                return jsonify({"success": False, "message": "Quarter தேர்வு செய்யவும்."}), 400

            cur.execute("SELECT name, email_id FROM magazines")
            email_map = {r["name"]: (r["email_id"] or "").strip() for r in cur.fetchall()}

            cur.execute("SELECT name, periodicity FROM magazines")
            ticks = {x["name"] for x in cur.fetchall() if uses_month_tick(x["periodicity"])}

            pending = []
            for quarter in quarters:
                master_magazines = get_master_magazines_for_quarter(cur, quarter)
                cur.execute(
                    "SELECT magazine, part, months, quarter FROM payments "
                    "WHERE quarter=%s AND invoice_no IS NOT NULL AND invoice_no<>''",
                    (quarter,),
                )
                inv = {}
                for r in cur.fetchall():
                    inv.setdefault(r["magazine"], []).append(r)
                for name in master_magazines:
                    rs = inv.get(name)
                    if not rs:                                   # இந்த Quarter-க்கு Invoice எதுவுமே இல்லை
                        pending.append({"magazine": name, "quarter": quarter, "email": email_map.get(name, ""),
                                        "partial": False, "missing": "", "received": ""})
                        continue
                    miss = missing_months(rs) if name in ticks else []
                    if miss:                                      # சில மாதங்களுக்கு மட்டும் வந்துள்ளது
                        got = sorted({m for r in rs for m in parse_months(r["months"])})
                        pending.append({
                            "magazine": name, "quarter": quarter, "email": email_map.get(name, ""),
                            "partial": True,
                            "missing": month_span_text(quarter, ",".join(str(x) for x in miss)),
                            "received": month_span_text(quarter, ",".join(str(x) for x in got)),
                        })
        pending.sort(key=lambda x: ((x["magazine"] or "").lower(), x["quarter"]))
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
        missing = (item.get("missing") or "").strip()
        received = (item.get("received") or "").strip()
        if item.get("partial") and missing:
            subject = f"Request for Invoice Submission - {magazine} for {quarter} ({missing})"
            body = f"""Dear Sir/Madam,<br><br>
We have received the invoice for <strong>{magazine}</strong> for <strong>{received}</strong> of the
<strong>{quarter}</strong> (i.e., {period_text}), but we have not yet received the invoice for the
remaining month(s): <strong>{missing}</strong>.<br><br>
Kindly issue and send the invoice for the remaining month(s) at the earliest so that we can process the payment without delay.<br><br>
Thank you for your kind cooperation.<br><br>
Regards,<br>District Library Officer<br>Dindigul"""
        else:
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
                    "SELECT id, magazine, part, months, paid_amt, voucher_no, quarter FROM payments "
                    "WHERE bill_set_no=%s AND quarter=%s ORDER BY magazine, part",
                    (set_no, quarter),
                )
            else:
                cur.execute(
                    "SELECT id, magazine, part, months, paid_amt, voucher_no, quarter FROM payments "
                    "WHERE bill_set_no=%s ORDER BY magazine, part",
                    (set_no,),
                )
            rows = cur.fetchall()
        result = [
            {
                "row": r["id"],
                "magazine": row_label(r),
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
                "SELECT voucher_no, magazine, part, months, paid_amt, quarter, bill_set_no FROM payments "
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
                "magazine": row_label(r),
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
        SELECT p.magazine, p.part, p.months, p.voucher_no, p.invoice_no, p.invoice_date,
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

    no_voucher = [row_label(r) for r in prows if not (r["voucher_no"] or "").strip()]
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
                "magazine": row_label(r),
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
    quarter = get_quarters_arg()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT p.id, p.magazine, p.part, p.months, p.quarter, p.voucher_no, p.transaction_no, p.payment_date, p.bill_set_no, "
                "p.requested_amt, p.paid_amt, m.email_id, m.tnpfts_code, m.vendor_name, m.payee_name "
                "FROM payments p LEFT JOIN magazines m ON m.name = p.magazine "
                "WHERE p.transaction_no IS NOT NULL AND p.transaction_no<>'' "
                "AND p.payment_date IS NOT NULL AND p.mail_sent = FALSE"
            )
            params = ()
            if quarter:
                sql += " AND p.quarter = ANY(%s)"
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
            for em in parse_email_list(r["email_id"])[0] + parse_email_list(r["email_id"])[1]:
                if em.lower() not in [x.lower() for x in g["emails"]]:
                    g["emails"].append(em)
            bill, net = float(r["requested_amt"] or 0), float(r["paid_amt"] or 0)
            g["items"].append({
                "row": r["id"], "voucherNo": r["voucher_no"] or "", "magazine": row_label(r),
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
            g["email"] = ", ".join(g["emails"])
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
            email = ", ".join(parse_email_list([m["email"] for m in members])[0])
            bad_emails = parse_email_list([m["email"] for m in members])[1]
            names = ", ".join(m["magazine"] for m in members)
            if bad_emails:
                return jsonify({"success": False, "message": f"'{names}' — தவறான Email முகவரி: {', '.join(bad_emails)}. Master Data → Vendors-ல் திருத்தவும்."}), 400
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


# --------------------------------------------------------------------------- #
# CSV Export — Google Sheet (A:V) அமைப்பிலேயே தரவைப் பதிவிறக்க
# --------------------------------------------------------------------------- #
_SHEET_CSV_HEADERS = [
    "S.no", "NAME OF THE MAGAZINE", "ISSUE PRICE", "No.of Subscription", "QTR1 ISSUES",
    "TOTAL ISSUES AS PER SUBSCRIPTION", "ACTUAL COST TO BE PAID", "NON SUPPLY COPIES AS PER REPORT",
    "DEDUCTION  AMOUNT", "NET PAYABLE AMOUNT", "INVOICE NUMBER", "INVOICE DATE", "REQUESTED AMOUNT",
    "PAID AMOUNT", "PAID DATE", "NET BANKING REFERENCE NUMBER", "REMARKS", "SET NUMBER",
    "MAIL SENT OR NOT", "URL", "Quarter details", "Voucher number",
    "PART", "MONTHS",     # W, X — ஒத்திசைவு key: NAME OF THE MAGAZINE + Quarter details + PART
]


def _csv_num(v):
    """எண்: 6000.00 -> 6000 ; 12.5 -> 12.5 ; இல்லையெனில் காலி."""
    if v is None:
        return ""
    f = float(v)
    return int(f) if f == int(f) else round(f, 2)


@app.route("/api/export/sheet-csv")
def api_export_sheet_csv():
    """payments அட்டவணையை Google Sheet-ன் A:V columns வரிசையிலேயே CSV ஆகத் தரும்.
    ?quarter=2026-2027-Q2 (விடுத்தால் எல்லா Quarter-களும்). வரிசை: இதழ் பெயர் A→Z, பிறகு Quarter."""
    quarters = get_quarters_arg()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            sql = "SELECT * FROM payments"
            params = ()
            if quarters:
                sql += " WHERE quarter = ANY(%s)"
                params = (quarters,)
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    rows.sort(key=lambda r: ((r["magazine"] or "").strip().lower(), r["quarter"] or "", r["part"] or 1))
    base = request.host_url.rstrip("/")

    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow(_SHEET_CSV_HEADERS)
    for r in rows:
        pdf = (r["pdf_url"] or "").strip()
        if pdf.startswith("/"):
            pdf = base + pdf
        w.writerow([
            r["sno"] if r["sno"] is not None else "",
            r["magazine"] or "",
            _csv_num(r["issue_price"]),
            r["subscriptions"] if r["subscriptions"] is not None else "",
            r["qtr_issues"] if r["qtr_issues"] is not None else "",
            r["total_issues"] if r["total_issues"] is not None else "",
            _csv_num(r["actual_cost"]),
            r["non_supply"] if r["non_supply"] is not None else "",
            _csv_num(r["deduction"]),
            _csv_num(r["net_payable"]),
            r["invoice_no"] or "",
            fmt_date(r["invoice_date"]),
            _csv_num(r["requested_amt"]),
            _csv_num(r["paid_amt"]) if (r["paid_amt"] or 0) else "",
            fmt_date(r["payment_date"]),
            r["transaction_no"] or "",
            r["remarks"] or "",
            r["bill_set_no"] or "",
            "Sent" if r["mail_sent"] else "",
            pdf,
            r["quarter"] or "",
            r["voucher_no"] or "",
            r["part"] or 1,
            month_span_text(r["quarter"], r["months"]),
        ])

    # UTF-8 BOM — Excel-ல் தமிழ் எழுத்துகள் சரியாகத் திறக்க
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    fname = f"payments_{'+'.join(quarters) or 'all'}_{date.today().strftime('%Y%m%d')}.csv"
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# =============================================================================
# Master Data — Payment Details (A:V) — திரையில் பார்க்க + CSV / Excel / PDF பதிவிறக்கம்
# =============================================================================
# (தலைப்பு, key, வகை)  — வகை: txt | int | money | date
PAY_EXPORT_COLS = [
    ("S.No", "sno", "int"),
    ("NAME OF THE MAGAZINE", "magazine", "txt"),
    ("ISSUE PRICE", "issue_price", "money"),
    ("No. of SUBSCRIPTION", "subscriptions", "int"),
    ("QTR ISSUES", "qtr_issues", "int"),
    ("TOTAL ISSUES AS PER SUBSCRIPTION", "total_issues", "int"),
    ("ACTUAL COST TO BE PAID", "actual_cost", "money"),
    ("NON SUPPLY COPIES AS PER REPORT", "non_supply", "int"),
    ("DEDUCTION AMOUNT", "deduction", "money"),
    ("NET PAYABLE AMOUNT", "net_payable", "money"),
    ("INVOICE NUMBER", "invoice_no", "txt"),
    ("INVOICE DATE", "invoice_date", "date"),
    ("REQUESTED AMOUNT", "requested_amt", "money"),
    ("PAID AMOUNT", "paid_amt", "money"),
    ("PAID DATE", "payment_date", "date"),
    ("NET BANKING REFERENCE NUMBER", "transaction_no", "txt"),
    ("REMARKS", "remarks", "txt"),
    ("SET NUMBER", "bill_set_no", "txt"),
    ("MAIL SENT OR NOT", "mail", "txt"),
    ("URL", "url", "txt"),
    ("Quarter details", "quarter", "txt"),
    ("Voucher number", "voucher_no", "txt"),
]
_PAY_SUM_KEYS = ("actual_cost", "deduction", "net_payable", "requested_amt", "paid_amt")


def fetch_payment_export_rows(cur, quarters=None, host_url=""):
    """payments அட்டவணையை A:V வடிவில் — இதழ் பெயர் A–Z (பின் Quarter) வரிசையில். S.No 1,2,3… என மீண்டும் எண்ணிடப்படும்."""
    sql = "SELECT * FROM payments"
    params = ()
    quarters = _qlist(quarters)
    if quarters:
        sql += " WHERE quarter = ANY(%s)"
        params = (quarters,)
    cur.execute(sql, params)
    rows = cur.fetchall()
    rows.sort(key=lambda r: ((r["magazine"] or "").strip().lower(), r["quarter"] or "", r["part"] or 1))
    base = (host_url or "").rstrip("/")
    out = []
    for n, r in enumerate(rows, 1):
        paid = r["payment_date"] is not None or float(r["paid_amt"] or 0) > 0
        pdf = r["pdf_url"] or ""
        out.append({
            "sno": n,
            "magazine": row_label(r) if r["magazine"] else "",
            "issue_price": float(r["issue_price"] or 0),
            "subscriptions": int(r["subscriptions"] or 0),
            "qtr_issues": int(r["qtr_issues"] or 0),
            "total_issues": int(r["total_issues"] or 0),
            "actual_cost": float(r["actual_cost"] or 0),
            "non_supply": int(r["non_supply"] or 0),
            "deduction": float(r["deduction"] or 0),
            "net_payable": float(r["net_payable"] or 0),
            "invoice_no": r["invoice_no"] or "",
            "invoice_date": r["invoice_date"],
            "requested_amt": float(r["requested_amt"] or 0),
            "paid_amt": float(r["paid_amt"] or 0) if paid else None,
            "payment_date": r["payment_date"],
            "transaction_no": r["transaction_no"] or "",
            "remarks": r["remarks"] or "",
            "bill_set_no": r["bill_set_no"] or "",
            "mail": "Sent" if r["mail_sent"] else "",
            "url": (base + pdf) if pdf.startswith("/") and base else pdf,
            "quarter": r["quarter"] or "",
            "voucher_no": r["voucher_no"] or "",
        })
    return out


def _pay_cell_text(row, key, kind):
    v = row.get(key)
    if v is None or v == "":
        return ""
    if kind == "date":
        return fmt_date(v)
    if kind == "money":
        return indian_grouping(v)
    return str(v)


def _pay_totals(rows):
    return {k: sum((r.get(k) or 0) for r in rows) for k in _PAY_SUM_KEYS}


def _pay_export_title(quarter):
    ql = _qlist(quarter)
    return "Payment Details — " + (", ".join(quarter_label(q) for q in ql) if ql else "All Quarters")


def build_payments_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in PAY_EXPORT_COLS])
    for r in rows:
        line = []
        for _h, key, kind in PAY_EXPORT_COLS:
            v = r.get(key)
            if v is None:
                line.append("")
            elif kind == "date":
                line.append(fmt_date(v))
            elif kind == "money":
                line.append(("%.2f" % v).rstrip("0").rstrip("."))
            else:
                line.append(v)
        w.writerow(line)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM — Excel-ல் தமிழ் சரியாகத் தெரிய


def build_payments_xlsx(rows, quarter=None):
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Payment Details")
    head = wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#0F2347", "border": 1,
                          "text_wrap": True, "align": "center", "valign": "vcenter"})
    txt = wb.add_format({"border": 1, "valign": "top"})
    txtc = wb.add_format({"border": 1, "valign": "top", "align": "center"})
    num = wb.add_format({"border": 1, "valign": "top", "num_format": "#,##0.00"})
    integer = wb.add_format({"border": 1, "valign": "top", "align": "center", "num_format": "0"})
    dt = wb.add_format({"border": 1, "valign": "top", "align": "center", "num_format": "dd/mm/yyyy"})
    tot_l = wb.add_format({"bold": True, "border": 1, "bg_color": "#FFF3C4", "align": "right"})
    tot_n = wb.add_format({"bold": True, "border": 1, "bg_color": "#FFF3C4", "num_format": "#,##0.00"})
    tot_b = wb.add_format({"bold": True, "border": 1, "bg_color": "#FFF3C4"})

    widths = [7, 34, 10, 12, 10, 14, 14, 12, 12, 14, 18, 12, 14, 14, 12, 28, 22, 8, 10, 34, 16, 12]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    ws.set_row(0, 48)
    for c, (h, _k, _t) in enumerate(PAY_EXPORT_COLS):
        ws.write(0, c, h, head)
    for r_i, r in enumerate(rows, start=1):
        for c, (_h, key, kind) in enumerate(PAY_EXPORT_COLS):
            v = r.get(key)
            if v is None or v == "":
                ws.write_blank(r_i, c, None, txt)
            elif kind == "date":
                ws.write_datetime(r_i, c, datetime(v.year, v.month, v.day), dt)
            elif kind == "money":
                ws.write_number(r_i, c, float(v), num)
            elif kind == "int":
                ws.write_number(r_i, c, int(v), integer)
            elif key in ("mail", "bill_set_no", "quarter", "voucher_no"):
                ws.write_string(r_i, c, str(v), txtc)
            elif key == "url" and str(v).startswith("http"):
                ws.write_url(r_i, c, str(v), txt, "PDF")
            else:
                ws.write_string(r_i, c, str(v), txt)
    # மொத்த வரி — SUM formula
    tr = len(rows) + 1
    ws.write(tr, 0, "", tot_b)
    for c, (_h, key, _t) in enumerate(PAY_EXPORT_COLS):
        if c == 1:
            ws.write(tr, c, "TOTAL", tot_l)
        elif key in _PAY_SUM_KEYS and rows:
            col = xlsxwriter.utility.xl_col_to_name(c)
            ws.write_formula(tr, c, f"=SUM({col}2:{col}{tr})", tot_n, _pay_totals(rows)[key])
        elif c:
            ws.write(tr, c, "", tot_b)
    ws.freeze_panes(1, 2)
    if rows:
        ws.autofilter(0, 0, len(rows), len(PAY_EXPORT_COLS) - 1)
    ws.set_landscape()
    ws.set_paper(8)          # A3
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(0)
    wb.close()
    return buf.getvalue()


def build_payments_pdf(rows, quarter=None):
    """A3 landscape, 22 columns — fpdf2 + uharfbuzz (இதே அமைப்பு Payment Advice PDF-ல் பயன்படுகிறது)."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(orientation="L", unit="mm", format="A3")
    pdf.set_margins(8, 10, 8)
    pdf.set_auto_page_break(auto=True, margin=10)
    pdf.add_font("Tamil", "", TAMIL_FONT_PATH)
    pdf.set_text_shaping(True)
    pdf.add_page()

    pdf.set_font("Tamil", size=15)
    pdf.cell(0, 8, "திண்டுக்கல் மாவட்ட நூலக ஆணைக்குழு", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Tamil", size=11)
    pdf.multi_cell(0, 7, f"{_pay_export_title(quarter)}  |  பதிவுகள்: {len(rows)}  |  {datetime.now().strftime('%d/%m/%Y')}",
                   align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    rel = [6, 26, 9, 10, 8, 11, 13, 10, 11, 13, 15, 12, 13, 13, 12, 22, 15, 6, 8, 9, 15, 10]
    avail = 420 - 16
    widths = tuple(round(w * avail / sum(rel), 2) for w in rel)
    aligns = []
    for _h, _k, kind in PAY_EXPORT_COLS:
        aligns.append("RIGHT" if kind == "money" else ("CENTER" if kind in ("int", "date") else "LEFT"))

    pdf.set_font("Tamil", size=6.5)
    head_style = FontFace(color=(255, 255, 255), fill_color=(15, 35, 71))
    with pdf.table(col_widths=widths, text_align=tuple(aligns), headings_style=head_style,
                   line_height=3.6, padding=0.7, borders_layout="ALL") as table:
        h = table.row()
        for t, _k, _tp in PAY_EXPORT_COLS:
            h.cell(t, align="C")
        for r in rows:
            row = table.row()
            for _h, key, kind in PAY_EXPORT_COLS:
                if key == "url":
                    u = r.get("url") or ""
                    if u.startswith("http"):
                        row.cell("PDF", align="C", link=u)
                    else:
                        row.cell("")
                else:
                    row.cell(_pay_cell_text(r, key, kind))
        if rows:
            t = _pay_totals(rows)
            fill = FontFace(fill_color=(255, 243, 196))
            tot = table.row()
            tot.cell("TOTAL", colspan=6, align="R", style=fill)
            for _h, key, _kind in PAY_EXPORT_COLS[6:]:
                tot.cell(indian_grouping(t[key]) if key in t else "", align="R", style=fill)
    return bytes(pdf.output())


@app.route("/api/export/payments")
def api_export_payments():
    """format=json (திரைக் காட்சி) | csv | xlsx | pdf ;  quarter (விருப்பம்)"""
    fmt = (request.args.get("format") or "json").strip().lower()
    quarter = get_quarters_arg()          # [] = அனைத்தும்
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rows = fetch_payment_export_rows(cur, quarter, request.host_url)
    finally:
        conn.close()

    stem = "Payment_Details_" + ("+".join(quarter) if quarter else "All") + "_" + datetime.now().strftime("%Y%m%d")
    try:
        if fmt == "csv":
            return send_file(io.BytesIO(build_payments_csv(rows)), mimetype="text/csv",
                             as_attachment=True, download_name=stem + ".csv")
        if fmt == "xlsx":
            return send_file(io.BytesIO(build_payments_xlsx(rows, quarter)),
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True, download_name=stem + ".xlsx")
        if fmt == "pdf":
            return send_file(io.BytesIO(build_payments_pdf(rows, quarter)), mimetype="application/pdf",
                             as_attachment=True, download_name=stem + ".pdf")
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": f"{fmt.upper()} உருவாக்கத்தில் பிழை: {e}"}), 500

    totals = _pay_totals(rows)
    return jsonify({
        "success": True,
        "headers": [c[0] for c in PAY_EXPORT_COLS],
        "keys": [c[1] for c in PAY_EXPORT_COLS],
        "kinds": [c[2] for c in PAY_EXPORT_COLS],
        "rows": [[_pay_cell_text(r, k, t) if k != "url" else (r.get("url") or "") for _h, k, t in PAY_EXPORT_COLS] for r in rows],
        "totals": {k: indian_grouping(v) for k, v in totals.items()},
        "count": len(rows),
    })


# =============================================================================
# தொகை வழங்கப்பட்ட விவரம் — Paid Details Report (English) — JSON / PDF / Excel / CSV
#   ஒரு இதழைத் தேர்ந்தெடுத்தால், அதன் Vendor Code-க்குரிய அனைத்து இதழ்களும் (தேர்ந்தெடுத்த Quarter-களில்)
#   Bank Transaction பதிவில் உள்ள அதே விதியில் (Quarter + Vendor Code + Bill Set No) குழுவாகக் காட்டப்படும்.
# =============================================================================
PAID_REPORT_HEADERS = ["S.NO", "MAGAZINE", "INVOICE NUMBER", "INVOICE DATE",
                       "REQUESTED AMOUNT", "PAID AMOUNT", "PAID DATE", "BANK TRANSACTION NUMBER"]


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suf)


def build_paid_report(cur, magazine, quarters):
    cur.execute(
        "SELECT name, tnpfts_code, vendor_name, payee_name FROM magazines WHERE name=%s", (magazine,)
    )
    m = cur.fetchone()
    if not m:
        return None
    code = norm_code(m["tnpfts_code"])
    if code:
        cur.execute(
            "SELECT name FROM magazines WHERE UPPER(TRIM(COALESCE(tnpfts_code,'')))=%s", (code,)
        )
        names = sorted({r["name"] for r in cur.fetchall()} | {magazine})
    else:
        names = [magazine]

    sql = (
        "SELECT p.id, p.magazine, p.part, p.months, p.quarter, p.bill_set_no, p.voucher_no, p.invoice_no, p.invoice_date, "
        "       p.requested_amt, p.paid_amt, p.payment_date, p.transaction_no "
        "FROM payments p WHERE p.magazine = ANY(%s) AND p.paid_amt > 0"
    )
    params = [names]
    ql = _qlist(quarters)
    if ql:
        sql += " AND p.quarter = ANY(%s)"
        params.append(ql)
    cur.execute(sql, params)
    rows = cur.fetchall()

    vendor = vendor_display_name(m["vendor_name"], m["payee_name"], magazine)
    # குழு விதி: ஒரே Quarter + Vendor Code + ஒரே Bank Transaction No = ஒரே தொகை வழங்கல்.
    # (Transaction No மாறினால் அதே Quarter-லும் தனி வழங்கல் → 2nd payment.)
    # Transaction No இல்லாத பதிவு: Code + Bill Set இருந்தால் அதன்படி; இல்லையெனில் தனியாக.
    groups = {}
    for r in rows:
        txn = (r["transaction_no"] or "").strip()
        s_no = (r["bill_set_no"] or "").strip()
        if code and txn:
            key = (r["quarter"], code, "T:" + txn.upper())
        elif code and s_no:
            key = (r["quarter"], code, "S:" + s_no)
        else:
            key = (r["quarter"], "#%d" % r["id"], "")
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "quarter": r["quarter"] or "", "quarterLabel": quarter_label(r["quarter"]),
                "transactionNo": txn, "rows": [], "_dates": [],
                "totalRequested": 0.0, "totalPaid": 0.0,
            }
        g["rows"].append({
            "magazine": row_label(r), "voucherNo": r["voucher_no"] or "",
            "invoiceNo": r["invoice_no"] or "", "invoiceDate": fmt_date(r["invoice_date"]),
            "invoiceDateRaw": r["invoice_date"],
            "requestedAmt": float(r["requested_amt"] or 0), "paidAmt": float(r["paid_amt"] or 0),
            "paidDate": fmt_date(r["payment_date"]), "transactionNo": r["transaction_no"] or "",
        })
        if r["payment_date"]:
            g["_dates"].append(r["payment_date"])
        g["totalRequested"] += float(r["requested_amt"] or 0)
        g["totalPaid"] += float(r["paid_amt"] or 0)

    out = []
    for g in groups.values():
        g["rows"].sort(key=lambda x: (voucher_sort_key(x["voucherNo"]), x["magazine"]))
        g["magazines"] = sorted({x["magazine"] for x in g["rows"]})
        dts = sorted(set(g.pop("_dates")))
        g["firstDate"] = dts[0] if dts else None
        g["paidDates"] = [fmt_date(d) for d in dts]
        g["multi"] = len(g["rows"]) > 1
        out.append(g)
    out.sort(key=lambda g: (g["quarter"], g["firstDate"] is None, g["firstDate"] or date.max,
                            voucher_sort_key(g["rows"][0]["voucherNo"]), g["rows"][0]["magazine"]))
    # Quarter-க்குள் வரிசை எண் — ஒன்றுக்கு மேல் இருந்தால் மட்டும் "1st/2nd Payment" என்று காட்டும்
    per_q = {}
    for g in out:
        per_q[g["quarter"]] = per_q.get(g["quarter"], 0) + 1
    seen = {}
    n = 0
    for g in out:
        seen[g["quarter"]] = seen.get(g["quarter"], 0) + 1
        g["ordinal"] = seen[g["quarter"]]
        g["paymentCount"] = per_q[g["quarter"]]
        g["ordinalLabel"] = (_ordinal(g["ordinal"]) + " Payment") if per_q[g["quarter"]] > 1 else ""
        g["displayLabel"] = g["quarterLabel"] + ((" — " + g["ordinalLabel"]) if g["ordinalLabel"] else "")
        for x in g["rows"]:
            n += 1
            x["sno"] = n
            x.pop("invoiceDateRaw", None)
    return {
        "magazine": magazine, "vendorCode": (m["tnpfts_code"] or "").strip(), "vendorName": vendor,
        "vendorMagazines": names, "quarters": ql, "groups": out, "count": n,
        "totalRequested": sum(g["totalRequested"] for g in out),
        "totalPaid": sum(g["totalPaid"] for g in out),
    }


def _paid_report_flat(rep):
    for g in rep["groups"]:
        for x in g["rows"]:
            yield g, x


def paid_report_csv(rep):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["S.NO", "QUARTER"] + PAID_REPORT_HEADERS[1:])
    for g, x in _paid_report_flat(rep):
        w.writerow([x["sno"], g["displayLabel"], x["magazine"], x["invoiceNo"], x["invoiceDate"],
                    "%.2f" % x["requestedAmt"], "%.2f" % x["paidAmt"], x["paidDate"], x["transactionNo"]])
    w.writerow(["", "", "", "TOTAL", "", "%.2f" % rep["totalRequested"], "%.2f" % rep["totalPaid"], "", ""])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def paid_report_xlsx(rep):
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Paid Details")
    title = wb.add_format({"bold": True, "font_size": 15})
    sub = wb.add_format({"bold": True, "font_size": 11})
    head = wb.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#1E4D8C", "border": 1,
                          "text_wrap": True, "align": "center", "valign": "vcenter"})
    txt = wb.add_format({"border": 1, "valign": "top"})
    ctr = wb.add_format({"border": 1, "valign": "top", "align": "center"})
    num = wb.add_format({"border": 1, "valign": "top", "num_format": "#,##0.00"})
    tot_l = wb.add_format({"bold": True, "border": 1, "bg_color": "#EEF2F8", "align": "right"})
    tot_n = wb.add_format({"bold": True, "border": 1, "bg_color": "#EEF2F8", "num_format": "#,##0.00"})
    tot_b = wb.add_format({"bold": True, "border": 1, "bg_color": "#EEF2F8"})
    for i, wd in enumerate([7, 24, 36, 20, 14, 18, 16, 14, 30]):
        ws.set_column(i, i, wd)
    qtxt = ", ".join(quarter_label(q) for q in rep["quarters"]) or "All Quarters"
    ws.write(0, 0, "District Library Office, Dindigul", title)
    ws.write(1, 0, "PAYMENT DETAILS — " + rep["vendorName"] + (" (Vendor Code: " + rep["vendorCode"] + ")" if rep["vendorCode"] else ""), sub)
    ws.write(2, 0, "Quarter: " + qtxt + "    Date: " + datetime.now().strftime("%d/%m/%Y"))
    hdr = ["S.NO", "QUARTER"] + PAID_REPORT_HEADERS[1:]
    ws.set_row(4, 32)
    for c, h in enumerate(hdr):
        ws.write(4, c, h, head)
    r0 = 5
    for i, (g, x) in enumerate(_paid_report_flat(rep)):
        r = r0 + i
        ws.write_number(r, 0, x["sno"], ctr)
        ws.write_string(r, 1, g["displayLabel"], ctr)
        ws.write_string(r, 2, x["magazine"], txt)
        ws.write_string(r, 3, x["invoiceNo"], ctr)
        ws.write_string(r, 4, x["invoiceDate"], ctr)
        ws.write_number(r, 5, x["requestedAmt"], num)
        ws.write_number(r, 6, x["paidAmt"], num)
        ws.write_string(r, 7, x["paidDate"], ctr)
        ws.write_string(r, 8, x["transactionNo"], txt)
    n = rep["count"]
    tr = r0 + n
    for c in range(9):
        ws.write(tr, c, "", tot_b)
    ws.write(tr, 4, "TOTAL", tot_l)
    if n:
        ws.write_formula(tr, 5, "=SUM(F%d:F%d)" % (r0 + 1, r0 + n), tot_n, rep["totalRequested"])
        ws.write_formula(tr, 6, "=SUM(G%d:G%d)" % (r0 + 1, r0 + n), tot_n, rep["totalPaid"])
    ws.freeze_panes(5, 0)
    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)
    wb.close()
    return buf.getvalue()


def _paid_report_pdf_combined(pdf, rep, para):
    """ஒன்றுக்கு மேற்பட்ட தொகை வழங்கல் இருந்தால்: கடித வாசகம் (Date, தலைப்பு, Ref, Kindly see below...)
    முதல் பக்கத்தில் மட்டும்; அடுத்தடுத்த Quarter-கள் அதே அட்டவணையில் தொடரும் (Quarter பட்டை + மொத்தம்);
    கையொப்பம் (Thanking and Regards...) கடைசியில் மட்டும்."""
    from fpdf.fonts import FontFace

    groups = rep["groups"]
    pdf.add_page()
    pdf.set_font("Tamil", "", 10.5)
    pdf.cell(0, 6, "Date: " + datetime.now().strftime("%d/%m/%Y"), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Tamil", "B", 21)
    pdf.cell(0, 11, "District Library Office, Dindigul", align="C", new_x="LMARGIN", new_y="NEXT")
    y = pdf.get_y()
    pdf.set_line_width(0.9)
    pdf.line(15, y + 0.5, 195, y + 0.5)
    pdf.ln(3)

    quarters = []
    for g in groups:
        if g["quarter"] and g["quarter"] not in quarters:
            quarters.append(g["quarter"])
    title = "PAYMENT CLEARED INTIMATION"
    if quarters:
        title += " FOR " + (quarters[0] if len(quarters) == 1 else quarters[0] + " TO " + quarters[-1])
    pdf.set_font("Tamil", "B", 12.5)
    pdf.multi_cell(0, 7, title, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    mags = sorted({x["magazine"] for g in groups for x in g["rows"]})
    pdf.set_font("Tamil", "", 11)
    pdf.cell(0, 6.5, "Sir,", new_x="LMARGIN", new_y="NEXT")
    para([("Ref: Your Invoices listed in the tables below, for the supply of the magazines ", False, 11),
          (", ".join(mags), True, 11)])
    para([("Sir,", False, 11)], h=6.2)
    pdf.set_y(pdf.get_y() - 2.5)
    para([("Kindly see below the quarter-wise details of the payments (total ", False, 11),
          ("Rs." + indian_grouping(rep["totalPaid"]), True, 11),
          (") transferred to your Bank Account from ", False, 11),
          ("The District Library Officer, Dindigul", True, 11),
          (", for the supply of the above magazines, with the break-up shown against each magazine. "
           "We kindly request you to acknowledge receipt of the same.", False, 11)])
    pdf.ln(3)

    head_style = FontFace(color=(255, 255, 255), fill_color=(30, 77, 140), emphasis="BOLD")
    band_style = FontFace(fill_color=(225, 232, 245), emphasis="BOLD")
    tot_style = FontFace(fill_color=(238, 242, 248), emphasis="BOLD")
    pdf.set_font("Tamil", "", 8.5)
    widths = (11, 35, 21, 22, 21, 20, 22, 28)
    aligns = ("CENTER", "LEFT", "CENTER", "CENTER", "RIGHT", "RIGHT", "CENTER", "LEFT")
    heads = ["S.NO", "MAGAZINE", "INVOICE NUMBER", "INVOICE DATE", "REQUESTED AMOUNT",
             "PAID AMOUNT", "PAID DATE", "BANK TRANSACTION NUMBER"]
    with pdf.table(col_widths=widths, text_align=aligns, headings_style=head_style,
                   line_height=5.4, padding=1.2, borders_layout="ALL") as table:
        h = table.row()
        for t in heads:
            h.cell(t, align="C")
        for g in groups:
            band = "QUARTER: " + g["quarterLabel"]
            if g["ordinalLabel"]:
                band += "  |  " + g["ordinalLabel"].upper()
            if g["multi"]:
                band += "  |  GROUPED PAYMENT (%d magazines)" % len(g["rows"])
            b = table.row()
            b.cell(band, colspan=8, align="L", style=band_style)
            for x in g["rows"]:
                r = table.row()
                r.cell(str(x["sno"]))
                r.cell(x["magazine"])
                r.cell(x["invoiceNo"] or "-")
                r.cell(x["invoiceDate"] or "-")
                r.cell(indian_grouping(x["requestedAmt"]))
                r.cell(indian_grouping(x["paidAmt"]))
                r.cell(x["paidDate"] or "-")
                r.cell(x["transactionNo"] or "-")
            if g["multi"]:
                tr = table.row()
                tr.cell("GROUP TOTAL", colspan=4, align="R", style=tot_style)
                tr.cell(indian_grouping(g["totalRequested"]), align="R", style=tot_style)
                tr.cell(indian_grouping(g["totalPaid"]), align="R", style=tot_style)
                tr.cell("", colspan=2, style=tot_style)
        gt = table.row()
        gt.cell("GRAND TOTAL", colspan=4, align="R", style=tot_style)
        gt.cell(indian_grouping(rep["totalRequested"]), align="R", style=tot_style)
        gt.cell(indian_grouping(rep["totalPaid"]), align="R", style=tot_style)
        gt.cell("", colspan=2, style=tot_style)

    # கையொப்பம் — கடைசிப் பக்கத்தில் மட்டும் (அட்டவணைக்குக் கீழே இடம் போதாவிட்டால் புதிய பக்கம்)
    pdf.ln(12)
    if pdf.get_y() > 255:
        pdf.add_page()
    pdf.set_font("Tamil", "", 11)
    pdf.cell(0, 6, "Thanking and Regards,", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "District Library Officer", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, "Dindigul", align="R", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def paid_report_pdf(rep):
    """ஒவ்வொரு தொகை வழங்கலுக்கும் (Quarter + Bank Transaction No) ஒரு Payment Cleared Intimation கடிதம்,
    publisher-க்குச் செல்லும் mail PDF போலவே (A4 portrait, ஆங்கிலம்). Vendor Code / Bill Set காட்டப்படாது.
    • ஒரு இதழ்  → ஒரே A4 பக்கம்; இதழ் பெயர் பெரிய எழுத்தில்
    • பல இதழ்கள் → அட்டவணை; ஒரே A4-ல் அடங்கும், இடம் போதாவிட்டால் அடுத்த பக்கத்துக்குத் தொடரும்"""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 16, 15)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_font("Tamil", "", TAMIL_FONT_PATH)
    pdf.add_font("Tamil", "B", TAMIL_BOLD_FONT_PATH)
    pdf.set_text_shaping(True)

    def para(parts, h=6.2):
        """parts = [(text, bold, size), ...] — ஒரே பத்தியில் கலந்த எழுத்து நடை."""
        for text, bold, size in parts:
            pdf.set_font("Tamil", "B" if bold else "", size)
            pdf.write(h, text)
        pdf.set_font("Tamil", "", 11)
        pdf.ln(h + 2.5)

    if not rep["groups"]:
        pdf.add_page()
        pdf.set_font("Tamil", "B", 21)
        pdf.cell(0, 11, "District Library Office, Dindigul", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(6)
        pdf.set_font("Tamil", "", 12)
        pdf.cell(0, 8, "No paid records found for the selected quarter(s).", align="C", new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())

    if len(rep["groups"]) > 1:
        return _paid_report_pdf_combined(pdf, rep, para)

    for g in rep["groups"]:
        rows = g["rows"]
        multi = g["multi"]
        pdf.add_page()

        dates = g["paidDates"] or [datetime.now().strftime("%d/%m/%Y")]
        pdf.set_font("Tamil", "", 10.5)
        pdf.cell(0, 6, "Date: " + ", ".join(dates), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Tamil", "B", 21)
        pdf.cell(0, 11, "District Library Office, Dindigul", align="C", new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y()
        pdf.set_line_width(0.9)
        pdf.line(15, y + 0.5, 195, y + 0.5)
        pdf.ln(3)
        title = "PAYMENT CLEARED INTIMATION FOR " + g["quarter"]
        if g["ordinalLabel"]:
            title += " (" + g["ordinalLabel"].upper() + ")"
        pdf.set_font("Tamil", "B", 12.5)
        pdf.multi_cell(0, 7, title, align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        pdf.set_font("Tamil", "", 11)
        pdf.cell(0, 6.5, "Sir,", new_x="LMARGIN", new_y="NEXT")
        if multi:
            para([("Ref: Your Invoices listed in the table below, for the supply of the magazines ", False, 11),
                  (", ".join(g["magazines"]), True, 11)])
            para([("Sir,", False, 11)], h=6.2)
            pdf.set_y(pdf.get_y() - 2.5)
            para([("Kindly see below the details of the ", False, 11),
                  ("single consolidated payment of Rs." + indian_grouping(g["totalPaid"]), True, 11),
                  (" transferred to your Bank Account from ", False, 11),
                  ("The District Library Officer, Dindigul", True, 11),
                  (", for the supply of the above magazines, with the break-up shown against each magazine. "
                   "We kindly request you to acknowledge receipt of the same.", False, 11)])
        else:
            x0 = rows[0]
            BIG = 15
            para([("Ref: Your Invoice Number ", False, 11), (x0["invoiceNo"] or "---", True, 11),
                  (" dated ", False, 11), (x0["invoiceDate"] or "---", True, 11),
                  (" for the supply of Magazine ", False, 11), (x0["magazine"], True, BIG)], h=7.5)
            para([("Sir,", False, 11)], h=6.2)
            pdf.set_y(pdf.get_y() - 2.5)
            para([("Kindly see below the details of the payment transferred to your Bank Account from ", False, 11),
                  ("The District Library Officer, Dindigul", True, 11),
                  (", for the supply of the magazine ", False, 11), (x0["magazine"], True, BIG),
                  (", as per the invoice under reference cited. "
                   "We kindly request you to acknowledge receipt of the same.", False, 11)], h=7.5)
        pdf.ln(3)

        head_style = FontFace(color=(255, 255, 255), fill_color=(30, 77, 140), emphasis="BOLD")
        tot_style = FontFace(fill_color=(238, 242, 248), emphasis="BOLD")
        pdf.set_font("Tamil", "", 8.5)
        if multi:
            widths = (11, 35, 21, 22, 21, 20, 22, 28)
            aligns = ("CENTER", "LEFT", "CENTER", "CENTER", "RIGHT", "RIGHT", "CENTER", "LEFT")
            heads = ["S.NO", "MAGAZINE", "INVOICE NUMBER", "INVOICE DATE", "REQUESTED AMOUNT",
                     "PAID AMOUNT", "PAID DATE", "BANK TRANSACTION NUMBER"]
        else:
            aligns = ("CENTER", "CENTER", "CENTER", "RIGHT", "RIGHT", "CENTER", "LEFT")
            heads = ["S.NO", "INVOICE NUMBER", "INVOICE DATE", "REQUESTED AMOUNT", "PAID AMOUNT",
                     "PAID DATE", "BANK TRANSACTION NUMBER"]
            widths = (12, 30, 26, 30, 28, 26, 28)
        with pdf.table(col_widths=widths, text_align=aligns, headings_style=head_style,
                       line_height=5.4, padding=1.2, borders_layout="ALL") as table:
            h = table.row()
            for t in heads:
                h.cell(t, align="C")
            for i, x in enumerate(rows, 1):
                r = table.row()
                r.cell(str(i))
                if multi:
                    r.cell(x["magazine"])
                r.cell(x["invoiceNo"] or "-")
                r.cell(x["invoiceDate"] or "-")
                r.cell(indian_grouping(x["requestedAmt"]))
                r.cell(indian_grouping(x["paidAmt"]))
                r.cell(x["paidDate"] or "-")
                r.cell(x["transactionNo"] or "-")
            if multi:
                tr = table.row()
                tr.cell("TOTAL", colspan=4, align="R", style=tot_style)
                tr.cell(indian_grouping(g["totalRequested"]), align="R", style=tot_style)
                tr.cell(indian_grouping(g["totalPaid"]), align="R", style=tot_style)
                tr.cell("", colspan=2, style=tot_style)

        pdf.ln(12)
        if pdf.get_y() > 255:
            pdf.add_page()
        pdf.set_font("Tamil", "", 11)
        pdf.cell(0, 6, "Thanking and Regards,", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, "District Library Officer", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, "Dindigul", align="R", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


# =============================================================================
# பதிவுத் திருத்தம் (Master → Admin) — ஏற்கனவே பதிவான Payments தரவைச் சரிசெய்ய
#   • இதழ் பெயர் Master-உடன் பொருந்தாதவற்றைக் கண்டுபிடித்து சரிசெய்தல் (தனி / தானாக)
#   • ஒரு Payment பதிவின் Invoice No / Date, Requested / Paid Amount, Paid Date, Bank Txn No,
#     இதழ் பெயர், Quarter ஆகியவற்றைத் திருத்துதல்
#   ஒவ்வொரு மாற்றமும் data_fix_log table-ல் (பழைய → புதிய மதிப்பு) பதிவாகும்.
# =============================================================================
def _fx_log(cur, action, detail):
    cur.execute(
        "CREATE TABLE IF NOT EXISTS data_fix_log ("
        " id SERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        " username TEXT, action TEXT, detail TEXT)"
    )
    u = (current_user() or {}).get("username", "")
    cur.execute(
        "INSERT INTO data_fix_log (username, action, detail) VALUES (%s,%s,%s)",
        (u, action, json.dumps(detail, ensure_ascii=False, default=str)),
    )


def _name_issues(sv):
    """ஒரு பெயரில் கண்ணுக்குத் தெரியாத / இடைவெளிக் குறைபாடுகளின் பட்டியல்."""
    import unicodedata
    out = []
    if sv != sv.strip():
        out.append("முன் / பின் இடைவெளி")
    if "\xa0" in sv:
        out.append("Non-breaking space")
    if any(c in sv for c in "\u200b\u200c\u200d\ufeff"):
        out.append("கண்ணுக்குத் தெரியாத (zero-width) எழுத்து")
    if "  " in sv.strip():
        out.append("இரட்டை இடைவெளி")
    if unicodedata.normalize("NFC", sv) != sv:
        out.append("Unicode வடிவ (NFC/NFD) வேறுபாடு")
    return out


def _name_diff_reason(pay_name, master_name):
    a = _name_issues(pay_name)
    b = _name_issues(master_name or "")
    parts = []
    if a:
        parts.append("Payments பெயரில்: " + ", ".join(a))
    if b:
        parts.append("Master பெயரில்: " + ", ".join(b))
    if parts:
        return " | ".join(parts)
    if master_name and _name_key(pay_name) == _name_key(master_name):
        return "எழுத்து அளவு / இடைவெளி வேறுபாடு"
    if re.search(r"\s*[-–]\s*\d+\s*$", pay_name or ""):
        return "பெயர் முடிவில் '-எண்' உள்ளது"
    return "எழுத்துகள் வேறுபடுகின்றன" if master_name else "Master-ல் ஒத்த பெயர் இல்லை"


def _fx_scan(cur):
    import difflib
    cur.execute("SELECT name FROM magazines")
    master = sorted({r["name"] for r in cur.fetchall()})
    mset = set(master)
    mkeys = {}
    for n in master:
        mkeys.setdefault(_name_key(n), n)
    cur.execute("SELECT id, magazine, quarter, part, months, paid_amt FROM payments ORDER BY magazine, quarter, part")
    pays = cur.fetchall()
    existing = {(r["magazine"], r["quarter"], r["part"]) for r in pays}
    cur.execute("SELECT name, periodicity FROM magazines")
    tick_names = {x["name"] for x in cur.fetchall() if uses_month_tick(x["periodicity"])}
    items = []
    for r in pays:
        nm = r["magazine"]
        if nm in mset:
            continue
        k = _name_key(nm)
        target = mkeys.get(k)
        exact = bool(target)
        if not target:
            k2 = _name_key(re.sub(r"\s*[-–]\s*\d+\s*$", "", nm or ""))
            target = mkeys.get(k2)
        if not target:
            best = difflib.get_close_matches(k, list(mkeys), n=1, cutoff=0.75)
            target = mkeys[best[0]] if best else None
        conflict = bool(target) and (target, r["quarter"], r["part"]) in existing
        # முரண் உள்ளபோது: இது அதே இதழின் அடுத்த Part ஆக மாற்றக்கூடியதா? (மாத வாரியாகப் பிரிக்கும் இதழ் மட்டும்)
        to_part = None
        if conflict and target in tick_names:
            tp = [x for x in pays if x["magazine"] == target and x["quarter"] == r["quarter"]]
            used_t = {m for x in tp for m in parse_months(x["months"])}
            free = [m for m in (1, 2, 3) if m not in used_t]
            names_q = quarter_month_names(r["quarter"])
            to_part = {
                "nextPart": max(x["part"] for x in tp) + 1,
                "monthNames": names_q, "usedMonths": sorted(used_t), "freeMonths": free,
                "existing": [{"part": x["part"], "monthsText": month_span_text(r["quarter"], x["months"])} for x in tp],
            }
        items.append({
            "id": r["id"], "magazine": nm, "quarter": r["quarter"],
            "toPart": to_part,
            "paidAmt": float(r["paid_amt"] or 0),
            "suggestion": target or "", "exact": exact, "conflict": conflict,
            "auto": bool(exact and not conflict),
            "reason": _name_diff_reason(nm, target),
            "codepoints": len(nm or ""),
        })
    names = sorted(mset | {r["magazine"] for r in pays})
    return {
        "items": items,
        "masterNames": master,
        "allNames": [{"name": n, "inMaster": n in mset} for n in names],
    }


def _fx_move_related(cur, old_mag, old_q, new_mag, new_q, old_part=1, new_part=None):
    """payments பதிவு இதழ்/Quarter/Part மாறும்போது vouchers, despatch_nonsupply பதிவுகளையும் மாற்றும்."""
    new_part = old_part if new_part is None else new_part
    if (old_mag, old_q, old_part) == (new_mag, new_q, new_part):
        return
    cur.execute("UPDATE vouchers SET magazine=%s, quarter=%s, part=%s WHERE magazine=%s AND quarter=%s AND part=%s",
                (new_mag, new_q, new_part, old_mag, old_q, old_part))
    if (old_mag, old_q) == (new_mag, new_q):
        return
    cur.execute("SELECT 1 FROM despatch_nonsupply WHERE magazine=%s AND quarter=%s", (new_mag, new_q))
    if not cur.fetchone():
        cur.execute("UPDATE despatch_nonsupply SET magazine=%s, quarter=%s WHERE magazine=%s AND quarter=%s",
                    (new_mag, new_q, old_mag, old_q))


def _fx_rename_one(cur, pid, new_name):
    """-> (ok, message)"""
    cur.execute("SELECT id, magazine, quarter, part FROM payments WHERE id=%s", (pid,))
    row = cur.fetchone()
    if not row:
        return False, "பதிவு கிடைக்கவில்லை"
    old, q = row["magazine"], row["quarter"]
    cur.execute("SELECT 1 FROM magazines WHERE name=%s", (new_name,))
    if not cur.fetchone():
        return False, "'%s' Master-ல் இல்லை" % new_name
    if old == new_name:
        return True, "மாற்றம் தேவையில்லை"
    cur.execute("SELECT 1 FROM payments WHERE magazine=%s AND quarter=%s AND part=%s AND id<>%s",
                (new_name, q, row["part"], pid))
    if cur.fetchone():
        return False, ("'%s' (%s) பதிவு ஏற்கனவே உள்ளது — இணைக்க முடியாது. "
                       "(இது அடுத்த Part எனில் \"Part ஆக மாற்று\" பயன்படுத்தவும்.)") % (new_name, q)
    cur.execute("UPDATE payments SET magazine=%s, updated_at=now() WHERE id=%s", (new_name, pid))
    _fx_move_related(cur, old, q, new_name, q, row["part"], row["part"])
    _fx_log(cur, "rename_magazine", {"id": pid, "quarter": q, "from": old, "to": new_name})
    return True, "சரிசெய்யப்பட்டது"


@app.route("/api/admin/data-fix/scan")
def api_fx_scan():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            res = _fx_scan(cur)
        return jsonify({"success": True, **res})
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/data-fix/rename", methods=["POST"])
def api_fx_rename():
    """body: {items:[{id, to}]}  — ஒவ்வொரு பதிவின் இதழ் பெயரை Master பெயருக்கு மாற்றும்."""
    data = request.get_json(force=True)
    items = data.get("items") or []
    conn = get_conn()
    try:
        fixed, skipped = 0, []
        with conn.cursor() as cur:
            for it in items:
                ok, msg = _fx_rename_one(cur, int(it.get("id") or 0), (it.get("to") or "").strip())
                if ok:
                    fixed += 1
                else:
                    skipped.append({"id": it.get("id"), "message": msg})
        conn.commit()
        return jsonify({"success": True, "fixed": fixed, "skipped": skipped})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/data-fix/auto-rename", methods=["POST"])
def api_fx_auto_rename():
    """பெயர் Master பெயருடன் இடைவெளி / Unicode / எழுத்து அளவில் மட்டும் வேறுபடும் பதிவுகளை மட்டும் தானாகச் சரிசெய்யும்.
    ('-1' போன்ற விகுதி, எழுத்துப்பிழை உள்ளவை தொடப்படாது — அவற்றை நீங்களே தேர்ந்து சரிசெய்ய வேண்டும்.)"""
    conn = get_conn()
    try:
        fixed, skipped = 0, []
        with conn.cursor() as cur:
            scan = _fx_scan(cur)
            for it in scan["items"]:
                if not it["exact"]:
                    continue
                ok, msg = _fx_rename_one(cur, it["id"], it["suggestion"])
                if ok:
                    fixed += 1
                else:
                    skipped.append({"id": it["id"], "magazine": it["magazine"], "quarter": it["quarter"], "message": msg})
        conn.commit()
        return jsonify({"success": True, "fixed": fixed, "skipped": skipped})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/data-fix/to-part", methods=["POST"])
def api_fx_to_part():
    """'Civil Service Chronicle-1' போன்ற தனிப் பெயர்ப் பதிவை, Master இதழின் அடுத்த Part ஆக மாற்றும்.
    body: {id, to?, months?:[1,2,3 துணைக்குழு]}
      - to      : Master இதழ் பெயர் (விடுத்தால் scan பரிந்துரை — பெயரின் முடிவில் உள்ள '-எண்' நீக்கப்பட்டது)
      - months  : இந்தப் பதிவு எந்த மாதங்களுக்கு (விடுத்தால் மற்ற Part-களில் இல்லாத மீதமுள்ள மாதங்கள்)
    - அதே இதழ் + Quarter-ல் மற்ற Part-களில் உள்ள மாதங்களைத் தேர்ந்தெடுக்க முடியாது
    - vouchers வரிசைகளும் புதிய Part எண்ணுடன் நகரும்; Quarter Non-supply ஒரே முறை மட்டும் கழிய சமன் செய்யப்படும்
    - தொகை வழங்கிய பதிவின் தொகைகள் மாறாது (பெயர் / Part / மாதம் மட்டும் மாறும்)"""
    data = request.get_json(force=True) or {}
    try:
        pid = int(data.get("id") or 0)
    except (TypeError, ValueError):
        pid = 0
    to = (data.get("to") or "").strip()
    months_in = data.get("months")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE id=%s", (pid,))
            row = cur.fetchone()
            if not row:
                return jsonify({"success": False, "message": "பதிவு கிடைக்கவில்லை."}), 404
            old_mag, q, old_part = row["magazine"], row["quarter"], row["part"]
            if not to:
                to = re.sub(r"\s*[-–]\s*\d+\s*$", "", old_mag or "").strip()
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (to,))
            m = cur.fetchone()
            if not m:
                return jsonify({"success": False, "message": "'%s' Master-ல் இல்லை." % to}), 400
            if not uses_month_tick(m["periodicity"]):
                return jsonify({"success": False, "message":
                                "'%s' மாத வாரியாகப் பிரித்து அனுப்பும் வகை இதழ் அல்ல (Monthly / Fortnightly / Weekly / Triweekly மட்டும்)." % to}), 400
            cur.execute("SELECT id, part, months FROM payments WHERE magazine=%s AND quarter=%s AND id<>%s ORDER BY part",
                        (to, q, pid))
            others = cur.fetchall()
            used = {x for o in others for x in parse_months(o["months"])}
            names = quarter_month_names(q)
            if months_in:
                months = sorted({x for x in (q_int(v) for v in months_in) if 1 <= x <= 3})
            else:
                months = [x for x in (1, 2, 3) if x not in used]
            if not months:
                return jsonify({"success": False, "message":
                                "இந்த Quarter-ன் எல்லா மாதங்களும் ஏற்கனவே மற்ற Part-களில் பதிவாகியுள்ளன — இணைக்க மாதம் இல்லை."}), 409
            overlap = sorted(used & set(months))
            if overlap:
                return jsonify({"success": False, "message":
                                "இந்த மாதங்கள் (%s) ஏற்கனவே மற்றொரு Part-ல் உள்ளன." % ", ".join(names[x - 1] for x in overlap)}), 409
            new_part = (max(o["part"] for o in others) + 1) if others else 1
            months_db = None if months == [1, 2, 3] else ",".join(str(x) for x in months)
            cur.execute("UPDATE payments SET magazine=%s, part=%s, months=%s, updated_at=now() WHERE id=%s",
                        (to, new_part, months_db, pid))
            _fx_move_related(cur, old_mag, q, to, q, old_part, new_part)
            changed = rebalance_non_supply(cur, to, q)
            _fx_log(cur, "to_part", {"id": pid, "quarter": q, "from": old_mag, "to": to,
                                      "oldPart": old_part, "newPart": new_part, "months": months_db,
                                      "rebalanced": changed})
        conn.commit()
        note = " Non-supply கழிவு Part %s-ல் சமன் செய்யப்பட்டது." % ", ".join(str(x) for x in changed) if changed else ""
        return jsonify({"success": True, "part": new_part,
                        "message": "'%s' → '%s' · Part %d (%s) ஆக மாற்றப்பட்டது.%s" % (
                            old_mag, to, new_part, month_span_text(q, months_db), note)})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/admin/data-fix/payments")
def api_fx_payments():
    magazine = request.args.get("magazine", "")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (magazine,))
            mg = cur.fetchone()
            month_tick = uses_month_tick(mg["periodicity"] if mg else "")
            cur.execute(
                "SELECT id, magazine, quarter, part, months, invoice_no, invoice_date, requested_amt, paid_amt, "
                "       payment_date, transaction_no, voucher_no, bill_set_no FROM payments "
                "WHERE magazine=%s ORDER BY quarter, part", (magazine,))
            db_rows = cur.fetchall()
            rows = []
            for r in db_rows:
                used_others = sorted({m for x in db_rows
                                      if x["quarter"] == r["quarter"] and x["id"] != r["id"]
                                      for m in parse_months(x["months"])})
                rows.append({
                    "id": r["id"], "magazine": r["magazine"], "quarter": r["quarter"],
                    "part": r["part"], "monthsText": month_span_text(r["quarter"], r["months"]),
                    "partial": parse_months(r["months"]) != [1, 2, 3],
                    "months": parse_months(r["months"]), "monthNames": quarter_month_names(r["quarter"]),
                    "monthTick": month_tick, "usedByOthers": used_others,
                    "invoiceNo": r["invoice_no"] or "",
                    "invoiceDate": r["invoice_date"].isoformat() if r["invoice_date"] else "",
                    "requestedAmt": float(r["requested_amt"] or 0), "paidAmt": float(r["paid_amt"] or 0),
                    "paymentDate": r["payment_date"].isoformat() if r["payment_date"] else "",
                    "transactionNo": r["transaction_no"] or "", "voucherNo": r["voucher_no"] or "",
                    "billSetNo": (r["bill_set_no"] or "").strip(),
                })
        return jsonify({"success": True, "rows": rows})
    finally:
        conn.close()


def _fx_date(v):
    v = (v or "").strip()
    if not v:
        return None
    for f in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, f).date()
        except ValueError:
            pass
    raise ValueError("தேதி வடிவம் தவறு: " + v)


def _fx_amt(v, label):
    t = str(v if v is not None else "").replace(",", "").strip()
    if not t:
        return 0.0
    try:
        n = round(float(t), 2)
    except ValueError:
        raise ValueError(label + " எண்ணாக இருக்க வேண்டும்")
    if n < 0:
        raise ValueError(label + " குறைவாக இருக்கக் கூடாது")
    return n


@app.route("/api/admin/data-fix/update-payment", methods=["POST"])
def api_fx_update_payment():
    data = request.get_json(force=True)
    try:
        pid = int(data.get("id") or 0)
    except (TypeError, ValueError):
        pid = 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payments WHERE id=%s", (pid,))
            old = cur.fetchone()
            if not old:
                return jsonify({"success": False, "message": "பதிவு கிடைக்கவில்லை."}), 404
            try:
                invoice_date = _fx_date(data.get("invoiceDate"))
                payment_date = _fx_date(data.get("paymentDate"))
                requested = _fx_amt(data.get("requestedAmt"), "Requested Amount")
                paid = _fx_amt(data.get("paidAmt"), "Paid Amount")
            except ValueError as ve:
                return jsonify({"success": False, "message": str(ve)}), 400
            invoice_no = (data.get("invoiceNo") or "").strip() or None
            txn = (data.get("transactionNo") or "").strip() or None
            new_mag = (data.get("magazine") or old["magazine"]).strip()
            new_q = (data.get("quarter") or old["quarter"]).strip()
            # Voucher No / Bill Set No / மாதங்கள் — அனுப்பப்பட்டால் மட்டுமே மாற்றப்படும்
            voucher_no = ((data.get("voucherNo") or "").strip() or None) if "voucherNo" in data else old["voucher_no"]
            bill_set_no = ((str(data.get("billSetNo") or "")).strip() or None) if "billSetNo" in data else old["bill_set_no"]
            months_req = None
            if isinstance(data.get("months"), list):
                months_req = sorted({x for x in (q_int(v) for v in data.get("months")) if 1 <= x <= 3})
                if not months_req:
                    return jsonify({"success": False, "message": "குறைந்தது ஒரு மாதத்தைத் தேர்ந்தெடுக்கவும்."}), 400

            if new_mag != old["magazine"]:
                cur.execute("SELECT 1 FROM magazines WHERE name=%s", (new_mag,))
                if not cur.fetchone():
                    return jsonify({"success": False, "message": "'%s' Master-ல் இல்லை." % new_mag}), 400
            if new_q != old["quarter"] and not re.match(r"^\d{4}-\d{4}-Q[1-4]$", new_q):
                return jsonify({"success": False, "message": "Quarter வடிவம் தவறு (எ.கா. 2025-2026-Q1)."}), 400
            if (new_mag, new_q) != (old["magazine"], old["quarter"]):
                cur.execute("SELECT 1 FROM payments WHERE magazine=%s AND quarter=%s AND part=%s AND id<>%s",
                            (new_mag, new_q, old["part"], pid))
                if cur.fetchone():
                    return jsonify({"success": False,
                                    "message": "'%s' (%s) பதிவு ஏற்கனவே உள்ளது." % (new_mag, new_q)}), 409

            # ---- Voucher No: அதே Quarter-ல் மற்றொரு பதிவில் அதே எண் இருக்கக்கூடாது ----
            if voucher_no and voucher_no != (old["voucher_no"] or "").strip() or \
                    (voucher_no and (new_mag, new_q) != (old["magazine"], old["quarter"])):
                cur.execute("SELECT magazine, part, months, quarter FROM payments "
                            "WHERE quarter=%s AND TRIM(COALESCE(voucher_no,''))=%s AND id<>%s",
                            (new_q, voucher_no, pid))
                dup = cur.fetchone()
                if dup:
                    return jsonify({"success": False, "message":
                                    "Voucher No %s (%s) ஏற்கனவே '%s'-க்கு உள்ளது." % (
                                        voucher_no, new_q, row_label(dup))}), 409

            # ---- Bill Set No: BENEFICIARY / BUSINESS VENDOR கலக்கக்கூடாது ----
            if bill_set_no and not vendor_mix_allowed(new_q):
                cur.execute("SELECT tnpfts_code FROM magazines WHERE name=%s", (new_mag,))
                mv = cur.fetchone()
                my_type = classify_vendor_code(mv["tnpfts_code"] if mv else "")
                cur.execute(
                    "SELECT p.magazine, m.tnpfts_code FROM payments p LEFT JOIN magazines m ON m.name=p.magazine "
                    "WHERE p.quarter=%s AND p.bill_set_no=%s AND p.magazine<>%s", (new_q, bill_set_no, new_mag))
                for o in cur.fetchall():
                    o_type = classify_vendor_code(o["tnpfts_code"])
                    if o_type and my_type and o_type != my_type:
                        return jsonify({"success": False, "message":
                                        "Bill Set %s (%s)-ல் ஏற்கனவே '%s' (%s) உள்ளது. '%s' (%s) — BENEFICIARY மற்றும் "
                                        "BUSINESS VENDOR ஒரே Set-ல் கலக்க முடியாது." % (
                                            bill_set_no, new_q, o["magazine"], o_type, new_mag, my_type)}), 400

            # ---- மாதங்கள் (Part-ன் மாத விவரம்) ----
            months_db, months_changed, months_note = old["months"], False, ""
            if months_req is not None and months_req != parse_months(old["months"]):
                cur.execute("SELECT periodicity FROM magazines WHERE name=%s", (new_mag,))
                mp = cur.fetchone()
                if months_req != [1, 2, 3] and not uses_month_tick(mp["periodicity"] if mp else ""):
                    return jsonify({"success": False, "message":
                                    "இந்த இதழ் வகைக்கு மாத வாரியாகப் பிரிக்கும் வசதி இல்லை."}), 400
                cur.execute("SELECT part, months FROM payments WHERE magazine=%s AND quarter=%s AND id<>%s",
                            (new_mag, new_q, pid))
                used_by_others = set()
                for o in cur.fetchall():
                    used_by_others |= set(parse_months(o["months"]))
                overlap = sorted(used_by_others & set(months_req))
                if overlap:
                    nm = quarter_month_names(new_q)
                    return jsonify({"success": False, "message":
                                    "இந்த மாதங்கள் (%s) ஏற்கனவே மற்றொரு Part-ல் உள்ளன." % ", ".join(nm[x - 1] for x in overlap)}), 409
                months_db = None if months_req == [1, 2, 3] else ",".join(str(x) for x in months_req)
                months_changed = True

            cur.execute(
                "UPDATE payments SET magazine=%s, quarter=%s, invoice_no=%s, invoice_date=%s, requested_amt=%s, "
                "paid_amt=%s, payment_date=%s, transaction_no=%s, voucher_no=%s, bill_set_no=%s, months=%s, "
                "updated_at=now() WHERE id=%s",
                (new_mag, new_q, invoice_no, invoice_date, requested, paid, payment_date, txn,
                 voucher_no, bill_set_no, months_db, pid))

            if months_changed:
                old_cnt, new_cnt = len(parse_months(old["months"])), len(parse_months(months_db))
                if old_cnt != new_cnt and not (paid > 0):
                    cur.execute("SELECT qtr_issues, issue_price, subscriptions, deduction FROM payments WHERE id=%s", (pid,))
                    pr = cur.fetchone()
                    new_qi = max(1, round((pr["qtr_issues"] or 0) * new_cnt / max(old_cnt, 1)))
                    price, libs = float(pr["issue_price"] or 0), q_int(pr["subscriptions"])
                    cost = price * libs * new_qi
                    cur.execute("UPDATE payments SET qtr_issues=%s, total_issues=%s, actual_cost=%s, "
                                "net_payable=%s - deduction WHERE id=%s", (new_qi, libs * new_qi, cost, cost, pid))
                    months_note = " QTR Issues %d ஆக மாற்றப்பட்டது." % new_qi
                elif old_cnt != new_cnt:
                    months_note = " (தொகை வழங்கப்பட்ட பதிவு — QTR Issues / தொகை மாறவில்லை.)"
                changed_ns = rebalance_non_supply(cur, new_mag, new_q)
                if changed_ns:
                    months_note += " Non-supply கழிவு Part %s-ல் சமன் செய்யப்பட்டது." % ", ".join(str(x) for x in changed_ns)

            # vouchers நகல்களையும் ஒத்திசைவாக வைக்க
            _fx_move_related(cur, old["magazine"], old["quarter"], new_mag, new_q, old["part"], old["part"])
            cur.execute("UPDATE vouchers SET invoice_no=%s, invoice_date=%s, requested_amt=%s "
                        "WHERE magazine=%s AND quarter=%s AND part=%s",
                        (invoice_no, invoice_date, requested, new_mag, new_q, old["part"]))
            cur.execute("SELECT COUNT(*) AS n FROM vouchers WHERE magazine=%s AND quarter=%s AND part=%s",
                        (new_mag, new_q, old["part"]))
            if cur.fetchone()["n"] == 1:
                cur.execute("UPDATE vouchers SET amount_paid=%s WHERE magazine=%s AND quarter=%s AND part=%s",
                            (paid, new_mag, new_q, old["part"]))

            _fx_log(cur, "update_payment", {
                "id": pid,
                "old": {"magazine": old["magazine"], "quarter": old["quarter"], "invoice_no": old["invoice_no"],
                        "invoice_date": old["invoice_date"], "requested_amt": old["requested_amt"],
                        "paid_amt": old["paid_amt"], "payment_date": old["payment_date"],
                        "transaction_no": old["transaction_no"], "voucher_no": old["voucher_no"],
                        "bill_set_no": old["bill_set_no"], "months": old["months"]},
                "new": {"magazine": new_mag, "quarter": new_q, "invoice_no": invoice_no,
                        "invoice_date": invoice_date, "requested_amt": requested, "paid_amt": paid,
                        "payment_date": payment_date, "transaction_no": txn, "voucher_no": voucher_no,
                        "bill_set_no": bill_set_no, "months": months_db},
            })
        conn.commit()
        return jsonify({"success": True, "message": "பதிவு புதுப்பிக்கப்பட்டது." + months_note})
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()


# =============================================================================
# தரவு சரிபார்ப்பு — Paid Details அறிக்கையில் இதழ் / Vendor Code தவறுகளைக் கண்டுபிடிக்க
#   (/api/payments/paid-report/audit?magazine=...)
# =============================================================================
def _name_key(v):
    """இதழ் பெயர் ஒப்பீட்டுக்கு: Unicode NFC, கண்ணுக்குத் தெரியாத எழுத்துகள் / கூடுதல் இடைவெளி நீக்கம்."""
    import unicodedata
    v = unicodedata.normalize("NFC", v or "")
    v = v.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\xa0", " ")
    return " ".join(v.split()).casefold()


def _code_key(v):
    """Vendor Code வடிவ வேறுபாடுகளை (இடைவெளி, '-', '_', எழுத்து அளவு) நீக்கி ஒப்பிட."""
    return re.sub(r"[\s\-_./]+", "", (v or "")).upper()


def _audit_compute(code, group_names, magazines, payments):
    """தூய கணக்கீடு (DB இல்லாமல் சோதிக்கலாம்).
    magazines: [{name, tnpfts_code, vendor_name, payee_name}] — Master முழுவதும்
    payments : [{id, magazine, quarter, invoice_no, invoice_date, requested_amt, paid_amt,
                 payment_date, transaction_no}] — Master-ல் உள்ள அனைத்துப் பதிவுகள்
    """
    import difflib
    issues = []

    def add(level, kind, msg, **extra):
        issues.append({"level": level, "kind": kind, "message": msg, **extra})

    master_names = {m["name"] for m in magazines}
    master_keys = {_name_key(n): n for n in master_names}
    group_keys = {_name_key(n): n for n in group_names}
    group_set = set(group_names)

    # 1) payments-ல் உள்ள இதழ் பெயர் Master பெயருடன் பொருந்தவில்லை (எழுத்துப்பிழை / இடைவெளி)
    for pr in payments:
        nm = pr["magazine"]
        if nm in master_names:
            continue
        k = _name_key(nm)
        hint = master_keys.get(k)
        if not hint:
            best = difflib.get_close_matches(k, list(master_keys), n=1, cutoff=0.75)
            hint = master_keys[best[0]] if best else None
        add("error", "payment_name_not_in_master",
            "Payments-ல் உள்ள இதழ் பெயர் '%s' (%s) Master-ல் இல்லை%s — இதனால் இந்தத் தொகை அறிக்கையில் வராது." % (
                nm, pr["quarter"], (" → Master பெயர்: '%s'" % hint) if hint else ""),
            magazine=nm, quarter=pr["quarter"], suggestion=hint)

    # 2) Master-ல் உள்ள மற்ற இதழ்கள் — பெயர் ஒத்திருக்கிறது ஆனால் Vendor Code வேறு / ஒரே Vendor Name
    vendor_names = {_name_key(m["vendor_name"]) for m in magazines
                    if m["name"] in group_set and (m["vendor_name"] or "").strip()}
    gcode = _code_key(code)
    for m in magazines:
        if m["name"] in group_set:
            continue
        mcode = _code_key(m["tnpfts_code"])
        same_vendor = bool(vendor_names) and _name_key(m["vendor_name"]) in vendor_names
        sim = max([difflib.SequenceMatcher(None, _name_key(m["name"]), gk).ratio() for gk in group_keys] or [0])
        starts = any(_name_key(m["name"]).startswith(gk) or gk.startswith(_name_key(m["name"]))
                     for gk in group_keys)
        if gcode and mcode == gcode:
            add("error", "code_format_differs",
                "'%s' இதழின் Vendor Code '%s' — இது '%s'-உடன் எழுத்து வடிவில் மட்டும் வேறுபடுகிறது (இடைவெளி / '-' / எழுத்து அளவு). "
                "இதனால் குழுவில் சேராது." % (m["name"], m["tnpfts_code"], code), magazine=m["name"])
        elif same_vendor:
            add("warn", "same_vendor_other_code",
                "'%s' இதழுக்கு Vendor Name அதே, ஆனால் Vendor Code '%s' (குழுவின் Code '%s')." % (
                    m["name"], m["tnpfts_code"] or "—", code), magazine=m["name"])
        elif sim >= 0.8 or starts:
            add("warn", "similar_name_other_code",
                "'%s' இதழ் பெயர் குழுவில் உள்ள இதழ்களை ஒத்திருக்கிறது, ஆனால் Vendor Code '%s'." % (
                    m["name"], m["tnpfts_code"] or "—"), magazine=m["name"])

    # 3) குழு இதழ்களின் பதிவுகள்
    gp = [pr for pr in payments if pr["magazine"] in group_set]
    quarters = sorted({pr["quarter"] for pr in gp if pr["quarter"]})
    coverage = {}
    for n in sorted(group_set):
        paid_q = sorted({pr["quarter"] for pr in gp if pr["magazine"] == n and float(pr["paid_amt"] or 0) > 0})
        any_q = sorted({pr["quarter"] for pr in gp if pr["magazine"] == n})
        coverage[n] = {"paidQuarters": paid_q, "recordQuarters": any_q}
        gaps = [q for q in quarters if q not in paid_q]
        if gaps:
            add("info", "quarter_gap",
                "'%s' — இந்த Quarter-களில் Paid பதிவு இல்லை: %s" % (n, ", ".join(gaps)),
                magazine=n, quarters=gaps)

    # 4) பதிவு உள்ளது ஆனால் Paid Amount 0 — அறிக்கை paid_amt > 0 மட்டுமே காட்டும்
    for pr in gp:
        if float(pr["paid_amt"] or 0) <= 0 and (pr["transaction_no"] or pr["payment_date"]):
            add("error", "zero_paid_but_txn",
                "'%s' %s — Bank Transaction No / Paid Date உள்ளது, ஆனால் Paid Amount 0. அறிக்கையில் வராது." % (
                    pr["magazine"], pr["quarter"]), magazine=pr["magazine"], quarter=pr["quarter"])

    # 5) Transaction No-வில் 'TOTAL AMOUNT -xxxx' எழுதியிருந்தால் குழு மொத்தத்துடன் ஒப்பிடுதல்
    by_txn = {}
    for pr in gp:
        t = (pr["transaction_no"] or "").strip()
        if t and float(pr["paid_amt"] or 0) > 0:
            by_txn.setdefault((pr["quarter"], t.upper()), []).append(pr)
    for (q, t), rows in by_txn.items():
        mt = re.search(r"TOTAL\s*AMOUNT\s*[-:=]?\s*([\d,]+(?:\.\d+)?)", t)
        if mt:
            stated = float(mt.group(1).replace(",", ""))
            actual = sum(float(r["paid_amt"] or 0) for r in rows)
            if abs(stated - actual) > 0.5:
                add("error", "txn_total_mismatch",
                    "%s — Bank Transaction No-வில் TOTAL AMOUNT %s, ஆனால் பதிவுகளின் கூட்டுத்தொகை %s (வேறுபாடு %s). "
                    "இந்த வேறுபாட்டுக்குரிய இதழ் / தொகை பதிவாகாமல் இருக்கலாம்." % (
                        q, indian_grouping(stated), indian_grouping(actual), indian_grouping(abs(stated - actual))),
                    quarter=q, stated=stated, actual=actual, diff=round(stated - actual, 2))

    # 6) தேதி முரண்பாடு (தட்டச்சுப் பிழை — எ.கா. 2025 / 2026)
    for pr in gp:
        idt, pdt = pr["invoice_date"], pr["payment_date"]
        if idt and pdt and float(pr["paid_amt"] or 0) > 0:
            gap = (pdt - idt).days
            if gap < 0:
                add("warn", "invoice_after_paid",
                    "'%s' %s — Invoice Date (%s) Paid Date-க்குப் (%s) பிந்தியது." % (
                        pr["magazine"], pr["quarter"], fmt_date(idt), fmt_date(pdt)),
                    magazine=pr["magazine"], quarter=pr["quarter"])
            elif gap > 270:
                add("warn", "invoice_date_far",
                    "'%s' %s — Invoice Date (%s) Paid Date-க்கு (%s) %d நாட்களுக்கு முன்; ஆண்டு தவறாக இருக்கலாம்." % (
                        pr["magazine"], pr["quarter"], fmt_date(idt), fmt_date(pdt), gap),
                    magazine=pr["magazine"], quarter=pr["quarter"])

    # 7) ஒரே தொகை வழங்கலில் (Quarter + Txn) Invoice No வடிவ வேறுபாடு
    for (q, t), rows in by_txn.items():
        invs = {re.sub(r"\s+", "", (r["invoice_no"] or "")).upper() for r in rows
                if (r["invoice_no"] or "").strip().upper() not in ("", "NIL", "---", "-")}
        if len(invs) > 1:
            add("info", "invoice_no_differs",
                "%s — ஒரே தொகை வழங்கலில் Invoice Number வெவ்வேறாக உள்ளன: %s" % (q, ", ".join(sorted(invs))),
                quarter=q, magazines=sorted({r["magazine"] for r in rows}))

    order = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda x: order.get(x["level"], 9))
    return {"quarters": quarters, "coverage": coverage, "issues": issues}


def build_paid_audit(cur, magazine):
    cur.execute("SELECT name, tnpfts_code, vendor_name, payee_name FROM magazines")
    mags = [dict(r) for r in cur.fetchall()]
    me = next((m for m in mags if m["name"] == magazine), None)
    if not me:
        return None
    code = norm_code(me["tnpfts_code"])
    names = sorted({m["name"] for m in mags if code and norm_code(m["tnpfts_code"]) == code} | {magazine})
    cur.execute("SELECT id, magazine, quarter, invoice_no, invoice_date, requested_amt, paid_amt, "
                "payment_date, transaction_no FROM payments")
    pays = [dict(r) for r in cur.fetchall()]
    res = _audit_compute(code, names, mags, pays)
    res.update({"magazine": magazine, "vendorCode": (me["tnpfts_code"] or "").strip(), "groupMagazines": names})
    return res


@app.route("/api/payments/paid-report/audit")
def api_paid_report_audit():
    magazine = (request.args.get("magazine") or "").strip()
    if not magazine:
        return jsonify({"success": False, "message": "இதழைத் தேர்ந்தெடுக்கவும்."}), 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            res = build_paid_audit(cur, magazine)
    finally:
        conn.close()
    if res is None:
        return jsonify({"success": False, "message": "இதழ் Master-ல் இல்லை."}), 404
    return jsonify({"success": True, **res})


@app.route("/api/payments/paid-report")
def api_paid_report():
    """?magazine=...&quarter=Q1,Q2 (காலி = அனைத்து)&format=json|pdf|xlsx|csv"""
    magazine = (request.args.get("magazine") or "").strip()
    fmt = (request.args.get("format") or "json").strip().lower()
    if not magazine:
        return jsonify({"success": False, "message": "இதழைத் தேர்ந்தெடுக்கவும்."}), 400
    quarters = get_quarters_arg()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            rep = build_paid_report(cur, magazine, quarters)
    finally:
        conn.close()
    if rep is None:
        return jsonify({"success": False, "message": "இதழ் Master-ல் இல்லை."}), 404
    if fmt == "json":
        return jsonify({"success": True, **rep})
    base = rep["vendorCode"] or magazine
    stem = re.sub(r"[^\w\-]+", "_", "Paid_Details_" + base + "_" + ("+".join(quarters) if quarters else "All")) \
        + "_" + datetime.now().strftime("%Y%m%d")
    try:
        if fmt == "csv":
            return send_file(io.BytesIO(paid_report_csv(rep)), mimetype="text/csv",
                             as_attachment=True, download_name=stem + ".csv")
        if fmt == "xlsx":
            return send_file(io.BytesIO(paid_report_xlsx(rep)),
                             mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True, download_name=stem + ".xlsx")
        if fmt == "pdf":
            return send_file(io.BytesIO(paid_report_pdf(rep)), mimetype="application/pdf",
                             as_attachment=True, download_name=stem + ".pdf")
    except Exception as e:  # noqa: BLE001
        return jsonify({"success": False, "message": f"{fmt.upper()} உருவாக்கத்தில் பிழை: {e}"}), 500
    return jsonify({"success": False, "message": "format தவறு"}), 400


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
