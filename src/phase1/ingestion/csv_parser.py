"""
Neural Ledger — CSV Parser
============================
Reads CSV files into raw row dicts with full encoding detection.

Challenges specific to Pakistani accounting CSV exports:
  - Windows-1252 encoding (most Windows-generated CSVs)
  - UTF-8 with BOM (Excel "Save as CSV" on Windows)
  - Mixed Urdu/English content in description columns
  - Tab-separated files saved with .csv extension
  - Semicolon-delimited files from European accounting software
  - Inconsistent quoting
  - Blank rows and summary rows mixed with data
"""

import csv
import logging
import io
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import chardet

logger = logging.getLogger(__name__)

# Encoding detection: read first 50KB for accuracy without loading full file
ENCODING_SAMPLE_BYTES = 50_000

# Confidence threshold below which we try multiple encodings
ENCODING_CONFIDENCE_THRESHOLD = 0.80

# Fallback encoding chain — tried in order if detection fails
FALLBACK_ENCODINGS = [
    "utf-8-sig",    # UTF-8 with BOM (Excel CSV on Windows)
    "utf-8",        # Standard UTF-8
    "cp1252",       # Windows-1252 (most Windows CSV exports)
    "latin-1",      # ISO-8859-1 (accepts all byte values — last resort)
]


class CSVParser:
    """
    Parses CSV files into a list of raw row dicts.

    Handles encoding detection, delimiter sniffing, and
    blank/summary row removal automatically.

    Example output row:
        {
            "Date":        "15/01/2024",
            "Amount":      "5000",
            "Vendor":      "Shell Clifton",
            "Description": "Fuel purchase",
        }
    """

    def parse(self, file_path: str) -> Tuple[List[Dict[str, Any]], List[str], dict]:
        """
        Parse a CSV file.

        Returns:
            rows        — list of raw row dicts (original column names)
            headers     — list of original column header strings
            parse_meta  — dict with encoding, delimiter, row count etc.
        """
        path = Path(file_path)
        logger.info(f"Parsing CSV: {path.name}")

        # Step 1: Detect encoding
        encoding, enc_confidence = self._detect_encoding(file_path)
        logger.info(
            f"Encoding detected: {encoding} "
            f"(confidence: {enc_confidence:.0%})"
        )

        # Step 2: Read raw content with detected encoding
        content = self._read_with_encoding(file_path, encoding)
        if content is None:
            raise ValueError(
                f"Could not decode {path.name} with any known encoding"
            )

        # Step 3: Sniff delimiter
        delimiter = self._detect_delimiter(content)
        logger.info(f"Delimiter detected: {repr(delimiter)}")

        # Step 4: Parse CSV
        rows, headers = self._parse_content(content, delimiter)

        # Step 5: Clean rows
        rows = self._clean_rows(rows, headers)

        parse_meta = {
            "encoding":          encoding,
            "encoding_confidence": enc_confidence,
            "delimiter":         delimiter,
            "total_rows":        len(rows),
            "total_cols":        len(headers),
            "parse_method":      "chardet_csv",
        }

        logger.info(
            f"CSV parsed: {len(rows)} rows, {len(headers)} cols "
            f"from {path.name}"
        )

        return rows, headers, parse_meta

    # ──────────────────────────────────────────────────────────────────────────
    # Encoding detection
    # ──────────────────────────────────────────────────────────────────────────

    def _detect_encoding(self, file_path: str) -> Tuple[str, float]:
        """
        Detect file encoding using chardet.

        Reads up to ENCODING_SAMPLE_BYTES for detection.
        Falls back to utf-8-sig if confidence is low.
        """
        with open(file_path, "rb") as f:
            raw = f.read(ENCODING_SAMPLE_BYTES)

        result = chardet.detect(raw)
        encoding = result.get("encoding") or "utf-8"
        confidence = result.get("confidence") or 0.0

        # Normalise common aliases
        encoding = self._normalise_encoding(encoding)

        # Low confidence — prefer utf-8-sig (handles Windows BOM)
        if confidence < ENCODING_CONFIDENCE_THRESHOLD:
            logger.debug(
                f"Low encoding confidence ({confidence:.0%}), "
                f"defaulting to utf-8-sig"
            )
            encoding = "utf-8-sig"

        return encoding, confidence

    @staticmethod
    def _normalise_encoding(encoding: str) -> str:
        """Normalise encoding names to Python-standard names."""
        mapping = {
            "ascii":        "utf-8",
            "iso-8859-1":   "latin-1",
            "iso-8859-2":   "latin-2",
            "windows-1252": "cp1252",
            "gb2312":       "gbk",
        }
        return mapping.get(encoding.lower(), encoding)

    def _read_with_encoding(
        self, file_path: str, primary_encoding: str
    ) -> Optional[str]:
        """
        Read file content as string, trying encodings in order.
        Returns None only if all encodings fail.
        """
        encodings_to_try = [primary_encoding] + [
            e for e in FALLBACK_ENCODINGS if e != primary_encoding
        ]

        for encoding in encodings_to_try:
            try:
                with open(file_path, "r", encoding=encoding, errors="strict") as f:
                    content = f.read()
                logger.debug(f"Successfully read with encoding: {encoding}")
                return content
            except (UnicodeDecodeError, LookupError):
                continue

        # Absolute last resort — replace undecodable characters
        logger.warning(
            f"All encodings failed for {Path(file_path).name}. "
            f"Reading with replacement characters."
        )
        try:
            with open(file_path, "r", encoding="latin-1", errors="replace") as f:
                return f.read()
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Delimiter detection
    # ──────────────────────────────────────────────────────────────────────────

    def _detect_delimiter(self, content: str) -> str:
        """
        Detect the delimiter used in the CSV content.

        Uses Python's csv.Sniffer first, then falls back to
        frequency analysis of common delimiters.
        """
        # Take first 4096 chars for sniffing
        sample = content[:4096]

        # Try csv.Sniffer
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            return dialect.delimiter
        except csv.Error:
            pass

        # Fallback: count occurrences of common delimiters in first 5 lines
        first_lines = sample.split("\n")[:5]
        joined = "\n".join(first_lines)

        candidates = {
            ",": joined.count(","),
            "\t": joined.count("\t"),
            ";": joined.count(";"),
            "|": joined.count("|"),
        }

        best = max(candidates, key=candidates.get)
        if candidates[best] > 0:
            return best

        # Default to comma
        return ","

    # ──────────────────────────────────────────────────────────────────────────
    # CSV parsing
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_content(
        self, content: str, delimiter: str
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Parse CSV content string into rows and headers."""

        reader = csv.DictReader(
            io.StringIO(content),
            delimiter=delimiter,
            quoting=csv.QUOTE_MINIMAL,
            skipinitialspace=True,
        )

        # Strip whitespace from headers
        if reader.fieldnames:
            reader.fieldnames = [
                str(h).strip() for h in reader.fieldnames
            ]

        headers = list(reader.fieldnames) if reader.fieldnames else []

        rows = []
        for row in reader:
            # Strip whitespace from all values
            cleaned = {
                k.strip() if k else k: v.strip() if isinstance(v, str) else v
                for k, v in row.items()
            }
            rows.append(cleaned)

        return rows, headers

    # ──────────────────────────────────────────────────────────────────────────
    # Row cleaning
    # ──────────────────────────────────────────────────────────────────────────

    def _clean_rows(
        self,
        rows: List[Dict[str, Any]],
        headers: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Remove blank rows and obvious summary/total rows.

        Blank row: all values are empty strings or None
        Total row: first column contains "total", "sub-total", "grand total"
        """
        cleaned = []
        total_keywords = {
            "total", "sub-total", "subtotal", "grand total",
            "sum", "balance", "closing balance", "opening balance",
            "kul", "majmua",  # Urdu total keywords
        }

        for row in rows:
            values = list(row.values())

            # Skip blank rows
            if all(
                v is None or str(v).strip() == ""
                for v in values
            ):
                continue

            # Skip total/summary rows based on first non-empty column
            first_val = ""
            for v in values:
                if v and str(v).strip():
                    first_val = str(v).strip().lower()
                    break

            if first_val in total_keywords:
                logger.debug(f"Skipping summary row: {first_val}")
                continue

            cleaned.append(row)

        return cleaned