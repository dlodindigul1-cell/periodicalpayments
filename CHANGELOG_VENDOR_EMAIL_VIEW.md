# Vendor Bank Master — Email மட்டும் தனிப் பட்டியல் + திருத்தம் (2026-10-30)

மாறிய கோப்புகள்: `missing_module.py`, `templates/index.html` (app.py, database மாறவில்லை)

- Master Data → Vendor Bank Master பக்கத்தில் புதிய அட்டை "📧 Email ID மட்டும் — தனிப் பட்டியல் & திருத்தம்":
  S.No | இதழ் பெயர் | Vendor Code | Email ID (திருத்தும் பெட்டி) | 💾
  - தேடல் (இதழ் / Vendor Code / Email), "Email இல்லாதவை மட்டும்" வடிகட்டி, மொத்தம் / Email இல்லாதவை எண்ணிக்கை
  - Enter அழுத்தினாலும் சேமிக்கும்; பல Email காற்புள்ளியால்; காலியாக்கிச் சேமித்தால் Email நீங்கும் (உறுதிக் கேள்வியுடன்)
  - ஒரே Vendor Code இதழ்கள் அருகருகே வரிசைப்படும்
- Email மட்டுமே சேமிக்கப்படும் (`magazines.email_id`) — Bank / IFSC / Vendor பெயர் தொடப்படாது.
- குழு விதி: சேமிக்கும்போது அதே Vendor Code குழுவின் Email காலியான இதழ்கள் நிரம்பும்; "குழு முழுவதற்கும் மாற்று" டிக் செய்தால் குழுவின் எல்லா இதழ்களும் (ஏற்கனவே உள்ள Email உட்பட) மாறும்.
- புதிய API: `GET /api/admin/vendor-emails`, `POST /api/admin/vendor-emails/save` (Admin மட்டும்).
- "② Email இல்லாத இதழ்கள்" தாவலும் இதே உதவிச் செயல்பாட்டைப் பயன்படுத்துகிறது.
