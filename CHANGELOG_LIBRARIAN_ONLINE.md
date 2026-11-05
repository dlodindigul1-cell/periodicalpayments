# நூலகர் Login Online பெட்டி (2026-11-05)

மாறிய கோப்புகள்: `librarian_module.py`, `app.py` (logout-இல் 4 வரி), `templates/index.html`

- புதிய table `lib_presence` (தானாக உருவாகும்; பழைய தரவு மாறாது)
- நூலகர் நூலகம் தேர்ந்ததும் பதிவு; dashboard திறந்திருந்தால் 60 வினாடிக்கு ஒரு heartbeat (`/api/librarian/ping`)
- 5 நிமிடத்துக்குள் செயல்பட்டவர்கள் மட்டுமே "login-இல்" எனக் காட்டப்படும் (`PRESENCE_MINUTES`)
- Logout / "நூலகம் மாற்று" செய்தால் உடனே பட்டியலிலிருந்து நீங்கும்
- Admin API: `/api/admin/librarian-online`
- Admin முகப்பில் "10 நூலகர் பதிவு" card அருகே தானாக ஓடும் பெட்டி; 30 வினாடிக்கு ஒருமுறை புதுப்பிக்கும்
