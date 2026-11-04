"""
நூலகர் இதழ் வருகைப் பதிவு (Librarian magazine-receipt register)
================================================================
app.py-ல் `init_librarian(app, get_conn)` என்று ஒரே ஒரு வரியில் இணைக்கப்படும் தனி Blueprint.

பகுதிகள்
  1. நூலகர் (Login dropdown-ல் "Librarian", Password இல்லை)
       /librarian/select      -> வகை + நூலகப் பெயர் தேர்வு
       /librarian/dashboard   -> இந்த வாரம் + முந்தைய வாரங்களின் (அதிகபட்சம் 4) "இன்னும் வராதவை" / "பதிவு செய்யாதவை": Received / Not received
  2. Admin
       /admin/librarian       -> Quarter Excel ஏற்றுதல், நூலகர் பதிவு நிலை (பதிவு செய்தவை / இன்னும் வராதவை / பதிவே செய்யாதவை),
                                 ஒரு நூலகத்தைத் தேர்ந்து Admin-ஆகவே Received / Not received பதிவு செய்தல்

Tables (தானாக உருவாகும்)
  lib_libraries (code, name, category)         <- data/libraries_seed.csv (காலியாக இருந்தால் ஒருமுறை) அல்லது Admin பக்கம்
  lib_schedule  (quarter, code, magazine, periodicity, expected_date)  <- Admin பக்கத்தில் Q3 / Q4 Excel ஏற்றுதல்
  lib_receipts  (code, magazine, expected_date, status, marked_at, marked_by)  <- நூலகர் / Admin பதிவு
  * lib_receipts தனி table என்பதால் Quarter Excel-ஐ மீண்டும் ஏற்றினாலும் நூலகர் பதிவுகள் அழியாது.
"""

import csv
import io
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from functools import wraps

import psycopg2.extras
from flask import Blueprint, Response, jsonify, redirect, render_template_string, request, session

lib_bp = Blueprint("librarian", __name__)
_get_conn = None
_ready = False

IST = timezone(timedelta(hours=5, minutes=30))
SEED_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "libraries_seed.csv")
STATUSES = ("Received", "Not received")
CARRY_WEEKS = 4   # நூலகர் திரையில் முந்தைய வாரங்களின் இதழ்கள் அதிகபட்சம் எத்தனை வாரம் காட்டப்படும்
NR_WAIT_DAYS = 5   # இதழ் தேதிக்குப் பின் இத்தனை நாள் காத்திருக்க வேண்டும்; அதற்கு அடுத்த நாளிலிருந்துதான் நூலகர் "Not received" பதிவு செய்யலாம்
CATEGORY_ORDER = ["மாவட்ட மைய நூலகம்", "முழுநேர கிளை நூலகம்", "கிளை நூலகம்", "ஊர்ப்புற நூலகம்"]
TA_DAYS = ["திங்கள்", "செவ்வாய்", "புதன்", "வியாழன்", "வெள்ளி", "சனி", "ஞாயிறு"]


def init_librarian(app, get_conn):
    global _get_conn
    _get_conn = get_conn
    app.register_blueprint(lib_bp)


# --------------------------------------------------------------------------- #
# பொது உதவிகள்
# --------------------------------------------------------------------------- #
def today_ist():
    return datetime.now(IST).date()


def week_bounds(d):
    """திங்கள் முதல் ஞாயிறு வரை."""
    start = d - timedelta(days=d.weekday())
    return start, start + timedelta(days=6)


def quarter_label(d):
    """நிதியாண்டு Quarter: Apr-Jun=Q1, Jul-Sep=Q2, Oct-Dec=Q3, Jan-Mar=Q4 (எ.கா. 2026-2027-Q3)."""
    fy = d.year if d.month >= 4 else d.year - 1
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]
    return f"{fy}-{fy + 1}-Q{q}"


def fmt_d(d):
    return d.strftime("%d/%m/%Y") if d else ""


def parse_iso(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def ensure_schema():
    global _ready
    if _ready:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7712001)")   # பல worker ஒரே நேரத்தில் உருவாக்குவதைத் தடுக்க
            cur.execute(
                "CREATE TABLE IF NOT EXISTS lib_libraries ("
                " code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',"
                " updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS lib_schedule ("
                " id SERIAL PRIMARY KEY, quarter TEXT NOT NULL, code TEXT NOT NULL, magazine TEXT NOT NULL,"
                " periodicity TEXT, expected_date DATE NOT NULL,"
                " UNIQUE (code, magazine, expected_date))")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_sched_date ON lib_schedule (expected_date)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_sched_quarter ON lib_schedule (quarter)")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS lib_receipts ("
                " code TEXT NOT NULL, magazine TEXT NOT NULL, expected_date DATE NOT NULL,"
                " status TEXT NOT NULL CHECK (status IN ('Received','Not received')),"
                " marked_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                " PRIMARY KEY (code, magazine, expected_date))")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_lib_receipts_date ON lib_receipts (expected_date)")
            # யார் பதிவு செய்தார்: librarian / admin (பழைய பதிவுகள் = librarian)
            cur.execute("ALTER TABLE lib_receipts ADD COLUMN IF NOT EXISTS marked_by TEXT NOT NULL DEFAULT 'librarian'")
            # நூலகர் விவரங்கள் (Master) — missing_module.py-ம் இதே நெடுவரிசைகளை உருவாக்கும் (IF NOT EXISTS என்பதால் முரண் இல்லை)
            for _c in ("name_en", "librarian_name", "librarian_name_en", "phone", "address", "address_en"):
                cur.execute("ALTER TABLE lib_libraries ADD COLUMN IF NOT EXISTS %s TEXT NOT NULL DEFAULT ''" % _c)
            # Admin அமைப்புகள் (எ.கா. நூலகர் பதிவுக் காலம்): key → value
            cur.execute("CREATE TABLE IF NOT EXISTS lib_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cur.execute("SELECT COUNT(*) AS n FROM lib_libraries")
            if cur.fetchone()["n"] == 0 and os.path.exists(SEED_CSV):
                with open(SEED_CSV, newline="", encoding="utf-8-sig") as f:
                    rows = [(r["code"].strip(), r["name"].strip(), r["category"].strip())
                            for r in csv.DictReader(f) if (r.get("code") or "").strip()]
                psycopg2.extras.execute_values(
                    cur, "INSERT INTO lib_libraries (code, name, category) VALUES %s ON CONFLICT (code) DO NOTHING", rows)
        conn.commit()
    finally:
        conn.close()
    _ready = True


def _user():
    return session.get("user") or {}


def librarian_page(f):
    @wraps(f)
    def w(*a, **k):
        if _user().get("role") != "librarian":
            return redirect("/")
        ensure_schema()
        return f(*a, **k)
    return w


def librarian_api(need_library=True):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if _user().get("role") != "librarian":
                return jsonify({"success": False, "message": "நூலகர் நுழைவு தேவை."}), 403
            ensure_schema()
            if need_library and not session.get("lib_code"):
                return jsonify({"success": False, "needLibrary": True, "message": "முதலில் நூலகத்தைத் தேர்ந்தெடுக்கவும்."}), 400
            return f(*a, **k)
        return w
    return deco


def admin_page(f):
    @wraps(f)
    def w(*a, **k):
        if _user().get("role") != "admin":
            return redirect("/")
        ensure_schema()
        return f(*a, **k)
    return w


def admin_api(f):
    @wraps(f)
    def w(*a, **k):
        if _user().get("role") != "admin":
            return jsonify({"success": False, "message": "இந்த வசதி Admin-க்கு மட்டுமே."}), 403
        ensure_schema()
        return f(*a, **k)
    return w


def _category_sort_key(c):
    return (CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER), c)


# =========================================================================== #
# 1. நூலகர் பக்கங்கள்
# =========================================================================== #
@lib_bp.route("/librarian/select")
@librarian_page
def librarian_select_page():
    return render_template_string(SELECT_HTML)


@lib_bp.route("/librarian/change")
@librarian_page
def librarian_change():
    session.pop("lib_code", None)
    session.pop("lib_name", None)
    return redirect("/librarian/select")


def _libraries_json():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, category FROM lib_libraries")
            rows = cur.fetchall()
    finally:
        conn.close()
    groups = {}
    for r in rows:
        groups.setdefault(r["category"] or "மற்றவை", []).append({"code": r["code"], "name": r["name"]})
    out = [{"category": c, "libraries": sorted(v, key=lambda x: x["name"])}
           for c, v in sorted(groups.items(), key=lambda kv: _category_sort_key(kv[0]))]
    return jsonify({"success": True, "categories": out})


@lib_bp.route("/api/librarian/libraries")
@librarian_api(need_library=False)
def api_librarian_libraries():
    return _libraries_json()


@lib_bp.route("/api/librarian/select", methods=["POST"])
@librarian_api(need_library=False)
def api_librarian_select():
    code = ((request.get_json(force=True, silent=True) or {}).get("code") or "").strip()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name FROM lib_libraries WHERE code=%s", (code,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
    session["lib_code"], session["lib_name"] = row["code"], row["name"]
    return jsonify({"success": True})


@lib_bp.route("/librarian/dashboard")
@librarian_page
def librarian_dashboard_page():
    if not session.get("lib_code"):
        return redirect("/librarian/select")
    return render_template_string(DASH_HTML)


def _row_json(r, ws):
    d = r["expected_date"]
    return {"magazine": r["magazine"], "periodicity": r["periodicity"] or "",
            "date": d.isoformat(), "dateLabel": fmt_d(d), "day": TA_DAYS[d.weekday()],
            "status": r["status"], "weeksAgo": (ws - week_bounds(d)[0]).days // 7,
            "dateDot": d.strftime("%d.%m.%Y"),
            "waitUntilDot": (d + timedelta(days=NR_WAIT_DAYS)).strftime("%d.%m.%Y"),
            "nrAllowed": today_ist() > d + timedelta(days=NR_WAIT_DAYS)}


def _get_range():
    """Admin அமைத்த நூலகர் பதிவுக் காலம் → (from_date, to_date); அமைக்கவில்லை எனில் None."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM lib_settings WHERE key IN ('reg_from','reg_to')")
            kv = {r["key"]: r["value"] for r in cur.fetchall()}
    finally:
        conn.close()
    a, b = parse_iso(kv.get("reg_from")), parse_iso(kv.get("reg_to"))
    return (a, b) if a and b and a <= b else None


def _library_week_payload(code, weeks_back, apply_range=False):
    """ஒரு நூலகத்துக்கு: இந்த வாரம் + முந்தைய வாரங்களில் (weeks_back வாரம் வரை; None = வரம்பின்றி)
    'இன்னும் வராதவை' (Not received) மற்றும் 'பதிவு செய்யாதவை'. நூலகர் திரையும் Admin பதிவுத் திரையும் இதையே பயன்படுத்தும்."""
    today = today_ist()
    ws, we = week_bounds(today)
    rng = _get_range() if apply_range else None
    if rng:
        r_lo, r_hi = rng[0], min(rng[1], today)     # எதிர்காலத் தேதிகள் எப்போதும் தடை
    join = ("LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine "
            "AND r.expected_date=s.expected_date ")
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, category FROM lib_libraries WHERE code=%s", (code,))
            lib = cur.fetchone()
            if not lib:
                return None
            a, b = (max(ws, r_lo), min(we, r_hi)) if rng else (ws, we)
            this_rows = []
            if a <= b:
                cur.execute(
                    "SELECT s.magazine, s.periodicity, s.expected_date, r.status FROM lib_schedule s " + join +
                    "WHERE s.code=%s AND s.expected_date BETWEEN %s AND %s ORDER BY s.expected_date, s.magazine",
                    (code, a, b))
                this_rows = cur.fetchall()
            sql = ("SELECT s.magazine, s.periodicity, s.expected_date, r.status FROM lib_schedule s " + join +
                   "WHERE s.code=%s AND s.expected_date < %s")
            params = [code, ws]
            if rng:
                sql += " AND s.expected_date >= %s AND s.expected_date <= %s"
                params += [r_lo, r_hi]
            elif weeks_back:
                sql += " AND s.expected_date >= %s"
                params.append(ws - timedelta(weeks=weeks_back))
            sql += " AND (r.code IS NULL OR r.status='Not received') ORDER BY s.expected_date, s.magazine"
            cur.execute(sql, params)
            earlier = cur.fetchall()
    finally:
        conn.close()
    return {
        "success": True, "library": dict(lib),
        "weekStart": fmt_d(ws), "weekEnd": fmt_d(we), "today": today.isoformat(), "carryWeeks": 0 if rng else (weeks_back or 0),
        "range": {"from": r_lo.strftime("%d.%m.%Y"), "to": r_hi.strftime("%d.%m.%Y")} if rng else None,
        "rows": [_row_json(r, ws) for r in this_rows],
        "earlierNotReceived": [_row_json(r, ws) for r in earlier if r["status"] == "Not received"],
        "earlierUnregistered": [_row_json(r, ws) for r in earlier if not r["status"]],
    }


def _save_entries(code, entries, weeks_back, by):  # by="librarian" எனில் Not received காத்திருப்பு விதி பொருந்தும்; Admin-க்கு இல்லை
    """Received / Not received சேமிப்பு. எதிர்காலத் தேதி (இந்த வாரத்துக்குப் பிந்தையவை) எப்போதும் தடை.
    weeks_back: இந்த வாரத்துக்கு முந்தைய எத்தனை வாரம் வரை அனுமதி (None = வரம்பின்றி — Admin)."""
    today = today_ist()
    ws, we = week_bounds(today)
    rng = _get_range() if by != "admin" else None
    if rng:   # Admin அமைத்த பதிவுக் காலம் (இன்றைய தேதி வரை மட்டும்)
        win_lo, win_hi = rng[0], min(rng[1], today)
    else:
        win_lo, win_hi = (ws - timedelta(weeks=weeks_back) if weeks_back else None), we
    clean = {}
    for e in entries:
        d = parse_iso(e.get("date"))
        mag, st = (e.get("magazine") or "").strip(), e.get("status")
        if by == "admin" and st == "Clear":   # Admin மட்டும்: பதிவை நீக்கி "தேர்வு செய்யாத" நிலைக்குக் கொண்டுவரும்
            pass
        elif not mag or st not in STATUSES:
            st = None
        if not mag or st is None or not d or d > win_hi or (win_lo and d < win_lo):
            msg = ("இந்த இதழ் தற்போதைய பதிவுக் காலத்துக்கு (%s – %s) வெளியே உள்ளது; பதிவு செய்ய முடியாது." %
                   (win_lo.strftime("%d.%m.%Y"), win_hi.strftime("%d.%m.%Y"))) if rng else \
                  "தவறான பதிவு (இந்த வாரம் மற்றும் அதற்கு முந்தையவை மட்டுமே; எதிர்காலத் தேதிகள் முடியாது)."
            return jsonify({"success": False, "message": msg}), 400
        clean[(mag, d)] = (code, mag, d, st, by)
    rows = list(clean.values())
    if not rows:
        return jsonify({"success": False, "message": "எந்த இதழும் தேர்ந்தெடுக்கப்படவில்லை."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # அட்டவணையில் உள்ள இதழ்களை மட்டுமே ஏற்கும்
            cur.execute("SELECT magazine, expected_date FROM lib_schedule WHERE code=%s AND expected_date BETWEEN %s AND %s",
                        (code, min(r[2] for r in rows), max(r[2] for r in rows)))
            valid = {(r["magazine"], r["expected_date"]) for r in cur.fetchall()}
            rows = [r for r in rows if (r[1], r[2]) in valid]
            if not rows:
                return jsonify({"success": False, "message": "பதிவு செய்ய இதழ்கள் எதுவும் பொருந்தவில்லை."}), 400
            clear_rows = [r for r in rows if r[3] == "Clear"]
            rows = [r for r in rows if r[3] != "Clear"]
            if by != "admin":
                # நூலகர்: இதழ் தேதி + NR_WAIT_DAYS நாட்கள் முடிவதற்கு முன் புதிதாக Not received குறிக்க முடியாது
                # (ஏற்கனவே Not received எனப் பதிவானவை மாறாமல் மீண்டும் வந்தால் தடை இல்லை)
                today = today_ist()
                early = [r for r in rows if r[3] == "Not received" and today <= r[2] + timedelta(days=NR_WAIT_DAYS)]
                if early:
                    cur.execute("SELECT magazine, expected_date FROM lib_receipts WHERE code=%s AND status='Not received' "
                                "AND expected_date BETWEEN %s AND %s",
                                (code, min(r[2] for r in early), max(r[2] for r in early)))
                    already = {(x["magazine"], x["expected_date"]) for x in cur.fetchall()}
                    bad = [r for r in early if (r[1], r[2]) not in already]
                    if bad:
                        b = bad[0]
                        wu = (b[2] + timedelta(days=NR_WAIT_DAYS)).strftime("%d.%m.%Y")
                        return jsonify({"success": False, "message":
                            f"\"{b[1]}\" ({b[2].strftime('%d.%m.%Y')}) இதழுக்கு {wu} வரை காத்திருங்கள்; "
                            f"அதன் பின்னரும் வரவில்லை எனில் Not received பதிவு செய்யுங்கள்."}), 400
            if rows:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO lib_receipts (code, magazine, expected_date, status, marked_by) VALUES %s "
                    "ON CONFLICT (code, magazine, expected_date) DO UPDATE "
                    "SET status=EXCLUDED.status, marked_at=now(), marked_by=EXCLUDED.marked_by "
                    "WHERE lib_receipts.status IS DISTINCT FROM EXCLUDED.status",
                    rows)
            for r in clear_rows:
                cur.execute("DELETE FROM lib_receipts WHERE code=%s AND magazine=%s AND expected_date=%s",
                            (r[0], r[1], r[2]))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    who = "Admin மூலம் " if by == "admin" else ""
    msg = f"✅ {len(rows)} இதழ்களின் பதிவு {who}சேமிக்கப்பட்டது."
    if clear_rows:
        msg = (f"✅ {len(rows)} இதழ்களின் பதிவு சேமிக்கப்பட்டது; " if rows else "✅ ") + f"{len(clear_rows)} இதழ்களின் பதிவு நீக்கப்பட்டது."
    return jsonify({"success": True, "saved": len(rows), "cleared": len(clear_rows), "message": msg})


@lib_bp.route("/api/librarian/week")
@librarian_api()
def api_librarian_week():
    data = _library_week_payload(session["lib_code"], CARRY_WEEKS, apply_range=True)
    if data is None:
        return jsonify({"success": False, "needLibrary": True, "message": "நூலகம் கண்டறியப்படவில்லை."}), 400
    return jsonify(data)


@lib_bp.route("/api/librarian/save", methods=["POST"])
@librarian_api()
def api_librarian_save():
    entries = (request.get_json(force=True, silent=True) or {}).get("entries") or []
    return _save_entries(session["lib_code"], entries, CARRY_WEEKS, "librarian")


# ---- நூலகர் விவரங்கள் (பெயர் / தொலைபேசி / முகவரி) — நூலகரே திருத்தலாம்; Master (lib_libraries)-ல் பதிவாகும் ---- #
# ஒரே ஒரு தொகுப்பு, ஆங்கிலத்தில் மட்டும்: LIBRARIAN NAME, MOBILE NUMBER, ADDRESS.
# சேமிக்கும்போது தமிழ் + English புலங்கள் இரண்டிலும் ஒரே மதிப்பு பதிவாகும் (எந்த report-லும் பழைய மதிப்பு வராது).
# படிக்கும்போது English புலம் முதலில்; காலி எனில் தமிழ் புலம்.
def _addr_clean(v):
    """முகவரியின் வரி மாற்றங்கள் (Enter) அப்படியே; வெற்று வரிகள் / ஓர இடைவெளி நீக்கம்."""
    lines = (v or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(ln.strip() for ln in lines if ln.strip())


@lib_bp.route("/api/librarian/profile")
@librarian_api()
def api_librarian_profile():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT librarian_name, librarian_name_en, phone, address, address_en "
                        "FROM lib_libraries WHERE code=%s", (session["lib_code"],))
            r = cur.fetchone()
    finally:
        conn.close()
    if not r:
        return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
    return jsonify({"success": True,
                    "name": r["librarian_name_en"] or r["librarian_name"] or "",
                    "phone": r["phone"] or "",
                    "address": r["address_en"] or r["address"] or ""})


@lib_bp.route("/api/librarian/profile-save", methods=["POST"])
@librarian_api()
def api_librarian_profile_save():
    body = request.get_json(force=True, silent=True) or {}
    name = " ".join((body.get("name") or "").split())
    phone = "".join((body.get("phone") or "").split())
    address = _addr_clean(body.get("address"))
    if not name or not phone or not address:
        return jsonify({"success": False, "message": "LIBRARIAN NAME, MOBILE NUMBER, ADDRESS — மூன்றையும் நிரப்பவும்."}), 400
    if _is_tamil(name) or _is_tamil(address):
        return jsonify({"success": False, "message": "தயவு செய்து ஆங்கிலத்தில் உள்ளிடவும் (Please enter in English)."}), 400
    if len(name) > 100 or len(address) > 500:
        return jsonify({"success": False, "message": "உள்ளீடு மிக நீளமாக உள்ளது."}), 400
    if not (len(phone) == 10 and phone.isascii() and phone.isdigit()):
        return jsonify({"success": False, "message": "MOBILE NUMBER சரியாக 10 இலக்கங்களாக இருக்க வேண்டும் (எண்கள் மட்டும்)."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE lib_libraries SET librarian_name=%s, librarian_name_en=%s, phone=%s, address=%s, address_en=%s, "
                        "updated_at=now() WHERE code=%s", (name, name, phone, address, address, session["lib_code"]))
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": "சேமிக்க முடியவில்லை: %s" % e}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "name": name, "phone": phone, "address": address})


# ---- ஆணையிடப்பட்ட இதழ்கள் பட்டியல் (PDF / Excel) ------------------------------------------------ #
HEAD_LINES = ["தமிழ்நாடு அரசு பொது நூலகத்துறை", "மாவட்ட நூலக ஆணைக்குழு, திண்டுக்கல்"]
REG_TITLE = "பருவ இதழ் வருகைப் பதிவேடு"
ORDERED_TITLE = "உங்கள் நூலகத்திற்கு ஆணையிடப்பட்ட இதழ்கள்"
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")


def _is_tamil(text):
    """இதழ் பெயரில் தமிழ் எழுத்து (U+0B80–U+0BFF) இருந்தால் தமிழ் இதழ்."""
    return any("\u0b80" <= ch <= "\u0bff" for ch in (text or ""))


def _ordered_magazines(code):
    """(நூலகப் பெயர், ஆங்கில இதழ்கள், தமிழ் இதழ்கள்) — ஒவ்வொன்றும் [(இதழ், காலமுறை)].
    நடப்பு Quarter-ல் அட்டவணை இருந்தால் அது; இல்லையெனில் அந்த நூலகத்தின் எல்லா அட்டவணையும்."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM lib_libraries WHERE code=%s", (code,))
            lib = cur.fetchone()
            if not lib:
                return None
            cur.execute("SELECT magazine, MAX(periodicity) AS periodicity FROM lib_schedule "
                        "WHERE code=%s AND quarter=%s GROUP BY magazine", (code, quarter_label(today_ist())))
            rows = cur.fetchall()
            if not rows:
                cur.execute("SELECT magazine, MAX(periodicity) AS periodicity FROM lib_schedule "
                            "WHERE code=%s GROUP BY magazine", (code,))
                rows = cur.fetchall()
    finally:
        conn.close()
    items = sorted(((r["magazine"], r["periodicity"] or "") for r in rows), key=lambda x: x[0].casefold())
    return (lib["name"], [i for i in items if not _is_tamil(i[0])], [i for i in items if _is_tamil(i[0])])


def _ordered_pdf(lib_name, eng, tam):
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("Tamil", "", os.path.join(_FONT_DIR, "NotoSansTamil-Regular.ttf"))
    pdf.add_font("Tamil", "B", os.path.join(_FONT_DIR, "NotoSansTamil-Bold.ttf"))
    pdf.set_text_shaping(True)
    pdf.add_page()
    for txt, size in [(HEAD_LINES[0], 15), (HEAD_LINES[1], 13), (lib_name, 13), (REG_TITLE, 13)]:
        pdf.set_font("Tamil", "B", size)
        pdf.multi_cell(0, 7.5, txt, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Tamil", "B", 12)
    pdf.multi_cell(0, 7, ORDERED_TITLE, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Tamil", "", 11)
    head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(30, 77, 140))
    sec = FontFace(emphasis="BOLD", fill_color=(220, 230, 247))
    with pdf.table(col_widths=(22, 108, 50), text_align=("CENTER", "LEFT", "CENTER"), headings_style=head,
                   line_height=7, padding=1.5, borders_layout="ALL") as t:
        h = t.row()
        for c in ("வ.எண்.", "இதழ் பெயர்", "காலமுறை"):
            h.cell(c)
        for title, items in (("ஆங்கிலம்", eng), ("தமிழ்", tam)):
            r = t.row()
            r.cell(title, colspan=3, align="L", style=sec)
            if not items:
                t.row().cell("— இல்லை —", colspan=3, align="C")
            for n, (mag, per) in enumerate(items, start=1):
                r = t.row()
                r.cell(str(n))
                r.cell(mag)
                r.cell(per)
    return bytes(pdf.output())


def _ordered_xlsx(lib_name, eng, tam):
    import xlsxwriter
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Ordered magazines")
    big = wb.add_format({"bold": True, "align": "center", "valign": "vcenter", "font_size": 14})
    mid = wb.add_format({"bold": True, "align": "center", "valign": "vcenter", "font_size": 12})
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1, "align": "center"})
    sec = wb.add_format({"bold": True, "bg_color": "#dce6f7", "border": 1})
    cell = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    mid_c = wb.add_format({"border": 1, "align": "center", "valign": "top"})
    r = 0
    for txt, f in [(HEAD_LINES[0], big), (HEAD_LINES[1], mid), (lib_name, mid), (REG_TITLE, mid), (ORDERED_TITLE, mid)]:
        ws.merge_range(r, 0, r, 2, txt, f)
        r += 1
    r += 1
    for c, h in enumerate(("வ.எண்.", "இதழ் பெயர்", "காலமுறை")):
        ws.write(r, c, h, head)
    r += 1
    for title, items in (("ஆங்கிலம்", eng), ("தமிழ்", tam)):
        ws.merge_range(r, 0, r, 2, title, sec)
        r += 1
        for n, (mag, per) in enumerate(items, start=1):
            ws.write_number(r, 0, n, mid_c)
            ws.write(r, 1, mag, cell)
            ws.write(r, 2, per, mid_c)
            r += 1
    ws.set_column(0, 0, 10)
    ws.set_column(1, 1, 60)
    ws.set_column(2, 2, 22)
    wb.close()
    return buf.getvalue()


@lib_bp.route("/api/librarian/ordered-export")
@librarian_api()
def api_librarian_ordered_export():
    code = session["lib_code"]
    data = _ordered_magazines(code)
    if data is None:
        return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 400
    lib_name, eng, tam = data
    fmt = (request.args.get("fmt") or "pdf").lower()
    stamp = today_ist().isoformat()
    if fmt in ("xlsx", "excel"):
        body, mime, ext = _ordered_xlsx(lib_name, eng, tam), \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    else:
        body, mime, ext = _ordered_pdf(lib_name, eng, tam), "application/pdf", "pdf"
    return Response(body, mimetype=mime,
                    headers={"Content-Disposition": f"attachment; filename=ordered_magazines_{code}_{stamp}.{ext}"})


# ---- இந்த மாதம் / முந்தைய மாதம்: வந்தவை - வராதவை ---------------------------------------------------- #
TA_MONTHS = ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்",
             "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"]


def month_bounds(d, offset=0):
    """d-ன் மாதம் (offset=1 -> முந்தைய மாதம்) முதல் நாள், கடைசி நாள்."""
    first = d.replace(day=1)
    for _ in range(offset):
        first = (first - timedelta(days=1)).replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, nxt - timedelta(days=1)


@lib_bp.route("/api/librarian/month")
@librarian_api()
def api_librarian_month():
    offset = 1 if request.args.get("offset") == "1" else 0
    rng = _get_range()      # Admin பதிவுக் காலம் அமைத்திருந்தால் அதுவே "இந்த மாதம்"; முந்தைய மாதம் = "தேதி முதல்" மாதத்துக்கு முந்தைய நாட்காட்டி மாதம்
    if rng:
        first, last = (month_bounds(rng[0], 1) if offset else rng)
    else:
        first, last = month_bounds(today_ist(), offset)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.magazine, s.expected_date, r.status FROM lib_schedule s "
                "JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                "WHERE s.code=%s AND s.expected_date BETWEEN %s AND %s "
                "ORDER BY s.expected_date DESC, s.magazine", (session["lib_code"], first, last))
            rows = cur.fetchall()
    finally:
        conn.close()
    pick = lambda st: [{"magazine": r["magazine"], "dateLabel": fmt_d(r["expected_date"])} for r in rows if r["status"] == st]
    label = (f"{first.strftime('%d.%m.%Y')} – {last.strftime('%d.%m.%Y')}" if (rng and not offset)
             else f"{TA_MONTHS[first.month - 1]} {first.year}")
    return jsonify({"success": True, "offset": offset, "label": label, "hasRange": bool(rng),
                    "received": pick("Received"), "notReceived": pick("Not received")})


# =========================================================================== #
# 2. Excel / CSV வாசிப்பு (Quarter ஏற்றுதல்)
# =========================================================================== #
def _iter_sheet_rows(fs):
    """.xlsx / .xls / .csv கோப்பின் வரிசைகளை tuple-ஆகத் தரும் (நெடுவரிசை எண்ணின்படி; தலைப்புப் பெயர் முக்கியமில்லை)."""
    name = (fs.filename or "").lower()
    data = fs.read()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for row in wb.worksheets[0].iter_rows(values_only=True):
            yield row
    elif name.endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        sh = wb.sheet_by_index(0)
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, wb.datemode))
                else:
                    row.append(cell.value)
            yield tuple(row)
    elif name.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            yield tuple(row)
    else:
        raise ValueError("கோப்பு .xlsx, .xls அல்லது .csv ஆக இருக்க வேண்டும்.")


def _to_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < v < 80000:
        from openpyxl.utils.datetime import from_excel
        return from_excel(v).date()
    s = str(v or "").strip()
    if not s:
        return None
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


# =========================================================================== #
# 3. Admin பக்கம் + API-கள்
# =========================================================================== #
@lib_bp.route("/admin/librarian")
@admin_page
def admin_librarian_page():
    return render_template_string(ADMIN_HTML)


@lib_bp.route("/api/admin/librarian/overview")
@admin_api
def api_admin_overview():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM lib_libraries")
            libs = cur.fetchone()["n"]
            cur.execute("SELECT DISTINCT category FROM lib_libraries ORDER BY category")
            cats = sorted([r["category"] for r in cur.fetchall() if r["category"]], key=_category_sort_key)
            cur.execute(
                "SELECT s.quarter, COUNT(*) AS rows, MIN(s.expected_date) AS dfrom, MAX(s.expected_date) AS dto, "
                "       COUNT(DISTINCT s.code) AS libs, COUNT(DISTINCT s.magazine) AS mags, "
                "       COUNT(r.code) AS recorded "
                "FROM lib_schedule s LEFT JOIN lib_receipts r "
                "  ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                "GROUP BY s.quarter ORDER BY MIN(s.expected_date)")
            quarters = [{"quarter": r["quarter"], "rows": r["rows"], "from": fmt_d(r["dfrom"]), "to": fmt_d(r["dto"]),
                         "libs": r["libs"], "mags": r["mags"], "recorded": r["recorded"]} for r in cur.fetchall()]
    finally:
        conn.close()
    ws, we = week_bounds(today_ist())
    return jsonify({"success": True, "libraries": libs, "categories": cats, "quarters": quarters,
                    "weekStart": ws.isoformat(), "weekEnd": we.isoformat(), "today": today_ist().isoformat()})


@lib_bp.route("/api/admin/librarian/upload-quarter", methods=["POST"])
@admin_api
def api_admin_upload_quarter():
    """Quarter Excel (A: Library code, B: Magazine, C: Periodicity, D: Expected Date).
    Quarter தேதியிலிருந்து தானாகக் கணிக்கப்படும். mode=replace (இயல்பு): கோப்பிலுள்ள Quarter-களின் பழைய அட்டவணை மாற்றப்படும்;
    நூலகர் பதிவுகள் (lib_receipts) பாதிக்கப்படாது. mode=add: புதியவை மட்டும் சேர்க்கப்படும்/புதுப்பிக்கப்படும்."""
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return jsonify({"success": False, "message": "கோப்பைத் தேர்ந்தெடுக்கவும்."}), 400
    mode = request.form.get("mode", "replace")
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM lib_libraries")
            known = {r["code"] for r in cur.fetchall()}
        if not known:
            return jsonify({"success": False, "message": "நூலகப் பட்டியல் (code) இன்னும் இல்லை — முதலில் அதை ஏற்றவும்."}), 400
        rows, seen = [], set()
        unknown, bad, dup, bad_samples = Counter(), 0, 0, []
        try:
            for i, row in enumerate(_iter_sheet_rows(fs)):
                if not row or len(row) < 4:
                    continue
                code, mag, per, d = _txt(row[0]), _txt(row[1]), _txt(row[2]), _to_date(row[3])
                if i == 0 and d is None:
                    continue                      # தலைப்பு வரிசை
                if not code and not mag and d is None:
                    continue                      # வெற்று வரிசை
                if not code or not mag or d is None:
                    bad += 1
                    if len(bad_samples) < 5:
                        bad_samples.append(i + 1)
                    continue
                if code not in known:
                    unknown[code] += 1
                    continue
                key = (code, mag, d)
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                rows.append((quarter_label(d), code, mag, per, d))
        except ValueError as e:
            return jsonify({"success": False, "message": str(e)}), 400
        except ImportError:
            return jsonify({"success": False, "message": ".xls படிக்க xlrd தேவை (requirements.txt-ல் xlrd உள்ளது; மீண்டும் Deploy செய்யவும்)."}), 500
        if not rows:
            return jsonify({"success": False, "message": "சரியான வரிசைகள் எதுவும் இல்லை. நெடுவரிசை வரிசை: Library code, Magazine, Periodicity, Expected Date."}), 400
        quarters = sorted({r[0] for r in rows})
        with conn.cursor() as cur:
            if mode == "replace":
                cur.execute("DELETE FROM lib_schedule WHERE quarter = ANY(%s)", (quarters,))
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lib_schedule (quarter, code, magazine, periodicity, expected_date) VALUES %s "
                "ON CONFLICT (code, magazine, expected_date) DO UPDATE "
                "SET periodicity=EXCLUDED.periodicity, quarter=EXCLUDED.quarter",
                rows, page_size=2000)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": "பிழை: " + str(e)}), 500
    finally:
        conn.close()
    per_q = Counter(r[0] for r in rows)
    warn = []
    if unknown:
        warn.append(f"நூலகப் பட்டியலில் இல்லாத code உள்ள {sum(unknown.values())} வரிசைகள் தவிர்க்கப்பட்டன ({', '.join(list(unknown)[:5])}…)")
    if bad:
        warn.append(f"தேதி / இதழ் / code குறைபாடுள்ள {bad} வரிசைகள் தவிர்க்கப்பட்டன (வரிசை எண்: {', '.join(map(str, bad_samples))}…)")
    if dup:
        warn.append(f"இரட்டை வரிசைகள் {dup} புறக்கணிக்கப்பட்டன")
    return jsonify({
        "success": True,
        "message": f"✅ {len(rows)} வரிசைகள் ஏற்றப்பட்டன — " + ", ".join(f"{q}: {n}" for q, n in sorted(per_q.items())),
        "warnings": warn,
        "libraries": len({r[1] for r in rows}), "magazines": len({r[2] for r in rows}),
    })


@lib_bp.route("/api/admin/librarian/delete-quarter", methods=["POST"])
@admin_api
def api_admin_delete_quarter():
    """தவறான Quarter ஏற்றத்தை நீக்க: அந்த Quarter அட்டவணை + அந்த தேதிகளுக்கான நூலகர் பதிவுகளும் நீக்கப்படும்."""
    q = ((request.get_json(force=True, silent=True) or {}).get("quarter") or "").strip()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MIN(expected_date) AS a, MAX(expected_date) AS b FROM lib_schedule WHERE quarter=%s", (q,))
            r = cur.fetchone()
            if not r or not r["a"]:
                return jsonify({"success": False, "message": "அந்த Quarter இல்லை."}), 404
            cur.execute("DELETE FROM lib_receipts WHERE expected_date BETWEEN %s AND %s", (r["a"], r["b"]))
            nr = cur.rowcount
            cur.execute("DELETE FROM lib_schedule WHERE quarter=%s", (q,))
            ns = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "message": f"🗑️ {q}: அட்டவணை {ns} வரிசைகள், நூலகர் பதிவுகள் {nr} நீக்கப்பட்டன."})


@lib_bp.route("/api/admin/librarian/upload-libraries", methods=["POST"])
@admin_api
def api_admin_upload_libraries():
    """நூலகப் பட்டியல்: name, category, code (தலைப்புப் பெயர் இருந்தால் அதன்படி; இல்லையெனில் இதே வரிசை). code வாரியாக UPSERT."""
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return jsonify({"success": False, "message": "கோப்பைத் தேர்ந்தெடுக்கவும்."}), 400
    try:
        data = list(_iter_sheet_rows(fs))
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    except ImportError:
        return jsonify({"success": False, "message": ".xls படிக்க xlrd தேவை."}), 500
    idx = {"name": 0, "category": 1, "code": 2}
    if data:
        head = [_txt(c).lower() for c in data[0]]
        if "code" in head and "name" in head:
            idx = {"name": head.index("name"), "category": head.index("category") if "category" in head else -1,
                   "code": head.index("code")}
            data = data[1:]
        elif head and head[0] in ("name", "library", "library name"):
            data = data[1:]
    rows = {}
    for r in data:
        code = _txt(r[idx["code"]]) if len(r) > idx["code"] else ""
        name = _txt(r[idx["name"]]) if len(r) > idx["name"] else ""
        cat = _txt(r[idx["category"]]) if idx["category"] >= 0 and len(r) > idx["category"] else ""
        if code and name:
            rows[code] = (code, name, cat)
    if not rows:
        return jsonify({"success": False, "message": "சரியான வரிசைகள் இல்லை (name, category, code)."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lib_libraries (code, name, category) VALUES %s ON CONFLICT (code) DO UPDATE "
                "SET name=EXCLUDED.name, category=EXCLUDED.category, updated_at=now()", list(rows.values()))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "message": f"✅ {len(rows)} நூலகங்கள் சேமிக்கப்பட்டன/புதுப்பிக்கப்பட்டன."})


# ---- Admin: பதிவு செய்தவை / இன்னும் வராதவை / பதிவே செய்யாதவை ------------------------------------- #
STATUS_TABS = ("received", "notreceived", "unregistered")


def _delay_days(marked_at, expected):
    """எதிர்பார்த்த தேதிக்குப் பிறகு Received எனக் குறித்த நாட்கள் (தாமதம்)."""
    if not marked_at or not expected:
        return 0
    return max((marked_at.astimezone(IST).date() - expected).days, 0)


def _filters():
    """status (received | notreceived | unregistered), category, from, to, code.
    'unregistered'-க்கு 'to' <= இன்று."""
    status = request.args.get("status", "received")
    if status not in STATUS_TABS:
        status = "received"
    cat = (request.args.get("category") or "").strip()
    dfrom, dto = parse_iso(request.args.get("from")), parse_iso(request.args.get("to"))
    code = (request.args.get("code") or "").strip()
    q = (request.args.get("q") or "").strip()
    where, params = [], []
    if q:   # நூலகம் பெயர் / code தேடல் (பதிவிறக்கத்துக்கு)
        where.append("(l.name ILIKE %s OR l.code ILIKE %s)")
        params += ["%" + q + "%", "%" + q + "%"]
    if cat:
        where.append("l.category = %s")
        params.append(cat)
    if code:
        where.append("l.code = %s")
        params.append(code)
    if status == "received":
        where.append("r.status = 'Received'")
    elif status == "notreceived":
        where.append("r.status = 'Not received'")
    else:
        cap = today_ist()
        dto = min(dto, cap) if dto else cap
    dcol = "s.expected_date" if status == "unregistered" else "r.expected_date"
    if dfrom:
        where.append(dcol + " >= %s")
        params.append(dfrom)
    if dto:
        where.append(dcol + " <= %s")
        params.append(dto)
    return status, where, params


_TZ_DAYS = "((r.marked_at AT TIME ZONE 'Asia/Kolkata')::date - r.expected_date)"


@lib_bp.route("/api/admin/librarian/summary")
@admin_api
def api_admin_summary():
    status, where, params = _filters()
    extra = (" AND " + " AND ".join(where)) if where else ""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM lib_libraries")
            total_libs = cur.fetchone()["n"]
            if status == "unregistered":
                cur.execute(
                    "SELECT l.code, l.name, l.category, COUNT(*) AS cnt, COUNT(DISTINCT s.magazine) AS mags, "
                    "       MIN(s.expected_date) AS first_d, "
                    "       (SELECT COUNT(*) FROM lib_receipts rr WHERE rr.code=l.code) AS recorded_total "
                    "FROM lib_schedule s JOIN lib_libraries l ON l.code=s.code "
                    "LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                    "WHERE r.code IS NULL" + extra + " GROUP BY l.code, l.name, l.category ORDER BY l.name", params)
                items = [{"code": r["code"], "name": r["name"], "category": r["category"], "count": r["cnt"],
                          "magazines": r["mags"], "firstDue": fmt_d(r["first_d"]), "recordedTotal": r["recorded_total"]}
                         for r in cur.fetchall()]
            else:
                cur.execute(
                    "SELECT l.code, l.name, l.category, COUNT(*) AS cnt, COUNT(DISTINCT r.magazine) AS mags, "
                    "       MIN(r.expected_date) AS first_d, MAX(r.marked_at) AS last_at, "
                    f"       SUM(CASE WHEN {_TZ_DAYS} > 0 THEN 1 ELSE 0 END) AS late, "
                    f"       MAX(GREATEST({_TZ_DAYS}, 0)) AS max_late, "
                    "       SUM(CASE WHEN r.marked_by = 'admin' THEN 1 ELSE 0 END) AS by_admin "
                    "FROM lib_receipts r JOIN lib_libraries l ON l.code=r.code "
                    "WHERE TRUE" + extra + " GROUP BY l.code, l.name, l.category ORDER BY l.name", params)
                items = [{"code": r["code"], "name": r["name"], "category": r["category"], "count": r["cnt"],
                          "magazines": r["mags"], "firstDue": fmt_d(r["first_d"]),
                          "late": int(r["late"] or 0), "maxLate": int(r["max_late"] or 0),
                          "byAdmin": int(r["by_admin"] or 0),
                          "lastAt": r["last_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["last_at"] else ""}
                         for r in cur.fetchall()]
            cur.execute("SELECT COUNT(DISTINCT code) AS n FROM lib_receipts")
            ever = cur.fetchone()["n"]
    finally:
        conn.close()
    return jsonify({"success": True, "items": items, "totalLibraries": total_libs, "librariesEverRecorded": ever,
                    "totalRows": sum(i["count"] for i in items), "lateTotal": sum(i.get("late", 0) for i in items)})


def _detail_rows(limit=None):
    status, where, params = _filters()
    extra = (" AND " + " AND ".join(where)) if where else ""
    lim = f" LIMIT {int(limit)}" if limit else ""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if status == "unregistered":
                cur.execute(
                    "SELECT l.code, l.name, l.category, s.magazine, s.periodicity, s.expected_date, NULL::text AS status, "
                    "       NULL::timestamptz AS marked_at, NULL::text AS marked_by "
                    "FROM lib_schedule s JOIN lib_libraries l ON l.code=s.code "
                    "LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                    "WHERE r.code IS NULL" + extra + " ORDER BY l.name, s.expected_date, s.magazine" + lim, params)
            else:
                cur.execute(
                    "SELECT l.code, l.name, l.category, r.magazine, s.periodicity, r.expected_date, r.status, "
                    "       r.marked_at, r.marked_by "
                    "FROM lib_receipts r JOIN lib_libraries l ON l.code=r.code "
                    "LEFT JOIN lib_schedule s ON s.code=r.code AND s.magazine=r.magazine AND s.expected_date=r.expected_date "
                    "WHERE TRUE" + extra + " ORDER BY l.name, r.expected_date, r.magazine" + lim, params)
            return status, cur.fetchall()
    finally:
        conn.close()


def _who(v):
    return "Admin" if v == "admin" else ("நூலகர்" if v else "")


@lib_bp.route("/api/admin/librarian/detail")
@admin_api
def api_admin_detail():
    status, rows = _detail_rows(limit=2000)
    ws, _we = week_bounds(today_ist())
    lo = ws - timedelta(weeks=CARRY_WEEKS)
    rng = _get_range()
    hi = None
    if rng:
        lo, hi = rng[0], min(rng[1], today_ist())
    out = []
    for r in rows:
        mk = r["marked_at"]
        out.append({
            "magazine": r["magazine"], "periodicity": r["periodicity"] or "", "date": fmt_d(r["expected_date"]),
            "status": r["status"] or "",
            "markedAt": mk.astimezone(IST).strftime("%d/%m/%Y %H:%M") if mk else "",
            "markedDate": fmt_d(mk.astimezone(IST).date()) if mk else "",
            "markedBy": _who(r["marked_by"]),
            "delay": _delay_days(mk, r["expected_date"]) if r["status"] == "Received" else 0,
            "onLibrarianScreen": r["expected_date"] >= lo and (hi is None or r["expected_date"] <= hi),
        })
    return jsonify({"success": True, "rows": out})


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _json_of(r):
    """view function-ன் jsonify முடிவிலிருந்து dict (அதே request args பயன்படுத்தும்)."""
    resp = r[0] if isinstance(r, tuple) else r
    return resp.get_json()


def _xlsx_bytes(sheets):
    """sheets = [(name, headers, rows, widths)]"""
    import xlsxwriter
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1, "text_wrap": True})
    for name, headers, rows, widths in sheets:
        ws = wb.add_worksheet(name[:31])
        for c, h in enumerate(headers):
            ws.write(0, c, h, head)
        for i, r in enumerate(rows, start=1):
            for c, v in enumerate(r):
                ws.write(i, c, "" if v is None else v)
        for c, w in enumerate(widths):
            ws.set_column(c, c, w)
        ws.freeze_panes(1, 0)
    wb.close()
    return buf.getvalue()


def _pdf_table(title, info_lines, headers, rows, widths, aligns=None, landscape=True):
    """தமிழ் PDF அட்டவணை (fpdf2 + Noto Sans Tamil)."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    pdf = FPDF(orientation="L" if landscape else "P", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("Tamil", "", os.path.join(_FONT_DIR, "NotoSansTamil-Regular.ttf"))
    pdf.add_font("Tamil", "B", os.path.join(_FONT_DIR, "NotoSansTamil-Bold.ttf"))
    pdf.set_text_shaping(True)
    pdf.add_page()
    for txt, size in [(HEAD_LINES[0], 14), (HEAD_LINES[1], 12), (title, 12)]:
        pdf.set_font("Tamil", "B", size)
        pdf.multi_cell(0, 7, txt, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Tamil", "", 9)
    for ln in info_lines:
        pdf.multi_cell(0, 5, ln, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    tot = float(sum(widths))
    cw = tuple(w * usable / tot for w in widths)
    head = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(30, 77, 140))
    with pdf.table(col_widths=cw, text_align=tuple(aligns or ["LEFT"] * len(headers)), headings_style=head,
                   line_height=5.5, padding=1.2, borders_layout="ALL") as t:
        h = t.row()
        for c in headers:
            h.cell(c)
        if not rows:
            t.row().cell("— பதிவுகள் இல்லை —", colspan=len(headers), align="C")
        for r in rows:
            row = t.row()
            for v in r:
                row.cell("" if v is None else str(v))
    return bytes(pdf.output())


def _export_info(extra=None):
    """வடிகட்டி விவரம் (PDF தலைப்புக்குக் கீழ்)."""
    a = request.args
    out = []
    if (a.get("category") or "").strip():
        out.append("நூலக வகை: " + a.get("category").strip())
    f, t = parse_iso(a.get("from")), parse_iso(a.get("to"))
    if f or t:
        out.append("காலம்: %s – %s" % (fmt_d(f) if f else "…", fmt_d(t) if t else "…"))
    if (a.get("q") or "").strip():
        out.append("தேடல்: " + a.get("q").strip())
    out += (extra or [])
    out.append("தயாரித்த தேதி: " + fmt_d(today_ist()))
    return out


STATUS_TITLES = {"received": "நூலகர் பதிவு நிலை — பதிவு செய்தவை (Received)",
                 "notreceived": "நூலகர் பதிவு நிலை — இன்னும் வராதவை",
                 "unregistered": "நூலகர் பதிவு நிலை — பதிவே செய்யாதவை"}


def _summary_table(status, items):
    """திரையில் உள்ள நூலகம் வாரியான அட்டவணை → (headers, rows, widths, aligns)."""
    if status == "received":
        hd = ["வ.எண்.", "நூலகம்", "வகை", "Received", "தாமதமாகப் பதிவானவை", "அதிக தாமதம் (நாள்)", "Admin பதிவு", "கடைசிப் பதிவு"]
        rows = [[n, i["name"], i["category"], i["count"], i["late"], i["maxLate"] or "", i["byAdmin"] or "", i["lastAt"]]
                for n, i in enumerate(items, 1)]
        return hd, rows, [11, 54, 38, 16, 30, 22, 16, 30], ["C", "L", "L", "C", "C", "C", "C", "C"]
    if status == "notreceived":
        hd = ["வ.எண்.", "நூலகம்", "வகை", "இன்னும் வராதவை", "இதழ்கள்", "பழைய தேதி", "கடைசிப் பதிவு"]
        rows = [[n, i["name"], i["category"], i["count"], i["magazines"], i["firstDue"], i["lastAt"]]
                for n, i in enumerate(items, 1)]
        return hd, rows, [11, 62, 40, 22, 16, 24, 32], ["C", "L", "L", "C", "C", "C", "C"]
    hd = ["வ.எண்.", "நூலகம்", "வகை", "பதிவு செய்யாதவை", "இதழ்கள்", "முதல் தேதி", "இதுவரை பதிவு செய்தவை"]
    rows = [[n, i["name"], i["category"], i["count"], i["magazines"], i["firstDue"], i["recordedTotal"]]
            for n, i in enumerate(items, 1)]
    return hd, rows, [11, 62, 40, 22, 16, 24, 28], ["C", "L", "L", "C", "C", "C", "C"]


def _detail_table(status, rows):
    """இதழ் வாரி விவரம் → (headers, rows, widths, aligns)."""
    def at(r):
        return r["marked_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["marked_at"] else ""
    if status == "received":
        hd = ["நூலகம்", "இதழ்", "Periodicity", "எதிர்பார்த்த தேதி", "பதிவு நேரம்", "பதிவு செய்தவர்", "தாமதம் (நாள்)"]
        out = [[r["name"], r["magazine"], r["periodicity"] or "", fmt_d(r["expected_date"]), at(r), _who(r["marked_by"]),
                _delay_days(r["marked_at"], r["expected_date"])] for r in rows]
        return hd, out, [48, 56, 22, 24, 30, 20, 18], ["L", "L", "L", "C", "C", "C", "C"]
    if status == "notreceived":
        hd = ["நூலகம்", "இதழ்", "Periodicity", "தேதி", "பதிவு நேரம்", "பதிவு செய்தவர்"]
        out = [[r["name"], r["magazine"], r["periodicity"] or "", fmt_d(r["expected_date"]), at(r), _who(r["marked_by"])]
               for r in rows]
        return hd, out, [52, 60, 24, 24, 32, 22], ["L", "L", "L", "C", "C", "C"]
    hd = ["நூலகம்", "இதழ்", "Periodicity", "தேதி"]
    out = [[r["name"], r["magazine"], r["periodicity"] or "", fmt_d(r["expected_date"])] for r in rows]
    return hd, out, [65, 80, 28, 28], ["L", "L", "L", "C"]


@lib_bp.route("/api/admin/librarian/export")
@admin_api
def api_admin_export():
    fmt = (request.args.get("fmt") or "xlsx").lower()
    status, rows = _detail_rows(limit=3000 if fmt == "pdf" else None)
    stamp = today_ist().isoformat()
    if fmt == "pdf":
        if (request.args.get("code") or "").strip():   # ஒரு நூலகம் — இதழ் வாரி விவரம்
            hd, trs, w, al = _detail_table(status, rows)
            nm = rows[0]["name"] if rows else (request.args.get("code") or "")
            info = _export_info(["நூலகம்: " + nm, "வரிசைகள்: %d" % len(trs)])
        else:                                           # திரையில் உள்ள நூலகம் வாரி அட்டவணை
            d = _json_of(api_admin_summary())
            hd, trs, w, al = _summary_table(status, d["items"])
            info = _export_info(["நூலகங்கள்: %d / %d" % (len(d["items"]), d["totalLibraries"]),
                                 "மொத்த வரிசைகள்: %d" % d["totalRows"]])
        body = _pdf_table(STATUS_TITLES[status], info, hd, trs, w, al, landscape=True)
        return Response(body, mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=librarian_{status}_{stamp}.pdf"})
    # Excel: தாள் 1 = நூலகம் வாரி சுருக்கம், தாள் 2 = இதழ் வாரி விவரம்
    names = {"received": "Received", "notreceived": "Not received yet", "unregistered": "Not registered"}
    d = _json_of(api_admin_summary())
    shd, srows, sw, _al = _summary_table(status, d["items"])
    cols = ["Library Code", "Library", "Category", "Magazine", "Periodicity", "Expected Date"]
    if status != "unregistered":
        cols += ["Status", "Marked At", "Marked By"]
    if status == "received":
        cols += ["Delay (days)"]
    drows = []
    for r in rows:
        vals = [r["code"], r["name"], r["category"], r["magazine"], r["periodicity"] or "", fmt_d(r["expected_date"])]
        if status != "unregistered":
            vals += [r["status"], r["marked_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["marked_at"] else "",
                     _who(r["marked_by"])]
        if status == "received":
            vals += [_delay_days(r["marked_at"], r["expected_date"])]
        drows.append(vals)
    body = _xlsx_bytes([("Summary " + names[status], shd, srows, [max(8, int(x * 0.7)) for x in sw]),
                        (names[status], cols, drows, [14, 38, 22, 34, 14, 14, 14, 18, 12, 12])])
    return Response(body, mimetype=XLSX_MIME,
                    headers={"Content-Disposition": f"attachment; filename=librarian_{status}_{stamp}.xlsx"})


# ---- Admin: சுருக்கம் — பதிவு செய்யாதவை (Received / Not received எதுவும் குறிக்காதவை) ----------------- #
def _pend_range():
    """(from, to) — to எப்போதும் இன்றைய தேதிக்குள் (எதிர்காலத் தேதிகள் 'பதிவு செய்யாதவை' அல்ல)."""
    dfrom, dto = parse_iso(request.args.get("from")), parse_iso(request.args.get("to"))
    cap = today_ist()
    return dfrom, (min(dto, cap) if dto else cap), dto


@lib_bp.route("/api/admin/librarian/pending-summary")
@admin_api
def api_admin_pending_summary():
    """நூலகம் வாரியாக, இதழ் (magazine) எண்ணிக்கையில்:
       அனுமதிக்கப்பட்ட இதழ்கள் = அட்டவணையில் உள்ள தனித்த இதழ்கள்;
       பதிவு செய்யாதவை = இன்று வரை வர வேண்டிய தேதிகளில் குறைந்தது ஒன்றுக்காவது Received / Not received குறிக்காத இதழ்கள்;
       பதிவு செய்தவை = மீதமுள்ளவை (இன்று வரை வர வேண்டிய எல்லாத் தேதிகளுக்கும் பதிவு உள்ளவை, அல்லது இன்னும் வர வேண்டிய தேதியே இல்லாதவை).
       ஆகவே பதிவு செய்தவை + பதிவு செய்யாதவை = அனுமதிக்கப்பட்ட இதழ்கள்."""
    dfrom, dto_cap, dto_raw = _pend_range()
    cat = (request.args.get("category") or "").strip()
    rng_w, rng_p = [], []
    if dfrom:
        rng_w.append("s.expected_date >= %s"); rng_p.append(dfrom)
    if dto_raw:
        rng_w.append("s.expected_date <= %s"); rng_p.append(dto_raw)
    sql = ("SELECT l.code, l.name, l.category, COUNT(m.magazine) AS allowed, COALESCE(SUM(m.pend), 0) AS unreg "
           "FROM lib_libraries l LEFT JOIN ("
           " SELECT s.code, s.magazine, "
           "  MAX(CASE WHEN r.code IS NULL AND s.expected_date <= %s THEN 1 ELSE 0 END) AS pend "
           " FROM lib_schedule s LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine "
           "  AND r.expected_date=s.expected_date "
           + (" WHERE " + " AND ".join(rng_w) if rng_w else "") +
           " GROUP BY s.code, s.magazine) m ON m.code=l.code "
           + ("WHERE l.category=%s " if cat else "") +
           "GROUP BY l.code, l.name, l.category")
    params = [dto_cap] + rng_p + ([cat] if cat else [])
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    items = []
    for r in rows:
        allowed, unreg = int(r["allowed"] or 0), int(r["unreg"] or 0)
        items.append({"code": r["code"], "name": r["name"], "category": r["category"],
                      "allowed": allowed, "registered": allowed - unreg, "unregistered": unreg})
    items.sort(key=lambda i: (_category_sort_key(i["category"]), i["name"]))
    return jsonify({"success": True, "items": items, "upTo": fmt_d(dto_cap)})


@lib_bp.route("/api/admin/librarian/pending-detail")
@admin_api
def api_admin_pending_detail():
    """ஒரு நூலகம்: இதழ் வாரியாகப் பதிவு செய்யாத தேதிகள்."""
    code = (request.args.get("code") or "").strip()
    dfrom, dto_cap, _raw = _pend_range()
    where, params = ["s.code = %s", "r.code IS NULL", "s.expected_date <= %s"], [code, dto_cap]
    if dfrom:
        where.append("s.expected_date >= %s"); params.append(dfrom)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, category FROM lib_libraries WHERE code=%s", (code,))
            lib = cur.fetchone()
            if not lib:
                return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
            cur.execute(
                "SELECT s.magazine, MAX(s.periodicity) AS periodicity, s.expected_date "
                "FROM lib_schedule s LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine "
                " AND r.expected_date=s.expected_date WHERE " + " AND ".join(where) +
                " GROUP BY s.magazine, s.expected_date ORDER BY s.magazine, s.expected_date", params)
            rows = cur.fetchall()
    finally:
        conn.close()
    by = {}
    for r in rows:
        g = by.setdefault(r["magazine"], {"magazine": r["magazine"], "periodicity": r["periodicity"] or "", "dates": []})
        g["dates"].append(r["expected_date"])
    items = []
    for m in sorted(by.values(), key=lambda x: x["magazine"].casefold()):
        d = m["dates"]
        items.append({"magazine": m["magazine"], "periodicity": m["periodicity"], "count": len(d),
                      "first": fmt_d(d[0]), "last": fmt_d(d[-1]), "dates": [fmt_d(x) for x in d]})
    return jsonify({"success": True, "library": {"code": code, "name": lib["name"], "category": lib["category"]},
                    "items": items, "total": sum(i["count"] for i in items), "upTo": fmt_d(dto_cap)})


@lib_bp.route("/api/admin/librarian/pending-export")
@admin_api
def api_admin_pending_export():
    """சுருக்கம் — பதிவு செய்யாதவை: Excel / PDF (திரையில் உள்ள வடிகட்டிகளுடன்)."""
    fmt = (request.args.get("fmt") or "xlsx").lower()
    code = (request.args.get("code") or "").strip()
    q = (request.args.get("q") or "").strip().lower()
    flt = (request.args.get("flt") or "").strip()
    stamp = today_ist().isoformat()
    if code:
        d = _json_of(api_admin_pending_detail())
        if not d.get("success"):
            return jsonify(d), 404
        hd = ["வ.எண்.", "இதழ் பெயர்", "Periodicity", "பதிவு செய்யாத இதழ்கள்", "காலம்", "தேதிகள்"]
        trs = [[n, m["magazine"], m["periodicity"], m["count"],
                m["first"] if m["first"] == m["last"] else m["first"] + " – " + m["last"], ", ".join(m["dates"])]
               for n, m in enumerate(d["items"], 1)]
        widths, al = [11, 55, 22, 24, 38, 126], ["C", "L", "L", "C", "C", "L"]
        info = _export_info(["நூலகம்: " + d["library"]["name"],
                             "பதிவு செய்யாத தேதிகள் (மொத்தம்): %d (%s வரை)" % (d["total"], d["upTo"])])
        fname = f"pending_{code}_{stamp}"
    else:
        d = _json_of(api_admin_pending_summary())
        items = [i for i in d["items"]
                 if (not q or q in i["name"].lower() or q in i["code"].lower())
                 and (flt != "pending" or i["unregistered"] > 0) and (flt != "clean" or i["unregistered"] == 0)]
        hd = ["வ.எண்.", "நூலகம் பெயர்", "வகை", "அனுமதிக்கப்பட்ட இதழ்கள்", "பதிவு செய்தவை", "பதிவு செய்யாதவை"]
        trs = [[n, i["name"], i["category"], i["allowed"], i["registered"], i["unregistered"]]
               for n, i in enumerate(items, 1)]
        widths, al = [11, 82, 48, 32, 26, 28], ["C", "L", "L", "C", "C", "C"]
        extra = []
        if flt:
            extra.append("வடிகட்டி: " + ("பதிவு செய்யாதவை உள்ள நூலகங்கள் மட்டும்" if flt == "pending"
                                         else "எல்லாம் பதிவு செய்த நூலகங்கள் மட்டும்"))
        extra.append("நூலகங்கள்: %d · பதிவு செய்யாத இதழ்கள் (மொத்தம்): %d (%s வரை)"
                     % (len(items), sum(i["unregistered"] for i in items), d["upTo"]))
        info = _export_info(extra)
        fname = f"pending_summary_{stamp}"
    title = "சுருக்கம் — பதிவு செய்யாதவை (Received / Not received எதுவும் குறிக்காதவை)"
    if fmt == "pdf":
        return Response(_pdf_table(title, info, hd, trs, widths, al, landscape=True), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={fname}.pdf"})
    return Response(_xlsx_bytes([("பதிவு செய்யாதவை", hd, trs, [max(8, int(w * 0.8)) for w in widths])]),
                    mimetype=XLSX_MIME, headers={"Content-Disposition": f"attachment; filename={fname}.xlsx"})


# ---- Admin: ஒரு நூலகத்துக்காக Admin பதிவு செய்தல் ------------------------------------------------ #
def _weeks_arg(v, default=CARRY_WEEKS):
    """weeks=0 -> வரம்பின்றி (None)."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return None if n <= 0 else min(n, 520)


@lib_bp.route("/api/admin/librarian/libraries")
@admin_api
def api_admin_libraries():
    return _libraries_json()


@lib_bp.route("/api/admin/librarian/mark-list")
@admin_api
def api_admin_mark_list():
    code = (request.args.get("code") or "").strip()
    data = _library_week_payload(code, _weeks_arg(request.args.get("weeks")))
    if data is None:
        return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
    return jsonify(data)


@lib_bp.route("/api/admin/librarian/mark-save", methods=["POST"])
@admin_api
def api_admin_mark_save():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM lib_libraries WHERE code=%s", (code,))
            ok = cur.fetchone()
    finally:
        conn.close()
    if not ok:
        return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
    return _save_entries(code, body.get("entries") or [], None, "admin")


@lib_bp.route("/api/admin/librarian/range", methods=["GET", "POST"])
@admin_api
def api_admin_range():
    """நூலகர் பதிவுத் திரையில் காட்ட வேண்டிய தேதி வரம்பு — Admin அமைக்கும்/அழிக்கும்."""
    if request.method == "GET":
        rng = _get_range()
        return jsonify({"success": True, "carryWeeks": CARRY_WEEKS,
                        "from": rng[0].isoformat() if rng else "", "to": rng[1].isoformat() if rng else ""})
    body = request.get_json(force=True, silent=True) or {}
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if body.get("clear"):
                cur.execute("DELETE FROM lib_settings WHERE key IN ('reg_from','reg_to')")
                msg = "↺ வரம்பு நீக்கப்பட்டது — இனி இந்த வாரம் + முந்தைய %d வாரங்கள் காட்டப்படும்." % CARRY_WEEKS
            else:
                a, b = parse_iso(body.get("from")), parse_iso(body.get("to"))
                if not a or not b:
                    return jsonify({"success": False, "message": "இரண்டு தேதிகளையும் தேர்ந்தெடுக்கவும்."}), 400
                if a > b:
                    return jsonify({"success": False, "message": "\"தேதி முதல்\" என்பது \"தேதி வரை\"க்குப் பிந்தையதாக இருக்கக்கூடாது."}), 400
                for k, v in (("reg_from", a.isoformat()), ("reg_to", b.isoformat())):
                    cur.execute("INSERT INTO lib_settings (key, value) VALUES (%s, %s) "
                                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, v))
                msg = "✅ நூலகர் பதிவுக் காலம்: %s – %s" % (a.strftime("%d.%m.%Y"), b.strftime("%d.%m.%Y"))
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": msg})


@lib_bp.route("/api/admin/librarian/mark-clear-all", methods=["POST"])
@admin_api
def api_admin_mark_clear_all():
    """ஒரு நூலகத்தின் Received / Not received பதிவுகள் அனைத்தும் (எல்லா வாரங்களும்) நீக்கம் — Admin மட்டும்."""
    code = ((request.get_json(force=True, silent=True) or {}).get("code") or "").strip()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM lib_libraries WHERE code=%s", (code,))
            lib = cur.fetchone()
            if not lib:
                return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
            cur.execute("DELETE FROM lib_receipts WHERE code=%s", (code,))
            n = cur.rowcount
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "deleted": n, "message": f"🗑 {lib['name']} — {n} பதிவுகள் நீக்கப்பட்டன."})


# =========================================================================== #
# HTML (தனித்து இயங்கும் பக்கங்கள்)
# =========================================================================== #
BASE_CSS = """
*{box-sizing:border-box} body{margin:0;font-family:'Noto Sans Tamil','Segoe UI',Latha,Tahoma,sans-serif;background:#eef2f9;color:#1b2540}
.top{background:linear-gradient(135deg,#0f2347,#1e4d8c);color:#fff;padding:14px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.top h1{font-size:18px;margin:0;flex:1;min-width:200px} .top a{color:#fff;text-decoration:none;background:rgba(255,255,255,.16);padding:7px 12px;border-radius:8px;font-size:13px}
.wrap{max-width:980px;margin:18px auto;padding:0 14px 90px}
.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 4px 18px rgba(15,35,71,.08);margin-bottom:16px}
label{display:block;font-size:13px;font-weight:600;color:#1e4d8c;margin:12px 0 6px}
select,input[type=date],input[type=file]{width:100%;padding:11px 12px;border:1.5px solid #c5cee3;border-radius:10px;font-size:15px;background:#fff;color:#111}
button,.btn{cursor:pointer;border:0;border-radius:10px;padding:11px 18px;font-size:15px;font-weight:700;background:#1e4d8c;color:#fff}
button:hover{background:#0f2347} button:disabled{opacity:.5;cursor:not-allowed}
.msg{margin-top:12px;padding:10px 12px;border-radius:10px;font-size:14px;display:none} .msg.ok{background:#e6f6ea;color:#1b6b34;display:block} .msg.err{background:#fdecea;color:#a12b20;display:block}
.muted{color:#5a6685;font-size:13px}
"""

SELECT_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>நூலகத் தேர்வு</title>
<style>""" + BASE_CSS + """.wrap{max-width:520px}</style></head><body>
<div class="top"><h1>📚 நூலகர் நுழைவு</h1><a href="/logout">வெளியேறு</a></div>
<div class="wrap"><div class="card">
  <h2 style="margin:0 0 4px;font-size:18px">உங்கள் நூலகத்தைத் தேர்ந்தெடுக்கவும்</h2>
  <p class="muted" style="margin:0">முதலில் நூலக வகை, பிறகு நூலகத்தின் பெயர்.</p>
  <label for="cat">நூலக வகை</label>
  <select id="cat"><option value="">— தேர்ந்தெடுக்கவும் —</option></select>
  <label for="lib">நூலகத்தின் பெயர்</label>
  <select id="lib" disabled><option value="">— முதலில் வகையைத் தேர்ந்தெடுக்கவும் —</option></select>
  <button id="go" style="width:100%;margin-top:20px" disabled>தொடர்க ➜</button>
  <div id="msg" class="msg"></div>
</div></div>
<script>
let DATA=[];const cat=document.getElementById('cat'),lib=document.getElementById('lib'),go=document.getElementById('go'),msg=document.getElementById('msg');
function opt(v,t){const o=document.createElement('option');o.value=v;o.textContent=t;return o}
fetch('/api/librarian/libraries').then(r=>r.json()).then(d=>{
  if(!d.success){msg.className='msg err';msg.textContent=d.message||'பிழை';return}
  DATA=d.categories;DATA.forEach(c=>cat.appendChild(opt(c.category,c.category+' ('+c.libraries.length+')')));
}).catch(()=>{msg.className='msg err';msg.textContent='சேவையகத்தை அணுக முடியவில்லை.'});
cat.onchange=()=>{lib.innerHTML='';const c=DATA.find(x=>x.category===cat.value);
  if(!c){lib.appendChild(opt('','— முதலில் வகையைத் தேர்ந்தெடுக்கவும் —'));lib.disabled=true;go.disabled=true;return}
  lib.appendChild(opt('','— நூலகத்தைத் தேர்ந்தெடுக்கவும் —'));c.libraries.forEach(l=>lib.appendChild(opt(l.code,l.name)));lib.disabled=false;go.disabled=true};
lib.onchange=()=>{go.disabled=!lib.value};
go.onclick=async()=>{go.disabled=true;
  const r=await fetch('/api/librarian/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:lib.value})});
  const d=await r.json();if(d.success)location.href='/librarian/dashboard';else{msg.className='msg err';msg.textContent=d.message||'பிழை';go.disabled=false}};
</script></body></html>"""

DASH_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>இதழ்கள் வருகை பதிவு</title>
<style>""" + BASE_CSS + """
.day{background:#1e4d8c;color:#fff;border-radius:10px;padding:9px 14px;margin:16px 0 8px;font-weight:700;font-size:16px}
.sec{border-radius:12px;padding:13px 16px;margin:24px 0 8px;font-weight:800;font-size:19px}
.sec.unreg{background:#fff3cd;color:#7a5600;border:1.5px solid #f0c75e}
.sec.pend{background:#fdecea;color:#a12b20;border:1.5px solid #f2b8b1}
.sec.this{background:#e8eefb;color:#0f2347;border:1.5px solid #b9c7e6}
.row{background:#fff;border-radius:12px;padding:12px 14px;margin-bottom:8px;box-shadow:0 2px 8px rgba(15,35,71,.07);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.row .nm{flex:1;min-width:180px;font-weight:700;font-size:16px} .row .nm small{display:block;color:#6b7694;font-weight:400;font-size:12px;margin-top:2px}
.opts{display:flex;gap:8px} .opts label{margin:0;display:block}
.opts input{display:none}
.opts span{display:inline-block;padding:9px 14px;border-radius:9px;border:1.5px solid #c5cee3;font-size:14px;font-weight:600;color:#44506f;background:#fff;cursor:pointer;user-select:none}
.opts input:checked + span.rc{background:#1b8a43;border-color:#1b8a43;color:#fff}
.opts input:checked + span.nr{background:#c0392b;border-color:#c0392b;color:#fff}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #d6ddee;padding:10px 14px;display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;z-index:5}
.bar .info{font-size:14px;color:#1b2540;font-weight:600} .bar .alt{background:#e8eefb;color:#1e4d8c}
.chip{display:inline-block;background:#e8eefb;color:#1e4d8c;border-radius:999px;padding:3px 10px;font-size:12px;margin-right:6px}
.top{padding:22px 24px}
.ttl{flex:1;min-width:260px;text-align:center;line-height:1.5}
.ttl .l1{font-size:clamp(22px,4.2vw,36px);font-weight:800} .ttl .l2{font-size:clamp(19px,3.4vw,29px);font-weight:700} .ttl .l3{font-size:clamp(19px,3.4vw,29px);font-weight:800;color:#ffe08a} .ttl .l4{font-size:clamp(18px,3.1vw,26px);font-weight:700;margin-top:4px;border-top:2px solid rgba(255,255,255,.4);padding-top:6px;display:inline-block}
.nav{display:flex;gap:8px;flex-wrap:wrap}
.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.tools input[type=search]{flex:1;min-width:200px;padding:11px 12px;border:1.5px solid #c5cee3;border-radius:10px;font-size:15px}
.dl{display:none;gap:8px;margin-top:10px;flex-wrap:wrap} .dl.show{display:flex}
.dl .btn2{background:#e8eefb;color:#1e4d8c;border:1.5px solid #b9c7e6}
.boxes{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
@media(max-width:640px){.boxes{grid-template-columns:1fr}}
.bx{border-radius:14px;overflow:hidden;background:#fff;box-shadow:0 4px 18px rgba(15,35,71,.08)}
.bx h3{margin:0;padding:12px 14px;font-size:19px;color:#fff;line-height:1.35}
.bx.rc h3{background:#1b8a43} .bx.nr h3{background:#c0392b}
.mq{height:150px;overflow-y:auto;padding:6px 14px;scrollbar-width:thin}
.mq div.it{padding:7px 0;border-bottom:1px dashed #d6ddee;font-size:16px;font-weight:600}
.mq .none{color:#6b7694;font-size:13px;padding:10px 0}
.seg{display:flex;gap:8px;margin:0 0 12px;flex-wrap:wrap}
.seg button{background:#e8eefb;color:#1e4d8c;border:2px solid #b9c7e6;font-size:17px;padding:10px 20px}
.seg button.on{background:#1e4d8c;color:#fff;border-color:#1e4d8c}
.hint2{margin-top:8px;font-size:13px;color:#5a6685;width:100%}
.opts span.lock{opacity:.55;border-style:dashed}
#nrov{position:fixed;inset:0;background:rgba(15,35,71,.55);z-index:60;display:none;align-items:center;justify-content:center;padding:16px}
#nrov .box{background:#fff;border-radius:16px;max-width:420px;width:100%;padding:20px 22px;box-shadow:0 10px 40px rgba(0,0,0,.35);border-top:6px solid #e0a100}
#nrov h3{margin:0 0 10px;font-size:18px;color:#7a5600}
#nrov p{margin:0 0 10px;font-size:15px;line-height:1.55;color:#1b2540}
#nrov .until{background:#fff3cd;border-radius:10px;padding:10px 12px;font-weight:800;color:#7a5600;margin:12px 0}
.prof{margin-top:14px;border:1.5px solid #b9c7e6;background:#f5f8fe;border-radius:12px;padding:12px 14px}
.prof h4{margin:0 0 8px;font-size:15px;color:#1e4d8c;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.prof h4 button{padding:5px 12px;font-size:13px;background:#e8eefb;color:#1e4d8c;border:1.5px solid #b9c7e6}
.prof .pr{display:flex;gap:8px;margin:4px 0;font-size:15px;line-height:1.5} .prof .pr b{min-width:96px;color:#44506f;font-weight:600}
.prof .pr span{flex:1;white-space:pre-line;font-weight:600}
.prof input[type=text],.prof textarea{width:100%;padding:10px 12px;border:1.5px solid #c5cee3;border-radius:10px;font-size:15px;font-family:inherit}
.prof textarea{min-height:88px;resize:vertical}
.prof .warn{display:none;margin-top:6px;padding:8px 10px;border-radius:8px;background:#fdecea;color:#a12b20;font-size:14px;font-weight:700;border:1.5px solid #f2b8b1}
.prof input.bad,.prof textarea.bad{border-color:#c0392b;background:#fff7f6}
.prof .acts{display:flex;gap:8px;margin-top:10px}
.prof .acts .cn{background:#e8eefb;color:#1e4d8c}
#toast{position:fixed;left:50%;top:40%;transform:translate(-50%,-50%);background:#1b6b34;color:#fff;padding:16px 28px;border-radius:14px;font-size:18px;font-weight:800;box-shadow:0 8px 30px rgba(0,0,0,.3);z-index:50;display:none}
</style></head><body>
<div class="top"><div class="ttl"><div class="l1">தமிழ்நாடு அரசு பொது நூலகத்துறை</div><div class="l2">மாவட்ட நூலக ஆணைக்குழு, திண்டுக்கல்</div><div class="l3" id="libName">…</div><div class="l4">பருவ இதழ் வருகைப் பதிவேடு</div></div>
<div class="nav"><a href="/librarian/change">நூலகத்தை மாற்று</a><a href="/logout">வெளியேறு</a></div></div>
<div class="wrap">
  <div class="card" id="head"><div class="muted">ஏற்றுகிறது…</div></div>
  <div class="card">
    <button id="ordBtn" type="button">📋 உங்கள் நூலகத்திற்கு ஆணையிடப்பட்ட இதழ்கள்</button>
    <div class="dl" id="dl"><a class="btn btn2" href="/api/librarian/ordered-export?fmt=pdf">⬇️ PDF பதிவிறக்கம்</a><a class="btn btn2" href="/api/librarian/ordered-export?fmt=xlsx">⬇️ Excel பதிவிறக்கம்</a></div>
    <div class="prof" id="prof">
      <h4>👤 LIBRARIAN DETAILS <button type="button" id="pEdit" style="display:none">✏️ திருத்து</button></h4>
      <div id="pView"><div class="muted">ஏற்றுகிறது…</div></div>
      <div id="pForm" style="display:none">
        <label for="pName">LIBRARIAN NAME</label><input type="text" id="pName" maxlength="100" autocomplete="off"><div class="warn" id="wName">⚠️ தயவு செய்து ஆங்கிலத்தில் உள்ளிடவும் (Please enter in English)</div>
        <label for="pPhone">MOBILE NUMBER</label><input type="text" id="pPhone" maxlength="10" inputmode="numeric" pattern="[0-9]{10}" autocomplete="off" placeholder="10 இலக்க எண்"><div class="warn" id="wPhone">⚠️ MOBILE NUMBER சரியாக 10 இலக்கங்கள் இருக்க வேண்டும்</div>
        <label for="pAddr">ADDRESS</label><textarea id="pAddr" maxlength="500"></textarea><div class="warn" id="wAddr">⚠️ தயவு செய்து ஆங்கிலத்தில் உள்ளிடவும் (Please enter in English)</div>
        <div class="acts"><button type="button" id="pSave">💾 சேமி</button><button type="button" class="cn" id="pCancel">ரத்து</button></div>
        <div id="pMsg" class="msg"></div>
      </div>
    </div>
  </div>
  <div class="seg"><button type="button" id="mCur" class="on">📅 இந்த மாதம்</button><button type="button" id="mPrev">⏪ முந்தைய மாதம்</button></div>
  <div class="boxes">
    <div class="bx rc"><h3 id="rcT">🟢 இந்த மாதம் வந்த இதழ்கள்</h3><div class="mq" id="mqRc"></div></div>
    <div class="bx nr"><h3 id="nrT">🔴 இந்த மாதம் வராத இதழ்கள்</h3><div class="mq" id="mqNr"></div></div>
  </div>
  <div class="card tools"><input type="search" id="q" placeholder="🔍 இதழின் பெயரைத் தேடுக…" autocomplete="off"><div class="hint2">💡 "வரவில்லை" எனக் குறித்த இதழ் பின்னர் வந்தால்: இங்கே பெயரைத் தேடி, ✅ Received தேர்ந்து, 💾 சேமி அழுத்தவும்.<br>🔄 ஒவ்வொரு 5 தேர்வுகளுக்கும் தானாகச் சேமிக்கப்படும்; எல்லா இதழ்களையும் தேர்ந்து முடித்ததும் மீதமுள்ளவையும் தானாகச் சேமிக்கப்படும். 💾 சேமி பொத்தானும் உள்ளது.</div></div>
  <div id="list"></div>
  <div id="msg" class="msg"></div>
</div>
<div id="toast"></div>
<div id="nrov"><div class="box"><h3>⏳ சற்றுக் காத்திருங்கள்</h3>
<p id="nrtxt"></p><div class="until" id="nruntil"></div>
<p>தபால் மூலம் வரும் இதழ்கள் கிடைக்கத் தாமதமாகலாம். இந்தத் தேதி வரை காத்திருந்து, அதன் பின்னரும் வரவில்லை எனில் <b>Not received</b> என பதிவு செய்யுங்கள்.</p>
<button id="nrok" style="width:100%">சரி</button></div></div>
<div class="bar" id="bar" style="display:none">
  <span class="info" id="cnt"></span>
  <button class="alt" id="allRc" type="button">இந்த வாரம் — எல்லாவற்றையும் Received எனக் குறி</button>
  <button id="save" type="button">💾 சேமி</button>
</div>
<script>
const list=document.getElementById('list'),msg=document.getElementById('msg'),bar=document.getElementById('bar'),cnt=document.getElementById('cnt');
let ROWS=[];
const AUTO_N=5,dirty=new Map();let autoBusy=false;
function el(t,a,x){const e=document.createElement(t);if(a)for(const k in a)e.setAttribute(k,a[k]);if(x!==undefined)e.textContent=x;return e}
function update(){const n=ROWS.length,m=document.querySelectorAll('.opts input:checked').length;
  cnt.textContent='தேர்ந்தவை: '+m+' / '+n+(dirty.size?'  ·  சேமிக்கப்படாதவை: '+dirty.size:'')}
function allPicked(){return ROWS.length>0&&document.querySelectorAll('.opts input:checked').length===ROWS.length}
function maybeAuto(){if(dirty.size&&(dirty.size>=AUTO_N||allPicked()))autoSave()}
function onPick(i,r,v){dirty.set(i,{magazine:r.magazine,date:r.date,status:v});update();maybeAuto()}
async function autoSave(){
  if(autoBusy||!dirty.size)return;autoBusy=true;
  const snap=Array.from(dirty.entries());
  try{const r=await fetch('/api/librarian/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries:snap.map(x=>x[1])})});
    const d=await r.json();
    if(d.success){snap.forEach(([k,e])=>{const c=dirty.get(k);if(c&&c.status===e.status)dirty.delete(k)});
      msg.className='msg';msg.textContent='';update();toast('✅ '+snap.length+' இதழ்கள் தானாகச் சேமிக்கப்பட்டன');loadBoxes()}
    else{msg.className='msg err';msg.textContent='தானாகச் சேமிக்க முடியவில்லை: '+(d.message||'பிழை')}}
  catch(e){msg.className='msg err';msg.textContent='தானாகச் சேமிக்க முடியவில்லை: '+e}
  finally{autoBusy=false;if(!msg.classList.contains('err'))maybeAuto()}}
window.addEventListener('beforeunload',e=>{if(dirty.size){e.preventDefault();e.returnValue=''}});
function fillBox(id,t,title,names){
  const b=document.getElementById(id);b.innerHTML='';b.scrollTop=0;document.getElementById(t).textContent=title+' ('+names.length+')';
  if(!names.length){b.appendChild(el('div',{class:'none'},'— இல்லை —'));return}
  const add=()=>names.forEach(n=>b.appendChild(el('div',{class:'it'},n)));add();
  if(b.scrollHeight>b.clientHeight+4){add();b.dataset.loop=b.scrollHeight/2}else b.dataset.loop=''}
const paused={mqRc:false,mqNr:false};
['mqRc','mqNr'].forEach(id=>{const b=document.getElementById(id);
  ['mouseenter','touchstart','focusin'].forEach(e=>b.addEventListener(e,()=>{paused[id]=true}));
  ['mouseleave','touchend','touchcancel','focusout'].forEach(e=>b.addEventListener(e,()=>{setTimeout(()=>{paused[id]=false},1500)}))});
setInterval(()=>{['mqRc','mqNr'].forEach(id=>{const b=document.getElementById(id),h=parseFloat(b.dataset.loop);
  if(!h||paused[id])return;b.scrollTop+=1;if(b.scrollTop>=h)b.scrollTop-=h})},45);
let MOFF=0;
async function loadBoxes(){
  try{const r=await fetch('/api/librarian/month?offset='+MOFF);const d=await r.json();if(!d.success)return;
    document.getElementById('mCur').textContent=d.hasRange?'📅 பதிவுக் காலம்':'📅 இந்த மாதம்';
    const p=MOFF?('முந்தைய மாதம் ('+d.label+')'):(d.hasRange?d.label:('இந்த மாதம் ('+d.label+')'));
    const f=x=>x.magazine+' — '+x.dateLabel;
    fillBox('mqRc','rcT','🟢 '+p+' வந்த இதழ்கள்',d.received.map(f));
    fillBox('mqNr','nrT','🔴 '+p+' வராத இதழ்கள்',d.notReceived.map(f))}catch(e){}}
function setMonth(o){MOFF=o;document.getElementById('mCur').className=o?'':'on';document.getElementById('mPrev').className=o?'on':'';loadBoxes()}
function applyFilter(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  document.querySelectorAll('#list .row').forEach(r=>{r.style.display=(!q||(r.getAttribute('data-n')||'').includes(q))?'':'none'});
  document.querySelectorAll('#list .day').forEach(dy=>{let n=dy.nextElementSibling,any=false;
    while(n&&n.classList.contains('row')){if(n.style.display!=='none')any=true;n=n.nextElementSibling}
    dy.style.display=(!q||any)?'':'none'});
  document.querySelectorAll('#list .sec').forEach(s=>{let n=s.nextElementSibling,any=false,hint=null;
    while(n&&!n.classList.contains('sec')&&n.tagName!=='DETAILS'){
      if(n.classList.contains('row')&&n.style.display!=='none')any=true;
      if(n.classList.contains('muted'))hint=n;n=n.nextElementSibling}
    s.style.display=(!q||any)?'':'none';if(hint)hint.style.display=(!q||any)?'':'none'});
  document.querySelectorAll('#list details').forEach(dt=>{
    const any=Array.from(dt.querySelectorAll('.row')).some(r=>r.style.display!=='none');
    dt.style.display=(!q||any)?'':'none';if(q&&any)dt.open=true})}
function showNR(r){
  document.getElementById('nrtxt').textContent='"'+r.magazine+'" — '+r.dateDot+' இதழ் இன்னும் வராததாகப் பதிவு செய்ய இது நேரம் இல்லை.';
  document.getElementById('nruntil').textContent='📅 '+r.waitUntilDot+' வரை காத்திருங்கள்';
  document.getElementById('nrov').style.display='flex'}
document.getElementById('nrok').onclick=()=>{document.getElementById('nrov').style.display='none'};
document.getElementById('nrov').onclick=e=>{if(e.target.id==='nrov')e.target.style.display='none'};
function toast(t){const e=document.getElementById('toast');e.textContent=t;e.style.display='block';clearTimeout(toast.t);toast.t=setTimeout(()=>{e.style.display='none'},2200)}
function render(d){
  const prevOpen=!!document.querySelector('#list details[open]');
  loadBoxes();
  const h=document.getElementById('head');h.innerHTML='';
  document.getElementById('libName').textContent=d.library.name||'';
  const s=el('div',{style:'margin-top:6px'});s.appendChild(el('span',{class:'chip'},d.library.category||''));s.appendChild(el('span',{class:'chip'},d.library.code||''));
  s.appendChild(el('span',{class:'chip'},d.range?('📅 பதிவுக் காலம்: '+d.range.from+' – '+d.range.to):('📅 இந்த வாரம்: '+d.weekStart+' – '+d.weekEnd)));h.appendChild(s);
  const early=d.earlierUnregistered.length+d.earlierNotReceived.length;
  if(early&&!d.range)h.appendChild(el('div',{class:'muted',style:'margin-top:8px'},'முந்தைய வாரங்களின் இதழ்கள் அதிகபட்சம் '+d.carryWeeks+' வாரங்கள் வரை இங்கே தெரியும்.'));
  ROWS=[];dirty.clear();list.innerHTML='';
  function addRows(parent,rows,k,early){
    let last='';
    rows.forEach(r=>{
      if(r.date!==last){last=r.date;parent.appendChild(el('div',{class:'day'},r.day+' — '+r.dateLabel+(r.date===d.today?'  (இன்று)':'')+(early&&r.weeksAgo?'  · '+r.weeksAgo+' வாரத்துக்கு முன்':'')))}
      const i=ROWS.push({magazine:r.magazine,date:r.date})-1;
      const row=el('div',{class:'row','data-n':r.magazine.toLowerCase()});const nm=el('div',{class:'nm'},r.magazine);if(r.periodicity)nm.appendChild(el('small',null,r.periodicity));row.appendChild(nm);
      const o=el('div',{class:'opts'});
      [['Received','rc','✅ Received'],['Not received','nr','❌ Not received']].forEach(([v,c,t])=>{
        const l=el('label');const inp=el('input',{type:'radio',name:'r'+i,value:v,'data-sec':k});if(r.status===v)inp.checked=true;inp.onchange=()=>onPick(i,r,v);
        const locked=(v==='Not received'&&!r.nrAllowed&&r.status!=='Not received');
        l.appendChild(inp);l.appendChild(el('span',{class:c+(locked?' lock':'')},(locked?'🔒 ':'')+t));
        if(locked)l.addEventListener('click',ev=>{ev.preventDefault();showNR(r)});
        o.appendChild(l)});
      row.appendChild(o);parent.appendChild(row)})
  }
  const openRows=d.rows.filter(r=>r.status!=='Received'),doneRows=d.rows.filter(r=>r.status==='Received');
  [['⚠️ கடந்த வாரங்களில் பதிவு செய்யாதவை','unreg',d.earlierUnregistered,'இவை இன்னும் பதிவு செய்யப்படவில்லை — Received / Not received தேர்ந்து சேமிக்கவும்.',true],
   ['⏳ முந்தைய வாரங்களில் இன்னும் வராதவை','pend',d.earlierNotReceived,'முன்பு "Not received" எனக் குறித்தவை — இதழ் வந்திருந்தால் Received எனக் குறிக்கவும்; வராவிட்டால் அப்படியே விடவும்.',true],
   ['📅 இந்த வாரம் — இன்னும் பதிவாகாதவை / வராதவை ('+d.weekStart+' – '+d.weekEnd+')','this',openRows,'',false]].forEach(([title,k,rows,hint,early])=>{
    if(!rows.length)return;
    list.appendChild(el('div',{class:'sec '+k},title+' ('+rows.length+')'));
    if(hint)list.appendChild(el('div',{class:'muted',style:'margin:0 2px 4px'},hint));
    addRows(list,rows,k,early)});
  if(!d.rows.length)list.appendChild(el('div',{class:'card muted',style:'margin-top:14px'},d.range?'இந்தப் பதிவுக் காலத்தில் ('+d.range.from+' – '+d.range.to+') பதிவு செய்ய வேண்டிய இதழ்கள் எதுவும் இல்லை.':'இந்த வாரத்துக்கு ('+d.weekStart+' – '+d.weekEnd+') வர வேண்டிய இதழ்கள் எதுவும் இல்லை.'));
  else if(!openRows.length)list.appendChild(el('div',{class:'card',style:'margin-top:14px;background:#e6f6ea;color:#1b6b34;font-weight:700'},'✅ இந்த வாரத்துக்கான எல்லா இதழ்களும் Received எனப் பதிவாகிவிட்டன.'));
  if(doneRows.length){
    const det=el('details',{style:'margin-top:18px'});
    det.appendChild(el('summary',{style:'cursor:pointer;font-weight:700;color:#1b6b34;padding:8px 2px'},'✅ இந்த வாரம் Received எனப் பதிவானவை ('+doneRows.length+') — மாற்ற வேண்டுமானால் திறக்கவும்'));
    addRows(det,doneRows,'this',false);if(prevOpen)det.open=true;list.appendChild(det)}
  if(!ROWS.length){bar.style.display='none';return}
  bar.style.display='flex';update();applyFilter();
}
async function load(){
  const r=await fetch('/api/librarian/week');const d=await r.json();
  if(d.needLibrary){location.href='/librarian/select';return}
  if(!d.success){msg.className='msg err';msg.textContent=d.message||'பிழை';return}
  render(d)}
document.getElementById('q').addEventListener('input',applyFilter);
document.getElementById('mCur').onclick=()=>setMonth(0);
document.getElementById('mPrev').onclick=()=>setMonth(1);
document.getElementById('ordBtn').onclick=()=>document.getElementById('dl').classList.toggle('show');
document.getElementById('allRc').onclick=()=>{
  document.querySelectorAll('.opts input[value="Received"][data-sec="this"]').forEach(inp=>{
    if(!inp.checked){inp.checked=true;const i=parseInt(inp.name.slice(1),10);dirty.set(i,{magazine:ROWS[i].magazine,date:ROWS[i].date,status:'Received'})}});
  update();maybeAuto()};
document.getElementById('save').onclick=async()=>{
  const entries=[];ROWS.forEach((r,i)=>{const c=document.querySelector('input[name="r'+i+'"]:checked');if(c)entries.push({magazine:r.magazine,date:r.date,status:c.value})});
  if(!entries.length){msg.className='msg err';msg.textContent='குறைந்தது ஒரு இதழுக்காவது Received / Not received தேர்ந்தெடுக்கவும்.';return}
  const b=document.getElementById('save');b.disabled=true;
  try{const r=await fetch('/api/librarian/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})});
    const d=await r.json();const y=window.scrollY;if(d.success){msg.className='msg';msg.textContent='';await load();window.scrollTo(0,y);toast('✅ சேமிக்கப்பட்டது')}else{msg.className='msg err';msg.textContent=d.message||'பிழை'}}
  catch(e){msg.className='msg err';msg.textContent='சேமிக்க முடியவில்லை: '+e}finally{b.disabled=false}};
let PROF={name:'',phone:'',address:''};
function showProf(){
  const v=document.getElementById('pView');v.innerHTML='';
  [['LIBRARIAN NAME',PROF.name],['MOBILE NUMBER',PROF.phone],['ADDRESS',PROF.address]].forEach(([k,x])=>{
    const r=el('div',{class:'pr'});r.appendChild(el('b',null,k));r.appendChild(el('span',null,x||'— பதிவு இல்லை —'));v.appendChild(r)});
  document.getElementById('pView').style.display='';document.getElementById('pForm').style.display='none';
  document.getElementById('pEdit').style.display=''}
async function loadProf(){
  try{const r=await fetch('/api/librarian/profile');const d=await r.json();
    if(d.success){PROF={name:d.name,phone:d.phone,address:d.address};showProf()}
    else document.getElementById('pView').textContent=d.message||'பிழை'}
  catch(e){document.getElementById('pView').textContent='விவரங்களை ஏற்ற முடியவில்லை.'}}
document.getElementById('pEdit').onclick=()=>{
  ['pName','pAddr'].forEach(i=>document.getElementById(i).classList.remove('bad'));document.getElementById('wName').style.display='none';document.getElementById('wAddr').style.display='none';document.getElementById('wPhone').style.display='none';document.getElementById('pPhone').classList.remove('bad');
  document.getElementById('pName').value=PROF.name;document.getElementById('pPhone').value=PROF.phone;document.getElementById('pAddr').value=PROF.address;
  const m=document.getElementById('pMsg');m.className='msg';m.textContent='';
  document.getElementById('pView').style.display='none';document.getElementById('pEdit').style.display='none';document.getElementById('pForm').style.display=''};
document.getElementById('pCancel').onclick=showProf;
const TAMIL=/[\u0B80-\u0BFF]/;
function chkTa(inp,w){const bad=TAMIL.test(inp.value);document.getElementById(w).style.display=bad?'block':'none';inp.classList.toggle('bad',bad);return bad}
function chkPh(){const i=document.getElementById('pPhone');const v=i.value.replace(/[^0-9]/g,'');if(v!==i.value)i.value=v;
  const bad=v.length>0&&v.length<10;document.getElementById('wPhone').style.display=bad?'block':'none';i.classList.toggle('bad',bad);return v.length!==10}
['input','keyup','change','blur'].forEach(ev=>document.getElementById('pPhone').addEventListener(ev,chkPh));
['pName:wName','pAddr:wAddr'].forEach(x=>{const [i,w]=x.split(':'),e=document.getElementById(i);
  ['input','keyup','compositionupdate','change'].forEach(ev=>e.addEventListener(ev,()=>chkTa(e,w)))});
document.getElementById('pSave').onclick=async()=>{
  const m=document.getElementById('pMsg'),b=document.getElementById('pSave');
  if(chkTa(document.getElementById('pName'),'wName')|chkTa(document.getElementById('pAddr'),'wAddr')){m.className='msg err';m.textContent='தயவு செய்து ஆங்கிலத்தில் உள்ளிடவும் (Please enter in English).';return}
  if(chkPh()){document.getElementById('wPhone').style.display='block';m.className='msg err';m.textContent='MOBILE NUMBER சரியாக 10 இலக்கங்கள் இருக்க வேண்டும்.';return}
  b.disabled=true;
  try{const r=await fetch('/api/librarian/profile-save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:document.getElementById('pName').value,phone:document.getElementById('pPhone').value,address:document.getElementById('pAddr').value})});
    const d=await r.json();
    if(d.success){PROF={name:d.name,phone:d.phone,address:d.address};showProf();toast('✅ Saved')}
    else{m.className='msg err';m.textContent=d.message||'பிழை'}}
  catch(e){m.className='msg err';m.textContent='சேமிக்க முடியவில்லை: '+e}finally{b.disabled=false}};
loadProf();
load();
</script></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>நூலகர் பதிவு — Admin</title>
<style>""" + BASE_CSS + """
.wrap{max-width:1100px} h2{margin:0 0 8px;font-size:17px;color:#0f2347}
.tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap} .tab{background:#e8eefb;color:#1e4d8c;flex:1;min-width:150px} .tab.on{background:#1e4d8c;color:#fff}
.flt{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;align-items:end}
.flt>div{min-width:0} .flt input,.flt select{max-width:100%;box-sizing:border-box} .btnrow{display:flex;flex-wrap:wrap;gap:8px}
.frow{display:flex;gap:6px;align-items:flex-end;flex-wrap:nowrap} .frow>div{flex:1 1 90px;min-width:0}
@media(max-width:940px){.frow{flex-wrap:wrap} .frow>div{flex-basis:130px}} .frow>div.dt{flex:0 0 122px} .frow>div.bt{flex:0 0 auto;display:flex;gap:5px}
.frow label{margin:0 0 4px;font-size:12px;white-space:nowrap} .frow select,.frow input[type=date]{padding:8px 5px;font-size:13px}
.frow button{padding:8px 9px;font-size:13px;white-space:nowrap}
table{width:100%;border-collapse:collapse;font-size:14px} th{background:#1e4d8c;color:#fff;text-align:left;padding:8px 10px;position:sticky;top:0}
td{padding:8px 10px;border-bottom:1px solid #e3e8f4;vertical-align:top} tr.lib{cursor:pointer} tr.lib:hover{background:#f3f6fd}
.sub{background:#f8faff} .sub table th{background:#5b7fb8}
.ok-c{color:#1b6b34;font-weight:700} .bad-c{color:#c0392b;font-weight:700} .warn-c{color:#b26a00;font-weight:700}
.kpi{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0} .kpi div{background:#e8eefb;border-radius:10px;padding:8px 14px;font-size:14px}
.sm{padding:6px 12px;font-size:13px} .danger{background:#c0392b} .alt{background:#e8eefb;color:#1e4d8c}
.scroll{overflow-x:auto} details summary{cursor:pointer;font-weight:700;color:#1e4d8c}
.mh{border-radius:10px;padding:9px 12px;margin:16px 0 6px;font-weight:800;font-size:14px}
.mh.unreg{background:#fff3cd;color:#7a5600} .mh.pend{background:#fdecea;color:#a12b20} .mh.this{background:#e8eefb;color:#0f2347}
.md{background:#1e4d8c;color:#fff;border-radius:8px;padding:6px 10px;margin:10px 0 6px;font-weight:700;font-size:13px}
.mr{background:#fff;border:1px solid #e3e8f4;border-radius:10px;padding:10px 12px;margin-bottom:6px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.mr .nm{flex:1;min-width:180px;font-weight:600} .mr .nm small{display:block;color:#6b7694;font-weight:400;font-size:12px}
.opts{display:flex;gap:8px} .opts label{margin:0;display:block} .opts input{display:none}
.opts span{display:inline-block;padding:8px 13px;border-radius:9px;border:1.5px solid #c5cee3;font-size:14px;font-weight:600;color:#44506f;background:#fff;cursor:pointer;user-select:none}
.opts input:checked + span.rc{background:#1b8a43;border-color:#1b8a43;color:#fff}
.opts input:checked + span.nr{background:#c0392b;border-color:#c0392b;color:#fff}
.opts span.cl{font-size:13px;color:#6b7694;border-style:dashed}
.opts input:checked + span.cl{background:#6b7694;border-color:#6b7694;border-style:solid;color:#fff}
#cfov{position:fixed;inset:0;background:rgba(15,35,71,.55);z-index:60;display:none;align-items:center;justify-content:center;padding:16px}
#cfov .box{background:#fff;border-radius:16px;max-width:440px;width:100%;padding:20px 22px;box-shadow:0 10px 40px rgba(0,0,0,.35);border-top:6px solid #c0392b}
#cfov h3{margin:0 0 10px;font-size:18px;color:#a12b20} #cfov p{margin:0 0 14px;font-size:15px;line-height:1.55}
.mbar{position:sticky;bottom:0;background:#fff;border-top:1px solid #d6ddee;padding:10px 0;margin-top:12px;display:none;gap:10px;align-items:center;flex-wrap:wrap}
.chip{display:inline-block;background:#e8eefb;color:#1e4d8c;border-radius:999px;padding:3px 10px;font-size:12px;margin-right:6px}
</style></head><body>
<div class="top"><h1>🧑‍💼 நூலகர் பதிவு (இதழ் வருகை)</h1><a href="/">← முகப்பு</a></div>
<div class="wrap">

<div class="card">
  <h2>① Quarter Excel ஏற்றுதல் (Q3 / Q4 …)</h2>
  <p class="muted" style="margin:0 0 6px">நெடுவரிசை வரிசை: <b>A – Library code, B – Magazine Name, C – Periodicity, D – Expected Date</b> (தலைப்பு வரிசை இருக்கலாம்). கோப்பு .xlsx / .xls / .csv. Quarter தேதியிலிருந்து தானாகக் கணிக்கப்படும்.</p>
  <label>Excel கோப்பு</label><input type="file" id="qfile" accept=".xlsx,.xls,.csv">
  <label style="display:flex;gap:8px;align-items:center;font-weight:500;color:#1b2540"><input type="checkbox" id="qreplace" checked style="width:auto"> இந்தக் கோப்பிலுள்ள Quarter-ன் பழைய அட்டவணையை மாற்று (நூலகர் பதிவுகள் அழியாது)</label>
  <button id="qgo" style="margin-top:12px">⬆️ ஏற்று</button>
  <div id="qmsg" class="msg"></div>
  <div class="scroll" style="margin-top:14px"><table id="qtab"><thead><tr><th>Quarter</th><th>வரிசைகள்</th><th>தேதிகள்</th><th>நூலகங்கள்</th><th>இதழ்கள்</th><th>பதிவானவை</th><th></th></tr></thead><tbody></tbody></table></div>
</div>

<div class="card">
  <details><summary>நூலகப் பட்டியல் (name, category, code) புதுப்பிக்க</summary>
    <p class="muted">தற்போது <b id="libcnt">?</b> நூலகங்கள் உள்ளன. புதிய நூலகம் சேர்க்க/பெயர் மாற்ற, name · category · code உள்ள கோப்பை ஏற்றவும் (code வாரியாகப் புதுப்பிக்கப்படும்).</p>
    <input type="file" id="lfile" accept=".xlsx,.xls,.csv"><button id="lgo" class="sm" style="margin-top:10px">⬆️ ஏற்று</button><div id="lmsg" class="msg"></div>
  </details>
</div>

<div class="card">
  <h2>② நூலகர் பதிவு நிலை</h2>
  <div class="tabs"><button class="tab on" id="t1">✅ பதிவு செய்தவை (Received)</button><button class="tab" id="t2">⏳ இன்னும் வராதவை</button><button class="tab" id="t3">⚠️ பதிவே செய்யாதவை</button></div>
  <div class="frow">
    <div><label>நூலக வகை</label><select id="fcat"><option value="">எல்லாம்</option></select></div>
    <div><label>நூலகம்</label><select id="flib"><option value="">எல்லா நூலகங்களும் (சுருக்கம்)</option></select></div>
    <div class="dt"><label>தேதி முதல்</label><input type="date" id="ffrom"></div>
    <div class="dt"><label>தேதி வரை</label><input type="date" id="fto"></div>
    <div class="bt"><button id="fgo">🔍 காட்டு</button><button id="fhide" class="alt" type="button" style="display:none">🔼 மறை</button><button id="fweek" class="alt">இந்த வாரம்</button><button id="fperiod" class="alt" type="button" title="நூலகர் பதிவுக் காலத்தை மீண்டும் இடு">↺ பதிவுக் காலம்</button></div>
  </div>
  <div class="kpi" id="kpi"></div>
  <div style="margin:6px 0 10px"><button id="fxl" class="sm alt">📥 Excel பதிவிறக்கு</button> <button id="fpdf" class="sm alt">📄 PDF பதிவிறக்கு</button> <span class="muted">வரிசையைக் கிளிக் செய்தால் இதழ்கள் விரியும் · "✏️ பதிவு" பொத்தான் அந்த நூலகத்துக்கு Admin பதிவுக்குக் கொண்டு செல்லும்</span></div>
  <div class="scroll"><table id="stab"><thead></thead><tbody></tbody></table></div>
  <div id="fhidebar" style="display:none;margin-top:10px;text-align:center"><button id="fhide2" class="alt sm" type="button">🔼 மறை</button></div>
</div>

<div class="card" id="pcard">
  <h2>📊 சுருக்கம் — பதிவு செய்யாதவை (Received / Not received எதுவும் குறிக்காதவை)</h2>
  <p class="muted" style="margin:0 0 6px">எல்லா எண்ணிக்கையும் <b>இதழ்கள்</b> அடிப்படையில் (வார / இருவார / மாத இதழ் எதுவானாலும் ஒரு இதழ் = 1). <b>அனுமதிக்கப்பட்ட இதழ்கள்</b> = அந்த நூலகத்துக்கு அட்டவணையில் உள்ள இதழ்கள். <b>பதிவு செய்யாதவை</b> = இன்று வரை வர வேண்டிய தேதிகளில் குறைந்தது ஒன்றுக்காவது Received / Not received குறிக்காத இதழ்கள். <b>பதிவு செய்தவை</b> = மீதமுள்ளவை (செய்தவை + செய்யாதவை = அனுமதிக்கப்பட்டவை). வரிசையைக் கிளிக் செய்தால் அந்த நூலகத்தின் இதழ் விவரம் வரும்.</p>
  <div class="frow">
    <div><label>நூலக வகை</label><select id="pcat"><option value="">எல்லாம்</option></select></div>
    <div><label>நூலகத்தின் பெயர்</label><select id="plib"><option value="">— எல்லா நூலகங்களும் (சுருக்கம்) —</option></select></div>
    <div class="dt"><label>தேதி முதல்</label><input type="date" id="pfrom"></div>
    <div class="dt"><label>தேதி வரை</label><input type="date" id="pto"></div>
    <div style="flex:0 1 130px"><label>நிலை</label><select id="pflt"><option value="">எல்லாம்</option><option value="pending">பதிவு செய்யாதவை உள்ளவை</option><option value="clean">எல்லாம் பதிவு செய்தவை</option></select></div>
    <div class="bt"><button id="pgo" type="button">🔍 காட்டு</button><button id="phide" class="alt" type="button" style="display:none">🔼 மறை</button><button id="pperiod" class="alt" type="button" title="நூலகர் பதிவுக் காலத்தை மீண்டும் இடு">↺ பதிவுக் காலம்</button></div>
  </div>
  <div class="btnrow" style="margin:8px 0"><button id="pxl" class="sm alt" type="button">📥 Excel பதிவிறக்கு</button><button id="ppdf" class="sm alt" type="button">📄 PDF பதிவிறக்கு</button></div>
  <div class="kpi" id="pkpi"></div>
  <div class="scroll"><table id="ptab"><thead></thead><tbody></tbody></table></div>
  <div id="phidebar" style="display:none;margin-top:10px;text-align:center"><button id="phide2" class="alt sm" type="button">🔼 மறை</button></div>
</div>

<div class="card" id="mcard">
  <h2>③ நூலகத்துக்காக Admin பதிவு (Received / Not received)</h2>
  <p class="muted" style="margin:0 0 6px">ஒரு நூலகத்தைத் தேர்ந்தெடுத்து, நூலகருக்குப் பதிலாக நீங்களே பதிவு செய்யலாம். இந்த வாரம் மற்றும் அதற்கு முந்தைய தேதிகள் மட்டும்; எதிர்காலத் தேதிகள் முடியாது. இங்கே பதிவானவை "Admin" எனக் குறிக்கப்படும்.</p>
  <div class="flt">
    <div><label>நூலக வகை</label><select id="mcat"><option value="">— தேர்ந்தெடுக்கவும் —</option></select></div>
    <div><label>நூலகத்தின் பெயர்</label><select id="mlib"><option value="">— நூலகம் —</option></select></div>
    <div><label>முந்தைய வாரங்கள்</label><select id="mweeks"><option value="4">4 வாரம்</option><option value="8">8 வாரம்</option><option value="13">13 வாரம்</option><option value="26">26 வாரம்</option><option value="0">எல்லாம்</option></select></div>
    <div><button id="mload">📋 இதழ்களைக் காட்டு</button></div>
  </div>
  <div id="mmsg" class="msg"></div>
  <div id="mhead" style="margin-top:12px"></div>
  <div id="mlist"></div>
  <div class="mbar" id="mbar"><span id="mcnt" style="font-weight:600"></span><button id="mallrc" class="alt" type="button">இந்த வாரம் — எல்லாம் Received</button><button id="msave" type="button">💾 சேமி (Admin)</button></div>
  <div id="mclrw" style="display:none;margin-top:14px;padding-top:12px;border-top:1px dashed #c5cee3"><button id="mclr" class="danger sm" type="button">🗑 இந்த நூலகத்தின் எல்லாப் பதிவுகளையும் நீக்கு</button> <span class="muted">எல்லா வாரங்களின் Received / Not received பதிவுகளும் நீங்கும்</span></div>
</div>
<div class="card" id="rcard">
  <h2>④ நூலகர் பதிவுக் காலம் (தேதி வரம்பு)</h2>
  <p class="muted" style="margin:0 0 6px">நூலகர் திரையில் எந்தத் தேதி முதல் எந்தத் தேதி வரையிலான இதழ்கள் பதிவுக்குக் காட்டப்பட வேண்டும் என்பதை இங்கே அமைக்கவும். எதிர்காலத் தேதிகளை நூலகர் பதிய முடியாது. வரம்பு அமைக்காவிட்டால் இந்த வாரம் + முந்தைய வாரங்கள் காட்டப்படும். இது Admin ③ திரையைப் பாதிக்காது.</p>
  <div class="flt">
    <div><label>தேதி முதல்</label><input type="date" id="rfrom"></div>
    <div><label>தேதி வரை</label><input type="date" id="rto"></div>
    <div style="display:flex;gap:8px"><button id="rsave" type="button">💾 சேமி</button><button id="rclr" class="alt" type="button">↺ தானியங்கி</button></div>
  </div>
  <div id="rmsg" class="msg"></div>
  <div id="rstat" class="muted" style="margin-top:8px"></div>
</div>
</div>
<div id="cfov"><div class="box"><h3>⚠️ பதிவுகளை நீக்கவா?</h3><p id="cftxt"></p>
<div style="display:flex;gap:10px"><button id="cfno" class="alt" style="flex:1" type="button">ரத்து</button><button id="cfyes" class="danger" style="flex:1" type="button">ஆம், நீக்கு</button></div></div></div>
<script>
let MODE='received',OV=null,LIBS=[],MROWS=[],MCODE='';
const $=id=>document.getElementById(id);
function el(t,a,x){const e=document.createElement(t);if(a)for(const k in a)e.setAttribute(k,a[k]);if(x!==undefined)e.textContent=x;return e}
function td(t,cls){const c=el('td',cls?{class:cls}:null,t===undefined?'':String(t));return c}
function say(id,ok,t,warn){const m=$(id);m.className='msg '+(ok?'ok':'err');m.textContent=t;(warn||[]).forEach(w=>{m.appendChild(el('div',{style:'margin-top:4px'},'⚠️ '+w))})}
async function api(u,o){const r=await fetch(u,o);if(r.status===401){location.href='/login';throw 0}return r.json()}

async function loadOverview(){
  OV=await api('/api/admin/librarian/overview');
  $('libcnt').textContent=OV.libraries;
  const fc=$('fcat');fc.length=1;OV.categories.forEach(c=>{const o=el('option',{value:c},c);fc.appendChild(o)});
  const pc=$('pcat'),pk=pc.value;pc.length=1;OV.categories.forEach(c=>pc.appendChild(el('option',{value:c},c)));pc.value=pk;
  const tb=$('qtab').querySelector('tbody');tb.innerHTML='';
  if(!OV.quarters.length){const tr=el('tr');const c=td('இன்னும் எந்த Quarter-ம் ஏற்றப்படவில்லை.','muted');c.colSpan=7;tr.appendChild(c);tb.appendChild(tr)}
  OV.quarters.forEach(q=>{const tr=el('tr');[q.quarter,q.rows,q.from+' – '+q.to,q.libs,q.mags,q.recorded].forEach(v=>tr.appendChild(td(v)));
    const c=el('td');const b=el('button',{class:'sm danger',type:'button'},'🗑️ நீக்கு');
    b.onclick=async()=>{if(!confirm(q.quarter+' அட்டவணையையும் அதற்கான '+q.recorded+' நூலகர் பதிவுகளையும் நீக்கவா?'))return;
      const d=await api('/api/admin/librarian/delete-quarter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({quarter:q.quarter})});
      say('qmsg',d.success,d.message);loadOverview();loadSummary()};
    c.appendChild(b);tr.appendChild(c);tb.appendChild(tr)})
}
$('qgo').onclick=async()=>{
  const f=$('qfile').files[0];if(!f){say('qmsg',false,'கோப்பைத் தேர்ந்தெடுக்கவும்.');return}
  const fd=new FormData();fd.append('file',f);fd.append('mode',$('qreplace').checked?'replace':'add');
  $('qgo').disabled=true;say('qmsg',true,'ஏற்றுகிறது… (பெரிய கோப்புக்கு சில விநாடிகள் ஆகலாம்)');
  try{const d=await api('/api/admin/librarian/upload-quarter',{method:'POST',body:fd});say('qmsg',d.success,d.message,d.warnings);if(d.success){$('qfile').value='';loadOverview();loadSummary()}}
  catch(e){say('qmsg',false,'ஏற்ற முடியவில்லை (நேரம் முடிந்திருக்கலாம்): '+e)}finally{$('qgo').disabled=false}};
$('lgo').onclick=async()=>{
  const f=$('lfile').files[0];if(!f){say('lmsg',false,'கோப்பைத் தேர்ந்தெடுக்கவும்.');return}
  const fd=new FormData();fd.append('file',f);const d=await api('/api/admin/librarian/upload-libraries',{method:'POST',body:fd});
  say('lmsg',d.success,d.message);if(d.success){loadOverview();loadLibs()}};

let SUMD=null,PSUM=null;
function filterSum(d){return d}
function filterPend(d){const f=$('pflt').value;
  return Object.assign({},d,{items:d.items.filter(i=>(f==='pending'?i.unregistered>0:f==='clean'?i.unregistered===0:true))})}
function qs(extra){const p=new URLSearchParams({status:MODE});if($('fcat').value)p.set('category',$('fcat').value);if($('flib').value)p.set('code',$('flib').value);
  if($('ffrom').value)p.set('from',$('ffrom').value);if($('fto').value)p.set('to',$('fto').value);for(const k in (extra||{}))p.set(k,extra[k]);return p.toString()}
const HEADS={
  received:['நூலகம்','வகை','Received','தாமதமாகப் பதிவானவை','அதிக தாமதம் (நாள்)','Admin பதிவு','கடைசிப் பதிவு',''],
  notreceived:['நூலகம்','வகை','இன்னும் வராதவை','இதழ்கள்','பழைய தேதி','கடைசிப் பதிவு',''],
  unregistered:['நூலகம்','வகை','பதிவு செய்யாதவை','இதழ்கள்','முதல் தேதி','இதுவரை பதிவு செய்தவை','']};
function buildDetailTable(rows){
      const t=el('table');const h=el('tr');
      (MODE==='received'?['இதழ்','Periodicity','எதிர்பார்த்தது → வந்தது','பதிவு நேரம்','பதிவு செய்தவர்']:MODE==='notreceived'?['தேதி','இதழ்','Periodicity','பதிவு நேரம்','பதிவு செய்தவர்','நூலகர் திரையில்']:['தேதி','இதழ்','Periodicity','நூலகர் திரையில்']).forEach(x=>h.appendChild(el('th',null,x)));t.appendChild(h);
      rows.forEach(w=>{const rr=el('tr');
        if(MODE==='received'){rr.appendChild(td(w.magazine));rr.appendChild(td(w.periodicity));
          rr.appendChild(td(w.date+' → '+(w.markedDate||'')+(w.delay>0?' ('+w.delay+' நாள் தாமதம்)':''),w.delay>0?'warn-c':'ok-c'));rr.appendChild(td(w.markedAt));rr.appendChild(td(w.markedBy))}
        else if(MODE==='notreceived'){rr.appendChild(td(w.date));rr.appendChild(td(w.magazine));rr.appendChild(td(w.periodicity));rr.appendChild(td(w.markedAt));rr.appendChild(td(w.markedBy));rr.appendChild(td(w.onLibrarianScreen?'✔ தெரியும்':'— (நூலகர் திரைக்கு வெளியே)'))}
        else{rr.appendChild(td(w.date));rr.appendChild(td(w.magazine));rr.appendChild(td(w.periodicity));rr.appendChild(td(w.onLibrarianScreen?'✔ தெரியும்':'— (நூலகர் திரைக்கு வெளியே)'))}
        t.appendChild(rr)});
  return t}
async function loadLibDetail(){
  // ஒரு தனி நூலகம் தேர்ந்தால்: இதழ் வாரி விவரம் நேரடியாக
  const th=$('stab').querySelector('thead'),tb=$('stab').querySelector('tbody'),k=$('kpi');th.innerHTML='';tb.innerHTML='';k.innerHTML='';
  const r=await api('/api/admin/librarian/detail?'+qs());
  const sel=$('flib').selectedOptions[0];const nm=sel?sel.textContent:'';
  const lbl={received:'Received இதழ்கள்',notreceived:'இன்னும் வராத இதழ்கள்',unregistered:'பதிவு செய்யப்படாத இதழ்கள்'}[MODE];
  k.appendChild(el('div',null,'📚 '+nm+' — '+lbl+': '+r.rows.length));
  if(MODE==='received'){const late=r.rows.filter(w=>w.delay>0).length;k.appendChild(el('div',null,'தாமதமாகப் பதிவானவை: '+late))}
  const tr=el('tr'),c=el('td');c.colSpan=8;
  const bar=el('div',{style:'margin-bottom:8px'});const mb=el('button',{class:'sm alt',type:'button'},'✏️ இந்த நூலகத்துக்குப் பதிவு');
  mb.onclick=()=>openMark($('flib').value,$('fcat').value||((LIBS.find(x=>x.libraries.some(l=>l.code===$('flib').value))||{}).category||''));bar.appendChild(mb);c.appendChild(bar);
  if(!r.rows.length)c.appendChild(el('div',{class:'muted'},'பதிவுகள் இல்லை.'));
  else{c.appendChild(buildDetailTable(r.rows));if(r.rows.length>=2000)c.appendChild(el('div',{class:'muted'},'முதல் 2000 வரிசைகள் மட்டும் — முழுமைக்கு Excel பதிவிறக்கவும்.'))}
  tr.appendChild(c);tb.appendChild(tr)}
function fillFlib(){
  const f=$('flib'),keep=f.value,cat=$('fcat').value;f.length=1;
  (LIBS||[]).filter(x=>!cat||x.category===cat).forEach(x=>x.libraries.forEach(l=>f.appendChild(el('option',{value:l.code},l.name))));
  if([...f.options].some(o=>o.value===keep))f.value=keep}
$('fcat').onchange=()=>{$('flib').value='';fillFlib()};
async function loadSummary(){
  $('fhide').style.display='';$('fhidebar').style.display='';
  if(typeof loadPend==='function')loadPend();
  if($('flib').value)return loadLibDetail();
  SUMD=await api('/api/admin/librarian/summary?'+qs());renderSummary()}
function renderSummary(){
  const d=filterSum(SUMD);
  const th=$('stab').querySelector('thead'),tb=$('stab').querySelector('tbody');th.innerHTML='';tb.innerHTML='';
  const hr=el('tr');HEADS[MODE].forEach(h=>hr.appendChild(el('th',null,h)));th.appendChild(hr);
  const k=$('kpi');k.innerHTML='';
  if(MODE==='received'){k.appendChild(el('div',null,'Received பதிவு செய்த நூலகங்கள்: '+d.items.length+' / '+d.totalLibraries));k.appendChild(el('div',null,'மொத்த Received: '+d.totalRows));k.appendChild(el('div',null,'தாமதமாகப் பதிவானவை: '+d.lateTotal))}
  else if(MODE==='notreceived'){k.appendChild(el('div',null,'இன்னும் வராத இதழ்கள் உள்ள நூலகங்கள்: '+d.items.length+' / '+d.totalLibraries));k.appendChild(el('div',null,'வராத இதழ் வரிசைகள்: '+d.totalRows))}
  else{k.appendChild(el('div',null,'பதிவு செய்யாத பதிவுகள் உள்ள நூலகங்கள்: '+d.items.length+' / '+d.totalLibraries));k.appendChild(el('div',null,'பதிவு செய்யப்படாத வரிசைகள் (இன்று வரை): '+d.totalRows));
    const never=d.items.filter(i=>i.recordedTotal===0).length;k.appendChild(el('div',null,'இதுவரை ஒன்றுமே பதிவு செய்யாத நூலகங்கள்: '+never))}
  if(!d.items.length){const tr=el('tr');const c=td('பதிவுகள் இல்லை.','muted');c.colSpan=8;tr.appendChild(c);tb.appendChild(tr)}
  d.items.forEach(i=>{
    const tr=el('tr',{class:'lib'});tr.appendChild(td(i.name));tr.appendChild(td(i.category));
    if(MODE==='received'){tr.appendChild(td(i.count,'ok-c'));tr.appendChild(td(i.late,i.late?'warn-c':''));tr.appendChild(td(i.maxLate||''));tr.appendChild(td(i.byAdmin||''));tr.appendChild(td(i.lastAt))}
    else if(MODE==='notreceived'){tr.appendChild(td(i.count,'bad-c'));tr.appendChild(td(i.magazines));tr.appendChild(td(i.firstDue));tr.appendChild(td(i.lastAt))}
    else{tr.appendChild(td(i.count,'bad-c'));tr.appendChild(td(i.magazines));tr.appendChild(td(i.firstDue));tr.appendChild(td(i.recordedTotal))}
    const ac=el('td');const mb=el('button',{class:'sm alt',type:'button'},'✏️ பதிவு');mb.onclick=e=>{e.stopPropagation();openMark(i.code,i.category)};ac.appendChild(mb);tr.appendChild(ac);
    let open=false,sub=null;
    tr.onclick=async()=>{ if(open){sub.remove();open=false;return}
      open=true;sub=el('tr',{class:'sub'});const c=el('td');c.colSpan=8;c.textContent='ஏற்றுகிறது…';sub.appendChild(c);tr.after(sub);
      const r=await api('/api/admin/librarian/detail?'+qs({code:i.code}));c.textContent='';
      c.appendChild(buildDetailTable(r.rows));if(r.rows.length>=2000)c.appendChild(el('div',{class:'muted'},'முதல் 2000 வரிசைகள் மட்டும் — முழுமைக்கு Excel பதிவிறக்கவும்.'))};
    tb.appendChild(tr)})
}
function setMode(m){MODE=m;$('t1').classList.toggle('on',m==='received');$('t2').classList.toggle('on',m==='notreceived');$('t3').classList.toggle('on',m==='unregistered');loadSummary()}
$('t1').onclick=()=>setMode('received');$('t2').onclick=()=>setMode('notreceived');$('t3').onclick=()=>setMode('unregistered');
$('fgo').onclick=loadSummary;
$('fweek').onclick=()=>{$('ffrom').value=OV.weekStart;$('fto').value=OV.weekEnd;loadSummary()};
$('fperiod').onclick=()=>{applyRange();loadSummary()};
$('fxl').onclick=()=>{location.href='/api/admin/librarian/export?'+qs({fmt:'xlsx'})};
$('fpdf').onclick=()=>{location.href='/api/admin/librarian/export?'+qs({fmt:'pdf'})};
function hideSum(){const th=$('stab').querySelector('thead'),tb=$('stab').querySelector('tbody');th.innerHTML='';tb.innerHTML='';$('kpi').innerHTML='';
  $('fhide').style.display='none';$('fhidebar').style.display='none';$('fgo').scrollIntoView({behavior:'smooth',block:'center'})}
$('fhide').onclick=hideSum;$('fhide2').onclick=hideSum;

/* ---------- ③ Admin பதிவு ---------- */
async function loadLibs(){
  const d=await api('/api/admin/librarian/libraries');LIBS=d.categories||[];
  const c=$('mcat');c.length=1;LIBS.forEach(x=>c.appendChild(el('option',{value:x.category},x.category+' ('+x.libraries.length+')')));fillLibs();fillFlib();fillPlib()}
function fillLibs(){const l=$('mlib');l.length=1;const c=LIBS.find(x=>x.category===$('mcat').value);if(c)c.libraries.forEach(x=>l.appendChild(el('option',{value:x.code},x.name)))}
$('mcat').onchange=fillLibs;
function mcount(){$('mcnt').textContent='தேர்ந்தவை: '+document.querySelectorAll('#mlist .opts input:checked').length+' / '+MROWS.length}
function renderMark(d){
  const h=$('mhead');h.innerHTML='';h.appendChild(el('span',{style:'font-weight:700;font-size:16px;margin-right:8px'},d.library.name||''));
  h.appendChild(el('span',{class:'chip'},d.library.category||''));h.appendChild(el('span',{class:'chip'},d.library.code||''));
  const box=$('mlist');box.innerHTML='';MROWS=[];
  [['⚠️ கடந்த வாரங்களில் பதிவு செய்யாதவை','unreg',d.earlierUnregistered],['⏳ முந்தைய வாரங்களில் இன்னும் வராதவை','pend',d.earlierNotReceived],['📅 இந்த வாரம் ('+d.weekStart+' – '+d.weekEnd+')','this',d.rows]].forEach(([t,k,rows])=>{
    if(!rows.length)return;box.appendChild(el('div',{class:'mh '+k},t+' ('+rows.length+')'));let last='';
    rows.forEach(r=>{
      if(r.date!==last){last=r.date;box.appendChild(el('div',{class:'md'},r.day+' — '+r.dateLabel+(r.date===d.today?'  (இன்று)':'')))}
      const i=MROWS.push({magazine:r.magazine,date:r.date})-1;
      const row=el('div',{class:'mr'});const nm=el('div',{class:'nm'},r.magazine);if(r.periodicity)nm.appendChild(el('small',null,r.periodicity));row.appendChild(nm);
      const o=el('div',{class:'opts'});
      [['Received','rc','✅ Received'],['Not received','nr','❌ Not received'],['Clear','cl','↺ நீக்கு']].forEach(([v,c,tt])=>{
        const l=el('label');const inp=el('input',{type:'radio',name:'m'+i,value:v,'data-sec':k});if(r.status===v)inp.checked=true;inp.onchange=mcount;
        l.appendChild(inp);l.appendChild(el('span',{class:c},tt));o.appendChild(l)});
      row.appendChild(o);box.appendChild(row)})});
  if(!MROWS.length)box.appendChild(el('div',{class:'muted',style:'margin-top:10px'},'இந்தக் காலத்துக்குப் பதிவு செய்ய இதழ்கள் எதுவும் இல்லை.'));
  $('mbar').style.display=MROWS.length?'flex':'none';$('mclrw').style.display='block';mcount()}
async function loadMark(){
  const code=$('mlib').value;if(!code){say('mmsg',false,'முதலில் நூலகத்தைத் தேர்ந்தெடுக்கவும்.');return}
  MCODE=code;const d=await api('/api/admin/librarian/mark-list?code='+encodeURIComponent(code)+'&weeks='+$('mweeks').value);
  if(!d.success){say('mmsg',false,d.message||'பிழை');return}
  renderMark(d)}
$('mload').onclick=()=>{$('mmsg').className='msg';loadMark()};
function openMark(code,cat){$('mcat').value=cat;fillLibs();$('mlib').value=code;$('mmsg').className='msg';loadMark().then(()=>$('mcard').scrollIntoView({behavior:'smooth'}))}
$('mallrc').onclick=()=>{document.querySelectorAll('#mlist .opts input[value="Received"][data-sec="this"]').forEach(i=>i.checked=true);mcount()};
$('mclr').onclick=()=>{
  $('cftxt').textContent='"'+(($('mlib').selectedOptions[0]||{}).textContent||MCODE)+'" நூலகத்தின் Received / Not received பதிவுகள் அனைத்தும் (எல்லா வாரங்களும்) நீக்கப்படும். இதைத் திரும்பப் பெற முடியாது.';
  $('cfov').style.display='flex'};
$('cfno').onclick=()=>{$('cfov').style.display='none'};
$('cfyes').onclick=async()=>{
  $('cfyes').disabled=true;
  try{const d=await api('/api/admin/librarian/mark-clear-all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:MCODE})});
    $('cfov').style.display='none';say('mmsg',d.success,d.message||'');if(d.success){await loadMark();loadSummary()}}
  catch(e){$('cfov').style.display='none';say('mmsg',false,'நீக்க முடியவில்லை: '+e)}finally{$('cfyes').disabled=false}};
$('msave').onclick=async()=>{
  const entries=[];MROWS.forEach((r,i)=>{const c=document.querySelector('#mlist input[name="m'+i+'"]:checked');if(c)entries.push({magazine:r.magazine,date:r.date,status:c.value})});
  if(!entries.length){say('mmsg',false,'குறைந்தது ஒரு இதழுக்காவது Received / Not received தேர்ந்தெடுக்கவும்.');return}
  $('msave').disabled=true;
  try{const d=await api('/api/admin/librarian/mark-save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:MCODE,entries})});
    say('mmsg',d.success,d.message||'');if(d.success){await loadMark();loadSummary()}}
  catch(e){say('mmsg',false,'சேமிக்க முடியவில்லை: '+e)}finally{$('msave').disabled=false}};

/* ---------- சுருக்கம்: பதிவு செய்யாதவை ---------- */
function pq(extra){const p=new URLSearchParams();if($('pcat').value)p.set('category',$('pcat').value);
  if($('pfrom').value)p.set('from',$('pfrom').value);if($('pto').value)p.set('to',$('pto').value);for(const k in (extra||{}))p.set(k,extra[k]);return p.toString()}
function fillPlib(){const f=$('plib'),keep=f.value,cat=$('pcat').value;f.length=1;
  (LIBS||[]).filter(x=>!cat||x.category===cat).forEach(x=>x.libraries.forEach(l=>f.appendChild(el('option',{value:l.code},l.name))));
  if([...f.options].some(o=>o.value===keep))f.value=keep}
$('pcat').onchange=()=>{$('plib').value='';fillPlib();loadPend()};
$('plib').onchange=()=>loadPend();$('pgo').onclick=()=>loadPend();$('pperiod').onclick=()=>{applyRange();loadPend()};
function hidePend(){
  const th=$('ptab').querySelector('thead'),tb=$('ptab').querySelector('tbody');
  th.innerHTML='';tb.innerHTML='';$('pkpi').innerHTML='';
  $('phide').style.display='none';$('phidebar').style.display='none';
  $('pcard').scrollIntoView({behavior:'smooth',block:'start'});
}
$('phide').onclick=hidePend;$('phide2').onclick=hidePend;
$('pxl').onclick=()=>{location.href='/api/admin/librarian/pending-export?'+pq({fmt:'xlsx',code:$('plib').value,flt:$('pflt').value})};
$('ppdf').onclick=()=>{location.href='/api/admin/librarian/pending-export?'+pq({fmt:'pdf',code:$('plib').value,flt:$('pflt').value})};
$('pflt').onchange=()=>{if(!$('plib').value&&PSUM)loadPend(true)};
async function loadPend(cached){
  $('phide').style.display='';$('phidebar').style.display='';
  const th=$('ptab').querySelector('thead'),tb=$('ptab').querySelector('tbody'),k=$('pkpi');th.innerHTML='';tb.innerHTML='';k.innerHTML='';
  try{
    if($('plib').value){
      const d=await api('/api/admin/librarian/pending-detail?'+pq({code:$('plib').value}));
      if(!d.success){tb.appendChild(el('tr')).appendChild(td(d.message||'பிழை','muted'));return}
      k.appendChild(el('div',null,'📚 '+d.library.name));k.appendChild(el('div',null,'பதிவு செய்யாத இதழ்கள்: '+d.items.length));k.appendChild(el('div',null,'பதிவு செய்யாத தேதிகள் (மொத்தம்): '+d.total+' ('+d.upTo+' வரை)'));
      const hr=el('tr');['வ.எண்.','இதழ் பெயர்','பதிவு செய்யாத காலம்'].forEach(h=>hr.appendChild(el('th',null,h)));th.appendChild(hr);
      if(!d.items.length){const tr=el('tr'),c=td('✅ பதிவு செய்யாத இதழ்கள் இல்லை.','ok-c');c.colSpan=3;tr.appendChild(c);tb.appendChild(tr)}
      d.items.forEach((m,i)=>{const tr=el('tr');tr.appendChild(td(i+1));
        const n=td(m.magazine);if(m.periodicity)n.appendChild(el('div',{class:'muted'},m.periodicity));tr.appendChild(n);
        const c=el('td');c.appendChild(el('div',{class:'bad-c'},(m.first===m.last?m.first:m.first+' – '+m.last)+'  ('+m.count+' இதழ்)'));
        c.appendChild(el('div',{class:'muted'},m.dates.join(', ')));tr.appendChild(c);tb.appendChild(tr)});
      const bt=el('tr'),bc=el('td');bc.colSpan=3;
      const b1=el('button',{class:'sm alt',type:'button'},'← எல்லா நூலகங்களும்');b1.onclick=()=>{$('plib').value='';loadPend()};
      const b2=el('button',{class:'sm',type:'button',style:'margin-left:8px'},'✏️ இந்த நூலகத்துக்குப் பதிவு');
      b2.onclick=()=>openMark(d.library.code,d.library.category);bc.appendChild(b1);bc.appendChild(b2);bt.appendChild(bc);tb.appendChild(bt);
    }else{
      if(!(cached&&PSUM))PSUM=await api('/api/admin/librarian/pending-summary?'+pq());
      const d=filterPend(PSUM);
      const hr=el('tr');['வ.எண்.','நூலகம் பெயர்','அனுமதிக்கப்பட்ட இதழ்கள்','பதிவு செய்தவை','பதிவு செய்யாதவை'].forEach(h=>hr.appendChild(el('th',null,h)));th.appendChild(hr);
      const tot=d.items.reduce((a,i)=>a+i.unregistered,0),clean=d.items.filter(i=>i.unregistered===0).length;
      k.appendChild(el('div',null,'நூலகங்கள்: '+d.items.length));k.appendChild(el('div',null,'பதிவு செய்யாத இதழ்கள் (மொத்தம்): '+tot+' ('+d.upTo+' வரை)'));k.appendChild(el('div',null,'எல்லாம் பதிவு செய்த நூலகங்கள்: '+clean));
      if(!d.items.length){const tr=el('tr'),c=td('நூலகங்கள் இல்லை.','muted');c.colSpan=5;tr.appendChild(c);tb.appendChild(tr)}
      d.items.forEach((i,n)=>{const tr=el('tr',{class:'lib'});tr.appendChild(td(n+1));tr.appendChild(td(i.name));tr.appendChild(td(i.allowed));
        tr.appendChild(td(i.registered,'ok-c'));tr.appendChild(td(i.unregistered,i.unregistered?'bad-c':'ok-c'));
        tr.onclick=()=>{if(!$('pcat').value)$('pcat').value=i.category;fillPlib();$('plib').value=i.code;loadPend()};tb.appendChild(tr)})
    }
  }catch(e){if(e!==0)tb.appendChild(el('tr')).appendChild(td('ஏற்ற முடியவில்லை: '+e,'muted'))}}
function fdot(s){const p=(s||'').split('-');return p.length===3?p[2]+'.'+p[1]+'.'+p[0]:''}
let RANGE={from:'',to:''};
function applyRange(){['ffrom','pfrom'].forEach(i=>$(i).value=RANGE.from||'');['fto','pto'].forEach(i=>$(i).value=RANGE.to||'')}
async function loadRange(apply){
  const d=await api('/api/admin/librarian/range');$('rfrom').value=d.from||'';$('rto').value=d.to||'';
  RANGE={from:d.from||'',to:d.to||''};if(apply)applyRange();
  $('rstat').textContent=(d.from&&d.to)?('தற்போது: நூலகர் திரை '+fdot(d.from)+' முதல் '+fdot(d.to)+' வரை (இன்றைய தேதி வரை மட்டும்) காட்டும்.'):('தற்போது வரம்பு அமைக்கப்படவில்லை — இந்த வாரம் + முந்தைய '+d.carryWeeks+' வாரங்கள் காட்டப்படும்.');
  return d}
async function postRange(body){
  try{const d=await api('/api/admin/librarian/range',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    say('rmsg',d.success,d.message||'');if(d.success){await loadRange(true);loadSummary()}}
  catch(e){say('rmsg',false,'சேமிக்க முடியவில்லை: '+e)}}
$('rsave').onclick=()=>postRange({from:$('rfrom').value,to:$('rto').value});
$('rclr').onclick=()=>postRange({clear:true});
loadRange(true).catch(()=>{}).then(()=>loadOverview()).then(loadSummary);loadLibs();
</script></body></html>"""
