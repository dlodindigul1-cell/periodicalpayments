"""
நூலகர் இதழ் வருகைப் பதிவு (Librarian magazine-receipt register)
================================================================
app.py-ல் `init_librarian(app, get_conn)` என்று ஒரே ஒரு வரியில் இணைக்கப்படும் தனி Blueprint.

பகுதிகள்
  1. நூலகர் (Login dropdown-ல் "Librarian", Password இல்லை)
       /librarian/select      -> வகை + நூலகப் பெயர் தேர்வு
       /librarian/dashboard   -> இந்த வாரத்துக்கான இதழ்கள்: Received / Not received
  2. Admin
       /admin/librarian       -> Quarter Excel ஏற்றுதல், நூலகர் பதிவு (பதிவு செய்தவை / செய்யாதவை)

Tables (தானாக உருவாகும்)
  lib_libraries (code, name, category)         <- data/libraries_seed.csv (காலியாக இருந்தால் ஒருமுறை) அல்லது Admin பக்கம்
  lib_schedule  (quarter, code, magazine, periodicity, expected_date)  <- Admin பக்கத்தில் Q3 / Q4 Excel ஏற்றுதல்
  lib_receipts  (code, magazine, expected_date, status, marked_at)     <- நூலகர் பதிவு
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


@lib_bp.route("/api/librarian/libraries")
@librarian_api(need_library=False)
def api_librarian_libraries():
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


@lib_bp.route("/api/librarian/week")
@librarian_api()
def api_librarian_week():
    code = session["lib_code"]
    ws, we = week_bounds(today_ist())
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, category FROM lib_libraries WHERE code=%s", (code,))
            lib = cur.fetchone()
            cur.execute(
                "SELECT s.magazine, s.periodicity, s.expected_date, r.status "
                "FROM lib_schedule s LEFT JOIN lib_receipts r "
                "  ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                "WHERE s.code=%s AND s.expected_date BETWEEN %s AND %s "
                "ORDER BY s.expected_date, s.magazine", (code, ws, we))
            rows = cur.fetchall()
    finally:
        conn.close()
    return jsonify({
        "success": True, "library": dict(lib) if lib else {},
        "weekStart": fmt_d(ws), "weekEnd": fmt_d(we), "today": today_ist().isoformat(),
        "rows": [{"magazine": r["magazine"], "periodicity": r["periodicity"] or "",
                  "date": r["expected_date"].isoformat(), "dateLabel": fmt_d(r["expected_date"]),
                  "day": TA_DAYS[r["expected_date"].weekday()], "status": r["status"]} for r in rows],
    })


@lib_bp.route("/api/librarian/save", methods=["POST"])
@librarian_api()
def api_librarian_save():
    code = session["lib_code"]
    ws, we = week_bounds(today_ist())
    entries = (request.get_json(force=True, silent=True) or {}).get("entries") or []
    clean = []
    for e in entries:
        d = parse_iso(e.get("date"))
        mag, st = (e.get("magazine") or "").strip(), e.get("status")
        if not mag or st not in STATUSES or not d or not (ws <= d <= we):
            return jsonify({"success": False, "message": "தவறான பதிவு (இந்த வாரத்துக்கு உரியவை மட்டுமே சேமிக்கலாம்)."}), 400
        clean.append((code, mag, d, st))
    if not clean:
        return jsonify({"success": False, "message": "எந்த இதழும் தேர்ந்தெடுக்கப்படவில்லை."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # அட்டவணையில் உள்ள இதழ்களை மட்டுமே ஏற்கும்
            cur.execute("SELECT magazine, expected_date FROM lib_schedule WHERE code=%s AND expected_date BETWEEN %s AND %s",
                        (code, ws, we))
            valid = {(r["magazine"], r["expected_date"]) for r in cur.fetchall()}
            clean = [c for c in clean if (c[1], c[2]) in valid]
            if not clean:
                return jsonify({"success": False, "message": "பதிவு செய்ய இதழ்கள் எதுவும் பொருந்தவில்லை."}), 400
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO lib_receipts (code, magazine, expected_date, status) VALUES %s "
                "ON CONFLICT (code, magazine, expected_date) DO UPDATE "
                "SET status=EXCLUDED.status, marked_at=now() WHERE lib_receipts.status IS DISTINCT FROM EXCLUDED.status",
                clean)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "saved": len(clean), "message": f"✅ {len(clean)} இதழ்களின் பதிவு சேமிக்கப்பட்டது."})


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


# ---- பதிவு செய்தவை / செய்யாதவை ---------------------------------------------------------------- #
def _filters():
    """status, category, from, to, code (+ pending-க்கு 'to' <= இன்று)."""
    status = request.args.get("status", "recorded")
    cat = (request.args.get("category") or "").strip()
    dfrom, dto = parse_iso(request.args.get("from")), parse_iso(request.args.get("to"))
    code = (request.args.get("code") or "").strip()
    where, params = [], []
    if cat:
        where.append("l.category = %s")
        params.append(cat)
    if code:
        where.append("l.code = %s")
        params.append(code)
    if status == "pending":
        cap = today_ist()
        dto = min(dto, cap) if dto else cap
    if dfrom:
        where.append("s.expected_date >= %s" if status == "pending" else "r.expected_date >= %s")
        params.append(dfrom)
    if dto:
        where.append("s.expected_date <= %s" if status == "pending" else "r.expected_date <= %s")
        params.append(dto)
    return status, where, params


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
            if status == "pending":
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
                    "       SUM(CASE WHEN r.status='Received' THEN 1 ELSE 0 END) AS got, "
                    "       SUM(CASE WHEN r.status='Not received' THEN 1 ELSE 0 END) AS notgot, "
                    "       MAX(r.marked_at) AS last_at "
                    "FROM lib_receipts r JOIN lib_libraries l ON l.code=r.code "
                    "WHERE TRUE" + extra + " GROUP BY l.code, l.name, l.category ORDER BY l.name", params)
                items = [{"code": r["code"], "name": r["name"], "category": r["category"], "count": r["cnt"],
                          "magazines": r["mags"], "received": int(r["got"] or 0), "notReceived": int(r["notgot"] or 0),
                          "lastAt": r["last_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["last_at"] else ""}
                         for r in cur.fetchall()]
            cur.execute("SELECT COUNT(DISTINCT code) AS n FROM lib_receipts")
            ever = cur.fetchone()["n"]
    finally:
        conn.close()
    return jsonify({"success": True, "items": items, "totalLibraries": total_libs, "librariesEverRecorded": ever,
                    "totalRows": sum(i["count"] for i in items)})


def _detail_rows(limit=None):
    status, where, params = _filters()
    extra = (" AND " + " AND ".join(where)) if where else ""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if status == "pending":
                cur.execute(
                    "SELECT l.code, l.name, l.category, s.magazine, s.periodicity, s.expected_date, NULL::text AS status, "
                    "       NULL::timestamptz AS marked_at "
                    "FROM lib_schedule s JOIN lib_libraries l ON l.code=s.code "
                    "LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
                    "WHERE r.code IS NULL" + extra + " ORDER BY l.name, s.expected_date, s.magazine"
                    + (f" LIMIT {int(limit)}" if limit else ""), params)
            else:
                cur.execute(
                    "SELECT l.code, l.name, l.category, r.magazine, s.periodicity, r.expected_date, r.status, r.marked_at "
                    "FROM lib_receipts r JOIN lib_libraries l ON l.code=r.code "
                    "LEFT JOIN lib_schedule s ON s.code=r.code AND s.magazine=r.magazine AND s.expected_date=r.expected_date "
                    "WHERE TRUE" + extra + " ORDER BY l.name, r.expected_date, r.magazine"
                    + (f" LIMIT {int(limit)}" if limit else ""), params)
            return status, cur.fetchall()
    finally:
        conn.close()


@lib_bp.route("/api/admin/librarian/detail")
@admin_api
def api_admin_detail():
    status, rows = _detail_rows(limit=2000)
    return jsonify({"success": True, "rows": [
        {"magazine": r["magazine"], "periodicity": r["periodicity"] or "", "date": fmt_d(r["expected_date"]),
         "status": r["status"] or "",
         "markedAt": r["marked_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["marked_at"] else ""} for r in rows]})


@lib_bp.route("/api/admin/librarian/export")
@admin_api
def api_admin_export():
    import xlsxwriter
    status, rows = _detail_rows()
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Recorded" if status != "pending" else "Not recorded")
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1})
    cols = ["Library Code", "Library", "Category", "Magazine", "Periodicity", "Expected Date"]
    if status != "pending":
        cols += ["Status", "Marked At"]
    for c, h in enumerate(cols):
        ws.write(0, c, h, head)
    for i, r in enumerate(rows, start=1):
        vals = [r["code"], r["name"], r["category"], r["magazine"], r["periodicity"] or "", fmt_d(r["expected_date"])]
        if status != "pending":
            vals += [r["status"], r["marked_at"].astimezone(IST).strftime("%d/%m/%Y %H:%M") if r["marked_at"] else ""]
        for c, v in enumerate(vals):
            ws.write(i, c, v)
    for c, w in enumerate([14, 38, 22, 34, 14, 14, 14, 18]):
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)
    wb.close()
    fname = "librarian_" + ("not_recorded" if status == "pending" else "recorded") + f"_{today_ist().isoformat()}.xlsx"
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


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
.day{background:#1e4d8c;color:#fff;border-radius:10px;padding:8px 12px;margin:16px 0 8px;font-weight:700;font-size:14px}
.row{background:#fff;border-radius:12px;padding:12px 14px;margin-bottom:8px;box-shadow:0 2px 8px rgba(15,35,71,.07);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.row .nm{flex:1;min-width:180px;font-weight:600} .row .nm small{display:block;color:#6b7694;font-weight:400;font-size:12px;margin-top:2px}
.opts{display:flex;gap:8px} .opts label{margin:0;display:block}
.opts input{display:none}
.opts span{display:inline-block;padding:9px 14px;border-radius:9px;border:1.5px solid #c5cee3;font-size:14px;font-weight:600;color:#44506f;background:#fff;cursor:pointer;user-select:none}
.opts input:checked + span.rc{background:#1b8a43;border-color:#1b8a43;color:#fff}
.opts input:checked + span.nr{background:#c0392b;border-color:#c0392b;color:#fff}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #d6ddee;padding:10px 14px;display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap;z-index:5}
.bar .info{font-size:14px;color:#1b2540;font-weight:600} .bar .alt{background:#e8eefb;color:#1e4d8c}
.chip{display:inline-block;background:#e8eefb;color:#1e4d8c;border-radius:999px;padding:3px 10px;font-size:12px;margin-right:6px}
</style></head><body>
<div class="top"><h1>📖 இதழ்கள் வருகை பதிவு செய்யுங்கள்</h1><a href="/librarian/change">நூலகத்தை மாற்று</a><a href="/logout">வெளியேறு</a></div>
<div class="wrap">
  <div class="card" id="head"><div class="muted">ஏற்றுகிறது…</div></div>
  <div id="list"></div>
  <div id="msg" class="msg"></div>
</div>
<div class="bar" id="bar" style="display:none">
  <span class="info" id="cnt"></span>
  <button class="alt" id="allRc" type="button">எல்லாவற்றையும் Received எனக் குறி</button>
  <button id="save" type="button">💾 சேமி</button>
</div>
<script>
const list=document.getElementById('list'),msg=document.getElementById('msg'),bar=document.getElementById('bar'),cnt=document.getElementById('cnt');
let ROWS=[];
function el(t,a,x){const e=document.createElement(t);if(a)for(const k in a)e.setAttribute(k,a[k]);if(x!==undefined)e.textContent=x;return e}
function update(){const n=ROWS.length,m=document.querySelectorAll('.opts input:checked').length;cnt.textContent='தேர்ந்தவை: '+m+' / '+n}
function render(d){
  const h=document.getElementById('head');h.innerHTML='';
  h.appendChild(el('div',{style:'font-size:17px;font-weight:700'},d.library.name||''));
  const s=el('div',{style:'margin-top:6px'});s.appendChild(el('span',{class:'chip'},d.library.category||''));s.appendChild(el('span',{class:'chip'},d.library.code||''));
  s.appendChild(el('span',{class:'chip'},'📅 இந்த வாரம்: '+d.weekStart+' – '+d.weekEnd));h.appendChild(s);
  ROWS=d.rows;list.innerHTML='';
  if(!ROWS.length){list.appendChild(el('div',{class:'card muted'},'இந்த வாரத்துக்கு (' +d.weekStart+' – '+d.weekEnd+') வர வேண்டிய இதழ்கள் எதுவும் இல்லை.'));bar.style.display='none';return}
  let last='';
  ROWS.forEach((r,i)=>{
    if(r.date!==last){last=r.date;list.appendChild(el('div',{class:'day'},r.day+' — '+r.dateLabel+(r.date===d.today?'  (இன்று)':'')))}
    const row=el('div',{class:'row'});const nm=el('div',{class:'nm'},r.magazine);if(r.periodicity)nm.appendChild(el('small',null,r.periodicity));row.appendChild(nm);
    const o=el('div',{class:'opts'});
    [['Received','rc','✅ Received'],['Not received','nr','❌ Not received']].forEach(([v,c,t])=>{
      const l=el('label');const inp=el('input',{type:'radio',name:'r'+i,value:v});if(r.status===v)inp.checked=true;inp.onchange=update;
      l.appendChild(inp);l.appendChild(el('span',{class:c},t));o.appendChild(l)});
    row.appendChild(o);list.appendChild(row)});
  bar.style.display='flex';update();
}
async function load(){
  const r=await fetch('/api/librarian/week');const d=await r.json();
  if(d.needLibrary){location.href='/librarian/select';return}
  if(!d.success){msg.className='msg err';msg.textContent=d.message||'பிழை';return}
  render(d)}
document.getElementById('allRc').onclick=()=>{document.querySelectorAll('.opts input[value="Received"]').forEach(i=>i.checked=true);update()};
document.getElementById('save').onclick=async()=>{
  const entries=[];ROWS.forEach((r,i)=>{const c=document.querySelector('input[name="r'+i+'"]:checked');if(c)entries.push({magazine:r.magazine,date:r.date,status:c.value})});
  if(!entries.length){msg.className='msg err';msg.textContent='குறைந்தது ஒரு இதழுக்காவது Received / Not received தேர்ந்தெடுக்கவும்.';return}
  const b=document.getElementById('save');b.disabled=true;
  try{const r=await fetch('/api/librarian/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})});
    const d=await r.json();msg.className='msg '+(d.success?'ok':'err');msg.textContent=d.message||'';if(d.success)await load();window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})}
  catch(e){msg.className='msg err';msg.textContent='சேமிக்க முடியவில்லை: '+e}finally{b.disabled=false}};
load();
</script></body></html>"""

ADMIN_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>நூலகர் பதிவு — Admin</title>
<style>""" + BASE_CSS + """
.wrap{max-width:1100px} h2{margin:0 0 8px;font-size:17px;color:#0f2347}
.tabs{display:flex;gap:8px;margin-bottom:12px} .tab{background:#e8eefb;color:#1e4d8c;flex:1} .tab.on{background:#1e4d8c;color:#fff}
.flt{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;align-items:end}
table{width:100%;border-collapse:collapse;font-size:14px} th{background:#1e4d8c;color:#fff;text-align:left;padding:8px 10px;position:sticky;top:0}
td{padding:8px 10px;border-bottom:1px solid #e3e8f4;vertical-align:top} tr.lib{cursor:pointer} tr.lib:hover{background:#f3f6fd}
.sub{background:#f8faff} .sub table th{background:#5b7fb8}
.ok-c{color:#1b6b34;font-weight:700} .bad-c{color:#c0392b;font-weight:700}
.kpi{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0} .kpi div{background:#e8eefb;border-radius:10px;padding:8px 14px;font-size:14px}
.sm{padding:6px 12px;font-size:13px} .danger{background:#c0392b} .alt{background:#e8eefb;color:#1e4d8c}
.scroll{overflow-x:auto} details summary{cursor:pointer;font-weight:700;color:#1e4d8c}
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
  <div class="tabs"><button class="tab on" id="t1">✅ பதிவு செய்தவை</button><button class="tab" id="t2">⏳ பதிவு செய்யாதவை</button></div>
  <div class="flt">
    <div><label>நூலக வகை</label><select id="fcat"><option value="">எல்லாம்</option></select></div>
    <div><label>தேதி முதல்</label><input type="date" id="ffrom"></div>
    <div><label>தேதி வரை</label><input type="date" id="fto"></div>
    <div style="display:flex;gap:8px"><button id="fgo">🔍 காட்டு</button><button id="fweek" class="alt">இந்த வாரம்</button></div>
  </div>
  <div class="kpi" id="kpi"></div>
  <div style="margin:6px 0 10px"><button id="fxl" class="sm alt">📥 Excel பதிவிறக்கு</button> <span class="muted">வரிசையைக் கிளிக் செய்தால் இதழ்கள் விரியும்</span></div>
  <div class="scroll"><table id="stab"><thead></thead><tbody></tbody></table></div>
</div>
</div>
<script>
let MODE='recorded',OV=null;
const $=id=>document.getElementById(id);
function el(t,a,x){const e=document.createElement(t);if(a)for(const k in a)e.setAttribute(k,a[k]);if(x!==undefined)e.textContent=x;return e}
function td(t,cls){const c=el('td',cls?{class:cls}:null,t===undefined?'':String(t));return c}
function say(id,ok,t,warn){const m=$(id);m.className='msg '+(ok?'ok':'err');m.textContent=t;(warn||[]).forEach(w=>{m.appendChild(el('div',{style:'margin-top:4px'},'⚠️ '+w))})}
async function api(u,o){const r=await fetch(u,o);if(r.status===401){location.href='/login';throw 0}return r.json()}

async function loadOverview(){
  OV=await api('/api/admin/librarian/overview');
  $('libcnt').textContent=OV.libraries;
  const fc=$('fcat');fc.length=1;OV.categories.forEach(c=>{const o=el('option',{value:c},c);fc.appendChild(o)});
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
  say('lmsg',d.success,d.message);if(d.success)loadOverview()};

function qs(extra){const p=new URLSearchParams({status:MODE});if($('fcat').value)p.set('category',$('fcat').value);
  if($('ffrom').value)p.set('from',$('ffrom').value);if($('fto').value)p.set('to',$('fto').value);for(const k in (extra||{}))p.set(k,extra[k]);return p.toString()}
async function loadSummary(){
  const d=await api('/api/admin/librarian/summary?'+qs());
  const th=$('stab').querySelector('thead'),tb=$('stab').querySelector('tbody');th.innerHTML='';tb.innerHTML='';
  const hr=el('tr');(MODE==='recorded'?['நூலகம்','வகை','பதிவுகள்','Received','Not received','இதழ்கள்','கடைசிப் பதிவு']:['நூலகம்','வகை','பதிவு செய்யாதவை','இதழ்கள்','முதல் தேதி','இதுவரை பதிவு செய்தவை']).forEach(h=>hr.appendChild(el('th',null,h)));th.appendChild(hr);
  const k=$('kpi');k.innerHTML='';
  if(MODE==='recorded'){k.appendChild(el('div',null,'பதிவு செய்த நூலகங்கள்: '+d.items.length+' / '+d.totalLibraries));k.appendChild(el('div',null,'மொத்தப் பதிவுகள்: '+d.totalRows))}
  else{k.appendChild(el('div',null,'பதிவு செய்யாத பதிவுகள் உள்ள நூலகங்கள்: '+d.items.length+' / '+d.totalLibraries));k.appendChild(el('div',null,'பதிவு செய்யப்படாத வரிசைகள் (இன்று வரை): '+d.totalRows));
    const never=d.items.filter(i=>i.recordedTotal===0).length;k.appendChild(el('div',null,'இதுவரை ஒன்றுமே பதிவு செய்யாத நூலகங்கள்: '+never))}
  if(!d.items.length){const tr=el('tr');const c=td('பதிவுகள் இல்லை.','muted');c.colSpan=7;tr.appendChild(c);tb.appendChild(tr)}
  d.items.forEach(i=>{
    const tr=el('tr',{class:'lib'});tr.appendChild(td(i.name));tr.appendChild(td(i.category));
    if(MODE==='recorded'){tr.appendChild(td(i.count));tr.appendChild(td(i.received,'ok-c'));tr.appendChild(td(i.notReceived,i.notReceived?'bad-c':''));tr.appendChild(td(i.magazines));tr.appendChild(td(i.lastAt))}
    else{tr.appendChild(td(i.count,'bad-c'));tr.appendChild(td(i.magazines));tr.appendChild(td(i.firstDue));tr.appendChild(td(i.recordedTotal))}
    let open=false,sub=null;
    tr.onclick=async()=>{ if(open){sub.remove();open=false;return}
      open=true;sub=el('tr',{class:'sub'});const c=el('td');c.colSpan=7;c.textContent='ஏற்றுகிறது…';sub.appendChild(c);tr.after(sub);
      const r=await api('/api/admin/librarian/detail?'+qs({code:i.code}));c.textContent='';
      const t=el('table');const h=el('tr');(MODE==='recorded'?['தேதி','இதழ்','Periodicity','நிலை','பதிவு நேரம்']:['தேதி','இதழ்','Periodicity']).forEach(x=>h.appendChild(el('th',null,x)));t.appendChild(h);
      r.rows.forEach(w=>{const rr=el('tr');rr.appendChild(td(w.date));rr.appendChild(td(w.magazine));rr.appendChild(td(w.periodicity));
        if(MODE==='recorded'){rr.appendChild(td(w.status,w.status==='Received'?'ok-c':'bad-c'));rr.appendChild(td(w.markedAt))}t.appendChild(rr)});
      c.appendChild(t);if(r.rows.length>=2000)c.appendChild(el('div',{class:'muted'},'முதல் 2000 வரிசைகள் மட்டும் — முழுமைக்கு Excel பதிவிறக்கவும்.'))};
    tb.appendChild(tr)})
}
function setMode(m){MODE=m;$('t1').classList.toggle('on',m==='recorded');$('t2').classList.toggle('on',m==='pending');loadSummary()}
$('t1').onclick=()=>setMode('recorded');$('t2').onclick=()=>setMode('pending');
$('fgo').onclick=loadSummary;
$('fweek').onclick=()=>{$('ffrom').value=OV.weekStart;$('fto').value=OV.weekEnd;loadSummary()};
$('fxl').onclick=()=>{location.href='/api/admin/librarian/export?'+qs()};
loadOverview().then(loadSummary);
</script></body></html>"""
