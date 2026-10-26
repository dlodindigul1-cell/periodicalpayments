# Periodicity → QTR Issues இயல்பு மதிப்பு (2026-10-26)

- Fortnightly = 6 (சரிபார்க்கப்பட்டது); Twice Weekly = 12 (புதியது); Bimonthly 5 → 2
- பகுதி மாத இன்வாய்ஸ்: இயல்பு × தேர்ந்த மாதங்கள் ÷ 3 (Fortnightly: 1 மாதம்=2, 2 மாதம்=4; Twice Weekly: 4 / 8)
- Periodicity பெயர் பொருத்தம் இனி எழுத்து வேறுபாடு, இடைவெளி, புள்ளி, '-' பாராது
  (fortnightly, FORTNIGHTLY, Fort nightly, Fortnightly. எல்லாம் 6)
- app.py: MONTH_TICK_PERIODICITIES-ல் Twice Weekly சேர்ப்பு (மாத வாரிப் பிரிப்பு வேலை செய்ய)
- templates/index.html: QTR_DEFAULTS + _periodicityKey
