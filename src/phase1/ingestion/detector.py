"""
Neural Ledger — File Type Detector
====================================
Identifies what kind of financial file was dropped into the Data Vault
and which accounting software likely produced it.

This is critical because every accounting software produces different
column structures. Knowing the source software upfront means the
normalization engine knows exactly which mapping strategy to apply.

Detection order:
1. Extension-based (fast, usually sufficient)
2. Content-based sniffing (for ambiguous files)
3. Filename pattern matching (for common export naming conventions)
"""

import re
from pathlib import Path
from typing import Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Supported file extensions
# ─────────────────────────────────────────────────────────────────────────────

SUPPORTED_EXTENSIONS = {
    ".xlsx", ".xls",   # Excel
    ".csv",            # CSV
    ".pdf",            # Bank statements, invoices
    ".json",           # API exports
}

IGNORED_EXTENSIONS = {
    ".tmp", ".lock", ".swp", ".bak", ".~",
    ".part", ".crdownload", ".ds_store",
}


# ─────────────────────────────────────────────────────────────────────────────
# Software fingerprint patterns
# Filename patterns that indicate which software exported the file
# ─────────────────────────────────────────────────────────────────────────────

# Each entry: (regex_pattern, source_software, confidence)
FILENAME_PATTERNS = [
    # QuickBooks Desktop — transaction exports
    (r"(?i)quickbooks|quick.?books|qb.*transactions|export.*quickbooks", "quickbooks_desktop", 0.85),
    (r"(?i)transaction.*detail.*report|general.*ledger.*detail", "quickbooks_desktop", 0.70),

    # QuickBooks Online — report exports
    (r"(?i)qbo.*export|quickbooks.*online", "quickbooks_online", 0.85),

    # Tally — characteristic naming
    (r"(?i)tally.*export|tallyprime|tally\.xml", "tally", 0.90),
    (r"(?i)daybook|cashbook.*tally|ledger.*tally", "tally", 0.70),

    # LedgerMax — local Pakistani software
    (r"(?i)ledgermax|ledger.?max", "ledgermax", 0.95),

    # Moneypex — Pakistani software
    (r"(?i)moneypex", "moneypex", 0.95),

    # Bank statement patterns — Pakistani banks
    (r"(?i)hbl.*statement|habib.*bank.*statement", "bank_pdf_hbl", 0.90),
    (r"(?i)ubl.*statement|united.*bank.*statement", "bank_pdf_ubl", 0.90),
    (r"(?i)alfalah.*statement|bank.*alfalah", "bank_pdf_alfalah", 0.90),
    (r"(?i)meezan.*statement|meezan.*bank", "bank_pdf_meezan", 0.90),
    (r"(?i)nbp.*statement|national.*bank.*pakistan", "bank_pdf_nbp", 0.90),
    (r"(?i)account.*statement|bank.*statement|e.?statement", "bank_pdf_generic", 0.75),

    # Xero
    (r"(?i)xero.*export|xero.*report", "xero", 0.90),

    # Odoo / ERPNext
    (r"(?i)odoo.*export|erpnext", "odoo", 0.90),

    # Sage
    (r"(?i)sage.*export|sage.*report", "sage", 0.85),
]


def detect_file(file_path: str) -> Tuple[str, str, float]:
    """
    Detect the source_type and source_software for a given file.

    Returns:
        Tuple of (source_type, source_software, confidence)

        source_type:     'xlsx' | 'csv' | 'pdf' | 'json' | 'unknown'
        source_software: 'manual_excel' | 'quickbooks_desktop' | 'tally' | etc.
        confidence:      0.0–1.0 — how confident the detection is

    Examples:
        detect_file("ledger_jan_2024.xlsx")
        → ("xlsx", "manual_excel", 0.5)

        detect_file("TallyPrime_Export_2024.xlsx")
        → ("xlsx", "tally", 0.9)

        detect_file("HBL_Statement_Jan2024.pdf")
        → ("pdf", "bank_pdf_hbl", 0.9)
    """
    path = Path(file_path)
    extension = path.suffix.lower()
    filename = path.name

    # ── Step 1: Determine source_type from extension ─────────────────────────
    if extension in (".xlsx", ".xls"):
        source_type = "xlsx"
    elif extension == ".csv":
        source_type = "csv"
    elif extension == ".pdf":
        source_type = "pdf"
    elif extension == ".json":
        source_type = "json"
    else:
        source_type = "unknown"

    # ── Step 2: Identify source_software from filename patterns ──────────────
    for pattern, software, confidence in FILENAME_PATTERNS:
        if re.search(pattern, filename):
            return source_type, software, confidence

    # ── Step 3: Default fallback based on type ───────────────────────────────
    if source_type == "pdf":
        # PDFs without a recognisable name are likely bank statements
        return source_type, "bank_pdf_generic", 0.5

    if source_type in ("xlsx", "xls"):
        # Unrecognised Excel files are assumed to be manual Excel bookkeeping
        return source_type, "manual_excel", 0.5

    if source_type == "csv":
        return source_type, "unknown", 0.4

    if source_type == "json":
        return source_type, "unknown", 0.4

    return "unknown", "unknown", 0.0


def is_supported(file_path: str) -> bool:
    """Return True if this file type can be ingested."""
    ext = Path(file_path).suffix.lower()
    return ext in SUPPORTED_EXTENSIONS


def is_ignored(file_path: str) -> bool:
    """Return True if this file should be silently ignored (temp files etc.)."""
    path = Path(file_path)
    ext = path.suffix.lower()
    name = path.name.lower()

    # Ignore by extension
    if ext in IGNORED_EXTENSIONS:
        return True

    # Ignore hidden files (start with .)
    if name.startswith("."):
        return True

    # Ignore temp files created by Excel/Office while file is open
    if name.startswith("~$"):
        return True

    return False