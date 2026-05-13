"""
Neural Ledger — Bronze Writer (Step 3)
========================================
Takes a file detected by the File Watcher and writes every row
into the bronze_transactions table.

Responsibilities:
  1. Route to the correct parser (Excel or CSV)
  2. Write every raw row as a JSON blob preserving original column names
  3. Assign a batch_id to group all rows from one file
  4. Set processing_status = 'pending' for every row
  5. Mark the Bronze rows as ready for Silver normalization
  6. Log everything to the audit trail

Rules:
  - Bronze is append-only. Never update, never delete.
  - Original column names are preserved exactly in raw_content JSON.
  - If a file fails mid-way, already-written rows stay (idempotent via hash).
  - Any error per-row is logged and skipped — never crash the whole file.
"""

import json
import uuid
import time
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

import src.database.init_db as _db_module
from src.database.init_db import file_hash
from src.phase1.ingestion.excel_parser import ExcelParser
from src.phase1.ingestion.csv_parser import CSVParser

logger = logging.getLogger(__name__)


class BronzeWriter:
    """
    Writes raw file contents into the Bronze layer.

    Usage:
        writer = BronzeWriter()
        result = writer.ingest(metadata)
        # metadata comes directly from FileProcessor in Step 2
    """

    def __init__(self):
        self._excel_parser = ExcelParser()
        self._csv_parser   = CSVParser()

    def ingest(self, file_metadata: dict) -> dict:
        """
        Main entry point — called by the pipeline after file detection.

        Args:
            file_metadata: dict from FileProcessor (Step 2) containing:
                file_path, file_name, source_type, source_software,
                detection_conf, file_hash, file_size_bytes, detected_at

        Returns:
            result dict with:
                success       — bool
                rows_written  — int
                batch_id      — UUID string
                error         — str or None
                parse_meta    — dict with parsing details
        """
        start = time.time()
        file_path    = file_metadata["file_path"]
        file_name    = file_metadata["file_name"]
        source_type  = file_metadata["source_type"]
        source_sw    = file_metadata["source_software"]
        sha256       = file_metadata["file_hash"]
        batch_id     = str(uuid.uuid4())

        logger.info(f"Bronze ingestion started: {file_name} (batch: {batch_id[:8]})")

        try:
            # Step 1: Parse the file
            rows, headers, parse_meta = self._parse_file(
                file_path, source_type
            )

            if not rows:
                logger.warning(f"No data rows found in {file_name}")
                _db_module.db.log_operation(
                    operation    = "bronze_ingest_empty",
                    source_layer = "bronze",
                    status       = "warning",
                    message      = f"No data rows: {file_name}",
                )
                return {
                    "success":      True,
                    "rows_written": 0,
                    "batch_id":     batch_id,
                    "error":        None,
                    "parse_meta":   parse_meta,
                }

            # Step 2: Write rows to Bronze
            rows_written = self._write_bronze(
                rows         = rows,
                headers      = headers,
                file_path    = file_path,
                file_name    = file_name,
                file_hash    = sha256,
                source_type  = source_type,
                source_sw    = source_sw,
                batch_id     = batch_id,
            )

            duration_ms = int((time.time() - start) * 1000)

            logger.info(
                f"Bronze complete: {file_name} — "
                f"{rows_written} rows in {duration_ms}ms "
                f"(batch: {batch_id[:8]})"
            )

            _db_module.db.log_operation(
                operation     = "bronze_ingest_success",
                source_layer  = "bronze",
                status        = "success",
                message       = (
                    f"{file_name}: {rows_written} rows written | "
                    f"batch={batch_id[:8]} | "
                    f"software={source_sw}"
                ),
                rows_affected = rows_written,
                duration_ms   = duration_ms,
            )

            return {
                "success":      True,
                "rows_written": rows_written,
                "batch_id":     batch_id,
                "error":        None,
                "parse_meta":   parse_meta,
            }

        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            logger.error(f"Bronze ingestion failed for {file_name}: {e}")

            _db_module.db.log_operation(
                operation    = "bronze_ingest_failed",
                source_layer = "bronze",
                status       = "failure",
                message      = f"{file_name}: {str(e)}",
                duration_ms  = duration_ms,
            )

            return {
                "success":      False,
                "rows_written": 0,
                "batch_id":     batch_id,
                "error":        str(e),
                "parse_meta":   {},
            }

    # ──────────────────────────────────────────────────────────────────────────
    # Parse routing
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_file(
        self,
        file_path:   str,
        source_type: str
    ):
        """Route to the correct parser based on source_type."""

        if source_type in ("xlsx", "xls"):
            return self._excel_parser.parse(file_path)

        elif source_type == "csv":
            return self._csv_parser.parse(file_path)

        else:
            raise ValueError(
                f"Unsupported source_type for Bronze ingestion: {source_type}. "
                f"PDF and JSON parsers are handled separately."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Bronze write
    # ──────────────────────────────────────────────────────────────────────────

    def _write_bronze(
        self,
        rows:        List[Dict[str, Any]],
        headers:     List[str],
        file_path:   str,
        file_name:   str,
        file_hash:   str,
        source_type: str,
        source_sw:   str,
        batch_id:    str,
    ) -> int:
        """
        Write all rows to bronze_transactions in a single transaction.

        Each row is stored as:
        - raw_content: the original row as JSON (all original column names)
        - raw_headers: the complete header list from the file
        - All metadata fields

        Returns number of rows successfully written.
        """
        headers_json = json.dumps(headers, ensure_ascii=False)
        rows_written = 0
        skipped      = 0

        with _db_module.db.connection() as conn:
            for row_number, row in enumerate(rows, start=1):
                try:
                    # Serialise the row — convert non-JSON-safe types
                    raw_content = self._serialise_row(row)

                    conn.execute(
                        """
                        INSERT INTO bronze_transactions (
                            source_type,
                            source_software,
                            source_file,
                            source_file_hash,
                            source_row_number,
                            raw_content,
                            raw_headers,
                            ingestion_batch_id,
                            processing_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        [
                            source_type,
                            source_sw,
                            file_name,
                            file_hash,
                            row_number,
                            raw_content,
                            headers_json,
                            batch_id,
                        ]
                    )
                    rows_written += 1

                except Exception as e:
                    skipped += 1
                    logger.warning(
                        f"Row {row_number} skipped in {file_name}: {e}"
                    )

        if skipped > 0:
            logger.warning(
                f"{skipped} rows skipped in {file_name} due to errors"
            )

        return rows_written

    @staticmethod
    def _serialise_row(row: Dict[str, Any]) -> str:
        """
        Convert a row dict to a JSON string.
        Handles non-JSON-serialisable types like pandas NaN, Timestamps.
        """
        import math

        def make_serialisable(value):
            if value is None:
                return None
            # Handle pandas NaN and float nan
            if isinstance(value, float) and math.isnan(value):
                return None
            # Handle pandas Timestamp
            if hasattr(value, "isoformat"):
                return value.isoformat()
            # Handle pandas NA
            try:
                import pandas as pd
                if pd.isna(value):
                    return None
            except (TypeError, ImportError):
                pass
            return value

        cleaned = {
            str(k): make_serialisable(v)
            for k, v in row.items()
        }

        return json.dumps(cleaned, ensure_ascii=False)