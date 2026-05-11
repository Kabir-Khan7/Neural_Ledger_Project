# Neural Ledger — Step 1: Database Initialization

## Overview

This is the foundation of the entire Neural Ledger system. Before any file can be ingested, any AI query answered, or any report generated — the database layer must exist and be correct.

Step 1 builds the **Medallion Architecture** using DuckDB: a local, embedded, zero-install analytical database that runs entirely inside the application process. No separate database server. No PostgreSQL. No configuration required from the user.

---

## What Was Built

### Files Created

```
neural_ledger/
├── src/
│   └── database/
│       ├── __init__.py        # Module marker
│       ├── schema.sql         # All table definitions
│       └── init_db.py         # DatabaseManager class
├── config/
│   └── settings.py            # All environment-based configuration
├── tests/
│   └── phase1/
│       └── test_db_init.py    # 33 tests — all passing
├── init_database.py           # One-command DB initialisation script
├── .env.example               # Environment variable template
└── storage/
    └── neural_ledger.db       # Created on first run (DuckDB file)
```

---

## Architecture — Medallion Design

The database follows the **Medallion Architecture** pattern with three progressive data quality tiers. Each tier has a specific purpose and strict rules about what can enter and leave it.

```
Raw Files (Excel, CSV, PDF)
         ↓
   BRONZE LAYER          Raw data preserved exactly as received
         ↓
   SILVER LAYER          Cleaned, normalised, PII-masked
         ↓
   GOLD LAYER            AI-ready, quality-gated, Phase 2 ready
         ↓
   Phase 2 (RAG)         Reads Gold only — never touches Bronze or Silver
```

---

## Database Tables

### Bronze Layer

#### `bronze_transactions`
The raw ingestion table. **Append-only. Never modified. Never deleted.**

Every row from every source file lands here exactly as received. Column names from the user's original file are preserved inside a JSON blob — so if a Pakistani accountant's Excel has `Taareekh` and `Raqam` instead of `Date` and `Amount`, those exact names are stored.

| Key Field | Purpose |
|---|---|
| `bronze_id` | UUID primary key, auto-generated |
| `source_type` | What file type was ingested (`xlsx`, `csv`, `pdf`, `api_quickbooks`, etc.) |
| `source_software` | Which accounting software produced the file (`manual_excel`, `tally`, `ledgermax`, `quickbooks_desktop`, etc.) |
| `source_file_hash` | SHA256 of the original file — detects re-uploads and prevents duplicate ingestion |
| `raw_content` | The original row as a JSON blob — preserves original column names exactly |
| `raw_headers` | Array of all original column headers from the source file |
| `ingestion_batch_id` | Groups all rows from a single file upload together |
| `processing_status` | Tracks pipeline progress: `pending → processing → normalised / quarantined / error` |

**Why `source_software` matters:** A Tally export and a QuickBooks export look completely different. The `source_software` field tells the normalization engine in Silver which column-mapping strategy to apply. Without it, every file would need manual configuration.

#### `bronze_schema_mappings`
The adaptive learning table. Remembers how column names from each software map to the Neural Ledger standard schema.

Once the system learns that `Raqam` means `amount` for `manual_excel` files, it never asks again. If a user corrects a mapping, `confirmed_by_user = TRUE` and `confidence = 1.0` locks it permanently.

---

### Silver Layer

#### `silver_transactions`
Normalised, deduplicated, PII-masked transactions with a consistent schema regardless of source.

Every row here has the same structure whether it came from a handwritten Excel file, a Tally export, or a QuickBooks API. The original messiness has been resolved.

| Key Field | Purpose |
|---|---|
| `bronze_id` | Lineage link back to the original raw row |
| `transaction_date` | Standardised ISO 8601 date regardless of source format |
| `year_month` | `"2024-01"` format — the primary partition key for all time-based queries |
| `fiscal_year` | `"2024-2025"` — Pakistani fiscal year (July–June) |
| `amount_debit` / `amount_credit` | Split Dr/Cr handling — many Pakistani Excel files use separate debit and credit columns |
| `net_amount` | Virtual computed column: `credit - debit`. Always accurate, never stored redundantly |
| `language_detected` | `en`, `ur`, or `mixed` — drives the embedding text construction strategy in Phase 2 |
| `is_duplicate` | Flagged if this row is a duplicate of another (cross-source deduplication) |
| `pii_masked` | Whether the privacy firewall has run on this row |

#### `silver_quarantine`
Rows that failed the quality gate or had unresolvable parsing errors. Never promoted to Gold. Surfaced to the user for manual review.

Quarantine reasons: `quality_too_low`, `date_parse_failed`, `amount_parse_failed`, `missing_required_field`, `suspected_duplicate`, `pii_unresolvable`, `encoding_error`

---

### Gold Layer

#### `gold_transactions`
**The Phase 2 contract.** AI-ready, privacy-safe, fully enriched transactions. This is the only table Phase 2 reads from.

**Entry requirement:** `quality_score >= 0.7` — enforced by a database CHECK constraint. Rows below this threshold go to `silver_quarantine` instead.

| Key Field | Purpose |
|---|---|
| `transaction_id` | UUID primary key |
| `silver_id` + `bronze_id` | Full lineage chain — every answer the AI gives can be traced to its source |
| `embedding_text` | **Most important field for Phase 2.** A pre-built natural language sentence: `"On 15 Jan 2024, PKR 4,500 was paid to Shell Clifton — Fuel & Transport"`. This is what gets embedded into the vector store. |
| `category` / `subcategory` | AI-inferred transaction category (e.g. `"Fuel & Transport"` / `"Petrol"`) |
| `category_confidence` | 0.0–1.0 confidence score for the category assignment |
| `fbr_category` | FBR tax head mapping — enables direct Annexure-C generation |
| `fbr_tax_applicable` | Whether withholding tax applies to this transaction |
| `quality_score` | Data quality score ≥ 0.7 (enforced by constraint) |
| `qdrant_indexed` | Whether Phase 2 has indexed this row into the Qdrant vector store |
| `gold_version` | Increments when a row is re-processed — enables audit of reprocessing |

#### `gold_period_summaries`
Pre-computed daily, weekly, and monthly aggregates. Phase 2 uses these for trend queries and period comparisons — dramatically faster than aggregating thousands of raw transactions on every query.

Includes anomaly signals (`anomaly_flag`, `vs_prior_period_pct`) and category breakdowns stored as JSON for flexible querying.

---

### Audit Layer

#### `pipeline_audit_log`
Immutable record of every pipeline operation. Every Bronze insert, every Silver normalisation, every Gold promotion, every quarantine — all logged here with status, duration, and row count.

This is the accountability layer. If something goes wrong at 2am, this log tells you exactly what happened, when, and to which rows.

---

## The Gold Schema Contract

This is the most important architectural boundary in the system.

```
Phase 1 writes Gold.
Phase 2 reads Gold.
Neither touches the other's layers.
```

The Gold schema is a formal contract between the two phases. Phase 1 guarantees that every row in `gold_transactions` meets the schema exactly. Phase 2 can build its entire RAG pipeline against this contract without caring how Phase 1 produced the data.

**Changing the Gold schema requires coordination between both phases.**

---

## DatabaseManager Class

`src/database/init_db.py` exports a singleton `db` object used throughout the application.

```python
from src.database.init_db import db

# Called once at application startup
db.initialise()

# Phase 1 — read/write operations
with db.connection() as conn:
    conn.execute("INSERT INTO bronze_transactions ...")

# Phase 2 — read-only operations
with db.read_connection() as conn:
    result = conn.execute("SELECT * FROM gold_transactions").fetchdf()

# Audit logging
db.log_operation(
    operation="bronze_insert",
    source_layer="bronze",
    status="success",
    rows_affected=50,
    duration_ms=120
)

# Health check
stats = db.get_schema_stats()
# Returns: {"bronze_transactions": 0, "gold_transactions": 0, ...}
```

**Key behaviour:** `initialise()` is idempotent — safe to call on every application startup. It uses `CREATE TABLE IF NOT EXISTS` so it never destroys existing data.

---

## Configuration

All settings are loaded from a `.env` file via `config/settings.py`. The application never has hardcoded values.

```
# .env — required for Step 1
DB_MEMORY_LIMIT=512MB      # Conservative — works on 2GB RAM machines
DB_THREADS=4               # Parallel query execution
QUALITY_GATE_MIN=0.7       # Minimum quality score for Gold entry
TIMEZONE=Asia/Karachi      # Pakistani Standard Time
FISCAL_YEAR_START=07-01    # Pakistani fiscal year starts July 1
```

The Groq API key and Qdrant settings are not needed until Phase 2. Leave them blank for now.

---

## Tests

33 tests cover every aspect of the database layer.

```bash
python -m pytest tests/phase1/test_db_init.py -v
```

**Test coverage:**

| Test Class | What It Tests |
|---|---|
| `TestInitialization` | DB file creation, idempotent init, all tables created |
| `TestBronzeLayer` | Inserts, UUID generation, CHECK constraint enforcement, JSON preservation |
| `TestSilverLayer` | Normalised inserts, virtual `net_amount` column, quarantine table |
| `TestGoldLayer` | Quality gate enforcement (rejects score < 0.7), embedding_text requirement, period summaries |
| `TestPhase2Contract` | Phase 2 can read Gold, correct data returned |
| `TestAuditLog` | Audit log writes, non-fatal failure handling, schema stats |
| `TestUtilities` | SHA256 file hashing, determinism, collision detection |

**Known DuckDB constraints handled:**
- DuckDB 1.5.2 does not support partial indexes (`WHERE` clause on `CREATE INDEX`) — all indexes are standard
- DuckDB does not allow concurrent read-only and read-write connections to the same file — read-only contract is enforced at application layer

---

## Setup & Running

### Prerequisites
- Python 3.11 or higher
- Windows, Mac, or Linux

### First-time setup

```bash
# 1. Navigate to project folder
cd Neural_Ledger

# 2. Create virtual environment
python -m venv venv

# 3. Activate it
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# 4. Install dependencies
pip install duckdb python-dotenv pytest

# 5. Create .env file (copy from .env.example and fill in)
# On Windows: copy .env.example .env
# On Mac/Linux: cp .env.example .env

# 6. Initialise the database
python init_database.py

# 7. Run tests to verify everything works
python -m pytest tests/phase1/test_db_init.py -v
```

### Expected output from `init_database.py`

```
Database ready. Table status:
  bronze_transactions: 0 rows
  silver_transactions: 0 rows
  silver_quarantine: 0 rows
  gold_transactions: 0 rows
  gold_period_summaries: 0 rows
  pipeline_audit_log: 0 rows

Done. Check your storage/ folder for neural_ledger.db
```

### Expected output from tests

```
33 passed in 2.99s
```

---

## Design Decisions

**Why DuckDB over MSSQL for local storage?**
MSSQL requires a separate server installation, minimum 1GB RAM just for the engine, and Windows-only without significant overhead. DuckDB is embedded (zero install), runs inside the Python process, and is 10–100x faster than SQLite for analytical queries — exactly the workload of financial aggregation and reporting.

**Why `net_amount` as a virtual column?**
Storing `net_amount = credit - debit` as a real column creates the risk of it going out of sync with the debit/credit values. A virtual `GENERATED ALWAYS AS` column is computed on read — always accurate, never inconsistent.

**Why JSON for `raw_content`?**
Pakistani accountants use wildly different column names (`Raqam`, `Taareekh`, `Dr`, `Amount`, `Debit Amount`, mixed Urdu-English). Storing the original row as JSON means we never lose the original structure. The adaptive mapper can always go back to the raw content if the normalisation needs to be re-run.

**Why `year_month` as a dedicated column?**
The most common financial query pattern is time-based: "last month", "Q1 2024", "this year". Storing `year_month` as `VARCHAR(7)` (`"2024-01"`) as a dedicated indexed column makes these queries instant without needing to parse or truncate the full date on every query.

**Why `quality_score >= 0.7` at the database level?**
The quality gate could be enforced in Python code — but that means a bug could accidentally insert bad data into Gold. Enforcing it as a `CHECK` constraint means the database itself guarantees the invariant. It is impossible to insert a row with `quality_score < 0.7` into Gold, regardless of which code path calls the insert.

---

## What Comes Next

**Step 2 — File Watcher**

The File Watcher monitors the `data_vault/` folder. When a user drops any file (Excel, CSV, PDF), it is automatically detected, identified by type and likely source software, and queued for ingestion into the Bronze layer.

This is the feature that makes Neural Ledger feel alive — the user never has to press a button.
