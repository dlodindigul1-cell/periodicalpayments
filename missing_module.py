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
import os
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
_send_email = None
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


def init_missing(app, get_conn, classify_lang, parse_email_list, send_email=None):
    global _get_conn, _classify, _parse_email, _send_email
    _get_conn, _classify, _parse_email, _send_email = get_conn, classify_lang, parse_email_list, send_email
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
            cur.execute(
                "CREATE TABLE IF NOT EXISTS missing_mail_log (id SERIAL PRIMARY KEY, magazine TEXT NOT NULL, email TEXT,"
                " date_from DATE, date_to DATE, rows_count INTEGER, ok BOOLEAN NOT NULL DEFAULT TRUE, error TEXT,"
                " sent_by TEXT, sent_at TIMESTAMPTZ NOT NULL DEFAULT now())")
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


def _addr(v):
    """முகவரியின் வரி மாற்றங்கள் (Enter) அப்படியே இருக்கும்; \\r\\n -> \\n, ஒவ்வொரு வரியின் ஓரத்து இடைவெளி நீக்கம், வெற்று வரிகள் நீக்கம்."""
    lines = (v or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(ln.strip() for ln in lines if ln.strip())


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


ALL_MAGS = "__all__"


def _build_report(cur, magazine, dfrom, dto, lang="", only=False):
    """வராத இதழ்கள் (நூலகர் / Admin \"Not received\" எனக் குறித்தவை மட்டும்).
    magazine == ALL_MAGS -> எல்லா இதழ்கள் (lang: ta | en | '' வடிகட்டி; வராத இதழ் உள்ளவை மட்டும்).
    இல்லையெனில் தேர்ந்த இதழ் + அதே Vendor Code உள்ள Group இதழ்கள் (only=True எனில் தேர்ந்த இதழ் மட்டும்)."""
    mags, resolver, aliases = _load_master(cur)
    by_name = {m["name"]: m for m in mags}
    if magazine == ALL_MAGS:
        sel = None
        members = [m for m in mags if not lang or m["lang"] == lang]
    else:
        sel = by_name.get(magazine) or by_name.get(resolver.get(_key(magazine), ""))
        if not sel:
            return None
        members = [sel]
        ck = _code_key(sel["code"])
        if ck and not only:
            members += [m for m in mags if m["name"] != sel["name"] and _code_key(m["code"]) == ck]
    wanted = {m["name"] for m in members}

    cur.execute(
        "SELECT l.code AS lcode, l.name AS lname, l.name_en, l.librarian_name, l.librarian_name_en, l.phone, "
        "       l.address, l.address_en, s.magazine, s.expected_date "
        "FROM lib_schedule s JOIN lib_libraries l ON l.code = s.code "
        "JOIN lib_receipts r ON r.code=s.code AND r.magazine=s.magazine AND r.expected_date=s.expected_date "
        "WHERE s.expected_date BETWEEN %s AND %s AND r.status = 'Not received' "
        "ORDER BY l.code, s.expected_date", (dfrom, dto))
    rows_by = {n: [] for n in wanted}
    for r in cur.fetchall():
        mname = resolver.get(_key(r["magazine"]))
        if mname in wanted:
            rows_by[mname].append(r)

    cur.execute("SELECT DISTINCT ON (magazine) magazine, sent_at, date_from, date_to FROM missing_mail_log "
                "WHERE ok ORDER BY magazine, sent_at DESC")
    last = {x["magazine"]: "%s (%s – %s)" % (x["sent_at"].astimezone(lm.IST).strftime("%d/%m/%Y %H:%M"),
                                              lm.fmt_d(x["date_from"]), lm.fmt_d(x["date_to"])) for x in cur.fetchall()}

    groups = []
    for m in members:
        if magazine == ALL_MAGS and not rows_by[m["name"]]:
            continue
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
                "address": _addr((r["address_en"] or r["address"]) if en else r["address"]),
            })
        groups.append({"magazine": m["name"], "lang": m["lang"], "vendorCode": m["code"], "email": m["email"],
                       "aliases": aliases.get(m["name"], []), "labels": lab,
                       "isSelected": bool(sel) and m["name"] == sel["name"], "lastMail": last.get(m["name"], ""),
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


def _report_args(src):
    """-> (magazine, lang, only, dfrom, dto) ; src = request.args அல்லது JSON body."""
    magazine = (src.get("magazine") or "").strip()
    lang = (src.get("lang") or "").strip()
    lang = lang if lang in ("ta", "en") else ""
    only = str(src.get("only") or "") in ("1", "true", "True")
    dto = lm.parse_iso(src.get("to")) or lm.today_ist()
    dfrom = lm.parse_iso(src.get("from")) or (dto - timedelta(days=30))
    return magazine, lang, only, dfrom, min(dto, lm.today_ist())


def _run_report(src):
    """-> (groups, dfrom, dto, error_response|None)"""
    magazine, lang, only, dfrom, dto = _report_args(src)
    if not magazine:
        return None, dfrom, dto, (jsonify({"success": False, "message": "இதழைத் தேர்ந்தெடுக்கவும்."}), 400)
    if dfrom > dto:
        return None, dfrom, dto, (jsonify({"success": False, "message": "'தேதி முதல்' என்பது 'தேதி வரை'-க்கு முந்தையதாக இருக்க வேண்டும்."}), 400)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            groups = _build_report(cur, magazine, dfrom, dto, lang, only)
    finally:
        conn.close()
    if groups is None:
        return None, dfrom, dto, (jsonify({"success": False, "message": "இதழ் Master-ல் கிடைக்கவில்லை."}), 404)
    return groups, dfrom, dto, None


@miss_bp.route("/api/admin/missing/report")
@_api
def api_missing_report():
    groups, dfrom, dto, err = _run_report(request.args)
    if err:
        return err
    return jsonify({"success": True, "from": lm.fmt_d(dfrom), "to": lm.fmt_d(dto), "groups": groups})


@miss_bp.route("/api/admin/missing/export")
@_api
def api_missing_export():
    import xlsxwriter
    groups, dfrom, dto, err = _run_report(request.args)
    if err:
        return err
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Missing issues")
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1, "text_wrap": True})
    title = wb.add_format({"bold": True, "font_size": 13, "bg_color": "#e8eefb"})
    cell = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "num_format": "@"})
    r = 0
    for g in groups:
        lab = g["labels"]
        ws.merge_range(r, 0, r, 6, "%s: %s   (%s: %s)   %s – %s" % (
            lab["mag"], g["magazine"], lab["vendor"], g["vendorCode"] or "-", lm.fmt_d(dfrom), lm.fmt_d(dto)), title)
        r += 1
        for c, k in enumerate(["sno", "code", "lib", "librarian", "date", "phone", "addr"]):
            ws.write(r, c, lab[k], head)
        r += 1
        if not g["rows"]:
            ws.merge_range(r, 0, r, 6, lab["none"], cell)
            r += 1
        for x in g["rows"]:
            for c, v in enumerate([x["sno"], x["code"], x["library"], x["librarian"], x["date"], x["phone"], x["address"]]):
                ws.write_string(r, c, str(v), cell)
            r += 1
        r += 1
    for c, w in enumerate([7, 15, 36, 24, 16, 16, 40]):
        ws.set_column(c, c, w)
    wb.close()
    fname = "missing_issues_%s_%s.xlsx" % (dfrom.isoformat(), dto.isoformat())
    return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=" + fname})


# ---- மாடல் கடிதம் (தமிழ் / English) — PDF + Mail ------------------------------------------------------ #
LETTER = {
    "ta": {
        "head1": "தமிழ்நாடு அரசு பொது நூலகத் துறை",
        "head2": "திண்டுக்கல் மாவட்ட நூலக ஆணைக்குழு",
        "salute": "அன்புடையீர்",
        "date": "நாள்",
        "paras": [
            "திண்டுக்கல் மாவட்ட நூலக ஆணைக்குழுவின் கீழ் செயல்படும் நூலகங்களுக்கு தங்களின் பருவ இதழான {mag} "
            "விநியோகம் செய்ய ஆணை பிறப்பிக்கப் பட்டுள்ளது.",
            "நூலகர்களிடம் இருந்து பெறப்பட்ட அறிக்கையில் தங்களது இதழ் கீழ்க்கண்ட நூலகங்களில் கிடைக்கப் பெறவில்லை என "
            "சம்மந்தப்பட்ட நூலகர்கள் தெரிவித்துள்ளனர்.",
            "எனவே கீழ்க்குறிப்பிட்டுள்ள நூலகங்களுக்கு விடுபட்ட மேற்படி இதழினை அனுப்பி வைக்கும்படியும், அனுப்பப்பட்ட "
            "விவரத்தினை இவ்வலுவலக மின்னஞ்சல் முகவரி (dlodindigul1Gmail.com)க்கு அனுப்பி வைக்கும்படியும் தங்களைக் கேட்டுக் கொள்கிறேன்.",
            "குறிப்பிட்ட காலக் கெடுவிற்குள் தங்களிடம் இருந்து எவ்வித தகவலும் பெறப்படவில்லை எனில் எதிர்வரும் கேட்புப் "
            "பட்டியலில் இருந்து மேற்கண்ட நூலகங்களுக்கு அனுப்பாத இதழ்களுக்கான தொகை பிடித்தம் செய்யப்படும் என்ற "
            "விவரத்தினையும் தெரிவித்துக் கொள்கிறேன்.",
        ],
        "cols": ["வ.எண்.", "நூலகம் பெயர்", "கிடைக்கப் பெறாத இதழ் தேதி", "நூலக எண்", "நூலகர் தொலைபேசி எண்", "நூலக முகவரி"],
        "sign": ["மாவட்ட நூலக அலுவலர்,", "திண்டுக்கல்."],
        "subject": "இதழ் கிடைக்கப் பெறாதது குறித்து — {mag}",
    },
    "en": {
        "head1": "PUBLIC LIBRARIES DEPARTMENT",
        "head2": "DINDIGUL DISTRICT LOCAL LIBRARY AUTHORITY",
        "salute": "Dear Sir / Madam,",
        "date": "Date",
        "paras": [
            "We have subscribed to the magazines {mag} for the year {fy}.",
            "We have received reports from the concerned librarians regarding the non-receipt of periodical issues "
            "for the dates mentioned below. The following issues have been reported as not received.",
            "Hence, I kindly request you to arrange for the dispatch of the missing issues to the concerned libraries "
            "at the earliest.",
            "I also request you to inform me by email(dlodindigul1@gmail.com) of the date on which the missing issues will be supplied at "
            "the earliest. If no response or confirmation is received from your end, the cost of the missing issues "
            "will be deducted from the forthcoming quarterly invoice.",
        ],
        "list_line": "The List of Libraries are given below",
        "cols": ["S.NO", "NAME OF THE LIBRARY", "NON RECEIVED ISSUE DATE", "LIBRARY CODE", "CONTACT NUM", "ADDRESS"],
        "sign": ["District Library Officer,", "Dindigul."],
        "subject": "Non-receipt of magazine issues — {mag}",
    },
}


def _letter_rows(group):
    """ஒரு நூலகத்துக்கு ஒரே வரிசை; அந்த நூலகத்தின் வராத தேதிகள் ஒன்றின் கீழ் ஒன்றாக."""
    by, order = {}, []
    for x in group["rows"]:
        if x["code"] not in by:
            by[x["code"]] = {"library": x["library"], "dates": [], "code": x["code"],
                             "phone": x["phone"], "address": x["address"]}
            order.append(x["code"])
        by[x["code"]]["dates"].append(x["date"])
    return [dict(by[c], sno=i, date="\n".join(by[c]["dates"])) for i, c in enumerate(order, start=1)]


def _fy_text(d):
    return lm.quarter_label(d).split("-Q")[0]


def _letter_paras(group, today):
    L = LETTER[group["lang"]]
    return [p.format(mag=group["magazine"], fy=_fy_text(today)) for p in L["paras"]]


def build_letter_pdf(group, today):
    from fpdf import FPDF
    from fpdf.fonts import FontFace
    L = LETTER[group["lang"]]
    en = group["lang"] == "en"
    paras = _letter_paras(group, today)
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_margins(15, 14, 15)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("Tamil", "", FONT_REG)
    pdf.add_font("Tamil", "B", FONT_BOLD)
    pdf.set_text_shaping(True)
    pdf.add_page()
    pdf.set_font("Tamil", "B", 11)
    pdf.cell(0, 6, L["head1"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, L["head2"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)
    pdf.set_font("Tamil", size=10)
    y = pdf.get_y()
    pdf.cell(110, 6, L["salute"], new_x="LEFT", new_y="TOP")
    pdf.set_xy(125, y)
    pdf.cell(0, 6, "%s: %s" % (L["date"], lm.fmt_d(today)), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    for i, p in enumerate(paras):
        pdf.set_x(pdf.l_margin + 10)
        pdf.multi_cell(0, 5.6, p, align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)
    if en:
        pdf.set_font("Tamil", size=9)
        pdf.cell(0, 6, L["list_line"], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Tamil", size=8)
    widths = (17, 35, 24, 26, 28, 60)                       # மொத்தம் 190 மி.மீ
    head_style = FontFace(emphasis="BOLD", color=(0, 0, 0), fill_color=(238, 241, 248))
    with pdf.table(col_widths=widths, text_align=("CENTER", "LEFT", "CENTER", "CENTER", "CENTER", "LEFT"),
                   headings_style=head_style, line_height=4.2, padding=1.2, borders_layout="ALL") as table:
        h = table.row()
        for c in L["cols"]:
            h.cell(c, align="C")
        for x in _letter_rows(group):
            r = table.row()
            for v in (str(x["sno"]), x["library"], x["date"], x["code"], x["phone"], x["address"]):
                r.cell(v)
    pdf.ln(8)
    pdf.set_font("Tamil", size=10)
    for ln in L["sign"]:
        pdf.set_x(pdf.w - pdf.r_margin - 70)
        pdf.cell(70, 6, ln, align="C", new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def build_letter_html(group, today):
    """மெயில் உடல் — அதே கடிதம் + அட்டவணை (PDF-ம் இணைக்கப்படும்)."""
    from html import escape as e
    L = LETTER[group["lang"]]
    en = group["lang"] == "en"
    paras = _letter_paras(group, today)

    def multi(v):
        return e(v).replace("\n", "<br>")
    th = "border:1px solid #444;padding:5px 7px;background:#eef1f8;font-size:13px;text-align:center"
    td = "border:1px solid #444;padding:5px 7px;font-size:13px;vertical-align:top"
    head = "".join("<th style='%s'>%s</th>" % (th, e(c)) for c in L["cols"])
    body = "".join(
        "<tr><td style='%s;text-align:center'>%d</td><td style='%s'>%s</td><td style='%s;text-align:center'>%s</td>"
        "<td style='%s;text-align:center'>%s</td><td style='%s;text-align:center'>%s</td><td style='%s'>%s</td></tr>" % (
            td, x["sno"], td, e(x["library"]), td, multi(x["date"]), td, e(x["code"]), td, e(x["phone"]),
            td, multi(x["address"])) for x in _letter_rows(group))
    ps = "".join("<p style='margin:0 0 12px 28px;text-align:justify'>%s</p>" % e(p) for p in paras)
    listline = "<p style='margin:0 0 6px'>%s</p>" % e(L["list_line"]) if en else ""
    return (
        "<div style=\"font-family:'Noto Sans Tamil','Latha',Arial,sans-serif;font-size:14px;color:#111;max-width:760px\">"
        "<div style='text-align:center;font-weight:700'>%s<br>%s</div><br>"
        "<table style='width:100%%'><tr><td>%s</td><td style='text-align:right'>%s: %s</td></tr></table><br>"
        "%s%s<table style='border-collapse:collapse;width:100%%'><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
        "<p style='margin:18px 0 0;text-align:right'>%s<br>%s</p></div>" % (
            e(L["head1"]), e(L["head2"]), e(L["salute"]), e(L["date"]), lm.fmt_d(today), ps, listline, head, body,
            e(L["sign"][0]), e(L["sign"][1])))


@miss_bp.route("/api/admin/missing/letter-pdf")
@_api
def api_missing_letter_pdf():
    """தேர்ந்த இதழுக்கான கடிதம் (PDF) — மெயில் அனுப்பும் முன் பார்வையிட / பதிவிறக்க."""
    args = request.args.to_dict()
    args["only"] = "1"
    groups, dfrom, dto, err = _run_report(args)
    if err:
        return err
    g = groups[0]
    if not g["rows"]:
        return jsonify({"success": False, "message": "இந்த இதழுக்கு வராத பதிவுகள் இல்லை."}), 404
    try:
        pdf = build_letter_pdf(g, lm.today_ist())
    except ImportError as ex:
        return jsonify({"success": False, "message": "PDF நூலகம் இல்லை: %s" % ex}), 500
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": "inline; filename=missing_issues_letter_%s.pdf" % lm.today_ist().isoformat()})


SAMPLE_NOTE = {
    "ta": "🧪 இது சோதனை (Sample) மெயில் — இதழ் விநியோகஸ்தருக்கு அனுப்பப்படவில்லை.",
    "en": "🧪 This is a SAMPLE mail — it has NOT been sent to the magazine vendor.",
}


@miss_bp.route("/api/admin/missing/send-sample", methods=["POST"])
@_api
def api_missing_send_sample():
    """சோதனை மெயில்: தேர்ந்த இதழின் உண்மையான கடிதத்தை Admin கொடுத்த Email-க்கு மட்டும் அனுப்பும்
    (விநியோகஸ்தருக்குச் செல்லாது; missing_mail_log-ல் பதியாது). வராத பதிவுகள் இல்லையெனில் முதல் 3 நூலகங்களின் மாதிரி வரிசைகள்."""
    if _send_email is None:
        return jsonify({"success": False, "message": "Mail வசதி இணைக்கப்படவில்லை (app.py-ல் init_missing-க்கு send_email கொடுக்கவும்)."}), 500
    body = request.get_json(force=True, silent=True) or {}
    ok_list, bad_list = _parse_email(body.get("email") or "")
    if bad_list or not ok_list:
        return jsonify({"success": False, "message": "சரியான Sample Email முகவரியை உள்ளிடவும்" +
                        ((": " + ", ".join(bad_list)) if bad_list else ".")}), 400
    groups, dfrom, dto, err = _run_report(body)
    if err:
        return err
    today = lm.today_ist()
    g = next((x for x in groups if x["rows"]), None)
    dummy = False
    if g is None:
        dummy = True
        g = dict(groups[0]) if groups else {"magazine": "(இதழ்)", "lang": (body.get("lang") or "ta"), "email": ""}
        g["lang"] = g.get("lang") if g.get("lang") in ("ta", "en") else "ta"
        en = g["lang"] == "en"
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT code, name, name_en, librarian_name, librarian_name_en, phone, address, address_en "
                            "FROM lib_libraries ORDER BY code LIMIT 3")
                libs = cur.fetchall()
        finally:
            conn.close()
        g["rows"] = [{
            "sno": i, "code": r["code"], "library": (r["name_en"] or r["name"]) if en else r["name"],
            "librarian": ((r["librarian_name_en"] or r["librarian_name"]) if en else r["librarian_name"]) or "",
            "date": lm.fmt_d(dto - timedelta(days=7 * i)), "phone": r["phone"] or "",
            "address": _addr((r["address_en"] or r["address"]) if en else r["address"]),
        } for i, r in enumerate(libs, start=1)]
        if not g["rows"]:
            return jsonify({"success": False, "message": "நூலகப் பட்டியல் காலியாக உள்ளது; Sample உருவாக்க முடியவில்லை."}), 400
    try:
        pdf = build_letter_pdf(g, today)
        L = LETTER[g["lang"]]
        html = ("<p style='background:#fff3cd;border:1px solid #e0c36a;padding:8px 12px;font-size:13px'>%s%s</p>" % (
            SAMPLE_NOTE[g["lang"]],
            (" (வராத பதிவுகள் இல்லாததால் மாதிரி வரிசைகள்)" if dummy and g["lang"] == "ta" else
             (" (no missing records — sample rows shown)" if dummy else "")))) + build_letter_html(g, today)
        _send_email(ok_list, "[SAMPLE] " + L["subject"].format(mag=g["magazine"]), html, pdf,
                    "sample_missing_issues_%s.pdf" % today.isoformat())
    except Exception as ex:  # noqa: BLE001
        return jsonify({"success": False, "message": "Sample Mail அனுப்ப முடியவில்லை: %s" % ex}), 500
    return jsonify({"success": True, "message": "🧪 Sample Mail %s முகவரிக்கு அனுப்பப்பட்டது (%s%s)." % (
        ", ".join(ok_list), g["magazine"], " — மாதிரி வரிசைகள்" if dummy else "")})


@miss_bp.route("/api/admin/missing/send-mail", methods=["POST"])
@_api
def api_missing_send_mail():
    """வராத இதழ்கள் உள்ள ஒவ்வொரு இதழுக்கும் (magazine / ALL) அந்த இதழின் Email-க்கு மாடல் கடிதம் + PDF அனுப்பும்."""
    if _send_email is None:
        return jsonify({"success": False, "message": "Mail வசதி இணைக்கப்படவில்லை (app.py-ல் init_missing-க்கு send_email கொடுக்கவும்)."}), 500
    body = request.get_json(force=True, silent=True) or {}
    groups, dfrom, dto, err = _run_report(body)
    if err:
        return err
    today = lm.today_ist()
    user = (lm._user().get("username") or "admin")
    results = []
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            for g in groups:
                if not g["rows"]:
                    continue
                res = {"magazine": g["magazine"], "email": g["email"], "count": len(g["rows"]), "ok": False, "note": ""}
                if not g["email"]:
                    res["note"] = "Email இல்லை (\"Email இல்லாத இதழ்கள்\" தாவலில் சேர்க்கவும்)"
                else:
                    try:
                        pdf = build_letter_pdf(g, today)
                        subject = LETTER[g["lang"]]["subject"].format(mag=g["magazine"])
                        _send_email(g["email"], subject, build_letter_html(g, today), pdf,
                                    "missing_issues_%s.pdf" % today.isoformat())
                        res["ok"] = True
                        res["note"] = "அனுப்பப்பட்டது"
                    except Exception as ex:  # noqa: BLE001
                        res["note"] = str(ex)
                    cur.execute(
                        "INSERT INTO missing_mail_log (magazine, email, date_from, date_to, rows_count, ok, error, sent_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (g["magazine"], g["email"], dfrom, dto, len(g["rows"]), res["ok"],
                         "" if res["ok"] else res["note"], user))
                results.append(res)
        conn.commit()
    finally:
        conn.close()
    sent = sum(1 for r in results if r["ok"])
    if not results:
        return jsonify({"success": False, "message": "இந்தத் தேதி வரம்பில் வராத இதழ்கள் இல்லை; மெயில் அனுப்பப்படவில்லை.", "results": []})
    return jsonify({"success": sent > 0, "results": results,
                    "message": "✉️ %d / %d இதழ்களுக்கு மெயில் அனுப்பப்பட்டது." % (sent, len(results))})


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
    vals = [(_addr(body.get(c)) if c in ("address", "address_en") else (body.get(c) or "").strip()) for c in LIB_COLS]
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
                    vals[2], vals[5] = _addr(vals[2]), _addr(vals[5])      # முகவரி: வரி மாற்றங்கள் அப்படியே
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


# ---- நூலகங்கள் / நூலகர் முகவரி & தொலைபேசி — Excel / PDF பதிவிறக்கம் ------------------------------------ #
FONT_REG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "NotoSansTamil-Regular.ttf")
FONT_BOLD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "NotoSansTamil-Bold.ttf")

LIBX_LABELS = {
    "ta": {"title": "நூலகங்கள் / நூலகர் முகவரி மற்றும் தொலைபேசி எண்", "office": "மாவட்ட நூலக அலுவலகம், திண்டுக்கல்",
           "sno": "வ.எண்.", "code": "நூலகக் குறியீடு", "lib": "நூலகம்", "cat": "வகை", "librarian": "நூலகர் பெயர்",
           "phone": "தொலைபேசி எண்", "addr": "முகவரி", "page": "பக்கம்", "total": "மொத்த நூலகங்கள்", "date": "நாள்"},
    "en": {"title": "Libraries - Librarian Address & Phone Directory", "office": "District Library Office, Dindigul",
           "sno": "S.No.", "code": "Library Code", "lib": "Library", "cat": "Category", "librarian": "Librarian",
           "phone": "Phone", "addr": "Address", "page": "Page", "total": "Total libraries", "date": "Date"},
}


def _libx_rows(lang):
    """வடிகட்டிய (category, q) நூலகப் பட்டியல் -> (rows, category) ; lang=en எனில் English புலங்கள் (காலி எனில் தமிழ்)."""
    cat = (request.args.get("category") or "").strip()
    q = _key(request.args.get("q") or "")
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, name, category, name_en, librarian_name, librarian_name_en, phone, "
                        "address, address_en FROM lib_libraries ORDER BY code")
            db = cur.fetchall()
    finally:
        conn.close()
    en = lang == "en"
    out = []
    for r in db:
        if cat and r["category"] != cat:
            continue
        if q and q not in _key("%s %s" % (r["code"], r["name"])):
            continue
        out.append({
            "code": r["code"] or "",
            "library": ((r["name_en"] or r["name"]) if en else r["name"]) or "",
            "category": r["category"] or "",
            "librarian": ((r["librarian_name_en"] or r["librarian_name"]) if en else r["librarian_name"]) or "",
            "phone": r["phone"] or "",
            "address": _addr((r["address_en"] or r["address"]) if en else r["address"]),
        })
    out.sort(key=lambda x: (lm._category_sort_key(x["category"]), x["code"]))
    for i, x in enumerate(out, start=1):
        x["sno"] = i
    return out, cat


def _libx_xlsx(rows, lab, cat):
    import xlsxwriter
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Libraries")
    ttl = wb.add_format({"bold": True, "font_size": 14, "align": "center", "valign": "vcenter"})
    sub = wb.add_format({"font_size": 10, "align": "center", "font_color": "#44506f"})
    head = wb.add_format({"bold": True, "bg_color": "#1e4d8c", "font_color": "#ffffff", "border": 1,
                          "text_wrap": True, "align": "center", "valign": "vcenter"})
    cell = wb.add_format({"border": 1, "text_wrap": True, "valign": "top", "num_format": "@"})
    num = wb.add_format({"border": 1, "valign": "top", "align": "center"})
    keys = ["sno", "code", "lib", "cat", "librarian", "phone", "addr"]
    ws.merge_range(0, 0, 0, len(keys) - 1, lab["office"] + " - " + lab["title"], ttl)
    ws.merge_range(1, 0, 1, len(keys) - 1, "%s: %s%s   |   %s: %d" % (
        lab["date"], lm.fmt_d(lm.today_ist()), ("   |   " + cat) if cat else "", lab["total"], len(rows)), sub)
    ws.set_row(0, 24)
    for c, k in enumerate(keys):
        ws.write(3, c, lab[k], head)
    ws.set_row(3, 28)
    for i, x in enumerate(rows, start=4):
        ws.write_number(i, 0, x["sno"], num)
        for c, v in enumerate([x["code"], x["library"], x["category"], x["librarian"], x["phone"], x["address"]], start=1):
            ws.write_string(i, c, v, cell)
    for c, w in enumerate([8, 16, 36, 22, 24, 18, 34]):
        ws.set_column(c, c, w)
    ws.freeze_panes(4, 0)
    ws.autofilter(3, 0, 3 + max(len(rows), 1), len(keys) - 1)
    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(3)
    wb.close()
    return buf.getvalue()


def _libx_pdf(rows, lab, cat):
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    class _Pdf(FPDF):
        def footer(self):
            self.set_y(-10)
            self.set_font("Tamil", size=8)
            self.cell(0, 5, "%s %d / {nb}" % (lab["page"], self.page_no()), align="C")

    pdf = _Pdf(orientation="L", unit="mm", format="A4")
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_font("Tamil", "", FONT_REG)
    pdf.add_font("Tamil", "B", FONT_BOLD)
    pdf.set_text_shaping(True)
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font("Tamil", "B", 14)
    pdf.cell(0, 8, lab["office"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Tamil", "B", 12)
    pdf.cell(0, 7, lab["title"], align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Tamil", size=9)
    pdf.cell(0, 6, "%s: %s%s   |   %s: %d" % (lab["date"], lm.fmt_d(lm.today_ist()), ("   |   " + cat) if cat else "",
                                              lab["total"], len(rows)), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("Tamil", size=8)
    widths = (12, 26, 60, 34, 44, 32, 69)          # மொத்தம் 277 மி.மீ (A4 landscape - 20)
    head_style = FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=(30, 77, 140))
    with pdf.table(col_widths=widths, text_align=("CENTER", "LEFT", "LEFT", "LEFT", "LEFT", "LEFT", "LEFT"),
                   headings_style=head_style, line_height=4.4, padding=1, borders_layout="ALL") as table:
        h = table.row()
        for k in ("sno", "code", "lib", "cat", "librarian", "phone", "addr"):
            h.cell(lab[k], align="C")
        for x in rows:
            r = table.row()
            for v in (str(x["sno"]), x["code"], x["library"], x["category"], x["librarian"], x["phone"], x["address"]):
                r.cell(v)
    return bytes(pdf.output())


@miss_bp.route("/api/admin/missing/libraries-export")
@_api
def api_missing_libraries_export():
    """நூலகப் பட்டியல் (code, நூலகம், வகை, நூலகர், தொலைபேசி, முகவரி) — format=xlsx|pdf, lang=ta|en, category, q."""
    fmt = (request.args.get("format") or "xlsx").lower()
    lang = "en" if (request.args.get("lang") or "ta").lower() == "en" else "ta"
    rows, cat = _libx_rows(lang)
    lab = LIBX_LABELS[lang]
    stamp = lm.today_ist().isoformat()
    try:
        if fmt == "pdf":
            body, mime, ext = _libx_pdf(rows, lab, cat), "application/pdf", "pdf"
        else:
            body, mime, ext = _libx_xlsx(rows, lab, cat), \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    except ImportError as e:
        return jsonify({"success": False, "message": "தேவையான நூலகம் இல்லை: %s (requirements.txt-ஐ நிறுவவும்)" % e}), 500
    return Response(body, mimetype=mime,
                    headers={"Content-Disposition": "attachment; filename=library_directory_%s_%s.%s" % (lang, stamp, ext)})


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
  <p class="muted" style="margin:0 0 8px">நூலகர் / Admin "Not received" எனக் குறித்த இதழ்கள் மட்டும். "அனைத்தும்" தேர்ந்தால் வராத இதழ் உள்ள எல்லா இதழ்களும் வரும். காட்டிய பின் இதழுக்கான கடிதத்தைப் பார்க்கலாம்; ✉️ பொத்தான் அந்த இதழின் Email-க்கு மாடல் கடிதத்தை (தமிழ் இதழுக்குத் தமிழ், ஆங்கில இதழுக்கு English) PDF-உடன் அனுப்பும்.</p>
  <div class="flt noprint">
    <div><label>தேதி முதல்</label><input type="date" id="dfrom"></div>
    <div><label>தேதி வரை</label><input type="date" id="dto"></div>
    <div><label>மொழி</label><select id="mlang" onchange="fillMags()"><option value="">அனைத்தும்</option><option value="ta">தமிழ்</option><option value="en">English</option></select></div>
    <div><label>இதழ்</label><select id="mag"><option value="">-- தேர்ந்தெடு --</option><option value="__all__">அனைத்தும்</option></select></div>
  </div>
  <div class="noprint" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
    <button onclick="loadReport()">📋 காட்டு</button>
    <button class="alt sm" onclick="exportXlsx()">⬇️ Excel</button>
    <button class="alt sm" onclick="window.print()">🖨️ Print</button>
  </div>
  <div class="noprint" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input type="text" id="smail" placeholder="Sample Mail பெற உங்கள் Email" style="max-width:300px">
    <button class="alt sm" onclick="sendSample()">🧪 Sample Mail அனுப்பு</button>
    <span class="muted">தேர்ந்த இதழின் கடிதம் இந்த Email-க்கு மட்டும் செல்லும் (இதழ் Email-க்குச் செல்லாது)</span>
  </div>
  <div id="rmsg" class="msg"></div>
  <div id="mailres" class="noprint"></div>
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
  $('mag').innerHTML = '<option value="">-- தேர்ந்தெடு --</option><option value="__all__">அனைத்தும்</option>' +
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
function qs(mag, only) {
  const m = mag || $('mag').value;
  let q = 'magazine=' + encodeURIComponent(m) + '&from=' + $('dfrom').value + '&to=' + $('dto').value;
  if (m === '__all__' && $('mlang').value) q += '&lang=' + $('mlang').value;
  if (only) q += '&only=1';
  return q;
}
let GROUPS = [];
async function loadReport() {
  if (!$('mag').value) { msg('rmsg', false, 'இதழைத் தேர்ந்தெடுக்கவும்.'); return; }
  msg('rmsg', true, 'ஏற்றப்படுகிறது…');
  $('mailres').innerHTML = '';
  const d = await api('/api/admin/missing/report?' + qs());
  if (!d.success) { msg('rmsg', false, esc(d.message)); $('rout').innerHTML = ''; return; }
  GROUPS = d.groups;
  const total = d.groups.reduce((a, g) => a + g.rows.length, 0);
  msg('rmsg', true, d.from + ' முதல் ' + d.to + ' வரை — மொத்தம் ' + total + ' வராத இதழ் பதிவுகள் (' + d.groups.filter(g => g.rows.length).length + ' இதழ்).');
  const withRows = d.groups.filter(g => g.rows.length);
  $('rout').innerHTML = (withRows.length > 1 ? '<div class="noprint" style="margin:12px 0"><button onclick="sendMail(-1)">✉️ காட்டிய ' + withRows.length + ' இதழ்களுக்கும் Mail அனுப்பு</button></div>' : '') +
  d.groups.map((g, gi) => {
    const L = g.labels;
    const al = g.aliases.length ? ' · ' + esc(g.aliases.join(', ')) : '';
    const act = g.rows.length
      ? '<span class="noprint" style="float:right;display:flex;gap:6px;flex-wrap:wrap"><button class="sm alt" onclick="letterPdf(' + gi + ')">📄 கடிதம் PDF</button>' +
        '<button class="sm" onclick="sendMail(' + gi + ')">✉️ Mail அனுப்பு</button></span>' : '';
    const em = g.rows.length
      ? '<div class="noprint muted" style="margin:-2px 0 6px">' + (g.email ? '📧 ' + esc(g.email) : '<span class="bad-c">Email இல்லை — "② Email இல்லாத இதழ்கள்" தாவலில் சேர்க்கவும்</span>') +
        (g.lastMail ? ' · கடைசியாக அனுப்பியது: ' + esc(g.lastMail) : '') + '</div>' : '';
    return '<div class="gh">' + act + esc(L.mag) + ': ' + esc(g.magazine) + al + ' <small>(' + esc(L.vendor) + ': ' + esc(g.vendorCode || '-') + ' · ' + g.rows.length + ')</small></div>' + em +
      '<div class="scroll"><table><thead><tr><th>' + [L.sno, L.code, L.lib, L.librarian, L.date, L.phone, L.addr].map(esc).join('</th><th>') + '</th></tr></thead><tbody>' +
      (g.rows.map(x => '<tr><td>' + x.sno + '</td><td>' + esc(x.code) + '</td><td>' + esc(x.library) + '</td><td>' + esc(x.librarian) + '</td><td>' + esc(x.date) +
        '</td><td>' + esc(x.phone) + '</td><td style="white-space:pre-line">' + esc(x.address) + '</td></tr>').join('') ||
        '<tr><td colspan="7" class="ok-c">' + esc(L.none) + '</td></tr>') + '</tbody></table></div>';
  }).join('');
}
async function sendSample() {
  if (!$('mag').value) { msg('rmsg', false, 'இதழைத் தேர்ந்தெடுக்கவும்.'); return; }
  const email = $('smail').value.trim();
  if (!email) { msg('rmsg', false, 'Sample Mail பெற வேண்டிய Email-ஐ உள்ளிடவும்.'); return; }
  msg('rmsg', true, 'Sample Mail அனுப்பப்படுகிறது…');
  let d;
  try { d = await api('/api/admin/missing/send-sample', {magazine: $('mag').value, email, from: $('dfrom').value, to: $('dto').value, lang: $('mlang').value, only: '1'}); }
  catch (e) { msg('rmsg', false, 'Sample Mail அனுப்ப முடியவில்லை: ' + esc(e)); return; }
  msg('rmsg', !!d.success, esc(d.message));
}
function letterPdf(gi) {
  window.open('/api/admin/missing/letter-pdf?' + qs(GROUPS[gi].magazine, true), '_blank');
}
async function sendMail(gi) {
  const list = gi < 0 ? GROUPS.filter(g => g.rows.length) : [GROUPS[gi]];
  const noMail = list.filter(g => !g.email).map(g => g.magazine);
  const lines = list.filter(g => g.email).map(g => '• ' + g.magazine + ' → ' + g.email + ' (' + g.rows.length + ' பதிவு, ' + (g.lang === 'en' ? 'English' : 'தமிழ்') + ')');
  if (!lines.length) { msg('rmsg', false, 'Email உள்ள இதழ் இல்லை — "② Email இல்லாத இதழ்கள்" தாவலில் Email சேர்க்கவும்.'); return; }
  if (!confirm('இந்த இதழ்களின் Email-க்கு கடிதம் (PDF இணைப்புடன்) அனுப்பவா?\\n\\n' + lines.join('\\n') +
      (noMail.length ? '\\n\\nEmail இல்லாததால் தவிர்க்கப்படுபவை: ' + noMail.join(', ') : ''))) return;
  msg('rmsg', true, 'Mail அனுப்பப்படுகிறது…');
  const body = {from: $('dfrom').value, to: $('dto').value, lang: $('mlang').value};
  if (gi < 0) { body.magazine = $('mag').value; }
  else { body.magazine = GROUPS[gi].magazine; body.only = '1'; }
  let d;
  try { d = await api('/api/admin/missing/send-mail', body); } catch (e) { msg('rmsg', false, 'Mail அனுப்ப முடியவில்லை: ' + esc(e)); return; }
  msg('rmsg', !!d.success, esc(d.message));
  $('mailres').innerHTML = (d.results || []).map(r => '<div class="' + (r.ok ? 'ok-c' : 'bad-c') + '" style="margin:3px 0">' + (r.ok ? '✅ ' : '❌ ') + esc(r.magazine) + ' → ' + esc(r.email || '-') + ' : ' + esc(r.note) + '</div>').join('');
  if (d.results && d.results.some(r => r.ok)) loadReport().then(() => { msg('rmsg', !!d.success, esc(d.message)); });
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
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
    <input type="text" id="flt" placeholder="code அல்லது நூலகப் பெயரைத் தேடு…" oninput="render()" style="max-width:300px">
    <select id="fcat" onchange="render()" style="max-width:220px"><option value="">எல்லா வகைகளும்</option></select>
    <select id="flang" style="max-width:130px" title="பதிவிறக்க மொழி"><option value="ta">தமிழ்</option><option value="en">English</option></select>
    <button class="alt" onclick="dl('xlsx')">📥 Excel பதிவிறக்கு</button>
    <button class="alt" onclick="dl('pdf')">📄 PDF பதிவிறக்கு</button>
  </div>
  <div class="muted" style="margin:-4px 0 8px">மேலே தேடல் / வகை தேர்ந்தால் அந்தப் பட்டியல் மட்டுமே Excel / PDF-ல் வரும். English தேர்ந்தால் English விவரம் இல்லாத இடத்தில் தமிழ் வரும்.</div>
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
  LIBS = d.items;
  const cats = [...new Set(LIBS.map(x => x.category).filter(Boolean))].sort();
  $('fcat').innerHTML = '<option value="">எல்லா வகைகளும்</option>' + cats.map(c => '<option>' + esc(c) + '</option>').join('');
  render();
}
function dl(fmt) {
  const p = new URLSearchParams({format: fmt, lang: $('flang').value});
  if ($('fcat').value) p.set('category', $('fcat').value);
  if ($('flt').value.trim()) p.set('q', $('flt').value.trim());
  location.href = '/api/admin/missing/libraries-export?' + p.toString();
}
function render() {
  const q = $('flt').value.trim().toLowerCase();
  $('ltb').innerHTML = LIBS.map((x, i) => [x, i]).filter(([x]) => (!q || (x.code + ' ' + x.name).toLowerCase().includes(q)) && (!$('fcat').value || x.category === $('fcat').value)).map(([x, i]) =>
    '<tr><td>' + esc(x.code) + '</td><td>' + esc(x.name) + '</td>' +
    F.map(k => (k === 'address' || k === 'address_en')
       ? '<td><textarea id="l_' + k + i + '" rows="6" style="width:100%;min-width:200px;font:inherit;font-size:13px;padding:5px 7px">' + esc(x[k]) + '</textarea></td>'
       : '<td><input id="l_' + k + i + '" value="' + esc(x[k]) + '"></td>').join('') +
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
