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
