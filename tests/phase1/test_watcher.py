"""
Tests — Step 2: File Watcher & Detector
=========================================
Run with: python -m pytest tests/phase1/test_watcher.py -v
"""

import time
import shutil
import pytest
import threading
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.phase1.ingestion.detector import detect_file, is_supported, is_ignored
from src.phase1.watcher import DataVaultWatcher, DebounceTimer, FileProcessor
from src.database.init_db import DatabaseManager


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_vault(tmp_path):
    vault = tmp_path / "data_vault"
    vault.mkdir()
    return vault

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(db_path=db_path)
    manager.initialise()
    yield manager
    manager.close()

@pytest.fixture
def sample_xlsx(tmp_path):
    f = tmp_path / "transactions_jan_2024.xlsx"
    f.write_bytes(b"PK\x03\x04fake_excel_content")  # Excel magic bytes
    return f

@pytest.fixture
def sample_csv(tmp_path):
    f = tmp_path / "ledger_export.csv"
    f.write_text("Date,Amount,Vendor\n2024-01-15,5000,Shell\n")
    return f

@pytest.fixture
def sample_pdf(tmp_path):
    f = tmp_path / "HBL_Statement_Jan2024.pdf"
    f.write_bytes(b"%PDF-1.4 fake pdf content")
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 1. Detector Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDetector:

    def test_excel_manual_detected(self):
        source_type, software, conf = detect_file("monthly_accounts.xlsx")
        assert source_type == "xlsx"
        assert software == "manual_excel"
        assert conf > 0

    def test_tally_detected_from_filename(self):
        source_type, software, conf = detect_file("TallyPrime_Export_2024.xlsx")
        assert source_type == "xlsx"
        assert software == "tally"
        assert conf >= 0.85

    def test_quickbooks_detected(self):
        _, software, conf = detect_file("QuickBooks_Transaction_Detail.xlsx")
        assert software == "quickbooks_desktop"
        assert conf >= 0.70

    def test_ledgermax_detected(self):
        _, software, conf = detect_file("LedgerMax_Report_2024.xlsx")
        assert software == "ledgermax"
        assert conf >= 0.90

    def test_hbl_bank_statement(self):
        source_type, software, conf = detect_file("HBL_Statement_Jan2024.pdf")
        assert source_type == "pdf"
        assert software == "bank_pdf_hbl"
        assert conf >= 0.85

    def test_ubl_bank_statement(self):
        _, software, _ = detect_file("UBL_Account_Statement_2024.pdf")
        assert software == "bank_pdf_ubl"

    def test_alfalah_bank_statement(self):
        _, software, _ = detect_file("Bank_Alfalah_eStatement.pdf")
        assert software == "bank_pdf_alfalah"

    def test_meezan_bank_statement(self):
        _, software, _ = detect_file("Meezan_Bank_Statement.pdf")
        assert software == "bank_pdf_meezan"

    def test_generic_bank_pdf(self):
        source_type, software, _ = detect_file("account_statement_dec.pdf")
        assert source_type == "pdf"
        assert "bank_pdf" in software

    def test_csv_unknown(self):
        source_type, _, _ = detect_file("data.csv")
        assert source_type == "csv"

    def test_xls_extension(self):
        source_type, _, _ = detect_file("old_ledger.xls")
        assert source_type == "xlsx"  # xls maps to xlsx source_type

    def test_json_detected(self):
        source_type, _, _ = detect_file("api_export.json")
        assert source_type == "json"

    def test_case_insensitive_tally(self):
        _, software, _ = detect_file("tallyprime_daybook.xlsx")
        assert software == "tally"

    def test_xero_detected(self):
        _, software, conf = detect_file("Xero_Export_Jan2024.csv")
        assert software == "xero"
        assert conf >= 0.85


class TestIsSupported:

    def test_xlsx_supported(self):
        assert is_supported("report.xlsx") is True

    def test_xls_supported(self):
        assert is_supported("report.xls") is True

    def test_csv_supported(self):
        assert is_supported("data.csv") is True

    def test_pdf_supported(self):
        assert is_supported("statement.pdf") is True

    def test_json_supported(self):
        assert is_supported("export.json") is True

    def test_docx_not_supported(self):
        assert is_supported("report.docx") is False

    def test_txt_not_supported(self):
        assert is_supported("notes.txt") is False

    def test_pptx_not_supported(self):
        assert is_supported("slides.pptx") is False


class TestIsIgnored:

    def test_temp_file_ignored(self):
        assert is_ignored("data.tmp") is True

    def test_excel_lock_file_ignored(self):
        assert is_ignored("~$report.xlsx") is True

    def test_hidden_file_ignored(self):
        assert is_ignored(".hidden_file.xlsx") is True

    def test_regular_excel_not_ignored(self):
        assert is_ignored("report.xlsx") is False

    def test_regular_csv_not_ignored(self):
        assert is_ignored("transactions.csv") is False

    def test_bak_file_ignored(self):
        assert is_ignored("ledger.bak") is True

    def test_swp_file_ignored(self):
        assert is_ignored(".ledger.xlsx.swp") is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. Debounce Timer Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDebounceTimer:

    def test_callback_fires_after_delay(self):
        fired = []

        def callback(path):
            fired.append(path)

        timer = DebounceTimer(callback=callback)
        timer.DEBOUNCE_SECONDS = 0.1  # Fast for testing
        timer.schedule("test_file.xlsx")

        time.sleep(0.3)
        assert "test_file.xlsx" in fired

    def test_rapid_events_fire_once(self):
        """Multiple rapid events for same file should result in one callback."""
        fired = []

        def callback(path):
            fired.append(path)

        timer = DebounceTimer(callback=callback)
        timer.DEBOUNCE_SECONDS = 0.1

        # Simulate multiple rapid events
        for _ in range(5):
            timer.schedule("busy_file.xlsx")
            time.sleep(0.02)

        time.sleep(0.3)
        assert fired.count("busy_file.xlsx") == 1

    def test_cancel_prevents_callback(self):
        fired = []

        def callback(path):
            fired.append(path)

        timer = DebounceTimer(callback=callback)
        timer.DEBOUNCE_SECONDS = 0.2
        timer.schedule("cancelled_file.xlsx")
        timer.cancel_all()

        time.sleep(0.4)
        assert len(fired) == 0

    def test_different_files_each_fire(self):
        """Two different files should each trigger their own callback."""
        fired = []

        def callback(path):
            fired.append(path)

        timer = DebounceTimer(callback=callback)
        timer.DEBOUNCE_SECONDS = 0.1
        timer.schedule("file_a.xlsx")
        timer.schedule("file_b.csv")

        time.sleep(0.4)
        assert "file_a.xlsx" in fired
        assert "file_b.csv" in fired


# ─────────────────────────────────────────────────────────────────────────────
# 3. File Processor Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFileProcessor:

    def test_valid_file_triggers_callback(self, sample_csv, temp_db):
        received = []

        def on_queued(meta):
            received.append(meta)

        processor = FileProcessor(on_queued=on_queued)
        processor.process(str(sample_csv))

        assert len(received) == 1
        assert received[0]["file_name"] == sample_csv.name
        assert received[0]["source_type"] == "csv"

    def test_metadata_complete(self, sample_xlsx, temp_db):
        received = []

        processor = FileProcessor(on_queued=lambda m: received.append(m))
        processor.process(str(sample_xlsx))

        assert len(received) == 1
        meta = received[0]
        assert "file_path" in meta
        assert "file_name" in meta
        assert "source_type" in meta
        assert "source_software" in meta
        assert "detection_conf" in meta
        assert "file_hash" in meta
        assert "file_size_bytes" in meta
        assert "detected_at" in meta

    def test_empty_file_skipped(self, tmp_path, temp_db):
        empty = tmp_path / "empty.csv"
        empty.write_text("")

        received = []
        processor = FileProcessor(on_queued=lambda m: received.append(m))
        processor.process(str(empty))

        assert len(received) == 0

    def test_missing_file_skipped(self, temp_db):
        received = []
        processor = FileProcessor(on_queued=lambda m: received.append(m))
        processor.process("/nonexistent/path/file.csv")

        assert len(received) == 0

    def test_duplicate_file_skipped(self, sample_csv, temp_db):
        """Same file dropped twice should only be processed once."""
        received = []
        processor = FileProcessor(on_queued=lambda m: received.append(m))

        processor.process(str(sample_csv))
        processor.process(str(sample_csv))  # Second drop — same hash

        assert len(received) == 1

    def test_hbl_pdf_detected_correctly(self, sample_pdf, temp_db):
        received = []
        processor = FileProcessor(on_queued=lambda m: received.append(m))
        processor.process(str(sample_pdf))

        assert len(received) == 1
        assert received[0]["source_software"] == "bank_pdf_hbl"

    def test_processor_survives_error(self, tmp_path, temp_db):
        """Processor must not raise — errors are logged silently."""
        # Provide a path that will cause an error during processing
        processor = FileProcessor(on_queued=None)
        # Should not raise
        processor.process("/invalid/\x00/path.csv")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Watcher Integration Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDataVaultWatcher:

    def test_watcher_starts_and_stops(self, temp_vault, temp_db):
        watcher = DataVaultWatcher(vault_path=str(temp_vault))
        watcher.start()
        assert watcher.is_running() is True
        watcher.stop()
        assert watcher.is_running() is False

    def test_vault_folder_created_if_missing(self, tmp_path, temp_db):
        new_vault = tmp_path / "new_vault_that_doesnt_exist"
        assert not new_vault.exists()
        watcher = DataVaultWatcher(vault_path=str(new_vault))
        assert new_vault.exists()

    def test_file_detected_on_creation(self, temp_vault, temp_db):
        received = []

        watcher = DataVaultWatcher(
            vault_path=str(temp_vault),
            on_file_ready=lambda m: received.append(m),
        )
        watcher.start()

        # Drop a file
        test_file = temp_vault / "transactions.csv"
        test_file.write_text("Date,Amount\n2024-01-15,5000\n")

        # Wait for debounce + processing
        time.sleep(3.5)
        watcher.stop()

        assert len(received) == 1
        assert received[0]["file_name"] == "transactions.csv"

    def test_temp_file_not_detected(self, temp_vault, temp_db):
        received = []

        watcher = DataVaultWatcher(
            vault_path=str(temp_vault),
            on_file_ready=lambda m: received.append(m),
        )
        watcher.start()

        # Drop a temp file
        temp_file = temp_vault / "~$report.xlsx"
        temp_file.write_bytes(b"temp content")

        time.sleep(3.5)
        watcher.stop()

        assert len(received) == 0

    def test_startup_scan_finds_existing_files(self, temp_vault, temp_db):
        # Place a file in vault before watcher starts
        existing = temp_vault / "old_ledger.csv"
        existing.write_text("Date,Amount\n2024-01-01,1000\n")

        received = []
        watcher = DataVaultWatcher(
            vault_path=str(temp_vault),
            on_file_ready=lambda m: received.append(m),
        )
        watcher.start()
        count = watcher.scan_existing()

        time.sleep(0.5)
        watcher.stop()

        assert count == 1
        assert len(received) == 1

    def test_multiple_files_all_detected(self, temp_vault, temp_db):
        received = []

        watcher = DataVaultWatcher(
            vault_path=str(temp_vault),
            on_file_ready=lambda m: received.append(m),
        )
        watcher.start()

        (temp_vault / "sales.csv").write_text("Date,Amount\n2024-01-01,1000\n")
        time.sleep(0.5)
        (temp_vault / "expenses.csv").write_text("Date,Amount\n2024-01-02,500\n")

        time.sleep(3.5)
        watcher.stop()

        assert len(received) == 2