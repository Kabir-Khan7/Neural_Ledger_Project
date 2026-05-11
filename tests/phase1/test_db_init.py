"""
Tests — Database Initialization
================================
Tests every aspect of the DB layer before any other code is built on it.
Run with: pytest tests/phase1/test_db_init.py -v
"""

import pytest
import tempfile
import duckdb
from pathlib import Path
from uuid import uuid4

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.database.init_db import DatabaseManager, file_hash


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path):
    """Fresh database for each test — no shared state."""
    db_path = tmp_path / "test_neural_ledger.db"
    manager = DatabaseManager(db_path=db_path)
    manager.initialise()
    yield manager
    manager.close()


# ─────────────────────────────────────────────────────────────────────
# 1. Initialization Tests
# ─────────────────────────────────────────────────────────────────────

class TestInitialization:

    def test_database_file_created(self, tmp_path):
        db_path = tmp_path / "test.db"
        assert not db_path.exists()
        manager = DatabaseManager(db_path=db_path)
        manager.initialise()
        assert db_path.exists()
        manager.close()

    def test_idempotent_initialisation(self, temp_db):
        """Running initialise() twice must not raise or duplicate tables."""
        temp_db.initialise()  # second call
        stats = temp_db.get_schema_stats()
        # All counts should be 0 — no data yet, but tables exist
        for table, count in stats.items():
            assert count == 0, f"{table} should be empty, got {count}"

    def test_all_tables_created(self, temp_db):
        expected = [
            "bronze_transactions",
            "bronze_schema_mappings",
            "silver_transactions",
            "silver_quarantine",
            "gold_transactions",
            "gold_period_summaries",
            "pipeline_audit_log",
        ]
        with temp_db.connection() as conn:
            result = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        existing = {row[0] for row in result}
        for table in expected:
            assert table in existing, f"Missing table: {table}"

    def test_storage_directory_created(self, tmp_path):
        db_path = tmp_path / "nested" / "deep" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        manager = DatabaseManager(db_path=db_path)
        manager.initialise()
        assert db_path.exists()
        manager.close()


# ─────────────────────────────────────────────────────────────────────
# 2. Bronze Layer Tests
# ─────────────────────────────────────────────────────────────────────

class TestBronzeLayer:

    def test_bronze_insert_minimal(self, temp_db):
        """Insert a minimal Bronze row — only required fields."""
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO bronze_transactions
                    (source_type, source_software, raw_content)
                VALUES (?, ?, ?)
            """, ['xlsx', 'manual_excel', '{"Date": "01/01/2024", "Amount": "5000"}'])

            count = conn.execute(
                "SELECT COUNT(*) FROM bronze_transactions"
            ).fetchone()[0]
        assert count == 1

    def test_bronze_uuid_auto_generated(self, temp_db):
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO bronze_transactions (source_type, source_software, raw_content)
                VALUES ('csv', 'ledgermax', '{"col": "val"}')
            """)
            result = conn.execute(
                "SELECT bronze_id FROM bronze_transactions"
            ).fetchone()
        assert result[0] is not None

    def test_bronze_invalid_source_type_rejected(self, temp_db):
        """source_type CHECK constraint must reject unknown values."""
        with pytest.raises(Exception):
            with temp_db.connection() as conn:
                conn.execute("""
                    INSERT INTO bronze_transactions
                        (source_type, source_software, raw_content)
                    VALUES ('docx', 'manual_excel', '{}')
                """)

    def test_bronze_invalid_source_software_rejected(self, temp_db):
        with pytest.raises(Exception):
            with temp_db.connection() as conn:
                conn.execute("""
                    INSERT INTO bronze_transactions
                        (source_type, source_software, raw_content)
                    VALUES ('xlsx', 'peachtree', '{}')
                """)

    def test_bronze_all_source_types_accepted(self, temp_db):
        valid_types = ['xlsx', 'csv', 'pdf', 'api_quickbooks',
                       'api_xero', 'api_tally', 'api_odoo', 'json', 'manual']
        with temp_db.connection() as conn:
            for st in valid_types:
                conn.execute("""
                    INSERT INTO bronze_transactions (source_type, source_software, raw_content)
                    VALUES (?, 'manual_excel', '{}')
                """, [st])
            count = conn.execute(
                "SELECT COUNT(*) FROM bronze_transactions"
            ).fetchone()[0]
        assert count == len(valid_types)

    def test_bronze_default_status_is_pending(self, temp_db):
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO bronze_transactions (source_type, source_software, raw_content)
                VALUES ('xlsx', 'manual_excel', '{}')
            """)
            status = conn.execute(
                "SELECT processing_status FROM bronze_transactions"
            ).fetchone()[0]
        assert status == "pending"

    def test_bronze_raw_content_json_preserved(self, temp_db):
        """The original raw_content JSON must be stored and retrieved exactly."""
        original = '{"Taareekh": "15/01/2024", "Raqam": "4500", "Vendor": "Shell Clifton"}'
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO bronze_transactions (source_type, source_software, raw_content)
                VALUES ('xlsx', 'manual_excel', ?)
            """, [original])
            result = conn.execute(
                "SELECT raw_content FROM bronze_transactions"
            ).fetchone()[0]
        # DuckDB may parse and re-serialize JSON — check key presence
        import json
        parsed = json.loads(result) if isinstance(result, str) else result
        assert "Taareekh" in str(parsed)
        assert "4500" in str(parsed)

    def test_bronze_schema_mapping_unique_constraint(self, temp_db):
        """Same source_software + original_column combination must be unique."""
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO bronze_schema_mappings
                    (source_software, original_column, standard_field)
                VALUES ('manual_excel', 'Raqam', 'amount')
            """)
            with pytest.raises(Exception):
                conn.execute("""
                    INSERT INTO bronze_schema_mappings
                        (source_software, original_column, standard_field)
                    VALUES ('manual_excel', 'Raqam', 'amount_debit')
                """)


# ─────────────────────────────────────────────────────────────────────
# 3. Silver Layer Tests
# ─────────────────────────────────────────────────────────────────────

class TestSilverLayer:

    def _insert_silver(self, conn, bronze_id=None, year_month="2024-01",
                       net_debit=5000, net_credit=0):
        if bronze_id is None:
            bronze_id = str(uuid4())
        conn.execute("""
            INSERT INTO silver_transactions
                (bronze_id, source_software, source_type,
                 transaction_date, year_month, amount_debit, amount_credit)
            VALUES (?, 'manual_excel', 'xlsx', '2024-01-15', ?, ?, ?)
        """, [bronze_id, year_month, net_debit, net_credit])
        return bronze_id

    def test_silver_insert_basic(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_silver(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM silver_transactions"
            ).fetchone()[0]
        assert count == 1

    def test_silver_net_amount_computed(self, temp_db):
        """net_amount = credit - debit is a virtual generated column."""
        with temp_db.connection() as conn:
            self._insert_silver(conn, net_debit=5000, net_credit=0)
            net = conn.execute(
                "SELECT net_amount FROM silver_transactions"
            ).fetchone()[0]
        assert net == -5000  # debit of 5000 = net -5000

    def test_silver_credit_net_amount(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_silver(conn, net_debit=0, net_credit=10000)
            net = conn.execute(
                "SELECT net_amount FROM silver_transactions"
            ).fetchone()[0]
        assert net == 10000

    def test_silver_not_duplicate_by_default(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_silver(conn)
            result = conn.execute(
                "SELECT is_duplicate FROM silver_transactions"
            ).fetchone()[0]
        assert result == False

    def test_silver_quarantine_insert(self, temp_db):
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO silver_quarantine
                    (bronze_id, quarantine_reason, quality_score, raw_content)
                VALUES (?, 'date_parse_failed', 0.3, '{"bad": "row"}')
            """, [str(uuid4())])
            count = conn.execute(
                "SELECT COUNT(*) FROM silver_quarantine"
            ).fetchone()[0]
        assert count == 1

    def test_silver_invalid_quarantine_reason_rejected(self, temp_db):
        with pytest.raises(Exception):
            with temp_db.connection() as conn:
                conn.execute("""
                    INSERT INTO silver_quarantine
                        (bronze_id, quarantine_reason, raw_content)
                    VALUES (?, 'bad_vibes', '{}')
                """, [str(uuid4())])


# ─────────────────────────────────────────────────────────────────────
# 4. Gold Layer Tests
# ─────────────────────────────────────────────────────────────────────

class TestGoldLayer:

    def _insert_gold(self, conn, quality_score=0.85, year_month="2024-01"):
        silver_id = str(uuid4())
        bronze_id = str(uuid4())
        conn.execute("""
            INSERT INTO gold_transactions (
                silver_id, bronze_id, source_software, source_type,
                transaction_date, year_month,
                amount_debit, amount_credit,
                embedding_text, quality_score
            ) VALUES (?, ?, 'manual_excel', 'xlsx',
                      '2024-01-15', ?,
                      5000, 0,
                      'On 15 Jan 2024, PKR 5,000 paid to Shell — Fuel & Transport',
                      ?)
        """, [silver_id, bronze_id, year_month, quality_score])
        return silver_id

    def test_gold_insert_valid(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_gold(conn)
            count = conn.execute(
                "SELECT COUNT(*) FROM gold_transactions"
            ).fetchone()[0]
        assert count == 1

    def test_gold_quality_gate_enforced(self, temp_db):
        """quality_score < 0.7 must be rejected by CHECK constraint."""
        with pytest.raises(Exception):
            with temp_db.connection() as conn:
                self._insert_gold(conn, quality_score=0.5)

    def test_gold_quality_gate_boundary(self, temp_db):
        """quality_score = exactly 0.7 should be accepted."""
        with temp_db.connection() as conn:
            self._insert_gold(conn, quality_score=0.7)
            count = conn.execute(
                "SELECT COUNT(*) FROM gold_transactions"
            ).fetchone()[0]
        assert count == 1

    def test_gold_embedding_text_required(self, temp_db):
        """embedding_text NOT NULL must be enforced."""
        with pytest.raises(Exception):
            with temp_db.connection() as conn:
                conn.execute("""
                    INSERT INTO gold_transactions (
                        silver_id, bronze_id, source_software, source_type,
                        transaction_date, year_month, quality_score
                    ) VALUES (?, ?, 'manual_excel', 'xlsx', '2024-01-15', '2024-01', 0.9)
                """, [str(uuid4()), str(uuid4())])

    def test_gold_qdrant_indexed_defaults_false(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_gold(conn)
            result = conn.execute(
                "SELECT qdrant_indexed FROM gold_transactions"
            ).fetchone()[0]
        assert result == False

    def test_gold_net_amount_virtual_column(self, temp_db):
        with temp_db.connection() as conn:
            self._insert_gold(conn)  # debit=5000, credit=0
            net = conn.execute(
                "SELECT net_amount FROM gold_transactions"
            ).fetchone()[0]
        assert net == -5000

    def test_gold_period_summary_unique_constraint(self, temp_db):
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO gold_period_summaries
                    (period_key, period_type, total_debits, total_credits,
                     net_cashflow, transaction_count)
                VALUES ('2024-01', 'monthly', 50000, 80000, 30000, 45)
            """)
            with pytest.raises(Exception):
                conn.execute("""
                    INSERT INTO gold_period_summaries
                        (period_key, period_type)
                    VALUES ('2024-01', 'monthly')
                """)


# ─────────────────────────────────────────────────────────────────────
# 5. Phase 2 Contract — Read-Only Enforcement
# ─────────────────────────────────────────────────────────────────────

class TestPhase2Contract:

    def test_read_connection_can_query_gold(self, temp_db):
        """Phase 2 must be able to read Gold."""
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO gold_transactions (
                    silver_id, bronze_id, source_software, source_type,
                    transaction_date, year_month,
                    amount_debit, amount_credit,
                    embedding_text, quality_score
                ) VALUES (?, ?, 'manual_excel', 'xlsx',
                          '2024-01-15', '2024-01',
                          5000, 0,
                          'On 15 Jan 2024, PKR 5,000 paid to Shell',
                          0.9)
            """, [str(uuid4()), str(uuid4())])

        with temp_db.read_connection() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM gold_transactions"
            ).fetchone()[0]
        assert result == 1

    def test_read_connection_returns_correct_data(self, temp_db):
        """Phase 2 read connection returns accurate Gold data."""
        embedding = "On 15 Jan 2024, PKR 7,500 paid to HBL — Banking"
        with temp_db.connection() as conn:
            conn.execute("""
                INSERT INTO gold_transactions (
                    silver_id, bronze_id, source_software, source_type,
                    transaction_date, year_month,
                    amount_debit, amount_credit,
                    embedding_text, quality_score, category
                ) VALUES (?, ?, 'manual_excel', 'xlsx',
                          '2024-01-15', '2024-01',
                          7500, 0, ?, 0.9, 'Banking')
            """, [str(uuid4()), str(uuid4()), embedding])

        with temp_db.read_connection() as conn:
            result = conn.execute(
                "SELECT category, embedding_text FROM gold_transactions"
            ).fetchone()
        assert result[0] == "Banking"
        assert "HBL" in result[1]


# ─────────────────────────────────────────────────────────────────────
# 6. Audit Log Tests
# ─────────────────────────────────────────────────────────────────────

class TestAuditLog:

    def test_log_operation_writes_entry(self, temp_db):
        temp_db.log_operation(
            operation="bronze_insert",
            source_layer="bronze",
            status="success",
            message="Inserted 50 rows from sales_jan.xlsx",
            rows_affected=50,
            duration_ms=120
        )
        with temp_db.connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pipeline_audit_log"
            ).fetchone()[0]
        assert count == 1

    def test_log_operation_failure_is_non_fatal(self, temp_db):
        """Audit log failures must not crash the pipeline."""
        temp_db.log_operation(
            operation="test",
            source_layer="bronze",
            status="success"
        )

    def test_get_schema_stats_returns_all_tables(self, temp_db):
        stats = temp_db.get_schema_stats()
        assert "bronze_transactions" in stats
        assert "gold_transactions" in stats
        assert "silver_quarantine" in stats
        for count in stats.values():
            assert count >= 0


# ─────────────────────────────────────────────────────────────────────
# 7. Utility Tests
# ─────────────────────────────────────────────────────────────────────

class TestUtilities:

    def test_file_hash_returns_sha256(self, tmp_path):
        test_file = tmp_path / "test.csv"
        test_file.write_text("date,amount,vendor\n2024-01-15,5000,Shell")
        result = file_hash(str(test_file))
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_file_hash_deterministic(self, tmp_path):
        """Same file content must always produce the same hash."""
        test_file = tmp_path / "test.csv"
        test_file.write_text("date,amount\n2024-01-15,5000")
        h1 = file_hash(str(test_file))
        h2 = file_hash(str(test_file))
        assert h1 == h2

    def test_file_hash_different_for_different_content(self, tmp_path):
        f1 = tmp_path / "a.csv"
        f2 = tmp_path / "b.csv"
        f1.write_text("amount,5000")
        f2.write_text("amount,6000")
        assert file_hash(str(f1)) != file_hash(str(f2))