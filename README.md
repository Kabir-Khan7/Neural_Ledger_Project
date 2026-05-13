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


# Neural Ledger — Step 2: File Watcher & Accounting Department Research

## The Research First — How an Accounting Department Actually Works

Before building any feature, we must understand who we are building for and
what data they work with. Neural Ledger's primary user is the accountant inside
a Pakistani SME. This section documents everything that informed Step 2's design.

---

### How an Accounting Department Operates

An accounting department manages the financial memory of a company. Their work
is structured around five core data domains — each representing a distinct type
of financial activity that generates its own files, reports, and records.

#### 1. General Ledger (GL) — The Master Record

The General Ledger is the central repository of every financial transaction a
business makes. Every other accounting domain eventually posts its summarised
totals into the GL.

**What the accountant does daily:**
- Records journal entries for every transaction
- Categorises transactions under Chart of Accounts headings
- Closes sub-ledgers into the GL at period end
- Produces the Trial Balance to verify debit = credit

**Data they produce (what Neural Ledger must ingest):**
- GL export files (Excel/CSV) with columns: Date, Account Code, Description, Debit, Credit
- Chart of Accounts master list
- Journal voucher records
- Trial balance exports

**What they need from Neural Ledger:**
- Real-time GL balance by account
- Variance analysis against prior period
- Instant answer to: "What is the balance on account 5010 Fuel Expenses?"

---

#### 2. Accounts Payable (AP) — Money Going Out

Tracks every amount the company owes to suppliers and vendors.

**What the accountant does:**
- Records supplier invoices when received
- Tracks payment due dates (30/60/90 day terms)
- Processes payment runs
- Reconciles supplier statements monthly

**Data they produce:**
- Supplier invoice registers (Excel: Vendor, Invoice#, Date, Amount, Due Date, Status)
- Payment registers (Date, Cheque#, Vendor, Amount Paid)
- AP aging reports (Outstanding 0-30, 31-60, 61-90, 90+ days)
- Bank payment confirmations

**What they need from Neural Ledger:**
- "Which suppliers are we overdue to pay?"
- "What is our total AP outstanding this month?"
- "Show me all unpaid invoices above PKR 50,000"
- Cash flow impact of upcoming payments

---

#### 3. Accounts Receivable (AR) — Money Coming In

Tracks every amount customers owe the company.

**What the accountant does:**
- Issues sales invoices to customers
- Records customer payments received
- Chases overdue customers
- Reconciles customer accounts

**Data they produce:**
- Sales invoice registers (Customer, Invoice#, Date, Amount, Due Date)
- Receipt registers (Date, Customer, Amount Received, Invoice#)
- AR aging reports (current and overdue)
- Customer statements

**What they need from Neural Ledger:**
- "How much is owed to us right now?"
- "Which customers are more than 60 days overdue?"
- "What is our average collection period?"
- Cash flow forecasting based on expected receipts

---

#### 4. Payroll — Employee Costs

Monthly employee salary calculations, deductions, and compliance.

**What the accountant does:**
- Calculates gross salary, income tax deduction, EOBI, SESSI
- Processes salary transfers
- Maintains payroll register
- Files withholding tax returns with FBR

**Data they produce:**
- Monthly payroll register (Employee, Basic, Allowances, Deductions, Net Pay)
- Bank transfer files
- WHT (Withholding Tax) computation sheets
- EOBI contribution registers

**What they need from Neural Ledger:**
- Total payroll cost by department per month
- Year-to-date salary expense
- WHT liability tracking
- Payroll as % of revenue trend

---

#### 5. Fixed Assets — Long-Term Assets

Tracks equipment, vehicles, furniture and their depreciation.

**What the accountant does:**
- Records asset purchases with cost and date
- Calculates monthly depreciation
- Maintains disposal/write-off records
- Prepares fixed asset schedule for audit

**Data they produce:**
- Fixed asset register (Asset, Date Purchased, Cost, Depreciation Rate, Net Book Value)
- Depreciation schedule
- Disposal records
- Asset-wise accumulated depreciation summary

**What they need from Neural Ledger:**
- Total net book value of assets
- Monthly depreciation expense
- Assets fully depreciated but still in use

---

### The Six Types of Financial Reports an Accounting Department Must Produce

Understanding these reports is essential because they define what Gold layer
data must contain. Every report maps to specific data fields.

| Report | Frequency | Data Required | Purpose |
|---|---|---|---|
| Income Statement (P&L) | Monthly | Revenue, COGS, Operating Expenses by category | Profitability |
| Balance Sheet | Quarterly | All asset, liability, equity accounts | Financial position |
| Cash Flow Statement | Monthly | All cash in/out grouped by operating/investing/financing | Liquidity |
| AP Aging Report | Weekly | Vendor, invoice date, due date, outstanding amount | Payment management |
| AR Aging Report | Weekly | Customer, invoice date, due date, outstanding amount | Collection management |
| FBR Sales Tax Return | Monthly | Sales, purchases, tax collected, tax paid | Regulatory compliance |

---

### What Data Neural Ledger Must Be Able to Ingest

From the research above, here is the complete taxonomy of files an accounting
department will drop into the Data Vault:

#### Transaction Files (Daily / Weekly)
```
sales_register_jan.xlsx         — Sales invoices
purchase_register_jan.xlsx      — Supplier invoices
cash_book_jan.xlsx              — Cash transactions
bank_transactions_jan.xlsx      — Bank entries
expense_vouchers_jan.xlsx       — Petty cash and expenses
payroll_jan.xlsx                — Salary register
```

#### Software Exports (Monthly)
```
QuickBooks_General_Ledger.xlsx  — Full GL export from QuickBooks
TallyPrime_Daybook.xlsx         — Tally day book export
LedgerMax_Report_Jan.xlsx       — LedgerMax transaction export
Xero_Export_Jan.csv             — Xero transaction CSV
```

#### Bank Statements (Monthly)
```
HBL_Statement_Jan2024.pdf       — HBL e-statement
UBL_Account_Statement.pdf       — UBL bank statement
Alfalah_eStatement.pdf          — Bank Alfalah statement
Meezan_Statement.pdf            — Meezan Bank statement
```

#### Compliance Files (Monthly / Annual)
```
fbr_annex_c_jan.xlsx            — FBR Annex-C data
withholding_tax_jan.xlsx        — WHT register
eobi_register.xlsx              — EOBI contribution register
```

---

### The Accounting Department's Expectation of Neural Ledger

When an accountant drops a file, they expect:

1. **Immediate acknowledgement** — the system should visibly register the file
2. **No data loss** — their original file is preserved exactly as provided
3. **Intelligent categorisation** — the system should understand what kind of
   file it is without being told
4. **Error surfacing** — if a row cannot be processed, it appears in a review
   queue with the original data intact
5. **Audit trail** — every file ingested must be traceable back to its source
6. **Duplicate protection** — dropping the same file twice does nothing harmful

---

## What Was Built in Step 2

### Files Created

```
neural_ledger/
├── src/
│   └── phase1/
│       ├── __init__.py
│       ├── watcher.py                  # Main file watcher service
│       └── ingestion/
│           ├── __init__.py
│           └── detector.py             # File type & software detector
└── tests/
    └── phase1/
        └── test_watcher.py             # 46 tests — all passing
```

---

## Architecture of the File Watcher

The watcher is composed of four distinct components, each with a single
responsibility:

```
Data Vault Folder  (accountant drops file here)
         ↓
DataVaultEventHandler     Receives OS file system events
         ↓
DebounceTimer             Waits 2 seconds after last event
         ↓                (handles Excel's burst-write behaviour)
FileProcessor             Validates, hashes, detects, queues
         ↓
Bronze Ingestion          (Step 3 — next step)
```

---

## Component 1: File Detector (`detector.py`)

Identifies what type of file was dropped and which accounting software
most likely produced it. This is critical because every software exports
data in a different column format. The normalization engine in Step 4
uses this information to select the correct mapping strategy.

### Source Types Detected

| Extension | `source_type` value |
|---|---|
| `.xlsx`, `.xls` | `xlsx` |
| `.csv` | `csv` |
| `.pdf` | `pdf` |
| `.json` | `json` |

### Source Software Detected

Detection is based on filename pattern matching using regular expressions.

| Filename Pattern | `source_software` | Confidence |
|---|---|---|
| Contains "QuickBooks" or "QuickBooks" | `quickbooks_desktop` | 85% |
| Contains "TallyPrime" or "Tally" | `tally` | 90% |
| Contains "LedgerMax" | `ledgermax` | 95% |
| Contains "Xero" | `xero` | 90% |
| Contains "Moneypex" | `moneypex` | 95% |
| Contains "HBL" + "Statement" | `bank_pdf_hbl` | 90% |
| Contains "UBL" + "Statement" | `bank_pdf_ubl` | 90% |
| Contains "Alfalah" | `bank_pdf_alfalah` | 90% |
| Contains "Meezan" | `bank_pdf_meezan` | 90% |
| Contains "NBP" | `bank_pdf_nbp` | 90% |
| Generic "account statement" | `bank_pdf_generic` | 75% |
| Unrecognised `.xlsx`/`.xls` | `manual_excel` | 50% |

### Files Ignored

These are silently skipped — no error, no audit log entry:

- `~$filename.xlsx` — Excel lock files created while file is open
- `.hidden_file` — Hidden files starting with `.`
- `*.tmp`, `*.bak`, `*.swp`, `*.lock` — Temporary and backup files
- Empty files (0 bytes)

---

## Component 2: Debounce Timer (`watcher.py → DebounceTimer`)

**The Problem:** When Windows or Mac copies a large Excel file into the Data
Vault folder, the OS fires 10–30 file-modified events as the file is written
in chunks. Without debouncing, the system would attempt to parse an incomplete
file on the first event.

**The Solution:** After every file event, a 2-second timer is (re)started.
Only when 2 seconds of silence have passed is the file considered ready.

```
Event 1 → timer starts (2s)
Event 2 → timer resets (2s)
Event 3 → timer resets (2s)
  ... silence ...
Timer fires → file is ready → processor called
```

**Result:** The processor only ever sees the complete, final version of the file.

---

## Component 3: File Processor (`watcher.py → FileProcessor`)

After debounce, the processor runs five sequential checks before queuing:

```
1. File still exists?          No  → log warning, skip
2. File has content?           No  → log warning, skip
3. SHA256 already in Bronze?   Yes → log duplicate, skip
4. Detect source type/software     → attach to metadata
5. Queue for ingestion             → callback with full metadata dict
```

### Metadata Passed to Ingestion Pipeline

Every queued file carries this metadata dictionary:

```python
{
    "file_path":       "/path/to/data_vault/sales_jan.xlsx",
    "file_name":       "sales_jan.xlsx",
    "source_type":     "xlsx",
    "source_software": "manual_excel",
    "detection_conf":  0.5,
    "file_hash":       "a3f8c2d1e9b4...",  # SHA256
    "file_size_bytes": 24576,
    "detected_at":     "2024-01-15T14:30:22.123456",
}
```

This metadata flows directly into the Bronze layer in Step 3.

---

## Component 4: Data Vault Watcher (`watcher.py → DataVaultWatcher`)

The main orchestrator. Manages the observer lifecycle and exposes a clean API.

### Usage

```python
from src.phase1.watcher import DataVaultWatcher

def on_file_ready(metadata: dict):
    print(f"File ready for ingestion: {metadata['file_name']}")
    print(f"Software detected: {metadata['source_software']}")
    # Step 3 Bronze ingestion happens here

watcher = DataVaultWatcher(
    vault_path="data_vault/",
    on_file_ready=on_file_ready,
)

# Start monitoring (background thread)
watcher.start()

# Process files already in vault from before app started
watcher.scan_existing()

# Application runs...

# Clean shutdown
watcher.stop()
```

### Startup Scan

`scan_existing()` processes any files already present in the Data Vault
when the application starts. This handles files dropped while the application
was offline — a common scenario when an accountant works without the app open.

---

## Test Coverage

**46 tests — all passing.**

Run with:
```bash
python -m pytest tests/phase1/test_watcher.py -v
```

| Test Class | Count | What It Tests |
|---|---|---|
| `TestDetector` | 14 | All software detections, extensions, case-insensitivity |
| `TestIsSupported` | 8 | Supported and unsupported file types |
| `TestIsIgnored` | 7 | Temp files, lock files, hidden files |
| `TestDebounceTimer` | 4 | Delay fires, rapid events coalesced, cancel, two files |
| `TestFileProcessor` | 7 | Valid files, empty files, missing files, duplicates, error survival |
| `TestDataVaultWatcher` | 6 | Start/stop, folder creation, file detection, temp ignored, startup scan, multiple files |

---

## Setup for Step 2

### Install the new dependency

```bash
pip install watchdog
```

### Updated `requirements.txt`

```
duckdb
python-dotenv
pytest
watchdog
```

### Run the tests

```bash
python -m pytest tests/phase1/test_watcher.py -v
```

### Try the watcher manually

Create a file called `run_watcher.py` in your project root:

```python
import sys
import time
sys.path.insert(0, '.')

from src.database.init_db import db
from src.phase1.watcher import DataVaultWatcher

db.initialise()

def on_file_ready(metadata):
    print(f"\n[DETECTED] {metadata['file_name']}")
    print(f"  Type:       {metadata['source_type']}")
    print(f"  Software:   {metadata['source_software']}")
    print(f"  Confidence: {metadata['detection_conf']:.0%}")
    print(f"  Size:       {metadata['file_size_bytes']:,} bytes")

watcher = DataVaultWatcher(
    vault_path="data_vault/",
    on_file_ready=on_file_ready,
)

watcher.start()
watcher.scan_existing()

print("\nWatcher running — drop files into data_vault/ folder")
print("Press Ctrl+C to stop\n")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.stop()
    print("\nWatcher stopped.")
```

Then run:
```bash
python run_watcher.py
```

Drop an Excel or CSV file into the `data_vault/` folder. You will see it
detected immediately with its type and source software identified.

---

## Design Decisions

**Why Watchdog over manual polling?**
Watchdog uses OS-native APIs: `inotify` on Linux, `FSEvents` on macOS,
`ReadDirectoryChangesW` on Windows. This means sub-millisecond detection with
zero CPU usage between events. Manual polling every N seconds would consume CPU
constantly and introduce detection latency.

**Why 2 seconds for debounce?**
Testing on Windows showed Excel writes large files in bursts lasting up to
1.5 seconds. 2 seconds provides a safe margin while still feeling near-instant
to the user. This value is configurable via `WATCHER_DEBOUNCE_MS` in `.env`.

**Why filename-based detection over content-based?**
Content-based detection (opening the file, reading headers) is slower and
more fragile — a partially-written file would cause parse errors. Filename
patterns are resolved in microseconds and are accurate for 90% of real-world
accounting software exports. Content-based detection is used as a fallback
in Step 3 (Bronze ingestion) once the file is confirmed complete.

**Why SHA256 for duplicate detection?**
An accountant who drops the same file twice (very common) should experience
no negative consequence. MD5 is faster but has known collisions. SHA256
guarantees that two files with the same hash are identical.

**Why `recursive=False` in the watcher?**
The Data Vault is a single flat folder. Watching recursively would also pick
up files from subfolders the accountant might use for organisation (e.g. an
`archive/` folder). Keeping it non-recursive keeps the behaviour predictable.

---

## Accounting Data: What Goes in Each Layer

Based on the research above, here is how accounting department data maps
to the Neural Ledger layers:

### Bronze Layer receives:
Everything as-is — original column names, original formats, original values.
No transformation. The accountant's `Raqam` stays `Raqam`.

### Silver Layer produces:
Standardised transactions regardless of source. After Silver, there is no
difference between a Tally export and a manual Excel file — both have the
same schema with standardised dates, amounts, and vendor names.

### Gold Layer exposes:
AI-ready, categorised, time-indexed transactions with:
- FBR tax head mapping
- Category with confidence score
- Pre-built embedding sentence
- Quality gate enforced (score ≥ 0.7)

---

## What Comes Next

**Step 3 — Bronze Writer**

Takes the metadata dict produced by Step 2's `FileProcessor` and writes every
row of the detected file into the `bronze_transactions` table. This includes:
- Reading the file (Excel parser, CSV parser, PDF parser)
- Preserving original column names in `raw_content` JSON
- Assigning a `ingestion_batch_id` to group all rows from one file
- Setting `processing_status = 'pending'` for every row

The Bronze Writer is the first step that actually touches the file contents.

# Neural Ledger — Step 3: Bronze Writer

## Overview

Step 3 is the first point where Neural Ledger actually opens a financial file
and reads its contents. The Bronze Writer takes a file queued by the File
Watcher (Step 2), parses every row, and writes it into the `bronze_transactions`
table exactly as provided — original column names, original values, nothing
changed.

Bronze is the memory of the system. Every file the accountant ever drops is
preserved here permanently. Even if the normalization engine in Silver makes a
mistake, the original data is always recoverable from Bronze.

---

## Files Created

```
neural_ledger/
├── src/
│   └── phase1/
│       ├── ingestion/
│       │   ├── excel_parser.py     # Excel (.xlsx/.xls) parser
│       │   └── csv_parser.py       # CSV parser with encoding detection
│       └── bronze/
│           ├── __init__.py
│           └── bronze_writer.py    # Main Bronze ingestion orchestrator
└── tests/
    └── phase1/
        └── test_bronze_writer.py   # 33 tests — all passing
```

---

## The Core Challenge: Real-World Accounting Files Are Messy

Pakistani accountant Excel files are not clean, structured tables. Before
writing any parser, the team studied the actual files accountants produce.
Here is what the parsers handle:

### Excel Challenges Solved

**Multiple title rows before the real headers**
An Excel file from a Pakistani accountant typically looks like:
```
Row 1: ABC Trading Company (Pvt) Ltd        ← company name
Row 2: Cash Book — January 2024             ← report title
Row 3: (blank)                              ← visual space
Row 4: Date | Particulars | Dr | Cr | Bal  ← REAL headers
Row 5: 01/01/2024 | Opening Balance | ...   ← data starts
```
The header detection algorithm scans the first 15 rows and identifies the
first row that looks like column headers (mostly string values, not numbers
or dates). This finds row 4 and ignores the title rows completely.

**Dr/Cr split columns**
Standard accounting format uses separate Debit and Credit columns instead
of a single net amount. Both columns are preserved as-is in Bronze. The
normalization engine in Silver handles the Dr/Cr to net_amount conversion.

**Urdu and mixed-language headers**
`تاریخ` (date), `رقم` (amount), `تفصیل` (description) are common column
names in Pakistani accountant files. The parser uses Unicode-aware handling
to preserve these exactly. The `language_detected` field in Silver will be
set to `ur` or `mixed` accordingly.

**Blank rows used as visual separators**
Accountants add blank rows between sections. These are detected and removed
before writing to Bronze.

**Multiple sheets**
Excel files often have a Summary sheet and a Transactions sheet. The parser
picks the sheet with the most data automatically.

**Merged cells in headers**
The openpyxl engine handles merged cell ranges. Missing values in merged
ranges are forward-filled to reconstruct the full header.

### CSV Challenges Solved

**Encoding detection**
Pakistani Windows machines produce CSV files in Windows-1252 encoding by
default. `chardet` detects the encoding automatically with confidence scoring.
If confidence is below 80%, it defaults to `utf-8-sig` (handles Windows BOM).

Encoding fallback chain (tried in order if detection fails):
1. `utf-8-sig` — UTF-8 with BOM (Excel "Save as CSV" on Windows)
2. `utf-8` — standard
3. `cp1252` — Windows-1252 (most Windows CSV exports)
4. `latin-1` — accepts all byte values (absolute last resort)

**Delimiter detection**
Uses Python's `csv.Sniffer` first, then frequency analysis. Handles comma,
tab, semicolon, and pipe-delimited files. Semicolon is common from Tally
and other European-origin accounting software.

**BOM characters**
Files saved from Windows Excel with "UTF-8 (BOM)" encoding have an invisible
BOM character at the start. Without handling, this appears in the first column
name as `\ufeffDate`. The `utf-8-sig` codec strips it automatically.

**Total and summary rows**
CSV exports from accounting software often include total rows at the bottom:
`Total, 500000, ,`. These are detected by checking if the first column value
matches known total keywords (including Urdu `kul`, `majmua`) and skipped.

---

## Architecture

### Component 1: ExcelParser

```python
from src.phase1.ingestion.excel_parser import ExcelParser

parser = ExcelParser()
rows, headers, meta = parser.parse("sales_jan.xlsx")

# rows: list of dicts with original column names
# [{"Date": "15/01/2024", "Vendor": "Shell", "Amount": "5000"}, ...]

# headers: original column names in order
# ["Date", "Vendor", "Amount"]

# meta: parsing information
# {"sheet_name": "Transactions", "header_row": 3, "total_rows": 45, ...}
```

**Header detection strategy:**
1. Load with openpyxl (read_only=True for speed)
2. Scan first 15 rows
3. Find first row where ≥50% of non-empty cells are strings
4. Use that row index as the `header=` parameter for pandas

**Sheet selection strategy:**
Count non-empty rows in each sheet (up to 200 rows scan).
Pick the sheet with the most data.

### Component 2: CSVParser

```python
from src.phase1.ingestion.csv_parser import CSVParser

parser = CSVParser()
rows, headers, meta = parser.parse("transactions.csv")

# meta includes detected encoding and delimiter
# {"encoding": "cp1252", "encoding_confidence": 0.95, "delimiter": ",", ...}
```

### Component 3: BronzeWriter

```python
from src.phase1.bronze.bronze_writer import BronzeWriter

writer = BronzeWriter()

# metadata comes directly from FileProcessor (Step 2)
result = writer.ingest({
    "file_path":       "/path/to/data_vault/sales_jan.xlsx",
    "file_name":       "sales_jan.xlsx",
    "source_type":     "xlsx",
    "source_software": "manual_excel",
    "detection_conf":  0.85,
    "file_hash":       "a3f8c2...",
    "file_size_bytes": 24576,
    "detected_at":     "2024-01-15T14:30:00",
})

print(result)
# {
#     "success":      True,
#     "rows_written": 45,
#     "batch_id":     "f3a1c2d4-...",
#     "error":        None,
#     "parse_meta":   {"total_rows": 45, "total_cols": 5, ...}
# }
```

---

## What Gets Written to Bronze

Each row in `bronze_transactions` contains:

| Field | What it stores |
|---|---|
| `bronze_id` | Auto-generated UUID |
| `source_type` | `xlsx` or `csv` |
| `source_software` | `manual_excel`, `tally`, `quickbooks_desktop`, etc. |
| `source_file` | Original filename |
| `source_file_hash` | SHA256 — same as Step 2 duplicate detection |
| `source_row_number` | Row number in original file (1-based) |
| `raw_content` | **The original row as JSON** — original column names preserved |
| `raw_headers` | **All original column headers** from the file |
| `ingestion_batch_id` | UUID grouping all rows from this one file |
| `processing_status` | Always `pending` — Silver picks these up next |
| `ingested_at` | Timestamp |

### Example `raw_content` for a Tally export row:
```json
{
    "Date": "15/01/2024",
    "Particulars": "Shell Clifton",
    "Vch Type": "Payment",
    "Vch No.": "PV-001",
    "Debit": "5000",
    "Credit": null
}
```

### Example `raw_content` for a manual Urdu Excel row:
```json
{
    "تاریخ": "15/01/2024",
    "رقم": "5000",
    "تفصیل": "Petrol purchase",
    "Vendor": "Shell"
}
```

Both rows have `processing_status = 'pending'`. The Silver normalization engine
reads both and applies different mapping strategies based on `source_software`.

---

## Pipeline Integration

Step 3 connects directly to Steps 2 and 4:

```
Step 2 (File Watcher)
    FileProcessor detects file → produces metadata dict
            ↓
Step 3 (Bronze Writer)
    BronzeWriter.ingest(metadata) → writes to bronze_transactions
            ↓
Step 4 (Schema Mapper — coming next)
    Reads bronze rows with status='pending'
    Maps original column names to standard schema
    Writes to silver_transactions
```

To connect Steps 2 and 3 in the pipeline:

```python
from src.phase1.watcher import DataVaultWatcher
from src.phase1.bronze.bronze_writer import BronzeWriter

writer = BronzeWriter()

watcher = DataVaultWatcher(
    vault_path="data_vault/",
    on_file_ready=writer.ingest,   # Step 2 calls Step 3 directly
)
watcher.start()
watcher.scan_existing()
```

---

## Test Coverage

**33 tests — all passing.**

```bash
python -m pytest tests/phase1/test_bronze_writer.py -v
```

| Test Class | Count | What It Tests |
|---|---|---|
| `TestExcelParser` | 10 | Clean files, title rows, Dr/Cr, Urdu headers, blank rows, multiple sheets, empty columns |
| `TestCSVParser` | 9 | UTF-8, Windows-1252, BOM, semicolons, tabs, blank rows, total rows, whitespace |
| `TestBronzeWriter` | 14 | Excel ingest, CSV ingest, raw_content integrity, source_software, pending status, batch IDs, Urdu headers, file hash, row numbers, audit log |

---

## Setup for Step 3

### Install new dependencies

```bash
pip install pandas openpyxl xlrd chardet
```

### Updated requirements.txt

```
duckdb
python-dotenv
pytest
watchdog
pandas
openpyxl
xlrd
chardet
```

### New folder to create

Create `src/phase1/bronze/` and add an empty `__init__.py` inside it.

### Run the tests

```bash
python -m pytest tests/phase1/test_bronze_writer.py -v
```

### Try it end-to-end

Create `run_pipeline.py` in project root:

```python
import sys, time
sys.path.insert(0, '.')

from src.database.init_db import db
from src.phase1.watcher import DataVaultWatcher
from src.phase1.bronze.bronze_writer import BronzeWriter

db.initialise()
writer = BronzeWriter()

def on_file_ready(metadata):
    print(f"\n[INGESTING] {metadata['file_name']}")
    result = writer.ingest(metadata)
    if result["success"]:
        print(f"  Written: {result['rows_written']} rows to Bronze")
        print(f"  Batch:   {result['batch_id'][:8]}")
    else:
        print(f"  Failed:  {result['error']}")

    # Show Bronze stats
    stats = db.get_schema_stats()
    print(f"  Bronze total: {stats['bronze_transactions']} rows")

watcher = DataVaultWatcher(vault_path="data_vault/", on_file_ready=on_file_ready)
watcher.start()
watcher.scan_existing()

print("Drop Excel or CSV files into data_vault/ — Ctrl+C to stop")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    watcher.stop()
```

```bash
python run_pipeline.py
```

Drop any Excel or CSV file into `data_vault/`. You will see it ingested and
the Bronze row count increase in real time.

---

## Design Decisions

**Why openpyxl for header detection and pandas for data reading?**
openpyxl gives fine-grained control over individual cells and merged cell
ranges — essential for detecting where the real headers are. Once the header
row is identified, pandas handles the bulk data reading efficiently (much
faster than iterating rows with openpyxl for large files).

**Why `dtype=str` when reading with pandas?**
If pandas infers types, it converts `"01/01/2024"` to a Timestamp object and
`"5000"` to an integer. This destroys the original values. By reading
everything as string, Bronze stores exactly what the accountant typed.
Type inference happens in Silver — not in Bronze.

**Why JSON for `raw_content`?**
A flexible JSON blob means Bronze can accept any file structure without
schema changes. A file with 3 columns and a file with 15 columns both fit
in the same table. The Silver normalization engine reads the JSON and maps
whatever columns exist to the standard schema.

**Why `ensure_ascii=False` in JSON serialization?**
Without this flag, Urdu text like `تاریخ` would be stored as
`\u062a\u0627\u0631\u06cc\u062e`. With `ensure_ascii=False`, it is stored
as readable Unicode text. This makes debugging and manual inspection vastly
easier.

**Why `source_row_number` (1-based)?**
When an accountant reports "row 47 of my Excel has an error," they count
from row 1. Storing 1-based row numbers means the audit trail speaks the
same language as the accountant.

---

## What Comes Next

**Step 4 — Schema Mapper (Silver Layer)**

The Schema Mapper reads Bronze rows with `processing_status = 'pending'`.
For each row, it:
1. Looks up the `source_software` in `bronze_schema_mappings`
2. Maps original column names to the Neural Ledger standard schema
3. Handles Dr/Cr split columns, date format detection, amount parsing
4. Writes the normalized row to `silver_transactions`
5. Updates `bronze_transactions.processing_status = 'normalised'`

This is where `Taareekh` becomes `transaction_date` and `Raqam` becomes
`net_amount`.
