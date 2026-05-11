-- =============================================================================
-- Neural Ledger — DuckDB Schema
-- Medallion Architecture: Bronze → Silver → Gold
-- Every design decision here is intentional. Do not modify without understanding
-- the impact on the Phase 2 RAG pipeline and the Gold schema contract.
-- =============================================================================


-- =============================================================================
-- BRONZE LAYER
-- Raw ingestion. Append-only. Never modified. Never deleted.
-- Preserves the exact data the user provided, in its original form.
-- Source of truth for audit and lineage.
-- =============================================================================

CREATE TABLE IF NOT EXISTS bronze_transactions (

    -- Identity
    bronze_id           UUID        DEFAULT gen_random_uuid() PRIMARY KEY,

    -- Source tracking — critical for adaptive normalisation in Silver
    -- source_type drives which parser was used
    source_type         VARCHAR(20) NOT NULL CHECK (source_type IN (
                            'xlsx', 'csv', 'pdf', 'api_quickbooks',
                            'api_xero', 'api_tally', 'api_odoo', 'json', 'manual'
                        )),
    -- source_software tells the normaliser which column mapping strategy to apply
    source_software     VARCHAR(50) NOT NULL CHECK (source_software IN (
                            'manual_excel', 'ledgermax', 'quickbooks_desktop',
                            'quickbooks_online', 'tally', 'xero', 'odoo',
                            'erpnext', 'sage', 'moneypex', 'bank_pdf_hbl',
                            'bank_pdf_ubl', 'bank_pdf_alfalah', 'bank_pdf_meezan',
                            'bank_pdf_nbp', 'bank_pdf_generic', 'unknown'
                        )),
    source_file         VARCHAR(500),           -- original filename
    source_file_hash    VARCHAR(64),            -- SHA256 of file — detects re-uploads
    source_row_number   INTEGER,                -- row number in original file

    -- The original row preserved as JSON — column names exactly as the user had them
    -- e.g. {"Taareekh": "01/01/2024", "Raqam": "5000", "Vendor Name (City)": "Shell Clifton"}
    raw_content         JSON        NOT NULL,

    -- Original column headers preserved for schema learning
    raw_headers         JSON,                   -- array of original column names

    -- Ingestion metadata
    ingested_at         TIMESTAMP   DEFAULT NOW(),
    ingestion_batch_id  UUID,                   -- groups all rows from one file upload

    -- Status
    processing_status   VARCHAR(20) DEFAULT 'pending' CHECK (processing_status IN (
                            'pending', 'processing', 'normalised', 'quarantined', 'error'
                        )),
    error_message       VARCHAR(1000)           -- populated if processing_status = 'error'
);


-- =============================================================================
-- BRONZE: Schema mapping memory
-- Remembers how columns were mapped for each source_software.
-- Once the accountant's "Raqam" column is mapped to "amount", it's remembered
-- for all future uploads from the same software.
-- =============================================================================

CREATE TABLE IF NOT EXISTS bronze_schema_mappings (
    mapping_id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    source_software     VARCHAR(50) NOT NULL,
    original_column     VARCHAR(200) NOT NULL,  -- "Raqam", "Taareekh", "Dr Amount"
    standard_field      VARCHAR(50) NOT NULL,   -- "amount", "transaction_date", "amount_debit"
    confidence          FLOAT       DEFAULT 1.0, -- 1.0 = user confirmed, < 1.0 = auto-detected
    confirmed_by_user   BOOLEAN     DEFAULT FALSE,
    times_seen          INTEGER     DEFAULT 1,
    created_at          TIMESTAMP   DEFAULT NOW(),
    updated_at          TIMESTAMP   DEFAULT NOW(),
    UNIQUE (source_software, original_column)
);


-- =============================================================================
-- SILVER LAYER
-- Normalised, deduplicated, PII-masked, structurally clean.
-- Every row has a consistent schema regardless of source.
-- Still contains business-sensitive data — privacy firewall has run.
-- =============================================================================

CREATE TABLE IF NOT EXISTS silver_transactions (

    -- Identity & lineage
    silver_id           UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    bronze_id           UUID        NOT NULL,   -- FK to bronze_transactions
    source_software     VARCHAR(50) NOT NULL,   -- inherited from bronze
    source_type         VARCHAR(20) NOT NULL,

    -- Core financial fields — standardised
    transaction_date    DATE        NOT NULL,
    year_month          VARCHAR(7)  NOT NULL,   -- "2024-01" — used for time-based queries
    fiscal_year         VARCHAR(9),             -- "2024-2025" — Pakistani fiscal year Jul-Jun

    -- Amount handling — supports Dr/Cr split or net amount
    amount_debit        DECIMAL(18,2) DEFAULT 0,
    amount_credit       DECIMAL(18,2) DEFAULT 0,
    net_amount          DECIMAL(18,2) GENERATED ALWAYS AS (amount_credit - amount_debit) VIRTUAL,
    currency            VARCHAR(3)  DEFAULT 'PKR',

    -- Descriptive fields — raw from source, before category inference
    vendor_raw          VARCHAR(500),           -- original vendor/payee name
    description_raw     VARCHAR(1000),          -- original memo/narration
    account_code        VARCHAR(100),           -- Chart of Accounts code from source
    tx_type             VARCHAR(100),           -- Payment / Receipt / Journal / Invoice

    -- Normalisation quality
    normalisation_confidence FLOAT DEFAULT 1.0, -- how confident the mapper was
    date_parse_method   VARCHAR(50),            -- which date format was detected
    amount_parse_method VARCHAR(50),            -- net / split_drCr / single_column

    -- Privacy
    pii_detected        BOOLEAN     DEFAULT FALSE,
    pii_masked          BOOLEAN     DEFAULT FALSE,
    pii_fields_masked   JSON,                   -- list of which fields were masked

    -- Deduplication
    duplicate_of        UUID,                   -- points to surviving silver_id if duplicate
    is_duplicate        BOOLEAN     DEFAULT FALSE,
    dedup_confidence    FLOAT,                  -- how confident we are it's a duplicate

    -- Language detection — drives embedding text strategy
    language_detected   VARCHAR(10) DEFAULT 'en' CHECK (language_detected IN ('en', 'ur', 'mixed')),

    -- Timestamps
    silver_created_at   TIMESTAMP   DEFAULT NOW()
);


-- =============================================================================
-- SILVER: Quarantine
-- Rows that failed quality gate (quality_score < 0.7) or have critical errors.
-- Never promoted to Gold. Shown to user for manual review.
-- =============================================================================

CREATE TABLE IF NOT EXISTS silver_quarantine (
    quarantine_id       UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    bronze_id           UUID        NOT NULL,
    quarantine_reason   VARCHAR(50) NOT NULL CHECK (quarantine_reason IN (
                            'quality_too_low', 'date_parse_failed', 'amount_parse_failed',
                            'missing_required_field', 'suspected_duplicate',
                            'pii_unresolvable', 'encoding_error'
                        )),
    quality_score       FLOAT,
    raw_content         JSON,                   -- original row for user to inspect
    source_software     VARCHAR(50),
    source_file         VARCHAR(500),
    quarantined_at      TIMESTAMP   DEFAULT NOW(),
    reviewed_by_user    BOOLEAN     DEFAULT FALSE,
    user_action         VARCHAR(20) CHECK (user_action IN ('approve', 'reject', 'edit', NULL)),
    reviewed_at         TIMESTAMP
);


-- =============================================================================
-- GOLD LAYER
-- The Phase 2 contract. AI-ready, privacy-safe, metadata-rich.
-- Phase 2 reads from here. Phase 1 writes here. Never cross this boundary.
-- quality_score >= 0.7 required for entry.
-- =============================================================================

CREATE TABLE IF NOT EXISTS gold_transactions (

    -- Identity & full lineage chain
    transaction_id      UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    silver_id           UUID        NOT NULL,   -- FK to silver_transactions
    bronze_id           UUID        NOT NULL,   -- FK to bronze_transactions (for full lineage)
    source_software     VARCHAR(50) NOT NULL,
    source_type         VARCHAR(20) NOT NULL,

    -- Core financial fields — clean and definitive
    transaction_date    DATE        NOT NULL,
    year_month          VARCHAR(7)  NOT NULL,   -- "2024-01" — Qdrant partition key
    fiscal_year         VARCHAR(9),             -- "2024-2025"
    amount_debit        DECIMAL(18,2) DEFAULT 0,
    amount_credit       DECIMAL(18,2) DEFAULT 0,
    net_amount          DECIMAL(18,2) GENERATED ALWAYS AS (amount_credit - amount_debit) VIRTUAL,
    currency            VARCHAR(3)  DEFAULT 'PKR',

    -- Vendor & description — cleaned
    vendor_raw          VARCHAR(500),
    vendor_normalised   VARCHAR(200),           -- "shell_clifton" → normalised for grouping
    description_clean   VARCHAR(1000),

    -- AI Category inference
    category            VARCHAR(100),           -- "Fuel & Transport"
    subcategory         VARCHAR(100),           -- "Petrol"
    category_confidence FLOAT,                  -- 0.0 to 1.0
    category_method     VARCHAR(20) CHECK (category_method IN (
                            'rule_based', 'embedding', 'tx_type_map',
                            'account_code_map', 'user_confirmed'
                        )),

    -- FBR compliance mapping — critical for Pakistani accountants
    fbr_category        VARCHAR(100),           -- FBR tax head
    fbr_tax_applicable  BOOLEAN     DEFAULT FALSE,
    withholding_tax_rate FLOAT,                 -- applicable WHT rate if any

    -- Account classification
    account_code        VARCHAR(100),
    tx_type             VARCHAR(100),
    debit_account       VARCHAR(100),           -- double-entry accounting
    credit_account      VARCHAR(100),

    -- The most important field for Phase 2 RAG
    -- Pre-built natural language sentence used for embedding
    -- e.g. "On 15 January 2024, PKR 4,500 was paid to Shell Clifton — Fuel & Transport"
    embedding_text      TEXT        NOT NULL,

    -- Quality & data governance
    quality_score       FLOAT       NOT NULL CHECK (quality_score >= 0.7),
    gold_version        INTEGER     DEFAULT 1,  -- increments when re-processed
    pii_masked          BOOLEAN     DEFAULT TRUE,
    language_detected   VARCHAR(10) DEFAULT 'en',

    -- Phase 2 indexing status
    qdrant_indexed      BOOLEAN     DEFAULT FALSE,
    qdrant_indexed_at   TIMESTAMP,
    embedding_model     VARCHAR(100),           -- which model was used to embed

    -- Timestamps
    gold_created_at     TIMESTAMP   DEFAULT NOW(),
    gold_updated_at     TIMESTAMP   DEFAULT NOW()
);


-- =============================================================================
-- GOLD: Period aggregates
-- Pre-computed daily/weekly/monthly summaries.
-- Phase 2 uses these for trend queries and comparisons — much faster than
-- aggregating raw transactions every time.
-- =============================================================================

CREATE TABLE IF NOT EXISTS gold_period_summaries (
    summary_id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    period_key          VARCHAR(20) NOT NULL,   -- "2024-01" / "2024-W03" / "2024-01-15"
    period_type         VARCHAR(10) NOT NULL CHECK (period_type IN ('daily', 'weekly', 'monthly')),
    fiscal_year         VARCHAR(9),

    -- Aggregated financials
    total_debits        DECIMAL(18,2) DEFAULT 0,
    total_credits       DECIMAL(18,2) DEFAULT 0,
    net_cashflow        DECIMAL(18,2) DEFAULT 0,
    transaction_count   INTEGER     DEFAULT 0,

    -- Category breakdown (JSON for flexibility)
    category_breakdown  JSON,   -- {"Fuel & Transport": 45000, "Utilities": 12000}
    top_vendors         JSON,   -- [{"vendor": "Shell", "amount": 45000}]

    -- Anomaly signals
    anomaly_flag        BOOLEAN     DEFAULT FALSE,
    anomaly_reason      VARCHAR(200),
    vs_prior_period_pct FLOAT,                  -- % change vs same period prior

    -- Qdrant indexing
    qdrant_indexed      BOOLEAN     DEFAULT FALSE,
    summary_text        TEXT,                   -- NL sentence for embedding

    computed_at         TIMESTAMP   DEFAULT NOW(),
    UNIQUE (period_key, period_type)
);


-- =============================================================================
-- AUDIT LOG
-- Immutable record of every pipeline operation.
-- Essential for debugging, compliance, and user trust.
-- =============================================================================

CREATE TABLE IF NOT EXISTS pipeline_audit_log (
    log_id              UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
    operation           VARCHAR(50) NOT NULL,   -- "bronze_insert", "silver_normalise", "gold_promote"
    source_id           UUID,                   -- bronze_id / silver_id / gold_id
    source_layer        VARCHAR(10) CHECK (source_layer IN ('bronze', 'silver', 'gold', 'bridge')),
    status              VARCHAR(20) NOT NULL CHECK (status IN ('success', 'failure', 'warning')),
    message             VARCHAR(1000),
    rows_affected       INTEGER,
    duration_ms         INTEGER,
    logged_at           TIMESTAMP   DEFAULT NOW()
);


-- =============================================================================
-- INDEXES
-- Chosen specifically for the query patterns Phase 2 will use.
-- Do not add indexes blindly — DuckDB is columnar and manages many internally.
-- =============================================================================

-- Bronze: fast lookup by batch and status for pipeline processing
CREATE INDEX IF NOT EXISTS idx_bronze_status      ON bronze_transactions(processing_status);
CREATE INDEX IF NOT EXISTS idx_bronze_batch       ON bronze_transactions(ingestion_batch_id);
CREATE INDEX IF NOT EXISTS idx_bronze_file_hash   ON bronze_transactions(source_file_hash);

-- Silver: year_month is the primary query dimension for all financial queries
CREATE INDEX IF NOT EXISTS idx_silver_year_month  ON silver_transactions(year_month);
CREATE INDEX IF NOT EXISTS idx_silver_bronze_id   ON silver_transactions(bronze_id);
CREATE INDEX IF NOT EXISTS idx_silver_is_duplicate ON silver_transactions(is_duplicate);

-- Gold: the three most common Phase 2 access patterns
CREATE INDEX IF NOT EXISTS idx_gold_year_month    ON gold_transactions(year_month);
CREATE INDEX IF NOT EXISTS idx_gold_category      ON gold_transactions(category);
CREATE INDEX IF NOT EXISTS idx_gold_qdrant_indexed ON gold_transactions(qdrant_indexed);
CREATE INDEX IF NOT EXISTS idx_gold_source        ON gold_transactions(source_software);
CREATE INDEX IF NOT EXISTS idx_gold_silver_id     ON gold_transactions(silver_id);

-- Period summaries: Phase 2 retrieves these by period type and key constantly
CREATE INDEX IF NOT EXISTS idx_summary_period     ON gold_period_summaries(period_type, period_key);
CREATE INDEX IF NOT EXISTS idx_summary_qdrant ON gold_period_summaries(qdrant_indexed);