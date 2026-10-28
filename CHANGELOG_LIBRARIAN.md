# நூலகர் இதழ் வருகைப் பதிவு (2026-10-28)

## புதிய / மாறிய கோப்புகள்
- புதிது: `librarian_module.py` (முழு வசதியும் இதில்), `data/libraries_seed.csv` (153 நூலகங்கள்), `CHANGELOG_LIBRARIAN.md`
- மாற்றம்: `app.py` (login dropdown, நூலகர் கட்டுப்பாடு, Blueprint இணைப்பு), `templates/index.html` (Admin மெனுவில் "நூலகர் பதிவு" அட்டை), `requirements.txt` (xlrd)

## நூலகர் ஓட்டம்
1. Login → பயனர் dropdown: admin / Section / **Librarian** (Librarian தேர்ந்தால் Password பெட்டி மறையும்)
2. `/librarian/select` → நூலக வகை → நூலகத்தின் பெயர்
3. `/librarian/dashboard` → "இதழ்கள் வருகை பதிவு செய்யுங்கள்": இந்த வாரம் (திங்கள்–ஞாயிறு, IST) வர வேண்டிய இதழ்கள் மட்டும்; ஒவ்வொன்றுக்கும் Received / Not received; மாற்றிச் சேமிக்கலாம்
4. Librarian role-க்கு `/librarian/*` தவிர வேறு எந்தப் பக்கமும் API-யும் திறக்காது (global_auth)

## Admin
- முகப்பு → "10 நூலகர் பதிவு" → `/admin/librarian`
  - ① Quarter Excel ஏற்றுதல் (A: code, B: Magazine, C: Periodicity, D: Expected Date). Quarter தேதியிலிருந்து தானாக (Oct–Dec = Q3, Jan–Mar = Q4). மீண்டும் ஏற்றினாலும் நூலகர் பதிவுகள் அழியாது. தவறான Quarter-ஐ நீக்கும் பொத்தானும் உண்டு
  - நூலகப் பட்டியல் (name, category, code) — முதல் முறை தானாக `data/libraries_seed.csv`-லிருந்து; பின்னர் இதே பக்கத்தில் புதுப்பிக்கலாம்
  - ② பதிவு செய்தவை / பதிவு செய்யாதவை (வகை, தேதி வடிகட்டி; நூலகம் வாரியாக; வரிசையைக் கிளிக் செய்தால் இதழ்கள்; Excel பதிவிறக்கம்)
  - "பதிவு செய்யாதவை" = Expected Date இன்று அல்லது அதற்கு முன் இருந்தும் பதிவு இல்லாதவை

## Database (தானாக உருவாகும்)
`lib_libraries`, `lib_schedule`, `lib_receipts` — மற்ற table-களைத் தொடாது.
'Librarian' என்ற பெயரை Master → பயனர் கணக்கில் வேறு பயனருக்கு வைக்க முடியாது.
