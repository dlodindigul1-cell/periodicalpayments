-- =====================================================================
-- பருவ இதழ்கள் தொகை செலுத்துதல் — Neon Postgres Schema
-- GAS Spreadsheet-ல் இருந்த "DATA-2026-27", "2026-27 PAYMENTS",
-- "VOUCHERS", "VENDOR BANK ACCOUNT", "QUARTER-1..4" sheets-ஐ இங்கே
-- table-களாக மாற்றியுள்ளோம்.
-- =====================================================================

-- 1) இதழ் master data (பழைய "DATA-2026-27" sheet)
CREATE TABLE IF NOT EXISTS magazines (
    id                 SERIAL PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    periodicity        TEXT,
    issue_price        NUMERIC(10,2) NOT NULL DEFAULT 0,
    no_of_libraries    INTEGER NOT NULL DEFAULT 0,
    effective_quarters TEXT,           -- e.g. "2025-2026-Q4,2026-2027-Q1"
    tnpfts_code        TEXT,           -- "VENDOR BANK ACCOUNT" sheet-லிருந்து
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2) Despatch / Non-supply தரவு, quarter வாரியாக (பழைய "QUARTER-1..4" sheets)
CREATE TABLE IF NOT EXISTS despatch_nonsupply (
    id          SERIAL PRIMARY KEY,
    quarter     TEXT NOT NULL,          -- "2026-2027-Q1"
    magazine    TEXT NOT NULL,
    non_supply  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (quarter, magazine)
);

-- 3) முதன்மை Payments table (பழைய "2026-27 PAYMENTS" sheet)
CREATE TABLE IF NOT EXISTS payments (
    id              SERIAL PRIMARY KEY,
    sno             INTEGER,                 -- A
    magazine        TEXT NOT NULL,           -- B
    issue_price     NUMERIC(10,2) DEFAULT 0, -- C
    subscriptions   INTEGER DEFAULT 0,       -- D
    qtr_issues      INTEGER DEFAULT 0,       -- E
    total_issues    INTEGER DEFAULT 0,       -- F
    actual_cost     NUMERIC(12,2) DEFAULT 0, -- G
    non_supply      INTEGER DEFAULT 0,       -- H
    deduction       NUMERIC(12,2) DEFAULT 0, -- I = C × H
    net_payable     NUMERIC(12,2) DEFAULT 0, -- J = G − I
    invoice_no      TEXT,                    -- K
    invoice_date    DATE,                    -- L
    requested_amt   NUMERIC(12,2) DEFAULT 0, -- M
    paid_amt        NUMERIC(12,2) DEFAULT 0, -- N (Amount Now Paid)
    payment_date    DATE,                    -- O
    transaction_no  TEXT,                    -- P
    remarks         TEXT,                    -- Q
    bill_set_no     TEXT,                    -- R
    mail_sent       BOOLEAN DEFAULT FALSE,   -- S
    pdf_url         TEXT,                    -- T
    quarter         TEXT NOT NULL,           -- U
    voucher_no      TEXT,                    -- V
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (magazine, quarter)
);

-- 4) Vouchers table (பழைய "VOUCHERS" sheet)
CREATE TABLE IF NOT EXISTS vouchers (
    id             SERIAL PRIMARY KEY,
    payment_sno    INTEGER,
    magazine       TEXT,
    tnpfts_code    TEXT,
    invoice_no     TEXT,
    invoice_date   DATE,
    requested_amt  NUMERIC(12,2),
    deduction      NUMERIC(12,2),
    amount_paid    NUMERIC(12,2),
    quarter        TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_quarter  ON payments (quarter);
CREATE INDEX IF NOT EXISTS idx_payments_magazine ON payments (magazine);
CREATE INDEX IF NOT EXISTS idx_despatch_quarter  ON despatch_nonsupply (quarter);

-- =====================================================================
-- Vendor / Bank Account விவரங்கள் (2026-09-25) — பழைய "VENDOR BANK ACCOUNT" sheet
-- tnpfts_code ஏற்கனவே இருந்தது (Vendor Code-ஆக பயன்படுத்தப்படுகிறது); மீதமுள்ள
-- vendor/bank columns இங்கே சேர்க்கப்படுகின்றன. ஒரு இதழுக்கு ஒரு Vendor என்பதால்
-- இவை magazines table-லேயே வைக்கப்படுகின்றன.
-- =====================================================================
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS vendor_name          TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS bank_account_number  TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS bank_name            TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS bank_place           TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS ifsc_code            TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS payee_name           TEXT;
ALTER TABLE magazines ADD COLUMN IF NOT EXISTS email_id             TEXT;

-- =====================================================================
-- உள் நுழைவு பயனர்கள் (2026-10-01) — Basic Auth popup-க்குப் பதிலாக Login card.
-- app தொடங்கும்போது தானாக உருவாகும்; இங்கே குறிப்புக்காக மட்டும்.
-- role: 'admin' (Master உட்பட முழு அணுகல்) | 'section' (Master தவிர)
-- Password hash செய்யப்பட்டு சேமிக்கப்படும் (Master → பயனர் கணக்கு-ல் மாற்றலாம்).
-- =====================================================================
CREATE TABLE IF NOT EXISTS app_users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'section',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_app_users_username ON app_users (LOWER(username));

-- =====================================================================
-- Part (ஒரே Quarter-க்கு பல இன்வாய்ஸ்) — படி 1
-- app தொடங்கும்போது ensure_part_schema() இதைத் தானாகச் செய்யும் (மீண்டும் இயக்கினாலும் பாதுகாப்பு).
--   part   : 1,2,3…  (பழைய பதிவுகள் எல்லாம் 1)
--   months : NULL = Quarter முழுமை; '1,2' = Quarter-ன் 1-வது, 2-வது மாதம்
--   UNIQUE (magazine, quarter)  ->  UNIQUE (magazine, quarter, part)
-- =====================================================================
ALTER TABLE payments ADD COLUMN IF NOT EXISTS part   INTEGER NOT NULL DEFAULT 1;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS months TEXT;
-- (பழைய constraint பெயர் DB-க்கு DB மாறலாம்; app அதை தானாகக் கண்டறிந்து நீக்கும்)
-- ALTER TABLE payments DROP CONSTRAINT payments_magazine_quarter_key;
CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_mag_qtr_part ON payments (magazine, quarter, part);

-- =====================================================================
-- படி 2–4: vouchers.part (எந்த Part-க்கான Voucher வரிசை என்பதை அறிய)
-- app தொடங்கும்போது ensure_part_schema() இதையும் தானாகச் செய்யும்; பழையவை எல்லாம் Part 1.
-- Voucher Register / நீக்கும் திரைகள் இனி (இதழ் + Quarter + Part) வாரியாகவே இயங்கும்.
-- Google Sheet CSV: A:V மாறவில்லை; W = PART, X = MONTHS சேர்ந்துள்ளன.
--   ஒத்திசைவு key = NAME OF THE MAGAZINE (B) + Quarter details (U) + PART (W)
-- =====================================================================
ALTER TABLE vouchers ADD COLUMN IF NOT EXISTS part INTEGER NOT NULL DEFAULT 1;
