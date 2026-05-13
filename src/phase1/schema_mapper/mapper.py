"""
Neural Ledger — Schema Mapper Orchestrator (Step 4d)
======================================================
Ties Layers 1–4 together with the security gates.

Flow:
  Security Gate (pre-mapping)
        ↓
  Layer 1 — Known mapping lookup (bronze_schema_mappings)
        ↓
  Layer 2 — Fuzzy rule-based matching
        ↓ (if low confidence remains)
  Layer 3 — LLM assist (headers + 2 sample rows only)
        ↓ (if still unresolved)
  Layer 4 — Flag for user confirmation
        ↓
  Security Gate (post-LLM validation)
        ↓
  Silver-ready mapping dict returned

The mapper never writes to Silver directly.
It returns a mapping result that the Silver writer consumes.
"""

import json
import time
import logging
import os
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from src.phase1.schema_mapper.dictionary import (
    STANDARD_FIELDS,
    get_software_mapping,
    normalise_tx_type,
)
from src.phase1.schema_mapper.fuzzy_mapper import (
    FuzzyMapper,
    MappingResult,
    ColumnMapping,
    THRESHOLD_AUTO_ACCEPT,
    THRESHOLD_SUGGEST,
)
from src.phase1.schema_mapper.security import (
    check_file_size,
    check_row_count,
    neutralize_formulas,
    protect_reserved_names,
    apply_column_renames,
    normalize_unicode,
    validate_llm_output,
    sanitize_mapped_row,
)
import src.database.init_db as _db_module

logger = logging.getLogger(__name__)


@dataclass
class SchemaMapperResult:
    """Complete result from the Schema Mapper."""
    success:            bool
    source_software:    str
    batch_id:           str
    column_mapping:     Dict[str, str]      # {original_col: standard_field}
    rename_map:         Dict[str, str]      # {original_col: raw_col} for reserved names
    needs_user_review:  List[str]           # columns below confidence threshold
    security_warnings:  List[str]           # security gate findings
    formula_count:      int                 # number of formula cells neutralized
    coverage:           float               # 0.0–1.0 fraction of columns mapped
    has_date:           bool
    has_amount:         bool
    error:              Optional[str] = None
    llm_used:           bool = False
    llm_rejected:       List[str] = field(default_factory=list)


class SchemaMapper:
    """
    Orchestrates the full 4-layer schema mapping pipeline.

    Usage:
        mapper = SchemaMapper()
        result = mapper.map(
            columns=["Taareekh", "Raqam", "Vendor Name (City)", "Tafseelat"],
            rows=rows,       # list of raw row dicts (for LLM sample)
            source_software="manual_excel",
            batch_id="abc-123",
            file_size_bytes=24576,
        )
    """

    def __init__(self):
        self._fuzzy = FuzzyMapper()
        self._groq_api_key = os.getenv("GROQ_API_KEY", "")

    def map(
        self,
        columns:          List[str],
        rows:             List[Dict[str, Any]],
        source_software:  str,
        batch_id:         str,
        file_size_bytes:  int = 0,
    ) -> SchemaMapperResult:
        """
        Full mapping pipeline. Returns SchemaMapperResult.
        Never raises — errors are captured in result.error.
        """
        start = time.time()

        try:
            return self._run_pipeline(
                columns, rows, source_software, batch_id, file_size_bytes
            )
        except Exception as e:
            logger.error(f"Schema mapper failed for batch {batch_id[:8]}: {e}")
            return SchemaMapperResult(
                success          = False,
                source_software  = source_software,
                batch_id         = batch_id,
                column_mapping   = {},
                rename_map       = {},
                needs_user_review= columns,
                security_warnings= [],
                formula_count    = 0,
                coverage         = 0.0,
                has_date         = False,
                has_amount       = False,
                error            = str(e),
            )
        finally:
            duration = int((time.time() - start) * 1000)
            logger.info(f"Schema mapper finished in {duration}ms")

    def _run_pipeline(
        self,
        columns:         List[str],
        rows:            List[Dict[str, Any]],
        source_software: str,
        batch_id:        str,
        file_size_bytes: int,
    ) -> SchemaMapperResult:

        security_warnings = []
        llm_used          = False
        llm_rejected      = []

        # ── Pre-mapping Security Gates ─────────────────────────────────────

        # Gate 1: File size
        passed, msg = check_file_size(file_size_bytes)
        if not passed:
            return SchemaMapperResult(
                success=False, source_software=source_software,
                batch_id=batch_id, column_mapping={}, rename_map={},
                needs_user_review=columns, security_warnings=[msg],
                formula_count=0, coverage=0.0,
                has_date=False, has_amount=False, error=msg,
            )

        # Gate 2: Row count
        passed, msg = check_row_count(len(rows))
        if not passed:
            return SchemaMapperResult(
                success=False, source_software=source_software,
                batch_id=batch_id, column_mapping={}, rename_map={},
                needs_user_review=columns, security_warnings=[msg],
                formula_count=0, coverage=0.0,
                has_date=False, has_amount=False, error=msg,
            )

        # Gate 3: Formula neutralization
        rows, formula_count = neutralize_formulas(rows)
        if formula_count > 0:
            security_warnings.append(
                f"{formula_count} formula cells neutralized"
            )

        # Gate 4: Reserved name protection
        columns, rename_map = protect_reserved_names(columns)
        if rename_map:
            rows = apply_column_renames(rows, rename_map)
            security_warnings.append(
                f"Reserved column names renamed: {list(rename_map.keys())}"
            )

        # Gate 5: Unicode normalization
        rows = normalize_unicode(rows)

        # ── Load confirmed mappings from DB (Layer 1 input) ────────────────
        confirmed_mappings = self._load_confirmed_mappings(source_software)

        # ── Layers 1 + 2: Fuzzy mapping ───────────────────────────────────
        mapping_result = self._fuzzy.map_columns(
            columns          = columns,
            source_software  = source_software,
            confirmed_mappings= confirmed_mappings,
        )

        # ── Layer 3: LLM assist for low-confidence columns ─────────────────
        low_confidence_cols = [
            c for c in mapping_result.columns
            if c.standard_field is None
            or (c.confidence < THRESHOLD_AUTO_ACCEPT and not c.confirmed)
        ]

        if low_confidence_cols and self._groq_api_key:
            llm_mappings, rejected = self._llm_assist(
                low_confidence_cols, columns, rows, source_software
            )
            llm_used     = bool(llm_mappings)
            llm_rejected = rejected

            # Apply validated LLM mappings
            for col_mapping in mapping_result.columns:
                if col_mapping.original_name in llm_mappings:
                    col_mapping.standard_field = llm_mappings[col_mapping.original_name]
                    col_mapping.confidence     = 0.75
                    col_mapping.match_method   = "llm_assist"

        # ── Build final column_mapping dict ───────────────────────────────
        column_mapping = {}
        needs_user_review = []

        for col in mapping_result.columns:
            if col.standard_field and col.confidence >= THRESHOLD_AUTO_ACCEPT:
                column_mapping[col.original_name] = col.standard_field
            elif col.standard_field and col.confidence >= THRESHOLD_SUGGEST:
                # Include mapping but flag for review
                column_mapping[col.original_name] = col.standard_field
                needs_user_review.append(col.original_name)
            else:
                # No mapping or too low confidence — flag for user
                needs_user_review.append(col.original_name)

        # ── Save new confirmed mappings to DB ─────────────────────────────
        self._save_mappings(source_software, mapping_result.columns)

        # ── Audit log ─────────────────────────────────────────────────────
        coverage = mapping_result.coverage
        _db_module.db.log_operation(
            operation    = "schema_mapping_complete",
            source_layer = "silver",
            status       = "success",
            message      = (
                f"{source_software}: {len(column_mapping)} mapped, "
                f"{len(needs_user_review)} need review, "
                f"coverage={coverage:.0%}, "
                f"llm_used={llm_used}"
            ),
            rows_affected = len(column_mapping),
        )

        return SchemaMapperResult(
            success           = True,
            source_software   = source_software,
            batch_id          = batch_id,
            column_mapping    = column_mapping,
            rename_map        = rename_map,
            needs_user_review = needs_user_review,
            security_warnings = security_warnings,
            formula_count     = formula_count,
            coverage          = coverage,
            has_date          = mapping_result.has_date,
            has_amount        = mapping_result.has_amount,
            llm_used          = llm_used,
            llm_rejected      = llm_rejected,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Layer 3 — LLM assist
    # ──────────────────────────────────────────────────────────────────────────

    def _llm_assist(
        self,
        unresolved:      List[ColumnMapping],
        all_columns:     List[str],
        rows:            List[Dict[str, Any]],
        source_software: str,
    ) -> tuple:
        """
        Send unresolved columns to Groq LLM for mapping assistance.

        CRITICAL PRIVACY RULES enforced here:
        - Only column headers are sent — never full row data
        - Sample values are truncated to 50 chars max
        - Only 2 sample rows sent
        - PII-pattern values (phone numbers, IDs) are masked before sending
        - LLM output is validated by Gate 6 before use
        """
        try:
            import requests

            unresolved_cols = [c.original_name for c in unresolved]

            # Build sanitized sample (headers + 2 rows max, values truncated)
            sample_rows = []
            for row in rows[:2]:
                sample_row = {}
                for col in unresolved_cols:
                    value = row.get(col, "")
                    if value:
                        # Truncate and mask potential PII
                        str_val = str(value)[:50]
                        str_val = self._mask_pii_in_sample(str_val)
                        sample_row[col] = str_val
                sample_rows.append(sample_row)

            prompt = f"""You are a financial data schema mapper for Pakistani SME accounting.

Map these column headers to their standard financial field names.

Column headers to map: {json.dumps(unresolved_cols)}
Sample data (2 rows max, values truncated for privacy): {json.dumps(sample_rows)}
Source software: {source_software}

Valid target field names ONLY (use exactly these strings):
{json.dumps(sorted(list(STANDARD_FIELDS)))}

Respond with ONLY a JSON object like:
{{"mappings": {{"ColumnName": "standard_field", ...}}}}

Rules:
- Only map columns you are confident about
- Use null for columns you cannot map
- Do not invent new field names
- Do not add any explanation outside the JSON"""

            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._groq_api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "llama-3.3-70b-versatile",
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  300,
                    "temperature": 0.0,  # Deterministic output
                },
                timeout=10,
            )

            if response.status_code != 200:
                logger.warning(f"LLM API returned {response.status_code}")
                return {}, [f"LLM API error: {response.status_code}"]

            content = response.json()["choices"][0]["message"]["content"]

            # Parse and validate LLM output (Gate 6)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract JSON from response
                import re
                json_match = re.search(r"\{.*\}", content, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group())
                else:
                    return {}, ["LLM output was not valid JSON"]

            valid_mappings, rejected = validate_llm_output(parsed, all_columns)
            return valid_mappings, rejected

        except Exception as e:
            logger.warning(f"LLM assist failed (non-fatal): {e}")
            return {}, [str(e)]

    @staticmethod
    def _mask_pii_in_sample(value: str) -> str:
        """Mask potential PII patterns in sample values before sending to LLM."""
        import re
        # Phone numbers
        value = re.sub(r"\b0\d{10}\b", "[PHONE]", value)
        value = re.sub(r"\b\+92\d{10}\b", "[PHONE]", value)
        # NTN numbers (7 digits)
        value = re.sub(r"\b\d{7}\b", "[NTN]", value)
        # CNIC (13 digits)
        value = re.sub(r"\b\d{5}-\d{7}-\d\b", "[CNIC]", value)
        # Bank account numbers (10-16 digits)
        value = re.sub(r"\b\d{10,16}\b", "[ACCOUNT]", value)
        return value

    # ──────────────────────────────────────────────────────────────────────────
    # DB integration — Layer 1 input and mapping persistence
    # ──────────────────────────────────────────────────────────────────────────

    def _load_confirmed_mappings(self, source_software: str) -> Dict[str, str]:
        """Load all previously confirmed mappings for this software from DB."""
        try:
            with _db_module.db.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT original_column, standard_field
                    FROM bronze_schema_mappings
                    WHERE source_software = ?
                    AND (confirmed_by_user = TRUE OR confidence >= 0.85)
                    ORDER BY times_seen DESC
                    """,
                    [source_software]
                ).fetchall()
                return {row[0]: row[1] for row in rows}
        except Exception as e:
            logger.warning(f"Could not load confirmed mappings: {e}")
            return {}

    def _save_mappings(
        self,
        source_software: str,
        columns:         List[ColumnMapping],
    ) -> None:
        """
        Persist new mappings to bronze_schema_mappings.
        Only saves high-confidence non-user-confirmed mappings for future learning.
        """
        try:
            with _db_module.db.connection() as conn:
                for col in columns:
                    if not col.standard_field:
                        continue
                    if col.confidence < THRESHOLD_SUGGEST:
                        continue

                    # Upsert — update times_seen if exists, insert if new
                    existing = conn.execute(
                        """
                        SELECT mapping_id, times_seen
                        FROM bronze_schema_mappings
                        WHERE source_software = ? AND original_column = ?
                        """,
                        [source_software, col.original_name]
                    ).fetchone()

                    if existing:
                        conn.execute(
                            """
                            UPDATE bronze_schema_mappings
                            SET times_seen = times_seen + 1,
                                updated_at = NOW()
                            WHERE source_software = ? AND original_column = ?
                            """,
                            [source_software, col.original_name]
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO bronze_schema_mappings
                                (source_software, original_column, standard_field,
                                 confidence, confirmed_by_user, times_seen)
                            VALUES (?, ?, ?, ?, FALSE, 1)
                            """,
                            [
                                source_software,
                                col.original_name,
                                col.standard_field,
                                col.confidence,
                            ]
                        )
        except Exception as e:
            logger.warning(f"Could not save mappings to DB (non-fatal): {e}")