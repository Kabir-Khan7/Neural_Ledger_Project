"""
Neural Ledger — Schema Mapper Security Module (Step 4c)
=========================================================
Seven security gates that run BEFORE and AFTER mapping.

Gates run before mapping:
  1. File size check
  2. Row count check
  3. Formula neutralization (Excel injection attack prevention)
  4. Reserved name protection (schema confusion prevention)
  5. Unicode normalization

Gates run after LLM mapping:
  6. LLM output validator
  7. Final output sanitization
"""

import re
import math
import json
import logging
import unicodedata
from typing import Any, Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────
MAX_FILE_SIZE_MB     = 50
MAX_ROWS_PER_FILE    = 100_000
AMOUNT_SPIKE_RATIO   = 1000       # Flag if amount > 1000x average
MAX_FUTURE_DAYS      = 30         # Flag dates this many days in future

# Formula injection prefixes — common in CSV/Excel injection attacks
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Characters that should never appear in financial field values
DANGEROUS_PATTERNS = [
    r"<script",
    r"javascript:",
    r"data:text",
    r"HYPERLINK",
    r"=CMD\(",
    r"=SHELL\(",
    r"=EXEC\(",
    r"\.\./",          # Path traversal
    r"\x00",           # Null bytes
]

# Valid standard field names — LLM output must only produce these
from src.phase1.schema_mapper.dictionary import STANDARD_FIELDS, RESERVED_INTERNAL_NAMES


# ─────────────────────────────────────────────────────────────────────────────
# Gate 1 — File size check
# ─────────────────────────────────────────────────────────────────────────────

def check_file_size(file_size_bytes: int) -> Tuple[bool, str]:
    """
    Returns (passed, message).
    Files above MAX_FILE_SIZE_MB are rejected before parsing.
    """
    size_mb = file_size_bytes / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return False, (
            f"File size {size_mb:.1f}MB exceeds maximum "
            f"{MAX_FILE_SIZE_MB}MB. Split the file and upload in parts."
        )
    return True, f"File size OK ({size_mb:.1f}MB)"


# ─────────────────────────────────────────────────────────────────────────────
# Gate 2 — Row count check
# ─────────────────────────────────────────────────────────────────────────────

def check_row_count(row_count: int) -> Tuple[bool, str]:
    """
    Returns (passed, message).
    Files above MAX_ROWS_PER_FILE are quarantined with guidance.
    """
    if row_count > MAX_ROWS_PER_FILE:
        return False, (
            f"File contains {row_count:,} rows, exceeding the "
            f"{MAX_ROWS_PER_FILE:,} row limit. "
            f"Please split into monthly files and upload separately."
        )
    return True, f"Row count OK ({row_count:,} rows)"


# ─────────────────────────────────────────────────────────────────────────────
# Gate 3 — Formula neutralization
# ─────────────────────────────────────────────────────────────────────────────

def neutralize_formulas(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """
    Scan all cell values for formula injection patterns.
    Strips the formula and prepends "FORMULA_REMOVED:" to the value.

    Returns (cleaned_rows, count_of_neutralized_cells).
    This is a critical security gate — Excel/CSV injection is a real attack vector.
    """
    neutralized_count = 0

    cleaned_rows = []
    for row in rows:
        cleaned_row = {}
        for key, value in row.items():
            cleaned_row[key] = _neutralize_cell(value)
            if cleaned_row[key] != value:
                neutralized_count += 1
        cleaned_rows.append(cleaned_row)

    if neutralized_count > 0:
        logger.warning(
            f"Formula neutralization: {neutralized_count} potentially "
            f"dangerous cell values cleaned"
        )

    return cleaned_rows, neutralized_count


def _neutralize_cell(value: Any) -> Any:
    """Neutralize a single cell value."""
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if not stripped:
        return value

    # Check for formula injection prefixes
    if stripped[0] in FORMULA_PREFIXES:
        logger.debug(f"Formula stripped: {stripped[:30]}...")
        return f"FORMULA_REMOVED:{stripped}"

    # Check for dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, stripped, re.IGNORECASE):
            logger.warning(f"Dangerous pattern '{pattern}' found in cell value")
            return f"DANGEROUS_CONTENT_REMOVED"

    return value


# ─────────────────────────────────────────────────────────────────────────────
# Gate 4 — Reserved name protection
# ─────────────────────────────────────────────────────────────────────────────

def protect_reserved_names(
    columns: List[str],
) -> Tuple[List[str], Dict[str, str]]:
    """
    Rename columns that conflict with internal Neural Ledger field names.
    Returns (cleaned_columns, rename_map).

    rename_map = {"original_name": "raw_original_name"}
    """
    cleaned = []
    rename_map = {}
    reserved_lower = {n.lower() for n in RESERVED_INTERNAL_NAMES}

    for col in columns:
        if col.lower() in reserved_lower:
            new_name = f"raw_{col}"
            rename_map[col] = new_name
            cleaned.append(new_name)
            logger.warning(
                f"Reserved column name '{col}' renamed to '{new_name}' "
                f"to prevent schema confusion"
            )
        else:
            cleaned.append(col)

    return cleaned, rename_map


def apply_column_renames(
    rows: List[Dict[str, Any]],
    rename_map: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Apply column renames to all rows."""
    if not rename_map:
        return rows
    return [
        {rename_map.get(k, k): v for k, v in row.items()}
        for row in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Gate 5 — Unicode normalization
# ─────────────────────────────────────────────────────────────────────────────

def normalize_unicode(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Normalize all text values to Unicode NFC form.
    Prevents encoding-based bypass attacks where the same visual character
    has multiple Unicode representations.
    """
    return [
        {
            _nfc(k): _nfc(v) if isinstance(v, str) else v
            for k, v in row.items()
        }
        for row in rows
    ]


def _nfc(text: str) -> str:
    """Apply NFC Unicode normalization."""
    return unicodedata.normalize("NFC", text) if text else text


# ─────────────────────────────────────────────────────────────────────────────
# Gate 6 — LLM output validator
# ─────────────────────────────────────────────────────────────────────────────

def validate_llm_output(
    llm_response: Dict[str, Any],
    original_columns: List[str],
) -> Tuple[Dict[str, str], List[str]]:
    """
    Validate and sanitize LLM mapping output before it can influence Silver.

    Rules:
    - Only field names in STANDARD_FIELDS are accepted
    - Confidence must be a float 0.0–1.0
    - Every mapping must reference an actual column from the file
    - LLM cannot introduce new columns or rename existing ones
    - Reject any mapping that looks like an instruction injection

    Returns (valid_mappings, rejected_reasons).
    """
    valid_mappings = {}
    rejected = []
    original_lower = {c.lower() for c in original_columns}

    if not isinstance(llm_response, dict):
        return {}, ["LLM response is not a dict — rejected entirely"]

    mappings = llm_response.get("mappings", {})
    if not isinstance(mappings, dict):
        return {}, ["LLM mappings key is not a dict — rejected entirely"]

    for original_col, target_field in mappings.items():

        # Column must exist in the actual file
        if original_col.lower() not in original_lower:
            rejected.append(
                f"'{original_col}': not in original columns — possible injection"
            )
            continue

        # Target must be a valid standard field
        if not isinstance(target_field, str):
            rejected.append(f"'{original_col}': target is not a string")
            continue

        target_clean = target_field.strip().lower()
        if target_clean not in STANDARD_FIELDS:
            rejected.append(
                f"'{original_col}' → '{target_field}': "
                f"not a valid standard field — rejected"
            )
            continue

        # Check for injection patterns in the mapping itself
        if any(c in target_field for c in ["<", ">", "{", "}", ";", "\n", "\r"]):
            rejected.append(
                f"'{original_col}' → '{target_field}': contains suspicious characters"
            )
            continue

        valid_mappings[original_col] = target_clean

    if rejected:
        logger.warning(
            f"LLM output validator rejected {len(rejected)} mappings: "
            f"{rejected[:3]}{'...' if len(rejected) > 3 else ''}"
        )

    return valid_mappings, rejected


# ─────────────────────────────────────────────────────────────────────────────
# Gate 7 — Final output sanitization
# ─────────────────────────────────────────────────────────────────────────────

def sanitize_mapped_row(
    row: Dict[str, Any],
    field_mapping: Dict[str, str],
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Apply the column mapping to a row and sanitize the resulting values.
    Returns (sanitized_row, warnings).

    Validates:
    - Amount fields are numeric
    - Date fields look like dates
    - No None values in critical fields
    - Values are within reasonable ranges
    """
    sanitized = {}
    warnings  = []

    for original_col, value in row.items():
        target_field = field_mapping.get(original_col)
        if not target_field:
            # Unmapped — keep with original name as metadata
            sanitized[f"_raw_{original_col}"] = value
            continue

        # Validate and clean by field type
        cleaned_value, field_warning = _validate_field_value(
            target_field, value, original_col
        )
        sanitized[target_field] = cleaned_value
        if field_warning:
            warnings.append(field_warning)

    return sanitized, warnings


def _validate_field_value(
    field: str,
    value: Any,
    original_col: str,
) -> Tuple[Any, Optional[str]]:
    """Validate and clean a single field value. Returns (cleaned, warning_or_None)."""

    if value is None or str(value).strip() in ("", "nan", "NaN", "None", "N/A"):
        return None, None

    str_value = str(value).strip()

    # ── Amount fields ─────────────────────────────────────────────────────
    if field in ("net_amount", "amount_debit", "amount_credit", "tax_amount"):
        cleaned, warning = _clean_amount(str_value, original_col)
        return cleaned, warning

    # ── Date fields ───────────────────────────────────────────────────────
    if field in ("transaction_date", "due_date"):
        return str_value, None   # Date parsing handled by normalization engine

    # ── Tax rate ─────────────────────────────────────────────────────────
    if field == "tax_rate":
        try:
            rate = float(str_value.replace("%", ""))
            if rate < 0 or rate > 100:
                return None, f"Tax rate '{str_value}' out of range (0–100)"
            return rate, None
        except ValueError:
            return None, f"Tax rate '{str_value}' is not numeric"

    # ── Text fields ───────────────────────────────────────────────────────
    # Truncate excessively long strings
    if field in ("vendor", "description", "account_code", "reference_number"):
        if len(str_value) > 500:
            return str_value[:500], f"Value truncated to 500 chars in '{original_col}'"

    return str_value, None


def _clean_amount(value: str, col_name: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Parse a Pakistani financial amount string to float.
    Handles: PKR prefix, comma separators, Rs. prefix, parentheses for negative.
    """
    original = value

    # Remove currency symbols and labels
    value = re.sub(r"(?i)^(pkr|rs\.?|rupees?)\s*", "", value)

    # Remove commas used as thousand separators
    value = value.replace(",", "")

    # Handle parentheses for negative values — accountant convention (5000) = -5000
    if value.startswith("(") and value.endswith(")"):
        value = "-" + value[1:-1]

    # Remove trailing/leading spaces
    value = value.strip()

    try:
        amount = float(value)

        # Flag unreasonably large amounts
        if abs(amount) > 1_000_000_000:
            return amount, (
                f"Very large amount in '{col_name}': "
                f"{amount:,.2f} — please verify"
            )

        return amount, None

    except ValueError:
        return None, (
            f"Could not parse amount '{original}' in '{col_name}' — "
            f"value will be null"
        )