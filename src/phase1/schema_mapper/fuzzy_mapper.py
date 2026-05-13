"""
Neural Ledger — Fuzzy Schema Mapper (Step 4b)
================================================
Layers 1 and 2 of the hybrid mapping strategy.

Layer 1 — Known mapping lookup
  Checks bronze_schema_mappings for previously confirmed mappings
  for this source_software. If confidence >= 0.85, use it silently.
  If user has confirmed it (confirmed_by_user=True), always use it.

Layer 2 — Fuzzy rule-based matching
  Matches against the Pakistani column dictionary using:
    1. Exact match (case-insensitive) — confidence 0.95+
    2. Normalized match (strip spaces, punctuation) — confidence 0.85
    3. Fuzzy string similarity — confidence proportional to score
    4. Substring match — confidence 0.70

Returns a MappingResult for each column with confidence scores.
The orchestrator in mapper.py decides whether to proceed or escalate
to Layer 3 (LLM) or Layer 4 (user confirmation).
"""

import re
import unicodedata
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field

from src.phase1.schema_mapper.dictionary import (
    get_software_mapping,
    get_all_general_mappings,
    get_all_known_columns,
    is_reserved,
    STANDARD_FIELDS,
)

logger = logging.getLogger(__name__)

# Confidence thresholds
THRESHOLD_AUTO_ACCEPT  = 0.85   # Auto-map without user confirmation
THRESHOLD_SUGGEST      = 0.60   # Suggest to user for confirmation
THRESHOLD_REJECT       = 0.40   # Below this — send to LLM or user


@dataclass
class ColumnMapping:
    """Result of mapping a single column."""
    original_name:    str
    standard_field:   Optional[str]      # Target field name or None
    confidence:       float              # 0.0 to 1.0
    match_method:     str               # How the match was found
    confirmed:        bool = False      # True if user-confirmed
    is_reserved:      bool = False      # True if name conflicts with internals
    renamed_to:       Optional[str] = None  # If reserved, renamed to raw_X


@dataclass
class MappingResult:
    """Complete mapping result for one file."""
    source_software:  str
    columns:          List[ColumnMapping] = field(default_factory=list)
    unmapped:         List[str]           = field(default_factory=list)
    needs_review:     List[str]           = field(default_factory=list)

    @property
    def auto_mapped_count(self) -> int:
        return sum(1 for c in self.columns if c.confidence >= THRESHOLD_AUTO_ACCEPT)

    @property
    def coverage(self) -> float:
        """Percentage of columns that have a mapping."""
        if not self.columns:
            return 0.0
        mapped = sum(1 for c in self.columns if c.standard_field is not None)
        return mapped / len(self.columns)

    @property
    def has_date(self) -> bool:
        return any(c.standard_field == "transaction_date" for c in self.columns)

    @property
    def has_amount(self) -> bool:
        return any(
            c.standard_field in ("net_amount", "amount_debit", "amount_credit")
            for c in self.columns
        )


class FuzzyMapper:
    """
    Maps raw column names to Neural Ledger standard fields.

    Usage:
        mapper = FuzzyMapper()
        result = mapper.map_columns(
            columns=["Taareekh", "Raqam", "Shell", "Tafseelat"],
            source_software="manual_excel",
            batch_id="abc-123"
        )
        for col in result.columns:
            print(col.original_name, "→", col.standard_field, f"({col.confidence:.0%})")
    """

    def __init__(self):
        # Build lookup table from dictionary
        self._exact_lookup:      Dict[str, tuple] = {}   # normalized → (target, confidence, method)
        self._general_lookup:    Dict[str, tuple] = {}
        self._all_known:         List[tuple] = []        # (original, target, confidence)

        self._build_lookup_tables()

    def _build_lookup_tables(self) -> None:
        """Pre-build normalized lookup tables for fast matching."""
        for original, target, confidence in get_all_known_columns():
            normalized = self._normalize(original)
            # Keep highest-confidence entry for each normalized form
            existing = self._exact_lookup.get(normalized)
            if existing is None or existing[2] < confidence:
                self._exact_lookup[normalized] = (original, target, confidence)

        self._all_known = get_all_known_columns()
        logger.debug(
            f"Fuzzy mapper built with {len(self._exact_lookup)} "
            f"normalized lookup entries"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def map_columns(
        self,
        columns:         List[str],
        source_software: str,
        confirmed_mappings: Optional[Dict[str, str]] = None,
    ) -> MappingResult:
        """
        Map a list of raw column names to standard fields.

        Args:
            columns:            list of original column names from file
            source_software:    detected software (drives Layer 1)
            confirmed_mappings: dict of {original_col: standard_field}
                                from bronze_schema_mappings (Layer 1 input)

        Returns:
            MappingResult with per-column ColumnMapping objects
        """
        result = MappingResult(source_software=source_software)
        software_map = get_software_mapping(source_software)
        confirmed = confirmed_mappings or {}

        for col in columns:
            mapping = self._map_single_column(col, software_map, confirmed)
            result.columns.append(mapping)

            if mapping.standard_field is None:
                result.unmapped.append(col)
            elif mapping.confidence < THRESHOLD_AUTO_ACCEPT and not mapping.confirmed:
                result.needs_review.append(col)

        logger.info(
            f"Mapping complete: {result.auto_mapped_count}/{len(columns)} "
            f"auto-mapped, {len(result.unmapped)} unmapped, "
            f"{len(result.needs_review)} need review"
        )
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Single column mapping
    # ──────────────────────────────────────────────────────────────────────────

    def _map_single_column(
        self,
        column:       str,
        software_map: Dict[str, str],
        confirmed:    Dict[str, str],
    ) -> ColumnMapping:
        """Map one column through all matching strategies in priority order."""

        # Security check — reserved name
        if is_reserved(column):
            renamed = f"raw_{column}"
            logger.warning(f"Reserved column name detected: '{column}' → renamed to '{renamed}'")
            return ColumnMapping(
                original_name = column,
                standard_field= None,
                confidence    = 0.0,
                match_method  = "reserved_rename",
                is_reserved   = True,
                renamed_to    = renamed,
            )

        # Layer 1a — User-confirmed mapping
        if column in confirmed:
            return ColumnMapping(
                original_name = column,
                standard_field= confirmed[column],
                confidence    = 1.0,
                match_method  = "user_confirmed",
                confirmed     = True,
            )

        # Layer 1b — Software-specific exact match
        if column in software_map:
            return ColumnMapping(
                original_name = column,
                standard_field= software_map[column],
                confidence    = 0.95,
                match_method  = "software_exact",
            )

        # Layer 2a — Exact case-insensitive match
        result = self._exact_match(column)
        if result:
            return result

        # Layer 2b — Normalized match (strip punctuation/spaces)
        result = self._normalized_match(column)
        if result:
            return result

        # Layer 2c — Fuzzy similarity match
        result = self._fuzzy_match(column)
        if result:
            return result

        # Layer 2d — Substring / partial match
        result = self._substring_match(column)
        if result:
            return result

        # No match found
        return ColumnMapping(
            original_name = column,
            standard_field= None,
            confidence    = 0.0,
            match_method  = "no_match",
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Matching strategies
    # ──────────────────────────────────────────────────────────────────────────

    def _exact_match(self, column: str) -> Optional[ColumnMapping]:
        """Case-insensitive exact match against all known column names."""
        col_lower = column.strip().lower()
        for original, target, confidence in self._all_known:
            if original.lower() == col_lower:
                return ColumnMapping(
                    original_name = column,
                    standard_field= target,
                    confidence    = confidence,
                    match_method  = "exact_case_insensitive",
                )
        return None

    def _normalized_match(self, column: str) -> Optional[ColumnMapping]:
        """Match after normalizing: strip spaces, punctuation, unicode."""
        normalized = self._normalize(column)
        if normalized in self._exact_lookup:
            _, target, confidence = self._exact_lookup[normalized]
            # Slight confidence reduction for normalized match
            return ColumnMapping(
                original_name = column,
                standard_field= target,
                confidence    = min(confidence, 0.85),
                match_method  = "normalized_match",
            )
        return None

    def _fuzzy_match(self, column: str) -> Optional[ColumnMapping]:
        """
        Fuzzy string similarity using character n-gram overlap.
        Lightweight — no external library needed.
        """
        normalized = self._normalize(column)
        if len(normalized) < 2:
            return None

        best_target    = None
        best_score     = 0.0
        best_original  = None

        for original, target, base_confidence in self._all_known:
            orig_normalized = self._normalize(original)
            if len(orig_normalized) < 2:
                continue

            similarity = self._ngram_similarity(normalized, orig_normalized, n=2)
            # Scale similarity by base confidence
            score = similarity * base_confidence

            if score > best_score:
                best_score    = score
                best_target   = target
                best_original = original

        if best_score >= THRESHOLD_SUGGEST:
            return ColumnMapping(
                original_name = column,
                standard_field= best_target,
                confidence    = round(best_score, 3),
                match_method  = f"fuzzy_ngram (best: {best_original})",
            )
        return None

    def _substring_match(self, column: str) -> Optional[ColumnMapping]:
        """
        Check if a known column name is a substring of this column or vice versa.
        Handles cases like "Vendor Name (City)" containing "Vendor Name".
        """
        col_lower = column.strip().lower()

        best_target = None
        best_score  = 0.0

        for original, target, confidence in self._all_known:
            orig_lower = original.lower()

            # Column contains known name
            if orig_lower in col_lower and len(orig_lower) >= 3:
                score = (len(orig_lower) / len(col_lower)) * confidence * 0.80
                if score > best_score:
                    best_score  = score
                    best_target = target

            # Known name contains column (short column is prefix of known name)
            elif col_lower in orig_lower and len(col_lower) >= 3:
                score = (len(col_lower) / len(orig_lower)) * confidence * 0.75
                if score > best_score:
                    best_score  = score
                    best_target = target

        if best_score >= THRESHOLD_REJECT:
            return ColumnMapping(
                original_name = column,
                standard_field= best_target,
                confidence    = round(best_score, 3),
                match_method  = "substring_match",
            )
        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Utilities
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize(text: str) -> str:
        """
        Normalize text for comparison:
        - Unicode NFC normalization
        - Lowercase
        - Remove punctuation and extra spaces
        - Strip common suffixes like (PKR), (Rs), etc.
        """
        if not text:
            return ""
        # Unicode normalization
        text = unicodedata.normalize("NFC", text)
        # Lowercase
        text = text.lower()
        # Remove parenthetical content like "(PKR)" or "(city)"
        text = re.sub(r"\([^)]*\)", "", text)
        # Remove punctuation except letters, digits, spaces
        text = re.sub(r"[^\w\s]", "", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _ngram_similarity(a: str, b: str, n: int = 2) -> float:
        """
        Compute n-gram overlap similarity between two strings.
        Returns float 0.0–1.0.
        """
        def ngrams(s, n):
            return set(s[i:i+n] for i in range(len(s) - n + 1))

        a_grams = ngrams(a, n)
        b_grams = ngrams(b, n)

        if not a_grams or not b_grams:
            return 0.0

        intersection = a_grams & b_grams
        union = a_grams | b_grams
        return len(intersection) / len(union)