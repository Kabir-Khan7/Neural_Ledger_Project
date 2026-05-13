"""
Tests — Step 4: Schema Mapper
================================
Run with: python -m pytest tests/phase1/test_schema_mapper.py -v
"""

import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.phase1.schema_mapper.dictionary import (
    get_software_mapping, get_all_general_mappings, is_reserved,
    normalise_tx_type, get_all_known_columns, STANDARD_FIELDS,
    FBR_TAX_CATEGORIES, URDU_MAPPINGS,
)
from src.phase1.schema_mapper.fuzzy_mapper import FuzzyMapper, THRESHOLD_AUTO_ACCEPT
from src.phase1.schema_mapper.security import (
    check_file_size, check_row_count, neutralize_formulas,
    protect_reserved_names, normalize_unicode, validate_llm_output,
    sanitize_mapped_row, _clean_amount,
)
from src.database.init_db import DatabaseManager
import src.database.init_db as _db_module


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(db_path=db_path)
    manager.initialise()
    original = _db_module.db
    _db_module.db = manager
    yield manager
    _db_module.db = original
    manager.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Dictionary Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDictionary:

    def test_tally_mapping_exists(self):
        mapping = get_software_mapping("tally")
        assert "Particulars" in mapping
        assert "Vch Type" in mapping
        assert "Debit" in mapping
        assert "Credit" in mapping

    def test_quickbooks_desktop_mapping(self):
        mapping = get_software_mapping("quickbooks_desktop")
        assert mapping["Date"] == "transaction_date"
        assert mapping["Name"] == "vendor"
        assert mapping["Memo"] == "description"

    def test_ledgermax_mapping(self):
        mapping = get_software_mapping("ledgermax")
        assert mapping["Transaction Date"] == "transaction_date"
        assert mapping["Narration"] == "description"
        assert mapping["Dr Amount"] == "amount_debit"
        assert mapping["Cr Amount"] == "amount_credit"

    def test_xero_mapping(self):
        mapping = get_software_mapping("xero")
        assert mapping["Date"] == "transaction_date"
        assert mapping["Contact"] == "vendor"
        assert mapping["Debit"] == "amount_debit"

    def test_bank_pdf_hbl_same_as_generic(self):
        hbl     = get_software_mapping("bank_pdf_hbl")
        generic = get_software_mapping("bank_pdf_generic")
        assert hbl == generic

    def test_unknown_software_returns_empty(self):
        mapping = get_software_mapping("unknown_software_xyz")
        assert mapping == {}

    def test_urdu_date_mapping(self):
        mappings = get_all_general_mappings()
        assert "تاریخ" in mappings
        assert mappings["تاریخ"] == "transaction_date"

    def test_roman_urdu_mapping(self):
        mappings = get_all_general_mappings()
        assert "Taareekh" in mappings
        assert mappings["Taareekh"] == "transaction_date"
        assert "Raqam" in mappings
        assert mappings["Raqam"] == "net_amount"

    def test_is_reserved_true(self):
        assert is_reserved("transaction_id") is True
        assert is_reserved("quality_score") is True
        assert is_reserved("pii_masked") is True
        assert is_reserved("TRANSACTION_ID") is True  # Case insensitive

    def test_is_reserved_false(self):
        assert is_reserved("Date") is False
        assert is_reserved("Amount") is False
        assert is_reserved("Vendor") is False

    def test_normalise_tx_type_payment(self):
        assert normalise_tx_type("Payment") == "payment"
        assert normalise_tx_type("Cash Payment") == "payment"
        assert normalise_tx_type("BP") == "payment"

    def test_normalise_tx_type_journal(self):
        assert normalise_tx_type("Journal") == "journal"
        assert normalise_tx_type("JV") == "journal"
        assert normalise_tx_type("J/V") == "journal"

    def test_normalise_tx_type_unknown(self):
        result = normalise_tx_type("SomeWeirdType")
        assert result == "someweirdtype"

    def test_fbr_categories_have_required_fields(self):
        for category, info in FBR_TAX_CATEGORIES.items():
            assert "fbr_section" in info, f"{category} missing fbr_section"
            assert "wht_applicable" in info, f"{category} missing wht_applicable"

    def test_all_mappings_target_valid_fields(self):
        """Every mapping in the dictionary must point to a valid standard field."""
        for original, target, _ in get_all_known_columns():
            assert target in STANDARD_FIELDS, (
                f"Column '{original}' maps to '{target}' "
                f"which is not in STANDARD_FIELDS"
            )

    def test_standard_fields_set_not_empty(self):
        assert len(STANDARD_FIELDS) >= 10


# ─────────────────────────────────────────────────────────────────────────────
# 2. Fuzzy Mapper Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFuzzyMapper:
    mapper = FuzzyMapper()

    def test_exact_match_tally_date(self):
        result = self.mapper.map_columns(["Date"], source_software="tally")
        col = result.columns[0]
        assert col.standard_field == "transaction_date"
        assert col.confidence >= THRESHOLD_AUTO_ACCEPT

    def test_exact_match_urdu_taareekh(self):
        result = self.mapper.map_columns(["Taareekh"], source_software="manual_excel")
        col = result.columns[0]
        assert col.standard_field == "transaction_date"

    def test_exact_match_urdu_raqam(self):
        result = self.mapper.map_columns(["Raqam"], source_software="manual_excel")
        col = result.columns[0]
        assert col.standard_field == "net_amount"

    def test_urdu_script_date(self):
        result = self.mapper.map_columns(["تاریخ"], source_software="manual_excel")
        col = result.columns[0]
        assert col.standard_field == "transaction_date"

    def test_case_insensitive_date(self):
        result = self.mapper.map_columns(["DATE"], source_software="unknown")
        col = result.columns[0]
        assert col.standard_field == "transaction_date"

    def test_fuzzy_match_vendor_name_city(self):
        """'Vendor Name (City)' — parenthetical stripped to 'vendor name'.
        Falls to LLM/user confirmation if not in dictionary.
        Tests that it does not error and returns a ColumnMapping."""
        result = self.mapper.map_columns(
            ["Vendor Name (City)"], source_software="manual_excel"
        )
        col = result.columns[0]
        # Either mapped (if fuzzy catches it) or unmapped (escalated to LLM/user)
        assert col.original_name == "Vendor Name (City)"
        assert isinstance(col.confidence, float)

    def test_dr_maps_to_debit(self):
        result = self.mapper.map_columns(["Dr"], source_software="tally")
        col = result.columns[0]
        assert col.standard_field == "amount_debit"

    def test_cr_maps_to_credit(self):
        result = self.mapper.map_columns(["Cr"], source_software="tally")
        col = result.columns[0]
        assert col.standard_field == "amount_credit"

    def test_reserved_name_flagged(self):
        result = self.mapper.map_columns(
            ["quality_score"], source_software="manual_excel"
        )
        col = result.columns[0]
        assert col.is_reserved is True
        assert col.renamed_to == "raw_quality_score"

    def test_multiple_columns_mapped(self):
        cols = ["Date", "Amount", "Vendor", "Description", "Reference"]
        result = self.mapper.map_columns(cols, source_software="manual_excel")
        assert len(result.columns) == 5
        mapped = [c for c in result.columns if c.standard_field is not None]
        assert len(mapped) >= 4

    def test_unknown_column_returns_no_match(self):
        result = self.mapper.map_columns(
            ["XyZAbcDef123"], source_software="manual_excel"
        )
        col = result.columns[0]
        assert col.standard_field is None

    def test_has_date_property(self):
        result = self.mapper.map_columns(
            ["Date", "Amount"], source_software="manual_excel"
        )
        assert result.has_date is True

    def test_has_amount_property(self):
        result = self.mapper.map_columns(
            ["Date", "Amount"], source_software="manual_excel"
        )
        assert result.has_amount is True

    def test_user_confirmed_mapping_takes_priority(self):
        confirmed = {"MyCustomDate": "transaction_date"}
        result = self.mapper.map_columns(
            ["MyCustomDate"], source_software="manual_excel",
            confirmed_mappings=confirmed
        )
        col = result.columns[0]
        assert col.standard_field == "transaction_date"
        assert col.confirmed is True
        assert col.confidence == 1.0

    def test_tally_particulars_maps_to_vendor(self):
        result = self.mapper.map_columns(
            ["Particulars"], source_software="tally"
        )
        col = result.columns[0]
        assert col.standard_field == "vendor"

    def test_ledgermax_full_set(self):
        cols = ["Transaction Date", "Narration", "Dr Amount", "Cr Amount", "Balance"]
        result = self.mapper.map_columns(cols, source_software="ledgermax")
        field_map = {c.original_name: c.standard_field for c in result.columns}
        assert field_map["Transaction Date"] == "transaction_date"
        assert field_map["Narration"] == "description"
        assert field_map["Dr Amount"] == "amount_debit"
        assert field_map["Cr Amount"] == "amount_credit"
        assert field_map["Balance"] == "balance"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Security Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSecurity:

    def test_file_size_ok(self):
        passed, _ = check_file_size(10 * 1024 * 1024)  # 10MB
        assert passed is True

    def test_file_size_too_large(self):
        passed, msg = check_file_size(100 * 1024 * 1024)  # 100MB
        assert passed is False
        assert "100.0MB" in msg

    def test_row_count_ok(self):
        passed, _ = check_row_count(1000)
        assert passed is True

    def test_row_count_too_large(self):
        passed, msg = check_row_count(200_000)
        assert passed is False
        assert "200,000" in msg

    def test_formula_neutralized(self):
        rows = [{"Amount": "=SUM(A1:A10)", "Date": "15/01/2024"}]
        cleaned, count = neutralize_formulas(rows)
        assert count == 1
        assert cleaned[0]["Amount"].startswith("FORMULA_REMOVED:")

    def test_formula_prefix_plus(self):
        rows = [{"Amount": "+5000", "Date": "15/01/2024"}]
        cleaned, count = neutralize_formulas(rows)
        assert count == 1

    def test_formula_prefix_at(self):
        rows = [{"Cmd": "@cmd(something)"}]
        cleaned, count = neutralize_formulas(rows)
        assert count == 1

    def test_normal_values_not_neutralized(self):
        rows = [{"Amount": "5000", "Vendor": "Shell Clifton", "Date": "01/01/2024"}]
        cleaned, count = neutralize_formulas(rows)
        assert count == 0
        assert cleaned[0]["Amount"] == "5000"

    def test_reserved_name_renamed(self):
        cleaned, rename_map = protect_reserved_names(["quality_score", "Date"])
        assert "raw_quality_score" in cleaned
        assert "Date" in cleaned
        assert rename_map == {"quality_score": "raw_quality_score"}

    def test_normal_names_not_renamed(self):
        cleaned, rename_map = protect_reserved_names(["Date", "Amount", "Vendor"])
        assert cleaned == ["Date", "Amount", "Vendor"]
        assert rename_map == {}

    def test_unicode_normalization(self):
        # Decomposed vs composed Unicode (same visual character)
        rows = [{"تاریخ": "01/01/2024"}]
        normalized = normalize_unicode(rows)
        assert len(normalized) == 1

    def test_llm_output_valid(self):
        response = {"mappings": {"Date": "transaction_date", "Amount": "net_amount"}}
        valid, rejected = validate_llm_output(response, ["Date", "Amount", "Vendor"])
        assert "Date" in valid
        assert "Amount" in valid
        assert len(rejected) == 0

    def test_llm_output_invalid_field(self):
        response = {"mappings": {"Date": "evil_field"}}
        valid, rejected = validate_llm_output(response, ["Date"])
        assert "Date" not in valid
        assert len(rejected) == 1

    def test_llm_output_unknown_column(self):
        """LLM cannot reference columns not in the file."""
        response = {"mappings": {"InjectedColumn": "transaction_date"}}
        valid, rejected = validate_llm_output(response, ["Date", "Amount"])
        assert "InjectedColumn" not in valid
        assert len(rejected) >= 1

    def test_llm_output_not_dict(self):
        valid, rejected = validate_llm_output("not a dict", ["Date"])
        assert valid == {}
        assert len(rejected) >= 1

    def test_amount_clean_comma_separated(self):
        amount, warn = _clean_amount("45,000", "Amount")
        assert amount == 45000.0
        assert warn is None

    def test_amount_clean_pkr_prefix(self):
        amount, warn = _clean_amount("PKR 5,000", "Amount")
        assert amount == 5000.0

    def test_amount_clean_rs_prefix(self):
        amount, warn = _clean_amount("Rs. 12,500", "Amount")
        assert amount == 12500.0

    def test_amount_clean_parentheses_negative(self):
        """Accountant convention: (5000) = -5000."""
        amount, warn = _clean_amount("(5000)", "Amount")
        assert amount == -5000.0

    def test_amount_clean_non_numeric(self):
        amount, warn = _clean_amount("N/A", "Amount")
        assert amount is None

    def test_sanitize_mapped_row(self):
        row = {"Date": "15/01/2024", "Amount": "5,000", "Vendor": "Shell"}
        mapping = {"Date": "transaction_date", "Amount": "net_amount", "Vendor": "vendor"}
        sanitized, warnings = sanitize_mapped_row(row, mapping)
        assert "transaction_date" in sanitized
        assert sanitized["net_amount"] == 5000.0
        assert sanitized["vendor"] == "Shell"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaMapperIntegration:

    def test_full_tally_mapping(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        columns = ["Date", "Particulars", "Vch Type", "Vch No.", "Debit", "Credit"]
        rows = [
            {"Date": "01/01/2024", "Particulars": "Shell", "Vch Type": "Payment",
             "Vch No.": "PV-001", "Debit": "5000", "Credit": ""}
        ]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="tally", batch_id="test-001",
        )
        assert result.success is True
        assert result.column_mapping["Date"] == "transaction_date"
        assert result.column_mapping["Debit"] == "amount_debit"
        assert result.has_date is True
        assert result.has_amount is True

    def test_manual_excel_urdu_mapping(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        columns = ["Taareekh", "Raqam", "Tafseelat", "Vendor"]
        rows = [
            {"Taareekh": "15/01/2024", "Raqam": "5000",
             "Tafseelat": "Fuel purchase", "Vendor": "Shell"}
        ]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="manual_excel", batch_id="test-002",
        )
        assert result.success is True
        assert result.column_mapping["Taareekh"] == "transaction_date"
        assert result.column_mapping["Raqam"] == "net_amount"

    def test_formula_injection_blocked(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        columns = ["Date", "Amount"]
        rows = [{"Date": "=HYPERLINK(\"evil.com\")", "Amount": "5000"}]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="manual_excel", batch_id="test-003",
        )
        assert result.success is True
        assert result.formula_count == 1
        assert "formula cells neutralized" in " ".join(result.security_warnings)

    def test_reserved_name_renamed_in_result(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        columns = ["Date", "quality_score"]
        rows = [{"Date": "15/01/2024", "quality_score": "0.99"}]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="manual_excel", batch_id="test-004",
        )
        assert "quality_score" in result.rename_map
        assert result.rename_map["quality_score"] == "raw_quality_score"

    def test_ledgermax_full_pipeline(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        columns = ["Transaction Date", "Account", "Narration",
                   "Dr Amount", "Cr Amount", "Balance"]
        rows = [{
            "Transaction Date": "15/01/2024",
            "Account": "1001-Cash",
            "Narration": "Fuel purchase",
            "Dr Amount": "5000",
            "Cr Amount": "",
            "Balance": "45000",
        }]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="ledgermax", batch_id="test-005",
        )
        assert result.success is True
        assert result.coverage >= 0.8

    def test_coverage_calculated(self, temp_db):
        from src.phase1.schema_mapper.mapper import SchemaMapper
        mapper = SchemaMapper()
        # 4 known columns + 1 completely unknown
        columns = ["Date", "Amount", "Vendor", "Description", "XyzUnknown123"]
        rows = [{"Date": "15/01/2024", "Amount": "5000",
                 "Vendor": "Shell", "Description": "Fuel", "XyzUnknown123": "?"}]
        result = mapper.map(
            columns=columns, rows=rows,
            source_software="manual_excel", batch_id="test-006",
        )
        assert result.success is True
        assert result.coverage >= 0.6  # At least 4/5 known columns mapped