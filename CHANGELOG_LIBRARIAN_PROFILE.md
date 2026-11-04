# நூலகர் விவரங்கள் + மெயிலில் English நூலகப் பெயர் (2026-11-04)

## மாறிய கோப்புகள்
- `librarian_module.py`, `missing_module.py` (app.py, schema.sql மாறவில்லை)

## 1) நூலகர் Dashboard — LIBRARIAN DETAILS
- "ஆணையிடப்பட்ட இதழ்கள்" card-ல்: LIBRARIAN NAME, MOBILE NUMBER, ADDRESS (ஒரே ஒரு தொகுப்பு, ஆங்கிலம்). ✏️ திருத்து → 💾 சேமி.
- சேமிக்கும்போது `lib_libraries`-ன் தமிழ் + English புலங்கள் (librarian_name/_en, address/_en) இரண்டிலும் ஒரே மதிப்பு பதிவாகும்; phone ஒரே புலம்.
- பெயர் / முகவரியில் தமிழ் எழுத்து வந்தவுடன் "தயவு செய்து ஆங்கிலத்தில் உள்ளிடவும் (Please enter in English)" எச்சரிக்கை; தமிழ் இருந்தால் சேமிக்க முடியாது (server-லும் தடுக்கும்).
- மூன்றும் கட்டாயம்; தொலைபேசியில் எண்கள் + - , / ( ) மட்டும், குறைந்தது 6 இலக்கம்.
- API: `GET /api/librarian/profile`, `POST /api/librarian/profile-save` (session-ன் நூலகம் மட்டும்).

## 2) இதழ்களுக்குச் செல்லும் மெயில் / கடிதம் PDF / முன்னோட்டம்
- இதழ் தமிழ் ஆனாலும் நூலகப் பெயர், நூலகர், முகவரி — English புலம் (எ.கா. Dindigul District Central Library). English புலம் காலியாக இருந்தால் மட்டும் தமிழ் பெயர் வரும்.
- Admin நூலகப் பட்டியல் பதிவிறக்கம் (தமிழ்/English தேர்வு) மாறவில்லை.
