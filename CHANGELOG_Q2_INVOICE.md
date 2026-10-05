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
