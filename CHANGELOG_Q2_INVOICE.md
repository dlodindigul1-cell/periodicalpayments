# Q2 / Partial Invoice & Grouped Quarter update

## Implemented
- Removed the `magazine + quarter` single-invoice restriction. Multiple invoice rows can now exist for one magazine/quarter.
- Invoice entry asks whether the invoice covers the full quarter.
- For partial invoices, July/August/September (or the corresponding quarter months) can be selected individually.
- QTR Issues is recalculated from the selected coverage.
- Q1/Q2/Q3/Q4 can be grouped into one invoice, intended for publishers who send several completed quarters together at Q4.
- Payment selection is invoice-based, so separate invoices for the same magazine/quarter can be paid independently.
- Partial/grouped coverage is shown in the payment screen; partial coverage is highlighted in red and can be clicked to view the exact months/quarters.
- The quarter non-supply deduction is applied only once to the first invoice recorded for that magazine/quarter, preventing duplicate deduction across split invoices.
- Payment voucher/advice data carries grouped quarter / partial-month information.
- Existing databases are migrated automatically on first authenticated request: new coverage columns are added and the old unique `(magazine, quarter)` constraint is removed.

## Important
- Grouped-quarter invoices are treated as full quarters; partial-month selection is disabled when grouping quarters.
- Existing data is preserved. The migration does not delete existing payment rows.

# பல Quarter — ஒரே Payment Advice / Payment Voucher (2026-10-05)

## Implemented
- Payment Advice, Payment Voucher Creation, Voucher Numbers (வவுச்சர் எண் உள்ளிடல்) ஆகிய மூன்று திரைகளிலும் Quarter தேர்வு இப்போது டிக் பெட்டிகள் (பல Quarter தேர்வு).
- ஒரு Set + தேர்ந்தெடுத்த Quarter-கள் => ஒரே Payment Advice PDF, ஒரே Payment Voucher PDF. ஒவ்வொரு Quarter invoice-க்கும் தனி வரி, தனி Voucher எண் (முன்புபோல்).
- பல Quarter இருந்தால் இதழ் பெயருடன் Quarter காட்டும்: "குமுதம் (Q2 2025-26)".
- Advice தலைப்பு / Voucher காலம்: "2025-2026 Q1-Q4 ( ஏப்ரல் 2025 முதல் மார்ச் 2026 வரை )". Q-க்கள் தொடர்ச்சியாக இல்லையெனில் ஒவ்வொன்றும் தனியாகப் பட்டியலிடப்படும்.
- "Vouchers Numbers : 101 to 104/2025-26" — பல நிதியாண்டுகள் கலந்தால் கடைசி Quarter-ன் நிதியாண்டு.
- Voucher-ல் இதழ் பெயர் + Quarter நீளமானால் எழுத்தளவு தானாகக் குறையும்.
- API: `/api/vouchers/set-numbers`, `/api/vouchers/by-set`, `/api/reports/payment-advice[/pdf]`, `/api/reports/payment-voucher[/pdf]` — `quarter=Q1,Q2,...` (கமா) ஏற்கும்; ஒரே Quarter அனுப்பினால் பழைய நடத்தையே.

## Important
- Bill Set எண்கள் Quarter-க்குள் தனித்தனி (1–20). எனவே Set எண் ஒன்றுதான் என்றாலும், தேர்ந்தெடுத்த Quarter-களில் அந்த Set-ல் உள்ள எல்லா இதழ்களும் வரும். தேவையான Quarter-களை மட்டும் டிக் செய்யவும்.
- ஒரு Voucher-ல் அதிகபட்சம் 10 வரிகள் (VOUCHER_MAX_ROWS) — Quarter வரிகளும் இதில் அடங்கும்.
- அனைத்து வரிகளுக்கும் Voucher எண் இருந்தால் மட்டுமே Advice / Voucher உருவாகும் (முன்புபோல்).

# இதழ்–Quarter தேர்வு, Issue Price தானியக் கணக்கு, Invoice தடை (2026-10-05)

## Implemented
- Master → இதழ் திருத்து: Price / Discount (%) மாறும்போது Issue Price (After Discount) = Price − (Price × Discount ÷ 100) தானாக வரும் (100, 5% = 95); கையால் மாற்றவும் முடியும்.
- Master → இதழ் திருத்து: இதழின் உண்மையான quarter பதிவுகளின்படி Quarter டிக் பெட்டிகள் முன்பே டிக் செய்யப்படும்.
- சேமிக்கும்போது, ஏற்கனவே பதிவுள்ள டிக் செய்த Quarter-களின் விலை/நூலக எண்ணிக்கை மாறுமானால், "இந்த Quarter-களின் விலை மாறும்: Q ₹27 → ₹36" என்ற உறுதிப்படுத்தல் card வரும்.
- ஒரே ஒரு முறை (app_flags: magazine_quarters_backfill_2025Q1_2026Q1): எல்லா இதழ்களுக்கும் 2025-2026-Q1 … 2026-2027-Q1 விடுபட்ட quarter பதிவுகள் சேர்க்கப்படும் (இப்போது தெரியும் அதே விலை). எதுவும் நீக்கப்படாது; பிறகு நீக்கியவை மீண்டும் சேராது.
- Invoice பதிவு: Master-ல் அந்த Quarter டிக் செய்யப்படாத இதழ் பட்டியலில் சாம்பல் நிறத்தில் "— இந்த quarter-க்கு இல்லை" என்று காட்டும்; தேர்ந்தால் சிவப்பு எச்சரிக்கை, பதிவு பட்டன் முடக்கம். Server (`POST /api/payments`, புதிய பதிவு) இதே சோதனையைச் செய்யும்.
- Magazine-wise report, Pending Invoice Reminder: carry-forward நீக்கம் — அந்த Quarter-க்கு டிக் செய்த இதழ்கள் மட்டுமே வரும்.

## Important
- 2026-2027-Q2 முதல் இதழ்களை Master → இதழ் திருத்து-ல் (அல்லது "📅 Quarter சேர்") டிக் செய்ய வேண்டும்; இல்லையெனில் அந்த Quarter-ல் Invoice பதிவாகாது.
- ஏற்கனவே பதிவான Invoice-ஐ Update செய்வது (is_update) தடுக்கப்படாது; புதிய பதிவுகள் மட்டும் தடுக்கப்படும்.

# Master திருத்து — பதிவுள்ள Quarter-கள் பூட்டு + பழைய Quarter மாற்று (2026-10-05)
- இதழைத் திருத்தும்போது ஏற்கனவே பதிவுள்ள எல்லா Quarter-களும் டிக் செய்யப்பட்டு 🔒 பூட்டப்படும்; பதிவில்லாத Quarter-களை மட்டுமே டிக் செய்யலாம். சேமிப்பு அவற்றுக்கு மட்டுமே பொருந்தும் (server பக்கமும் பதிவுள்ளவற்றைத் தொடாது).
- புதிய பகுதி "🛠️ பழைய Quarter விலை / No of Libraries (Subscription) மாற்று": பூட்டப்பட்ட Quarter-களில் ஒன்று/பல தேர்ந்து Price, Discount %, Issue Price, நூலக எண்ணிக்கையை மாற்றலாம்; பழைய → புதிய உறுதிப்படுத்தல் card வரும். API: `POST /api/admin/update-old-quarters` (Admin மட்டும்; புதிய Quarter உருவாக்காது; ஏற்கனவே பதிவான Invoice-கள் மாறாது).

# Invoice பதிவு — டிக் செய்த இதழ்கள் மட்டும் (2026-10-05)
- Invoice பதிவில் ஒவ்வொரு Quarter-க்கும் Master-ல் அந்த Quarter டிக் செய்யப்பட்ட இதழ்கள் மட்டுமே இதழ் பட்டியலில் வரும் (சாம்பல் நிற உள்ளீடுகள் இனி இல்லை). Server பக்கத் தடை தொடரும்.

# வவுச்சர் உருவாக்கு — நிறுவனவாரிக் குழு (2026-10-05)
- `GET /api/vouchers/by-set`: ஒரே Vendor/Beneficiary Code (அதே Quarter + அதே Set No) உள்ள இதழ்கள் அடுத்தடுத்து வரும்; குழுவுக்குள் A→Z; குழு, அதன் A→Z முதல் இதழின் இடத்தில் வரும். குழு இல்லாத இதழ்கள் வழக்கம்போல் A→Z. குழு தொடக்கத்தில் 🏢 நிறுவனப் பெயர் தலைப்பு வரி காட்டப்படும்.
- குழு விதி, தொகை வழங்கல் / Bank Transaction-ல் பயன்படுத்தும் அதே `group_key_for` (Quarter + Code + Set No).

# இன்வாய்ஸ் பதிவு — வங்கி விவரச் சரிபார்ப்பு card (2026-10-08)
- "✅ இன்வாய்ஸ் பதிவு செய்" அழுத்தியதும் (எல்லாச் சரிபார்ப்புகளுக்கும் பிறகு) முதலில் ஒரு card வரும்: Vendor Name, Payee Name, Bank, Branch, Account Number, IFSC, Email — பெரிய எழுத்துகளில், திருத்த முடியாதபடி. பதிவில் இல்லாத புலம் சிவப்பில் "⚠️ பதிவு இல்லை" எனக் காட்டும்.
- "✅ ஆம், சரி" → இன்வாய்ஸ் வழக்கம்போல் பதிவாகும். "✖ ரத்து" / இன்வாய்ஸ் பதிவாகாது.
- "✏️ தவறு — Edit" → அதே card-ல் புலங்கள் திருத்தக்கூடியவையாக மாறும். "💾 Save" → Vendor Master-ல் சேமிப்பதுபோலவே `magazines` table-ல் (vendor_name, payee_name, bank_account_number, bank_name, bank_place, ifsc_code, email_id) சேமிக்கப்படும்; பின் மீண்டும் திருத்த முடியாத பார்வை வந்து, "ஆம், சரி" அழுத்திய பின்பே இன்வாய்ஸ் பதிவாகும்.
- புதிய API (admin அல்லாத பயனரும் பயன்படுத்தலாம்): `GET /api/vendor-bank?name=`, `POST /api/vendor-bank/update`. இது மேற்கண்ட 7 புலங்களை மட்டுமே மாற்றும் — Vendor Code மாறாது. IFSC வடிவம் (4 எழுத்து + 0 + 6), Email வடிவம் (பல முகவரிகள் காற்புள்ளியால்) சோதிக்கப்படும்.
- ஏற்கனவே பதிவான இன்வாய்ஸ்கள் / வவுச்சர்கள் மாறாது; Master மாறுவதால் இனி உருவாகும் PDF / Mail-ல் புதிய வங்கி விவரம் வரும்.

# Master — Excel மூலம் பல Quarter சேர்த்தல் (2026-10-25)
- Master Data → இதழ் Master-ல் புதிய card: "📊 Excel மூலம் பல Quarter-களுக்கு இதழ்கள் சேர்".
- Excel (Magazine Name, Periodicity, Total Subscription, Single Issue Rate, Discount, Single Issue After Discount) → Preview (DB மாறாது) → உறுதிப்படுத்தல் → ஒரே transaction-ல் சேமிப்பு.
- Master-ல் உள்ள இதழ்: தேர்ந்த Quarter-கள் (2026-2027-Q2..Q4) Excel விலையுடன் சேரும்; மொழி மாறாது; பதிவுள்ள 🔒 Quarter-கள் தொடப்படாது (தனி டிக் செய்தால் மட்டும் மாறும்).
- புதிய இதழ்: magazines-ல் சேர்ந்து, தேர்ந்த Quarter-களுக்கு மட்டும் பதிவாகும்.
- பெயர் ஒத்த இதழ்கள் (எழுத்து வேறுபாடு): "இணை / புதிதாகச் சேர் / தவிர்" என நீங்களே தேர்வு.
- API: `POST /api/admin/bulk-quarters/preview`, `POST /api/admin/bulk-quarters/apply` (Admin மட்டும்). requirements.txt-ல் `openpyxl==3.1.5` சேர்க்கப்பட்டது.

# Vendor / Bank Master — Vendor Code மூலம் விவரம் தானாக நிரப்பு (2026-10-25)
- "புதிய Vendor / Bank விவரம் சேர்" படிவத்தில் Vendor Code-ஐ உள்ளிட்டு வெளியே வந்தால் / Enter / "🔍 Code மூலம் நிரப்பு" அழுத்தினால், அதே Code உள்ள இதழின் Vendor பெயர், Payee, Bank A/C, IFSC, Bank பெயர், Place, Email நிரம்பும் (இதழ் பெயர் மாறாது).
- Vendor Code புலத்தில் ஏற்கனவே பதிவான Code-களின் பட்டியல் (Vendor பெயருடன்) dropdown ஆக வரும்.
- திருத்தும்போது / படிவத்தில் வேறு மதிப்பு இருந்தால் மாற்றும் முன் உறுதிப்படுத்தல். ஒரே Code-ல் வெவ்வேறு A/C இருந்தால் எச்சரிக்கை.
- Server மாற்றம் இல்லை (`/api/admin/vendors-list` தரவே பயன்பாடு).

# Vendor / Bank Master — இதழ் பெயர் தேர்வுப் பட்டியல் (2026-10-25)
- இதழ் பெயர் புலத்தில் 4 எழுத்துகள் தட்டச்சு செய்ததும் Master இதழ்கள் (பெயரின் எந்தப் பகுதியிலும் பொருந்தும்) பட்டியலாக வரும்; கிளிக் / ↑↓ + Enter மூலம் தேர்வு.
- தேர்ந்த இதழுக்கு ஏற்கனவே Vendor விவரம் இருந்தால் (மற்ற புலங்கள் காலியாக இருந்தால்) நிரம்பும்.
- சேமிக்கும்போது Master-ல் இல்லாத பெயர் எனில் எச்சரிக்கை + ஒத்த பெயர்கள் — தவறான இரட்டை இதழ் உருவாவதைத் தடுக்க.

# இன்வாய்ஸ் பதிவு — Requested Amount தானாக நிரப்பு (2026-10-25)
- "QTR Issues & தொகை சரிபார்ப்பு" card-ல் "✅ சரி, பதிவு செய்" அழுத்தியதும், மொத்த தொகை (Issue Price × Libraries × QTR Issues) Requested Amount-ல் தானாக நிரம்பும்; திருத்தலாம்.
- கையால் மாற்றிய தொகை / ஏற்கனவே சேமித்த Invoice-ன் தொகை மேலெழுதப்படாது; தானாக நிரம்பிய தொகை மட்டும் QTR Issues மாறினால் புதுப்பிக்கப்படும்; இதழ் மாறினால் தானாக நிரம்பிய பழைய தொகை நீங்கும்.

# Master — Excel மீண்டும் upload: மாற்றங்களைக் காட்டி வரி வாரியாகப் பதிவு (2026-10-25)
- Excel Preview-ல் ஏற்கனவே பதிவுள்ள Quarter-களுடன் ஒப்பிட்டு, மாறிய Price / Disc% / Issue Price / Libraries-ஐ "பழைய → புதிய" என்று சிவப்பில் காட்டும்.
- மாற்றம் உள்ள ஒவ்வொரு இதழுக்கும் "இந்த மாற்றத்தைப் பதிவு செய்" டிக் பெட்டி; டிக் செய்தவை மட்டும் புதுப்பிக்கப்படும் (பொதுவான "எல்லாவற்றையும் மாற்று" பெட்டி நீக்கப்பட்டது).
- Filter: அனைத்தும் / மாற்றம் உள்ளவை / ஒத்த பெயர் / புதியவை.
- API `bulk-quarters/apply`: ஒவ்வொரு item-க்கும் `overwriteQuarters` (அந்த இதழின் எந்த Quarter-களை மாற்றலாம்).

# Master — Financial Year View (Dashboard) (2026-10-26)
- Master Data → புதிய tile "📅 Financial Year View (Dashboard)": நிதியாண்டு வாரியாக (முழு ஆண்டு / தனித்தனி Q) பல Quarter-களைத் தேர்ந்து சேமிக்கலாம்; எல்லாப் பயனர்களுக்கும் பொருந்தும். தேர்வு DB-யில் (`app_settings`, key `visible_quarters`).
- `/api/quarters`: `quarters` / `addable` தேர்ந்த Quarter-களுக்குள் மட்டும்; `allQuarters` / `allAddable` / `restricted` / `visible` கூடுதலாக. `POST /api/admin/visible-quarters` ({quarters:[...]} அல்லது {all:true}).
- இன்வாய்ஸ் பதிவு, தொகை வழங்கல், வவுச்சர், Payment Advice, Bank Txn, Mail+PDF, Pending Reminder, அறிக்கைகள், தொகை வித்தியாசம், தொகை வழங்கப்பட்ட விவரம் — அனைத்திலும் தேர்ந்த Quarter-கள் மட்டும்; "அனைத்தும்" = தேர்ந்த Quarter-கள் மட்டும்; முகப்பு Pending ticker-உம் அதே.
- Master-ன் சொந்தக் கருவிகள் (இதழ் Master Quarter டிக், தவறான பதிவு நீக்கு, Payment Details export) முழுப் பட்டியலையே காட்டும்.
- தரவு எதுவும் நீக்கப்படாது — மறைக்கப்படுவது மட்டுமே.

# 🌐 மொழி தேர்வு — தமிழ் / English (2026-10-26)

## Implemented
- **இன்வாய்ஸ் பதிவு**: Quarter தேர்ந்தெடுத்த பின் "2. மொழி" தேர்வு (தமிழ் / English) கட்டாயம். மொழி தேர்ந்த பின்னரே இதழ் பட்டியல் திறக்கும்; அதில் அந்த மொழிக்குரிய இதழ்கள் மட்டும். Quarter மாற்றினால் மொழியை மீண்டும் தேர்ந்தெடுக்க வேண்டும்.
- **தொகை வழங்கல்**: அதே முறை — Quarter → மொழி → இதழ் (அந்த மொழிக்குரியவை மட்டும்). வேறு திரையிலிருந்து இதழை நேரடியாகத் திறந்தால் (Group popup போன்றவை) இதழின் மொழி தானாக அமையும்.
- **மற்ற அனைத்துத் திரைகள்** (அறிக்கைகள் 1–8 + CSV, Pending Invoice Reminder, Bank Account விவரங்கள், வவுச்சர் / Payment Advice / Payment Voucher, Bank Transaction, Mail + PDF, தொகை வித்தியாசம், தொகை வழங்கப்பட்ட விவரம்): தலைப்புக்குக் கீழே "🌐 மொழி: அனைத்தும் / தமிழ் / English". மொழி மாறியதும் திறந்துள்ள அறிக்கை / பட்டியல் தானாக மீண்டும் ஏற்றப்படும்; PDF / Excel / CSV பதிவிறக்கங்களிலும் அதே வடிகட்டி.
- Server: `?language=ta|en` (காலி = அனைத்தும்) — `/api/magazines`, `payments/by-quarter`, `payments/magazines-for-quarter`, `payments/paid`, `payments/transactions`, `reports/*` (quarter-summary, magazine-wise, payment-status, unpaid, voucher-register, email-status, amount-mismatch, financial-year), `reminders/pending-invoices`, `vouchers/*`, `payment-advice`, `payment-voucher` (+pdf), `mail/ready`, `export/sheet-csv`, `export/payments`, `admin/vendors-list`.

## Important
- இதழின் மொழி = Master-ல் உள்ள `LANGUAGE` புலம் ("Tamil"/"தமிழ்" → தமிழ்; "English"/"ஆங்கிலம்" → English). அது காலியாக இருந்தால் இதழ் பெயரில் தமிழ் எழுத்து இருந்தால் தமிழ், இல்லையெனில் English.
- Master Data திரைகள் மற்றும் முகப்புப் பக்க Pending ticker-ல் மொழி வடிகட்டி இல்லை (எப்போதும் அனைத்தும்).
- DB / schema மாற்றம் இல்லை; பதிவான தரவு எதுவும் மாறாது.
