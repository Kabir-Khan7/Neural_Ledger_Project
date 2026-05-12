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
