"""
வராத இதழ்கள் விவரம் (Admin → /admin/missing)
=============================================
app.py-ல் `init_missing(app, get_conn, classify_lang, parse_email_list)` என ஒரே வரியில் இணையும் தனி Blueprint.

தாவல்கள்
  ① இதழ் வாரியாக வராத இதழ்கள் — தேதி முதல் / வரை + இதழ் தேர்வு. அதே Vendor Code உள்ள (Group) இதழ்களும் ஒன்றின் கீழ் ஒன்றாக.
       வ.எண்., நூலகக் குறியீடு, நூலகம், நூலகர் பெயர், இதழ் வராத தேதி, தொலைபேசி, முகவரி.
       தமிழ் இதழ் -> தமிழ்த் தலைப்புகள்; ஆங்கில இதழ் -> English தலைப்புகள் (English பெயர்கள் இருந்தால் அவை).
  ② Email இல்லாத இதழ்கள் — magazines.email_id காலியானவை; இங்கே சேமித்தால் Vendor Bank Master-லும் (அதே புலம்) மாறும்.
  (நூலகர் பெயர், தொலைபேசி, முகவரி — தனிப் பக்கம் /admin/library-details, Admin முகப்பு "12")

"வராத" = (அ) நூலகர் / Admin "Not received" எனக் குறித்தவை + (ஆ) Expected Date கடந்தும் பதிவே இல்லாதவை.
இதழ் பெயர் பொருத்தம்: Master பெயர் + Master → "இதழ் பெயர் மாற்றம்" இணைப்புகள் (magazine_aliases) — எழுத்து / இடைவெளி வேறுபாடு பாராது.
"""

import io
import re
import unicodedata
from datetime import timedelta
from functools import wraps

from flask import Blueprint, Response, jsonify, render_template_string, request

import librarian_module as lm

miss_bp = Blueprint("missing", __name__)
_get_conn = None
_classify = None
_parse_email = None
_cols_ready = False

LIB_COLS = ("name_en", "librarian_name", "librarian_name_en", "phone", "address", "address_en")

LABELS = {
    "ta": {"sno": "வ.எண்.", "code": "நூலகக் குறியீடு", "lib": "நூலகம்", "librarian": "நூலகர் பெயர்",
           "date": "இதழ் வராத தேதி", "phone": "தொலைபேசி எண்", "addr": "முகவரி", "status": "நிலை",
           "mag": "இதழ்", "vendor": "Vendor குறியீடு", "none": "வராத இதழ்கள் இல்லை",
           "s_nr": "நூலகர் 'வரவில்லை' எனக் குறித்தது", "s_un": "பதிவு செய்யப்படவில்லை"},
    "en": {"sno": "S.No.", "code": "Library Code", "lib": "Library", "librarian": "Librarian",
           "date": "Issue Date Not Received", "phone": "Phone", "addr": "Address", "status": "Status",
           "mag": "Magazine", "vendor": "Vendor Code", "none": "No missing issues",
           "s_nr": "Marked 'Not received'", "s_un": "Not yet registered"},
}


def init_missing(app, get_conn, classify_lang, parse_email_list):
    global _get_conn, _classify, _parse_email
    _get_conn, _classify, _parse_email = get_conn, classify_lang, parse_email_list
    app.register_blueprint(miss_bp)


# --------------------------------------------------------------------------- #
# உதவிகள்
# --------------------------------------------------------------------------- #
def _ensure_cols():
    global _cols_ready
    if _cols_ready:
        return
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7712002)")
            for c in LIB_COLS:
                cur.execute("ALTER TABLE lib_libraries ADD COLUMN IF NOT EXISTS %s TEXT NOT NULL DEFAULT ''" % c)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS magazine_aliases ("
                " alias_name TEXT PRIMARY KEY, master_name TEXT NOT NULL, from_quarter TEXT NOT NULL,"
                " created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        conn.commit()
    finally:
        conn.close()
    _cols_ready = True


def _api(f):
    @wraps(f)
    @lm.admin_api
    def w(*a, **k):
        _ensure_cols()
        return f(*a, **k)
    return w


def _key(v):
    v = unicodedata.normalize("NFC", v or "")
    for ch in "\u200b\u200c\u200d\ufeff":
        v = v.replace(ch, "")
    return " ".join(v.replace("\xa0", " ").split()).casefold()


def _code_key(v):
    return re.sub(r"[\s\-_./]+", "", v or "").upper()


def _load_master(cur):
    """-> (magazines list[dict], resolver {name_key: master_name}, aliases {master: [alias,...]})"""
    cur.execute("SELECT name, language, tnpfts_code, vendor_name, email_id FROM magazines ORDER BY name")
    mags = []
    for r in cur.fetchall():
        mags.append({"name": r["name"], "lang": _classify(r["language"], r["name"]),
                     "code": (r["tnpfts_code"] or "").strip(), "vendorName": r["vendor_name"] or "",
                     "email": (r["email_id"] or "").strip()})
    resolver = {_key(m["name"]): m["name"] for m in mags}
    cur.execute("SELECT alias_name, master_name FROM magazine_aliases")
    aliases = {}
    for a in cur.fetchall():
        if a["master_name"] in {m["name"] for m in mags}:
            resolver.setdefault(_key(a["alias_name"]), a["master_name"])
            aliases.setdefault(a["master_name"], []).append(a["alias_name"])
    return mags, resolver, aliases


def _build_report(cur, magazine, dfrom, dto):
    """தேர்ந்த இதழ் + அதே Vendor Code உள்ள Group இதழ்கள் -> groups list"""
    mags, resolver, aliases = _load_master(cur)
    by_name = {m["name"]: m for m in mags}
    sel = by_name.get(magazine) or by_name.get(resolver.get(_key(magazine), ""))
    if not sel:
        return None
    members = [sel]
    ck = _code_key(sel["code"])
    if ck:
        members += [m for m in mags if m["name"] != sel["name"] and _code_key(m["code"]) == ck]
    wanted = {m["name"] for m in members}

    cur.execute(
        "SELECT l.code AS lcode, l.name AS lname, l.name_en, l.librarian_name, l.librarian_name_en, l.phone, "
        "       l.address, l.address_en, s.magazine, s.expected_date, r.status "
        "FROM lib_schedule s JOIN lib_libraries l ON l.code = s.code "
        "LEFT JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
        "WHERE s.expected_date BETWEEN %s AND %s AND (r.code IS NULL OR r.status = 'Not received') "
        "ORDER BY l.code, s.expected_date", (dfrom, dto))
    rows_by = {n: [] for n in wanted}
    for r in cur.fetchall():
        mname = resolver.get(_key(r["magazine"]))
        if mname in wanted:
            rows_by[mname].append(r)

    groups = []
    for m in members:
        lab = LABELS[m["lang"]]
        en = m["lang"] == "en"
        out = []
        for i, r in enumerate(rows_by[m["name"]], start=1):
            out.append({
                "sno": i, "code": r["lcode"],
                "library": (r["name_en"] or r["lname"]) if en else r["lname"],
                "librarian": ((r["librarian_name_en"] or r["librarian_name"]) if en else r["librarian_name"]) or "",
                "date": lm.fmt_d(r["expected_date"]),
                "phone": r["phone"] or "",
                "address": ((r["address_en"] or r["address"]) if en else r["address"]) or "",
                "status": lab["s_nr"] if r["status"] == "Not received" else lab["s_un"],
            })
        groups.append({"magazine": m["name"], "lang": m["lang"], "vendorCode": m["code"],
                       "aliases": aliases.get(m["name"], []), "labels": lab, "isSelected": m["name"] == sel["name"],
                       "rows": out})
    return groups


def _range_args():
    dto = lm.parse_iso(request.args.get("to")) or lm.today_ist()
    dfrom = lm.parse_iso(request.args.get("from")) or (dto - timedelta(days=30))
    dto = min(dto, lm.today_ist())          # எதிர்காலத் தேதிகள் "வராதவை" அல்ல
    return dfrom, dto


# --------------------------------------------------------------------------- #
# பக்கம் + API
# --------------------------------------------------------------------------- #
@miss_bp.route("/admin/missing")
@lm.admin_page
def admin_missing_page():
    _ensure_cols()
    return render_template_string(MISSING_HTML)


@miss_bp.route("/api/admin/missing/magazines")
@_api
def api_missing_magazines():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            mags, _res, _al = _load_master(cur)
    finally:
        conn.close()
    return jsonify({"success": True, "magazines": [
        {"name": m["name"], "lang": m["lang"], "code": m["code"]} for m in mags]})


@miss_bp.route("/api/admin/missing/report")
@_api
def api_missing_report():
    magazine = (request.args.get("magazine") or "").strip()
    dfrom, dto = _range_args()
    if dfrom > dto:
        return jsonify({"success": False, "message": "'தேதி முதல்' என்பது 'தேதி வரை'-க்கு முந்தையதாக இருக்க வேண்டும்."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            groups = _build_report(cur, magazine, dfrom, dto)
    finally:
        conn.close()
    if groups is None:
        return jsonify({"success": False, "message": "இதழ் Master-ல் கிடைக்கவில்லை."}), 404
    return jsonify({"success": True, "from": lm.fmt_d(dfrom), "to": lm.fmt_d(dto), "groups": groups})


@miss_bp.route("/api/admin/missing/export")
@_api
def api_missing_export():
    import xlsxwriter
    magazine = (request.args.get("magazine") or "").strip()
    dfrom, dto = _range_args()
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            groups = _build_report(cur, magazine, dfrom, dto)
    finally:
        conn.close()
    if groups is None:
        return jsonify({"success": False, "message": "இதழ் Master-ல் கிடைக்கவில்லை."}), 404
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Missing issues")
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1, "text_wrap": True})
    title = wb.add_format({"bold": True, "font_size": 13, "bg_color": "#e8eefb"})
    cell = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    r = 0
    for g in groups:
        lab = g["labels"]
        ws.merge_range(r, 0, r, 7, "%s: %s   (%s: %s)   %s – %s" % (
            lab["mag"], g["magazine"], lab["vendor"], g["vendorCode"] or "-", lm.fmt_d(dfrom), lm.fmt_d(dto)), title)
        r += 1
        for c, k in enumerate(["sno", "code", "lib", "librarian", "date", "phone", "addr", "status"]):
            ws.write(r, c, lab[k], head)
        r += 1
        if not g["rows"]:
            ws.merge_range(r, 0, r, 7, lab["none"], cell)
            r += 1
        for x in g["rows"]:
            for c, v in enumerate([x["sno"], x["code"], x["library"], x["librarian"], x["date"], x["phone"],
                                   x["address"], x["status"]]):
                ws.write(r, c, v, cell)
            r += 1
        r += 1
    for c, w in enumerate([7, 15, 36, 24, 16, 16, 40, 28]):
        ws.set_column(c, c, w)
    wb.close()
    fname = "missing_issues_%s_%s.xlsx" % (dfrom.isoformat(), dto.isoformat())
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=" + fname})


# ---- Email இல்லாத இதழ்கள் -------------------------------------------------------------------------- #
@miss_bp.route("/api/admin/missing/no-email")
@_api
def api_missing_no_email():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            mags, _r, _a = _load_master(cur)
    finally:
        conn.close()
    items = [{"name": m["name"], "vendorName": m["vendorName"], "code": m["code"], "lang": m["lang"]}
             for m in mags if not m["email"]]
    return jsonify({"success": True, "items": items, "total": len(mags)})


@miss_bp.route("/api/admin/missing/save-email", methods=["POST"])
@_api
def api_missing_save_email():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip()
    if not name or not email:
        return jsonify({"success": False, "message": "இதழ் பெயர் மற்றும் Email அவசியம்."}), 400
    ok_list, bad_list = _parse_email(email)
    if bad_list or not ok_list:
        return jsonify({"success": False, "message": "தவறான Email முகவரி: " + ", ".join(bad_list or [email])}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Vendor Bank Master பயன்படுத்தும் அதே புலம் (magazines.email_id)
            cur.execute("UPDATE magazines SET email_id=%s WHERE name=%s", (email, name))
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"success": False, "message": "இதழ் கிடைக்கவில்லை."}), 404
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True, "message": "'%s' Email சேமிக்கப்பட்டது (Vendor Bank Master-லும் மாறியது)." % name})


# ---- நூலக விவரங்கள் (நூலகர் பெயர், தொலைபேசி, முகவரி) ------------------------------------------------- #
@miss_bp.route("/api/admin/missing/libraries")
@_api
def api_missing_libraries():
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, category, name_en, librarian_name, librarian_name_en, phone, "
                        "address, address_en FROM lib_libraries ORDER BY code")
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return jsonify({"success": True, "items": rows})


@miss_bp.route("/api/admin/missing/library-save", methods=["POST"])
@_api
def api_missing_library_save():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    vals = [(body.get(c) or "").strip() for c in LIB_COLS]
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE lib_libraries SET " + ", ".join("%s=%%s" % c for c in LIB_COLS) +
                        ", updated_at=now() WHERE code=%s", vals + [code])
            if cur.rowcount == 0:
                conn.rollback()
                return jsonify({"success": False, "message": "நூலகம் கண்டறியப்படவில்லை."}), 404
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"success": True})


# ---- நூலகங்கள் / நூலகர் முகவரி & தொலைபேசி (Admin முகப்பு "12") ------------------------------------- #
# Excel நெடுவரிசைகள்: A code, B நூலகம் (பார்க்க மட்டும்), C நூலகர் பெயர், D தொலைபேசி, E முகவரி,
#                     F Library (English), G Librarian (English), H Address (English)
XL_COLS = ("librarian_name", "phone", "address", "name_en", "librarian_name_en", "address_en")


@miss_bp.route("/admin/library-details")
@lm.admin_page
def admin_library_details_page():
    _ensure_cols()
    return render_template_string(LIBDETAIL_HTML)


@miss_bp.route("/api/admin/missing/libraries-template")
@_api
def api_missing_libraries_template():
    import xlsxwriter
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, librarian_name, phone, address, name_en, librarian_name_en, address_en "
                        "FROM lib_libraries ORDER BY code")
            rows = cur.fetchall()
    finally:
        conn.close()
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Libraries")
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1, "text_wrap": True})
    lock = wb.add_format({"bg_color": "#eef1f8", "border": 1, "num_format": "@"})
    txt = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "num_format": "@"})
    heads = ["Library Code (மாற்ற வேண்டாம்)", "நூலகம் (பார்க்க மட்டும்)", "நூலகர் பெயர்", "தொலைபேசி எண்", "முகவரி",
             "Library (English)", "Librarian (English)", "Address (English)"]
    for c, h in enumerate(heads):
        ws.write(0, c, h, head)
    for i, r in enumerate(rows, start=1):
        vals = [r["code"], r["name"], r["librarian_name"], r["phone"], r["address"], r["name_en"],
                r["librarian_name_en"], r["address_en"]]
        for c, v in enumerate(vals):
            ws.write_string(i, c, v or "", lock if c < 2 else txt)
    for c, w in enumerate([18, 36, 24, 18, 42, 30, 24, 42]):
        ws.set_column(c, c, w)
    ws.set_row(0, 32)
    ws.freeze_panes(1, 2)
    wb.close()
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=library_details_template.xlsx"})


@miss_bp.route("/api/admin/missing/libraries-upload", methods=["POST"])
@_api
def api_missing_libraries_upload():
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return jsonify({"success": False, "message": "கோப்பைத் தேர்ந்தெடுக்கவும்."}), 400
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code FROM lib_libraries")
            known = {r["code"] for r in cur.fetchall()}
        updated, empty, unknown = 0, 0, []
        try:
            with conn.cursor() as cur:
                for i, row in enumerate(lm._iter_sheet_rows(fs)):
                    if not row:
                        continue
                    code = lm._txt(row[0])
                    if not code:
                        continue
                    if code not in known:
                        if i > 0:                      # முதல் வரிசை தலைப்பாக இருக்கலாம்
                            unknown.append(code)
                        continue
                    vals = [lm._txt(row[k]) if len(row) > k else "" for k in range(2, 8)]
                    if not any(vals):
                        empty += 1
                        continue
                    # காலிக் கட்டம் ஏற்கனவே உள்ள மதிப்பை அழிக்காது
                    cur.execute(
                        "UPDATE lib_libraries SET " +
                        ", ".join("%s=COALESCE(NULLIF(%%s,''), %s)" % (c, c) for c in XL_COLS) +
                        ", updated_at=now() WHERE code=%s", vals + [code])
                    updated += 1
        except ValueError as e:
            conn.rollback()
            return jsonify({"success": False, "message": str(e)}), 400
        except ImportError:
            conn.rollback()
            return jsonify({"success": False, "message": ".xls படிக்க xlrd தேவை; .xlsx ஆகச் சேமித்து ஏற்றவும்."}), 500
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return jsonify({"success": False, "message": "பிழை: " + str(e)}), 500
    finally:
        conn.close()
    warn = []
    if unknown:
        warn.append("நூலகப் பட்டியலில் இல்லாத code (%d): %s%s" % (
            len(unknown), ", ".join(unknown[:10]), "…" if len(unknown) > 10 else ""))
    return jsonify({"success": True, "updated": updated, "emptyRows": empty, "warnings": warn,
                    "message": "✅ %d நூலகங்களின் விவரம் புதுப்பிக்கப்பட்டது; %d வரிசைகளில் புதிய தகவல் இல்லை." % (updated, empty)})


MISSING_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>வராத இதழ்கள் விவரம்</title>
<style>""" + lm.BASE_CSS + """
.wrap{max-width:1150px} h2{margin:0 0 8px;font-size:17px;color:#0f2347}
.tabs{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap} .tab{background:#e8eefb;color:#1e4d8c;flex:1;min-width:170px} .tab.on{background:#1e4d8c;color:#fff}
.flt{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;align-items:end}
table{width:100%;border-collapse:collapse;font-size:14px} th{background:#1e4d8c;color:#fff;text-align:left;padding:8px 10px}
td{padding:7px 10px;border-bottom:1px solid #e3e8f4;vertical-align:top} .scroll{overflow-x:auto}
.gh{background:#e8eefb;border-radius:10px;padding:9px 12px;margin:16px 0 6px;font-weight:800;color:#0f2347}
.gh small{font-weight:500;color:#44506f} .sm{padding:6px 12px;font-size:13px}
.ok-c{color:#1b6b34;font-weight:700} .bad-c{color:#c0392b;font-weight:700}
td input{padding:5px 7px;font-size:13px;width:100%;min-width:110px}
@media print{.top,.tabs,.noprint{display:none!important} .card{box-shadow:none;border:0}}
</style></head><body>
<div class="top"><h1>📭 வராத இதழ்கள் விவரம்</h1><a href="/">← முகப்பு</a></div>
<div class="wrap">
<div class="tabs noprint">
  <button class="tab on" id="t1" onclick="tab(1)">① இதழ் வாரியாக வராத இதழ்கள்</button>
  <button class="tab" id="t2" onclick="tab(2)">② Email இல்லாத இதழ்கள்</button>
</div>

<div class="card" id="p1">
  <h2>① இதழ் வாரியாக வராத இதழ்கள்</h2>
  <p class="muted" style="margin:0 0 8px">நூலகர் "Not received" எனக் குறித்தவை + Expected Date கடந்தும் பதிவு செய்யப்படாதவை. அதே Vendor Code உள்ள (Group) இதழ்களும் ஒன்றின் கீழ் ஒன்றாக வரும்.</p>
  <div class="flt noprint">
    <div><label>தேதி முதல்</label><input type="date" id="dfrom"></div>
    <div><label>தேதி வரை</label><input type="date" id="dto"></div>
    <div><label>மொழி</label><select id="mlang" onchange="fillMags()"><option value="">அனைத்தும்</option><option value="ta">தமிழ்</option><option value="en">English</option></select></div>
    <div><label>இதழ்</label><select id="mag"><option value="">-- தேர்ந்தெடு --</option></select></div>
  </div>
  <div class="noprint" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
    <button onclick="loadReport()">📋 காட்டு</button>
    <button class="alt sm" onclick="exportXlsx()">⬇️ Excel</button>
    <button class="alt sm" onclick="window.print()">🖨️ Print</button>
  </div>
  <div id="rmsg" class="msg"></div>
  <div id="rout"></div>
</div>

<div class="card" id="p2" style="display:none">
  <h2>② Email முகவரி இல்லாத இதழ்கள்</h2>
  <p class="muted" style="margin:0 0 8px">இங்கே சேமிக்கும் Email, Vendor Bank Master-லும் (அதே புலம்) உடனே மாறும். பல முகவரிகளைக் காற்புள்ளியால் பிரிக்கலாம்.</p>
  <div id="emsg" class="msg"></div>
  <div class="scroll"><table><thead><tr><th>#</th><th>இதழ்</th><th>Vendor பெயர்</th><th>Vendor Code</th><th>Email</th><th></th></tr></thead><tbody id="etb"></tbody></table></div>
</div>

</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {};
  const r = await fetch(url, opt);
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  return r.json();
}
function msg(id, ok, t) { const e = $(id); e.className = 'msg ' + (ok ? 'ok' : 'err'); e.innerHTML = t; e.style.display = t ? 'block' : 'none'; }
function tab(n) {
  [1,2].forEach(i => { $('p'+i).style.display = i === n ? '' : 'none'; $('t'+i).classList.toggle('on', i === n); });
  if (n === 2) loadNoEmail();
}
let MAGS = [];
function fillMags() {
  const lg = $('mlang').value, cur = $('mag').value;
  $('mag').innerHTML = '<option value="">-- தேர்ந்தெடு --</option>' +
    MAGS.filter(m => !lg || m.lang === lg).map(m => '<option value="' + esc(m.name) + '">' + esc(m.name) + '</option>').join('');
  if (cur) $('mag').value = cur;
}
function iso(d) { return d.toISOString().slice(0, 10); }
async function init() {
  const t = new Date(), f = new Date(t.getTime() - 30 * 86400000);
  $('dto').value = iso(t); $('dfrom').value = iso(f);
  const d = await api('/api/admin/missing/magazines');
  MAGS = d.magazines || []; fillMags();
}
function qs() { return 'magazine=' + encodeURIComponent($('mag').value) + '&from=' + $('dfrom').value + '&to=' + $('dto').value; }
async function loadReport() {
  if (!$('mag').value) { msg('rmsg', false, 'இதழைத் தேர்ந்தெடுக்கவும்.'); return; }
  msg('rmsg', true, 'ஏற்றப்படுகிறது…');
  const d = await api('/api/admin/missing/report?' + qs());
  if (!d.success) { msg('rmsg', false, esc(d.message)); $('rout').innerHTML = ''; return; }
  const total = d.groups.reduce((a, g) => a + g.rows.length, 0);
  msg('rmsg', true, d.from + ' முதல் ' + d.to + ' வரை — மொத்தம் ' + total + ' வராத இதழ் பதிவுகள் (' + d.groups.length + ' இதழ்).');
  $('rout').innerHTML = d.groups.map(g => {
    const L = g.labels;
    const al = g.aliases.length ? ' · ' + esc(g.aliases.join(', ')) : '';
    return '<div class="gh">' + esc(L.mag) + ': ' + esc(g.magazine) + al + ' <small>(' + esc(L.vendor) + ': ' + esc(g.vendorCode || '-') + ' · ' + g.rows.length + ')</small></div>' +
      '<div class="scroll"><table><thead><tr><th>' + [L.sno, L.code, L.lib, L.librarian, L.date, L.phone, L.addr, L.status].map(esc).join('</th><th>') + '</th></tr></thead><tbody>' +
      (g.rows.map(x => '<tr><td>' + x.sno + '</td><td>' + esc(x.code) + '</td><td>' + esc(x.library) + '</td><td>' + esc(x.librarian) + '</td><td>' + esc(x.date) +
        '</td><td>' + esc(x.phone) + '</td><td>' + esc(x.address) + '</td><td>' + esc(x.status) + '</td></tr>').join('') ||
        '<tr><td colspan="8" class="ok-c">' + esc(L.none) + '</td></tr>') + '</tbody></table></div>';
  }).join('');
}
function exportXlsx() {
  if (!$('mag').value) { msg('rmsg', false, 'இதழைத் தேர்ந்தெடுக்கவும்.'); return; }
  location.href = '/api/admin/missing/export?' + qs();
}
async function loadNoEmail() {
  const d = await api('/api/admin/missing/no-email');
  if (!d.success) { msg('emsg', false, esc(d.message)); return; }
  msg('emsg', d.items.length === 0, d.items.length ? ('Email இல்லாத இதழ்கள்: ' + d.items.length + ' / ' + d.total) : '✅ எல்லா இதழ்களுக்கும் Email உள்ளது.');
  $('etb').innerHTML = d.items.map((m, i) => '<tr><td>' + (i + 1) + '</td><td>' + esc(m.name) + '</td><td>' + esc(m.vendorName) + '</td><td>' + esc(m.code) +
    '</td><td><input id="em' + i + '" type="text" placeholder="name@example.com"></td><td><button class="sm" onclick="saveEmail(' + i + ',\\'' + encodeURIComponent(m.name) + '\\')">சேமி</button></td></tr>').join('');
}
async function saveEmail(i, enc) {
  const email = $('em' + i).value.trim();
  const d = await api('/api/admin/missing/save-email', {name: decodeURIComponent(enc), email});
  if (!d.success) { msg('emsg', false, esc(d.message)); return; }
  await loadNoEmail(); msg('emsg', true, '✅ ' + esc(d.message));
}
init();
</script></body></html>"""


LIBDETAIL_HTML = """<!DOCTYPE html><html lang="ta"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>நூலகங்கள் / நூலகர் முகவரி &amp; தொலைபேசி</title>
<style>""" + lm.BASE_CSS + """
.wrap{max-width:1250px} h2{margin:0 0 8px;font-size:17px;color:#0f2347}
table{width:100%;border-collapse:collapse;font-size:14px} th{background:#1e4d8c;color:#fff;text-align:left;padding:8px 10px;position:sticky;top:0}
td{padding:6px 8px;border-bottom:1px solid #e3e8f4;vertical-align:top} .scroll{overflow-x:auto;max-height:70vh}
td input{padding:5px 7px;font-size:13px;width:100%;min-width:120px} .sm{padding:6px 12px;font-size:13px}
.alt{background:#e8eefb;color:#1e4d8c}
</style></head><body>
<div class="top"><h1>📇 நூலகங்கள் / நூலகர் முகவரி &amp; தொலைபேசி எண்</h1><a href="/">← முகப்பு</a></div>
<div class="wrap">
<div class="card">
  <h2>① Excel மூலம் ஒரே முறையில் ஏற்றுதல்</h2>
  <ol class="muted" style="margin:0 0 10px;padding-left:20px;line-height:1.7">
    <li>"மாதிரி Excel பதிவிறக்கு" — எல்லா நூலகங்களின் code, பெயருடன் கோப்பு கிடைக்கும் (ஏற்கனவே உள்ள விவரங்களும் நிரப்பப்பட்டிருக்கும்).</li>
    <li>நூலகர் பெயர், தொலைபேசி, முகவரி (தேவையெனில் English நெடுவரிசைகளும்) நிரப்பவும். <b>A நெடுவரிசை code-ஐ மாற்ற வேண்டாம்.</b></li>
    <li>கோப்பைத் தேர்ந்து "ஏற்று" அழுத்தவும். காலிக் கட்டங்கள் ஏற்கனவே உள்ள மதிப்பை அழிக்காது.</li>
  </ol>
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <button class="alt" onclick="location.href='/api/admin/missing/libraries-template'">⬇️ மாதிரி Excel பதிவிறக்கு</button>
    <input type="file" id="xf" accept=".xlsx,.xls,.csv" style="max-width:320px">
    <button onclick="upload()">⬆️ Excel ஏற்று</button>
  </div>
  <div id="xmsg" class="msg"></div>
</div>
<div class="card">
  <h2>② நூலகம் வாரியாகத் திருத்த</h2>
  <input type="text" id="flt" placeholder="code அல்லது நூலகப் பெயரைத் தேடு…" oninput="render()" style="max-width:360px;margin-bottom:10px">
  <div id="lmsg" class="msg"></div>
  <div class="scroll"><table><thead><tr><th>Code</th><th>நூலகம்</th><th>நூலகர் பெயர்</th><th>தொலைபேசி</th><th>முகவரி</th><th>Library (English)</th><th>Librarian (English)</th><th>Address (English)</th><th></th></tr></thead><tbody id="ltb"></tbody></table></div>
</div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function msg(id, ok, t) { const e = $(id); e.className = 'msg ' + (ok ? 'ok' : 'err'); e.innerHTML = t; e.style.display = t ? 'block' : 'none'; }
async function api(url, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)} : {};
  const r = await fetch(url, opt);
  if (r.status === 401) { location.href = '/login'; throw new Error('login'); }
  return r.json();
}
let LIBS = [];
const F = ['librarian_name', 'phone', 'address', 'name_en', 'librarian_name_en', 'address_en'];
async function load() {
  const d = await api('/api/admin/missing/libraries');
  if (!d.success) { msg('lmsg', false, esc(d.message)); return; }
  LIBS = d.items; render();
}
function render() {
  const q = $('flt').value.trim().toLowerCase();
  $('ltb').innerHTML = LIBS.map((x, i) => [x, i]).filter(([x]) => !q || (x.code + ' ' + x.name).toLowerCase().includes(q)).map(([x, i]) =>
    '<tr><td>' + esc(x.code) + '</td><td>' + esc(x.name) + '</td>' +
    F.map(k => '<td><input id="l_' + k + i + '" value="' + esc(x[k]) + '"></td>').join('') +
    '<td><button class="sm" onclick="saveLib(' + i + ')">சேமி</button></td></tr>').join('');
}
async function saveLib(i) {
  const body = {code: LIBS[i].code};
  F.forEach(k => body[k] = $('l_' + k + i).value);
  const d = await api('/api/admin/missing/library-save', body);
  if (d.success) F.forEach(k => LIBS[i][k] = body[k]);
  msg('lmsg', d.success, d.success ? '✅ ' + esc(LIBS[i].name) + ' சேமிக்கப்பட்டது.' : esc(d.message));
}
async function upload() {
  const f = $('xf').files[0];
  if (!f) { msg('xmsg', false, 'Excel கோப்பைத் தேர்ந்தெடுக்கவும்.'); return; }
  msg('xmsg', true, 'ஏற்றப்படுகிறது…');
  const fd = new FormData(); fd.append('file', f);
  const r = await fetch('/api/admin/missing/libraries-upload', {method:'POST', body: fd});
  if (r.status === 401) { location.href = '/login'; return; }
  const d = await r.json();
  if (!d.success) { msg('xmsg', false, esc(d.message)); return; }
  msg('xmsg', !(d.warnings || []).length, esc(d.message) + (d.warnings || []).map(w => '<br>⚠️ ' + esc(w)).join(''));
  $('xf').value = ''; load();
}
load();
</script></body></html>"""
